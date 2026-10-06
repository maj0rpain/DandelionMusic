import sys
import asyncio
from itertools import islice
from inspect import isawaitable
from traceback import print_exc
from typing import (
    TYPE_CHECKING,
    Coroutine,
    Iterable,
    List,
    Literal,
    Optional,
    Union,
)

import discord
from config import config

from musicbot import loader, utils
from musicbot.song import Song
from musicbot.linkutils import SiteTypes
from musicbot.playlist import Playlist, LoopMode, LoopState, PauseState
from musicbot.utils import CheckError, asset, play_check, dj_check
from pathlib import Path
import pickle
from enum import Enum, auto

# avoiding circular import
if TYPE_CHECKING:
    from musicbot.bot import MusicBot


VC_CONNECT_TIMEOUT = 10

PLAYLIST = object()
_not_provided = object()


class MusicButton(discord.ui.Button):
    def __init__(self, callback, check=play_check, **kwargs):
        super().__init__(**kwargs)
        self._callback = callback
        self._check = check

    async def callback(self, inter: discord.Interaction):
        ctx = await inter.client.get_context(inter)
        try:
            await self._check(ctx)
        except CheckError as e:
            await ctx.send(e, ephemeral=True)
            return
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
            try:
                await dj_check(ctx)
            except CheckError as e:
                await ctx.send(e, ephemeral=True)
                return
        await inter.response.defer()
        res = self._callback(ctx)
        if isawaitable(res):
            await res

        controller = inter.client.sessions.controller(inter.guild)
        if controller:
            if inter.data.get("custom_id") in ["next", "prev"]:
                await ctx.send(f"{inter.user} Skipped a Song")
            else:
                await controller.update_view()


class RestoreResult(Enum):
    """What AudioController.restore() did."""

    RESTORED = auto()
    NOTHING_TO_RESTORE = auto()
    REFUSED_WHILE_ACTIVE = auto()


def _require_loop():
    """Raises RuntimeError unless called on a thread running an event
    loop - the guard for AudioController's loop-only methods."""
    asyncio.get_running_loop()


class AudioController(object):
    """Controls the playback of audio and the sequential playing of the songs.

    Attributes:
        bot: The instance of the bot that will be playing the music.
        playlist: A Playlist object that stores the history and queue of songs.
        current_song: A Song object that stores details of the current song.
        guild: The guild in which the Audiocontroller operates.
    """

    def __init__(self, bot: "MusicBot", guild: discord.Guild):
        self._stopping = False
        self.bot = bot
        self.playlist = Playlist()
        self.pickle_file = Path("backup") / f"playlist_{guild.id}.pickle"
        self.pickle_file.parent.mkdir(parents=True, exist_ok=True)
        self._next_song = None
        # bumped by play_song() for every track it starts; a track
        # end carries the generation it was started as
        self._generation = 0
        self.guild = guild

        # True from the moment playback starts until the queue runs
        # dry or the player is stopped - see play_song(). is_active()
        # cannot stand in for this: by the time _advance() moves
        # the queue, discord.py has already cleared the player state
        # (see _advance()'s own comment below), so is_active() reads
        # False on an ordinary track change just as it does when
        # nothing was playing at all.
        self._playing = False

        sett = bot.sessions.settings(guild)
        self._volume: int = sett.default_volume

        self.timer = utils.Timer(self.timeout_handler)

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

    def _pickle_playlist(self):
        with open(self.pickle_file, "wb") as f:
            pickle.dump(self.playlist, f)

    def load_pickle_playlist(self):
        if self.pickle_file.exists():
            with open(self.pickle_file, "rb") as f:
                self.playlist = pickle.load(f)

    def volume_up(self):
        self.set_volume(min(self.volume + 10, 100))

    def volume_down(self):
        self.set_volume(max(self.volume - 10, 10))

    async def register_voice_channel(self, channel: discord.VoiceChannel):
        perms = channel.permissions_for(self.guild.me)
        if not perms.connect or not perms.speak:
            raise CheckError(config.VOICE_PERMISSIONS_MISSING)

        bot_vc = self.guild.voice_client
        if bot_vc:
            await bot_vc.move_to(channel)
            # to avoid ClientException: Not connected to voice
            await asyncio.sleep(1)
        else:
            await channel.connect(reconnect=True, timeout=VC_CONNECT_TIMEOUT)

    def make_view(self):
        if not self.is_active():
            self._last_view = None
            return None

        is_empty = len(self.playlist) == 0

        view = self._last_view = discord.ui.View(timeout=None)
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
                disabled=self.volume == 10,
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

        return self._last_view

    async def current_song_callback(self, ctx):
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
            res = await send(self.make_view())
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

    def track_history(self):
        history_string = config.INFO_HISTORY_TITLE
        for trackname in self.playlist.trackname_history:
            history_string += "\n" + trackname
        return history_string

    def pause(self):
        self._pickle_playlist()
        client = self.guild.voice_client
        if client:
            if client.is_playing():
                client.pause()
                self.add_task(self.timer.start(True))
                return PauseState.PAUSED
            elif client.is_paused():
                client.resume()
                return PauseState.RESUMED
        return PauseState.NOTHING_TO_PAUSE

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

    def shuffle(self):
        self.playlist.shuffle()
        self._pickle_playlist()
        self._preload_queue()

    def move(self, src: int, dest: int):
        """Moves the queued track at index `src` to index `dest`
        (0 is the current track, which cannot be moved). Raises
        PlaylistError for an index that cannot be moved."""
        self.playlist.move(src, dest)
        self._pickle_playlist()
        self._preload_queue()

    def remove(self, index: int) -> Song:
        """Removes and returns the queued track at `index` (0 is the
        current track, which cannot be removed). Raises PlaylistError
        for an index that cannot be removed."""
        song = self.playlist.remove(index)
        self._pickle_playlist()
        self._preload_queue()
        return song

    def clear(self):
        """Empties the queue, keeping the current track. Nothing new
        is queued behind it, so there is nothing to preload."""
        self.playlist.clear()
        self._pickle_playlist()

    def skip(self):
        """Ends the current track and moves on to the next one,
        ignoring a single-track loop"""
        self.next_song(forced=True)

    def next_song(self, *, forced=False):
        """Ends the current track and moves on to the next one. Runs
        on the event loop only - raises RuntimeError anywhere else.

        With a track playing, it picks the next track and stops the
        voice client: the after= hop of that track (see
        _on_track_end()) does the advance. With nothing playing, it
        advances directly."""
        _require_loop()

        if self.is_active():
            self._next_song = self.playlist.next(forced)
            self.guild.voice_client.stop()
            return

        self._advance(forced)

    def _track_end_callback(self, generation: int):
        """The after= callable for the track play_song() starts as
        `generation`. discord.py calls it on its audio-player thread,
        so it does only thread-safe work: print the error, then hand
        the track end to the loop."""

        def after(error):
            if error is not None:
                print(
                    f"Playback error in guild {self.guild.id}: {error!r}",
                    file=sys.stderr,
                )
            try:
                self.bot.loop.call_soon_threadsafe(
                    self._on_track_end, error, generation
                )
            except RuntimeError:
                # the loop is closed: the bot is shutting down, and
                # there is nothing left to advance to
                pass

        return after

    def _on_track_end(self, error, generation: int):
        """Runs on the loop once the track play_song() started as
        `generation` has ended, and advances the queue."""
        _require_loop()

        # the teardown callback of a stop() - one-shot, see stop()
        teardown, self._stopping = self._stopping, False
        if generation != self._generation:
            # a newer track has already started: it is not this
            # callback's to end
            return

        self._advance(teardown=teardown)

    def _advance(self, forced=False, *, teardown=False):
        """Moves the queue on past the finished track and starts the
        next one, or the idle timer when there is none. Loop only."""
        if self.playlist:
            # current_song is unusable here: is_active() is always
            # False at this point, so it would always return None.
            # playlist[0] still holds the just-finished song, since
            # playlist.next() hasn't advanced the queue yet.
            self.playlist.add_name(self.playlist[0].title)

        if self._next_song:
            next_song = self._next_song
            self._next_song = None
        else:
            next_song = self.playlist.next(forced)

        if not teardown:
            # a teardown sees the queue stop() just emptied - keep
            # stop()'s snapshot instead
            self._pickle_playlist()

        if next_song is None:
            # nothing left to advance to - the next song to start is a
            # fresh one, and play_song() should say so
            self._playing = False
            # if self.pickle_file.exists():
            #     self.pickle_file.unlink()
            if not self.timer.triggered and self.guild.voice_client:
                self.add_task(
                    self.timer.start(
                        not all(
                            m.bot
                            for m in self.guild.voice_client.channel.members
                        )
                    )
                )
            return

        coro = self.play_song(next_song)
        self.add_task(coro)

    async def play_song(self, song: Song):
        """Plays a song object"""

        if not await loader.preload(song, self.bot):
            if self.command_channel:
                await self.command_channel.send(
                    f"{config.SONGINFO_ERROR}\n"
                    f"(Skipped: {song.title or song.webpage_url})"
                )
            self.next_song(forced=True)
            return

        if song.url is None:
            print(
                "Something is wrong."
                " Refusing to play a song without direct url.",
                file=sys.stderr,
            )
            self.next_song(forced=True)
            return

        before_options = (
            None
            if song.host == SiteTypes.LOCAL_LIBRARY
            else "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        )
        # Claimed before play() rather than after it, because play()
        # starts the audio thread there and then: a source that yields
        # nothing (a truncated file, a stream URL that died) ends
        # immediately, and its after= hop advances an empty queue,
        # which clears this flag. Reading it afterwards would
        # see the value that callback left, announce a track that has
        # already finished, and leave the flag set with nothing
        # playing - silencing the next genuine start. A play() that
        # raises below disconnects, and stop() clears it there.
        was_idle = not self._playing
        self._playing = True
        # a track end from an earlier track arriving after this one
        # has started must not end it - see _on_track_end() - and a
        # track a skip picked for an earlier track's end is not this
        # one's next
        self._generation += 1
        self._next_song = None
        try:
            self.guild.voice_client.play(
                discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(
                        song.url,
                        before_options=before_options,
                        options="-loglevel error",
                        stderr=sys.stderr,
                    ),
                    float(self.volume) / 100.0,
                ),
                after=self._track_end_callback(self._generation),
            )
        except discord.ClientException:
            await self.udisconnect("playback error")
            return

        if was_idle:
            # only the transition into playing, so advancing through a
            # queue stays silent. A first track that fails to preload
            # returns above without claiming the flag, so whichever
            # track actually starts is the one that gets announced.
            print(
                f"Started playing {(song.title or song.webpage_url)!r}"
                f" in guild {self.guild.name!r}"
            )

        if (
            self.bot.sessions.settings(self.guild).announce_songs
            and self.command_channel
        ):
            await self.command_channel.send(
                embed=song.format_output(config.SONGINFO_NOW_PLAYING)
            )

        self._preload_queue()

    async def ensure_playing(self):
        """Starts the head of the queue if nothing is playing and the
        queue is non-empty; otherwise preloads the queue. The one
        "start if idle" rule for everything that adds to the queue."""
        if self.current_song is None and len(self.playlist) > 0:
            await self.play_song(self.playlist[0])
        else:
            self._preload_queue()

    async def queue(self, songs: Iterable[Song]):
        """Appends `songs` to the queue, refreshes the backup and
        starts playing if nothing is (see ensure_playing())."""
        for song in songs:
            self.playlist.add(song)
        self._pickle_playlist()
        await self.ensure_playing()

    async def restore(self) -> RestoreResult:
        """Reloads the playlist backup and starts its head.

        Refuses while something is playing or paused, before touching
        the backup or the queue: starting the head then would make
        discord.py refuse play() and the controller disconnect (#24).
        Reports NOTHING_TO_RESTORE, playing nothing, when there is no
        queue to restore."""
        if self.is_active():
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
    ) -> Union[Optional[Song], Literal[PLAYLIST]]:
        """Adds the track to the playlist instance
        Starts playing if it is the first song.

        For many tracks at once use process_local_tracks() rather than
        calling this in a loop - pickling here on every call would mean
        serializing the whole, progressively larger playlist once per
        track, an O(n^2) blocking write."""

        # Logged on the way out rather than on the way in, so the
        # success cases below can name what the track resolved to
        # instead of echoing the link. A request that resolves to
        # nothing still has to leave a trace, though - both ways it
        # can fail, since an unsupported or blocked link raises
        # (SongError, which the caller turns into a Discord reply and
        # nothing else) where an empty extraction returns falsy.
        try:
            loaded_song = await loader.load_song(track)
        except loader.SongError:
            print(
                f"{user} queued {track!r} in guild {self.guild.name!r}"
                " - could not be loaded"
            )
            raise
        if not loaded_song:
            print(
                f"{user} queued {track!r} in guild {self.guild.name!r}"
                " - nothing could be loaded"
            )
            return None
        elif isinstance(loaded_song, Song):
            added = [loaded_song]
            print(
                f"{user} queued {loaded_song.title!r}"
                f" by {loaded_song.uploader or 'unknown'}"
                f" ({loaded_song.host.name}) in guild {self.guild.name!r}"
            )
        else:
            added = list(loaded_song)
            count = len(loaded_song)
            if count == 1:
                # special-case one-item playlists
                loaded_song = loaded_song[0]
                print(
                    f"{user} queued {loaded_song.title!r}"
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
                loaded_song = PLAYLIST

        await self.queue(added)

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
        single-worker loader process per track, and a fresh preload
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

        # With nothing playing, the first track is loaded and started
        # on its own before the rest. Batching means the whole batch
        # has to finish before any of it is queued, where the per-track
        # path this replaced began on track one - a few hundred
        # milliseconds of silence for a discography on local disk, and
        # seconds of it on a network-mounted library. One extra round
        # trip buys that back; loading a single track costs about
        # 1.5ms.
        if self.current_song is None and len(tracks) > 1:
            songs += await loader.load_local_songs(tracks[:1])
            tail = tracks[1:]
            if songs[0] is not None:
                await self.queue(songs[:1])

        loaded_tail = await loader.load_local_songs(tail)
        songs += loaded_tail

        added = [song for song in loaded_tail if song is not None]
        if not added:
            return songs

        await self.queue(added)

        return songs

    def add_task(self, coro: Coroutine):
        # loop only: discord.py's audio thread never reaches this - its
        # after= callback only hops to the loop (see
        # _track_end_callback()), and next_song() runs on the loop
        task = self.bot.loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def dispose(self, reason: str = "left guild"):
        """Tear this controller down for good: disconnect, then cancel
        every task add_task() still has pending."""
        await self.udisconnect(reason)
        for task in list(self._tasks):
            task.cancel()

    async def _preload_songs(self):
        rerun_needed = False
        for song in list(
            islice(self.playlist.playque, 1, config.MAX_SONG_PRELOAD)
        ):
            if not await loader.preload(song, self.bot):
                try:
                    self.playlist.playque.remove(song)
                    rerun_needed = True
                except ValueError:
                    # already removed
                    pass
        if rerun_needed:
            self.add_task(self._preload_songs())

    def _preload_queue(self):
        """Preloads the first MAX_SONG_PRELOAD songs asynchronously"""
        self.add_task(self._preload_songs())

    def stop(self):
        """Stops the player and removes all songs from the queue"""
        # whatever starts after this is a new session, not a track
        # change - see play_song()
        self._playing = False
        self._pickle_playlist()
        self.playlist.loop = LoopMode.OFF
        self.playlist.clear()
        self.playlist.next()

        if not self.is_active():
            return

        # only stopping an active voice client runs an `after`
        # callback; its _on_track_end() consumes this - see there
        self._stopping = True
        self.guild.voice_client.stop()

    def prev_song(self) -> bool:
        """Loads the last song from the history into the queue and starts it"""

        prev_song = self.playlist.prev()
        if not prev_song:
            return False

        if not self.is_active():
            self.add_task(self.play_song(prev_song))
        else:
            self._next_song = prev_song
            self.guild.voice_client.stop()
        self._pickle_playlist()
        return True

    async def timeout_handler(self):
        if not self.guild.voice_client:
            return

        sett = self.bot.sessions.settings(self.guild)

        if sett.vc_timeout and (
            not self.guild.voice_client.is_playing()
            or all(m.bot for m in self.guild.voice_client.channel.members)
        ):
            await self.udisconnect("inactivity timeout")

    async def uconnect(self, ctx, move=False):
        author_vc = ctx.author.voice
        bot_vc = self.guild.voice_client

        if not author_vc:
            raise CheckError(config.USER_NOT_IN_VC_MESSAGE)

        if bot_vc is None or bot_vc.channel != author_vc.channel and move:
            await self.register_voice_channel(author_vc.channel)
        else:
            raise CheckError(config.ALREADY_CONNECTED_MESSAGE)
        # self.load_pickle_playlist()
        return True

    async def udisconnect(self, reason: str):
        # sampled before the teardown below wipes both - see the
        # no-connection branch
        had_session = self._playing or bool(self.playlist)
        self._pickle_playlist()
        self.stop()
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
