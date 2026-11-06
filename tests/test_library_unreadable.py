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


def fail_stat(monkeypatch, name, bad):
    original = getattr(Path, name)

    def stat(self, *a, **kw):
        if self == bad:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, name, stat)


def test_unreadable_artist_dir_is_skipped_and_sibling_indexed(
    tree, monkeypatch, capsys
):
    fail_stat(monkeypatch, "is_dir", tree / "A1")
    index = library.build_index()
    assert list(index) == ["A2"]
    assert f"library: skipping unreadable {tree / 'A1'}" in (
        capsys.readouterr().err
    )


def test_unreadable_album_dir_is_skipped_and_sibling_indexed(
    tree, monkeypatch, capsys
):
    bad = tree / "A1" / "Al1"
    fail_stat(monkeypatch, "is_dir", bad)
    index = library.build_index()
    assert sorted(index["A1"]) == ["Al2"]
    assert f"library: skipping unreadable {bad}" in capsys.readouterr().err


def test_unreadable_file_is_skipped_and_sibling_track_indexed(
    tree, monkeypatch, capsys
):
    bad = tree / "A1" / "Al1" / "t1.flac"
    fail_stat(monkeypatch, "is_file", bad)
    index = library.build_index()
    assert [s.filename for s in index["A1"]["Al1"]] == ["t4.flac"]
    assert f"library: skipping unreadable {bad}" in capsys.readouterr().err


def test_unreadable_root_gives_an_empty_index(tree, monkeypatch, capsys):
    fail_stat(monkeypatch, "is_dir", tree)
    assert library.build_index() == {}
    assert f"library: skipping unreadable {tree}" in capsys.readouterr().err


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
    tree, monkeypatch
):
    library.build_index()
    uri = library.song_uri("A1", "Al1", "t1.flac")
    assert library.track_path_from_uri(uri) is not None
    fail_stat(monkeypatch, "is_file", tree / "A1" / "Al1" / "t1.flac")
    assert library.track_path_from_uri(uri) is None
