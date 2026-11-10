import discord
from discord.ext import commands
from musicbot import admission, linkutils, loader, utils
from musicbot.message_link import gate_or_refuse
from musicbot.bot import MusicBot
from musicbot.outcome import send_outcome

SUPPORTED_SITES = (
    linkutils.SiteTypes.SPOTIFY,
    linkutils.SiteTypes.YT_DLP,
    linkutils.SiteTypes.SHORT_LINK,
)


class Button(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    @staticmethod
    def get_links(text: str):
        links = map(linkutils.normalize_track, linkutils.get_urls(text))
        return [
            link
            for link in links
            if linkutils.get_site_type(link) in SUPPORTED_SITES
        ]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author == self.bot.user:
            return

        await self.bot.absolutely_ready

        sett = self.bot.sessions.settings(message.guild)
        if sett is None:
            return
        button = sett.button_emote

        if not button:
            return

        emoji = utils.get_emoji(self.bot, button)
        if not emoji:
            return

        if self.get_links(message.content):
            await message.add_reaction(emoji)

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self, reaction: discord.RawReactionActionEvent
    ):
        serv = self.bot.get_guild(reaction.guild_id)

        member = reaction.member
        # member is None for reactions outside a guild (e.g. DMs)
        if not serv or member is None or member.bot:
            return

        user_vc = member.voice
        if not user_vc:
            return

        # Both halves, before either is used: a raw reaction event is
        # not gated behind absolutely_ready (unlike on_message above),
        # and while on_ready runs a guild can have its settings but no
        # controller yet - or neither.
        sett = self.bot.sessions.settings(serv)
        audiocontroller = self.bot.sessions.controller(serv)
        if sett is None or audiocontroller is None:
            return
        button = sett.button_emote

        if not button:
            return

        if (
            reaction.emoji.name == button
            or str(reaction.emoji.id or "") == button
        ):
            chan = serv.get_channel_or_thread(reaction.channel_id)
            if chan is None:
                return
            message = await chan.fetch_message(reaction.message_id)

            links = self.get_links(message.content)

            if not links:
                return

            if chan.permissions_for(serv.me).manage_messages:
                await message.remove_reaction(reaction.emoji, member)

            ctx = await self.bot.get_context(message)
            # author is the user who added the reaction,
            # not the one who sent the message
            ctx.author = member
            try:
                admitted = await admission.check(
                    ctx, admission.RequestKind.REACTION
                )
            except utils.CheckError:
                return
            checked_tracks = []
            for url in links:
                checked = await gate_or_refuse(url, serv, member, ctx.send)
                if checked is not None:
                    checked_tracks.append(checked)
            if not checked_tracks:
                return
            try:
                audiocontroller = await admitted.enter()
            except utils.CheckError:
                return
            if sett.command_channel:
                channel = serv.get_channel_or_thread(int(sett.command_channel))
                if channel is not None:
                    self.bot.sessions.direct_panel(
                        serv, channel, replace=False
                    )
            for checked in checked_tracks:
                try:
                    outcome = await audiocontroller.process_song(
                        checked, user=member
                    )
                except loader.SongError as e:
                    await ctx.send(str(e))
                    continue
                await send_outcome(ctx, outcome)


async def setup(bot: MusicBot):
    await bot.add_cog(Button(bot))
