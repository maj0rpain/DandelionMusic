"""Guard: the suite's one bounded coroutine runner is ``helpers.run``.

Scans every test module (but ``helpers.py``) for a bare ``asyncio.run``
or ``run_until_complete`` call without an ``# unbounded: <reason>``
marker on the call's start line, and for a module-level ``run``
definition.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

MARKER = re.compile(r"#\s*unbounded:\s*\S")
TESTS_DIR = Path(__file__).parent


def _marked_lines(source):
    return {
        tok.start[0]
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT and MARKER.search(tok.string)
    }


def _asyncio_names(tree):
    modules, runners = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                a.asname or a.name for a in node.names if a.name == "asyncio"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "asyncio":
            runners.update(
                a.asname or a.name for a in node.names if a.name == "run"
            )
    return modules, runners


def scan(source, name):
    """Return the violations of ``source`` as ``name:line: message``."""
    tree = ast.parse(source)
    marked = _marked_lines(source)
    modules, runners = _asyncio_names(tree)
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "run":
                found.append(
                    f"{name}:{node.lineno}: module-level run() defined"
                )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            bare = (
                func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id in modules
            )
            if not (bare or func.attr == "run_until_complete"):
                continue
            what = f"{func.attr}()"
        elif isinstance(func, ast.Name) and func.id in runners:
            what = "asyncio.run()"
        else:
            continue
        if node.lineno not in marked:
            found.append(
                f"{name}:{node.lineno}: {what} without"
                " '# unbounded: <reason>'"
            )
    return sorted(found)


def lines(violations):
    return [int(v.split(":")[1]) for v in violations]


def test_bare_asyncio_run_is_flagged():
    src = "import asyncio\nasyncio.run(main())\n"
    assert lines(scan(src, "t.py")) == [2]


def test_from_import_run_is_flagged():
    src = "from asyncio import run\nrun(main())\n"
    assert lines(scan(src, "t.py")) == [2]


def test_run_until_complete_is_flagged():
    src = "loop.run_until_complete(main())\n"
    assert lines(scan(src, "t.py")) == [1]


def test_aliased_module_is_flagged():
    src = "import asyncio as aio\naio.run(main())\n"
    assert lines(scan(src, "t.py")) == [2]


def test_aliased_run_is_flagged():
    src = "from asyncio import run as r\nr(main())\n"
    assert lines(scan(src, "t.py")) == [2]


def test_marked_line_is_accepted():
    src = "import asyncio\nasyncio.run(m())  # unbounded: real child\n"
    assert scan(src, "t.py") == []


def test_empty_reason_marker_is_flagged():
    src = "import asyncio\nasyncio.run(m())  # unbounded:   \n"
    assert lines(scan(src, "t.py")) == [2]


def test_prose_mentions_are_ignored():
    src = (
        '"""Runs through asyncio.run() and run_until_complete()."""\n'
        "# asyncio.run(x)\n"
    )
    assert scan(src, "t.py") == []


def test_module_level_def_run_is_flagged():
    assert lines(scan("def run(c):\n    pass\n", "t.py")) == [1]


def test_module_level_async_def_run_is_flagged():
    assert lines(scan("async def run(c):\n    pass\n", "t.py")) == [1]


def test_nested_and_method_run_are_not_flagged():
    src = (
        "def outer():\n"
        "    def run():\n"
        "        pass\n"
        "class C:\n"
        "    def run(self):\n"
        "        pass\n"
    )
    assert scan(src, "t.py") == []


def test_multiline_call_marked_on_first_line_is_accepted():
    src = (
        "import asyncio\n"
        "asyncio.run(  # unbounded: real child\n"
        "    m()\n"
        ")\n"
    )
    assert scan(src, "t.py") == []


def test_multiline_call_marked_only_on_later_line_is_flagged():
    src = (
        "import asyncio\n"
        "asyncio.run(\n"
        "    m()  # unbounded: real child\n"
        ")\n"
    )
    assert lines(scan(src, "t.py")) == [2]


def test_marker_text_inside_a_string_is_not_a_marker():
    src = 'import asyncio\nasyncio.run(f("# unbounded: x"))\n'
    assert lines(scan(src, "t.py")) == [2]


def test_violation_names_file_and_line():
    assert scan("loop.run_until_complete(m())\n", "t.py")[0].startswith(
        "t.py:1:"
    )


def test_tests_tree_has_no_unbounded_runners():
    violations = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == "helpers.py":
            continue
        violations += scan(
            path.read_text(encoding="utf-8"), str(path.relative_to(TESTS_DIR))
        )
    assert violations == []
