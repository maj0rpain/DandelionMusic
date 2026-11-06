"""check_dependencies: a missing ffmpeg fails startup, nothing is fetched."""

import socket
import subprocess
import sys
import urllib.request

import pytest
from discord import opus

from musicbot import utils

OLD_SL_VERSION = (
    "ffmpeg version N-115000-g1234567-SL_1700000000 Copyright (c) the FFmpeg"
)
OLD_5_1_VERSION = """
ffmpeg version 5.1.git Copyright (c) 2000-2022 the FFmpeg developers
built with gcc 12.1.0 (Rev2, Built by MSYS2 project)
configuration: --disable-programs --enable-ffmpeg --disable-doc\
 --enable-w32threads --enable-openssl --extra-ldflags=-static\
 --pkg-config='pkg-config --static --with-path=/usr/local/lib/pkgconfig'
libavutil      57. 33.101 / 57. 33.101
libavcodec     59. 42.102 / 59. 42.102
libavformat    59. 30.100 / 59. 30.100
libavdevice    59.  8.101 / 59.  8.101
libavfilter     8. 46.103 /  8. 46.103
libswscale      6.  8.103 /  6.  8.103
libswresample   4.  8.100 /  4.  8.100
""".strip()


@pytest.fixture
def probe(monkeypatch, tmp_path):
    """Patch the boundaries; return the list of probe calls' kwargs."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False
    )
    monkeypatch.setattr(opus.Encoder, "get_opus_version", lambda: "1.5")

    def no_network(*args, **kwargs):
        raise AssertionError("network access")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    class Probe(list):
        state = {"result": "ffmpeg version 7.0"}

    calls = Probe()

    def fake_check_output(cmd, **kwargs):
        calls.append(kwargs)
        result = calls.state["result"]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(utils, "check_output", fake_check_output)
    return calls


def _assert_not_found(tmp_path):
    with pytest.raises(RuntimeError) as exc:
        utils.check_dependencies()
    message = str(exc.value)
    assert message.startswith("ffmpeg was not found")
    assert "install" in message.lower()
    assert "PATH" in message
    assert not (tmp_path / "ffmpeg.exe").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize(
    "error",
    [FileNotFoundError(), subprocess.CalledProcessError(1, "ffmpeg")],
    ids=["missing", "probe-fails"],
)
def test_missing_ffmpeg_raises_install_advice(
    monkeypatch, tmp_path, probe, platform, error
):
    monkeypatch.setattr(sys, "platform", platform)
    probe.state["result"] = error
    _assert_not_found(tmp_path)


@pytest.mark.parametrize("version", [OLD_SL_VERSION, OLD_5_1_VERSION])
def test_old_solaluset_ffmpeg_on_windows_starts_normally(
    monkeypatch, probe, version
):
    monkeypatch.setattr(sys, "platform", "win32")
    probe.state["result"] = version
    utils.check_dependencies()


@pytest.mark.parametrize(
    "platform, flags", [("win32", 0x08000000), ("linux", 0)]
)
def test_probe_creationflags(monkeypatch, probe, platform, flags):
    monkeypatch.setattr(sys, "platform", platform)
    utils.check_dependencies()
    assert probe[0]["creationflags"] == flags


def test_missing_opus_raises(monkeypatch, probe):
    def not_loaded():
        raise opus.OpusNotLoaded()

    monkeypatch.setattr(opus.Encoder, "get_opus_version", not_loaded)
    with pytest.raises(RuntimeError, match="^opus was not found"):
        utils.check_dependencies()
