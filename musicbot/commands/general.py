from __future__ import annotations
import asyncio
from typing import Optional

import discord
from discord.ext import commands
from discord.ext.commands.view import StringView

from config import config
from musicbot import admission
from musicbot.bot import MusicBot
from musicbot.settings import SETTINGS, ConversionError, SettingDescriptor
from musicbot.utils import (
    CheckError,
    dj_check,
    get_settings,
    joining_command,
    settings_admin_check,
)

# the group's default subcommand, and the one word that is not an
# unknown setting
SHOW_SUBCOMMAND = "show"


def _unmatched_word(view: StringView) -> Optional[str]:
    """The word after the command name that matched no subcommand, or
    None when there is none (a slash invocation, or a bare command).
    Leaves the view where it was."""
    # discord.py clears ctx.subcommand_passed before the group callback
    # runs, but leaves the view at the word after the command name
    # (empty on a slash invocation): a prefix subcommand never reaches
    # the callback, so that word matched none. skip_ws is needed as the
    # view stops before the whitespace, and one undo() could not
    # restore it, as get_word sets previous past the skipped whitespace
    start = view.index
    view.skip_ws()
    word = view.get_word()
    view.index = start
    return word or None


async def _apply_setting(ctx, descriptor: SettingDescriptor, value):
    """The body of every d!setting <name> subcommand: replies with the
    refusal or error, or confirms the update"""
    refusal = descriptor.edit_gate() if descriptor.edit_gate else None
    if refusal is not None:
        await ctx.send(refusal)
        return
    sett = get_settings(ctx)
    failure = f"`Error: Setting {descriptor.name} could not be updated.`"
    if value is None:
        if not await sett.reset_setting(descriptor, ctx):
            await ctx.send(failure)
            return
        await ctx.send(
            config.SETTING_RESET.format(
                descriptor.name, descriptor.show(ctx, descriptor.default)
            )
        )
        return
    try:
        updated = await sett.update_setting(descriptor, value, ctx)
    except ConversionError as e:
        await ctx.send(f"`Error: {e}`")
        return
    if not updated:
        await ctx.send(failure)
        return
    await ctx.send(
        f"Setting `{descriptor.name}` updated to"
        f" {descriptor.confirm(value)}!"
    )


def _setting_subcommand(descriptor: SettingDescriptor, owner: str):
    """The callback of the d!setting subcommand for one setting, taking
    the descriptor's parameter name and type; owner is the qualname of
    the class the callback becomes a method of"""

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
    callback.__defaults__ = (None,)
    callback.__annotations__ = {
        "ctx": commands.Context,
        descriptor.param_name: descriptor.param_type,
    }
    callback.__name__ = f"_set_{descriptor.name}"
    # discord.py's is_inside_class reads the qualname to decide whether
    # the callback takes self: without this override it would end in
    # <locals>, self would not be skipped and every subcommand would break
    callback.__qualname__ = f"{owner}._set_{descriptor.name}"
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
    @joining_command
    async def _connect(self, ctx):
        await ctx.defer()  # a connect or move can outlast the deadline
        # CheckError while the bot is starting, or on a refusal
        admitted = await admission.check(ctx)
        if admitted.join is admission.Join.NONE:
            # nothing to do: the bot stays where it is
            raise CheckError(config.ALREADY_CONNECTED_MESSAGE)
        await admitted.enter()
        if admitted.join is admission.Join.MOVE:
            await ctx.send("Moved to your voice channel.")
        else:
            await ctx.send("Connected.")

    @commands.hybrid_command(
        name="disconnect",
        description=config.HELP_DISCONNECT_LONG,
        help=config.HELP_DISCONNECT_SHORT,
        aliases=["dc"],
    )
    @commands.check(dj_check)
    async def _disconnect(self, ctx):
        await ctx.defer()  # ANNOUNCE_DISCONNECT will take a while
        audiocontroller = await (await admission.check(ctx)).enter()
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
    @joining_command
    @commands.check(dj_check)
    async def _reset(self, ctx):
        await ctx.defer()
        # a refused user resets nothing; a reset is the recovery from a
        # disconnect in progress, so that one refusal is skipped
        await admission.check(ctx, allow_disconnecting=True)
        was_connected = ctx.guild.voice_client is not None
        # reset() disposes of the old controller, which disconnects it -
        # so not here as well: a second udisconnect() would back the
        # already-cleared queue up over the one d!restore reloads
        await ctx.bot.sessions.reset(ctx.guild, "reset command")
        if was_connected:
            # bot was connected and need some rest
            await asyncio.sleep(1)

        if ctx.guild.voice_client is not None:
            # enter() would do nothing, or move the bot
            raise CheckError(config.ALREADY_CONNECTED_MESSAGE)
        # a refusal here refuses only the join: the reset stands
        await (await admission.check(ctx)).enter()
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
        fallback=SHOW_SUBCOMMAND,
        invoke_without_command=True,
    )
    async def _settings(self, ctx: commands.Context):
        word = _unmatched_word(ctx.view)
        if word is not None and word != SHOW_SUBCOMMAND:
            raise CheckError(config.UNKNOWN_SETTING.format(word))
        sett = get_settings(ctx)
        await ctx.send(embed=sett.format(ctx))

    for _descriptor in SETTINGS.values():
        vars()[f"_set_{_descriptor.name}"] = _settings.command(
            name=_descriptor.name
        )(
            commands.check(settings_admin_check)(
                _setting_subcommand(_descriptor, __qualname__)
            )
        )
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
