"""The loader's local-library branch: what a file:// URI loads as.

Called synchronously in-process over a tmp_path library, with real
symlinks pointing at a second directory outside it.
"""

import os
from pathlib import Path

import pytest

from config import config
from musicbot import library, loader
from musicbot.linkutils import SiteTypes

MISSING = "library file is missing (test)"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", str(root))
    monkeypatch.setattr(config, "ENABLE_LOCAL_LIBRARY", True)
    monkeypatch.setattr(config, "LIBRARY_FILE_MISSING", MISSING)
    monkeypatch.setattr(library, "_root", None)
    return root.resolve()


@pytest.fixture
def outside(tmp_path):
    return tmp_path / "elsewhere"


def _link(link: Path, target: Path):
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _uri(lib: Path, rel: str) -> str:
    return f"file://{lib}/{rel}"


def _refused(uri):
    with pytest.raises(loader.SongError) as e:
        loader._load_song(uri)
    assert str(e.value) == MISSING


def test_a_plain_track_loads(lib):
    _file(lib / "A" / "B" / "t.mp3")
    song = loader._load_song(_uri(lib, "A/B/t.mp3"))
    assert song.host is SiteTypes.LOCAL_LIBRARY
    assert song.url == str(lib / "A" / "B" / "t.mp3")


def test_a_symlinked_album_folder_loads(lib, outside):
    _file(outside / "t.mp3")
    (lib / "A").mkdir()
    _link(lib / "A" / "B", outside)
    song = loader._load_song(_uri(lib, "A/B/t.mp3"))
    assert song.url == str(lib / "A" / "B" / "t.mp3")


def test_a_symlinked_track_file_loads_titled_by_the_link(lib, outside):
    target = _file(outside / "target-name.mp3")
    (lib / "A" / "B").mkdir(parents=True)
    _link(lib / "A" / "B" / "link-name.mp3", target)
    uri = _uri(lib, "A/B/link-name.mp3")
    song = loader._load_song(uri)
    assert song.url == str(lib / "A" / "B" / "link-name.mp3")
    assert song.title == "link-name"
    assert song.webpage_url == uri


@pytest.mark.parametrize(
    "rel",
    [
        "A/B/../B/t.mp3",
        "A/./B/t.mp3",
        "A/%2E%2E/A/B/t.mp3",
        "A/%2E/B/t.mp3",
    ],
)
def test_dot_segments_are_refused(lib, rel):
    _file(lib / "A" / "B" / "t.mp3")
    _refused(_uri(lib, rel))


def test_climbing_out_through_a_symlinked_artist_is_refused(lib, outside):
    _file(outside.parent / "secret.mp3")
    (outside / "B").mkdir()
    _link(lib / "A", outside)
    _refused(_uri(lib, "A/../secret.mp3"))
    _refused(_uri(lib, "A/B/../../secret.mp3"))
    _refused(_uri(lib, "A/%2E%2E/secret.mp3"))


@pytest.mark.parametrize("name", ["Who?", "Who#"])
def test_dots_after_a_query_or_fragment_char_are_refused(lib, outside, name):
    _file(outside.parent / "x.mp3")
    (outside / "B").mkdir()
    _link(lib / name, outside)
    _refused(_uri(lib, f"{name}/B/../../x.mp3"))
    _refused(_uri(lib, f"{name}/../x.mp3"))


@pytest.mark.parametrize("rel", ["A/t.mp3", "A/B/C/t.mp3", "t.mp3"])
def test_the_wrong_depth_is_refused(lib, rel):
    _file(lib / "A" / "t.mp3")
    _file(lib / "A" / "B" / "C" / "t.mp3")
    _file(lib / "t.mp3")
    _refused(_uri(lib, rel))


def test_a_localhost_authority_loads(lib):
    _file(lib / "A" / "B" / "t.mp3")
    song = loader._load_song(f"file://localhost{lib}/A/B/t.mp3")
    assert song.url == str(lib / "A" / "B" / "t.mp3")


def test_any_other_authority_is_refused(lib):
    _file(lib / "A" / "B" / "t.mp3")
    _refused(f"file://otherhost{lib}/A/B/t.mp3")


@pytest.mark.parametrize("name", ["t?.mp3", "t#.mp3"])
def test_a_raw_query_or_fragment_char_in_a_filename_loads(lib, name):
    _file(lib / "A" / "B" / name)
    song = loader._load_song(_uri(lib, f"A/B/{name}"))
    assert song.url == str(lib / "A" / "B" / name)


def test_a_path_outside_the_root_is_refused(lib):
    _refused("file:///etc/hosts")
    _refused("file:///etc/x/hosts")


def test_no_library_path_is_refused(lib, monkeypatch):
    _file(lib / "A" / "B" / "t.mp3")
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", "")
    _refused(_uri(lib, "A/B/t.mp3"))


def test_a_missing_file_is_refused(lib):
    _refused(_uri(lib, "A/B/gone.mp3"))


def test_a_symlinked_library_root_loads_generated_uris_only(
    tmp_path, lib, monkeypatch
):
    _file(lib / "A" / "B" / "t.mp3")
    alias = tmp_path / "alias"
    _link(alias, lib)
    monkeypatch.setattr(config, "MUSIC_LIBRARY_PATH", str(alias))
    song = loader._load_song(library.song_uri("A", "B", "t.mp3"))
    assert song.url == str(lib / "A" / "B" / "t.mp3")
    _refused(f"file://{alias}/A/B/t.mp3")


def test_one_bad_track_leaves_the_rest_of_the_batch(lib, monkeypatch):
    _file(lib / "A" / "B" / "t.mp3")
    _file(lib / "A" / "B" / "bad.mp3")
    real = loader.read_tags

    def read_tags(path):
        if path.name == "bad.mp3":
            raise RuntimeError("corrupt tags")
        return real(path)

    monkeypatch.setattr(loader, "read_tags", read_tags)

    good = _uri(lib, "A/B/t.mp3")
    songs = loader._load_local_songs(
        [
            _uri(lib, "A/B/gone.mp3"),
            good,
            _uri(lib, "A/B/bad.mp3"),
            good,
        ]
    )
    assert [s is None for s in songs] == [True, False, True, False]


def test_the_index_and_the_loader_agree_on_a_symlinked_artist(lib, outside):
    _file(outside / "B" / "t.mp3")
    _link(lib / "A", outside)
    index = library.build_index()
    assert "A" in index
    song = loader._load_song(library.song_uri("A", "B", "t.mp3"))
    assert song.url == str(lib / "A" / "B" / "t.mp3")


def test_an_upper_case_scheme_loads(lib):
    _file(lib / "A" / "B" / "t.mp3")
    song = loader._load_song("FILE" + _uri(lib, "A/B/t.mp3")[4:])
    assert song.host is SiteTypes.LOCAL_LIBRARY
    assert song.url == str(lib / "A" / "B" / "t.mp3")


def test_an_upper_case_scheme_is_unknown_with_the_library_disabled(
    lib, monkeypatch
):
    _file(lib / "A" / "B" / "t.mp3")
    monkeypatch.setattr(config, "ENABLE_LOCAL_LIBRARY", False)
    with pytest.raises(loader.SongError) as e:
        loader._load_song("FILE" + _uri(lib, "A/B/t.mp3")[4:])
    assert str(e.value) == config.SONGINFO_UNSUPPORTED


def test_an_upper_case_scheme_outside_the_root_is_refused(lib):
    _refused("FILE:///etc/hosts")
