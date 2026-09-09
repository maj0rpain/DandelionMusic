"""Config loading and the .env / .env.sample writer.

save() is the riskiest code in the project that isn't Discord-facing:
it rewrites two files on disk, one of which is committed, and its
output is read back by the next startup. Every test here is a
regression test for something it actually got wrong.
"""

import pytest

from config.utils import alchemize_url, ensure_sqlite_parent, get_env_var

SECRETS_ENV = (
    "BOT_TOKEN=real-token-abc123\n"
    "SPOTIFY_SECRET=real-spotify-secret\n"
    "LASTFM_API_KEY=real-lastfm-key\n"
    "GUILD_WHITELIST=[111]\n"
)
SECRETS_SAMPLE = (
    "# token\nBOT_TOKEN=\n\n"
    "# spotify\nSPOTIFY_SECRET=\n\n"
    "# lastfm\nLASTFM_API_KEY=\n\n"
    "# whitelist\nGUILD_WHITELIST=[]\n"
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


class TestSaveDoesNotLeakSecrets:
    def test_env_sample_never_receives_a_live_value(self, config_factory):
        """.env.sample is committed. save() used to write every changed
        setting into it using the loaded value, so one d!guild_whitelist
        add published BOT_TOKEN, SPOTIFY_SECRET and LASTFM_API_KEY into
        a tracked file."""
        config = config_factory(SECRETS_ENV, SECRETS_SAMPLE)
        config.save()

        sample = open(".env.sample", encoding="utf-8").read()
        for secret in (
            "real-token-abc123",
            "real-spotify-secret",
            "real-lastfm-key",
        ):
            assert secret not in sample

    def test_env_keeps_the_live_values(self, config_factory):
        config = config_factory(SECRETS_ENV, SECRETS_SAMPLE)
        config.save()

        env = open(".env", encoding="utf-8").read()
        assert "real-token-abc123" in env

    def test_existing_sample_entries_are_left_alone(self, config_factory):
        """The sample pass is append-only. Rewriting an entry that is
        already there is churn on a tracked file at best, and the
        placeholder it overwrites is deliberate."""
        config = config_factory(SECRETS_ENV, SECRETS_SAMPLE)
        config.save()

        assert open(".env.sample", encoding="utf-8").read() == SECRETS_SAMPLE

    def test_a_missing_setting_is_added_at_its_default(self, config_factory):
        config = config_factory("BOT_TOKEN=t\nGUILD_WHITELIST=[1]\n", "")
        config.save()

        sample = open(".env.sample", encoding="utf-8").read()
        # the schema default, not the [1] this deployment loaded
        assert "GUILD_WHITELIST=[]" in sample


class TestSaveRoundTrips:
    """What save() writes to .env, the next startup has to be able to
    read back."""

    def test_embed_color_survives_a_save(self, config_factory):
        """EMBED_COLOR is authored as a hex string and rewritten in
        __init__ as the int it parses to. Persisting that int meant the
        next boot re-parsed it as hex: 0x4DD4D0 -> 5100752 -> 84936530,
        a different colour on every save."""
        before = config_factory(SECRETS_ENV, SECRETS_SAMPLE)
        original = before.EMBED_COLOR
        before.save()

        after = config_factory(
            open(".env", encoding="utf-8").read(), SECRETS_SAMPLE
        )
        assert after.EMBED_COLOR == original

    def test_a_tuple_setting_survives_a_save(self, config_factory):
        """get_env_var() requires the reloaded value to have the same
        type as the default, so writing a tuple out as a list made the
        next startup die with "invalid value for
        SUPPORTED_EXTENSIONS"."""
        env = SECRETS_ENV + "SUPPORTED_EXTENSIONS=('.mp3', '.flac')\n"
        before = config_factory(env, SECRETS_SAMPLE)
        before.save()

        # would raise TypeError before the fix
        after = config_factory(
            open(".env", encoding="utf-8").read(), SECRETS_SAMPLE
        )
        assert after.SUPPORTED_EXTENSIONS == (".mp3", ".flac")

    def test_writes_to_the_env_it_loaded_not_to_cwd(
        self, config_factory, tmp_path, monkeypatch
    ):
        """load_dotenv()/find_dotenv() resolve the .env by walking up
        from the calling module's file, but the writer used to open the
        literal relative path ".env". Started from anywhere but the
        project root, the bot read one file and d!guild_whitelist wrote
        a different one into cwd - so the change vanished on restart."""
        # BOT_PREFIX is left at its class default, so it never enters
        # _changed_vars. That makes it the discriminator below: only a
        # writer that actually read this file carries it through.
        config = config_factory(
            SECRETS_ENV + "BOT_PREFIX=d!\n", SECRETS_SAMPLE
        )

        elsewhere = tmp_path / "some" / "other" / "cwd"
        elsewhere.mkdir(parents=True)
        monkeypatch.chdir(elsewhere)

        config.GUILD_WHITELIST.append(222)
        config.save()

        # nothing created in cwd ...
        assert not (elsewhere / ".env").exists()
        written = (tmp_path / ".env").read_text(encoding="utf-8")
        # ... the change landed in the file that was loaded ...
        assert "222" in written
        # ... and the *read* side used that same file. A reader pointed
        # at a different (missing) path sees no existing settings, so it
        # appends every changed one and silently drops everything the
        # file held that this run did not change.
        assert "BOT_PREFIX=d!" in written

    def test_a_changed_setting_is_written(self, config_factory):
        config = config_factory(SECRETS_ENV, SECRETS_SAMPLE)
        config.GUILD_WHITELIST.append(222)
        config.save()

        after = config_factory(
            open(".env", encoding="utf-8").read(), SECRETS_SAMPLE
        )
        assert after.GUILD_WHITELIST == [111, 222]


class TestGuildWhitelistPersistence:
    """d!guild_whitelist is the only caller of save(), so the way it
    mutates the list decides whether save() has anything to do."""

    def test_rebinding_is_persisted_from_the_default(self, config_factory):
        config = config_factory("BOT_TOKEN=t\nGUILD_WHITELIST=[]\n", "")
        config.GUILD_WHITELIST = config.GUILD_WHITELIST + [123]
        config.save()
        assert "123" in open(".env", encoding="utf-8").read()

    def test_in_place_mutation_is_not_persisted(self, config_factory):
        """Documents why the command must rebind: __setattr__ is what
        records a changed setting, and .append() never reaches it."""
        config = config_factory("BOT_TOKEN=t\nGUILD_WHITELIST=[]\n", "")
        config.GUILD_WHITELIST.append(123)
        config.save()
        assert "123" not in open(".env", encoding="utf-8").read()

    def test_removing_the_last_id_is_persisted(self, config_factory):
        """Back to [] is back to the class default, so the old
        "differs from the default" tracking never recorded it: save()
        left the id in .env and the next restart re-read it and left
        every other guild again."""
        config = config_factory("BOT_TOKEN=t\nGUILD_WHITELIST=[123]\n", "")
        whitelist = list(config.GUILD_WHITELIST)
        whitelist.remove(123)
        config.GUILD_WHITELIST = whitelist
        config.save()
        written = open(".env", encoding="utf-8").read()
        assert "GUILD_WHITELIST=[]" in written
        assert "123" not in written

    def test_add_then_remove_in_one_process_is_persisted(self, config_factory):
        """save() clears _changed_vars, so the second save has to
        record the revert on its own."""
        config = config_factory("BOT_TOKEN=t\nGUILD_WHITELIST=[]\n", "")
        config.GUILD_WHITELIST = config.GUILD_WHITELIST + [7]
        config.save()
        assert "GUILD_WHITELIST=[7]" in open(".env", encoding="utf-8").read()

        whitelist = list(config.GUILD_WHITELIST)
        whitelist.remove(7)
        config.GUILD_WHITELIST = whitelist
        config.save()
        assert "GUILD_WHITELIST=[]" in open(".env", encoding="utf-8").read()

    def test_the_command_rebinds_rather_than_appending(self):
        import inspect

        from musicbot.commands.developer import Developer

        for cmd in (
            Developer._guild_whitelist_add,
            Developer._guild_whitelist_remove,
        ):
            src = inspect.getsource(cmd.callback)
            assert "config.GUILD_WHITELIST = " in src
            assert "config.GUILD_WHITELIST.append" not in src
            assert "config.GUILD_WHITELIST.remove" not in src


def test_extra_owners_defaults_to_empty(config_factory):
    """An unconfigured deployment must trust only the application
    owner(s) Discord reports - this replaced a hardcoded user id."""
    assert config_factory(SECRETS_ENV, SECRETS_SAMPLE).EXTRA_OWNERS == []


def test_message_strings_do_not_pick_up_stray_substitutions(
    config_factory,
):
    """config.Formatter is a string.Template with an empty delimiter,
    so any bare identifier in en.json that matches a config key is
    substituted. Naming a setting inside its own message renders the
    value instead of the name."""
    config = config_factory(SECRETS_ENV, SECRETS_SAMPLE)
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
        ],
    )
    def test_leaves_non_file_databases_alone(self, url, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ensure_sqlite_parent(url)
        assert list(tmp_path.iterdir()) == []
