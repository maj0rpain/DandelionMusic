from __future__ import annotations
import asyncio

import discord
from discord.ext import commands

from config import config
from musicbot.bot import MusicBot
from musicbot.settings import SETTINGS, ConversionError, SettingDescriptor
from musicbot.utils import (
    CheckError,
    dj_check,
    get_audiocontroller,
    get_settings,
    join_voice,
    joins_voice,
    voice_check,
)


async def _update_or_report(ctx, sett, setting: str, value) -> bool:
    """Updates a guild setting, replying with an error if it failed"""
    if await sett.update_setting(setting, value, ctx):
        return True
    await ctx.send(f"`Error: Setting {setting} could not be updated.`")
    return False


async def _apply_setting(ctx, descriptor: SettingDescriptor, value):
    """The body of every d!setting <name> subcommand: replies with the
    refusal or error, or confirms the update"""
    refusal = descriptor.edit_gate() if descriptor.edit_gate else None
    if refusal is not None:
        await ctx.send(refusal)
        return
    sett = get_settings(ctx)
    try:
        updated = await _update_or_report(ctx, sett, descriptor.name, value)
    except ConversionError as e:
        await ctx.send(f"`Error: {e}`")
        return
    if updated:
        await ctx.send(
            f"Setting `{descriptor.name}` updated to"
            f" {descriptor.confirm(value)}!"
        )


def _setting_subcommand(descriptor: SettingDescriptor):
    """The callback of the d!setting subcommand for one setting, taking
    the descriptor's parameter name and type"""

    async def callback(self, ctx: commands.Context, value):
        await _apply_setting(ctx, descriptor, value)

    # discord.py reads the parameter's name and type from the
    # callback's own code and annotations, and a slash command passes
    # the value by that name. A __signature__ would not do: discord.py
    # deletes it after reading it once, and the cog reads it again
    # when it copies its commands.
    code = callback.__code__
    callback.__code__ = code.replace(
        co_varnames=("self", "ctx", descriptor.param_name)
        + code.co_varnames[3:]
    )
    callback.__annotations__ = {
        "ctx": commands.Context,
        descriptor.param_name: descriptor.param_type,
    }
    callback.__name__ = f"_set_{descriptor.name}"
    callback.__qualname__ = f"General._set_{descriptor.name}"
    return callback


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
    @joins_voice
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
    @joins_voice
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

    for _descriptor in SETTINGS.values():
        vars()[f"_set_{_descriptor.name}"] = _settings.command(
            name=_descriptor.name
        )(commands.check(dj_check)(_setting_subcommand(_descriptor)))
    del _descriptor

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
