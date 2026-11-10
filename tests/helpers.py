"""The suite's generic shared helpers: the bounded `run(coro)`,
`FakeClock`, `plain_song`, `REPO_ROOT`, `COOKIES_HEADING` and the four
import-reading helpers `imported_names`, `imported_roots`,
`type_checking_imports` and `attribute_names`. `dockerfile_commands` (the
Dockerfile reader) and `shell_commands` (the shell-command splitter) are
readers, not import-reading helpers."""

import ast
import asyncio
import re
from pathlib import Path

from musicbot.linkutils import SiteTypes
from musicbot.song import Song

REPO_ROOT = Path(__file__).resolve().parent.parent
COOKIES_HEADING = "Custom Cookies"


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


def _parse_module_source(module):
    """The AST of a module's source file (UTF-8)."""
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def _is_type_checking_block(node) -> bool:
    """Whether `node` is an `if TYPE_CHECKING:` (the bare name only)."""
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    )


def imported_names(
    module, *, skip_type_checking=False, module_level_only=False
) -> set[str]:
    """The names a module's source imports, from its AST.

    Each `import x` gives `x`; each `from m import n` gives the module
    `m` and the pair `m.n` (a relative import's missing module is `""`).
    With `skip_type_checking=True`, every node inside an
    `if TYPE_CHECKING:` block is skipped. With `module_level_only=True`,
    every import inside a function body is skipped.
    """
    tree = _parse_module_source(module)
    skipped = set()
    if skip_type_checking:
        for node in ast.walk(tree):
            if _is_type_checking_block(node):
                skipped.update(id(inner) for inner in ast.walk(node))
    if module_level_only:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
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


def imported_roots(
    module, *, skip_type_checking=False, module_level_only=False
) -> set[str]:
    """The first dotted part of each name `imported_names` returns, with
    the same `skip_type_checking` and `module_level_only` meanings (a
    relative import's root is `""`)."""
    return {
        name.split(".")[0]
        for name in imported_names(
            module,
            skip_type_checking=skip_type_checking,
            module_level_only=module_level_only,
        )
    }


def type_checking_imports(module) -> list[str]:
    """The unparsed statements inside every `if TYPE_CHECKING:` block of
    the module, at any depth, in source order - the blocks
    `imported_names(..., skip_type_checking=True)` skips."""
    blocks = [
        node
        for node in ast.walk(_parse_module_source(module))
        if _is_type_checking_block(node)
    ]
    blocks.sort(key=lambda block: block.lineno)
    return [ast.unparse(s) for block in blocks for s in block.body]


def attribute_names(module) -> set[str]:
    """Every `name.attr` attribute access in the module's source whose
    value is a plain name, as the dotted string `"name.attr"`."""
    return {
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(_parse_module_source(module))
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }


def _join_continuations(text):
    """`text` with each `\\`-newline joined to one space."""
    return text.replace("\\\n", " ")


def shell_commands(text):
    """The shell commands in `text`: continuations joined, split on `&&`
    and newlines, each piece stripped, empty pieces dropped."""
    return [
        c.strip()
        for c in re.split(r"&&|\n", _join_continuations(text))
        if c.strip()
    ]


def dockerfile_commands(text=None):
    """The instructions of `text` (the repo Dockerfile by default), one
    string each: continuations joined, blank and `#` lines dropped, the
    keyword upper-cased and followed by exactly one space and the
    arguments unchanged (no trailing space without arguments)."""
    if text is None:
        text = (REPO_ROOT / "Dockerfile").read_text()
    commands = []
    for line in _join_continuations(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, *args = re.split(r"\s+", line, maxsplit=1)
        commands.append(" ".join([keyword.upper(), *args]))
    return commands


class RedirectFollower:
    """A recording async stand-in for `loader.resolve_redirects`: returns
    `result`, or raises it when it is an exception."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def fake_redirect_follower(monkeypatch, result):
    """Patches `loader.resolve_redirects` with a `RedirectFollower`."""
    follower = RedirectFollower(result)
    monkeypatch.setattr("musicbot.loader.resolve_redirects", follower)
    return follower


def assert_logged_with_traceback(err, message):
    """Asserts `message` appears in `err` and precedes its first
    `Traceback`: a failure-log line followed by the traceback it logged."""
    at = err.find(message)
    trace = err.find("Traceback")
    assert at >= 0, f"{message!r} not in the log: {err!r}"
    assert trace >= 0, f"no Traceback in the log: {err!r}"
    assert at < trace, f"{message!r} follows the first Traceback: {err!r}"
