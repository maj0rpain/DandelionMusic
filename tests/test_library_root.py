"""The library root is resolved once, by build_index, off the event loop."""

from pathlib import Path

import pytest

from config import config
from musicbot import library


@pytest.fixture
def root(tmp_path, monkeypatch):
    real = tmp_path / "lib"
    (real / "A" / "B").mkdir(parents=True)
    (real / "A" / "B" / "t.flac").write_bytes(b"")
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", str(real))
    monkeypatch.setattr(config, "LIBRARY_EXTENSIONS", (".flac",))
    yield real
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", "")
    library.build_index()


def count_resolves(monkeypatch):
    calls = []
    original = Path.resolve

    def resolve(self, *a, **kw):
        calls.append(self)
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", resolve)
    return calls


def test_song_path_makes_no_resolve_call_after_build(root, monkeypatch):
    library.build_index()
    expected = root.resolve() / "A" / "B" / "t.flac"
    calls = count_resolves(monkeypatch)
    path = library.song_path("A", "B", "t.flac")
    uri = library.song_uri("A", "B", "t.flac")
    assert calls == []
    assert path == expected
    assert uri == expected.as_uri()


@pytest.mark.parametrize("value", ["", "missing"])
def test_unset_or_not_a_directory_falls_back_to_resolving_the_config_path(
    root, monkeypatch, tmp_path, value
):
    library.build_index()
    target = str(tmp_path / value) if value else value
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", target)
    library.build_index()
    assert library.song_path("A", "B", "t.flac") == (
        Path(target).resolve() / "A" / "B" / "t.flac"
    )


def test_resolve_oserror_during_build_falls_back_to_resolving_the_config_path(
    root, monkeypatch
):
    original = Path.resolve

    def boom(self, *a, **kw):
        raise OSError("stale mount")

    monkeypatch.setattr(Path, "resolve", boom)
    library.build_index()
    monkeypatch.setattr(Path, "resolve", original)
    assert library.song_path("A", "B", "t.flac") == (
        root.resolve() / "A" / "B" / "t.flac"
    )


def test_rebuild_with_no_library_clears_the_stored_root(
    root, monkeypatch, tmp_path
):
    library.build_index()
    other = tmp_path / "other"
    other.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(other)
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", str(tmp_path / "gone"))
    library.build_index()
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", str(alias))
    # stored root cleared: falls back to resolving the current config
    assert library.song_path("A", "B", "t.flac") == (
        other.resolve() / "A" / "B" / "t.flac"
    )


def test_index_generation_rises_with_each_build(root):
    before = library.index_generation()
    index = library.build_index()
    assert library.index_generation() == before + 1
    assert library.get_index() is index


def test_a_build_that_raises_leaves_the_generation(root, monkeypatch):
    library.build_index()
    before = library.index_generation()

    def boom(*a, **kw):
        raise RuntimeError("walk failed")

    monkeypatch.setattr(library, "read_tags", boom)
    with pytest.raises(RuntimeError):
        library.build_index()
    assert library.index_generation() == before
