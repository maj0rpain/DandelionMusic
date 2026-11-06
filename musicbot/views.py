from inspect import isawaitable
from typing import List, Optional

import discord

from musicbot import admission
from musicbot.utils import CheckError


class OwnedView(discord.ui.View):
    """A view only its command's author may click, on a message that
    outlives the view's timeout."""

    def __init__(self, ctx, *, timeout=180):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        # whatever Context.send handed back, which is not always a
        # Message: the prefix paths and the deferred search path give
        # a Message/WebhookMessage, but answering an interaction
        # directly returns an InteractionCallbackResponse (discord.py
        # >= 2.5) that has no edit(). refresh_message() below
        # discriminates on the type rather than on which path ran.
        self.message = None
        # the latest accepted interaction: exactly the clicks that
        # restart discord.py's view timeout, so at on_timeout() it is
        # under the view's timeout old and its followup token still valid
        self._last_click: Optional[discord.Interaction] = None

    def claim_click(self) -> bool:
        """Claims the click: False defers it silently and refuses it;
        True accepts it, and an override may record the claim. Called
        exactly once per click the owner check admits, after the owner
        check and before the click's first await."""
        return True

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This belongs to someone else.", ephemeral=True
            )
            return False
        if not self.claim_click():
            await interaction.response.defer()
            return False
        self._last_click = interaction
        return True

    async def on_timeout(self):
        # Without this, a click after the view's timeout just fails
        # silently client-side (discord.py stops dispatching to a
        # timed-out view's items) and the message's Select/Buttons
        # stay visibly enabled forever.
        self.disable()
        await self.refresh_message()

    def disable(self) -> None:
        for item in self.children:
            item.disabled = True

    async def refresh_message(self) -> None:
        """Pushes the view's current state onto its sent message."""
        try:
            # A view sent after a defer goes out as a followup with its
            # own id, so @original is only the deferred placeholder and
            # editing it would leave the real components enabled
            # forever - those have to be edited directly. Answering an
            # interaction without deferring puts the view on the
            # original response, and hands back an
            # InteractionCallbackResponse rather than the message, so
            # that case has to go through the interaction. Testing what
            # we actually hold keeps this right whichever path ran.
            #
            # A bot-sent channel message (the prefix paths) is edited
            # directly: its bot token never expires. Anything else
            # goes through the last accepted click's followup when
            # there is one, because the command's interaction token
            # dies 15 minutes after the command while each click
            # restarts the view's timeout. Edited by id, never via
            # edit_original_response: after a bulk queue that click's
            # original response is the "Queueing..." placeholder.
            click = self._last_click
            if isinstance(self.message, discord.Message) and not isinstance(
                self.message, discord.WebhookMessage
            ):
                await self.message.edit(view=self)
            elif click is not None:
                await click.followup.edit_message(click.message.id, view=self)
            elif isinstance(self.message, discord.Message):
                await self.message.edit(view=self)
            elif self.ctx.interaction is not None:
                await self.ctx.interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class SimplePaginator(OwnedView):
    """`d!playlist show`'s listing: one embed per page, paged by Prev and
    Next. Only the command's author may page it."""

    def __init__(self, ctx, pages: List[discord.Embed]):
        super().__init__(ctx)
        self.pages = pages
        self.current_page = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_page.disabled = self.current_page <= 0
        self.next_page.disabled = self.current_page >= len(self.pages) - 1

    async def send(self) -> None:
        if not self.pages:
            return
        if len(self.pages) == 1:
            await self.ctx.send(embed=self.pages[0])
            return
        self.message = await self.ctx.send(embed=self.pages[0], view=self)

    async def _turn(self, interaction: discord.Interaction, step: int):
        page = self.current_page + step
        if not 0 <= page < len(self.pages):
            # a stale client or two quick clicks: nothing to turn to
            await interaction.response.defer()
            return
        self.current_page = page
        self._update_buttons()
        await interaction.response.edit_message(
            embed=self.pages[page], view=self
        )

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.grey)
    async def prev_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._turn(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.grey)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._turn(interaction, 1)


class MusicButton(discord.ui.Button):
    """A button running `callback` once admission admits the click as
    its request `kind`. A player button (the default kind) never
    touches voice and is refused while the bot is out of voice; a
    pick (`RequestKind.PICK`) acts in a joining command's name, so
    `enter()` joins voice before its action. An optional async
    `after(ctx)` runs once the action has succeeded; a refused click
    runs none."""

    def __init__(
        self,
        callback,
        kind=admission.RequestKind.PLAYER_BUTTON,
        *,
        after=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._callback = callback
        self._kind = kind
        self._after = after

    async def callback(self, inter: discord.Interaction):
        # Build ctx first, then acknowledge: get_context makes no HTTP
        # call for an interaction (Context.from_interaction awaits
        # nothing), so awaiting it ahead of the defer cannot delay the
        # response, and a failure at any later step still has a ctx to
        # report through. Acknowledge before the checks and the join:
        # a join can be slow, and it must not push the response past
        # Discord's 3-second deadline. Refusals then go out as
        # ephemeral followups. enter() runs only once the check has
        # passed, so a refused click never makes the bot join.
        ctx = await inter.client.get_context(inter)
        try:
            await inter.response.defer()
            try:
                admitted = await admission.check(ctx, self._kind)
                ctx.audiocontroller = await admitted.enter()
            except CheckError as e:
                self.on_refused()
                await ctx.send(e, ephemeral=True)
                return
            res = self._callback(ctx)
            if isawaitable(res):
                await res

            if self._after is not None:
                await self._after(ctx)
        except Exception as e:
            await self.on_failure(ctx, e)

    async def on_failure(self, ctx, error):
        """Called for any failure that is not a refusal: a failed
        defer, or an exception (a CheckError included) from the
        action or the `after` step. Re-raises by default."""
        raise error

    def on_refused(self):
        """Called for every CheckError that refuses a click (its
        check, the DJ check or a failed voice join), before the
        ephemeral refusal goes out. Does nothing by default."""
