import ast
import inspect
import os
import sys
import warnings
from typing import Optional

import jsonc
from packaging.requirements import Requirement
# from dotenv import load_dotenv
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
        self.DATABASE_LIBRARY = self.DATABASE.partition("+")[2].partition(":")[
            0
        ]
        db_req = Requirement(self.DATABASE_LIBRARY)
        self.DATABASE = self.DATABASE.replace(
            self.DATABASE_LIBRARY, db_req.name, 1
        )
        self.DATABASE_LIBRARY_NAME = db_req.name
        if not db_req.specifier:
            with open(
                os.path.join(os.path.dirname(__file__), "db-requirements.txt")
            ) as f:
                for line in f:
                    req = Requirement(line)
                    if req.name == db_req.name:
                        self.DATABASE_LIBRARY = str(req)
                        break

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
        # load_dotenv()
        
        # Check for deprecated environment variable with typo
        if "VC_TIMOUT_DEFAULT" in os.environ:
            # in env, we can't fix it easily
            raise RuntimeError(
                "Please rename VC_TIMOUT_DEFAULT"
                " to VC_TIMEOUT_DEFAULT in your environment"
            )

        # Initialize unknown_vars to track variables from environment that aren't in Config class
        self.unknown_vars = {}

        # Ensure SUPPORTED_EXTENSIONS is a tuple
        current_cfg["SUPPORTED_EXTENSIONS"] = tuple(
            current_cfg["SUPPORTED_EXTENSIONS"]
        )

        # Read .env file directly to check for unknown variables
        env_file = ".env"
        if os.path.isfile(env_file):
            with open(env_file, "r") as f:
                env_content = f.read()
                
            # Parse .env file
            for line in env_content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    # Check if this variable is defined in the Config class
                    if key not in current_cfg and not key.startswith('_'):
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
        Warn about environment variables that are not defined in the Config class.
        These might be typos or variables that are no longer used.
        """
        for name, value in self.unknown_vars.items():
            # Mask sensitive values like tokens
            masked_value = value
            if 'token' in name.lower() or 'key' in name.lower() or 'secret' in name.lower() or 'password' in name.lower():
                if len(value) > 8:
                    masked_value = value[:4] + '...' + value[-4:]
                else:
                    masked_value = '********'
                    
            warnings.warn(f"Unknown environment variable: {name}={masked_value}"
                          f"\nThis variable is not defined in the Config class and will be ignored.")

    def update(self, data: dict):
        for k, v in data.items():
            setattr(self, k, v)
            
    def __setattr__(self, name, value):
        """
        Override __setattr__ to track changes to variables.
        """
        # List of internal variables that shouldn't be tracked
        internal_vars = [
            'COOKIE_PATH',  # Don't track COOKIE_PATH as it can change based on runtime path
            'DATABASE',     # Internal database connection string
            'DATABASE_LIBRARY',  # Internal database library
            'DATABASE_LIBRARY_NAME',  # Internal database library name
            'messages',     # Internal messages dictionary
            'dicts',        # Internal dictionaries
            'unknown_vars', # Internal tracking of unknown variables
            'prefix',       # Internal prefix for display
        ]
        
        # Track changes to non-internal variables
        if not name.startswith('_') and name not in internal_vars:
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
        Update .env and .env.sample files with configuration values from the Config class
        that have been explicitly changed and don't match the current environment variables.
        """
        # Read .env file if it exists
        env_file = ".env"
        env_vars = {}
        env_content = ""
        if os.path.isfile(env_file):
            with open(env_file, "r") as f:
                env_content = f.read()
                
            # Parse .env file
            for line in env_content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key] = value
        
        # Read .env.sample file if it exists
        sample_file = ".env.sample"
        sample_vars = {}
        sample_content = ""
        sample_comments = {}
        current_comment = []
        
        if os.path.isfile(sample_file):
            with open(sample_file, "r") as f:
                sample_content = f.read()
                
            # Parse .env.sample file
            for line in sample_content.splitlines():
                line_stripped = line.strip()
                if not line_stripped:
                    current_comment = []
                    continue
                if line_stripped.startswith("#"):
                    current_comment.append(line)
                    continue
                if "=" in line_stripped:
                    key, value = line_stripped.split("=", 1)
                    sample_vars[key] = value
                    if current_comment:
                        sample_comments[key] = current_comment
                    current_comment = []
        
        # Check for variables that need to be updated in .env
        env_updated = False
        
        # Only update variables that have been explicitly changed
        for key, value in self._changed_vars.items():
            # Skip internal variables and methods
            if key.startswith("_") or callable(value):
                continue
                
            # Convert value to string representation for .env file
            if isinstance(value, str):
                env_value = value
            elif isinstance(value, (list, tuple)):
                env_value = str(list(value))
            else:
                env_value = str(value)
                
            # Check if variable exists in .env file with a different value
            if key in env_vars:
                # Variable exists in .env file, check if it matches current value
                env_var_str = env_vars[key]
                try:
                    if not isinstance(value, str):
                        env_var = ast.literal_eval(env_var_str)
                    else:
                        env_var = env_var_str
                except (SyntaxError, ValueError):
                    env_var = env_var_str
                    
                # Convert both to strings for comparison to handle different types
                env_value_str = str(env_value)
                current_env_str = str(env_var)
                
                # If values don't match, update .env
                if env_value_str != current_env_str:
                    # Update existing variable in .env
                    env_content = self._replace_env_var(env_content, key, env_value)
                    env_updated = True
                    print(f"Updating {key} in .env from {env_var_str} to {env_value}")
            else:
                # Variable doesn't exist in .env, append it
                env_content += f"\n{key}={env_value}"
                env_updated = True
                print(f"Adding {key}={env_value} to .env")
                
        # Write updated .env file if changes were made
        if env_updated:
            with open(env_file, "w") as f:
                f.write(env_content)
                
        # Check for variables that need to be updated in .env.sample
        sample_updated = False
        
        # Only update variables that have been explicitly changed
        for key, value in self._changed_vars.items():
            # Skip internal variables and methods
            if key.startswith("_") or callable(value):
                continue
                
            # Convert value to string representation for .env.sample file
            if isinstance(value, str):
                sample_value = value
            elif isinstance(value, (list, tuple)):
                sample_value = str(list(value))
            else:
                sample_value = str(value)
                
            # Check if variable exists in .env.sample with a different value
            if key in sample_vars:
                # Variable exists in .env.sample, check if it matches current value
                sample_var_str = sample_vars[key]
                try:
                    if not isinstance(value, str):
                        sample_var = ast.literal_eval(sample_var_str)
                    else:
                        sample_var = sample_var_str
                except (SyntaxError, ValueError):
                    sample_var = sample_var_str
                    
                # Convert both to strings for comparison to handle different types
                sample_value_str = str(sample_value)
                current_sample_str = str(sample_var)
                
                # If values don't match, update .env.sample
                if sample_value_str != current_sample_str:
                    # Update existing variable in .env.sample
                    sample_content = self._replace_env_var(sample_content, key, sample_value)
                    sample_updated = True
                    print(f"Updating {key} in .env.sample from {sample_var_str} to {sample_value}")
            else:
                # Variable doesn't exist in .env.sample, append it with comments
                if key in sample_comments:
                    # Use existing comments if available
                    sample_content += "\n" + "\n".join(sample_comments[key])
                else:
                    # Add a default comment
                    sample_content += f"\n# {key} configuration"
                sample_content += f"\n{key}={sample_value}\n"
                sample_updated = True
                print(f"Adding {key}={sample_value} to .env.sample")
                
        # Write updated .env.sample file if changes were made
        if sample_updated:
            with open(sample_file, "w") as f:
                f.write(sample_content)
                
        # Clear the changed variables after saving
        self._changed_vars = {}
                
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
                if not target.id.startswith('_'):
                    result[target.id] = comment
        return result
