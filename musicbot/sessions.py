"""The per-guild session registry.

A guild's session has two halves, its settings and its audio
controller, kept separately: a guild can be settings-only (loaded in
bulk before any controller exists, or left that way when building one
failed), and that is a legal state. `controller()` and `settings()`
are the single readiness policy - each returns None for a half that
is not registered.
"""

import sys
from typing import (
    Awaitable,
    Callable,
    Dict,
    Generic,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    TypeVar,
)

from config import config

Guild = TypeVar("Guild")
Controller = TypeVar("Controller")
Settings = TypeVar("Settings")


class GuildSessions(Generic[Guild, Controller, Settings]):
    def __init__(
        self,
        controller_factory: Callable[[Guild], Controller],
        settings_loader: Callable[
            [Iterable[Guild]], Awaitable[Mapping[Guild, Settings]]
        ],
    ):
        """`controller_factory(guild)` builds a controller;
        `settings_loader(guilds)` loads (creating where missing) the
        settings of every guild given, keyed by guild."""
        self._build_controller = controller_factory
        self._load_settings = settings_loader
        self._controllers: Dict[Guild, Controller] = {}
        self._settings: Dict[Guild, Settings] = {}

    def controller(self, guild: Guild) -> Optional[Controller]:
        return self._controllers.get(guild)

    def settings(self, guild: Guild) -> Optional[Settings]:
        return self._settings.get(guild)

    def __iter__(self) -> Iterator[Controller]:
        # a snapshot: a guild discarded while a caller awaits between
        # items must not break the iteration
        return iter(list(self._controllers.values()))

    async def load_settings(self, guilds: Iterable[Guild]) -> None:
        """Bulk-fill the settings half only."""
        self._settings.update(await self._load_settings(list(guilds)))

    async def _ensure_settings(self, guild: Guild) -> Settings:
        if guild not in self._settings:
            await self.load_settings([guild])
        return self._settings[guild]

    async def get_or_create(self, guild: Guild) -> Controller:
        """The guild's controller, building it (and loading its
        settings first) when it has none, then auto-joining voice."""
        controller = self._controllers.get(guild)
        if controller is not None:
            return controller

        # settings first: a factory that raises leaves the guild
        # settings-only rather than half-registered
        sett = await self._ensure_settings(guild)
        controller = self._controllers[guild] = self._build_controller(guild)

        if config.GLOBAL_DISABLE_AUTOJOIN_VC or sett.vc_timeout:
            return controller
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
        return controller

    async def discard(self, guild: Guild) -> None:
        """Dispose of the guild's controller, if any, and drop both
        halves - even when dispose() raises."""
        controller = self._controllers.get(guild)
        try:
            if controller is not None:
                await controller.dispose()
        finally:
            self._controllers.pop(guild, None)
            self._settings.pop(guild, None)

    async def reset(self, guild: Guild, reason: str = "reset") -> Controller:
        """Replace the guild's controller with a fresh one, keeping
        its settings. Leaves the guild settings-only if disposing of
        the old controller or building the new one fails."""
        await self._ensure_settings(guild)
        old = self._controllers.pop(guild, None)
        if old is not None:
            await old.dispose(reason)
        controller = self._controllers[guild] = self._build_controller(guild)
        return controller
