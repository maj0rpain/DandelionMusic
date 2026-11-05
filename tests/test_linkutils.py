import ast
from pathlib import Path

from musicbot import linkutils


def test_linkutils_does_not_import_the_loader():
    names = set()
    for node in ast.walk(ast.parse(Path(linkutils.__file__).read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    assert "musicbot.loader" not in names
