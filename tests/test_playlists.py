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
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from musicbot import playlists
from musicbot.playlists import PlaylistEntry, PlaylistRef
from musicbot.settings import SavedPlaylist, run_migrations

GUILD = "123"
MIX = PlaylistRef(GUILD, "mix")


def run(test):
    """Runs ``test(session_factory)`` in one event loop against a fresh
    in-memory database. StaticPool keeps the single connection the
    in-memory database lives in alive across sessions."""

    async def main():
        engine = create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool
        )
        async with engine.connect() as conn:
            await conn.run_sync(run_migrations)
        factory = sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        try:
            return await test(factory)
        finally:
            await engine.dispose()

    return asyncio.run(main())


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

    assert run(test) == [C, B]


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

    assert run(test) == (["jazz", "rap", "rock"], ["rap", "rock"])


def test_delete_removes_the_playlist():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        await playlists.delete(factory, MIX)
        return await playlists.get(factory, MIX)

    assert run(test) is None


def test_set_title_updates_only_the_matching_url():
    async def test(factory):
        await playlists.save(factory, MIX, [A, B])
        await playlists.set_title(factory, MIX, B.url, "New B")
        return await load(factory)

    assert run(test) == [A, PlaylistEntry(B.url, "New B")]


def test_set_title_on_a_missing_playlist_does_nothing():
    async def test(factory):
        await playlists.set_title(
            factory, PlaylistRef(GUILD, "gone"), A.url, "x"
        )
        return await playlists.list_names(factory, GUILD)

    assert run(test) == []


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

    assert run(test) == blob


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

    assert run(test) == [A, C]


def test_a_blob_written_through_the_module_is_todays_format():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        await playlists.add_songs(factory, MIX, [B, C])
        return await stored_json(factory)

    assert run(test) == json.dumps(
        [
            {"url": "https://a.example/1", "title": "A"},
            {"url": "https://b.example/2", "title": "B"},
            {"url": "https://c.example/3", "title": None},
        ]
    )


@pytest.mark.parametrize(
    "operation",
    [
        lambda f: playlists.delete(f, PlaylistRef(GUILD, "gone")),
        lambda f: playlists.add_songs(f, PlaylistRef(GUILD, "gone"), [A]),
        lambda f: playlists.remove_song(f, PlaylistRef(GUILD, "gone"), 1),
        lambda f: playlists.move_song(f, PlaylistRef(GUILD, "gone"), 1, 1),
    ],
    ids=["delete", "add_songs", "remove_song", "move_song"],
)
def test_operations_on_a_missing_playlist_report_not_found(operation):
    async def test(factory):
        with pytest.raises(playlists.PlaylistNotFound):
            await operation(factory)

    run(test)


def test_get_on_a_missing_playlist_returns_none():
    async def test(factory):
        return await playlists.get(factory, PlaylistRef(GUILD, "gone"))

    assert run(test) is None


def test_saving_a_duplicate_name_reports_it_and_keeps_the_original():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        with pytest.raises(playlists.PlaylistExists):
            await playlists.save(factory, MIX, [B])
        return await load(factory)

    assert run(test) == [A]


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

    assert run(test) == (2, [A, B])


def test_removing_the_only_song_is_refused():
    async def test(factory):
        await playlists.save(factory, MIX, [A])
        with pytest.raises(playlists.OnlySongInPlaylist):
            await playlists.remove_song(factory, MIX, 1)
        return await load(factory)

    assert run(test) == [A]


def test_get_returns_the_ref_it_was_given_and_the_saved_entries():

    async def test(factory):
        await playlists.save(factory, MIX, [A, C])
        return await playlists.get(factory, MIX)

    assert run(test) == playlists.PlaylistContents(MIX, [A, C])


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
