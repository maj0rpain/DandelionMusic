import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.config import Config as ConfigClass  # noqa: E402


@pytest.fixture
def config_factory(tmp_path, monkeypatch):
    """Builds Config instances against a throwaway .env/.env.sample in
    a temp cwd.

    Isolation matters more than usual here. Config reads ".env" and
    ".env.sample" by relative path and Config.save() *writes* them, so
    without the chdir a test would rewrite the developer's real .env -
    and python-dotenv walks parent directories looking for one, so an
    empty temp dir is not enough on its own; each call writes the files
    it wants.

    os.environ is cleared as well, because get_env_var() reads it
    directly and load_dotenv() will not override a variable that is
    already set, so a BOT_TOKEN exported in the shell running pytest
    would otherwise win over the fixture's.
    """
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.isupper():
            monkeypatch.delenv(key, raising=False)

    # chdir alone does not redirect the read side. Config.load() calls
    # load_dotenv() with no arguments, and python-dotenv's find_dotenv()
    # walks up from the *calling module's file* rather than from cwd, so
    # it resolves the project's own .env no matter where the process is
    # running - while _update_env_files() and the unknown-variable scan
    # open the literal relative path ".env", which is cwd-relative.
    # Without this the fixture would read the developer's real .env and
    # write a temp one.
    # sys.modules, not `import config.config as ...`: the package's
    # __init__ binds the name `config` to the Config *instance*, which
    # shadows the submodule of the same name.
    config_module = sys.modules["config.config"]
    from dotenv import load_dotenv as real_load_dotenv

    monkeypatch.setattr(
        config_module,
        "load_dotenv",
        lambda *a, **kw: real_load_dotenv(
            tmp_path / ".env", override=True, **kw
        ),
    )

    def build(env: str = "", sample: str = "") -> ConfigClass:
        (tmp_path / ".env").write_text(env, encoding="utf-8")
        (tmp_path / ".env.sample").write_text(sample, encoding="utf-8")
        # _changed_vars is a *class* attribute, so it is shared by
        # every instance and would otherwise carry entries between
        # tests. Production only ever builds one Config, which is why
        # this is not a bug there.
        ConfigClass._changed_vars = {}
        return ConfigClass()

    return build
