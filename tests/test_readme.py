"""The README describes this fork: every command is documented, and
nothing points to upstream or lets a cookies file be committed."""

import re
import subprocess
from pathlib import Path

from musicbot.bot import _help as help_command
from musicbot.commands.developer import Developer
from musicbot.commands.general import General
from musicbot.commands.library import Library
from musicbot.commands.music import Music

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
UPSTREAM = "solaluset/DandelionMusic"


def mentioned(form):
    return re.search(re.escape(form) + r"(?![\w-])", README) is not None


def test_every_command_and_alias_is_documented():
    missing = []
    for command in _commands():
        names = [command.name, *command.aliases]
        missing += [f"d!{n}" for n in names if not mentioned(f"d!{n}")]
        for sub in getattr(command, "commands", ()):
            for n in [sub.name, *sub.aliases]:
                if not any(mentioned(f"d!{p} {n}") for p in names):
                    missing.append(f"d!{names[0]} {n}")
    assert missing == []


def _commands():
    top = [help_command]
    for cog in (Music, General, Library, Developer):
        top += cog(None).get_commands()
    return top


def test_no_upstream_links():
    cookies = (ROOT / "config/cookies/cookies.txt").read_text(encoding="utf-8")
    assert UPSTREAM not in README
    assert UPSTREAM not in cookies


def ignored(path):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
    )
    return result.returncode == 0


def test_new_cookies_file_is_git_ignored():
    assert ignored("config/cookies/my-cookies.txt")


def test_placeholder_cookies_file_stays_tracked():
    assert not ignored("config/cookies/cookies.txt")


def test_dockerignore_excludes_the_cookies_directory():
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "config/cookies/*" in [line.strip() for line in lines]
