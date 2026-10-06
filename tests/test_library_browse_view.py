"""What LibraryBrowseView does when a message edit fails.

A failed edit used to brick the browse message until its timeout:
build_items() detached the old components before the edit, and
discord.py discards a click on any item whose .view is None. These
drive the view through a fake interaction whose edit can raise.

Each test runs its coroutine through asyncio.run(): constructing a
discord.ui.View needs a running event loop, and the dev group has no
pytest-asyncio.
"""

import asyncio
from types import SimpleNamespace

import discord

from musicbot.commands.library import LibraryBrowseView
from musicbot.library import LibrarySong
from musicbot.library_metadata import ArtInfo, Enrichment


def make_song(title):
    return LibrarySong(
        filename=title + ".flac",
        title=title,
        duration=None,
        year=None,
        genre=None,
        fmt="FLAC",
        bitrate=None,
        sample_rate=None,
        bit_depth=None,
    )


INDEX = {
    "Radiohead": {
        "OK Computer": [make_song("Airbag"), make_song("Lucky")],
        "Kid A": [make_song("Zoo")],
    },
    "The Beatles": {"Abbey Road": [make_song("Come Together")]},
}

COVER = Enrichment(
    stats=None, art=ArtInfo(url=None, data=b"\x89PNG", extension="png")
)


def http_error():
    return discord.HTTPException(
        SimpleNamespace(status=500, reason="Server Error"), "boom"
    )


class FakeInteraction:
    """Records every edit; raises from it while `fail` is set."""

    def __init__(self):
        self.edits = []
        self.fail = False

        async def defer():
            pass

        self.response = SimpleNamespace(defer=defer)

    async def edit_original_response(self, **kwargs):
        if self.fail:
            raise http_error()
        self.edits.append(kwargs)


def make_view(index=INDEX):
    ctx = SimpleNamespace(author=SimpleNamespace(id=1), interaction=None)
    return LibraryBrowseView(ctx, index)


def run(coro_fn):
    return asyncio.run(coro_fn())


def test_failed_render_keeps_previous_components_live(capsys):
    async def scenario():
        view = make_view()
        before = list(view.children)
        attached = view._attached
        interaction = FakeInteraction()
        interaction.fail = True
        view.cursor.descend("Radiohead")
        result = await view.render(interaction, sync_attachments=True)
        return view, before, attached, result

    view, before, attached, result = run(scenario)

    assert result is False
    assert view.children == before
    assert all(a is b for a, b in zip(view.children, before))
    assert all(item.view is view for item in view.children)
    assert view._attached == attached
    assert "library: render failed:" in capsys.readouterr().err


def test_render_syncs_a_changed_attachment():
    async def scenario():
        view = make_view()
        view.cursor.descend("Radiohead")
        view._enrichment = COVER
        interaction = FakeInteraction()
        result = await view.render(interaction, sync_attachments=True)
        return view, interaction, result

    view, interaction, result = run(scenario)

    assert result is True
    # the artist level: Select, Queue this Artist, Back
    assert len(view.children) == 3
    assert all(item.view is view for item in view.children)
    assert interaction.edits[-1]["view"] is view
    assert [f.filename for f in interaction.edits[-1]["attachments"]] == [
        "cover.png"
    ]
    assert view._attached == "Radiohead/None.png"


def test_render_without_sync_leaves_attachments_alone():
    async def scenario():
        view = make_view()
        view.cursor.descend("Radiohead")
        view._enrichment = COVER
        interaction = FakeInteraction()
        result = await view.render(interaction)
        return view, interaction, result

    view, interaction, result = run(scenario)

    assert result is True
    assert len(view.children) == 3
    assert "attachments" not in interaction.edits[-1]
    assert view._attached is None


def stub_enrichment(view, result=None):
    """Replaces the disk and HTTP lookup; returns the list each
    resolution is appended to."""
    resolved = []

    async def resolve():
        resolved.append(result)
        return result

    view._resolve_enrichment = resolve
    return resolved


def test_failed_descend_is_undone():
    # thirty artists: two pages of the root's Select
    index = {
        f"Artist {n:02}": {"Album": [make_song("Song")]} for n in range(30)
    }

    async def scenario():
        view = make_view(index)
        resolved = stub_enrichment(view, COVER)
        await view.turn_page(FakeInteraction(), 1)
        interaction = FakeInteraction()
        interaction.fail = True
        await view.descend(interaction, "Artist 27")
        return view, resolved

    view, resolved = run(scenario)

    assert (view.cursor.artist, view.cursor.album) == (None, None)
    assert view.cursor.page == 1
    assert resolved == []


def test_failed_back_is_undone():
    async def scenario():
        view = make_view()
        resolved = stub_enrichment(view)
        await view.descend(FakeInteraction(), "Radiohead")
        await view.descend(FakeInteraction(), "OK Computer")
        resolved.clear()
        interaction = FakeInteraction()
        interaction.fail = True
        await view.go_back(interaction)
        return view, resolved

    view, resolved = run(scenario)

    assert (view.cursor.artist, view.cursor.album) == (
        "Radiohead",
        "OK Computer",
    )
    assert resolved == []


def test_failed_enrichment_edit_is_healed_by_next_page_turn():
    async def scenario():
        view = make_view()
        stub_enrichment(view, COVER)
        interaction = FakeInteraction()

        async def edit(**kwargs):
            # the first draw lands; the enrichment edit fails
            if interaction.edits:
                raise http_error()
            interaction.edits.append(kwargs)

        interaction.edit_original_response = edit
        await view.descend(interaction, "Radiohead")
        enrichment = view._enrichment
        page_turn = FakeInteraction()
        await view.turn_page(page_turn, 0)
        return enrichment, page_turn

    enrichment, page_turn = run(scenario)

    assert enrichment is COVER
    assert [f.filename for f in page_turn.edits[-1]["attachments"]] == [
        "cover.png"
    ]
