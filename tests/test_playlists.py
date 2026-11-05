"""Saved playlists, through the module that owns them.

Every test runs against a fresh in-memory sqlite database with the
real schema applied, so the stored JSON is the same bytes an existing
deployment holds.
"""

import asyncio
import inspect
import json
import typing

import pytest

from musicbot import playlists, settings
from musicbot.playlists import PlaylistEntry, PlaylistRef
from musicbot.settings import SavedPlaylist
from db_seam import in_memory_db
from helpers import run

GUILD = "123"
MIX = PlaylistRef(GUILD, "mix")
GONE = PlaylistRef(GUILD, "gone")


def run_with_db(test):
    """Runs ``test(session_factory)`` in one event loop against a fresh
    in-memory database."""

    async def main():
        async with in_memory_db(settings) as factory:
            return await test(factory)

    return run(main())


A = PlaylistEntry("https://a.example/1", "A")
B = PlaylistEntry("https://b.example/2", "B")
C = PlaylistEntry("https://c.example/3", None)


async def stored_json(factory, name="mix"):
    async with factory() as session:
        row = await session.get(SavedPlaylist, (GUILD, name))
        return row.songs_json


async def load(factory, name="mix"):
    return (await playlists.get(factory, PlaylistRef(GUILD, name))).entries


def test_save_add_move_remove_and_load_round_trip():
    async def test(factory):
        await playlists.save(factory, MIX, [A, B])
        await playlists.add_songs(factory, MIX, [C])
        await playlists.move_song(factory, MIX, 3, 1)
        await playlists.remove_song(factory, MIX, 2)
        return await load(factory)

    assert run_with_db(test) == [C, B]


def test_list_names_filters_by_guild_and_prefix():
    async def test(factory):
        await playlists.save(factory, PlaylistRef(GUILD, "rock"), [A])
        await playlists.save(factory, PlaylistRef(GUILD, "rap"), [A])
        await playlists.save(factory, PlaylistRef(GUILD, "jazz"), [A])
        await playlists.save(factory, PlaylistRef("999", "rock2"), [A])
        return (
            sorted(await playlists.list_names(factory, GUILD)),
            sorted(await playlists.list_names(factory, GUILD, prefix="r")),
        )

    assert run_with_db(test) == (["jazz", "rap", "rock"], ["rap", "rock"])


def test_delete_removes_the_playlist():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        await playlists.delete(factory, MIX)
        return await playlists.get(factory, MIX)

    assert run_with_db(test) is None


def test_set_title_updates_only_the_matching_url():
    async def test(factory):
        await playlists.save(factory, MIX, [A, B])
        await playlists.set_title(factory, MIX, B.url, "New B")
        return await load(factory)

    assert run_with_db(test) == [A, PlaylistEntry(B.url, "New B")]


def test_set_title_on_a_missing_playlist_does_nothing():
    async def test(factory):
        await playlists.set_title(factory, GONE, A.url, "x")
        return await playlists.list_names(factory, GUILD)

    assert run_with_db(test) == []


def test_set_title_that_changes_nothing_leaves_the_blob_untouched():
    # Same entries as _encode would write, but spaced differently, so a
    # rewrite would show up as changed bytes.
    blob = '[{"url":"https://a.example/1","title":"A"}]'

    async def test(factory):
        async with factory() as session:
            session.add(
                SavedPlaylist(guild_id=GUILD, name="mix", songs_json=blob)
            )
            await session.commit()
        await playlists.set_title(factory, MIX, A.url, "A")
        await playlists.set_title(factory, MIX, "https://other.example", "x")
        return await stored_json(factory)

    assert run_with_db(test) == blob


def test_a_blob_in_todays_format_reads_back():
    blob = (
        '[{"url": "https://a.example/1", "title": "A"}, '
        '{"url": "https://c.example/3", "title": null}]'
    )

    async def test(factory):
        async with factory() as session:
            session.add(
                SavedPlaylist(guild_id=GUILD, name="mix", songs_json=blob)
            )
            await session.commit()
        return await load(factory)

    assert run_with_db(test) == [A, C]


def test_a_blob_written_through_the_module_is_todays_format():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        await playlists.add_songs(factory, MIX, [B, C])
        return await stored_json(factory)

    assert run_with_db(test) == json.dumps(
        [
            {"url": "https://a.example/1", "title": "A"},
            {"url": "https://b.example/2", "title": "B"},
            {"url": "https://c.example/3", "title": None},
        ]
    )


@pytest.mark.parametrize(
    "operation",
    [
        lambda f: playlists.delete(f, GONE),
        lambda f: playlists.add_songs(f, GONE, [A]),
        lambda f: playlists.remove_song(f, GONE, 1),
        lambda f: playlists.move_song(f, GONE, 1, 1),
    ],
    ids=["delete", "add_songs", "remove_song", "move_song"],
)
def test_operations_on_a_missing_playlist_report_not_found(operation):
    async def test(factory):
        with pytest.raises(playlists.PlaylistNotFound):
            await operation(factory)

    run_with_db(test)


def test_get_on_a_missing_playlist_returns_none():
    async def test(factory):
        return await playlists.get(factory, GONE)

    assert run_with_db(test) is None


def test_saving_a_duplicate_name_reports_it_and_keeps_the_original():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        with pytest.raises(playlists.PlaylistExists):
            await playlists.save(factory, MIX, [B])
        return await load(factory)

    assert run_with_db(test) == [A]


@pytest.mark.parametrize(
    "operation",
    [
        lambda f: playlists.remove_song(f, MIX, 0),
        lambda f: playlists.remove_song(f, MIX, 3),
        lambda f: playlists.move_song(f, MIX, 0, 1),
        lambda f: playlists.move_song(f, MIX, 1, 3),
    ],
    ids=["remove-0", "remove-past-end", "move-from-0", "move-past-end"],
)
def test_an_out_of_range_position_reports_the_playlist_size(operation):
    async def test(factory):
        await playlists.save(factory, MIX, [A, B])
        with pytest.raises(playlists.InvalidPosition) as excinfo:
            await operation(factory)
        return excinfo.value.size, await load(factory)

    assert run_with_db(test) == (2, [A, B])


def test_removing_the_only_song_is_refused():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        with pytest.raises(playlists.OnlySongInPlaylist):
            await playlists.remove_song(factory, MIX, 1)
        return await load(factory)

    assert run_with_db(test) == [A]


def test_get_returns_the_ref_it_was_given_and_the_saved_entries():

    async def test(factory):
        await playlists.save(factory, MIX, [A, C])
        return await playlists.get(factory, MIX)

    assert run_with_db(test) == playlists.PlaylistContents(MIX, [A, C])


def test_no_public_function_takes_or_returns_the_row():
    # The row is the module's to hold: callers get PlaylistRef,
    # PlaylistEntry and PlaylistContents, never SavedPlaylist.
    public = [
        value
        for name, value in vars(playlists).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and value.__module__ == playlists.__name__
    ]
    assert public

    def mentions_row(hint):
        return hint is SavedPlaylist or any(
            mentions_row(arg) for arg in typing.get_args(hint)
        )

    leaking = [
        function.__name__
        for function in public
        if any(map(mentions_row, typing.get_type_hints(function).values()))
    ]
    assert leaking == []
    assert not hasattr(playlists, "entries")


class _PausedSession:
    """Proxies a session; right after its first statement returns, says
    so on ``paused`` and waits for ``release``."""

    def __init__(self, inner, paused, release):
        self._inner = inner
        self._paused = paused
        self._release = release
        self._first = True

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)

    async def execute(self, *args, **kwargs):
        result = await self._inner.execute(*args, **kwargs)
        if self._first:
            self._first = False
            self._paused.set()
            await self._release.wait()
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


class Gate:
    """A session factory whose first session is paused (see above)."""

    def __init__(self, factory):
        self._factory = factory
        self.paused = asyncio.Event()
        self.release = asyncio.Event()
        self._used = False

    def __call__(self):
        session = self._factory()
        if self._used:
            return session
        self._used = True
        return _PausedSession(session, self.paused, self.release)


WAIT = 1.0


async def overlap(factory, first, second):
    """Starts ``first(gated_factory)``, pauses it mid-operation, starts
    ``second(factory)`` and gives it a bounded wait, then releases the
    first. Returns (second finished during the wait, first outcome,
    second outcome); an outcome is the exception raised or None."""
    gate = Gate(factory)
    task1 = asyncio.ensure_future(first(gate))
    await gate.paused.wait()
    task2 = asyncio.ensure_future(second(factory))
    done, _ = await asyncio.wait({task2}, timeout=WAIT)
    gate.release.set()
    outcomes = await asyncio.gather(task1, task2, return_exceptions=True)
    return (
        bool(done),
        *[o if isinstance(o, BaseException) else None for o in outcomes],
    )


def test_set_title_overlapping_add_songs_keeps_both_changes():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        await overlap(
            factory,
            lambda f: playlists.set_title(f, MIX, A.url, "New"),
            lambda f: playlists.add_songs(f, MIX, [B]),
        )
        return await load(factory)

    assert run_with_db(test) == [PlaylistEntry(A.url, "New"), B]


@pytest.mark.parametrize(
    "first_operation",
    [
        lambda f: playlists.set_title(f, MIX, A.url, "New"),
        lambda f: playlists.add_songs(f, MIX, [B]),
    ],
    ids=["set_title", "add_songs"],
)
def test_delete_overlapping_edit_leaves_it_deleted(first_operation):
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        _, first, second = await overlap(
            factory,
            first_operation,
            lambda f: playlists.delete(f, MIX),
        )
        return first, second, await playlists.get(factory, MIX)

    assert run_with_db(test) == (None, None, None)


def test_add_songs_overlapping_delete_reports_not_found():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        _, first, second = await overlap(
            factory,
            lambda f: playlists.delete(f, MIX),
            lambda f: playlists.add_songs(f, MIX, [B]),
        )
        return first, second, await playlists.get(factory, MIX)

    first, second, left = run_with_db(test)
    assert first is None
    assert isinstance(second, playlists.PlaylistNotFound)
    assert left is None


def test_an_edit_to_another_playlist_does_not_wait():
    other = PlaylistRef(GUILD, "other")

    async def test(factory):
        await playlists.save(factory, MIX, [A])
        await playlists.save(factory, other, [A])
        finished, first, second = await overlap(
            factory,
            lambda f: playlists.add_songs(f, MIX, [B]),
            lambda f: playlists.add_songs(f, other, [C]),
        )
        return finished, first, second, await load(factory, "other")

    assert run_with_db(test) == (True, None, None, [A, C])


def test_a_failed_move_does_not_block_the_next_edit():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        with pytest.raises(playlists.InvalidPosition):
            await playlists.move_song(factory, MIX, 1, 5)
        await asyncio.wait_for(playlists.add_songs(factory, MIX, [B]), WAIT)
        return await load(factory)

    assert run_with_db(test) == [A, B]
