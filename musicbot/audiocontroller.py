import sys
import asyncio
from dataclasses import dataclass
from itertools import islice
from inspect import isawaitable
from traceback import print_exc
from typing import (
    TYPE_CHECKING,
    Coroutine,
    Iterable,
    List,
    Optional,
    Union,
)

import discord
from config import config

from musicbot import loader, playlists, utils
from musicbot.song import PartialPlaylist, Song
from musicbot.truncation import Truncation
from musicbot.linkutils import SiteTypes
from musicbot.playback import Playback, PlaybackState
from musicbot.playlist import Playlist, LoopMode, LoopState
from musicbot.utils import (
    CheckError,
    asset,
    player_check,
    dj_check,
)
from pathlib import Path
import pickle
from enum import Enum, auto

# avoiding circular import
if TYPE_CHECKING:
    from musicbot.bot import MusicBot


@dataclass
class QueuedPlaylist:
    """process_song()'s result for a multi-song load. `truncation` is
    set when the playlist was cut short."""

    queued: int
    truncation: Optional[Truncation] = None

    def notice(self) -> Optional[str]:
        """The truncation notice for the queued tracks, or `None` when
        nothing was cut."""
        if self.truncation is None:
            return None
        return self.truncation.notice(self.queued)


_not_provided = object()


class MusicButton(discord.ui.Button):
    """A button running `callback` once `check` admits the click.
    A player button never touches voice: its default check,
    player_check(), refuses it while the bot is out of voice. A
    pick acting in a joining command's name (a search pick) overrides
    join() to join voice (connect, or move the bot) before its
    action."""

    def __init__(self, callback, check=player_check, **kwargs):
        super().__init__(**kwargs)
        self._callback = callback
        self._check = check

    async def join(self, ctx):
        """Awaited once every check has passed, before the action.
        Does nothing by default; a CheckError from it refuses the
        click like a failed check."""

    async def callback(self, inter: discord.Interaction):
        # Build ctx first, then acknowledge: get_context makes no HTTP
        # call for an interaction (Context.from_interaction awaits
        # nothing), so awaiting it ahead of the defer cannot delay the
        # response, and a failure at any later step still has a ctx to
        # report through. Acknowledge before the checks and the join:
        # a join can be slow, and it must not push the response past
        # Discord's 3-second deadline. Refusals then go out as
        # ephemeral followups. join() runs only once every check has
        # passed, so a refused click never makes the bot join.
        ctx = await inter.client.get_context(inter)
        try:
            await inter.response.defer()
            try:
                await self._check(ctx)
                if inter.data.get("custom_id") in [
                    "prev",
                    "pause",
                    "next",
                    "loop",
                    "shuffle",
                    "stop",
                    "volume_down",
                    "volume_up",
                ]:
                    await dj_check(ctx)
                await self.join(ctx)
            except CheckError as e:
                self.on_refused()
                await ctx.send(e, ephemeral=True)
                return
            res = self._callback(ctx)
            if isawaitable(res):
                await res

            controller = inter.client.sessions.controller(inter.guild)
            if controller:
                if inter.data.get("custom_id") in ["next", "prev"]:
                    await ctx.send(f"{inter.user} Skipped a Song")
                else:
                    await controller.update_view()
        except Exception as e:
            await self.on_failure(ctx, e)

    async def on_failure(self, ctx, error):
        """Called for any failure that is not a refusal: a failed
        defer, or an exception (a CheckError included) from the
        action or the controller update. Re-raises by default."""
        raise error

    def on_refused(self):
        """Called for every CheckError that refuses a click (its
        check, the DJ check or a failed voice join), before the
        ephemeral refusal goes out. Does nothing by default."""


class RestoreResult(Enum):
    """What AudioController.restore() did."""

    RESTORED = auto()
    NOTHING_TO_RESTORE = auto()
    REFUSED_WHILE_ACTIVE = auto()


class AudioController(object):
    """Controls the playback of audio and the sequential playing of the songs.

    Attributes:
        bot: The instance of the bot that will be playing the music.
        playlist: A Playlist object that stores the history and queue of songs.
        current_song: A Song object that stores details of the current song.
        guild: The guild in which the Audiocontroller operates.
    """

    def __init__(self, bot: "MusicBot", guild: discord.Guild):
        self.bot = bot
        self.playlist = Playlist()
        self.pickle_file = Path("backup") / f"playlist_{guild.id}.pickle"
        self.pickle_file.parent.mkdir(parents=True, exist_ok=True)
        self.guild = guild

        sett = bot.sessions.settings(guild)
        self._volume: int = sett.default_volume

        self.timer = utils.Timer(self.timeout_handler)

        self.playback = Playback(
            voice_client=lambda: self.guild.voice_client,
            preload=self._preload,
            make_source=self._make_source,
            next_track=lambda forced: self.playlist.next(forced),
            clear_queue=self._clear_queue,
            schedule_preload=self._schedule_preload,
            announce=self._announce,
            report_skipped=self._report_skipped,
            record_history=lambda name: self.playlist.add_name(name),
            write_backup=self._write_backup,
            queue_is_empty=lambda: not self.playlist,
            spawn=self.add_task,
            start_timer=lambda restart: self.timer.start(restart),
            timer_triggered=lambda: self.timer.triggered,
            humans_present=self._humans_present,
            on_play_failed=lambda: self.udisconnect("playback error"),
            get_loop=lambda: self.bot.loop,
            guild_name=guild.name,
            guild_id=guild.id,
        )

        self.command_channel: Optional[discord.abc.Messageable] = None

        self._last_message = None
        self._last_view = None

        # according to Python documentation, we need
        # to keep strong references to all tasks
        self._tasks = set()

        self.message_lock = asyncio.Lock()

    @property
    def current_song(self) -> Optional[Song]:
        if self.is_active():
            return self.playlist[0]
        return None

    @property
    def volume(self) -> int:
        return self._volume

    def set_volume(self, value: int):
        """Sets the volume, in percent, for this and every later track"""
        self._volume = value
        try:
            self.guild.voice_client.source.volume = float(value) / 100.0
        except AttributeError:
            pass
        except Exception:
            print("Unknown error when setting volume:", file=sys.stderr)
            print_exc(file=sys.stderr)

    def _write_backup(self):
        with open(self.pickle_file, "wb") as f:
            pickle.dump(self.playlist, f)

    def _humans_present(self) -> bool:
        return not all(m.bot for m in self.guild.voice_client.channel.members)

    def _clear_queue(self):
        self.playlist.loop = LoopMode.OFF
        self.playlist.clear()
        self.playlist.next()

    def _make_source(self, song: Song):
        before_options = (
            None
            if song.host == SiteTypes.LOCAL_LIBRARY
            else "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        )
        return discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                song.url,
                before_options=before_options,
                options="-loglevel error",
                stderr=sys.stderr,
            ),
            float(self.volume) / 100.0,
        )

    async def _announce(self, song: Song):
        if (
            self.bot.sessions.settings(self.guild).announce_songs
            and self.command_channel
        ):
            await self.command_channel.send(
                embed=song.format_output(config.SONGINFO_NOW_PLAYING)
            )

    async def _report_skipped(self, song: Song):
        if self.command_channel:
            await self.command_channel.send(
                f"{config.SONGINFO_ERROR}\n" f"(Skipped: {song.display_name})"
            )

    def load_pickle_playlist(self):
        if self.pickle_file.exists():
            with open(self.pickle_file, "rb") as f:
                self.playlist = pickle.load(f)

    def volume_up(self):
        self.set_volume(min(self.volume + 10, 100))

    def volume_down(self):
        self.set_volume(max(self.volume - 10, 0))

    async def register_voice_channel(self, channel: discord.VoiceChannel):
        """The controller's voice entry point: GuildSessions (whose
        auto-join calls it) and the button plugin connect or move
        through it. GuildSessions is generic over its controller and
        imports neither discord nor the voice utilities, and its
        tests fake exactly this method, so callers do not inline
        connect_to."""
        await utils.connect_to(self.guild, channel)

    def make_view(self):
        if not self.is_active():
            return None

        is_empty = len(self.playlist) == 0

        view = discord.ui.View(timeout=None)
        view.add_item(
            MusicButton(
                lambda _: self.prev_song(),
                custom_id="prev",
                disabled=not self.playlist.has_prev(),
                emoji="⏮️",
            )
        )
        view.add_item(
            MusicButton(
                lambda _: self.pause(),
                custom_id="pause",
                emoji="⏸️" if self.guild.voice_client.is_playing() else "▶️",
            )
        )
        view.add_item(
            MusicButton(
                lambda _: self.skip(),
                custom_id="next",
                disabled=not self.playlist.has_next(),
                emoji="⏭️",
            )
        )
        view.add_item(
            MusicButton(
                lambda _: self.loop(),
                custom_id="loop",
                disabled=is_empty,
                emoji="🔁",
                label="Loop: " + self.playlist.loop,
            )
        )
        view.add_item(
            MusicButton(
                self.current_song_callback,
                custom_id="current_song",
                row=1,
                disabled=self.current_song is None,
                emoji="💿",
            )
        )
        view.add_item(
            MusicButton(
                lambda _: self.shuffle(),
                custom_id="shuffle",
                row=1,
                disabled=is_empty,
                emoji="🔀",
            )
        )
        view.add_item(
            MusicButton(
                self.queue_callback,
                custom_id="queue",
                row=1,
                disabled=is_empty,
                emoji="📜",
            )
        )
        view.add_item(
            MusicButton(
                lambda _: self.stop(),
                custom_id="stop",
                row=1,
                emoji="⏹️",
                style=discord.ButtonStyle.red,
            )
        )
        view.add_item(
            MusicButton(
                lambda _: self.volume_down(),
                custom_id="volume_down",
                row=2,
                disabled=self.volume == 0,
                emoji="🔉",
            )
        )
        view.add_item(
            MusicButton(
                lambda _: self.volume_up(),
                custom_id="volume_up",
                row=2,
                disabled=self.volume == 100,
                emoji="🔊",
            )
        )

        return view

    async def current_song_callback(self, ctx):
        if self.current_song is None:
            await ctx.send(config.QUEUE_EMPTY)
            return
        await ctx.send(
            embed=self.current_song.format_output(config.SONGINFO_SONGINFO),
        )

    async def queue_callback(self, ctx):
        await ctx.send(
            embed=self.playlist.queue_embed(),
        )

    async def attach_view(self, send):
        """Moves the playback buttons onto the message `send` sends.

        `send(view)` is an async callable that sends the message,
        carrying `view` when it is not None, and returns what it sent.
        That becomes the message whose buttons later refreshes edit; an
        interaction stands for its original response. Returns what
        `send` returned.
        """
        async with self.message_lock:
            await self.update_view(None)
            view = self.make_view()
            res = await send(view)
            self._last_view = view
            if isinstance(res, discord.Interaction):
                self._last_message = await res.original_response()
            else:
                self._last_message = res
        return res

    async def update_view(self, view=_not_provided):
        msg = self._last_message
        if not msg:
            return
        old_view = self._last_view
        if view is None:
            self._last_message = None
        elif view is _not_provided:
            view = self.make_view()
        if view is old_view:
            return
        elif (
            old_view
            and view
            and old_view.to_components() == view.to_components()
        ):
            return
        try:
            await msg.edit(view=view)
            self._last_view = view
        except discord.HTTPException as e:
            if e.code == 50027:  # Invalid Webhook Token
                try:
                    self._last_message = await msg.channel.fetch_message(
                        msg.id
                    )
                    await self.update_view(view)
                except discord.NotFound:
                    self._last_message = None
            else:
                print("Failed to update view:", file=sys.stderr)
                print_exc(file=sys.stderr)

    def is_active(self) -> bool:
        client = self.guild.voice_client
        return client is not None and (
            client.is_playing() or client.is_paused()
        )

    def is_idle(self) -> bool:
        """True when a new track may be started: the voice client is
        neither playing nor paused and no start is pending. Every
        "start if idle" path asks this. Loop only."""
        return self.playback.state is PlaybackState.IDLE

    def track_history(self):
        history_string = config.INFO_HISTORY_TITLE
        for trackname in self.playlist.trackname_history:
            history_string += "\n" + trackname
        return history_string

    def pause(self):
        return self.playback.toggle_pause()

    def loop(self, mode=None):
        if mode is None:
            if self.playlist.loop == LoopMode.OFF:
                mode = LoopMode.ALL
            else:
                mode = LoopMode.OFF

        try:
            mode = LoopMode(mode)
        except ValueError:
            return LoopState.INVALID

        self.playlist.loop = mode

        if mode == LoopMode.OFF:
            return LoopState.DISABLED
        return LoopState.ENABLED

    def _queue_changed(self):
        """Refreshes the backup and the preload after the queue was
        edited in place."""
        self.playback.queue_changed()
        self._schedule_preload()

    def shuffle(self):
        self.playlist.shuffle()
        self._queue_changed()

    def move(self, src: int, dest: int):
        """Moves the queued track at index `src` to index `dest`
        (0 is the current track, which cannot be moved). Raises
        PlaylistError for an index that cannot be moved."""
        self.playlist.move(src, dest)
        self._queue_changed()

    def remove(self, index: int) -> Song:
        """Removes and returns the queued track at `index` (0 is the
        current track, which cannot be removed). Raises PlaylistError
        for an index that cannot be removed."""
        song = self.playlist.remove(index)
        self._queue_changed()
        return song

    def clear(self):
        """Empties the queue, keeping the current track. Nothing new
        is queued behind it, so there is nothing to preload."""
        self.playlist.clear()
        self.playback.queue_changed()

    def skip(self):
        """Ends the current track and moves on to the next one,
        ignoring a single-track loop"""
        self.next_song(forced=True)

    def next_song(self, *, forced=False):
        """Ends the current track and moves on to the next one. Runs
        on the event loop only - raises RuntimeError anywhere else."""
        self.playback.skip(forced)

    async def play_song(self, song: Song, generation: Optional[int] = None):
        """Plays a song object. `generation` is ignored: the start
        token is private to Playback."""
        await self.playback.start(song)

    async def ensure_playing(self):
        """Starts the head of the queue if nothing is playing and the
        queue is non-empty; otherwise preloads the queue. The one
        "start if idle" rule for everything that adds to the queue."""
        if self.is_idle() and len(self.playlist) > 0:
            await self.play_song(self.playlist[0])
        else:
            self._schedule_preload()

    async def enqueue_next(self, song: Song):
        """Puts `song` directly after the current track, refreshes the
        backup and starts playing if nothing is (see ensure_playing()).

        Placement happens before the first await, so no other command
        can slip between the song and its slot."""
        self.playlist.insert_next(song)
        self.playback.queue_changed()
        await self.ensure_playing()

    async def enqueue(self, songs: Iterable[Song]):
        """Appends `songs` to the queue, refreshes the backup once and
        starts playing if nothing is (see ensure_playing()).

        Placement happens before the first await, so the appended songs
        land together and in order."""
        for song in songs:
            self.playlist.add(song)
        self.playback.queue_changed()
        await self.ensure_playing()

    async def restore(self) -> RestoreResult:
        """Reloads the playlist backup and starts its head.

        Refuses while something is playing or paused, before touching
        the backup or the queue: starting the head then would make
        discord.py refuse play() and the controller disconnect (#24).
        Reports NOTHING_TO_RESTORE, playing nothing, when there is no
        queue to restore."""
        if not self.is_idle():
            return RestoreResult.REFUSED_WHILE_ACTIVE
        self.load_pickle_playlist()
        if not self.playlist:
            return RestoreResult.NOTHING_TO_RESTORE
        await self.play_song(self.playlist[0])
        return RestoreResult.RESTORED

    async def process_song(
        self,
        track: str,
        user: Optional[discord.abc.User] = None,
        playnext=False,
    ) -> Union[Song, QueuedPlaylist]:
        """Adds the track to the playlist instance
        Starts playing if it is the first song.

        With `playnext` a single track (or a one-item playlist) goes
        directly after the current track; a longer playlist is still
        appended.

        For many tracks at once use process_local_tracks() rather than
        calling this in a loop - pickling here on every call would mean
        serializing the whole, progressively larger playlist once per
        track, an O(n^2) blocking write."""

        # Logged on the way out rather than on the way in, so the
        # success cases below can name what the track resolved to
        # instead of echoing the link. A request that resolves to
        # nothing still has to leave a trace, though: a failed load
        # raises SongError, which the caller turns into a Discord reply
        # and nothing else.
        try:
            loaded_song = await loader.load_song(track)
        except loader.SongError:
            print(
                f"{user} queued {track!r} in guild {self.guild.name!r}"
                " - could not be loaded"
            )
            raise
        if isinstance(loaded_song, Song):
            added = [loaded_song]
            print(
                f"{user} queued {loaded_song.display_name!r}"
                f" by {loaded_song.uploader or 'unknown'}"
                f" ({loaded_song.host.name}) in guild {self.guild.name!r}"
            )
        else:
            added = list(loaded_song)
            count = len(loaded_song)
            if count == 1 and not isinstance(loaded_song, PartialPlaylist):
                # special-case one-item playlists
                loaded_song = loaded_song[0]
                print(
                    f"{user} queued {loaded_song.display_name!r}"
                    f" by {loaded_song.uploader or 'unknown'}"
                    f" ({loaded_song.host.name})"
                    f" in guild {self.guild.name!r}"
                )
            else:
                # a resolved playlist has no single title to show -
                # the input is the only useful identifier left
                print(
                    f"{user} queued a playlist ({count} track(s))"
                    f" via {track!r} in guild {self.guild.name!r}"
                )
                loaded_song = QueuedPlaylist(
                    count,
                    (
                        loaded_song.truncation
                        if isinstance(loaded_song, PartialPlaylist)
                        else None
                    ),
                )

        if playnext and len(added) == 1:
            await self.enqueue_next(added[0])
        else:
            await self.enqueue(added)

        return loaded_song

    async def process_local_tracks(
        self,
        tracks: List[str],
        source: str,
        user: Optional[discord.abc.User] = None,
    ) -> List[Optional[Song]]:
        """Queues a batch of local-library files and settles the
        playlist once at the end. Returns a list parallel to `tracks`,
        None wherever a file could not be loaded, so the caller can
        name what it skipped.

        `source` names the browse/search action that produced `tracks`
        (e.g. "browse: entire album Artist - Album") - unlike a single
        process_song() call, a file path alone doesn't say what the
        user actually picked, so the caller supplies it.

        The per-track equivalent is process_song() in a loop, which is
        what queueing an album or a discography from the library
        browser used to be: one IPC round trip through the
        load-lane loader process per track, and a fresh preload
        sweep per track on top - each of which walks up to
        MAX_SONG_PRELOAD songs, so a few hundred tracks meant a few
        thousand redundant iterations. Here the load is one call (two
        when it has to start playback first, see below), and the
        preload sweep happens once."""
        print(
            f"{user} queued {source} ({len(tracks)} track(s))"
            f" in guild {self.guild.name!r}"
        )

        songs: List[Optional[Song]] = []
        tail = tracks

        # With nothing playing, the first track is loaded and enqueued
        # on its own before the rest (this only splits the batch; the
        # start still goes through enqueue -> ensure_playing()).
        # Batching means the whole batch has to finish before any of
        # it is queued, where the per-track
        # path this replaced began on track one - a few hundred
        # milliseconds of silence for a discography on local disk, and
        # seconds of it on a network-mounted library. One extra round
        # trip buys that back; loading a single track costs about
        # 1.5ms.
        if self.is_idle() and len(tracks) > 1:
            songs += await loader.load_local_songs(tracks[:1])
            tail = tracks[1:]
            if songs[0] is not None:
                await self.enqueue(songs[:1])

        loaded_tail = await loader.load_local_songs(tail)
        songs += loaded_tail

        added = [song for song in loaded_tail if song is not None]
        if not added:
            return songs

        await self.enqueue(added)

        return songs

    def add_task(self, coro: Coroutine):
        # loop only: discord.py's audio thread never reaches this - its
        # after= callback only hops to the loop (see
        # Playback._track_end_callback in musicbot/playback.py), and
        # Playback._on_track_end runs on the loop
        task = self.bot.loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def dispose(self, reason: str = "left guild"):
        """Tear this controller down for good: disconnect, then cancel
        every task add_task() still has pending."""
        await self.udisconnect(reason)
        for task in list(self._tasks):
            task.cancel()
        # after the tasks: one of them may have started it
        self.timer.cancel()

    async def _preload(self, song: Song) -> bool:
        """Preloads `song`; when that ran a fresh extraction, refreshes
        its title in the saved playlist it was queued from. A song that
        was already current is not refreshed, so it costs no DB read.
        The refresh is best-effort: it never changes the result."""
        result = await loader.preload(song)
        if result is loader.Preload.FAILED:
            return False
        if result is loader.Preload.EXTRACTED and song.saved_entry is not None:
            try:
                await playlists.set_title(
                    self.bot.DbSession,
                    song.saved_entry.ref,
                    song.saved_entry.url,
                    song.title,
                )
            except Exception:
                print_exc(file=sys.stderr)
        return True

    async def _preload_songs(self):
        rerun_needed = False
        for song in list(
            islice(self.playlist.playque, 1, config.MAX_SONG_PRELOAD)
        ):
            if not await self._preload(song):
                # Not the head: while this preload ran, the song may
                # have become the current one, and play_song()'s own
                # failed-preload skip is the only owner of the head.
                # Removing it here too would make that skip drop the
                # song behind it.
                queue = self.playlist.playque
                if queue and queue[0] is song:
                    continue
                try:
                    queue.remove(song)
                    rerun_needed = True
                except ValueError:
                    # already removed
                    pass
        if rerun_needed:
            self.add_task(self._preload_songs())

    def _schedule_preload(self):
        """Preloads the first MAX_SONG_PRELOAD songs asynchronously"""
        self.add_task(self._preload_songs())

    def stop(self):
        """Stops the player and removes all songs from the queue"""
        self.playback.stop()

    def prev_song(self) -> bool:
        """Loads the last song from the history into the queue and starts it"""

        prev_song = self.playlist.prev()
        if not prev_song:
            return False

        self.playback.back(prev_song)
        return True

    async def timeout_handler(self):
        if not self.guild.voice_client:
            return

        sett = self.bot.sessions.settings(self.guild)

        if sett.vc_timeout and (
            (
                not self.guild.voice_client.is_playing()
                and self.playback.state is not PlaybackState.STARTING
            )
            or not self._humans_present()
        ):
            await self.udisconnect("inactivity timeout")

    async def start_idle_timer(self, restart: bool = False):
        """Starts the idle timer, unless a disconnect is in flight.
        The check runs when this coroutine does, not when it was
        scheduled. Loop only."""
        await self.playback.start_idle_timer(restart)

    async def udisconnect(self, reason: str):
        async with self.playback.teardown() as had_session:
            return await self._udisconnect(reason, had_session)

    async def _udisconnect(self, reason: str, had_session: bool):
        await self.update_view(None)
        # Cancelled here rather than after the disconnect below, so
        # the no-connection branch cancels it too. timeout_handler()
        # reads self.guild.voice_client, which is guild-global and not
        # this controller's own - so a timer left pending on a
        # controller that is being torn down (the bot was dragged out
        # of voice and did not reconnect, say) fires VC_TIMEOUT later
        # and disconnects whatever session has since taken its place.
        # d!reset, which replaces the controller outright, is the
        # easiest way to see it.
        self.timer.cancel()
        if self.guild.voice_client is None:
            # No connection left to close, but state was still torn
            # down above - and for a bot dragged out of voice that
            # failed to reconnect, this is the only record that the
            # disconnect happened at all. Only worth saying when there
            # was something to lose: there is one controller per
            # guild the bot is in, connected or not, and close()
            # disconnects all of them, so without this a shutdown
            # would print a line for every idle guild.
            if had_session:
                print(
                    f"Cleared voice state for guild {self.guild.name!r}"
                    f" ({reason})"
                )
            return False
        print(f"Disconnecting from guild {self.guild.name!r} ({reason})")
        if config.ANNOUNCE_DISCONNECT:
            try:
                self.guild.voice_client.play(
                    discord.FFmpegPCMAudio(asset("disconnect.mp3"))
                )
                while self.guild.voice_client.is_playing():
                    await asyncio.sleep(1)
            except Exception:
                print_exc(file=sys.stderr)
        await self.guild.voice_client.disconnect(force=True)
        return True
