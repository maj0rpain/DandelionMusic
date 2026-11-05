from __future__ import annotations
import os
import sys
import shlex
import asyncio
import builtins
from io import StringIO
from typing import List
from textwrap import TextWrapper
from traceback import print_exc

import discord
from discord.ext import commands
from discord import app_commands
from aioconsole import aexec

from musicbot.bot import MusicBot
from musicbot import guild_whitelist
from musicbot.guild_whitelist import Outcome
from musicbot.utils import owner_check


class Splitter(TextWrapper):
    def __init__(self, width: int):
        super().__init__(
            width, replace_whitespace=False, drop_whitespace=False, tabsize=4
        )

    def _split(self, text: str) -> List[str]:
        return text.splitlines(True)

    def _handle_long_word(
        self,
        reversed_chunks: List[str],
        cur_line: List[str],
        cur_len: int,
        width: int,
    ) -> None:
        # split by words if possible
        split_chunk = super()._split(reversed_chunks.pop())
        split_chunk.reverse()
        reversed_chunks.extend(split_chunk)
        super()._handle_long_word(reversed_chunks, cur_line, cur_len, width)


OUTPUT_FORMAT = "```\n\u200b{}\n```"
_paginate = Splitter(2002 - len(OUTPUT_FORMAT)).wrap


def _pages(output: str) -> List[str]:
    """Split output into code-block pages; [] for empty/blank output."""
    if not output or output.isspace():
        return []
    pages = (page.rstrip() for page in _paginate(output))
    return [OUTPUT_FORMAT.format(page) for page in pages if page]


async def run_and_report(ctx, argv: List[str]) -> None:
    """Run a command without blocking the loop and reply with its output."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    raw, _ = await process.communicate()
    output = raw.decode("utf-8", errors="replace")

    pages = _pages(output)
    if pages:
        for page in pages:
            await ctx.send(page)
    else:
        await ctx.send("No output.")
    if process.returncode:
        await ctx.send(
            f"`{shlex.join(argv)}` exited with code {process.returncode}"
        )


def _guild_line(bot, guild_id: int) -> str:
    """`id name` for a guild the bot is in, else the bare id."""
    guild = bot.get_guild(guild_id)
    return f"{guild_id} {guild.name}" if guild else str(guild_id)


class Developer(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    @commands.command(
        name="shutdown",
        hidden=True,
    )
    @commands.check(owner_check)
    async def _shutdown(self, ctx):
        await ctx.send("Shutting down...")
        # hide SystemExit error message
        sys.excepthook = lambda *_: None
        sys.exit()

    @commands.command(
        name="update",
        hidden=True,
    )
    @commands.check(owner_check)
    async def _update(self, ctx):
        await ctx.send("Updating...")
        await run_and_report(ctx, ["git", "pull"])

    @commands.command(
        name="execute",
        hidden=True,
        aliases=("exec",),
    )
    @commands.check(owner_check)
    async def _execute(self, ctx, *, code: str):
        if code.startswith("```"):
            code = code.partition("\n")[2].rstrip("`")
        else:
            code = code.strip("`")

        namespace = {
            "ctx": ctx,
            "bot": ctx.bot,
            "discord": discord,
            "asyncio": asyncio,
        }

        output = StringIO()

        def captured_print(*args, **kwargs):
            if kwargs.get("file") is None:
                kwargs["file"] = output
            builtins.print(*args, **kwargs)

        namespace["print"] = captured_print
        try:
            await aexec(code, namespace)
        except Exception:
            print_exc(file=output)
        output = output.getvalue()

        if output and not output.isspace():
            pages = (page.rstrip() for page in _paginate(output))
            pages = [OUTPUT_FORMAT.format(page) for page in pages if page]
            if len(pages) == 1:
                await ctx.send(pages[0])
            else:
                for page in pages:
                    await ctx.send(page)
        else:
            try:
                suppress = ctx.channel.last_message.author == ctx.me
            except AttributeError:
                suppress = False
            if not suppress:
                await ctx.send("No output.")

    @commands.hybrid_group(
        name="guild_whitelist",
        aliases=("gw",),
        usage="[action [guild id]]",
        invoke_without_command=True,
    )
    @commands.check(owner_check)
    async def _guild_whitelist(self, ctx, *, inexistent_subcommand=None):
        if inexistent_subcommand is not None:
            await ctx.send("`Error: Unknown subcommand`")
        else:
            await self._show_guild_whitelist_callback(ctx)

    async def _show_guild_whitelist_callback(self, ctx):
        whitelist = await guild_whitelist.load(ctx.bot.DbSession)
        if not whitelist.enabled:
            await ctx.send("Whitelist is disabled.")
            return
        lines = [_guild_line(ctx.bot, id_) for id_ in whitelist]
        await ctx.send("```\n" + "\n".join(lines) + "```")

    _show_guild_whitelist = _guild_whitelist.command(name="show")(
        commands.check(owner_check)(_show_guild_whitelist_callback)
    )

    async def _leave_guilds(self, ctx, guild_ids):
        """Leave each guild; one failing does not stop the rest."""
        for guild_id in guild_ids:
            guild = ctx.bot.get_guild(guild_id)
            if guild is None:
                continue
            await ctx.bot._leave(guild)

    @_guild_whitelist.command(name="add")
    @commands.check(owner_check)
    async def _guild_whitelist_add(self, ctx, *, id: str):
        id = int(id)
        invoking = getattr(ctx, "guild", None)
        change = await guild_whitelist.add(
            ctx.bot.DbSession,
            id,
            [g.id for g in ctx.bot.guilds],
            protect=invoking.id if invoking is not None else None,
        )
        if change.outcome is Outcome.ALREADY_LISTED:
            await ctx.send("That guild is already whitelisted.")
            return
        if change.outcome is Outcome.WOULD_LEAVE_HERE:
            await ctx.send(
                "That would make the bot leave this guild, so nothing was"
                " changed. Add this guild's id first."
            )
            return
        if change.outcome is Outcome.ENABLED:
            message = "Whitelist updated, the whitelist is now enabled."
            if change.to_leave:
                lines = [_guild_line(ctx.bot, g) for g in change.to_leave]
                message += " Leaving these guilds:\n" + "\n".join(lines)
        else:
            message = "Whitelist updated."
        await ctx.send(message)
        await self._leave_guilds(ctx, change.to_leave)

    async def _guild_whitelist_remove_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        all_guilds = [
            app_commands.Choice(name=f"{g.name} {g.id}", value=str(g.id))
            for g in self.bot.guilds
            if current.lower() in g.name.lower() or current in str(g.id)
        ]
        return all_guilds[:25]

    @_guild_whitelist.command(name="remove")
    @commands.check(owner_check)
    @app_commands.autocomplete(id=_guild_whitelist_remove_autocomplete)
    async def _guild_whitelist_remove(
        self,
        ctx,
        *,
        id: str,
    ):
        id = int(id.split()[-1])
        change = await guild_whitelist.remove(
            ctx.bot.DbSession, id, [g.id for g in ctx.bot.guilds]
        )
        if change.outcome is Outcome.NOT_LISTED:
            await ctx.send("That guild is not whitelisted.")
        elif change.outcome is Outcome.DISABLED:
            await ctx.send(
                "Whitelist updated. The whitelist is now empty and"
                " therefore disabled."
            )
        else:
            await ctx.send(
                "Whitelist updated, leaving the guild."
                if change.to_leave
                else "Whitelist updated."
            )
        await self._leave_guilds(ctx, change.to_leave)


async def setup(bot: MusicBot):
    await bot.add_cog(Developer(bot))
