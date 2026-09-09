"""Config loading.

Config is read-only now: the .env write-back that used to live here
(and produced five separate bugs) went away with the guild
whitelist, which the database owns instead.
"""

import pytest

from config.utils import alchemize_url, ensure_sqlite_parent, get_env_var

SECRETS_ENV = (
    "BOT_TOKEN=real-token-abc123\n"
    "SPOTIFY_SECRET=real-spotify-secret\n"
    "LASTFM_API_KEY=real-lastfm-key\n"
    "GUILD_WHITELIST=[111]\n"
)


class TestGetEnvVar:
    def test_returns_default_when_unset(self):
        assert get_env_var("DEFINITELY_UNSET_XYZ", 42) == 42

    def test_literal_evals_against_a_non_str_default(self, monkeypatch):
        monkeypatch.setenv("SOME_INT", "7")
        assert get_env_var("SOME_INT", 0) == 7

    def test_leaves_a_str_default_alone(self, monkeypatch):
        # no literal_eval, so "7" stays the string "7"
        monkeypatch.setenv("SOME_STR", "7")
        assert get_env_var("SOME_STR", "x") == "7"

    def test_rejects_a_value_of_the_wrong_type(self, monkeypatch):
        monkeypatch.setenv("SOME_TUPLE", "['a']")
        with pytest.raises(TypeError):
            get_env_var("SOME_TUPLE", ("a",))

    def test_empty_string_fails_a_non_str_default(self, monkeypatch):
        """What made the old docker-compose file unbootable: Compose
        interpolates an unset ${VAR} to "" and then sets it, which is
        not the same as leaving it unset."""
        monkeypatch.setenv("ENABLE_LOCAL_LIBRARY", "")
        with pytest.raises(TypeError):
            get_env_var("ENABLE_LOCAL_LIBRARY", False)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("sqlite:///settings.db", "sqlite+aiosqlite:///settings.db"),
        ("postgres://h/d", "postgresql+asyncpg://h/d"),
        ("mysql://h/d", "mysql+aiomysql://h/d"),
        ("weird+driver://h/d", "weird+driver://h/d"),
    ],
)
def test_alchemize_url(url, expected):
    assert alchemize_url(url) == expected


def test_extra_owners_defaults_to_empty(config_factory):
    """An unconfigured deployment must trust only the application
    owner(s) Discord reports - this replaced a hardcoded user id."""
    assert config_factory(SECRETS_ENV).EXTRA_OWNERS == []


def test_message_strings_do_not_pick_up_stray_substitutions(
    config_factory,
):
    """config.Formatter is a string.Template with an empty delimiter,
    so any bare identifier in en.json that matches a config key is
    substituted. Naming a setting inside its own message renders the
    value instead of the name."""
    config = config_factory(SECRETS_ENV)
    assert "True" not in config.VC_TIMEOUT_EDIT_DISABLED


class TestEnsureSqliteParent:
    """sqlite will not create a missing parent directory, and the
    default database now lives in data/."""

    def test_creates_a_missing_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ensure_sqlite_parent("sqlite+aiosqlite:///data/settings.db")
        assert (tmp_path / "data").is_dir()

    def test_existing_directory_is_fine(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        ensure_sqlite_parent("sqlite:///data/settings.db")
        assert (tmp_path / "data").is_dir()

    def test_bare_filename_needs_no_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ensure_sqlite_parent("sqlite:///settings.db")
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+asyncpg://host/db",
            "mysql+aiomysql://host/db",
            "sqlite:///:memory:",
            "sqlite://",
            "sqlite+aiosqlite:///?check_same_thread=False",
        ],
    )
    def test_leaves_non_file_databases_alone(self, url, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ensure_sqlite_parent(url)
        assert list(tmp_path.iterdir()) == []

    def test_strips_a_query_string(self, tmp_path, monkeypatch):
        """The path is parsed with str.partition, so a query string
        would otherwise become part of the directory name."""
        monkeypatch.chdir(tmp_path)
        ensure_sqlite_parent(
            "sqlite+aiosqlite:///data/x.db?check_same_thread=False"
        )
        assert (tmp_path / "data").is_dir()

    def test_absolute_path(self, tmp_path):
        target = tmp_path / "abs" / "nested"
        ensure_sqlite_parent(f"sqlite+aiosqlite:///{target}/x.db")
        assert target.is_dir()

    def test_wiring_is_in_place(self):
        """Nothing else asserts that bot.py actually calls this, so a
        refactor could drop it and every other test would still pass."""
        import inspect

        from musicbot.bot import MusicBot

        src = inspect.getsource(MusicBot.__init__)
        assert "ensure_sqlite_parent(config.DATABASE)" in src
