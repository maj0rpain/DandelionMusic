import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
        "SPOTIPY_CLIENT_ID",
        "SPOTIPY_CLIENT_SECRET",
        "HEROKU_DB",
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
    #
    # sys.modules, not `import config.config as ...`: the package's
    # __init__ binds the name `config` to the Config *instance*, which
    # shadows the submodule of the same name.
    config_module = sys.modules["config.config"]
    from dotenv import load_dotenv as real_load_dotenv

    env_path = tmp_path / ".env"
    monkeypatch.setattr(
        config_module, "find_dotenv", lambda *a, **kw: str(env_path)
    )
    monkeypatch.setattr(
        config_module,
        "load_dotenv",
        lambda *a, **kw: real_load_dotenv(env_path, override=True),
    )

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
