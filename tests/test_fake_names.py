"""Shared names are unambiguous: no two shared test-helper modules define
the same top-level class, function or module-level variable, and no class,
function or module-level variable in a test module takes a public
top-level name of a shared module.

Source scans over `ast`; nothing under test is imported.
"""

import ast
import re
from pathlib import Path

TESTS = Path(__file__).parent

SHARED_MODULES = [
    "discord_fakes",
    "helpers",
    "developer_fakes",
    "setting_fakes",
    "controller_seam",
    "controller_helpers",
    "db_seam",
    "library_fakes",
]

PARAGRAPH_OPENING = "A shared test-helper module is any of"


DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _bound_names(target):
    """The names a single assignment target binds."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _bound_names(element)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def _defined_names(nodes):
    """(name, line) of each name the nodes define: a class or function, or
    a name an assignment or annotated assignment binds. Imports, augmented
    assignments and attribute or subscript targets define nothing."""
    for node in nodes:
        if isinstance(node, DEFINITIONS):
            yield node.name, node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _bound_names(target):
                    yield name, node.lineno
        elif isinstance(node, ast.AnnAssign):
            for name in _bound_names(node.target):
                yield name, node.lineno


def _shared_names(sources):
    """public name -> shared module, over {module: source}."""
    names = {}
    for module, source in sources.items():
        for name, _ in _defined_names(ast.parse(source).body):
            if not name.startswith("_"):
                names.setdefault(name, module)
    return names


def duplicate_names(sources):
    """One message per top-level name defined in two shared modules."""
    first = {}
    problems = []
    for module, source in sources.items():
        for name, line in _defined_names(ast.parse(source).body):
            if name in first and first[name] != module:
                problems.append(
                    f"{module}:{line}: {name} is also defined in "
                    f"{first[name]}"
                )
            else:
                first.setdefault(name, module)
    return problems


def shadowing_names(module_name, source, shared):
    """One message per class or function at any nesting level, or
    module-level variable, taking a shared module's public name."""
    tree = ast.parse(source)
    nodes = [n for n in ast.walk(tree) if isinstance(n, DEFINITIONS)]
    nodes += [n for n in tree.body if not isinstance(n, DEFINITIONS)]
    problems = []
    for name, line in sorted(_defined_names(nodes), key=lambda pair: pair[1]):
        if name in shared:
            problems.append(
                f"{module_name}:{line}: {name} shadows the shared name "
                f"in {shared[name]}"
            )
    return problems


def listed_modules(claude_md):
    """The backticked names in the first sentence of the paragraph opening
    PARAGRAPH_OPENING."""
    for paragraph in claude_md.split("\n\n"):
        if paragraph.startswith(PARAGRAPH_OPENING):
            sentence = paragraph.split(". ")[0]
            return re.findall(r"`([^`]+)`", sentence)
    return []


def list_mismatch(guard_list, doc_list):
    """One message per module in one list but not the other."""
    problems = [
        f"{m} is in the guard's list but not in tests/CLAUDE.md"
        for m in guard_list
        if m not in doc_list
    ]
    problems += [
        f"{m} is in tests/CLAUDE.md but not in the guard's list"
        for m in doc_list
        if m not in guard_list
    ]
    return problems


def _read(path):
    return path.read_text(encoding="utf-8")


def _shared_sources():
    return {m: _read(TESTS / f"{m}.py") for m in SHARED_MODULES}


# The checks on fixture sources.


def test_duplicate_names_reports_both_modules_and_the_line():
    sources = {
        "one": "x = 1\n\nclass Dup:\n    pass\n",
        "two": "def Dup(): ...",
    }
    assert duplicate_names(sources) == ["two:1: Dup is also defined in one"]


def test_duplicate_names_reports_a_module_level_variable():
    sources = {"one": "BUTTONS = 1\n", "two": "\nBUTTONS = 2\n"}
    assert duplicate_names(sources) == [
        "two:2: BUTTONS is also defined in one"
    ]


def test_duplicate_names_ignores_imports():
    sources = {"one": "import discord\n", "two": "import discord\n"}
    assert duplicate_names(sources) == []


def test_shadowing_names_reports_module_line_and_shared_module():
    shared = {"shared": "class FakeThing:\n    pass\n"}
    test_src = "def test_x():\n    class FakeThing:\n        pass\n"
    found = shadowing_names("test_x.py", test_src, _shared_names(shared))
    assert found == [
        "test_x.py:2: FakeThing shadows the shared name in shared"
    ]


def test_shadowing_names_ignores_private_names():
    shared = {"shared": "class _Hidden: ...\n_X = 1\n"}
    test_src = "class _Hidden: ...\n_X = 2\n"
    assert shadowing_names("t.py", test_src, _shared_names(shared)) == []


def test_shadowing_names_reports_a_non_fake_helper():
    shared = {"shared": "def run(): ...\n"}
    found = shadowing_names("t.py", "def run(): ...\n", _shared_names(shared))
    assert found == ["t.py:1: run shadows the shared name in shared"]


def test_shadowing_names_reports_a_module_level_variable():
    shared = {"shared": "PLAYER_BUTTONS = 1\n"}
    found = shadowing_names(
        "t.py", "PLAYER_BUTTONS = 2\n", _shared_names(shared)
    )
    assert found == [
        "t.py:1: PLAYER_BUTTONS shadows the shared name in shared"
    ]


def test_shadowing_names_ignores_a_local_variable():
    shared = {"shared": "run = 1\n"}
    test_src = "def test_x():\n    run = 2\n"
    assert shadowing_names("t.py", test_src, _shared_names(shared)) == []


def test_shadowing_names_ignores_assignments_under_top_level_if():
    shared = {"shared": "run = 1\n"}
    test_src = "if True:\n    run = 2\n"
    assert shadowing_names("t.py", test_src, _shared_names(shared)) == []


def test_shared_names_count_every_definition_form():
    source = (
        "async def a(): ...\n"
        "B: int = 1\n"
        "C: int\n"
        "D, (E, *F) = 1, (2, 3)\n"
        "G = H = 1\n"
        "I = 0\n"
        "I += 1\n"
        "J.attr = 1\n"
        "K[0] = 1\n"
        "import L\n"
        "if True:\n"
        "    M = 1\n"
        "try:\n"
        "    N = 1\n"
        "except Exception:\n"
        "    pass\n"
    )
    assert list(_shared_names({"m": source})) == [
        "a",
        "B",
        "C",
        "D",
        "E",
        "F",
        "G",
        "H",
        "I",
    ]


def test_list_mismatch_reports_each_module_in_one_list_only():
    doc = f"{PARAGRAPH_OPENING} `a`, `b` and `c`. Rest.\n\nOther."
    assert list_mismatch(["a", "b", "d"], listed_modules(doc)) == [
        "d is in the guard's list but not in tests/CLAUDE.md",
        "c is in tests/CLAUDE.md but not in the guard's list",
    ]


# The checks on the tree.


def test_no_two_shared_modules_define_the_same_name():
    assert duplicate_names(_shared_sources()) == []


def test_no_test_module_shadows_a_shared_name():
    shared = _shared_names(_shared_sources())
    problems = []
    for path in sorted(TESTS.glob("test_*.py")):
        problems += shadowing_names(path.name, _read(path), shared)
    assert problems == []


def test_guard_module_list_matches_the_shared_helpers_paragraph():
    doc = listed_modules(_read(TESTS / "CLAUDE.md"))
    assert doc, "tests/CLAUDE.md has no shared test-helper module paragraph"
    assert list_mismatch(SHARED_MODULES, doc) == []
