"""Truncation and TruncationReason: pure values."""

import ast
import pickle
from pathlib import Path

import pytest

from config import config
from musicbot import truncation as truncation_module
from musicbot.truncation import Truncation, TruncationReason


@pytest.mark.parametrize(
    "total, loaded, expected", [(5, 5, False), (6, 5, True), (4, 5, False)]
)
def test_cuts_only_when_total_exceeds_loaded(total, loaded, expected):
    assert Truncation(total, TruncationReason.NO_API).cuts(loaded) is expected


def test_notice_holds_counts_and_reason_text_not_the_key():
    text = Truncation(50, TruncationReason.NO_API).notice(30)

    assert "30" in text and "50" in text
    assert config.SPOTIFY_TRUNCATED_NO_API in text
    assert "SPOTIFY_TRUNCATED_NO_API" not in text


@pytest.mark.parametrize("reason", list(TruncationReason))
def test_every_reason_resolves_to_a_message(reason):
    assert reason.text


def test_text_and_notice_follow_messages_changed_after_import(monkeypatch):
    monkeypatch.setattr(config, "SPOTIFY_TRUNCATED_NO_API", "NEW REASON")
    monkeypatch.setattr(
        config, "SPOTIFY_PLAYLIST_TRUNCATED", "{queued}/{total} {reason}"
    )
    value = Truncation(9, TruncationReason.NO_API)

    assert TruncationReason.NO_API.text == "NEW REASON"
    assert value.notice(2) == "2/9 NEW REASON"


def test_truncation_survives_pickling():
    value = Truncation(7, TruncationReason.API_ERROR)

    assert pickle.loads(pickle.dumps(value)) == value


def test_module_imports_only_config_enum_and_dataclasses():
    tree = ast.parse(Path(truncation_module.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)

    assert names == {"config", "enum", "dataclasses"}
