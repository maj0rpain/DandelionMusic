"""One guild's start/advance/stop state machine over its voice
connection: which track is starting or playing, which comes next, and
when the playlist backup is written.

Playback depends only on the narrow callables its owner injects, never
on the bot, the guild or the queue. discord.py's `after=` callback runs
on its audio thread and only hops to the loop (`call_soon_threadsafe`);
everything else here is loop-only.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from enum import Enum, auto
from typing import Awaitable, Callable, Optional

import discord

from musicbot.playlist import PauseState


class PlaybackState(Enum):
    """IDLE: nothing playing, paused or pending. STARTING: a track is
    claimed but its preload has not finished. ACTIVE: the voice client
    is playing or paused."""

    IDLE = auto()
    STARTING = auto()
    ACTIVE = auto()


def _require_loop():
    """Raises RuntimeError unless called on a thread running an event
    loop."""
    asyncio.get_running_loop()


class Playback:
    def __init__(
        self,
        *,
        voice_client: Callable[[], object],
        preload: Callable[[object], Awaitable[bool]],
        make_source: Callable[[object], object],
        next_track: Callable[[bool], Optional[object]],
        clear_queue: Callable[[], None],
        schedule_preload: Callable[[], None],
        announce: Callable[[object], Awaitable[None]],
        report_skipped: Callable[[object], Awaitable[None]],
        record_history: Callable[[str], None],
        write_backup: Callable[[], None],
        queue_is_empty: Callable[[], bool],
        spawn: Callable[[object], None],
        start_timer: Callable[[bool], Awaitable[None]],
        timer_triggered: Callable[[], bool],
        humans_present: Callable[[], bool],
        on_play_failed: Callable[[], Awaitable[None]],
        get_loop: Callable[[], asyncio.AbstractEventLoop],
        guild_name: str,
        guild_id: int,
    ):
        self._voice_client = voice_client
        self._preload = preload
        self._make_source = make_source
        self._next_track = next_track
        self._clear_queue = clear_queue
        self._schedule_preload = schedule_preload
        self._announce = announce
        self._report_skipped = report_skipped
        self._record_history = record_history
        self._write_backup = write_backup
        self._queue_is_empty = queue_is_empty
        self._spawn = spawn
        self._start_timer = start_timer
        self._timer_triggered = timer_triggered
        self._humans_present = humans_present
        self._on_play_failed = on_play_failed
        self._get_loop = get_loop
        self._guild_name = guild_name
        self._guild_id = guild_id

        self._next_song = None
        # The start token: bumped when a start is committed to (see
        # _claim_start()), before its preload. A track end carries the
        # generation it was started as; a start whose generation is no
        # longer current has been overtaken.
        self._generation = 0
        # whether a start is under way (claimed, not yet handed to the
        # voice client)
        self._start_pending = False
        # True from the moment playback starts until the queue runs
        # dry or the player is stopped. The voice client cannot stand
        # in for it: by the time _advance() moves the queue, discord.py
        # has already cleared the player state, so it reads idle on an
        # ordinary track change just as when nothing was playing.
        self._playing = False
        # True for the whole of teardown(): a track-end hop that lands
        # meanwhile must not start the idle timer.
        self._disconnecting = False

    # -- state -------------------------------------------------------

    def _active(self) -> bool:
        client = self._voice_client()
        return client is not None and (
            client.is_playing() or client.is_paused()
        )

    @property
    def state(self) -> PlaybackState:
        if self._start_pending:
            return PlaybackState.STARTING
        if self._active():
            return PlaybackState.ACTIVE
        return PlaybackState.IDLE

    # -- start token -------------------------------------------------

    def _claim_start(self) -> int:
        """Commits to a start: bumps the generation, which supersedes
        any pending start, and marks the new one pending. A track a
        skip picked for an earlier track's end is not this one's next.
        """
        self._generation += 1
        self._start_pending = True
        self._next_song = None
        return self._generation

    def _invalidate_start(self):
        """Drops a pending start, if any, so that it plays nothing once
        its preload returns."""
        if self._start_pending:
            self._generation += 1
            self._start_pending = False

    def _drop_if_overtaken(self, generation: int) -> bool:
        """True when the start claimed as `generation` must be dropped:
        a newer start superseded it, or the voice client is gone - in
        which case this also releases that start's pending mark."""
        if generation != self._generation:
            # a newer start owns the pending mark
            return True
        if self._voice_client() is None:
            # abandoned while still the current start: release its mark
            # so the bot is not left busy
            self._start_pending = False
            return True
        return False

    # -- intents -----------------------------------------------------

    async def start(self, song) -> bool:
        """Claims a start for `song` and plays it. Once the preload
        returns, a start that was overtaken - a stop, a disconnect or a
        newer start replaced it, or the bot left voice - is dropped:
        nothing plays, is said or advances. Returns whether the song
        was handed to the voice client."""
        return await self._play(song, self._claim_start())

    def skip(self, forced: bool = False):
        """Ends the current track and moves on to the next one. With a
        track playing, it picks the next track and stops the voice
        client: the after= hop of that track does the advance. With
        nothing playing, it advances directly, dropping a pending
        start. Loop only."""
        _require_loop()

        if self._active():
            self._hand_over_to(self._next_track(forced))
            return

        self._invalidate_start()
        self._advance(forced)

    def back(self, song):
        """Starts `song`, the track the queue stepped back to: at once
        when nothing is playing, otherwise after the current one is
        stopped. Then writes the backup."""
        if not self._active():
            self._spawn_start(song)
        else:
            self._hand_over_to(song)
        self.queue_changed()

    def stop(self):
        """Stops the player and empties the queue. Loop only."""
        # whatever starts after this is a new session, not a track
        # change
        self._playing = False
        self.queue_changed()
        self._clear_queue()
        # a pending start would otherwise play once its preload ends;
        # a playing track needs no bump - its teardown hop must stay
        # current
        self._invalidate_start()

        if not self._active():
            return

        # stopping an active voice client runs an `after` callback;
        # its _advance() finds the queue empty and idle, so
        # queue_changed() keeps this snapshot
        self._voice_client().stop()

    def toggle_pause(self) -> PauseState:
        """Writes the backup (as pause always has), then pauses a
        playing client or resumes a paused one. A pause restarts the
        idle timer."""
        self.queue_changed()
        client = self._voice_client()
        if client:
            if client.is_playing():
                client.pause()
                self._spawn(self.start_idle_timer(True))
                return PauseState.PAUSED
            elif client.is_paused():
                client.resume()
                return PauseState.RESUMED
        return PauseState.NOTHING_TO_PAUSE

    def queue_changed(self):
        """Writes the playlist backup. An idle playback with an empty
        queue has nothing to save, and writing it would wipe the last
        real backup d!restore reloads. The playing flag keeps the save
        when the last track ends on its own, so a finished queue still
        leaves an empty backup."""
        if self._queue_is_empty() and not self._playing:
            return
        self._write_backup()

    @asynccontextmanager
    async def teardown(self):
        """Raises the disconnect guard, stops, and yields whether there
        was anything to lose (sampled before the stop wipes it). The
        guard is lowered on exit, however the block ends."""
        self._disconnecting = True
        try:
            had_session = self._playing or not self._queue_is_empty()
            self.stop()
            yield had_session
        finally:
            self._disconnecting = False

    async def start_idle_timer(self, restart: bool = False):
        """Starts the idle timer, unless a disconnect is in flight.
        The check runs when this coroutine does, not when it was
        scheduled. Loop only."""
        if self._disconnecting:
            return
        await self._start_timer(restart)

    # -- internals ---------------------------------------------------

    def _track_end_callback(self, generation: int):
        """The after= callable for the track started as `generation`.
        discord.py calls it on its audio-player thread, so it does only
        thread-safe work: print the error, then hand the track end to
        the loop."""

        def after(error):
            if error is not None:
                print(
                    f"Playback error in guild {self._guild_id}: {error!r}",
                    file=sys.stderr,
                )
            try:
                self._get_loop().call_soon_threadsafe(
                    self._on_track_end, error, generation
                )
            except RuntimeError:
                # the loop is closed: the bot is shutting down, and
                # there is nothing left to advance to
                pass

        return after

    def _on_track_end(self, error, generation: int):
        """Runs on the loop once the track started as `generation` has
        ended, and advances the queue - unless a newer start has
        overtaken it, or discord.py has already torn the voice
        connection down (then the cut-off track stays at the head of
        the queue)."""
        _require_loop()

        if generation != self._generation:
            # a newer track has already started: it is not this
            # callback's to end
            return
        if self._voice_client() is None:
            # discord.py tore the connection down from outside and
            # stopped the player on its way: the track was cut off,
            # not finished. Leave the queue, history and backup as
            # they are - the disconnect ends the session, and the cut
            # off track restarts from the head of the queue. Only
            # `is None`: is_connected() is briefly False during a
            # move or reconnect, when a real track end must advance.
            return

        self._advance()

    def _advance(self, forced=False):
        """Moves the queue on past the finished track and starts the
        next one, or the idle timer when there is none. Loop only."""
        if self._next_song:
            next_song = self._next_song
            self._next_song = None
        else:
            next_song = self._next_track(forced)

        self.queue_changed()

        if next_song is None:
            # nothing left to advance to - the next song to start is a
            # fresh one
            self._playing = False
            if not self._timer_triggered() and self._voice_client():
                self._spawn(self.start_idle_timer(self._humans_present()))
            return

        self._spawn_start(next_song)

    def _hand_over_to(self, song):
        """With a track active, picks `song` as the next one and stops
        the voice client: the after= hop of the track does the advance.
        """
        self._next_song = song
        self._voice_client().stop()

    def _spawn_start(self, song):
        """Claims a start for `song` and spawns its play."""
        self._spawn(self._play(song, self._claim_start()))

    async def _play(self, song, generation: int) -> bool:
        """True when `song` was handed to the voice client; False for a
        failed preload, a song without a direct url, an overtaken start
        or a play() that raised."""
        preloaded = await self._preload(song)
        if self._drop_if_overtaken(generation):
            return False

        if not preloaded:
            await self._report_skipped(song)
            if self._drop_if_overtaken(generation):
                return False
            self.skip(forced=True)
            return False

        if song.url is None:
            print(
                "Something is wrong."
                " Refusing to play a song without direct url.",
                file=sys.stderr,
            )
            self.skip(forced=True)
            return False

        # Claimed before play() rather than after it, because play()
        # starts the audio thread there and then: a source that yields
        # nothing (a truncated file, a stream URL that died) ends
        # immediately, and its after= hop advances an empty queue,
        # which clears this flag. Reading it afterwards would see the
        # value that callback left, announce a track that has already
        # finished, and leave the flag set with nothing playing -
        # silencing the next genuine start. A play() that raises below
        # disconnects, and stop() clears it there.
        was_idle = not self._playing
        self._playing = True
        # no longer pending once it reaches play(); the generation
        # token that guards the start is _claim_start()'s
        self._start_pending = False
        try:
            self._voice_client().play(
                self._make_source(song),
                after=self._track_end_callback(generation),
            )
        except discord.ClientException:
            await self._on_play_failed()
            return False

        # The history's one write site: a song is recorded once the
        # voice client has accepted it, however it later ends.
        self._record_history(song.display_name)

        if was_idle:
            # only the transition into playing, so advancing through a
            # queue stays silent
            print(
                f"Started playing {song.display_name!r}"
                f" in guild {self._guild_name!r}"
            )

        await self._announce(song)

        self._schedule_preload()

        return True
