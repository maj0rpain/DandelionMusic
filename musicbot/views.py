from typing import Optional

import discord


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

    def accepts_click(self) -> bool:
        """Subclass hook: False defers the click silently and refuses
        it. Runs after the owner check, before the click's first
        await."""
        return True

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This belongs to someone else.", ephemeral=True
            )
            return False
        if not self.accepts_click():
            await interaction.response.defer()
            return False
        self._last_click = interaction
        return True

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
