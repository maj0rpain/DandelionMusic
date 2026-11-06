import os
import sys
import ast
import jsonc
import string
from typing import TypeVar, Callable, Iterable, Optional, List, Tuple


T = TypeVar("T")
CONFIG_DIRS = (
    os.path.dirname(__file__),
    os.path.dirname(os.path.abspath(sys.argv[0] or "dummy")),
    os.getcwd(),
)
CONFIG_DIRS = tuple(sorted(set(CONFIG_DIRS), key=CONFIG_DIRS.index))


def get_env_var(key: str, default: T) -> T:
    value = os.getenv(key)
    if value is None:
        return default
    if not isinstance(default, str):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            pass
    if type(value) is not type(default):
        raise TypeError(f"invalid value for {key}: {value!r}")
    return value


_ACCEPTED_FORMS = (
    "sqlite://, postgresql://, mysql://, or dialect+asyncdriver://"
)

_DEFAULT_DRIVERS = {
    "sqlite": "sqlite+aiosqlite",
    "postgres": "postgresql+asyncpg",
    "postgresql": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
}


def alchemize_url(url: str) -> Tuple[str, str]:
    """Give a DATABASE_URL an async driver.

    Returns the normalised URL and the driver it ended up with.

    A driverless sqlite/postgres/postgresql/mysql URL gets its dialect's
    async default; a named driver is kept, with the `postgres` dialect
    renamed to `postgresql`. Anything else raises ValueError, naming the
    scheme but never the rest of the URL (it may hold a password).
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        raise ValueError(
            "DATABASE_URL has no scheme:// "
            f"(accepted forms: {_ACCEPTED_FORMS})"
        )
    dialect, plus, driver = scheme.partition("+")
    if plus and driver:
        if dialect == "postgres":
            dialect = "postgresql"
        return f"{dialect}+{driver}://{rest}", driver
    if not plus and dialect in _DEFAULT_DRIVERS:
        full = _DEFAULT_DRIVERS[dialect]
        return f"{full}://{rest}", full.partition("+")[2]
    raise ValueError(
        f"DATABASE_URL scheme {scheme!r} names no async driver "
        f"(accepted forms: {_ACCEPTED_FORMS})"
    )


def ensure_sqlite_parent(url: str) -> None:
    """Create the directory a sqlite database is going to live in.

    sqlite refuses to create a missing parent and fails with a bare
    "unable to open database file". The default database now sits in
    data/, which exists in a repo checkout (data/.gitkeep) and is
    created by docker-compose's mount - but not next to a freshly
    unpacked DandelionMusic.exe, where the .env is copied from the
    sample into an empty directory.
    """
    if not url.startswith("sqlite"):
        return
    path = url.partition(":///")[2].partition("?")[0]
    if not path or path == ":memory:":
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


class Formatter(string.Template):
    """Load-time substitution of braced config names, `{NAME}`, only.

    A bare word is never substituted, so a message can name a setting
    without printing its value. `escaped` is required by
    string.Template but never matches: `{{` is not a load-time escape,
    the runtime `.format()` calls own brace escaping.
    """

    delimiter = ""
    pattern = r"""
        (?P<escaped>(?!))
        | \{(?P<braced>[_a-z][_a-z0-9]*)\}
        | (?P<named>(?!))
        | (?P<invalid>(?!))
    """
    format = string.Template.safe_substitute


def load_configs(
    name: str, object_hook: Optional[Callable[[dict], dict]] = None
) -> List[dict]:
    result = []
    for dir_ in CONFIG_DIRS:
        file = os.path.join(dir_, name)
        if os.path.isfile(file):
            with open(file) as f:
                result.append(jsonc.load(f, object_hook=object_hook))
        else:
            result.append({})
    return result


def join_dicts(dicts: Iterable[dict]) -> dict:
    result = {}
    for d in dicts:
        result.update(d)
    return result
