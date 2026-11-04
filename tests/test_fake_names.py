"""Fake names are unambiguous: no two shared test-helper modules define the
same name, and no fake in a test module takes a shared fake's name.

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
]

PARAGRAPH_OPENING = "A shared test-helper module is any of"


def _top_level_names(source):
    """name -> line of each top-level class or function in `source`."""
    names = {}
    for node in ast.parse(source).body:
        if isinstance(
            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            names.setdefault(node.name, node.lineno)
    return names


def _shared_fakes(sources):
    """fake name -> shared module, over {module: source}."""
    fakes = {}
    for module, source in sources.items():
        for node in ast.parse(source).body:
            is_class = isinstance(node, ast.ClassDef)
            is_factory = isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith(("make_", "fake_"))
            if (is_class or is_factory) and not node.name.startswith("_"):
                fakes.setdefault(node.name, module)
    return fakes


def duplicate_names(sources):
    """Rule (a): one message per name defined in two shared modules."""
    first = {}
    problems = []
    for module, source in sources.items():
        for name, line in _top_level_names(source).items():
            if name in first:
                problems.append(
                    f"{module}:{line}: {name} is also defined in "
                    f"{first[name]}"
                )
            else:
                first[name] = module
    return problems


def shadowing_names(module_name, source, fakes):
    """Rule (b): one message per class or function, at any nesting level,
    taking a shared fake's name."""
    problems = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.name in fakes
        ):
            problems.append(
                f"{module_name}:{node.lineno}: {node.name} shadows the "
                f"shared fake in {fakes[node.name]}"
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
    """Rule (c): one message per module in one list but not the other."""
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


def test_rule_a_names_both_modules_and_the_line():
    sources = {
        "one": "x = 1\n\nclass Dup:\n    pass\n",
        "two": "def Dup(): ...",
    }
    assert duplicate_names(sources) == ["two:1: Dup is also defined in one"]


def test_rule_b_names_module_line_and_shadowed_shared_module():
    shared = {"shared": "class FakeThing:\n    pass\n"}
    test_src = "def test_x():\n    class FakeThing:\n        pass\n"
    found = shadowing_names("test_x.py", test_src, _shared_fakes(shared))
    assert found == [
        "test_x.py:2: FakeThing shadows the shared fake in shared"
    ]


def test_rule_b_ignores_private_and_non_fake_shared_names():
    shared = {"shared": "def run(): ...\nclass _Hidden: ...\n"}
    test_src = "def run(): ...\nclass _Hidden: ...\n"
    assert shadowing_names("t.py", test_src, _shared_fakes(shared)) == []


def test_rule_b_covers_make_and_fake_functions():
    shared = {"shared": "def make_x(): ...\ndef fake_y(): ...\n"}
    test_src = "def make_x(): ...\ndef fake_y(): ...\n"
    found = shadowing_names("t.py", test_src, _shared_fakes(shared))
    assert [p.split(" ")[1] for p in found] == ["make_x", "fake_y"]


def test_rule_c_names_each_module_in_one_list_only():
    doc = f"{PARAGRAPH_OPENING} `a`, `b` and `c`. Rest.\n\nOther."
    assert list_mismatch(["a", "b", "d"], listed_modules(doc)) == [
        "d is in the guard's list but not in tests/CLAUDE.md",
        "c is in tests/CLAUDE.md but not in the guard's list",
    ]


# The checks on the tree.


def test_no_two_shared_modules_define_the_same_name():
    assert duplicate_names(_shared_sources()) == []


def test_no_test_fake_shadows_a_shared_fake():
    fakes = _shared_fakes(_shared_sources())
    problems = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        problems += shadowing_names(path.name, _read(path), fakes)
    assert problems == []


def test_guard_module_list_matches_the_shared_helpers_paragraph():
    doc = listed_modules(_read(TESTS / "CLAUDE.md"))
    assert doc, "tests/CLAUDE.md has no shared test-helper module paragraph"
    assert list_mismatch(SHARED_MODULES, doc) == []
