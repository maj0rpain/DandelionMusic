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


def parse_env_file(path: str) -> tuple:
    """Reads an env-style file into (raw text, {key: raw value}).

    One parser for .env and .env.sample alike - they have the same
    shape, and the three hand-rolled copies of this that used to exist
    had already drifted apart in how they treated blank and comment
    lines."""
    if not os.path.isfile(path):
        return "", {}
    with open(path, "r", encoding="utf-8") as f:
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

        # Resolve the .env explicitly rather than letting
        # load_dotenv() search: find_dotenv() walks up from the
        # *calling module's file* - this one - and the path is
        # reused below for the unknown-variable scan, which would
        # otherwise read a different file whenever the process was
        # started from somewhere other than the project root.
        self._env_path = find_dotenv() or os.path.abspath(".env")
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
