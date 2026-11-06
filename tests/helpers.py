"""The suite's generic shared helpers: the bounded `run(coro)`,
`FakeClock` and the import-reading helpers `imported_names`,
`imported_roots`, `type_checking_imports` and `attribute_names`."""

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


def imported_names(module, *, skip_type_checking=False) -> set[str]:
    """The names a module's source imports, from its AST.

    Each `import x` gives `x`; each `from m import n` gives the module
    `m` and the pair `m.n` (a relative import's missing module is `""`).
    With `skip_type_checking=True`, every node inside an
    `if TYPE_CHECKING:` block is skipped.
    """
    tree = _parse_module_source(module)
    skipped = set()
    if skip_type_checking:
        for node in ast.walk(tree):
            if _is_type_checking_block(node):
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


def imported_roots(module, *, skip_type_checking=False) -> set[str]:
    """The first dotted part of each name `imported_names` returns, with
    the same `skip_type_checking` meaning (a relative import's root is
    `""`)."""
    return {
        name.split(".")[0]
        for name in imported_names(
            module, skip_type_checking=skip_type_checking
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


def dockerfile_commands():
    """The repo Dockerfile's instructions, one string each: continuation
    lines joined, blank lines and `#` comment lines dropped."""
    text = (Path(__file__).parent.parent / "Dockerfile").read_text()
    return [
        c.strip()
        for c in text.replace("\\\n", " ").splitlines()
        if c.strip() and not c.strip().startswith("#")
    ]
