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
import dataclasses
from dataclasses import dataclass
from traceback import print_exc
from typing import Callable, Optional

import discord
from config import config

from musicbot import admission
from musicbot.panel_snapshot import PanelSnapshot
from musicbot.views import MusicButton

__all__ = [
    "PanelHooks",
    "PanelSnapshot",
    "PlayerPanel",
    "build_view",
    "send_song_info",
]

INVALID_WEBHOOK_TOKEN = 50027


@dataclass(frozen=True)
class PanelHooks:
    """The panel's click hooks: what the current-song and queue buttons
    run, and the step awaited after a successful click - `after_skip`
    for the `SKIP_IDS` buttons, `after_other` for every other."""

    current_song: Optional[Callable]
    queue: Optional[Callable]
    after_skip: Optional[Callable]
    after_other: Optional[Callable]


SKIP_IDS = frozenset({"prev", "next"})


async def send_song_info(ctx, song) -> None:
    """The current-song reply: the queue-empty message when `song` is
    `None`, otherwise the song's info embed."""
    if song is None:
        await ctx.send(config.QUEUE_EMPTY)
        return
    await ctx.send(embed=song.format_output(config.SONGINFO_SONGINFO))


def build_view(
    snapshot: PanelSnapshot, hooks: PanelHooks
) -> Optional[discord.ui.View]:
    """The player buttons, a pure function of `snapshot`: `None` while
    inactive. The playback buttons call the clicking context's
    controller; the current-song and queue buttons run `hooks`'
    callbacks. After a successful click, the `SKIP_IDS` buttons await
    `hooks.after_skip(ctx)` and every other button
    `hooks.after_other(ctx)`."""
    if not snapshot.active:
        return None

    def music_button(*args, custom_id, **kwargs):
        after = (
            hooks.after_skip if custom_id in SKIP_IDS else hooks.after_other
        )
        return MusicButton(*args, after=after, custom_id=custom_id, **kwargs)

    view = discord.ui.View(timeout=None)
    for button in (
        music_button(
            lambda ctx: ctx.audiocontroller.prev_song(),
            admission.RequestKind.DJ_PLAYER_BUTTON,
            custom_id="prev",
            disabled=not snapshot.has_previous,
            emoji="⏮️",
        ),
        music_button(
            lambda ctx: ctx.audiocontroller.pause(),
            admission.RequestKind.DJ_PLAYER_BUTTON,
            custom_id="pause",
            emoji="▶️" if snapshot.paused else "⏸️",
        ),
        music_button(
            lambda ctx: ctx.audiocontroller.skip(),
            admission.RequestKind.DJ_PLAYER_BUTTON,
            custom_id="next",
            disabled=not snapshot.has_next,
            emoji="⏭️",
        ),
        music_button(
            lambda ctx: ctx.audiocontroller.loop(),
            admission.RequestKind.DJ_PLAYER_BUTTON,
            custom_id="loop",
            disabled=snapshot.queue_empty,
            emoji="🔁",
            label="Loop: " + snapshot.loop_mode,
        ),
        music_button(
            hooks.current_song,
            custom_id="current_song",
            row=1,
            disabled=snapshot.current_song is None,
            emoji="💿",
        ),
        music_button(
            lambda ctx: ctx.audiocontroller.shuffle(),
            admission.RequestKind.DJ_PLAYER_BUTTON,
            custom_id="shuffle",
            row=1,
            disabled=snapshot.queue_empty,
            emoji="🔀",
        ),
        music_button(
            hooks.queue,
            custom_id="queue",
            row=1,
            disabled=snapshot.queue_empty,
            emoji="📜",
        ),
        music_button(
            lambda ctx: ctx.audiocontroller.stop(),
            admission.RequestKind.DJ_PLAYER_BUTTON,
            custom_id="stop",
            row=1,
            emoji="⏹️",
            style=discord.ButtonStyle.red,
        ),
        music_button(
            lambda ctx: ctx.audiocontroller.volume_down(),
            admission.RequestKind.DJ_PLAYER_BUTTON,
            custom_id="volume_down",
            row=2,
            disabled=snapshot.volume == 0,
            emoji="🔉",
        ),
        music_button(
            lambda ctx: ctx.audiocontroller.volume_up(),
            admission.RequestKind.DJ_PLAYER_BUTTON,
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


def _report_message_failure(error: Exception, what: str) -> None:
    """The one place a failed read or edit of the player message is
    reported,
    called from the handler of `error`: a `discord.NotFound` means a
    user deleted the message, which is normal and silent; anything else
    is logged with `what` and a traceback. It only reports - forgetting
    the message stays with each site."""
    if isinstance(error, discord.NotFound):
        return
    _log(what)


@dataclass(frozen=True)
class _Drawn:
    """A draw of the player message: the message with the snapshot it
    was drawn from and the view drawn on it (`None` while inactive).
    It is held (`self._held`) once it is on the message - sent by
    `attach`, or its edit succeeded - and pending before that. While
    `_recover` retries, the held record carries the refetched message
    with the previous snapshot and view."""

    message: object
    snapshot: PanelSnapshot
    view: Optional[discord.ui.View]


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
        self._held: Optional[_Drawn] = None
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
        async def after_skip(ctx):
            await ctx.send(f"{ctx.author} Skipped a Song")

        async def after_other(ctx):
            await self.refresh()

        return build_view(
            snapshot,
            PanelHooks(self.current_song, self.queue, after_skip, after_other),
        )

    # -- the player message --------------------------------------------

    def _forget(self) -> None:
        """Lets go of the player message and stops the view drawn on
        it, the one place a drawn view's life ends: stopping removes its
        buttons from discord.py's view store, which an edit with no view
        does not."""
        held = self._held
        self._held = None
        if held is not None and held.view is not None:
            held.view.stop()

    def _is_current(self, message) -> bool:
        return self._held is not None and self._held.message is message

    async def _strip(self, held: _Drawn) -> None:
        """Takes the buttons off the held message; a failure is
        reported, never raised. Nothing to do when it carries none."""
        if not held.snapshot.active:
            return
        try:
            await held.message.edit(view=None)
        except Exception as e:
            _report_message_failure(e, "Failed to strip the player buttons:")

    async def _take_down(self) -> None:
        """Forgets the player message and its drawn snapshot, then
        strips the old message's buttons, if there was one."""
        held = self._held
        self._forget()
        if held is not None:
            await self._strip(held)

    async def attach(self, send):
        """Moves the buttons onto the message `send` sends.

        `send(view)` is an async callable that sends the message,
        carrying `view` when it is not None, and returns what it sent.
        That becomes the player message. Returns what `send` returned.
        When `send` raises, the error propagates and no message is
        recorded.
        """
        async with self._lock:
            await self._take_down()
            snapshot = self._snapshot()
            view = self._layout(snapshot)
            res = await send(view)
            self._held = _Drawn(res, snapshot, view)
        return res

    async def refresh(self) -> None:
        """Redraws the buttons when the snapshot changed since the
        last draw."""
        held = self._held
        if held is None:
            return
        message = held.message
        snapshot = self._snapshot()
        if snapshot == held.snapshot:
            return
        if not snapshot.active:
            await self._take_down()
            return
        drawn = _Drawn(message, snapshot, self._layout(snapshot))
        try:
            await message.edit(view=drawn.view)
            self._swap_view(drawn)
        except discord.HTTPException as e:
            if e.code == INVALID_WEBHOOK_TOKEN:
                await self._recover(drawn)
            elif isinstance(e, (discord.NotFound, discord.Forbidden)):
                # The message can never be edited again: stop retrying
                # it every tick. Deleting it is normal, so only a lost
                # access is logged.
                _report_message_failure(
                    e, "Lost access to the player message:"
                )
                self._forget_if_current(message)
            else:
                _report_message_failure(
                    e, "Failed to update the player buttons:"
                )

    async def _recover(self, drawn: _Drawn) -> None:
        """An expired token: refetch the message `drawn` was meant for
        through its channel and edit it once more. Any failure forgets
        the message."""
        message = drawn.message
        try:
            fresh = await message.channel.fetch_message(message.id)
        except Exception as e:
            _report_message_failure(e, "Failed to refetch the player message:")
            self._forget_if_current(message)
            return
        if not self._is_current(message):
            return
        self._held = dataclasses.replace(self._held, message=fresh)
        try:
            await fresh.edit(view=drawn.view)
            self._swap_view(dataclasses.replace(drawn, message=fresh))
        except Exception as e:
            _report_message_failure(
                e, "Failed to update the refetched player message:"
            )
            self._forget_if_current(fresh)

    def _swap_view(self, drawn: _Drawn) -> None:
        """A redraw edit of `drawn.message` succeeded: `drawn` is now
        what is held. When that message is no longer the player
        message, a take-down ran meanwhile and already stopped the old
        view, so the new one is stopped at once."""
        if not self._is_current(drawn.message):
            drawn.view.stop()
            return
        # The replaced view is deliberately not stopped: the new view
        # has the same custom ids under the same message id, so
        # discord.py's view store overwrote the old view's entries in
        # place, and stopping the old one now would remove the entries
        # that belong to the new view and kill the live buttons.
        self._held = drawn

    def _forget_if_current(self, message) -> None:
        """Forgets `message` only when it is still the panel's
        current one."""
        if self._is_current(message):
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
        await self._take_down()

    # -- button callbacks -----------------------------------------------

    async def current_song(self, ctx) -> None:
        await send_song_info(ctx, self._snapshot().current_song)

    async def queue(self, ctx) -> None:
        controller = self._controller()
        if controller is None:
            return
        await ctx.send(embed=controller.queue_embed())
