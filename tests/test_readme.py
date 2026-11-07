"""The README describes this fork: every command is documented, and
nothing points to upstream or lets a cookies file be committed."""

import re
import subprocess
from pathlib import Path

import jsonc

from config.config import parse_env_file
from musicbot.bot import _help as help_command
from musicbot.commands.developer import Developer
from musicbot.commands.general import General
from musicbot.commands.library import Library
from musicbot.commands.music import Music
from musicbot.settings import SETTINGS, SettingsEmbed

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
UPSTREAM = "solaluset/DandelionMusic"
COOKIES_HEADING = "Custom Cookies"


def _commands():
    top = [help_command]
    for cog in (Music, General, Library, Developer):
        top += cog(None).get_commands()
    return top


def _mentioned(form):
    return re.search(re.escape(form) + r"(?![\w-])", README) is not None


def _git_ignores(path):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _stale_mentions(text):
    """Every `d!<group> <word>` in text whose group is a registered
    command group (name or alias) but whose word is not one of that
    group's subcommands or aliases."""
    groups = {}
    for command in _commands():
        if hasattr(command, "commands"):
            subs = {
                n for sub in command.commands for n in [sub.name, *sub.aliases]
            }
            for name in [command.name, *command.aliases]:
                groups[name] = subs
    return [
        f"d!{group} {word}"
        for group, word in re.findall(r"d!([\w-]+) ([\w-]+)", text)
        if group in groups and word not in groups[group]
    ]


def _setting_section():
    start = README.index("d!settings / d!setting / d!set")
    return README[start : README.index("d!connect / d!c / d!cc", start)]


def test_every_command_and_alias_is_documented():
    missing = []
    for command in _commands():
        names = [command.name, *command.aliases]
        missing += [f"d!{n}" for n in names if not _mentioned(f"d!{n}")]
        for sub in getattr(command, "commands", ()):
            for n in [sub.name, *sub.aliases]:
                if not any(_mentioned(f"d!{p} {n}") for p in names):
                    missing.append(f"d!{names[0]} {n}")
    assert missing == []


def test_stale_subcommand_mentions_are_reported():
    planted = (
        "d!setting no_such_setting d!pl no_such_sub d!gw no_such_sub "
        "d!loop single d!setting dj_role"
    )
    assert _stale_mentions(planted) == [
        "d!setting no_such_setting",
        "d!pl no_such_sub",
        "d!gw no_such_sub",
    ]


def test_readme_names_no_stale_subcommand():
    assert _stale_mentions(README) == []


def test_setting_section_says_what_reset_means():
    unset = {name for name, d in SETTINGS.items() if d.default is None}
    assert unset == {
        "command_channel",
        "start_voice_channel",
        "dj_role",
        "button_emote",
    }
    section = _setting_section()
    assert SettingsEmbed.FIELD_EMPTY in section
    lines = section.splitlines()
    omit = next(i for i, x in enumerate(lines) if "Omit the value" in x)
    assert any(x.startswith("* Arguments") for x in lines[:omit])
    assert any(x.startswith("* Example") for x in lines[omit:])


def test_env_sample_does_not_point_cookie_path_at_the_placeholder():
    path = ROOT / ".env.sample"
    text = path.read_text(encoding="utf-8")
    assert "COOKIE_PATH" not in parse_env_file(str(path))
    assert "COOKIE_PATH" in text
    paths = set(re.findall(r"config/cookies/[\w.-]+", text))
    assert paths - {"config/cookies/cookies.txt"}
    assert COOKIES_HEADING in text


def test_songinfo_error_points_at_custom_cookies_not_a_wiki():
    strings = jsonc.loads((ROOT / "config/en.json").read_text("utf-8"))
    assert COOKIES_HEADING in strings["SONGINFO_ERROR"]
    assert COOKIES_HEADING in README
    assert [k for k, v in strings.items() if "wiki" in str(v)] == []


def test_no_upstream_links():
    cookies = (ROOT / "config/cookies/cookies.txt").read_text(encoding="utf-8")
    assert UPSTREAM not in README
    assert UPSTREAM not in cookies


def test_new_cookies_file_is_git_ignored():
    assert _git_ignores("config/cookies/my-cookies.txt")


def test_placeholder_cookies_file_stays_tracked():
    assert not _git_ignores("config/cookies/cookies.txt")


def test_dockerignore_excludes_the_cookies_directory():
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "config/cookies/*" in [line.strip() for line in lines]
