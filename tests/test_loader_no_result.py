"""loader._load_song's no-result failures: which message comes back when
a search, a link or a playlist yields nothing."""

import pytest

from config import config
from musicbot import loader
from musicbot.linkutils import SiteTypes
from musicbot.loader import SongError


def _no_result_load(monkeypatch, track, search=None):
    monkeypatch.setattr(loader, "_search_youtube", lambda *a, **k: search)
    empty = {"entries": []}
    monkeypatch.setattr(loader, "extract_info", lambda *a, **k: empty)
    with pytest.raises(SongError) as info:
        loader._load_song(track)
    return info.value.args[0]


def test_empty_search_is_search_no_results(monkeypatch):
    assert _no_result_load(monkeypatch, "some title", []) == (
        config.SEARCH_NO_RESULTS
    )


def test_failed_search_is_songinfo_error_not_no_results(monkeypatch):
    message = _no_result_load(monkeypatch, "some title", None)
    assert message == config.SONGINFO_ERROR
    assert message != config.SEARCH_NO_RESULTS


def test_unknown_host_is_unsupported(monkeypatch):
    monkeypatch.setattr(loader, "identify_url", lambda t: SiteTypes.UNKNOWN)
    assert _no_result_load(monkeypatch, "https://x.invalid/a") == (
        config.SONGINFO_UNSUPPORTED
    )


def test_playlist_with_no_entries_is_songinfo_error(monkeypatch):
    monkeypatch.setattr(loader, "identify_url", lambda t: SiteTypes.YT_DLP)
    assert _no_result_load(monkeypatch, "https://x.invalid/pl") == (
        config.SONGINFO_ERROR
    )
