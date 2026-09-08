import ast
import inspect
import os
import sys
import warnings
from typing import Optional

import jsonc
from packaging.requirements import Requirement
from dotenv import find_dotenv, load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from utils import (  # noqa: E402
    CONFIG_DIRS,
    Formatter,
    get_env_var,
    alchemize_url,
    load_configs,
    join_dicts,
)

del sys.path[0]


DERIVED_SETTINGS = frozenset(
    {
        # Settings Config computes at startup rather than reading
        # straight from the environment. save() must not persist these:
        # what sits in memory is a *processed* form of what was
        # configured, and writing it back produces a file the next
        # startup either reads differently or refuses outright.
        #
        # EMBED_COLOR is the cautionary one - it is authored as the hex
        # string "0x4DD4D0" and rewritten in place as the int it parses
        # to, so persisting the int meant the next boot re-parsed it as
        # hex: 0x4DD4D0 -> 5100752 -> 84936530, a different colour on
        # every save.
        "COOKIE_PATH",  # resolved against CONFIG_DIRS
        "EMBED_COLOR",  # hex string parsed to int
        "DATABASE",  # alchemize_url() of DATABASE_URL
        "DATABASE_LIBRARY_NAME",  # driver name pulled out of DATABASE
        "messages",  # loaded from en.json
        "dicts",  # loaded from en.json
        "unknown_vars",  # scanned out of .env
        "prefix",  # display form of BOT_PREFIX
    }
)


def parse_env_file(path: str) -> tuple:
    """Reads an env-style file into (raw text, {key: raw value}).

    One parser for .env and .env.sample alike - they have the same
    shape, and the three hand-rolled copies of this that used to exist
    had already drifted apart in how they treated blank and comment
    lines."""
    if not os.path.isfile(path):
        return "", {}
    with open(path, "r") as f:
        content = f.read()
    values = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return content, values


def format_env_value(value) -> str:
    """The .env text for a setting's value.

    repr() for lists and tuples rather than str(list(...)):
    get_env_var() literal_evals what it reads back and then requires
    the result to have the same type as the Config default, so writing
    a tuple out as a list produced a file the next startup refused with
    "invalid value for SUPPORTED_EXTENSIONS"."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return repr(value)
    return str(value)


def env_value_matches(raw: str, value) -> bool:
    """Whether the file already holds `value`.

    A file only ever holds text, so a non-str setting is parsed back
    before comparing - otherwise [1, 2] and "[1, 2]" would look
    different on every save and rewrite the file each time."""
    if isinstance(value, str):
        current = raw
    else:
        try:
            current = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            current = raw
    return format_env_value(value) == str(current)


class Config:
    BOT_TOKEN = "YOUR_TOKEN_GOES_HERE"
    SPOTIFY_ID = ""
    SPOTIFY_SECRET = ""

    # set to empty string to disable
    BOT_PREFIX = "d!"
    ENABLE_SLASH_COMMANDS = False
    MENTION_AS_PREFIX = True

    # seconds
    VC_TIMEOUT = 600
    # default template setting for VC timeout
    # true = yes, timeout; false = no timeout
    VC_TIMEOUT_DEFAULT = True
    # allow or disallow editing the vc_timeout guild setting
    ALLOW_VC_TIMEOUT_EDIT = True

    # maximum of 25
    MAX_SONG_PRELOAD = 25
    # how many results to display in d!search
    SEARCH_RESULTS = 5

    MAX_HISTORY_LENGTH = 10
    MAX_TRACKNAME_HISTORY_LENGTH = 15

    # If database is not one of sqlite, postgres or MySQL
    # you need to provide the url in SQL Alchemy-supported format.
    # Must be async-compatible
    # CHANGE ONLY IF YOU KNOW WHAT YOU'RE DOING
    DATABASE_URL = os.getenv("HEROKU_DB") or "sqlite:///settings.db"

    ENABLE_BUTTON_PLUGIN = True

    # enables browsing/queueing a local music library via Discord UI
    ENABLE_LOCAL_LIBRARY = False

    # root folder for the local music library, expected to contain
    # Artist/Album/song.ext (extensions from SUPPORTED_EXTENSIONS)
    MUSIC_LIBRARY_PATH = ""

    # extensions the local library indexer treats as playable audio
    # (kept separate from SUPPORTED_EXTENSIONS, which also governs what
    # raw pasted URLs the bot accepts as custom download links)
    LIBRARY_EXTENSIONS = (
        ".mp3",
        ".flac",
        ".m4a",
        ".ogg",
        ".opus",
        ".wav",
        ".aac",
        ".wma",
        ".aiff",
        ".alac",
    )

    # enables album/artist bio summaries in d!library browse
    # (last.fm's artist.getInfo/album.getInfo); get a free key at
    # https://www.last.fm/api/account/create
    LASTFM_API_KEY = ""

    # replace after '0x' with desired hex code ex. '#ff0188' >> "0xff0188"
    EMBED_COLOR: int = "0x4DD4D0"  # converted to int in __init__

    SUPPORTED_EXTENSIONS = (
        ".webm",
        ".mp4",
        ".mp3",
        ".avi",
        ".wav",
        ".m4v",
        ".ogg",
        ".mov",
    )

    COOKIE_PATH = "config/cookies/cookies.txt"

    GLOBAL_DISABLE_AUTOJOIN_VC = False

    # whether to tell users the bot is disconnecting
    ANNOUNCE_DISCONNECT = True

    ENABLE_PLAYLISTS = True

    # if not empty, the bot will leave non-whitelisted guilds
    GUILD_WHITELIST = []

    # extra user ids treated as bot owners, on top of the application
    # owner(s) Discord itself reports. These unlock every owner-only
    # command, including d!execute, which runs arbitrary Python in the
    # bot process - only add someone you would hand the host to.
    # Format: [123456789012345678, 987654321098765432]
    EXTRA_OWNERS = []

    # Track which variables have been changed
    _changed_vars = {}

    def __init__(self):
        current_cfg = self.load()

        # prefix to display
        current_cfg["prefix"] = (
            self.BOT_PREFIX
            if self.BOT_PREFIX
            else ("/" if self.ENABLE_SLASH_COMMANDS else "@bot ")
        )

        self.DATABASE = alchemize_url(self.DATABASE_URL)
        driver_name = self.DATABASE.partition("+")[2].partition(":")[0]
        db_req = Requirement(driver_name)
        self.DATABASE = self.DATABASE.replace(driver_name, db_req.name, 1)
        self.DATABASE_LIBRARY_NAME = db_req.name

        # Convert EMBED_COLOR to integer if it's a string
        if isinstance(self.EMBED_COLOR, str):
            self.EMBED_COLOR = int(self.EMBED_COLOR, 16)
        for dir_ in CONFIG_DIRS[::-1]:
            path = os.path.join(dir_, self.COOKIE_PATH)
            if os.path.isfile(path):
                self.COOKIE_PATH = path
                break

        data = join_dicts(
            load_configs(
                "en.json",
                lambda d: {
                    k: (
                        Formatter(v).format(current_cfg)
                        if isinstance(v, str)
                        else v
                    )
                    for k, v in d.items()
                },
            )
        )

        self.messages = {}
        self.dicts = {}
        for k, v in data.items():
            if isinstance(v, str):
                self.messages[k] = v
            elif isinstance(v, dict):
                self.dicts[k] = v

    def load(self) -> dict:
        # Start with default configuration from class attributes
        current_cfg = self.as_dict()

        # Resolve the .env once, and reuse the resolved path for the
        # write side. load_dotenv() with no argument searches via
        # find_dotenv(), which walks up from the *calling module's
        # file* - this one - rather than from cwd, while the writer
        # below opened the literal relative path ".env". Those agree
        # only when the process happens to be started from the project
        # root; started anywhere else the bot read one file and
        # d!guild_whitelist wrote a different one into cwd, so the
        # change silently vanished on the next restart. Calling
        # find_dotenv() from this module gives exactly the path
        # load_dotenv() would have picked, so the read side is
        # unchanged and only the writer moves to follow it.
        self._env_path = find_dotenv() or os.path.abspath(".env")
        self._sample_path = os.path.join(
            os.path.dirname(self._env_path), ".env.sample"
        )
        load_dotenv(self._env_path)

        # Check for deprecated environment variable with typo
        if "VC_TIMOUT_DEFAULT" in os.environ:
            # in env, we can't fix it easily
            raise RuntimeError(
                "Please rename VC_TIMOUT_DEFAULT"
                " to VC_TIMEOUT_DEFAULT in your environment"
            )

        # Initialize unknown_vars to track variables from environment
        # that aren't in Config class
        self.unknown_vars = {}

        # Ensure SUPPORTED_EXTENSIONS is a tuple
        current_cfg["SUPPORTED_EXTENSIONS"] = tuple(
            current_cfg["SUPPORTED_EXTENSIONS"]
        )

        # Read .env directly to check for unknown variables
        for key, value in parse_env_file(self._env_path)[1].items():
            if key not in current_cfg and not key.startswith("_"):
                self.unknown_vars[key] = value

        for key, default in current_cfg.items():
            current_cfg[key] = get_env_var(key, default)

        for key, alias in (
            ("SPOTIFY_ID", "SPOTIPY_CLIENT_ID"),
            ("SPOTIFY_SECRET", "SPOTIPY_CLIENT_SECRET"),
        ):
            if not current_cfg[key]:
                current_cfg[key] = get_env_var(alias, current_cfg[key])

        # Embeds are limited to 25 fields
        current_cfg["MAX_SONG_PRELOAD"] = min(
            current_cfg["MAX_SONG_PRELOAD"], 25
        )

        self.update(current_cfg)
        return current_cfg

    def __getattr__(self, key: str) -> str:
        try:
            return self.messages[key]
        except KeyError as e:
            raise AttributeError(f"No text for {key!r} defined") from e

    def get_dict(self, name: str) -> dict:
        return self.dicts[name]

    def save(self):
        """
        Save configuration to .env and .env.sample files
        if the variable in the Config class doesn't match.
        """
        # Update .env and .env.sample files
        self._update_env_files()

    def warn_unknown_vars(self):
        """
        Warn about environment variables that are not defined
        in the Config class. These might be typos or variables
        that are no longer used.
        """
        for name, value in self.unknown_vars.items():
            # Mask sensitive values like tokens
            masked_value = value
            if (
                "token" in name.lower()
                or "key" in name.lower()
                or "secret" in name.lower()
                or "password" in name.lower()
            ):
                if len(value) > 8:
                    masked_value = value[:4] + "..." + value[-4:]
                else:
                    masked_value = "********"

            warnings.warn(
                f"Unknown environment variable: {name}={masked_value}"
                "\nThis variable is not defined in the Config class"
                " and will be ignored."
            )

    def update(self, data: dict):
        for k, v in data.items():
            setattr(self, k, v)

    def __setattr__(self, name, value):
        """
        Override __setattr__ to track changes to variables.
        """
        # Track changes to non-internal variables
        if not name.startswith("_") and name not in DERIVED_SETTINGS:
            if hasattr(self.__class__, name):
                # Get the default value from the class
                default_value = getattr(self.__class__, name)
                # If the value is different from the default, track it
                if value != default_value:
                    self._changed_vars[name] = value
            else:
                # Track new variables that don't exist in the class
                self._changed_vars[name] = value

        # Call the parent __setattr__
        super().__setattr__(name, value)

    def _update_env_files(self):
        """
        Persist settings changed at runtime.

        Two passes over _changed_vars with deliberately different
        rules, because the two files are for different things: .env is
        this deployment's configuration and gets the live values,
        .env.sample is a committed template and only ever gains
        settings it is missing, at their schema defaults.
        """
        self._update_env()
        self._extend_env_sample()

    def _update_env(self):
        """Write changed settings back to the .env that was loaded.

        The path comes from load(), not from cwd - see the note there.
        """
        content, existing = parse_env_file(self._env_path)
        updated = False

        for key, value in self._changed_vars.items():
            # Skip internal variables and methods
            if key.startswith("_") or callable(value):
                continue

            new_value = format_env_value(value)
            if key not in existing:
                content += f"\n{key}={new_value}"
                updated = True
                print(f"Adding {key}={new_value} to .env")
            elif not env_value_matches(existing[key], value):
                content = self._replace_env_var(content, key, new_value)
                updated = True
                print(
                    f"Updating {key} in .env"
                    f" from {existing[key]} to {new_value}"
                )

        if updated:
            with open(self._env_path, "w") as f:
                f.write(content)

    def _extend_env_sample(self):
        """Add settings the committed template is missing, documented
        with the schema's own default.

        Append-only, and never the live value. Writing live values here
        published BOT_TOKEN, SPOTIFY_SECRET and LASTFM_API_KEY into a
        git-tracked file the moment anything called save() -
        d!guild_whitelist add/remove does. Keeping the template in sync
        with the set of available *settings* is what this is for;
        keeping it in sync with one host's values never was, and an
        entry that is already there is either correct or has been
        deliberately left blank for the reader to fill in.
        """
        content, existing = parse_env_file(self._sample_path)
        updated = False

        for key in self._changed_vars:
            if key.startswith("_") or key in existing:
                continue
            if not hasattr(self.__class__, key):
                # not part of the schema, so there is nothing for the
                # template to document
                continue
            default = getattr(self.__class__, key)
            if callable(default):
                continue

            value = format_env_value(default)
            content += f"\n# {key} configuration\n{key}={value}\n"
            updated = True
            print(f"Adding {key}={value} to .env.sample")

        if updated:
            with open(self._sample_path, "w") as f:
                f.write(content)

    def _replace_env_var(self, content, key, value):
        """
        Replace a variable in the .env file content.
        """
        lines = content.splitlines()
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("#"):
                continue
            if "=" in line_stripped:
                line_key, _ = line_stripped.split("=", 1)
                if line_key == key:
                    lines[i] = f"{key}={value}"
                    break
        return "\n".join(lines)

    @classmethod
    def as_dict(cls) -> dict:
        return {
            k: v
            for k, v in inspect.getmembers(cls)
            if not k.startswith("__") and not inspect.isroutine(v)
        }

    @classmethod
    def get_comments(cls) -> Optional[dict]:
        try:
            src = inspect.getsource(cls)
        except OSError:
            fallback = os.path.join(
                getattr(sys, "_MEIPASS", ""), "config_comments.json"
            )
            if os.path.isfile(fallback):
                with open(fallback) as f:
                    return jsonc.load(f)
            return None
        result = {}
        body = ast.parse(src).body[0].body
        src = src.splitlines()
        for node in body:
            if isinstance(node, ast.Assign):
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            else:
                target = None
            if target is not None:
                comment = ""
                for i in range(node.lineno - 2, -1, -1):
                    line = src[i].strip()
                    if line and not line.startswith("#"):
                        break
                    comment = line[1:].strip() + "\n" + comment
                # Skip internal variables
                if not target.id.startswith("_"):
                    result[target.id] = comment
        return result
