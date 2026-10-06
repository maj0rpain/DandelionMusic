import discord
from discord.ext import commands
from musicbot import linkutils, utils
from musicbot.bot import MusicBot


SUPPORTED_SITES = (
    linkutils.SiteTypes.SPOTIFY,
    linkutils.SiteTypes.YT_DLP,
)


class Button(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    @staticmethod
    def get_links(text: str):
        return [
            link
            for link in linkutils.get_urls(text)
            if linkutils.identify_url(link) in SUPPORTED_SITES
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
            chan = serv.get_channel(reaction.channel_id)
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
                if not await utils.voice_check(ctx):
                    return
            except utils.CheckError:
                return
            await audiocontroller.register_voice_channel(user_vc.channel)
            if not audiocontroller.command_channel and sett.command_channel:
                audiocontroller.command_channel = serv.get_channel(
                    int(sett.command_channel)
                )
            for url in links:
                await audiocontroller.process_song(url, user=member)


async def setup(bot: MusicBot):
    await bot.add_cog(Button(bot))
