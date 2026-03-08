from __future__ import annotations
from typing import Union
import asyncio

import discord
from discord.ext import commands

from config import config
from musicbot.bot import Context, MusicBot
from musicbot.settings import CONFIG_OPTIONS, ConversionError
from musicbot.audiocontroller import AudioController
from musicbot.utils import dj_check, voice_check


class General(commands.Cog):
    """A collection of the commands for moving the bot around in you server.

    Attributes:
        bot: The instance of the bot that is executing the commands.
    """

    def __init__(self, bot: MusicBot):
        self.bot = bot

    # logic is split to uconnect() for wide usage
    @commands.hybrid_command(
        name="connect",
        description=config.HELP_CONNECT_LONG,
        help=config.HELP_CONNECT_SHORT,
        aliases=["c", "cc"],  # this command replaces removed changechannel
    )
    @commands.check(voice_check)
    async def _connect(self, ctx):
        # connect only if not connected yet
        if not ctx.guild.voice_client:
            audiocontroller = ctx.bot.audio_controllers[ctx.guild]
            await audiocontroller.uconnect(ctx, move=True)
        await ctx.send("Connected.")

    @commands.hybrid_command(
        name="disconnect",
        description=config.HELP_DISCONNECT_LONG,
        help=config.HELP_DISCONNECT_SHORT,
        aliases=["dc"],
    )
    @commands.check(voice_check)
    async def _disconnect(self, ctx):
        await ctx.defer()  # ANNOUNCE_DISCONNECT will take a while
        audiocontroller = ctx.bot.audio_controllers[ctx.guild]
        if await audiocontroller.udisconnect():
            await ctx.send("Disconnected.")
        else:
            await ctx.send(config.NOT_CONNECTED_MESSAGE)

    @commands.hybrid_command(
        name="reset",
        description=config.HELP_RESET_LONG,
        help=config.HELP_RESET_SHORT,
        aliases=["rs", "restart"],
    )
    @commands.check(voice_check)
    async def _reset(self, ctx):
        await ctx.defer()
        if await ctx.bot.audio_controllers[ctx.guild].udisconnect():
            # bot was connected and need some rest
            await asyncio.sleep(1)

        audiocontroller = ctx.bot.audio_controllers[ctx.guild] = (
            AudioController(self.bot, ctx.guild)
        )
        await audiocontroller.uconnect(ctx)
        await ctx.send(
            "{} Connected to {}".format(
                ":white_check_mark:", ctx.author.voice.channel.name
            )
        )

    @commands.hybrid_command(
        name="ping",
        description=config.HELP_PING_LONG,
        help=config.HELP_PING_SHORT,
    )
    async def _ping(self, ctx):
        await ctx.send(f"Pong ({int(ctx.bot.latency * 1000)} ms)")

    @commands.hybrid_group(
        name="setting",
        description=config.HELP_SETTINGS_LONG,
        help=config.HELP_SETTINGS_SHORT,
        aliases=["settings", "set"],
        fallback="show",
    )
    async def _settings(self, ctx: commands.Context):
        sett = self.bot.settings[ctx.guild]
        await ctx.send(embed=sett.format(ctx))

    @_settings.command(name="command_channel")
    @commands.check(dj_check)
    async def _set_command_channel(self, ctx: commands.Context, channel: Union[discord.TextChannel, discord.VoiceChannel]):
        sett = self.bot.settings[ctx.guild]
        await sett.update_setting("command_channel", str(channel.id), ctx)
        await ctx.send(f"Setting `command_channel` updated to {channel.mention}!")

    @_settings.command(name="start_voice_channel")
    @commands.check(dj_check)
    async def _set_start_voice_channel(self, ctx: commands.Context, channel: discord.VoiceChannel):
        sett = self.bot.settings[ctx.guild]
        await sett.update_setting("start_voice_channel", str(channel.id), ctx)
        await ctx.send(f"Setting `start_voice_channel` updated to {channel.mention}!")

    @_settings.command(name="dj_role")
    @commands.check(dj_check)
    async def _set_dj_role(self, ctx: commands.Context, role: discord.Role):
        sett = self.bot.settings[ctx.guild]
        await sett.update_setting("dj_role", str(role.id), ctx)
        await ctx.send(f"Setting `dj_role` updated to {role.name}!")

    @_settings.command(name="user_must_be_in_vc")
    @commands.check(dj_check)
    async def _set_user_must_be_in_vc(self, ctx: commands.Context, value: bool):
        sett = self.bot.settings[ctx.guild]
        await sett.update_setting("user_must_be_in_vc", value, ctx)
        await ctx.send(f"Setting `user_must_be_in_vc` updated to {value}!")

    @_settings.command(name="button_emote")
    @commands.check(dj_check)
    async def _set_button_emote(self, ctx: commands.Context, emoji: str):
        sett = self.bot.settings[ctx.guild]
        try:
            await sett.update_setting("button_emote", emoji, ctx)
        except ConversionError as e:
            await ctx.send(f"`Error: {e}`")
            return
        await ctx.send(f"Setting `button_emote` updated to {emoji}!")

    @_settings.command(name="default_volume")
    @commands.check(dj_check)
    async def _set_default_volume(self, ctx: commands.Context, value: int):
        sett = self.bot.settings[ctx.guild]
        if value < 0 or value > 100:
            await ctx.send("`Error: Volume must be between 0 and 100.`")
            return
        await sett.update_setting("default_volume", value, ctx)
        await ctx.send(f"Setting `default_volume` updated to {value}!")

    @_settings.command(name="vc_timeout")
    @commands.check(dj_check)
    async def _set_vc_timeout(self, ctx: commands.Context, value: bool):
        sett = self.bot.settings[ctx.guild]
        await sett.update_setting("vc_timeout", value, ctx)
        await ctx.send(f"Setting `vc_timeout` updated to {value}!")

    @_settings.command(name="announce_songs")
    @commands.check(dj_check)
    async def _set_announce_songs(self, ctx: commands.Context, value: bool):
        sett = self.bot.settings[ctx.guild]
        await sett.update_setting("announce_songs", value, ctx)
        await ctx.send(f"Setting `announce_songs` updated to {value}!")

    @commands.Cog.listener()
    async def on_ready(self):
        pass

    @commands.hybrid_command(
        name="addbot",
        description=config.HELP_ADDBOT_LONG,
        help=config.HELP_ADDBOT_SHORT,
    )
    async def _addbot(self, ctx):
        embed = discord.Embed(
            title="Invite",
            description=config.ADD_MESSAGE.format(
                link=discord.utils.oauth_url(self.bot.user.id)
            ),
            color=config.EMBED_COLOR,
        )

        await ctx.send(embed=embed)


async def setup(bot: MusicBot):
    await bot.add_cog(General(bot))
