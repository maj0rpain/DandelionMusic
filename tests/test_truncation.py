"""Truncation and TruncationReason: pure values."""

import pytest

from config import config
from musicbot import truncation as truncation_module
from musicbot.truncation import Truncation, TruncationReason
from helpers import imported_names


@pytest.mark.parametrize(
    "total, loaded, expected",
    [
        (5, 5, None),
        (6, 5, Truncation(6, TruncationReason.NO_API)),
        (4, 5, None),
        (None, 5, None),
    ],
)
def test_of_gives_a_truncation_only_when_total_exceeds_loaded(
    total, loaded, expected
):
    assert Truncation.of(total, loaded, TruncationReason.NO_API) == expected


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


def test_module_imports_only_config_enum_and_dataclasses():
    names = imported_names(truncation_module)

    assert {n.split(".")[0] for n in names} == {
        "config",
        "enum",
        "dataclasses",
    }
