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

import discord

from musicbot.commands import library as library_commands
from musicbot.commands.library import (
    BackButton,
    LibraryBrowseView,
    LibrarySelect,
    PageButton,
    QueueLevelButton,
)
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
MESSAGE_ID = 4242


class FakeInteraction:
    """Records every edit; raises from it while `fail` is set.

    `hold(n)` holds this interaction's n-th edit (0-based) in flight
    until `release()`: `in_flight` is set once it is waiting. Every
    landed edit is also appended to `log`, when one is shared between
    interactions, as (self, kwargs) - the order edits reached the
    message in. `deferred` counts the interaction's acknowledgements.
    """

    def __init__(self, log=None, clicks=None):
        self.edits = []
        self.fail = False
        self.fail_on = set()
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
            raise http_error()
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


def run(coro_fn):
    # bounded, so a click left waiting forever fails the test rather
    # than hanging the run
    return asyncio.run(asyncio.wait_for(coro_fn(), 5))


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

    def interaction(self, log=None):
        return FakeInteraction(log, clicks=self)

    def click(self, kind, interaction, label=None):
        """Clicks the shown `kind` component; for a Select, the
        option labelled `label`."""
        item = next(i for i in self.shown if isinstance(i, kind))
        if label is not None:
            option = next(o for o in item.options if o.label == label)
            interaction.data = {"values": [option.value]}
        before = len(self.tasks)
        self.store.dispatch_view(item.type.value, item.custom_id, interaction)
        return self.tasks[-1] if len(self.tasks) > before else None


async def finish(*tasks):
    await asyncio.gather(*[t for t in tasks if t is not None])


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
    """Holds the artist level's enrichment edit in flight and
    dispatches `click(clicks, interaction)` meanwhile."""

    async def scenario():
        view = make_view()
        stub_enrichment_by_level(view, {("Radiohead", None): COVER})
        clicks = Clicks(view)
        log = []
        entering = clicks.interaction(log)
        entering.hold(1)
        entry = clicks.click(LibrarySelect, entering, label="Radiohead")
        await entering.in_flight.wait()
        clicking = clicks.interaction(log)
        handler = click(clicks, clicking)
        await settle()
        moved_to = (view.cursor.artist, view.cursor.album)
        page = view.cursor.page
        entering.release()
        await finish(entry, handler)
        return moved_to, page, handler, entering, clicking, log

    return run(scenario)


def test_back_click_during_enrichment_edit_is_accepted():
    moved_to, _, handler, entering, clicking, log = (
        _click_during_enrichment_edit(
            lambda clicks, i: clicks.click(BackButton, i)
        )
    )

    assert handler is not None
    assert moved_to == (None, None)
    assert clicking.deferred == 1
    assert len(entering.edits) == 2
    assert [who for who, _ in log[:3]] == [entering, entering, clicking]
    assert log[1][1]["attachments"][0].filename == "cover.png"


def test_select_click_during_enrichment_edit_is_accepted():
    moved_to, _, handler, entering, clicking, log = (
        _click_during_enrichment_edit(
            lambda clicks, i: clicks.click(
                LibrarySelect, i, label="OK Computer"
            )
        )
    )

    assert handler is not None
    assert moved_to == ("Radiohead", "OK Computer")
    assert len(entering.edits) == 2
    assert [who for who, _ in log[:3]] == [entering, entering, clicking]
    assert log[2][1]["embed"].title != log[1][1]["embed"].title


def test_queue_click_during_enrichment_edit_queues_the_level(monkeypatch):
    queued = []

    async def queue_songs(ctx, interaction, triples, source):
        await interaction.response.defer()
        queued.extend(triples)

    monkeypatch.setattr(library_commands, "queue_songs", queue_songs)

    moved_to, _, handler, _, clicking, _ = _click_during_enrichment_edit(
        lambda clicks, i: clicks.click(QueueLevelButton, i)
    )

    assert handler is not None
    assert clicking.deferred == 1
    assert sorted(f for _, _, f in queued) == [
        "Airbag.flac",
        "Lucky.flac",
        "Zoo.flac",
    ]


def test_page_click_during_enrichment_edit_turns_the_page_once():
    index = {
        "Artist": {f"Album {n:02}": [make_song("Song")] for n in range(30)}
    }

    async def scenario():
        view = make_view(index)
        stub_enrichment_by_level(view, {("Artist", None): COVER})
        clicks = Clicks(view)
        entering = clicks.interaction()
        entering.hold(1)
        entry = clicks.click(LibrarySelect, entering, label="Artist")
        await entering.in_flight.wait()
        clicking = clicks.interaction()
        handler = clicks.click(PageButton, clicking)
        await settle()
        page = view.cursor.page
        entering.release()
        await finish(entry, handler)
        return handler, page, view

    handler, page, view = run(scenario)

    assert handler is not None
    assert page == 1
    assert view.cursor.page == 1


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

    handler, second, view = run(scenario)

    assert handler is not None
    assert second.deferred == 1
    assert (view.cursor.artist, view.cursor.album) == ("Radiohead", None)


PAGED = {f"Artist {n:02}": {"Album": [make_song("Song")]} for n in range(30)}


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

    handler, second, view = run(scenario)

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
        stub_enrichment_by_level(view, {})
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
        picking.data = {
            "values": [
                next(
                    o.value for o in previous.options if o.label == "Artist 03"
                )
            ]
        }
        previous._refresh_state(picking, picking.data)
        await previous.callback(picking)
        return view

    view = run(scenario)

    assert (view.cursor.artist, view.cursor.album) == ("Artist 03", None)


def test_click_after_failed_first_draw_acts_on_the_shown_level():
    async def scenario():
        view = make_view()
        stub_enrichment_by_level(view, {})
        clicks = Clicks(view)
        failing = clicks.interaction()
        failing.fail = True
        await finish(clicks.click(LibrarySelect, failing, label="Radiohead"))
        undone = (view.cursor.artist, view.cursor.album)
        again = clicks.interaction()
        await finish(clicks.click(LibrarySelect, again, label="The Beatles"))
        return undone, view

    undone, view = run(scenario)

    assert undone == (None, None)
    assert (view.cursor.artist, view.cursor.album) == ("The Beatles", None)


def test_click_after_failed_page_turn_acts_on_the_shown_page():
    async def scenario():
        view = make_view(PAGED)
        stub_enrichment_by_level(view, {})
        clicks = Clicks(view)
        failing = clicks.interaction()
        failing.fail = True
        await finish(clicks.click(PageButton, failing))
        again = clicks.interaction()
        await finish(clicks.click(LibrarySelect, again, label="Artist 03"))
        return view

    view = run(scenario)

    assert (view.cursor.artist, view.cursor.album) == ("Artist 03", None)


def test_click_after_failed_enrichment_edit_acts_on_the_shown_level():
    async def scenario():
        view = make_view()
        stub_enrichment_by_level(view, {("Radiohead", None): COVER})
        clicks = Clicks(view)
        entering = clicks.interaction()
        entering.fail_on = {1}
        await finish(clicks.click(LibrarySelect, entering, label="Radiohead"))
        again = clicks.interaction()
        await finish(clicks.click(BackButton, again))
        return view

    view = run(scenario)

    assert (view.cursor.artist, view.cursor.album) == (None, None)


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


def _descend_past_kept_components():
    """A view descended into Radiohead, with the root level's
    components kept from before the descend landed."""

    async def scenario():
        view = make_view()
        stub_enrichment_by_level(view, {})
        clicks = Clicks(view)
        kept = list(clicks.shown)
        await finish(
            clicks.click(
                LibrarySelect, clicks.interaction(), label="Radiohead"
            )
        )
        return view, clicks, kept

    return scenario


def test_component_from_an_earlier_scope_is_acknowledged_and_ignored():
    async def scenario():
        view, clicks, kept = await _descend_past_kept_components()()
        select = next(i for i in kept if isinstance(i, LibrarySelect))
        picking = clicks.interaction()
        picking.data = {"values": ["1"]}
        select._refresh_state(picking, picking.data)
        await select.callback(picking)
        return view, picking

    view, picking = run(scenario)

    assert picking.deferred == 1
    assert (view.cursor.artist, view.cursor.album) == ("Radiohead", None)


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

    view, replies, stranger = run(scenario)

    assert replies == ["This belongs to someone else."]
    assert stranger.deferred == 0
    assert (view.cursor.artist, view.cursor.album) == ("Radiohead", None)
