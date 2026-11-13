"""Pins whose versions must move together, read from the repo's files."""

import tomllib

from packaging.version import Version

from helpers import REPO_ROOT


def _locked(lock):
    return {p["name"]: Version(p["version"]) for p in lock["package"]}


def _davey_too_new(lock):
    """True when the lock pins discord-py 2.7.1 with davey 0.2.0 or later."""
    locked = _locked(lock)
    return locked.get("discord-py") == Version("2.7.1") and locked.get(
        "davey", Version("0")
    ) >= Version("0.2.0")


def _planted(davey):
    return {
        "package": [
            {"name": "discord-py", "version": "2.7.1"},
            {"name": "davey", "version": davey},
        ]
    }


def test_real_lock_pins_davey_0_1_with_discord_py_2_7_1():
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    locked = _locked(lock)
    assert locked["discord-py"] == Version("2.7.1"), (
        "discord-py moved off 2.7.1: revisit the davey pin in "
        "pyproject.toml and the davey ignore in .github/dependabot.yml"
    )
    assert Version("0.1.7") <= locked["davey"] < Version("0.2.0")


def test_davey_0_2_with_discord_py_2_7_1_is_too_new():
    assert _davey_too_new(_planted("0.2.0"))


def test_davey_0_1_7_with_discord_py_2_7_1_is_not_too_new():
    assert not _davey_too_new(_planted("0.1.7"))


def test_pyproject_declares_davey_0_1_7():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert "davey~=0.1.7" in project["project"]["dependencies"]
