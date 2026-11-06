"""The suite's generic shared helpers: the bounded `run(coro)`,
`FakeClock` and `imported_names(module)`."""

import ast
import asyncio
from pathlib import Path

from musicbot.linkutils import SiteTypes
from musicbot.song import Song


def run(coro):
    """Run a coroutine to completion, bounded by a 5 s timeout.

    The tests are synchronous functions running their coroutine through
    asyncio.run(): the dev group pins pytest with no asyncio_mode and no
    pytest-asyncio, so an async test would be collected, skipped with a
    warning, and silently pass.
    """
    # bounded, so a waiter left unresolved fails the test, not the run
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


def plain_song(title="a"):
    """A plain YT-DLP `Song` with a title."""
    return Song(
        SiteTypes.YT_DLP, f"https://example.invalid/{title}", title=title
    )


class FakeClock:
    """A controllable clock: calling it returns `now`."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def imported_names(module, *, skip_type_checking=False) -> set[str]:
    """The names a module's source imports, from its AST.

    Each `import x` gives `x`; each `from m import n` gives the module
    `m` and the pair `m.n` (a relative import's missing module is `""`).
    With `skip_type_checking=True`, every node inside an
    `if TYPE_CHECKING:` block is skipped.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    skipped = set()
    if skip_type_checking:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.If)
                and getattr(node.test, "id", None) == "TYPE_CHECKING"
            ):
                skipped.update(id(inner) for inner in ast.walk(node))
    names = set()
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{a.name}" for a in node.names)
    return names
