"""Config loading.

Config is read-only now: the .env write-back that used to live here
(and produced five separate bugs) went away with the guild
whitelist, which the database owns instead.
"""

import re
from pathlib import Path

import jsonc
import pytest

from config.utils import alchemize_url, ensure_sqlite_parent, get_env_var

from helpers import dockerfile_commands

REPO_ROOT = Path(__file__).parent.parent

SECRETS_ENV = (
    "BOT_TOKEN=real-token-abc123\n"
    "SPOTIFY_SECRET=real-spotify-secret\n"
    "LASTFM_API_KEY=real-lastfm-key\n"
    "GUILD_WHITELIST=[111]\n"
)
# The tests of the Spotipy aliases need the Spotify secret unset.
SECRETS_ENV_WITHOUT_SPOTIFY = (
    "BOT_TOKEN=t\nLASTFM_API_KEY=k\nGUILD_WHITELIST=[111]\n"
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
        (
            "sqlite:///settings.db",
            ("sqlite+aiosqlite:///settings.db", "aiosqlite"),
        ),
        ("postgres://h/d", ("postgresql+asyncpg://h/d", "asyncpg")),
        ("mysql://h/d", ("mysql+aiomysql://h/d", "aiomysql")),
        ("weird+driver://h/d", ("weird+driver://h/d", "driver")),
        ("postgresql://h/d", ("postgresql+asyncpg://h/d", "asyncpg")),
        ("postgres+asyncpg://h/d", ("postgresql+asyncpg://h/d", "asyncpg")),
        ("mysql+aiomysql://h/d", ("mysql+aiomysql://h/d", "aiomysql")),
    ],
)
def test_alchemize_url(url, expected):
    assert alchemize_url(url) == expected


@pytest.mark.parametrize(
    "url, scheme",
    [
        ("mariadb://u:secret@h/d", "mariadb"),
        ("postgresql+://u:secret@h/d", "postgresql+"),
        ("postgres", None),
        ("postgres:/u:secret@h", None),
    ],
)
def test_alchemize_url_rejects_unmappable_urls(url, scheme):
    with pytest.raises(ValueError) as exc:
        alchemize_url(url)
    msg = str(exc.value)
    assert "DATABASE_URL" in msg
    for form in ("sqlite://", "postgresql://", "mysql://"):
        assert form in msg
    assert "dialect+asyncdriver://" in msg
    if scheme:
        assert scheme in msg
    assert "secret" not in msg
    assert "postgres:/" not in msg


def test_config_normalises_a_postgresql_url(config_factory):
    config = config_factory(
        SECRETS_ENV + "DATABASE_URL=postgresql://u:p@h/db\n"
    )
    assert config.DATABASE == "postgresql+asyncpg://u:p@h/db"
    assert config.DATABASE_LIBRARY_NAME == "asyncpg"


def test_config_keeps_the_rest_of_the_url_when_naming_the_driver(
    config_factory,
):
    config = config_factory(
        SECRETS_ENV + "DATABASE_URL=postgres+asyncpg://asyncpg:asyncpg@h/db\n"
    )
    assert config.DATABASE == "postgresql+asyncpg://asyncpg:asyncpg@h/db"


def test_config_load_rejects_an_unmappable_url(config_factory):
    with pytest.raises(ValueError):
        config_factory(SECRETS_ENV + "DATABASE_URL=mariadb://u:p@h/db\n")


def test_config_rejects_a_driver_that_is_not_a_valid_requirement(
    config_factory,
):
    with pytest.raises(ValueError):
        config_factory(
            SECRETS_ENV + "DATABASE_URL=postgresql+bad name://h/d\n"
        )


def test_config_rejects_a_non_bare_driver_naming_only_the_driver(
    config_factory,
):
    with pytest.raises(ValueError) as exc:
        config_factory(
            SECRETS_ENV + "DATABASE_URL=postgresql+asyncpg>=1://u:secret@h/d\n"
        )
    msg = str(exc.value)
    assert "asyncpg>=1" in msg
    assert "secret" not in msg


def _uv_sync_index(commands):
    (index,) = _indices_where(
        commands, lambda c: c.startswith("RUN") and "uv sync" in c
    )
    return index


def test_dockerfile_installs_db_drivers_without_dev():
    commands = dockerfile_commands()
    run = commands[_uv_sync_index(commands)]
    assert "--frozen" not in run
    for flag in (
        "--locked",
        "--no-cache",
        "--no-dev",
        "--extra postgres",
        "--extra mysql",
    ):
        assert flag in run


def _indices_where(commands, predicate):
    return [i for i, c in enumerate(commands) if predicate(c)]


def test_dockerfile_runs_as_a_non_root_user():
    users = [
        c.split()[1] for c in dockerfile_commands() if c.split()[0] == "USER"
    ]
    assert users and users[-1].split(":")[0] not in ("root", "0")


def test_dockerfile_copies_source_only_after_dependencies():
    commands = dockerfile_commands()
    sync = _uv_sync_index(commands)
    (copy_all,) = _indices_where(commands, lambda c: c.startswith("COPY . ."))
    assert copy_all > sync
    early_copies = [
        c.split()[1:-1]
        for c in commands[:sync]
        if c.startswith("COPY") and "--from=" not in c
    ]
    assert early_copies == [["pyproject.toml", "uv.lock"]]


def _image_tag(image):
    """The tag of an image reference; a digest counts as pinned."""
    if "@" in image:
        return image.split("@", 1)[1]
    last = image.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else None


def test_dockerfile_images_are_pinned_and_not_piped_to_a_shell():
    import re

    commands = dockerfile_commands()
    images = [c.split()[1] for c in commands if c.startswith("FROM")]
    copied = []
    for c in commands:
        m = re.match(r"COPY .*--from=(\S+)", c)
        if m:
            copied.append(m.group(1))
    for image in images + copied:
        tag = _image_tag(image)
        assert tag and tag != "latest", f"{image!r} is not pinned"
    for image in copied:
        assert re.search(r":(?:\w+-)?v?\d+\.\d+\.\d+$", image)
    assert not any(re.search(r"curl.*\|\s*(ba)?sh", c) for c in commands)


def test_dockerfile_apt_installs_are_minimal_and_clean():
    commands = dockerfile_commands()
    sync = _uv_sync_index(commands)
    for i in _indices_where(commands, lambda c: "apt-get" in c):
        assert commands[i].startswith("RUN") and i < sync
    for c in commands:
        if "apt-get install" not in c:
            continue
        assert "curl" not in c.split() and "unzip" not in c.split()
        assert "rm -rf /var/lib/apt/lists/*" in c


def _compose_services():
    import yaml

    text = (REPO_ROOT / "docker-compose.yaml").read_text()
    return yaml.safe_load(text)["services"]


def test_compose_bgutil_provider_publishes_no_host_port():
    assert "ports" not in _compose_services()["bgutil-provider"]


def test_compose_library_mount_defaults_to_relative_music_dir():
    volumes = _compose_services()["doybot"]["volumes"]
    mounts = [v for v in volumes if ":/music" in v]
    assert len(mounts) == 1
    source, target, mode = mounts[0].rsplit(":", 2)
    assert (target, mode) == ("/music", "ro")
    match = re.fullmatch(r"\$\{MUSIC_LIBRARY_HOST_PATH:-(.*)\}", source)
    assert match and match.group(1) == "./music"


def test_default_library_dir_is_tracked_and_kept_out_of_git_and_image():
    assert (REPO_ROOT / "music" / ".gitkeep").is_file()
    ignored = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert "/music/*" in ignored and "!/music/.gitkeep" in ignored
    assert "music/" in (REPO_ROOT / ".dockerignore").read_text().splitlines()


def test_env_sample_documents_compose_only_library_host_path():
    from config.config import parse_env_file

    text, keys = parse_env_file(str(REPO_ROOT / ".env.sample"))
    assert "MUSIC_LIBRARY_HOST_PATH" in text
    assert "MUSIC_LIBRARY_HOST_PATH" not in keys


def test_compose_pulled_images_are_pinned():
    for name, service in _compose_services().items():
        if "image" not in service or "build" in service:
            continue
        tag = _image_tag(service["image"])
        assert (
            tag and tag != "latest"
        ), f"{name}: image {service['image']!r} is not pinned"


def test_compose_bgutil_provider_tag_matches_the_pinned_plugin():
    import tomllib

    meta = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    deps = meta["project"]["dependencies"]
    found = [
        d
        for d in deps
        if d.replace("_", "-").lower().startswith("bgutil-ytdlp-pot-provider")
    ]
    assert (
        len(found) == 1 and "==" in found[0]
    ), f"expected bgutil-ytdlp-pot-provider==X, found {found!r}"
    version = found[0].split("==", 1)[1].split(";")[0].strip()
    image = _compose_services()["bgutil-provider"]["image"]
    assert (
        _image_tag(image) == version
    ), f"{image!r} does not match the plugin pin {version!r}"


def test_extra_owners_defaults_to_empty(config_factory):
    """An unconfigured deployment must trust only the application
    owner(s) Discord reports - this replaced a hardcoded user id."""
    assert config_factory(SECRETS_ENV).EXTRA_OWNERS == []


def _expected_text(raw, mapping):
    """raw with only `{name}` occurrences of names in mapping replaced."""
    return re.sub(
        r"\{(\w+)\}",
        lambda m: (
            str(mapping[m.group(1)]) if m.group(1) in mapping else m.group(0)
        ),
        raw,
    )


def _check_rendered(raw, rendered, mapping, path=""):
    if isinstance(raw, str):
        assert rendered == _expected_text(raw, mapping), path
    elif isinstance(raw, dict):
        assert rendered.keys() == raw.keys(), path
        for k, v in raw.items():
            _check_rendered(v, rendered[k], mapping, f"{path}/{k}")


def test_en_json_renders_only_braced_config_names(config_factory):
    """Every en.json string comes out as its raw text with only the
    braced config names filled: a bare word that equals a config key
    stays literal, runtime placeholders reach their .format() call."""
    from config.config import Config
    from config.utils import CONFIG_DIRS

    shipped = [d for d in CONFIG_DIRS if (Path(d) / "en.json").is_file()]
    assert shipped == [str(REPO_ROOT / "config")]
    raw = jsonc.loads((REPO_ROOT / "config" / "en.json").read_text("utf-8"))

    keep = {"DATABASE_URL", "COOKIE_PATH", "EMBED_COLOR"}
    sentinels = {
        k: f"SENTINEL<{k}>"
        for k, v in Config.as_dict().items()
        if isinstance(v, str) and k not in keep
    }
    env = "".join(f"{k}={v}\n" for k, v in sentinels.items())
    config = config_factory(env)

    mapping = {**Config.as_dict(), **sentinels}
    mapping["prefix"] = sentinels["BOT_PREFIX"]
    rendered = {**config.messages, **config.dicts}
    _check_rendered(raw, rendered, mapping)


def test_double_braces_are_not_a_load_time_escape():
    from config.utils import Formatter

    text = Formatter("{{SEARCH_RESULTS}} {{}} {x:>3}").format(
        {"SEARCH_RESULTS": "5"}
    )
    assert text == "{5} {{}} {x:>3}"


def test_truncation_notice_does_not_leak_the_spotify_secret(config_factory):
    config = config_factory(
        "BOT_TOKEN=t\nSPOTIFY_ID=\nSPOTIFY_SECRET=s3cr3t-sentinel\n"
    )
    notice = config.SPOTIFY_TRUNCATED_NO_API
    assert "SPOTIFY_ID" in notice and "SPOTIFY_SECRET" in notice
    assert "s3cr3t-sentinel" not in notice


def test_library_messages_name_the_setting_not_its_value(config_factory):
    config = config_factory("BOT_TOKEN=t\nMUSIC_LIBRARY_PATH=/host/secret\n")
    for text in (
        config.LIBRARY_NOT_CONFIGURED,
        config.LIBRARY_EMPTY,
        config.HELP_LIBRARY_REFRESH_LONG,
    ):
        assert "MUSIC_LIBRARY_PATH" in text
        assert "/host/secret" not in text


@pytest.mark.parametrize("slash, shown", [("True", "/"), ("False", "@bot ")])
def test_library_hint_without_a_prefix(config_factory, slash, shown):
    cfg = config_factory(
        f"BOT_TOKEN=x\nBOT_PREFIX=\nENABLE_SLASH_COMMANDS={slash}\n"
    )
    assert f"`{shown}library refresh`" in cfg.LIBRARY_PICK_STALE_INDEX


@pytest.mark.parametrize(
    "key",
    ["LIBRARY_EMPTY", "LIBRARY_FILE_MISSING", "LIBRARY_PICK_STALE_INDEX"],
)
def test_library_hints_name_a_bang_prefix(config_factory, key):
    cfg = config_factory("BOT_TOKEN=x\nBOT_PREFIX=!\n")
    text = getattr(cfg, key)
    assert "`!library refresh`" in text
    assert "d!" not in text


def test_no_language_file_string_holds_a_literal_d_bang():
    def strings(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for v in node.values():
                yield from strings(v)
        elif isinstance(node, list):
            for v in node:
                yield from strings(v)

    path = REPO_ROOT / "config" / "en.json"
    with open(path, encoding="utf-8") as f:
        found = [s for s in strings(jsonc.load(f)) if "d!" in s]
    assert found == []


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


def test_heroku_db_is_used_as_the_database_url(config_factory):
    config = config_factory(SECRETS_ENV + "HEROKU_DB=postgresql://u:p@h/db\n")
    assert config.DATABASE == "postgresql+asyncpg://u:p@h/db"


def test_database_url_wins_over_heroku_db(config_factory):
    config = config_factory(
        SECRETS_ENV
        + "HEROKU_DB=postgresql://u:p@h/heroku\n"
        + "DATABASE_URL=postgresql://u:p@h/real\n"
    )
    assert config.DATABASE == "postgresql+asyncpg://u:p@h/real"


def test_heroku_db_is_not_reported_as_unknown(config_factory):
    config = config_factory(SECRETS_ENV + "HEROKU_DB=sqlite:///x.db\n")
    assert "HEROKU_DB" not in config.unknown_vars


def test_spotipy_aliases_are_not_reported_as_unknown(config_factory):
    config = config_factory(
        SECRETS_ENV
        + "SPOTIPY_CLIENT_ID=cid\nSPOTIPY_CLIENT_SECRET=csecret\n"
        + "MY_TYPO=1\n"
    )
    assert set(config.unknown_vars) == {"MY_TYPO"}


def test_spotipy_alias_applies_when_canonical_is_unset(config_factory):
    config = config_factory(
        SECRETS_ENV_WITHOUT_SPOTIFY
        + "SPOTIPY_CLIENT_ID=cid\nSPOTIPY_CLIENT_SECRET=csecret\n"
    )
    assert (config.SPOTIFY_ID, config.SPOTIFY_SECRET) == ("cid", "csecret")


def test_heroku_db_applies_when_database_url_is_empty(config_factory):
    config = config_factory(
        SECRETS_ENV + "DATABASE_URL=\nHEROKU_DB=postgresql://u:p@h/db\n"
    )
    assert config.DATABASE == "postgresql+asyncpg://u:p@h/db"


def test_spotipy_alias_applies_when_canonical_is_empty(config_factory):
    config = config_factory(
        SECRETS_ENV_WITHOUT_SPOTIFY
        + "SPOTIFY_ID=\nSPOTIPY_CLIENT_ID=cid\n"
        + "SPOTIPY_CLIENT_SECRET=csecret\n"
    )
    assert (config.SPOTIFY_ID, config.SPOTIFY_SECRET) == ("cid", "csecret")


def test_canonical_spotify_variable_wins_over_alias(config_factory):
    config = config_factory(
        SECRETS_ENV + "SPOTIFY_ID=real\nSPOTIPY_CLIENT_ID=alias\n"
    )
    assert config.SPOTIFY_ID == "real"


def test_empty_heroku_db_changes_nothing(config_factory):
    config = config_factory(SECRETS_ENV + "HEROKU_DB=\n")
    assert config.DATABASE.startswith("sqlite+aiosqlite:")
    with pytest.raises(ValueError):
        config_factory(SECRETS_ENV + "DATABASE_URL=\nHEROKU_DB=\n")


def test_aliases_do_not_write_the_environment(config_factory):
    import os

    config_factory(SECRETS_ENV + "HEROKU_DB=sqlite:///x.db\n")
    assert os.environ.get("DATABASE_URL", "") == ""


def test_unknown_var_warning_names_it_without_its_value(config_factory):
    config = config_factory(SECRETS_ENV + "MY_API_TOKEN=zq81xk-middle-v7mw\n")
    with pytest.warns(UserWarning) as record:
        config.warn_unknown_vars()
    text = " ".join(str(w.message) for w in record)
    assert "MY_API_TOKEN" in text
    assert "zq81" not in text
    assert "v7mw" not in text


@pytest.mark.parametrize("raw, expected", [(0, 1), (9, 9), (30, 9)])
def test_search_results_is_clamped_to_the_keycap_range(
    config_factory, raw, expected
):
    cfg = config_factory(f"SEARCH_RESULTS={raw}\n")
    assert cfg.SEARCH_RESULTS == expected


def test_config_no_longer_writes_anything():
    """Config is read-only now. save() and the .env writer are gone."""
    from config import config

    for gone in ("save", "_update_env_files", "_replace_env_var"):
        assert not hasattr(config, gone), f"{gone} is still there"
