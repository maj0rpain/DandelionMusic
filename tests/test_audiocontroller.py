"""AudioController, built with no Discord connection.

Every test here goes through the `controller` fixture (tests/conftest.py,
built on tests/controller_seam.py): a real AudioController over a stub
musicbot.loader, a fake bot, guild and voice client, with the working
directory set to tmp_path so the controller's backup/ lands there.
"""

import asyncio
import pickle
import sys
from pathlib import Path

from controller_seam import StubLoader, controller_seam


def test_controller_pickles_its_playlist_into_the_temp_backup_dir(
    controller, tmp_path
):
    # The Song and SiteTypes the controller module itself sees: the
    # seam imports it fresh, so they are not the ones a top-level
    # import in this file would bind.
    ac = sys.modules[type(controller).__module__]
    for title in ("first track", "second track"):
        controller.playlist.add(
            ac.Song(
                ac.SiteTypes.YT_DLP,
                f"https://example.invalid/{title}",
                title=title,
            )
        )

    controller.pickle_playlist()

    backup = tmp_path / "backup" / "playlist_1234.pickle"
    with open(backup, "rb") as f:
        restored = pickle.load(f)
    assert [song.title for song in restored.playque] == [
        "first track",
        "second track",
    ]


def test_controller_starts_at_the_guild_default_volume(controller):
    assert controller.volume == 70


def test_stub_loader_records_preload_calls(controller, stub_loader):
    import asyncio

    song = object()
    assert asyncio.run(stub_loader.preload(song, controller.bot)) is True
    assert stub_loader.calls == [("preload", (song, controller.bot))]


def test_seam_leaves_no_stub_and_no_backup_behind(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(tmp_path)
    before = sys.modules.get("musicbot.loader")

    with controller_seam(tmp_path / "work") as (built, _):
        assert isinstance(sys.modules["musicbot.loader"], StubLoader)
        built.pickle_playlist()

    after = sys.modules.get("musicbot.loader")
    assert after is before
    assert not isinstance(
        getattr(sys.modules.get("musicbot"), "loader", None), StubLoader
    )
    controller_module = sys.modules.get("musicbot.audiocontroller")
    assert not isinstance(
        getattr(controller_module, "loader", None), StubLoader
    )
    assert not (repo / "backup").exists()


def _queue(controller, *titles):
    ac = sys.modules[type(controller).__module__]
    for title in titles:
        controller.playlist.add(
            ac.Song(
                ac.SiteTypes.YT_DLP,
                f"https://example.invalid/{title}",
                url=f"https://stream.invalid/{title}",
                title=title,
            )
        )


class _FakeFFmpeg:
    """Stands in for ffmpeg: records the stream it was asked to open."""

    def __init__(self, url, **kwargs):
        self.url = url

    def read(self):
        return b""


async def _settle():
    # lets the preload task the controller scheduled run to the end
    for _ in range(10):
        await asyncio.sleep(0)


def test_ensure_playing_starts_the_head_of_the_queue_when_idle(
    controller, monkeypatch
):
    import discord

    monkeypatch.setattr(
        discord,
        "FFmpegPCMAudio",
        type("FakeFFmpeg", (_FakeFFmpeg, discord.AudioSource), {}),
    )
    _queue(controller, "first track", "second track")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        await _settle()

    asyncio.run(run())

    played = controller.guild.voice_client.played
    assert [source.original.url for source in played] == [
        "https://stream.invalid/first track"
    ]


def test_ensure_playing_only_preloads_while_something_is_playing(
    controller, stub_loader
):
    controller.guild.voice_client.playing = True
    _queue(controller, "current track", "next track")

    async def run():
        controller.bot.loop = asyncio.get_running_loop()
        await controller.ensure_playing()
        await _settle()

    asyncio.run(run())

    assert controller.guild.voice_client.played == []
    assert [args[0].title for name, args in stub_loader.calls] == [
        "next track"
    ]
