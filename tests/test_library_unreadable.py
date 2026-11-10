"""The index walk skips any entry it cannot stat, and startup survives
a failed index build."""

from pathlib import Path

import discord
import pytest

from config import config
from musicbot import library
from musicbot.bot import MusicBot
from helpers import run


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    for artist, album, track in [
        ("A1", "Al1", "t1.flac"),
        ("A1", "Al2", "t2.flac"),
        ("A2", "Al3", "t3.flac"),
        ("A1", "Al1", "t4.flac"),
    ]:
        (root / artist / album).mkdir(parents=True, exist_ok=True)
        (root / artist / album / track).write_bytes(b"")
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", str(root))
    monkeypatch.setattr(config, "LIBRARY_EXTENSIONS", (".flac",))
    yield root
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", "")
    library.build_index()


def _denied_error(path):
    return PermissionError(13, "Permission denied", str(path))


def denied(path):
    return str(_denied_error(path))


def assert_skipped(err, path):
    assert f"library: skipping unreadable {path}: {denied(path)}" in err


def fail_path_call(monkeypatch, name, bad):
    original = getattr(Path, name)

    def failing_call(self, *a, **kw):
        if self == bad:
            raise _denied_error(self)
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, name, failing_call)


def test_unreadable_artist_dir_is_skipped_and_sibling_indexed(
    tree, monkeypatch, capsys
):
    fail_path_call(monkeypatch, "is_dir", tree / "A1")
    index = library.build_index()
    assert list(index) == ["A2"]
    assert_skipped(capsys.readouterr().err, tree / "A1")


def test_unreadable_album_dir_is_skipped_and_sibling_indexed(
    tree, monkeypatch, capsys
):
    bad = tree / "A1" / "Al1"
    fail_path_call(monkeypatch, "is_dir", bad)
    index = library.build_index()
    assert sorted(index["A1"]) == ["Al2"]
    assert_skipped(capsys.readouterr().err, bad)


def test_unreadable_file_is_skipped_and_sibling_track_indexed(
    tree, monkeypatch, capsys
):
    bad = tree / "A1" / "Al1" / "t1.flac"
    fail_path_call(monkeypatch, "is_file", bad)
    index = library.build_index()
    assert [s.filename for s in index["A1"]["Al1"]] == ["t4.flac"]
    assert_skipped(capsys.readouterr().err, bad)


def test_unreadable_root_gives_an_empty_index(tree, monkeypatch, capsys):
    fail_path_call(monkeypatch, "is_dir", tree)
    assert library.build_index() == {}
    assert_skipped(capsys.readouterr().err, tree)


def test_unlistable_artist_dir_is_skipped_and_sibling_indexed(
    tree, monkeypatch, capsys
):
    fail_path_call(monkeypatch, "iterdir", tree / "A1")
    index = library.build_index()
    assert list(index) == ["A2"]
    assert_skipped(capsys.readouterr().err, tree / "A1")


def test_setup_hook_survives_a_failed_index_build(monkeypatch, capsys):
    bot = MusicBot(
        ["ext.a", "ext.b"],
        command_prefix="d!",
        intents=discord.Intents.default(),
    )
    loaded, synced = [], []

    async def broken():
        raise RuntimeError("index exploded")

    async def load_extension(name):
        loaded.append(name)

    async def sync():
        synced.append(True)

    monkeypatch.setattr(config, "ENABLE_LOCAL_LIBRARY", True)
    monkeypatch.setattr(config, "ENABLE_SLASH_COMMANDS", True)
    monkeypatch.setattr(library, "build_index_async", broken)
    monkeypatch.setattr(bot, "load_extension", load_extension)
    monkeypatch.setattr(bot.tree, "sync", sync)
    run(bot.setup_hook())
    err = capsys.readouterr().err
    assert (
        loaded,
        synced,
        "Traceback" in err and "index exploded" in err,
    ) == (["ext.a", "ext.b"], [True], True)


def test_track_path_from_uri_is_none_for_a_file_that_cannot_be_stat(
    tree, monkeypatch, capsys
):
    library.build_index()
    uri = library.song_uri("A1", "Al1", "t1.flac")
    assert library.track_path_from_uri(uri) is not None
    capsys.readouterr()
    bad = tree / "A1" / "Al1" / "t1.flac"
    fail_path_call(monkeypatch, "is_file", bad)
    assert library.track_path_from_uri(uri) is None
    err = capsys.readouterr().err
    assert f"library: cannot read track {bad}: {denied(bad)}" in err
    assert "skipping" not in err
