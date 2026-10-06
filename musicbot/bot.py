import re
import asyncio
from traceback import print_exception
from typing import List

import discord
from discord.ext import commands, tasks
from discord.ext.commands.view import StringView
from discord import app_commands
from discord.ext.commands import DefaultHelpCommand, NotOwner
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from config import config
from config.utils import ensure_sqlite_parent
from musicbot.audiocontroller import VC_CONNECT_TIMEOUT, AudioController
from musicbot import library, linkutils, loader
from musicbot.settings import (
    GuildSettings,
    run_migrations,
    extract_legacy_settings,
    get_guild_whitelist,
    import_env_whitelist,
    migrate_old_playlists,
)
from musicbot.sessions import GuildSessions
from musicbot.utils import CheckError


# Deliberately module-level, not a MusicBot method: discord.py decides
# how many leading parameters to skip (self/cog + ctx, or just ctx)
# based on whether the function is lexically defined inside a class,
# regardless of whether it's actually attached to a Cog at runtime.
# Since this command is added directly via add_command() below rather
# than through a Cog, defining it inside the class body would make
# discord.py's signature introspection expect a leading `self` that
# invocation never actually supplies, raising a TypeError on every
# call. Use ctx.bot/interaction.client instead of self to reach the
# bot instance.
@commands.hybrid_command(name="help", description=config.HELP_HELP_SHORT)
@app_commands.describe(command="The command to get help for")
async def _help(
    ctx,
    *,
    command: str = None,
):
    help_command = ctx.bot._default_help
    await help_command.prepare(ctx)
    await help_command.callback(ctx, command=command)


@_help.autocomplete("command")
async def _help_autocomplete(
    interaction: discord.Interaction, current: str
) -> List[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=c.qualified_name, value=c.qualified_name)
        for c in interaction.client.walk_commands()
        if current.lower() in c.qualified_name.lower() and not c.hidden
    ][:25]


class MusicBot(commands.Bot):
    def __init__(self, initial_extensions: List[str], *args, **kwargs):
        kwargs.setdefault("help_command", UniversalHelpCommand())
        super().__init__(*args, **kwargs)
        self.initial_extensions = initial_extensions

        # every guild's session: its settings and its audio controller
        self.sessions: GuildSessions[
            discord.Guild, AudioController, GuildSettings
        ] = GuildSessions(
            lambda guild: AudioController(self, guild),
            lambda guilds: GuildSettings.load_many(self, guilds),
        )

        ensure_sqlite_parent(config.DATABASE)
        self.db_engine = create_async_engine(config.DATABASE)
        self.DbSession = sessionmaker(
            self.db_engine, expire_on_commit=False, class_=AsyncSession
        )
        # replace default to register slash command
        self._default_help = self.remove_command("help")
        self.add_command(_help)

    async def setup_hook(self):
        if config.ENABLE_LOCAL_LIBRARY:
            await library.build_index_async()
        for extension in self.initial_extensions:
            await self.load_extension(extension)
        if config.ENABLE_SLASH_COMMANDS:
            await self.tree.sync()

    async def start(self, *args, **kwargs):
        # built here, inside the running loop: __init__ has no current
        # loop to bind it to (Python 3.14 raises there)
        self.absolutely_ready = asyncio.get_running_loop().create_future()
        print(config.STARTUP_MESSAGE)

        async with self.db_engine.connect() as connection:
            await connection.run_sync(run_migrations)
        await extract_legacy_settings(self)
        await migrate_old_playlists(self)
        await import_env_whitelist(self)

        loader.init()
        return await super().start(*args, **kwargs)

    async def close(self):
        print(config.SHUTDOWN_MESSAGE, flush=True)

        await asyncio.gather(
            *(
                audiocontroller.udisconnect("bot shutdown")
                for audiocontroller in self.sessions
            )
        )
        # this loop's aiohttp session (see linkutils.get_session);
        # the worker closes its own when shutdown() stops it
        await linkutils.stop()
        loader.shutdown()
        return await super().close()

    async def on_ready(self):
        await self.sessions.load_settings(self.guilds)

        # read once for the whole sweep rather than per guild
        whitelist = await get_guild_whitelist(self)
        for guild in self.guilds:
            if whitelist and guild.id not in whitelist:
                print(f"{guild.name} is not whitelisted, leaving.")
                await guild.leave()
                continue
            await self.register(guild)
            print("Joined {}".format(guild.name))

        print(config.STARTUP_COMPLETE_MESSAGE)

        if not self.update_views.is_running():
            self.update_views.start()

        if not self.absolutely_ready.done():
            self.absolutely_ready.set_result(True)

    async def on_guild_join(self, guild):
        print(guild.name)
        whitelist = await get_guild_whitelist(self)
        if whitelist and guild.id not in whitelist:
            print("Not whitelisted, leaving.")
            await guild.leave()
            return
        await self.register(guild)

    async def on_guild_remove(self, guild):
        # every way out of a guild lands here, guild.leave() included,
        # so this is the one place its session is dropped
        await self.sessions.discard(guild)

    async def on_command_error(self, ctx, error):
        await ctx.send(error)
        if not isinstance(error, (CheckError, NotOwner)):
            print_exception(error)

    async def on_hybrid_command_error(self, ctx, error):
        await self.on_command_error(ctx, error)

    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        # A raw gateway event, unlike a prefix command, is not gated
        # behind absolutely_ready - and on_ready registers guilds in a
        # loop that awaits a network call apiece, so an event can
        # arrive before this guild has a controller. There is no
        # session to adjust in that case, so drop it rather than
        # raising KeyError out of the event handler.
        audiocontroller = self.sessions.controller(guild)
        if audiocontroller is None:
            return
        if member == self.user:
            if not guild.voice_client:
                await asyncio.sleep(VC_CONNECT_TIMEOUT)
            if guild.voice_client:
                is_playing = guild.voice_client.is_playing()
                await audiocontroller.timer.start(is_playing)
                if is_playing:
                    # bot was moved, restore playback
                    await asyncio.sleep(1)
                    guild.voice_client.resume()
            else:
                # did not reconnect, clear state
                await audiocontroller.udisconnect("removed from voice channel")
        elif (
            guild.voice_client
            and guild.voice_client.channel == before.channel
            and all(m.bot for m in before.channel.members)
        ):
            # all users left
            await audiocontroller.timer.start(guild.voice_client.is_playing())

    @tasks.loop(seconds=1)
    async def update_views(self):
        await asyncio.gather(
            *(
                audiocontroller.update_view()
                for audiocontroller in self.sessions
            )
        )

    async def get_prefix(self, message: discord.Message):
        prefixes = await super().get_prefix(message)
        if not self.case_insensitive:
            return prefixes
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        # perform case-insensitive search
        for prefix in prefixes:
            if match := re.match(
                re.escape(prefix), message.content, re.IGNORECASE
            ):
                return match.group()
        # did not match
        return " "

    async def get_context(self, message, *, cls=None):
        return await super().get_context(message, cls=cls or Context)

    async def process_commands(self, message: discord.Message):
        if message.author.bot:
            return

        ctx = await self.get_context(message, cls=Context)

        if ctx.valid and not message.guild:
            await message.channel.send(config.NO_GUILD_MESSAGE)
            return

        await self.absolutely_ready

        await self.invoke(ctx)

    async def register(self, guild: discord.Guild):
        await self.sessions.get_or_create(guild)


class Context(commands.Context):
    bot: "MusicBot"
    guild: discord.Guild

    @classmethod
    async def from_interaction(cls, interaction: discord.Interaction):
        try:
            return await super().from_interaction(interaction)
        except ValueError:
            # Handle component interactions without command data
            ctx = cls(
                message=interaction.message,
                bot=interaction.client,
                view=StringView(""),
                prefix=None,
            )
            ctx.interaction = interaction
            ctx.command = None
            # Update attributes from interaction for accuracy
            ctx.author = interaction.user
            ctx.guild = interaction.guild
            ctx.channel = interaction.channel
            return ctx

    async def response_send_message(self, *args, **kwargs):
        if self.interaction:
            if self.interaction.response.is_done():
                return await self.interaction.followup.send(*args, **kwargs)
            return await self.interaction.response.send_message(
                *args, **kwargs
            )
        return await self.send(*args, **kwargs)

    async def send(self, *args, **kwargs):
        kwargs.pop("reference", None)  # not supported
        # .get(), because this runs for interactions too: a component
        # click or an application command is not gated behind
        # absolutely_ready the way process_commands gates prefix
        # commands, so it can land before on_ready has registered this
        # guild - and self.guild is None outside a guild entirely.
        # With no controller there is no playback message to carry the
        # view, so fall through to the plain send below.
        audiocontroller = self.bot.sessions.controller(self.guild)
        channel = audiocontroller.command_channel if audiocontroller else None
        if (
            audiocontroller is None
            or "view" in kwargs
            or kwargs.get("ephemeral", False)
            or (
                channel
                # unwrap channel from context
                and getattr(channel, "channel", channel) != self.channel
            )
        ):
            # sending ephemeral message or using different channel
            # don't bother with views
            if self.interaction:
                if self.interaction.response.is_done():
                    return await self.interaction.followup.send(
                        *args, **kwargs
                    )
                return await self.interaction.response.send_message(
                    *args, **kwargs
                )
            return await super().send(*args, **kwargs)

        async def send(view):
            if view:
                kwargs["view"] = view
            if self.interaction:
                if self.interaction.response.is_done():
                    return await self.interaction.followup.send(
                        *args, **kwargs
                    )
                await self.interaction.response.send_message(*args, **kwargs)
                return await self.interaction.original_response()
            return await super(Context, self).send(*args, **kwargs)

        return await audiocontroller.attach_view(send)


class UniversalHelpCommand(DefaultHelpCommand):
    def get_destination(self):
        return self.context
