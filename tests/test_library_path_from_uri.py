from pathlib import PurePosixPath, PureWindowsPath

import pytest

from musicbot.library import library_path_from_uri

DRIVE = PureWindowsPath("C:/Music")
UNC = PureWindowsPath("//nas/share/Music")
POSIX = PurePosixPath("/srv/music")


def _w(root, *parts):
    return PureWindowsPath(root, *parts)


@pytest.mark.parametrize("root", [DRIVE, UNC])
def test_windows_as_uri_round_trips(root):
    song = _w(root, "A", "B", "t.mp3")
    assert library_path_from_uri(song.as_uri(), root) == song


def test_windows_localhost_drive_form_is_accepted():
    song = _w(DRIVE, "A", "B", "t.mp3")
    uri = "file://localhost/C:/Music/A/B/t.mp3"
    assert library_path_from_uri(uri, DRIVE) == song


def test_windows_lowercase_drive_returns_root_casing():
    got = library_path_from_uri("file:///c:/Music/A/B/t.mp3", DRIVE)
    assert str(got) == "C:\\Music\\A\\B\\t.mp3"


def test_windows_segment_case_comes_back_as_the_uri_gives_it():
    got = library_path_from_uri("file:///C:/music/a/b/T.MP3", DRIVE)
    assert got.parts[-3:] == ("a", "b", "T.MP3")


@pytest.mark.parametrize(
    "uri",
    [
        "file:///C:/Music/A/..%5CB/t.mp3",
        "file:///C:/Music/A/.%5Ct.mp3",
        "file:///C:/Music/A/B/..%5C..%5Ct.mp3",
        "file:///C:/Music/A/../B/t.mp3",
        "file:///C:/Music/A/%2E%2E%5Ct/t.mp3",
        "file:///D:/Music/A/B/t.mp3",
        "file:///C:/Music/A/t.mp3",
        "file:///C:/Music/A/B/C/t.mp3",
        "file:///C|/Music/A/B/t.mp3",
        "file:///C%7C/Music/A/B/t.mp3",
        "file://other/share/Music/A/B/t.mp3",
        "file://nas/C:/Music/A/B/t.mp3",
    ],
)
def test_windows_drive_root_refusals(uri):
    assert library_path_from_uri(uri, DRIVE) is None


@pytest.mark.parametrize(
    "uri",
    [
        "file://other/share/Music/A/B/t.mp3",
        "file://nas/other/Music/A/B/t.mp3",
        "file://nas/share/Music/A/..%5CB/t.mp3",
        "file://nas/share/Music/A/B/t.mp3/..%5C",
        "file:////nas/share/Music/A/B/t.mp3",
        "file://localhost//nas/share/Music/A/B/t.mp3",
        "file:///%2Fnas/share/Music/A/B/t.mp3",
        "file://localhost/%2Fnas/share/Music/A/B/t.mp3",
        "file://localhost/nas/share/Music/A/B/t.mp3",
        "file://nas/C:/Music/A/B/t.mp3",
        "file:///%5C%5Cnas%5Cshare%5CMusic/A/B/t.mp3",
        "file://nas/share/Music/A/t.mp3",
        "file://nas/share/Music/A/B/C/t.mp3",
        "file:///C:/Music/A/B/t.mp3",
    ],
)
def test_windows_unc_root_refusals(uri):
    assert library_path_from_uri(uri, UNC) is None


def test_posix_host_authority_is_refused():
    uri = "file://host/srv/music/A/B/t.mp3"
    assert library_path_from_uri(uri, POSIX) is None


def test_posix_backslash_is_an_ordinary_filename_character():
    uri = "file:///srv/music/A/B/a%5Cb.mp3"
    assert library_path_from_uri(uri, POSIX) == POSIX / "A" / "B" / "a\\b.mp3"


def test_posix_localhost_is_accepted():
    uri = "file://localhost/srv/music/A/B/t.mp3"
    assert library_path_from_uri(uri, POSIX) == POSIX / "A" / "B" / "t.mp3"
