"""The per-guild session registry.

A guild's session has its settings, its audio controller and its
player panel, kept separately: a guild can be settings-only (loaded in
bulk before any controller exists, or left that way when building one
failed), and that is a legal state. `controller()`, `settings()` and
`panel()` are the single readiness policy - each returns None for a
part that is not registered.
"""

import asyncio
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
Panel = TypeVar("Panel")
Settings = TypeVar("Settings")


class GuildSessions(Generic[Guild, Panel, Controller, Settings]):
    def __init__(
        self,
        panel_factory: Callable[[Guild], Panel],
        controller_factory: Callable[[Guild, Panel], Controller],
        settings_loader: Callable[
            [Iterable[Guild]], Awaitable[Mapping[Guild, Settings]]
        ],
    ):
        """`panel_factory(guild)` builds a guild's player panel, which
        is built before its controller; `controller_factory(guild,
        panel)` builds the controller around that panel;
        `settings_loader(guilds)` loads (creating where missing) the
        settings of every guild given, keyed by guild."""
        self._build_panel = panel_factory
        self._build_controller = controller_factory
        self._panels: Dict[Guild, Panel] = {}
        self._load_settings = settings_loader
        self._controllers: Dict[Guild, Controller] = {}
        self._settings: Dict[Guild, Settings] = {}
        # guilds whose settings are being loaded, to the shared load
        self._loading: Dict[Guild, asyncio.Future] = {}

    def controller(self, guild: Guild) -> Optional[Controller]:
        return self._controllers.get(guild)

    def panel(self, guild: Guild) -> Optional[Panel]:
        return self._panels.get(guild)

    def direct_panel(
        self, guild: Guild, channel, *, replace: bool = True
    ) -> None:
        panel = self._panels.get(guild)
        if panel is not None:
            panel.direct_to(channel, replace=replace)

    def panels(self) -> Iterator[Panel]:
        # a snapshot, like __iter__
        return iter(list(self._panels.values()))

    def settings(self, guild: Guild) -> Optional[Settings]:
        return self._settings.get(guild)

    def _build(self, guild: Guild) -> Controller:
        """Builds the guild's panel, then its controller around it,
        registering both only once the controller exists."""
        panel = self._build_panel(guild)
        controller = self._build_controller(guild, panel)
        self._panels[guild] = panel
        self._controllers[guild] = controller
        return controller

    def __iter__(self) -> Iterator[Controller]:
        # a snapshot: a guild discarded while a caller awaits between
        # items must not break the iteration
        return iter(list(self._controllers.values()))

    async def load_settings(self, guilds: Iterable[Guild]) -> None:
        """Bulk-fill the settings half only, for guilds with no settings
        yet: registered settings are never replaced, and a guild
        already being loaded shares that load."""
        waiting = []
        fresh = []
        for guild in dict.fromkeys(guilds):
            if guild in self._settings:
                continue
            load = self._loading.get(guild)
            if load is not None:
                waiting.append(load)
            else:
                fresh.append(guild)
        if fresh:
            load = asyncio.ensure_future(self._load_fresh(fresh))
            for guild in fresh:
                self._loading[guild] = load
            waiting.append(load)
        # shielded: a cancelled waiter must not cancel the shared load
        results = await asyncio.gather(
            *(asyncio.shield(w) for w in waiting), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _load_fresh(self, guilds: list) -> None:
        me = asyncio.current_task()
        try:
            loaded = await self._load_settings(guilds)
            for guild, sett in loaded.items():
                # a guild discarded mid-load lost its entry: not registered
                if self._loading.get(guild) is me:
                    self._settings.setdefault(guild, sett)
        finally:
            for guild in guilds:
                if self._loading.get(guild) is me:
                    del self._loading[guild]

    async def _ensure_settings(self, guild: Guild) -> Settings:
        # a guild discarded mid-load is loaded afresh
        while guild not in self._settings:
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
        controller = self._build(guild)

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
        """Dispose of the guild's controller, if any, and drop every
        part - even when dispose() raises."""
        controller = self._controllers.get(guild)
        try:
            if controller is not None:
                await controller.dispose()
        finally:
            self._controllers.pop(guild, None)
            self._panels.pop(guild, None)
            self._settings.pop(guild, None)
            self._loading.pop(guild, None)

    async def reset(self, guild: Guild, reason: str = "reset") -> Controller:
        """Replace the guild's controller with a fresh one, keeping
        its settings. Leaves the guild settings-only if disposing of
        the old controller or building the new one fails."""
        await self._ensure_settings(guild)
        old = self._controllers.pop(guild, None)
        self._panels.pop(guild, None)
        if old is not None:
            await old.dispose(reason)
        return self._build(guild)
