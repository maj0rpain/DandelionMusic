"""What LibraryBrowseView does when a message edit fails, and with
clicks made while an edit is in flight.

A failed edit used to brick the browse message until its timeout, and
clicks made while an edit was in flight were dropped by discord.py
before reaching the view: build_items() detached the old components
before the edit, and discord.py discards a click on any item whose
.view is None. Clicks here are dispatched through discord.py's real
ViewStore, against a fake interaction whose edit can raise or be held
in flight.

Each test runs its coroutine through asyncio.run(): constructing a
discord.ui.View needs a running event loop, and the dev group has no
pytest-asyncio.
"""

import asyncio
from types import SimpleNamespace
from typing import NamedTuple, Optional

import aiohttp
import discord
import pytest

from musicbot.commands import library as library_commands
from musicbot.commands.library import (
    BackButton,
    CheckError,
    LibraryBrowseView,
    LibrarySelect,
    PageButton,
    QueueLevelButton,
)
from musicbot.library import LibrarySong
from musicbot.library_metadata import ArtInfo, Enrichment, ExternalStats
from discord_fakes import http_error, run


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


AUTHOR_ID = 1
MESSAGE_ID = 4242


class FakeInteraction:
    """Records every edit; raises from it while `fail` is set.

    `hold(n)` holds this interaction's n-th edit (0-based) in flight
    until `release()`: `in_flight` is set once it is waiting. Every
    landed edit is also appended to `log`, when one is shared between
    interactions, as (self, kwargs) - the order edits reached the
    message in. `deferred` counts the interaction's acknowledgements.
    """

    def __init__(
        self,
        log=None,
        clicks=None,
        fail=False,
        fail_on=None,
        edit_error=None,
        defer_error=None,
    ):
        self.edits = []
        self.fail = fail
        self.fail_on = set() if fail_on is None else fail_on
        # what a failing edit / defer() raises; an HTTPException by
        # default for the edit, nothing for defer()
        self.edit_error = edit_error
        self.defer_error = defer_error
        self.log = log
        self.clicks = clicks
        self.message = SimpleNamespace(id=MESSAGE_ID)
        self.data = {}
        self.user = SimpleNamespace(id=AUTHOR_ID)
        self.deferred = 0
        self._attempts = 0
        self._held = None
        self._gate = None
        self.in_flight = None

        async def defer():
            if self.defer_error is not None:
                raise self.defer_error
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
        if self.fail or attempt in self.fail_on:
            raise self.edit_error or http_error()
        self.edits.append(kwargs)
        if self.clicks is not None:
            self.clicks.landed(kwargs["view"])
        if self.log is not None:
            self.log.append((self, kwargs))


def make_view(index=INDEX):
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=AUTHOR_ID), interaction=None
    )
    return LibraryBrowseView(ctx, index)


class Clicks:
    """Dispatches clicks the way discord.py does: through a real
    ViewStore, with the view registered under one message id as the
    first send would, and re-added when an edit lands (FakeInteraction
    calls landed()). A click targets the components the message is
    showing, never the view's not-yet-landed children, and is dropped
    - click() returns None - when discord.py would drop it."""

    def __init__(self, view):
        self.view = view
        self.store = discord.ui.view.ViewStore(SimpleNamespace())
        self.tasks = []
        self.store.add_task = self.tasks.append
        self.landed(view)

    def landed(self, view):
        self.shown = list(view.children)
        self.store.add_view(view, MESSAGE_ID)

    def interaction(self, log=None, **kwargs):
        return FakeInteraction(log, clicks=self, **kwargs)

    def click(self, kind, interaction, label=None):
        """Clicks the shown `kind` component; for a Select, the
        option labelled `label`."""
        item = next(i for i in self.shown if isinstance(i, kind))
        if label is not None:
            pick(item, interaction, label)
        before = len(self.tasks)
        self.store.dispatch_view(item.type.value, item.custom_id, interaction)
        return self.tasks[-1] if len(self.tasks) > before else None


def pick(select, interaction, label):
    """Picks the option of `select` labelled `label`, as discord.py
    does before it calls the select's callback."""
    value = next(o.value for o in select.options if o.label == label)
    interaction.data = {"values": [value]}
    select._refresh_state(interaction, interaction.data)


def level_of(view):
    return (view.cursor.artist, view.cursor.album)


async def finish(*tasks):
    await asyncio.gather(*[t for t in tasks if t is not None])


def test_failed_render_keeps_previous_components_live(capsys):
    async def scenario():
        view = make_view()
        before = list(view.children)
        attached = view._attached
        interaction = FakeInteraction(fail=True)
        view.cursor.descend("Radiohead")
        result = await view.render(interaction, sync_attachments=True)
        return view, before, attached, result

    view, before, attached, result = run(scenario())

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

    view, interaction, result = run(scenario())

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

    view, interaction, result = run(scenario())

    assert result is True
    assert len(view.children) == 3
    assert "attachments" not in interaction.edits[-1]
    assert view._attached is None


# thirty artists: two pages of the root's Select
PAGED = {f"Artist {n:02}": {"Album": [make_song("Song")]} for n in range(30)}


def test_failed_descend_is_undone():
    async def scenario():
        view = make_view(PAGED)
        resolved = stub_enrichment(view, default=COVER)
        await view.turn_page(FakeInteraction(), 1)
        interaction = FakeInteraction(fail=True)
        await view.descend(interaction, "Artist 27")
        return view, resolved

    view, resolved = run(scenario())

    assert level_of(view) == (None, None)
    assert view.cursor.page == 1
    assert resolved == []


def test_failed_back_is_undone():
    async def scenario():
        view = make_view()
        resolved = stub_enrichment(view)
        await view.descend(FakeInteraction(), "Radiohead")
        await view.descend(FakeInteraction(), "OK Computer")
        resolved.clear()
        interaction = FakeInteraction(fail=True)
        await view.go_back(interaction)
        return view, resolved

    view, resolved = run(scenario())

    assert level_of(view) == ("Radiohead", "OK Computer")
    assert resolved == []


def test_failed_enrichment_edit_is_healed_by_next_page_turn():
    async def scenario():
        view = make_view()
        stub_enrichment(view, default=COVER)
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

    enrichment, page_turn = run(scenario())

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


def stub_enrichment(view, results=None, default=None):
    """Replaces the disk and HTTP lookup with one answering per
    (artist, album) from `results`, `default` for a level it does not
    name. Returns the list each *finished* resolution's level is
    appended to."""
    results = results or {}
    finished = []

    async def resolve():
        level = level_of(view)
        result = results.get(level, default)
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


class EditClick(NamedTuple):
    view: LibraryBrowseView
    moved_to: tuple
    page: int
    handler: Optional[asyncio.Task]
    entering: FakeInteraction
    clicking: FakeInteraction
    log: list


def _click_during_enrichment_edit(click, index=INDEX, artist="Radiohead"):
    """Holds the artist level's enrichment edit in flight and
    dispatches `click(clicks, interaction)` meanwhile."""

    async def scenario():
        view = make_view(index)
        stub_enrichment(view, {(artist, None): COVER})
        clicks = Clicks(view)
        log = []
        entering = clicks.interaction(log)
        entering.hold(1)
        entry = clicks.click(LibrarySelect, entering, label=artist)
        await entering.in_flight.wait()
        clicking = clicks.interaction(log)
        handler = click(clicks, clicking)
        await settle()
        moved_to = level_of(view)
        page = view.cursor.page
        entering.release()
        await finish(entry, handler)
        return EditClick(
            view, moved_to, page, handler, entering, clicking, log
        )

    return run(scenario())


def test_back_click_during_enrichment_edit_is_accepted():
    result = _click_during_enrichment_edit(
        lambda clicks, i: clicks.click(BackButton, i)
    )

    assert result.handler is not None
    assert result.moved_to == (None, None)
    assert result.clicking.deferred == 1
    assert len(result.entering.edits) == 2
    assert [who for who, _ in result.log[:3]] == [
        result.entering,
        result.entering,
        result.clicking,
    ]
    assert result.log[1][1]["attachments"][0].filename == "cover.png"


def test_select_click_during_enrichment_edit_is_accepted():
    result = _click_during_enrichment_edit(
        lambda clicks, i: clicks.click(LibrarySelect, i, label="OK Computer")
    )
    log = result.log

    assert result.handler is not None
    assert result.moved_to == ("Radiohead", "OK Computer")
    assert len(result.entering.edits) == 2
    assert [who for who, _ in log[:3]] == [
        result.entering,
        result.entering,
        result.clicking,
    ]
    assert log[2][1]["embed"].title != log[1][1]["embed"].title


def test_queue_click_during_enrichment_edit_queues_the_level(monkeypatch):
    queued = []

    async def queue_songs(ctx, interaction, triples, source):
        await interaction.response.defer()
        queued.extend(triples)

    monkeypatch.setattr(library_commands, "queue_songs", queue_songs)

    result = _click_during_enrichment_edit(
        lambda clicks, i: clicks.click(QueueLevelButton, i)
    )

    assert result.handler is not None
    assert result.clicking.deferred == 1
    assert sorted(f for _, _, f in queued) == [
        "Airbag.flac",
        "Lucky.flac",
        "Zoo.flac",
    ]


PAGED_INDEX = {
    "Artist": {f"Album {n:02}": [make_song("Song")] for n in range(30)}
}


def test_page_click_during_enrichment_edit_turns_the_page_once():
    result = _click_during_enrichment_edit(
        lambda clicks, i: clicks.click(PageButton, i),
        index=PAGED_INDEX,
        artist="Artist",
    )

    assert result.handler is not None
    assert result.page == 1
    assert result.view.cursor.page == 1


def test_enrichment_resolving_after_moving_on_is_not_drawn():
    async def scenario():
        view = make_view()
        gate = asyncio.Event()
        finished = stub_enrichment(
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

    accepted, resolved_while_pending, entering, paging, redraws = run(
        scenario()
    )

    assert accepted is True
    assert resolved_while_pending is True
    assert len(entering.edits) == 1
    assert paging.edits
    assert not any(carries_enrichment(edit) for edit in redraws)


def test_second_select_click_during_first_draw_is_turned_away():
    async def scenario():
        view = make_view()
        stub_enrichment(view)
        clicks = Clicks(view)
        entering = clicks.interaction()
        entering.hold(0)
        entry = clicks.click(LibrarySelect, entering, label="Radiohead")
        await entering.in_flight.wait()
        second = clicks.interaction()
        handler = clicks.click(LibrarySelect, second, label="The Beatles")
        await handler
        entering.release()
        await finish(entry, handler)
        return handler, second, view

    handler, second, view = run(scenario())

    assert handler is not None
    assert second.deferred == 1
    assert level_of(view) == ("Radiohead", None)


def test_second_click_during_page_turn_is_turned_away():
    async def scenario():
        view = make_view(PAGED)
        clicks = Clicks(view)
        turning = clicks.interaction()
        turning.hold(0)
        turn = clicks.click(PageButton, turning)
        await turning.in_flight.wait()
        second = clicks.interaction()
        handler = clicks.click(PageButton, second)
        await handler
        turning.release()
        await finish(turn, handler)
        return handler, second, view

    handler, second, view = run(scenario())

    assert handler is not None
    assert second.deferred == 1
    assert view.cursor.page == 1


def test_select_from_previous_page_during_page_turn_opens_its_entry():
    # The one test here that does not dispatch through the ViewStore:
    # while a page turn's edit is held the double-click guard turns
    # every dispatched click away, and once it lands discord.py has
    # dropped the old Select's custom_id. What is left is a pick whose
    # callback is already past both checks when the page moves, so its
    # callback is awaited directly.
    async def scenario():
        view = make_view(PAGED)
        stub_enrichment(view)
        clicks = Clicks(view)
        previous = next(
            i for i in view.children if isinstance(i, LibrarySelect)
        )
        turning = clicks.interaction()
        turning.hold(0)
        turn = clicks.click(PageButton, turning)
        await turning.in_flight.wait()
        turning.release()
        await finish(turn)
        assert view.cursor.page == 1
        picking = clicks.interaction()
        pick(previous, picking, "Artist 03")
        await previous.callback(picking)
        return view

    view = run(scenario())

    assert level_of(view) == ("Artist 03", None)


def test_click_after_failed_first_draw_acts_on_the_shown_level():
    async def scenario():
        view = make_view()
        stub_enrichment(view)
        clicks = Clicks(view)
        failing = clicks.interaction(fail=True)
        await finish(clicks.click(LibrarySelect, failing, label="Radiohead"))
        undone = level_of(view)
        again = clicks.interaction()
        await finish(clicks.click(LibrarySelect, again, label="The Beatles"))
        return undone, view

    undone, view = run(scenario())

    assert undone == (None, None)
    assert level_of(view) == ("The Beatles", None)


def test_click_after_failed_page_turn_acts_on_the_shown_page():
    async def scenario():
        view = make_view(PAGED)
        stub_enrichment(view)
        clicks = Clicks(view)
        failing = clicks.interaction(fail=True)
        await finish(clicks.click(PageButton, failing))
        undone_page = view.cursor.page
        again = clicks.interaction()
        await finish(clicks.click(LibrarySelect, again, label="Artist 03"))
        return undone_page, view

    undone_page, view = run(scenario())

    assert undone_page == 0
    assert level_of(view) == ("Artist 03", None)


def test_click_after_failed_enrichment_edit_acts_on_the_shown_level():
    async def scenario():
        view = make_view()
        stub_enrichment(view, {("Radiohead", None): COVER})
        clicks = Clicks(view)
        entering = clicks.interaction(fail_on={1})
        await finish(clicks.click(LibrarySelect, entering, label="Radiohead"))
        again = clicks.interaction()
        await finish(clicks.click(BackButton, again))
        return view

    view = run(scenario())

    assert level_of(view) == (None, None)


def test_leaving_a_level_lets_its_enrichment_finish_undrawn():
    async def scenario():
        view = make_view()
        gate = asyncio.Event()
        finished = stub_enrichment(
            view, {("Radiohead", None): Gated(gate, STALE)}
        )
        entering = FakeInteraction()
        entry = asyncio.create_task(view.descend(entering, "Radiohead"))
        await settle()
        await view.go_back(FakeInteraction())
        gate.set()
        await entry
        return finished, entering

    finished, entering = run(scenario())

    assert ("Radiohead", None) in finished
    assert len(entering.edits) == 1


def _descend_past_kept_components():
    """A view descended into Radiohead, with the root level's
    components kept from before the descend landed."""

    async def scenario():
        view = make_view()
        stub_enrichment(view)
        clicks = Clicks(view)
        kept = list(clicks.shown)
        await finish(
            clicks.click(
                LibrarySelect, clicks.interaction(), label="Radiohead"
            )
        )
        return view, clicks, kept

    return scenario


async def _view_at_paged_artist():
    view = make_view(PAGED_INDEX)
    stub_enrichment(view, {})
    clicks = Clicks(view)
    await finish(
        clicks.click(LibrarySelect, clicks.interaction(), label="Artist")
    )
    return view, clicks


@pytest.mark.parametrize(
    "kind", [LibrarySelect, BackButton, PageButton, QueueLevelButton]
)
def test_component_from_an_earlier_scope_is_acknowledged_and_ignored(
    kind, monkeypatch
):
    queued = []

    async def queue_songs(ctx, interaction, triples, source):
        queued.append(triples)

    monkeypatch.setattr(library_commands, "queue_songs", queue_songs)

    async def scenario():
        view, clicks = await _view_at_paged_artist()
        kept = next(i for i in clicks.shown if isinstance(i, kind))
        await finish(
            clicks.click(LibrarySelect, clicks.interaction(), label="Album 00")
        )
        where = (view.cursor.artist, view.cursor.album, view.cursor.page)
        picking = clicks.interaction()
        if kind is LibrarySelect:
            pick(kept, picking, "Album 01")
        await kept.callback(picking)
        return view, where, picking

    view, where, picking = run(scenario())

    assert picking.deferred == 1
    assert (view.cursor.artist, view.cursor.album, view.cursor.page) == where
    assert where[:2] == ("Artist", "Album 00")
    assert queued == []


def test_every_browse_component_is_built_on_the_stale_guard_base():
    async def scenario():
        views = []
        view = make_view()
        views.append(list(view.children))
        view.cursor.descend("Radiohead")
        view.build_items()
        views.append(list(view.children))
        view.cursor.descend("Kid A")
        view.build_items()
        views.append(list(view.children))
        paged = make_view(PAGED_INDEX)
        paged.cursor.descend("Artist")
        paged.build_items()
        views.append(list(paged.children))
        return views

    views = run(scenario())

    items = [item for children in views for item in children]
    assert any(isinstance(i, PageButton) for i in items)
    for item in items:
        assert isinstance(item, library_commands._BrowseItem)
        assert type(item).callback is library_commands._BrowseItem.callback


def test_stranger_on_a_stale_scope_component_is_told_it_is_not_theirs():
    async def scenario():
        view, clicks, kept = await _descend_past_kept_components()()
        select = next(i for i in kept if isinstance(i, LibrarySelect))
        replies = []

        async def send_message(content, ephemeral):
            replies.append(content)

        stranger = clicks.interaction()
        stranger.user = SimpleNamespace(id=AUTHOR_ID + 1)
        stranger.response = SimpleNamespace(
            send_message=send_message, defer=stranger.response.defer
        )
        stranger.data = {"values": ["1"]}
        # discord.py drops a replaced component's custom_id once its
        # edit lands; put it back to reach the guard through dispatch
        clicks.store._views[MESSAGE_ID][
            (select.type.value, select.custom_id)
        ] = select
        before = len(clicks.tasks)
        clicks.store.dispatch_view(
            select.type.value, select.custom_id, stranger
        )
        assert len(clicks.tasks) > before
        await finish(clicks.tasks[-1])
        return view, replies, stranger

    view, replies, stranger = run(scenario())

    assert replies == ["This belongs to someone else."]
    assert stranger.deferred == 0
    assert level_of(view) == ("Radiohead", None)


def reset_error():
    return aiohttp.ClientOSError(104, "Connection reset by peer")


def not_found():
    return discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), "expired"
    )


async def attempt(coro):
    """Awaits `coro`; returns the exception it raised, or None."""
    try:
        await coro
    except BaseException as e:
        return e
    return None


def test_descend_edit_reset_is_undone_and_next_pick_opens_it():
    async def scenario():
        view = make_view()
        resolved = stub_enrichment(view)
        interaction = FakeInteraction(fail=True, edit_error=reset_error())
        raised = await attempt(view.descend(interaction, "Radiohead"))
        cursor = level_of(view)
        await view.descend(FakeInteraction(), "The Beatles")
        return view, resolved, raised, cursor

    view, resolved, raised, cursor = run(scenario())

    assert isinstance(raised, aiohttp.ClientOSError)
    assert cursor == (None, None)
    assert level_of(view) == ("The Beatles", None)
    assert resolved == [("The Beatles", None)]


def test_back_edit_reset_is_undone():
    async def scenario():
        view = make_view()
        resolved = stub_enrichment(view)
        await view.descend(FakeInteraction(), "Radiohead")
        await view.descend(FakeInteraction(), "OK Computer")
        resolved.clear()
        interaction = FakeInteraction(fail=True, edit_error=reset_error())
        raised = await attempt(view.go_back(interaction))
        return view, resolved, raised

    view, resolved, raised = run(scenario())

    assert isinstance(raised, aiohttp.ClientOSError)
    assert level_of(view) == ("Radiohead", "OK Computer")
    assert resolved == []


def test_page_turn_edit_reset_is_undone():
    async def scenario():
        view = make_view(PAGED)
        interaction = FakeInteraction(fail=True, edit_error=reset_error())
        raised = await attempt(view.turn_page(interaction, 1))
        page = view.cursor.page
        await view.turn_page(FakeInteraction(), 1)
        return view, raised, page

    view, raised, page = run(scenario())

    assert isinstance(raised, aiohttp.ClientOSError)
    assert page == 0
    assert view.cursor.page == 1


def test_descend_cancelled_edit_is_undone():
    async def scenario():
        view = make_view()
        resolved = stub_enrichment(view)
        interaction = FakeInteraction(
            fail=True, edit_error=asyncio.CancelledError()
        )
        raised = await attempt(view.descend(interaction, "Radiohead"))
        return view, resolved, raised

    view, resolved, raised = run(scenario())

    assert isinstance(raised, asyncio.CancelledError)
    assert level_of(view) == (None, None)
    assert resolved == []


def test_failed_clamped_page_turn_is_undone():
    async def scenario():
        view = make_view(PAGED)
        await view.turn_page(FakeInteraction(), 1)
        last = view.cursor.page
        failing = FakeInteraction(fail=True)
        await view.turn_page(failing, 5)
        return last, view.cursor.page

    last, page = run(scenario())

    assert last == 1
    assert page == 1


def test_enrichment_in_flight_still_lands_after_failed_page_turn():
    async def scenario():
        view = make_view()
        gate = asyncio.Event()
        stub_enrichment(view, {("Radiohead", None): Gated(gate, COVER)})
        entering = FakeInteraction()
        entry = asyncio.create_task(view.descend(entering, "Radiohead"))
        await settle()
        failing = FakeInteraction(fail=True)
        await view.turn_page(failing, 1)
        gate.set()
        await entry
        return entering

    entering = run(scenario())

    assert carries_enrichment(entering.edits[-1])


@pytest.mark.parametrize(
    "click, error",
    [
        ("descend", reset_error),
        ("back", not_found),
        ("page", reset_error),
    ],
)
def test_failed_defer_leaves_cursor_unchanged(click, error):
    async def scenario():
        view = make_view(PAGED if click == "page" else INDEX)
        resolved = stub_enrichment(view)
        if click == "back":
            await view.descend(FakeInteraction(), "Radiohead")
            resolved.clear()
        before = (*level_of(view), view.cursor.page)
        interaction = FakeInteraction(defer_error=error())
        if click == "descend":
            coro = view.descend(interaction, "Radiohead")
        elif click == "back":
            coro = view.go_back(interaction)
        else:
            coro = view.turn_page(interaction, 1)
        raised = await attempt(coro)
        after = (*level_of(view), view.cursor.page)
        return before, after, raised, resolved

    before, after, raised, resolved = run(scenario())

    assert isinstance(raised, (aiohttp.ClientOSError, discord.NotFound))
    assert after == before
    assert resolved == []


def utf16_units(text):
    return len(text.encode("utf-16-le")) // 2


def has_lone_surrogate(text):
    try:
        text.encode("utf-16-le")
    except UnicodeEncodeError:
        return True
    return "\ufffd" in text


@pytest.mark.parametrize("level", ["artist", "album"])
@pytest.mark.parametrize("name", ["x" * 255, "x" + "\U0001f600" * 255])
def test_long_name_title_fits_embed_title_limit(level, name):
    index = {name: {"Album": [make_song("S")]}}
    if level == "album":
        index = {"Artist": {name: [make_song("S")]}}
    view = make_view(index)
    view.cursor.descend(name if level == "artist" else "Artist")
    if level == "album":
        view.cursor.descend(name)

    title = view.embed().title

    assert len(title) <= 256
    assert utf16_units(title) <= 256
    assert title.endswith("…")
    assert not has_lone_surrogate(title)


def test_trim_drops_whole_char_when_budget_ends_in_surrogate_pair():
    text = "x" + "\U0001f600" * 10

    result = library_commands._trim(text, 4)

    assert result == "x\U0001f600\u2026"


def test_trim_cuts_bmp_text_at_the_limit():
    assert library_commands._trim("abcdef", 4) == "abcd\u2026"


def test_trim_returns_text_within_budget_unchanged():
    assert library_commands._trim("abcd", 4) == "abcd"
    assert library_commands._trim("\U0001f600\U0001f600", 4) == (
        "\U0001f600\U0001f600"
    )


def test_trim_counts_a_lone_surrogate_as_one_unit():
    lone = "\udc80"  # an undecodable filename byte (surrogateescape)

    assert library_commands._trim("ab" + lone, 3) == "ab" + lone
    assert library_commands._trim("ab" + lone + "cd", 3) == ("ab" + lone + "…")


def test_footer_breadcrumb_is_not_trimmed():
    name = "x" * 255
    view = make_view({name: {"Album": [make_song("S")]}})
    view.cursor.descend(name)

    assert name in view.footer()


@pytest.mark.parametrize(
    "value, expected",
    [
        (999, "999"),
        (1_500, "1.5K"),
        (999_949, "999.9K"),
        (999_950, "1M"),
        (1_204_338_291, "1.2B"),
        (999_999_999, "1B"),
    ],
)
def test_fmt_count_rounds_before_choosing_unit(value, expected):
    assert library_commands._fmt_count(value) == expected


# --- Timeout ---


class Followup:
    """A click's followup webhook: records edit_message(id, ...)."""

    def __init__(self, fail=False):
        self.edits = []
        self.fail = fail

    async def edit_message(self, message_id, **kwargs):
        if self.fail:
            raise http_error()
        self.edits.append((message_id, kwargs))


class ClickInteraction:
    def __init__(self, user_id=AUTHOR_ID, message_id=MESSAGE_ID, fail=False):
        self.user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(id=message_id)
        self.followup = Followup(fail=fail)
        self.original_edits = []
        self.sent = []
        self.deferred = 0

        async def send_message(*args, **kwargs):
            self.sent.append(args)

        async def defer():
            self.deferred += 1

        self.response = SimpleNamespace(
            send_message=send_message,
            defer=defer,
            is_done=lambda: bool(self.sent) or bool(self.deferred),
        )

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)


def timeout_message(cls, fail=False):
    """A `cls` (Message / WebhookMessage) that records its edits."""

    class Recording(cls):
        def __init__(self):
            self.edits = []

        async def edit(self, **kwargs):
            if fail:
                raise http_error()
            self.edits.append(kwargs)

    return Recording()


def timeout_view(message=None, command_interaction=None):
    view = make_view()
    view.ctx.interaction = command_interaction
    view.message = message
    return view


def test_timeout_after_a_click_edits_by_id_through_its_followup():
    async def go():
        view = timeout_view(SimpleNamespace())
        click = ClickInteraction()
        assert await view.interaction_check(click)
        await view.on_timeout()
        return view, click

    view, click = run(go())
    assert click.followup.edits == [(MESSAGE_ID, {"view": view})]
    assert click.original_edits == []
    assert all(item.disabled for item in view.children)


def test_timeout_after_a_bulk_queue_click_edits_the_view_not_the_placeholder(
    monkeypatch,
):
    async def refuse(ctx):
        raise CheckError("nope")

    monkeypatch.setattr(library_commands, "play_check", refuse)

    async def go():
        view = timeout_view(SimpleNamespace())
        click = ClickInteraction()
        assert await view.interaction_check(click)
        await view.queue(click, [("A", "B", "c.mp3")], "test")
        # the click's original response is now the "Queueing..." message
        assert click.sent == [("Queueing...",)]
        await view.on_timeout()
        return view, click

    view, click = run(go())
    assert click.followup.edits == [(MESSAGE_ID, {"view": view})]
    assert all("view" not in edit for edit in click.original_edits)


def test_timeout_uses_the_later_of_two_accepted_clicks():
    async def go():
        view = timeout_view(SimpleNamespace())
        first, second = ClickInteraction(), ClickInteraction()
        assert await view.interaction_check(first)
        assert await view.interaction_check(second)
        await view.on_timeout()
        return first, second

    first, second = run(go())
    assert first.followup.edits == []
    assert len(second.followup.edits) == 1


def test_timeout_never_uses_a_non_owner_click():
    async def go():
        view = timeout_view(SimpleNamespace())
        owner, stranger = ClickInteraction(), ClickInteraction(user_id=99)
        assert await view.interaction_check(owner)
        assert not await view.interaction_check(stranger)
        await view.on_timeout()
        return owner, stranger

    owner, stranger = run(go())
    assert stranger.followup.edits == []
    assert len(owner.followup.edits) == 1


def test_timeout_never_uses_a_click_turned_away_as_busy():
    async def go():
        view = timeout_view(SimpleNamespace())
        owner, busy = ClickInteraction(), ClickInteraction()
        assert await view.interaction_check(owner)
        with view.busy():
            assert not await view.interaction_check(busy)
        await view.on_timeout()
        return owner, busy

    owner, busy = run(go())
    assert busy.followup.edits == []
    assert len(owner.followup.edits) == 1


def test_timeout_edits_a_webhook_message_view_through_the_click_followup():
    async def go():
        message = timeout_message(discord.WebhookMessage)
        view = timeout_view(message)
        click = ClickInteraction()
        assert await view.interaction_check(click)
        await view.on_timeout()
        return message, click, view

    message, click, view = run(go())
    assert message.edits == []
    assert click.followup.edits == [(MESSAGE_ID, {"view": view})]


def test_timeout_without_a_click_edits_a_webhook_message_directly():
    async def go():
        message = timeout_message(discord.WebhookMessage)
        view = timeout_view(message)
        await view.on_timeout()
        return message, view

    message, view = run(go())
    assert message.edits == [{"view": view}]


def test_timeout_without_a_click_edits_the_original_response():
    async def go():
        command = ClickInteraction()
        view = timeout_view(SimpleNamespace(), command)
        await view.on_timeout()
        return command, view

    command, view = run(go())
    assert command.original_edits == [{"view": view}]


def test_timeout_after_a_click_still_edits_a_prefix_message_directly():
    async def go():
        message = timeout_message(discord.Message)
        view = timeout_view(message)
        click = ClickInteraction()
        assert await view.interaction_check(click)
        await view.on_timeout()
        return message, click, view

    message, click, view = run(go())
    assert message.edits == [{"view": view}]
    assert click.followup.edits == []


def test_timeout_swallows_a_failed_followup_edit():
    async def go():
        view = timeout_view(SimpleNamespace())
        assert await view.interaction_check(ClickInteraction(fail=True))
        await view.on_timeout()
        return view

    assert all(item.disabled for item in run(go()).children)
