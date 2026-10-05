"""AudioController, built with no Discord connection.

Every test here goes through the `controller` fixture (tests/conftest.py,
built on tests/controller_seam.py): a real AudioController over a stub
musicbot.loader, a fake bot, guild and voice client, with the working
directory set to tmp_path so the controller's backup/ lands there.
"""

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
