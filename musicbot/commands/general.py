from __future__ import annotations
from typing import Union
import asyncio

import discord
from discord.ext import commands

from config import config
from musicbot.bot import MusicBot
from musicbot.settings import ConversionError
from musicbot.utils import (
    CheckError,
    dj_check,
    get_audiocontroller,
    get_settings,
    join_voice,
    voice_check,
)


async def _update_or_report(ctx, sett, setting: str, value) -> bool:
    """Updates a guild setting, replying with an error if it failed"""
    if await sett.update_setting(setting, value, ctx):
        return True
    await ctx.send(f"`Error: Setting {setting} could not be updated.`")
    return False


class General(commands.Cog):
    """A collection of the commands for moving the bot around in you server.

    Attributes:
        bot: The instance of the bot that is executing the commands.
    """

    def __init__(self, bot: MusicBot):
        self.bot = bot

    @commands.hybrid_command(
        name="connect",
        description=config.HELP_CONNECT_LONG,
        help=config.HELP_CONNECT_SHORT,
        aliases=["c", "cc"],  # this command replaces removed changechannel
    )
    @commands.check(voice_check)
    async def _connect(self, ctx):
        # connects, or moves the bot out of a channel with only bots
        # in it; otherwise the bot stays where it is
        get_audiocontroller(ctx)  # CheckError while the bot is starting
        await join_voice(ctx)
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
        audiocontroller = get_audiocontroller(ctx)
        if await audiocontroller.udisconnect("command"):
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
        get_audiocontroller(ctx)  # CheckError while the bot is starting
        was_connected = ctx.guild.voice_client is not None
        # reset() disposes of the old controller, which disconnects it -
        # so not here as well: a second udisconnect() would back the
        # already-cleared queue up over the one d!restore reloads
        await ctx.bot.sessions.reset(ctx.guild, "reset command")
        if was_connected:
            # bot was connected and need some rest
            await asyncio.sleep(1)

        if ctx.guild.voice_client is not None:
            # join_voice() would do nothing, or move the bot
            raise CheckError(config.ALREADY_CONNECTED_MESSAGE)
        await join_voice(ctx)
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
        sett = get_settings(ctx)
        await ctx.send(embed=sett.format(ctx))

    @_settings.command(name="command_channel")
    @commands.check(dj_check)
    async def _set_command_channel(
        self,
        ctx: commands.Context,
        channel: Union[
            discord.Thread, discord.VoiceChannel, discord.TextChannel
        ],
    ):
        sett = get_settings(ctx)
        if not await _update_or_report(ctx, sett, "command_channel", channel):
            return
        await ctx.send(
            f"Setting `command_channel` updated to {channel.mention}!"
        )

    @_settings.command(name="start_voice_channel")
    @commands.check(dj_check)
    async def _set_start_voice_channel(
        self, ctx: commands.Context, channel: discord.VoiceChannel
    ):
        sett = get_settings(ctx)
        if not await _update_or_report(
            ctx, sett, "start_voice_channel", channel
        ):
            return
        await ctx.send(
            f"Setting `start_voice_channel` updated to {channel.mention}!"
        )

    @_settings.command(name="dj_role")
    @commands.check(dj_check)
    async def _set_dj_role(self, ctx: commands.Context, role: discord.Role):
        sett = get_settings(ctx)
        if not await _update_or_report(ctx, sett, "dj_role", role):
            return
        await ctx.send(f"Setting `dj_role` updated to {role.name}!")

    @_settings.command(name="user_must_be_in_vc")
    @commands.check(dj_check)
    async def _set_user_must_be_in_vc(
        self, ctx: commands.Context, value: bool
    ):
        sett = get_settings(ctx)
        if not await _update_or_report(ctx, sett, "user_must_be_in_vc", value):
            return
        await ctx.send(f"Setting `user_must_be_in_vc` updated to {value}!")

    @_settings.command(name="button_emote")
    @commands.check(dj_check)
    async def _set_button_emote(self, ctx: commands.Context, emoji: str):
        sett = get_settings(ctx)
        try:
            updated = await _update_or_report(ctx, sett, "button_emote", emoji)
        except ConversionError as e:
            await ctx.send(f"`Error: {e}`")
            return
        if not updated:
            return
        await ctx.send(f"Setting `button_emote` updated to {emoji}!")

    @_settings.command(name="default_volume")
    @commands.check(dj_check)
    async def _set_default_volume(self, ctx: commands.Context, value: int):
        sett = get_settings(ctx)
        if value < 0 or value > 100:
            await ctx.send("`Error: Volume must be between 0 and 100.`")
            return
        if not await _update_or_report(ctx, sett, "default_volume", value):
            return
        await ctx.send(f"Setting `default_volume` updated to {value}!")

    @_settings.command(name="vc_timeout")
    @commands.check(dj_check)
    async def _set_vc_timeout(self, ctx: commands.Context, value: bool):
        if not config.ALLOW_VC_TIMEOUT_EDIT:
            await ctx.send(config.VC_TIMEOUT_EDIT_DISABLED)
            return
        sett = get_settings(ctx)
        if not await _update_or_report(ctx, sett, "vc_timeout", value):
            return
        await ctx.send(f"Setting `vc_timeout` updated to {value}!")

    @_settings.command(name="announce_songs")
    @commands.check(dj_check)
    async def _set_announce_songs(self, ctx: commands.Context, value: bool):
        sett = get_settings(ctx)
        if not await _update_or_report(ctx, sett, "announce_songs", value):
            return
        await ctx.send(f"Setting `announce_songs` updated to {value}!")

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
