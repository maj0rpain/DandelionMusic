"""What LibraryBrowseView does when a message edit fails, and with
clicks made while an edit is in flight.

A failed edit used to brick the browse message until its timeout:
build_items() detached the old components before the edit, and
discord.py discards a click on any item whose .view is None. And
clicks made while a level's enrichment edit uploaded its artwork used
to be dropped. These drive the view through a fake interaction whose
edit can raise or be held in flight.

Each test runs its coroutine through asyncio.run(): constructing a
discord.ui.View needs a running event loop, and the dev group has no
pytest-asyncio.
"""

import asyncio
from types import SimpleNamespace
from typing import NamedTuple, Optional

import discord

from musicbot.commands.library import LibraryBrowseView
from musicbot.library import LibrarySong
from musicbot.library_metadata import ArtInfo, Enrichment, ExternalStats


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


AUTHOR_ID = 1


class FakeInteraction:
    """Records every edit; raises from it while `fail` is set.

    `hold(n)` holds this interaction's n-th edit (0-based) in flight
    until `release()`: `in_flight` is set once it is waiting. Every
    landed edit is also appended to `log`, when one is shared between
    interactions, as (self, kwargs) - the order edits reached the
    message in. `deferred` counts the interaction's acknowledgements.
    """

    def __init__(self, log=None):
        self.edits = []
        self.fail = False
        self.log = log
        self.user = SimpleNamespace(id=AUTHOR_ID)
        self.deferred = 0
        self._attempts = 0
        self._held = None
        self._gate = None
        self.in_flight = None

        async def defer():
            self.deferred += 1

        self.response = SimpleNamespace(defer=defer)

    def hold(self, n):
        self._held = n
        self._gate = asyncio.Event()
        self.in_flight = asyncio.Event()

    def release(self):
        self._gate.set()

    async def edit_original_response(self, **kwargs):
        attempt = self._attempts
        self._attempts += 1
        if attempt == self._held:
            self.in_flight.set()
            await self._gate.wait()
        if self.fail:
            raise http_error()
        self.edits.append(kwargs)
        if self.log is not None:
            self.log.append((self, kwargs))


def make_view(index=INDEX):
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=AUTHOR_ID), interaction=None
    )
    return LibraryBrowseView(ctx, index)


def run(coro_fn):
    # bounded, so a click left waiting forever fails the test rather
    # than hanging the run
    return asyncio.run(asyncio.wait_for(coro_fn(), 5))


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


async def settle():
    """Lets every task that can run do so, up to its next real wait."""
    for _ in range(20):
        await asyncio.sleep(0)


STALE = Enrichment(
    stats=ExternalStats(
        listeners=424242,
        playcount=None,
        tags=("stale-tag",),
        popularity=77,
        followers=None,
        release_date=None,
    ),
    art=ArtInfo(url=None, data=b"\x89PNG", extension="png"),
)


class Gated(NamedTuple):
    """A stubbed resolution that waits for `gate` before answering."""

    gate: asyncio.Event
    result: Optional[Enrichment]


def stub_enrichment_by_level(view, results):
    """Replaces the lookup with one answering per (artist, album),
    None for a level `results` does not name. Returns the list each
    *finished* resolution's level is appended to."""
    finished = []

    async def resolve():
        level = (view.cursor.artist, view.cursor.album)
        result = results.get(level)
        if isinstance(result, Gated):
            await result.gate.wait()
            result = result.result
        finished.append(level)
        return result

    view._resolve_enrichment = resolve
    return finished


def carries_enrichment(edit):
    embed = edit["embed"]
    names = {field.name for field in embed.fields}
    return bool(
        embed.thumbnail.url
        or edit.get("attachments")
        or {"Listeners", "Popularity"} & names
        or "stale-tag" in (embed.description or "")
    )


def _click_during_enrichment_edit(click):
    async def scenario():
        view = make_view()
        stub_enrichment_by_level(view, {("Radiohead", None): COVER})
        log = []
        entering = FakeInteraction(log)
        entering.hold(1)
        entry = asyncio.create_task(view.descend(entering, "Radiohead"))
        await entering.in_flight.wait()
        clicking = FakeInteraction(log)
        accepted = await view.interaction_check(clicking)
        handler = asyncio.create_task(click(view, clicking))
        await settle()
        moved_to = (view.cursor.artist, view.cursor.album)
        entering.release()
        await asyncio.gather(entry, handler)
        return accepted, moved_to, entering, clicking, log

    return run(scenario)


def test_back_click_during_enrichment_edit_is_accepted():
    accepted, moved_to, entering, clicking, log = (
        _click_during_enrichment_edit(lambda view, i: view.go_back(i))
    )

    assert accepted is True
    assert moved_to == (None, None)
    assert len(entering.edits) == 2
    assert [who for who, _ in log[:3]] == [entering, entering, clicking]
    assert log[1][1]["attachments"][0].filename == "cover.png"


def test_select_click_during_enrichment_edit_is_accepted():
    accepted, moved_to, entering, clicking, log = (
        _click_during_enrichment_edit(
            lambda view, i: view.descend(i, "OK Computer")
        )
    )

    assert accepted is True
    assert moved_to == ("Radiohead", "OK Computer")
    assert len(entering.edits) == 2
    assert [who for who, _ in log[:3]] == [entering, entering, clicking]
    assert log[2][1]["embed"].title != log[1][1]["embed"].title


def test_enrichment_resolving_after_moving_on_is_not_drawn():
    async def scenario():
        view = make_view()
        gate = asyncio.Event()
        finished = stub_enrichment_by_level(
            view, {("Radiohead", None): Gated(gate, STALE)}
        )
        entering = FakeInteraction()
        entry = asyncio.create_task(view.descend(entering, "Radiohead"))
        await settle()
        # move on to the album, and hold its own enrichment edit in
        # flight so the render lock stays taken
        clicking = FakeInteraction()
        clicking.hold(1)
        accepted = await view.interaction_check(clicking)
        handler = asyncio.create_task(view.descend(clicking, "OK Computer"))
        await clicking.in_flight.wait()
        # a redraw of the album that queues behind that edit, so it is
        # built only after the stale enrichment below has resolved
        paging = FakeInteraction()
        page_accepted = await view.interaction_check(paging)
        page_turn = asyncio.create_task(view.turn_page(paging, 0))
        await settle()
        gate.set()
        await settle()
        resolved_while_pending = ("Radiohead", None) in finished
        clicking.release()
        await asyncio.gather(entry, handler, page_turn)
        redraws = clicking.edits + paging.edits
        return (
            accepted and page_accepted,
            resolved_while_pending,
            entering,
            paging,
            redraws,
        )

    accepted, resolved_while_pending, entering, paging, redraws = run(scenario)

    assert accepted is True
    assert resolved_while_pending is True
    assert len(entering.edits) == 1
    assert paging.edits
    assert not any(carries_enrichment(edit) for edit in redraws)


def test_second_select_click_during_first_draw_is_turned_away():
    async def scenario():
        view = make_view()
        stub_enrichment_by_level(view, {})
        entering = FakeInteraction()
        entering.hold(0)
        entry = asyncio.create_task(view.descend(entering, "Radiohead"))
        await entering.in_flight.wait()
        second = FakeInteraction()
        accepted = await view.interaction_check(second)
        entering.release()
        await entry
        return accepted, second, view

    accepted, second, view = run(scenario)

    assert accepted is False
    assert second.deferred == 1
    assert (view.cursor.artist, view.cursor.album) == ("Radiohead", None)


def test_leaving_a_level_lets_its_enrichment_finish_undrawn():
    async def scenario():
        view = make_view()
        gate = asyncio.Event()
        finished = stub_enrichment_by_level(
            view, {("Radiohead", None): Gated(gate, STALE)}
        )
        entering = FakeInteraction()
        entry = asyncio.create_task(view.descend(entering, "Radiohead"))
        await settle()
        await view.go_back(FakeInteraction())
        gate.set()
        await entry
        return finished, entering

    finished, entering = run(scenario)

    assert ("Radiohead", None) in finished
    assert len(entering.edits) == 1
