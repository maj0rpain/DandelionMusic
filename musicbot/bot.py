import re
import sys
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
from musicbot.audiocontroller import AudioController
from musicbot import library, linkutils, loader
from musicbot.settings import (
    GuildSettings,
    run_migrations,
    extract_legacy_settings,
    migrate_old_playlists,
)
from musicbot import guild_whitelist
from musicbot.player_panel import PlayerPanel
from musicbot.sessions import GuildSessions
from musicbot.utils import VC_CONNECT_TIMEOUT, CheckError


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
        # slash commands are offered in guilds only, never DMs
        kwargs.setdefault(
            "allowed_contexts",
            app_commands.AppCommandContext(
                guild=True, dm_channel=False, private_channel=False
            ),
        )
        super().__init__(*args, **kwargs)
        self.initial_extensions = initial_extensions

        # every guild's session: its settings, controller and panel
        self.sessions: GuildSessions[
            discord.Guild, PlayerPanel, AudioController, GuildSettings
        ] = GuildSessions(
            lambda guild: PlayerPanel(
                guild,
                lambda: self.sessions.controller(guild),
                lambda: self.sessions.settings(guild),
            ),
            lambda guild, panel: AudioController(self, guild, panel),
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
        await guild_whitelist.import_env_whitelist(self.DbSession)

        loader.init()
        return await super().start(*args, **kwargs)

    async def _gather_per_guild(self, targets, make_coro, action: str):
        """Run make_coro(target) for every target (the controllers or
        the panels); one guild's failure is logged and never stops the
        others."""
        targets = list(targets)
        results = await asyncio.gather(
            *(make_coro(target) for target in targets),
            return_exceptions=True,
        )
        for target, result in zip(targets, results):
            if isinstance(result, BaseException):
                self._log_failure(
                    f"{action} failed for guild {target.guild.name}:",
                    result,
                )

    async def close(self):
        print(config.SHUTDOWN_MESSAGE, flush=True)

        await self._gather_per_guild(
            self.sessions,
            lambda controller: controller.udisconnect("bot shutdown"),
            "Disconnect",
        )

        # every remaining step runs; a later step's exception propagates
        # once the steps after it have run
        async def stop_loader():
            # joining the lanes' workers blocks; do it off the loop so other
            # tasks keep running until they have exited
            await asyncio.to_thread(loader.shutdown)

        failure = None
        for step in (
            # this loop's aiohttp session (see linkutils.get_session);
            # each lane's worker closes its own when shutdown() stops it
            linkutils.stop,
            stop_loader,
            super().close,
        ):
            try:
                await step()
            except Exception as e:
                if failure is not None:
                    # as a try/finally chain would: the later one
                    # carries the earlier one
                    e.__context__ = failure
                failure = e
        if failure is not None:
            raise failure

    async def on_ready(self):
        try:
            await self._register_guilds()
        finally:
            print(config.STARTUP_COMPLETE_MESSAGE)

            if not self.refresh_panels.is_running():
                self.refresh_panels.start()

            if not self.absolutely_ready.done():
                self.absolutely_ready.set_result(True)

    async def _register_guilds(self):
        """The startup sweep: every step is isolated, so one failure is
        logged and never keeps the bot from becoming ready."""
        try:
            await self.sessions.load_settings(self.guilds)
        except Exception as e:
            self._log_failure("Loading settings failed:", e)

        # read once for the whole sweep rather than per guild; a failed
        # load fails closed: no guild is left, none is registered
        try:
            whitelist = await guild_whitelist.load(self.DbSession)
        except Exception as e:
            self._log_failure("Loading the whitelist failed:", e)
            return
        for guild in self.guilds:
            if not whitelist.admits(guild.id):
                print(f"{guild.name} is not whitelisted, leaving.")
                await self._leave(guild)
                continue
            try:
                await self.register(guild)
            except Exception as e:
                self._log_failure(
                    f"Registration failed for guild {guild.name}:", e
                )
                continue
            print("Joined {}".format(guild.name))

    @staticmethod
    def _log_failure(message, error):
        print(message, file=sys.stderr, flush=True)
        print_exception(error)

    async def _leave(self, guild):
        """Leave an unadmitted guild; a failure is logged with the
        guild's name and never propagates."""
        try:
            await guild.leave()
        except Exception as e:
            MusicBot._log_failure(f"Leaving guild {guild.name} failed:", e)

    async def leave_guilds(self, guild_ids):
        """Leave each of these guilds the bot is in; ids it is not in
        are skipped, and one failing does not stop the rest."""
        for guild_id in guild_ids:
            guild = self.get_guild(guild_id)
            if guild is None:
                continue
            await self._leave(guild)

    async def on_guild_join(self, guild):
        print(guild.name)
        whitelist = await guild_whitelist.load(self.DbSession)
        if not whitelist.admits(guild.id):
            print("Not whitelisted, leaving.")
            await self._leave(guild)
            return
        await self.register(guild)

    async def on_guild_remove(self, guild):
        # every way out of a guild lands here, guild.leave() included,
        # so this is the one place its session is dropped
        await self.sessions.discard(guild)

    async def on_command_error(self, ctx, error):
        # A SongError is a user-facing message; it may arrive wrapped in
        # a command-invoke error and the hybrid/app-command wrapper.
        inner = error
        while isinstance(
            inner,
            (
                commands.CommandInvokeError,
                commands.HybridCommandError,
                app_commands.CommandInvokeError,
            ),
        ):
            inner = inner.original
        if isinstance(inner, loader.SongError):
            await ctx.send(inner)
            return
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
                await audiocontroller.start_idle_timer(is_playing)
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
            await audiocontroller.start_idle_timer(
                guild.voice_client.is_playing()
            )

    @tasks.loop(seconds=1)
    async def refresh_panels(self):
        await self._gather_per_guild(
            self.sessions.panels(),
            lambda panel: panel.refresh(),
            "Panel refresh",
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

    async def defer(self, *, ephemeral: bool = False) -> None:
        """Context.defer, but a second call does nothing: a command
        body may defer an interaction that Music.cog_before_invoke
        already deferred before joining voice."""
        if self.interaction and not self.interaction.response.is_done():
            await self.interaction.response.defer(ephemeral=ephemeral)

    async def _send_to_interaction(
        self, *args, original_response=False, **kwargs
    ):
        """The interaction routing, written once: a followup when the
        response is done, else the first response (returning the
        original response message when original_response is true, else
        send_message's own result).
        Call only when self.interaction is set."""
        if self.interaction.response.is_done():
            return await self.interaction.followup.send(*args, **kwargs)
        sent = await self.interaction.response.send_message(*args, **kwargs)
        if original_response:
            return await self.interaction.original_response()
        return sent

    async def _send_routed(
        self, fallback, *args, original_response=False, **kwargs
    ):
        """The interaction-or-parent routing, written once: through
        _send_to_interaction when self.interaction is set, else through
        fallback."""
        if self.interaction:
            return await self._send_to_interaction(
                *args, original_response=original_response, **kwargs
            )
        return await fallback(*args, **kwargs)

    async def response_send_message(self, *args, **kwargs):
        return await self._send_routed(self.send, *args, **kwargs)

    async def send(self, *args, **kwargs):
        kwargs.pop("reference", None)  # not supported
        # .get(), because this runs for interactions too: a component
        # click or an application command is not gated behind
        # absolutely_ready the way process_commands gates prefix
        # commands, so it can land before on_ready has registered this
        # guild - and self.guild is None outside a guild entirely.
        # With no panel there is no playback message to carry the
        # view, so fall through to the plain send below.
        panel = self.bot.sessions.panel(self.guild)
        # bound here: a zero-argument super() inside the closure below
        # would bind to its `view` parameter and raise TypeError
        super_send = super().send
        if panel is None or not panel.carries_buttons(
            channel=self.channel,
            ephemeral=kwargs.get("ephemeral", False),
            has_view="view" in kwargs,
        ):
            # an ephemeral reply, one with its own view, or one in
            # another channel does not carry the buttons
            return await self._send_routed(super_send, *args, **kwargs)

        async def send_with_view(view):
            if view:
                kwargs["view"] = view
            return await self._send_routed(
                super_send, *args, original_response=True, **kwargs
            )

        return await panel.attach(send_with_view)


class UniversalHelpCommand(DefaultHelpCommand):
    def get_destination(self):
        return self.context
