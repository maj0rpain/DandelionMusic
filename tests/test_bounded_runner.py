"""Guard: the suite's one bounded coroutine runner is ``helpers.run``.

Scans every test module (but ``helpers.py``) for a bare ``asyncio.run``,
``asyncio.runners.run``, ``asyncio.Runner`` construction or
``run_until_complete`` call without an ``# unbounded: <reason>`` marker
on the call's start line, and for a module-level ``run`` definition.
Constructing a ``Runner`` is the violation, wherever the instance goes.
"""

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

MARKER = re.compile(r"#\s*unbounded:\s*\S")
TESTS_DIR = Path(__file__).parent
SUFFIX = "without '# unbounded: <reason>'"

RUN = "asyncio.run()"
RUNNERS_RUN = "asyncio.runners.run()"
RUNNER = "asyncio.Runner()"
CALLEES = {
    "asyncio.run": RUN,
    "asyncio.runners.run": RUNNERS_RUN,
    "asyncio.Runner": RUNNER,
    "asyncio.runners.Runner": RUNNER,
}


@dataclass(frozen=True, order=True)
class Violation:
    name: str
    line: int
    message: str

    def __str__(self):
        return f"{self.name}:{self.line}: {self.message}"


def _marked_lines(source):
    return {
        tok.start[0]
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT and MARKER.search(tok.string)
    }


def _asyncio_names(tree):
    """Map each local name bound to asyncio (or a part of it) to the
    dotted path it stands for, e.g. ``{"a": "asyncio", "R":
    "asyncio.Runner"}``."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "asyncio":
                    names[a.asname or "asyncio"] = "asyncio"
                elif a.name.startswith("asyncio."):
                    # a plain ``import asyncio.<sub>`` binds ``asyncio``
                    if a.asname:
                        names[a.asname] = a.name
                    else:
                        names["asyncio"] = "asyncio"
        elif isinstance(node, ast.ImportFrom) and node.module in (
            "asyncio",
            "asyncio.runners",
        ):
            for a in node.names:
                names[a.asname or a.name] = f"{node.module}.{a.name}"
    return names


def _dotted(func, names):
    """The resolved dotted path of a callee, or None."""
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name) or func.id not in names:
        return None
    return ".".join([names[func.id], *reversed(parts)])


def scan(source, name):
    """Return the sorted ``Violation`` values of ``source``."""
    tree = ast.parse(source)
    marked = _marked_lines(source)
    names = _asyncio_names(tree)
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "run":
                found.append(
                    Violation(name, node.lineno, "module-level run() defined")
                )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        what = CALLEES.get(_dotted(func, names))
        if what is None and (
            isinstance(func, ast.Attribute)
            and func.attr == "run_until_complete"
        ):
            what = "run_until_complete()"
        if what is None or node.lineno in marked:
            continue
        found.append(Violation(name, node.lineno, f"{what} {SUFFIX}"))
    return sorted(found)


def test_bare_asyncio_run_is_flagged():
    src = "import asyncio\nasyncio.run(main())\n"
    assert _lines(scan(src, "t.py")) == [2]


def test_from_import_run_is_flagged():
    src = "from asyncio import run\nrun(main())\n"
    assert _lines(scan(src, "t.py")) == [2]


def test_run_until_complete_is_flagged():
    src = "loop.run_until_complete(main())\n"
    assert _lines(scan(src, "t.py")) == [1]


def test_aliased_module_is_flagged():
    src = "import asyncio as aio\naio.run(main())\n"
    assert _lines(scan(src, "t.py")) == [2]


def test_aliased_run_is_flagged():
    src = "from asyncio import run as r\nr(main())\n"
    assert _lines(scan(src, "t.py")) == [2]


def test_marked_line_is_accepted():
    src = "import asyncio\nasyncio.run(m())  # unbounded: real child\n"
    assert scan(src, "t.py") == []


def test_empty_reason_marker_is_flagged():
    src = "import asyncio\nasyncio.run(m())  # unbounded:   \n"
    assert _lines(scan(src, "t.py")) == [2]


def test_prose_mentions_are_ignored():
    src = (
        '"""Runs through asyncio.run() and run_until_complete()."""\n'
        "# asyncio.run(x)\n"
    )
    assert scan(src, "t.py") == []


def test_module_level_def_run_is_flagged():
    assert _lines(scan("def run(c):\n    pass\n", "t.py")) == [1]


def test_module_level_async_def_run_is_flagged():
    assert _lines(scan("async def run(c):\n    pass\n", "t.py")) == [1]


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
    assert _lines(scan(src, "t.py")) == [2]


def test_marker_text_inside_a_string_is_not_a_marker():
    src = 'import asyncio\nasyncio.run(f("# unbounded: x"))\n'
    assert _lines(scan(src, "t.py")) == [2]


def test_violation_names_file_and_line():
    (v,) = scan("loop.run_until_complete(m())\n", "t.py")
    assert str(v) == (
        "t.py:1: run_until_complete() without '# unbounded: <reason>'"
    )


def _lines(violations):
    return [v.line for v in violations]


def _check(src, line, what):
    (v,) = scan(src, "t.py")
    assert v.line == line
    assert str(v) == f"t.py:{line}: {what} {SUFFIX}"


def test_runner_forms_are_flagged():
    cases = [
        ("import asyncio\nasyncio.Runner()\n", RUNNER),
        ("import asyncio as a\na.Runner()\n", RUNNER),
        ("from asyncio import Runner\nRunner()\n", RUNNER),
        ("from asyncio import Runner as R\nR()\n", RUNNER),
        ("from asyncio.runners import Runner as R\nR()\n", RUNNER),
        ("import asyncio.runners\nasyncio.runners.Runner()\n", RUNNER),
        ("import asyncio.runners as x\nx.Runner()\n", RUNNER),
        ("from asyncio import runners as x\nx.Runner()\n", RUNNER),
        ("import asyncio\nasyncio.runners.Runner()\n", RUNNER),
    ]
    for src, what in cases:
        _check(src, 2, what)


def test_runners_run_forms_are_flagged():
    cases = [
        ("import asyncio.runners\nasyncio.runners.run(c)\n", RUNNERS_RUN),
        ("import asyncio.runners as x\nx.run(c)\n", RUNNERS_RUN),
        ("from asyncio import runners as x\nx.run(c)\n", RUNNERS_RUN),
        ("from asyncio.runners import run as r\nr(c)\n", RUNNERS_RUN),
        ("from asyncio.runners import run\nrun(c)\n", RUNNERS_RUN),
    ]
    for src, what in cases:
        _check(src, 2, what)


def test_plain_import_of_runners_binds_asyncio():
    _check("import asyncio.runners\nasyncio.run(c)\n", 2, RUN)


def test_plain_import_of_any_submodule_binds_asyncio():
    _check("import asyncio.events\nasyncio.run(c)\n", 2, RUN)
    _check("import asyncio.subprocess\nasyncio.Runner()\n", 2, RUNNER)


def test_inline_runner_run_is_flagged_at_construction():
    src = "import asyncio\nasyncio.Runner().run(c)\n"
    _check(src, 2, RUNNER)


def test_with_runner_is_flagged_at_construction():
    src = "import asyncio\nwith asyncio.Runner() as r:\n    r.run(c)\n"
    _check(src, 2, RUNNER)


def test_assigned_runner_is_flagged_at_construction():
    src = "import asyncio\nr = asyncio.Runner()\nr.run(c)\n"
    _check(src, 2, RUNNER)


def test_marked_runner_is_accepted():
    src = "import asyncio\nasyncio.Runner()  # unbounded: real child\n"
    assert scan(src, "t.py") == []


def test_unrelated_runner_is_not_flagged():
    assert scan("Runner()\n", "t.py") == []
    assert scan("from other import Runner\nRunner()\n", "t.py") == []
    assert scan("obj.Runner()\n", "t.py") == []
    src = "import asyncio\nobj.Runner()\nobj.runners.run(c)\n"
    assert scan(src, "t.py") == []


def test_violations_order_by_line_as_a_number():
    src = "import asyncio\nasyncio.run(a)\n" + "\n" * 7 + "asyncio.run(b)\n"
    assert _lines(scan(src, "t.py")) == [2, 10]


def test_violations_at_one_line_order_by_message():
    src = "import asyncio\nasyncio.run(asyncio.Runner())\n"
    assert [v.message.split(" ")[0] for v in scan(src, "t.py")] == [
        "asyncio.Runner()",
        "asyncio.run()",
    ]


def test_violations_order_name_first():
    a = Violation("a.py", 9, "z")
    b = Violation("b.py", 1, "a")
    assert sorted([b, a]) == [a, b]


def test_tests_tree_has_no_unbounded_runners():
    violations = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == "helpers.py":
            continue
        violations += scan(
            path.read_text(encoding="utf-8"), str(path.relative_to(TESTS_DIR))
        )
    assert [str(v) for v in violations] == []
