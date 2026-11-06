import os
import sys
import asyncio
from itertools import islice
from traceback import print_exc
from typing import (
    TYPE_CHECKING,
    Callable,
    Coroutine,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

import discord
from config import config

from musicbot import library, loader, playlists, utils
from musicbot.outcome import BatchReport, EnqueueOutcome, OutcomeKind
from musicbot.song import Song
from musicbot.linkutils import SiteTypes
from musicbot.message_link import check_message_link
from musicbot.panel_snapshot import PanelSnapshot
from musicbot.playback import Playback, PlaybackState
from musicbot.playlist import Playlist, LoopMode, LoopState
from musicbot.utils import asset
from pathlib import Path
import pickle
from enum import Enum, auto

_T = TypeVar("_T")

# avoiding circular import
if TYPE_CHECKING:
    from musicbot.bot import MusicBot


class RestoreResult(Enum):
    """What load_backup() and start_restored() did."""

    READY = auto()
    RESTORED = auto()
    NOTHING_TO_RESTORE = auto()
    REFUSED_WHILE_ACTIVE = auto()
    BACKUP_UNREADABLE = auto()
    REFUSED_WHILE_DISCONNECTING = auto()


class AudioController(object):
    """Controls the playback of audio and the sequential playing of the songs.

    Attributes:
        bot: The instance of the bot that will be playing the music.
        playlist: A Playlist object that stores the history and queue of songs.
        current_song: A Song object that stores details of the current song.
        guild: The guild in which the Audiocontroller operates.
    """

    def __init__(self, bot: "MusicBot", guild: discord.Guild, panel):
        """`panel` is the controller's panel port, which for now has
        three methods: `announce(song)`, `report_skipped(song)` and
        `playback_ended()`."""
        self.bot = bot
        self.panel = panel
        self.playlist = Playlist()
        self.pickle_file = Path("backup") / f"playlist_{guild.id}.pickle"
        self.pickle_file.parent.mkdir(parents=True, exist_ok=True)
        self.guild = guild
        self._log_label = f"{guild.name!r} ({guild.id})"

        sett = bot.sessions.settings(guild)
        self._volume: int = sett.default_volume

        self.timer = utils.Timer(self.timeout_handler)

        self.playback = Playback(
            voice_client=lambda: self.guild.voice_client,
            preload=self._preload,
            make_source=self._make_source,
            next_track=lambda forced: self.playlist.next(forced),
            drop_failed=lambda: self.playlist.drop_head(),
            clear_queue=self._clear_queue,
            schedule_preload=self._schedule_preload,
            announce=self.panel.announce,
            report_skipped=self.panel.report_skipped,
            record_history=lambda name: self.playlist.add_name(name),
            write_backup=self._write_backup,
            queue_is_empty=lambda: not self.playlist,
            spawn=self.add_task,
            timer=self.timer,
            humans_present=self._humans_present,
            on_play_failed=lambda: self.udisconnect("playback error"),
            get_loop=lambda: self.bot.loop,
            log_label=self._log_label,
        )

        # according to Python documentation, we need
        # to keep strong references to all tasks
        self._tasks = set()
        # the number of the sweep allowed to start preloads
        self._sweep = 0

    @property
    def current_song(self) -> Optional[Song]:
        if self.is_active() and self.playlist:
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
        """Best-effort, atomic backup write; never raises. The previous
        backup survives any failure."""
        tmp = self.pickle_file.with_name(self.pickle_file.name + ".tmp")
        try:
            with open(tmp, "wb") as f:
                pickle.dump(self.playlist, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.pickle_file)
        except Exception as e:
            print(
                f"Playlist backup write failed in guild "
                f"{self._log_label}: {e!r}",
                file=sys.stderr,
            )
            if not isinstance(e, OSError):
                print_exc(file=sys.stderr)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _humans_present(self) -> bool:
        return not all(m.bot for m in self.guild.voice_client.channel.members)

    def _clear_queue(self):
        self.playlist.loop = LoopMode.OFF
        self.playlist.clear()
        self.playlist.next()

    def _make_source(self, song: Song):
        if song.host == SiteTypes.LOCAL_LIBRARY:
            before_options = "-protocol_whitelist file"
        else:
            before_options = (
                "-protocol_whitelist http,https,tcp,tls,crypto "
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
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

    def panel_snapshot(self) -> PanelSnapshot:
        """What the player panel draws: read-only, and inactive when
        nothing is playing or paused."""
        if not self.is_active():
            return PanelSnapshot.inactive()
        return PanelSnapshot(
            active=True,
            paused=not self.guild.voice_client.is_playing(),
            has_previous=self.playlist.has_prev(),
            has_next=self.playlist.has_next(),
            queue_empty=len(self.playlist) == 0,
            loop_mode=self.playlist.loop,
            volume=self.volume,
            current_song=self.current_song,
        )

    def queue_embed(self) -> discord.Embed:
        """The queue's embed, read when a queue button is clicked."""
        return self.playlist.queue_embed()

    def is_active(self) -> bool:
        return self.playback.is_active()

    def is_disconnecting(self) -> bool:
        """True for the whole of teardown. Loop only."""
        return self.playback.state is PlaybackState.DISCONNECTING

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
        self.playback.save_backup()
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
        self.playback.save_backup()

    def skip(self):
        """Ends the current track and moves on to the next one,
        ignoring a single-track loop. Runs on the event loop only -
        raises RuntimeError anywhere else."""
        self.playback.skip(forced=True)

    async def play_song(self, song: Song) -> bool:
        """Plays a song object."""
        return await self.playback.start(song)

    async def ensure_playing(self) -> Optional[bool]:
        """Starts the head of the queue if nothing is playing and the
        queue is non-empty, returning the start's result; otherwise
        preloads the queue and returns `None`. The one "start if idle"
        rule for everything that adds to the queue."""
        if self.is_idle() and len(self.playlist) > 0:
            return await self.play_song(self.playlist[0])
        self._schedule_preload()
        return None

    def _would_start(self) -> bool:
        """Whether a request placed now triggers the start: nothing is
        playing and the queue is empty."""
        return self.is_idle() and len(self.playlist) == 0

    def _refusal(self) -> Optional[OutcomeKind]:
        """The one guard every placement passes: the refusal kind when
        a request's songs may not be placed now, else `None`. Out of
        voice means the guild has no voice client at all; a client that
        exists but is not connected (a move, a reconnect) is not."""
        if self.guild.voice_client is None:
            return OutcomeKind.REFUSED_NOT_IN_VOICE
        return None

    async def _place_and_start(
        self, place: Callable[[], _T]
    ) -> Tuple[Optional[_T], Optional[OutcomeKind]]:
        """The one step that decides whether a request claimed the
        start of playback. Reads whether it would start, runs `place`,
        marks the queue changed and starts playback if idle. Returns
        `place`'s result and the start: STARTED or START_FAILED when
        this request claimed it, else `None`.

        A refused request (see _refusal()) places nothing, writes no
        backup and starts nothing: the result is `None` and the start
        is the refusal kind.

        Placement happens before the first await, so nothing can slip
        between the request's songs and their slots."""
        refusal = self._refusal()
        if refusal is not None:
            return None, refusal
        would_start = self._would_start()
        placed = place()
        self.playback.save_backup()
        started = await self.ensure_playing()
        if not would_start or started is None:
            return placed, None
        kind = OutcomeKind.STARTED if started else OutcomeKind.START_FAILED
        return placed, kind

    @staticmethod
    def _single_outcome(
        song: Song, index: int, start: Optional[OutcomeKind]
    ) -> EnqueueOutcome:
        if start is not None:
            return EnqueueOutcome(start, song=song)
        return EnqueueOutcome(
            OutcomeKind.QUEUED, song=song, position=index + 1
        )

    @staticmethod
    def _outcome(
        placed: List[Song],
        first_index: int,
        start: Optional[OutcomeKind],
        batch: Optional[BatchReport],
    ) -> EnqueueOutcome:
        """A batch load, or a placed count other than 1, reports
        PLAYLIST; else the single song's outcome."""
        if batch is not None or len(placed) != 1:
            return EnqueueOutcome(
                OutcomeKind.PLAYLIST,
                count=len(placed),
                truncation=batch.truncation if batch else None,
            )
        return AudioController._single_outcome(placed[0], first_index, start)

    async def enqueue_next(
        self,
        song: Song,
        *,
        batch: Optional[BatchReport] = None,
    ) -> EnqueueOutcome:
        """Puts `song` directly after the current track, refreshes the
        backup and starts playing if nothing is (see ensure_playing()).
        Reports as enqueue() does."""
        if self.is_disconnecting():
            return EnqueueOutcome(OutcomeKind.REFUSED_DISCONNECTING)
        index, start = await self._place_and_start(
            lambda: self.playlist.insert_next(song)
        )
        if start is OutcomeKind.REFUSED_NOT_IN_VOICE:
            return EnqueueOutcome(start)
        return self._outcome([song], index, start, batch)

    async def enqueue(
        self,
        songs: Iterable[Song],
        *,
        batch: Optional[BatchReport] = None,
    ) -> EnqueueOutcome:
        """Appends `songs` to the queue, refreshes the backup once and
        starts playing if nothing is (see ensure_playing()).

        Returns PLAYLIST when it places more than one song or `batch`
        is given, else the single song's outcome."""
        if self.is_disconnecting():
            return EnqueueOutcome(OutcomeKind.REFUSED_DISCONNECTING)
        placed = list(songs)

        def place() -> int:
            first_index = len(self.playlist)
            for song in placed:
                self.playlist.add(song)
            return first_index

        first_index, start = await self._place_and_start(place)
        if start is OutcomeKind.REFUSED_NOT_IN_VOICE:
            return EnqueueOutcome(start)
        return self._outcome(placed, first_index, start, batch)

    def load_backup(self) -> RestoreResult:
        """Reloads the playlist backup, starting nothing.

        Refuses while something is playing or paused, before touching
        the backup or the queue: starting the head then would make
        discord.py refuse play() and the controller disconnect (#24).
        Reports BACKUP_UNREADABLE, changing nothing, when the backup
        exists but cannot be loaded, NOTHING_TO_RESTORE when the queue
        is still empty, and READY when start_restored() has a head."""
        if self.is_disconnecting():
            return RestoreResult.REFUSED_WHILE_DISCONNECTING
        if not self.is_idle():
            return RestoreResult.REFUSED_WHILE_ACTIVE
        try:
            self.load_pickle_playlist()
        except Exception as e:
            print(
                f"Playlist backup unreadable in guild "
                f"{self._log_label}: {e!r}",
                file=sys.stderr,
            )
            return RestoreResult.BACKUP_UNREADABLE
        if not self.playlist:
            return RestoreResult.NOTHING_TO_RESTORE
        return RestoreResult.READY

    async def start_restored(self) -> RestoreResult:
        """Starts the head of the queue load_backup() brought back.
        With an empty queue it starts nothing and reports
        NOTHING_TO_RESTORE."""
        if not self.playlist:
            return RestoreResult.NOTHING_TO_RESTORE
        await self.play_song(self.playlist[0])
        return RestoreResult.RESTORED

    async def process_song(
        self,
        track: str,
        user: Optional[discord.abc.User] = None,
        playnext=False,
    ) -> EnqueueOutcome:
        """Adds the track to the playlist instance
        Starts playing if it is the first song.

        With `playnext` a single track (or a one-item playlist) goes
        directly after the current track; a longer playlist is still
        appended.

        For many tracks at once use process_local_tracks() rather than
        calling this in a loop - pickling here on every call would mean
        serializing the whole, progressively larger playlist once per
        track, an O(n^2) blocking write."""

        # A failed load raises SongError, which the caller turns into a
        # Discord reply; it still has to leave a trace here.
        try:
            await check_message_link(track, self.guild, user)
            result = await loader.load_song(track)
        except loader.SongError:
            print(
                f"{user} queued {track!r} in guild {self.guild.name!r}"
                " - could not be loaded"
            )
            raise
        added = result.songs
        if result.single:
            first = added[0]
            what = (
                f"{first.display_name!r} by {first.uploader or 'unknown'}"
                f" ({first.host.name})"
            )
        else:
            # a resolved playlist has no single title to show -
            # the input is the only useful identifier left
            what = f"a playlist ({len(added)} track(s)) via {track!r}"
        print(f"{user} queued {what} in guild {self.guild.name!r}")

        batch = None if result.single else BatchReport(result.truncation)
        if playnext and len(added) == 1:
            return await self.enqueue_next(added[0], batch=batch)
        return await self.enqueue(added, batch=batch)

    async def process_local_tracks(
        self,
        tracks: Sequence[library.LocalTrack],
        source: str,
        user: Optional[discord.abc.User] = None,
    ) -> EnqueueOutcome:
        """Queues a batch of local-library files and settles the
        playlist once at the end. Returns a LIBRARY_PICK outcome: `count`
        the songs queued, `skipped` the names of the tracks that could
        not be loaded (empty when all were).

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

        def refusal() -> Optional[EnqueueOutcome]:
            if self.is_disconnecting():
                return EnqueueOutcome(OutcomeKind.REFUSED_DISCONNECTING)
            kind = self._refusal()
            return None if kind is None else EnqueueOutcome(kind)

        refused = refusal()
        if refused is not None:
            return refused

        uris = [track.uri for track in tracks]
        songs: List[Optional[Song]] = []
        tail = uris

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
        if self.is_idle() and len(uris) > 1:
            songs += await loader.load_local_songs(uris[:1])
            tail = uris[1:]
            refused = refusal()
            if refused is not None:
                return refused
            if songs[0] is not None:
                placed = await self.enqueue(songs[:1])
                if placed.kind in (
                    OutcomeKind.REFUSED_DISCONNECTING,
                    OutcomeKind.REFUSED_NOT_IN_VOICE,
                ):
                    return placed

        loaded_tail = await loader.load_local_songs(tail)
        refused = refusal()
        if refused is not None:
            return refused
        songs += loaded_tail

        added = [song for song in loaded_tail if song is not None]
        if added:
            placed = await self.enqueue(added)
            if placed.kind in (
                OutcomeKind.REFUSED_DISCONNECTING,
                OutcomeKind.REFUSED_NOT_IN_VOICE,
            ):
                return placed

        return EnqueueOutcome(
            OutcomeKind.LIBRARY_PICK,
            count=len([song for song in songs if song is not None]),
            skipped=tuple(
                track.name
                for track, song in zip(tracks, songs)
                if song is None
            ),
        )

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
        try:
            await self.udisconnect(reason)
        finally:
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

    async def _preload_songs(self, sweep: int):
        """The warm-up sweep: preloads the songs behind the head and
        nothing else. It never removes a song, whatever a preload
        returns: a failure may be temporary, and a song that cannot be
        played is reported and skipped when it reaches the head. It
        ends, between songs, once `sweep` is no longer the current
        sweep; it is never cancelled, so a preload shared with the
        head play is not failed under it."""
        for song in list(
            islice(self.playlist.playque, 1, config.MAX_SONG_PRELOAD)
        ):
            if sweep != self._sweep:
                return
            if song not in self.playlist.playque:
                continue
            await self._preload(song)

    def _schedule_preload(self):
        """Preloads the first MAX_SONG_PRELOAD songs asynchronously,
        superseding any sweep already running."""
        self.add_task(self._preload_songs(self._end_sweep()))

    def _end_sweep(self) -> int:
        """Ends the running preload sweep; returns the new sweep number."""
        self._sweep += 1
        return self._sweep

    def stop(self):
        """Stops the player and removes all songs from the queue"""
        self._end_sweep()
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
        self._end_sweep()
        async with self.playback.teardown() as had_session:
            return await self._udisconnect(reason, had_session)

    async def _udisconnect(self, reason: str, had_session: bool):
        await self.panel.playback_ended()
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
        # guild.voice_client is guild-global and the sound below awaits,
        # so it can vanish or be replaced meanwhile: hold the one being
        # closed, and close it only if it is still the guild's. Closing
        # a stale client would make discord.py unregister whatever
        # client has taken its place.
        voice_client = self.guild.voice_client
        if config.ANNOUNCE_DISCONNECT:
            try:
                voice_client.play(
                    discord.FFmpegPCMAudio(asset("disconnect.mp3"))
                )
                while voice_client.is_playing():
                    await asyncio.sleep(1)
            except Exception:
                print_exc(file=sys.stderr)
        if self.guild.voice_client is voice_client:
            await voice_client.disconnect(force=True)
        return True
