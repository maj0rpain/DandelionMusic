import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest_plugins = ["pytester"]

import dotenv  # noqa: E402
import dotenv.main  # noqa: E402


def _stub_find_dotenv(path):
    """A find_dotenv stand-in that always resolves to `path`."""
    return lambda *a, **kw: path


def _config_module():
    """The config.config submodule.

    sys.modules, not `import config.config as ...`: the package's
    __init__ binds the name `config` to the Config *instance*, which
    shadows the submodule of the same name.
    """
    return sys.modules["config.config"]


def _import_config_without_dotenv():
    """Imports `config` with find_dotenv stubbed to os.devnull.

    The package's __init__ builds the global Config() at import time,
    and Config.load() resolves its .env with find_dotenv(), which walks
    up from the config module's file and so finds the developer's real
    .env. Without this stub that .env would reach os.environ, the global
    config's values and its unknown_vars for the whole session. The real
    function is put back on both names in a finally, so nothing but this
    one build sees the stub.
    """
    real = dotenv.main.find_dotenv
    dotenv.find_dotenv = _stub_find_dotenv(os.devnull)
    try:
        import config.config  # noqa: F401
    finally:
        dotenv.find_dotenv = real
        if "config.config" in sys.modules:
            _config_module().find_dotenv = real


_import_config_without_dotenv()

from config.config import ENV_ALIASES  # noqa: E402
from config.config import Config as ConfigClass  # noqa: E402


@pytest.fixture
def config_factory(tmp_path, monkeypatch):
    """Builds Config instances against a throwaway .env in a temp cwd.

    Config is read-only now, but isolation still matters: without the
    chdir a test reads the developer's real .env, and python-dotenv
    walks parent directories looking for one, so an empty temp dir is
    not enough on its own.

    os.environ is cleared as well, because get_env_var() reads it
    directly and load_dotenv() will not override a variable that is
    already set, so a BOT_TOKEN exported in the shell running pytest
    would otherwise win over the fixture's.
    """
    monkeypatch.chdir(tmp_path)
    # Only the variables Config actually reads. An earlier version
    # cleared every upper-case name, which on Windows (where os.environ
    # upper-cases keys) meant PATH, SYSTEMROOT and TEMP as well - fine
    # while nothing under test shells out or uses tempfile, and a
    # baffling failure the moment something does.
    for key in (
        *ConfigClass.as_dict(),
        *ENV_ALIASES,
        "VC_TIMOUT_DEFAULT",
    ):
        monkeypatch.delenv(key, raising=False)

    # chdir alone does not redirect the read side. Config.load()
    # resolves the .env with find_dotenv(), which walks up from the
    # *calling module's file* rather than from cwd, so it finds the
    # project's own .env however the process was started. Both it and
    # load_dotenv have to be redirected - patching one and not the
    # other is how an earlier version of this fixture read, and back
    # when Config could still write, overwrote the real .env.
    config_module = _config_module()
    from dotenv import dotenv_values

    env_path = tmp_path / ".env"
    monkeypatch.setattr(
        config_module, "find_dotenv", _stub_find_dotenv(str(env_path))
    )

    def load_dotenv(*a, **kw):
        # Through monkeypatch.setenv, not the real load_dotenv: that
        # writes os.environ behind monkeypatch's back, so only the keys
        # registered beforehand were restored and every other key the
        # .env held, known to Config or not, leaked into later tests.
        for key, value in dotenv_values(env_path).items():
            if value is not None:
                monkeypatch.setenv(key, value)

    monkeypatch.setattr(config_module, "load_dotenv", load_dotenv)

    def build(env: str = "") -> ConfigClass:
        env_path.write_text(env, encoding="utf-8")
        cfg = ConfigClass()
        # Belt and braces: whatever Config resolved must be inside
        # tmp_path. Config no longer writes anything, but it is still
        # read from here, and a fixture that silently stopped isolating
        # it would quietly test the developer's real configuration.
        assert str(tmp_path) in str(cfg._env_path), (
            f"Config resolved {cfg._env_path!r} outside the temp dir - "
            "refusing to run a test against the real .env"
        )
        return cfg

    return build


@pytest.fixture
def controller_and_stub(tmp_path):
    """A real AudioController over a stub loader, run from tmp_path.

    See tests/controller_seam.py: the stub replaces musicbot.loader
    (the one import with side effects) for this test only, and the
    controller's backup/ directory lands in tmp_path, not the repo.
    """
    from controller_seam import controller_seam

    with controller_seam(tmp_path) as built:
        yield built


@pytest.fixture
def expired_panel_seam(tmp_path):
    """(controller, stub loader) over a real PlayerPanel whose
    destination channel's send always fails with an expired token."""
    from controller_seam import controller_seam
    from discord_fakes import EXPIRED

    class ExpiredChannel:
        async def send(self, *args, **kwargs):
            raise EXPIRED

    def make_panel(guild, get_controller):
        import types

        from musicbot.player_panel import PlayerPanel

        panel = PlayerPanel(
            guild,
            get_controller,
            lambda: types.SimpleNamespace(announce_songs=True),
        )
        panel.direct_to(ExpiredChannel())
        return panel

    with controller_seam(tmp_path, make_panel) as built:
        yield built


@pytest.fixture
def controller(controller_and_stub):
    return controller_and_stub[0]


@pytest.fixture
def stub_loader(controller_and_stub):
    return controller_and_stub[1]


@pytest.fixture
def worker_loop(monkeypatch):
    from musicbot import loader

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(loader, "_loop", loop)
    yield loop
    loop.close()


class StubbedSleep:
    """What `no_sleep` returns: `count` stubbed sleeps so far, and an
    optional `hook` (unset by default) called on each one."""

    def __init__(self):
        self.count = 0
        self.hook = None


@pytest.fixture
def no_sleep(monkeypatch):
    """A move waits a second before returning; time is a boundary.
    Returns a `StubbedSleep`: `count` stubbed sleeps, and an optional
    `hook` called on each."""
    stub = StubbedSleep()

    async def sleep(_seconds):
        stub.count += 1
        if stub.hook:
            stub.hook()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return stub
