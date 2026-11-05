"""The player panel: a guild's playback-facing side in Discord.

It owns the destination (the channel playback news goes to), the one
player message whose buttons follow the playback state, that message's
refresh and its recovery from a stale message or an expired token. It
implements the controller's panel port (`announce`, `report_skipped`,
`playback_ended`)
and reads the controller only through a getter:
`panel_snapshot()` to draw, `queue_embed()` on a queue click.
"""

import asyncio
import sys
from traceback import print_exc
from typing import Callable, Optional

import discord
from config import config

from musicbot.panel_snapshot import PanelSnapshot
from musicbot.views import MusicButton

__all__ = ["PanelSnapshot", "PlayerPanel", "build_view"]

INVALID_WEBHOOK_TOKEN = 50027


def build_view(
    snapshot: PanelSnapshot, on_current_song, on_queue
) -> Optional[discord.ui.View]:
    """The player buttons, a pure function of `snapshot`: `None` while
    inactive. The playback buttons call the clicking context's
    controller; the current-song and queue buttons run the callbacks
    given."""
    if not snapshot.active:
        return None

    view = discord.ui.View(timeout=None)
    for button in (
        MusicButton(
            lambda ctx: ctx.audiocontroller.prev_song(),
            custom_id="prev",
            disabled=not snapshot.has_previous,
            emoji="⏮️",
        ),
        MusicButton(
            lambda ctx: ctx.audiocontroller.pause(),
            custom_id="pause",
            emoji="▶️" if snapshot.paused else "⏸️",
        ),
        MusicButton(
            lambda ctx: ctx.audiocontroller.skip(),
            custom_id="next",
            disabled=not snapshot.has_next,
            emoji="⏭️",
        ),
        MusicButton(
            lambda ctx: ctx.audiocontroller.loop(),
            custom_id="loop",
            disabled=snapshot.queue_empty,
            emoji="🔁",
            label="Loop: " + snapshot.loop_mode,
        ),
        MusicButton(
            on_current_song,
            custom_id="current_song",
            row=1,
            disabled=snapshot.current_song is None,
            emoji="💿",
        ),
        MusicButton(
            lambda ctx: ctx.audiocontroller.shuffle(),
            custom_id="shuffle",
            row=1,
            disabled=snapshot.queue_empty,
            emoji="🔀",
        ),
        MusicButton(
            on_queue,
            custom_id="queue",
            row=1,
            disabled=snapshot.queue_empty,
            emoji="📜",
        ),
        MusicButton(
            lambda ctx: ctx.audiocontroller.stop(),
            custom_id="stop",
            row=1,
            emoji="⏹️",
            style=discord.ButtonStyle.red,
        ),
        MusicButton(
            lambda ctx: ctx.audiocontroller.volume_down(),
            custom_id="volume_down",
            row=2,
            disabled=snapshot.volume == 0,
            emoji="🔉",
        ),
        MusicButton(
            lambda ctx: ctx.audiocontroller.volume_up(),
            custom_id="volume_up",
            row=2,
            disabled=snapshot.volume == 100,
            emoji="🔊",
        ),
    ):
        view.add_item(button)
    return view


def _log(what: str) -> None:
    print(what, file=sys.stderr)
    print_exc(file=sys.stderr)


class PlayerPanel:
    """One per guild. `controller()` returns the guild's controller or
    `None` (read as inactive); `settings()` its settings or `None`."""

    def __init__(
        self, guild: discord.Guild, controller: Callable, settings: Callable
    ):
        self.guild = guild
        self._controller = controller
        self._settings = settings
        self._channel = None
        self._message = None
        self._drawn: Optional[PanelSnapshot] = None
        self._lock = asyncio.Lock()

    # -- destination ---------------------------------------------------

    def direct_to(self, channel, *, replace: bool = True) -> None:
        """Sets the destination to the raw `channel`; with `replace`
        false, only when none is set yet."""
        if replace or self._channel is None:
            self._channel = channel

    def carries_buttons(self, *, channel, ephemeral, has_view) -> bool:
        """Whether a reply sent in `channel` carries the player
        buttons: not ephemeral, with no view of its own, and in the
        destination (or any channel while none is set)."""
        if ephemeral or has_view:
            return False
        return self._channel is None or channel == self._channel

    # -- the controller's state ----------------------------------------

    def _snapshot(self) -> PanelSnapshot:
        controller = self._controller()
        if controller is None:
            return PanelSnapshot.inactive()
        return controller.panel_snapshot()

    def _layout(self, snapshot: PanelSnapshot):
        return build_view(snapshot, self.current_song, self.queue)

    # -- the player message --------------------------------------------

    def _forget(self) -> None:
        self._message = None
        self._drawn = None

    async def _strip(self, message, drawn) -> None:
        """Takes the buttons off `message`; a failure is logged, never
        raised. Nothing to do when it carries none."""
        if drawn is None or not drawn.active:
            return
        try:
            await message.edit(view=None)
        except Exception:
            _log("Failed to strip the player buttons:")

    async def attach(self, send):
        """Moves the buttons onto the message `send` sends.

        `send(view)` is an async callable that sends the message,
        carrying `view` when it is not None, and returns what it sent.
        That becomes the player message; an interaction stands for its
        original response. Returns what `send` returned. When `send`
        raises, the error propagates and no message is recorded.
        """
        async with self._lock:
            previous, drawn = self._message, self._drawn
            self._forget()
            if previous is not None:
                await self._strip(previous, drawn)
            snapshot = self._snapshot()
            res = await send(self._layout(snapshot))
            if isinstance(res, discord.Interaction):
                self._message = await res.original_response()
            else:
                self._message = res
            self._drawn = snapshot
        return res

    async def refresh(self) -> None:
        """Redraws the buttons when the snapshot changed since the
        last draw."""
        message = self._message
        if message is None:
            return
        snapshot = self._snapshot()
        if snapshot == self._drawn:
            return
        if not snapshot.active:
            drawn = self._drawn
            self._forget()
            await self._strip(message, drawn)
            return
        view = self._layout(snapshot)
        try:
            await message.edit(view=view)
            self._drawn = snapshot
        except discord.HTTPException as e:
            if e.code == INVALID_WEBHOOK_TOKEN:
                await self._recover(message, view, snapshot)
            else:
                _log("Failed to update the player buttons:")

    async def _recover(self, message, view, snapshot) -> None:
        """An expired token: refetch the message through its channel
        and edit it once more. Any failure forgets the message."""
        try:
            fresh = await message.channel.fetch_message(message.id)
        except discord.NotFound:
            self._forget()
            return
        except Exception:
            _log("Failed to refetch the player message:")
            self._forget()
            return
        if self._message is message:
            self._message = fresh
        try:
            await fresh.edit(view=view)
            self._drawn = snapshot
        except Exception:
            _log("Failed to update the refetched player message:")
            if self._message is fresh:
                self._forget()

    async def announce(self, song) -> None:
        """The now-playing announcement, when the guild's
        `announce_songs` is on and a destination is set. A failure is
        logged, never raised."""
        settings = self._settings()
        if settings is None or not settings.announce_songs:
            return
        await self._post(embed=song.format_output(config.SONGINFO_NOW_PLAYING))

    async def report_skipped(self, song) -> None:
        """The notice for a track that failed to load, sent to the
        destination if one is set. A failure is logged, never
        raised."""
        await self._post(
            content=f"{config.SONGINFO_ERROR}\n"
            f"(Skipped: {song.display_name})"
        )

    async def _post(self, **content) -> None:
        """Sends to the raw destination the way `attach` does, so the
        message becomes the player message. Sends nothing without a
        destination; never raises."""
        channel = self._channel
        if channel is None:
            return
        try:
            await self.attach(lambda view: channel.send(view=view, **content))
        except Exception:
            _log("Failed to send a playback message:")

    async def playback_ended(self) -> None:
        """Forgets the player message, then strips its buttons: a
        failed strip is logged and never retried or raised."""
        message, drawn = self._message, self._drawn
        self._forget()
        if message is not None:
            await self._strip(message, drawn)

    # -- button callbacks -----------------------------------------------

    async def current_song(self, ctx) -> None:
        song = self._snapshot().current_song
        if song is None:
            await ctx.send(config.QUEUE_EMPTY)
            return
        await ctx.send(embed=song.format_output(config.SONGINFO_SONGINFO))

    async def queue(self, ctx) -> None:
        controller = self._controller()
        if controller is None:
            return
        await ctx.send(embed=controller.queue_embed())
