import re
import sys
import asyncio
from traceback import print_exception
from typing import Dict, Union, List

import discord
from discord.ext import commands, tasks
from discord.ext.commands.view import StringView
from discord import app_commands
from discord.ext.commands import DefaultHelpCommand, NotOwner
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from config import config
from musicbot.audiocontroller import VC_CONNECT_TIMEOUT, AudioController
from musicbot.settings import (
    GuildSettings,
    run_migrations,
    extract_legacy_settings,
    migrate_old_playlists,
)
from musicbot.utils import CheckError


class MusicBot(commands.Bot):
    def __init__(self, initial_extensions: List[str], *args, **kwargs):
        kwargs.setdefault("help_command", UniversalHelpCommand())
        super().__init__(*args, **kwargs)
        self.initial_extensions = initial_extensions

        # A dictionary that remembers
        # which guild belongs to which audiocontroller
        self.audio_controllers: Dict[discord.Guild, AudioController] = {}

        # A dictionary that remembers which settings belongs to which guild
        self.settings: Dict[discord.Guild, GuildSettings] = {}

        self.db_engine = create_async_engine(config.DATABASE)
        self.DbSession = sessionmaker(
            self.db_engine, expire_on_commit=False, class_=AsyncSession
        )
        # replace default to register slash command
        self._default_help = self.remove_command("help")
        self.add_command(self._help)

        self.absolutely_ready = asyncio.Future()

    async def setup_hook(self):
        for extension in self.initial_extensions:
            await self.load_extension(extension)
        if config.ENABLE_SLASH_COMMANDS:
            await self.tree.sync()

    async def start(self, *args, **kwargs):
        print(config.STARTUP_MESSAGE)

        async with self.db_engine.connect() as connection:
            await connection.run_sync(run_migrations)
        await extract_legacy_settings(self)
        await migrate_old_playlists(self)

        return await super().start(*args, **kwargs)

    async def close(self):
        if "--run" not in sys.argv:
            print(config.SHUTDOWN_MESSAGE, flush=True)

        await asyncio.gather(
            *(
                audiocontroller.udisconnect()
                for audiocontroller in self.audio_controllers.values()
            )
        )
        return await super().close()

    async def on_ready(self):
        self.settings.update(await GuildSettings.load_many(self, self.guilds))

        for guild in self.guilds:
            if (
                config.GUILD_WHITELIST
                and guild.id not in config.GUILD_WHITELIST
            ):
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
        if config.GUILD_WHITELIST and guild.id not in config.GUILD_WHITELIST:
            print("Not whitelisted, leaving.")
            await guild.leave()
            return
        await self.register(guild)

    async def on_command_error(self, ctx, error):
        await ctx.send(error)
        if not isinstance(error, (CheckError, NotOwner)):
            print_exception(error)

    async def on_hybrid_command_error(self, ctx, error):
        await self.on_command_error(ctx, error)

    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        if member == self.user:
            audiocontroller = self.audio_controllers[guild]
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
                await audiocontroller.udisconnect()
        elif (
            guild.voice_client
            and guild.voice_client.channel == before.channel
            and all(m.bot for m in before.channel.members)
        ):
            # all users left
            audiocontroller = self.audio_controllers[guild]
            await audiocontroller.timer.start(guild.voice_client.is_playing())

    @tasks.loop(seconds=1)
    async def update_views(self):
        await asyncio.gather(
            *(
                audiocontroller.update_view()
                for audiocontroller in self.audio_controllers.values()
            )
        )


    async def get_prefix(
        self, message: discord.Message
    ):
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
        if guild in self.audio_controllers:
            return

        if guild not in self.settings:
            self.settings[guild] = await GuildSettings.load(self, guild)

        sett = self.settings[guild]
        controller = self.audio_controllers[guild] = AudioController(
            self, guild
        )

        if config.GLOBAL_DISABLE_AUTOJOIN_VC:
            return

        if not sett.vc_timeout:
            try:
                await controller.register_voice_channel(
                    guild.get_channel(int(sett.start_voice_channel or 0))
                    or guild.voice_channels[0]
                )
            except Exception as e:
                print(
                    f"Couldn't autojoin VC at {guild.name}:",
                    e,
                    file=sys.stderr,
                )

    @commands.hybrid_command(name="help", description=config.HELP_HELP_SHORT)
    @app_commands.describe(command="The command to get help for")
    async def _help(
        self,
        ctx,
        *,
        command: str = None,
    ):
        help_command = self._default_help
        await help_command.prepare(ctx)
        await help_command.callback(ctx, command=command)

    @_help.autocomplete('command')
    async def _help_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=c.qualified_name, value=c.qualified_name)
            for c in self.walk_commands()
            if current.lower() in c.qualified_name.lower() and not c.hidden
        ][:25]


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
                prefix=None
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
            return await self.interaction.response.send_message(*args, **kwargs)
        return await self.send(*args, **kwargs)

    async def send(self, *args, **kwargs):
        kwargs.pop("reference", None)  # not supported
        audiocontroller = self.bot.audio_controllers[self.guild]
        channel = audiocontroller.command_channel
        if (
            "view" in kwargs
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
                    return await self.interaction.followup.send(*args, **kwargs)
                return await self.interaction.response.send_message(*args, **kwargs)
            return await super().send(*args, **kwargs)
        async with audiocontroller.message_lock:
            await audiocontroller.update_view(None)
            view = audiocontroller.make_view()
            if view:
                kwargs["view"] = view
            
            if self.interaction:
                if self.interaction.response.is_done():
                    res = await self.interaction.followup.send(*args, **kwargs)
                else:
                    await self.interaction.response.send_message(*args, **kwargs)
                    res = await self.interaction.original_response()
            else:
                res = await super().send(*args, **kwargs)
            
            if isinstance(res, discord.Interaction):
                audiocontroller.last_message = await res.original_response()
            else:
                audiocontroller.last_message = res
        return res


class UniversalHelpCommand(DefaultHelpCommand):
    def get_destination(self):
        return self.context
