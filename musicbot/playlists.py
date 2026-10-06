"""Saved playlists: the one module that reads and writes them.

A saved playlist is a ``SavedPlaylist`` row whose ``songs_json`` holds
a JSON list of ``{"url": ..., "title": ...}`` objects. That format is
encoded and decoded only here (outside the legacy migration in
``musicbot.settings``), so callers deal in ``PlaylistEntry`` records
and never in the blob. Every operation on one saved playlist names it
by a ``PlaylistRef``; ``get`` hands back a ``PlaylistContents``, so the
row itself never leaves this module.

Failures the caller must tell apart are raised as the exceptions
below; ``get`` returns ``None`` for a missing playlist instead.
"""

import json
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

from sqlalchemy import delete as sql_delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from musicbot.settings import SavedPlaylist

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class PlaylistEntry:
    url: str
    title: Optional[str]


@dataclass(frozen=True)
class PlaylistRef:
    """Names a saved playlist without holding its row."""

    guild_id: str
    name: str


@dataclass(frozen=True)
class PlaylistContents:
    """A saved playlist as ``get`` loads it: its ref and its entries."""

    ref: PlaylistRef
    entries: List[PlaylistEntry]


class PlaylistError(Exception):
    pass


class PlaylistNotFound(PlaylistError):
    pass


class PlaylistExists(PlaylistError):
    pass


class InvalidPosition(PlaylistError):
    def __init__(self, size: int):
        super().__init__(f"position out of range 1..{size}")
        self.size = size


class OnlySongInPlaylist(PlaylistError):
    pass


def _decode(songs_json: str) -> List[PlaylistEntry]:
    return [
        PlaylistEntry(song["url"], song["title"])
        for song in json.loads(songs_json)
    ]


def _encode(entries: Iterable[PlaylistEntry]) -> str:
    return json.dumps(
        [{"url": entry.url, "title": entry.title} for entry in entries]
    )


def _lookup(playlist: PlaylistRef):
    return (
        select(SavedPlaylist)
        .where(SavedPlaylist.guild_id == str(playlist.guild_id))
        .where(SavedPlaylist.name == playlist.name)
    )


async def get(
    session_factory: SessionFactory, playlist: PlaylistRef
) -> Optional[PlaylistContents]:
    async with session_factory() as session:
        row = (await session.execute(_lookup(playlist))).scalar_one_or_none()
        if row is None:
            return None
        return PlaylistContents(playlist, _decode(row.songs_json))


async def list_names(
    session_factory: SessionFactory, guild_id: str, prefix: str = ""
) -> List[str]:
    query = select(SavedPlaylist.name).where(
        SavedPlaylist.guild_id == str(guild_id)
    )
    if prefix:
        query = query.where(SavedPlaylist.name.startswith(prefix))
    async with session_factory() as session:
        return list((await session.execute(query)).scalars().all())


async def save(
    session_factory: SessionFactory,
    playlist: PlaylistRef,
    songs: Iterable[PlaylistEntry],
) -> None:
    async with session_factory() as session:
        session.add(
            SavedPlaylist(
                guild_id=str(playlist.guild_id),
                name=playlist.name,
                songs_json=_encode(songs),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            raise PlaylistExists(playlist.name) from None


async def delete(
    session_factory: SessionFactory, playlist: PlaylistRef
) -> None:
    async with session_factory() as session:
        result = await session.execute(
            sql_delete(SavedPlaylist)
            .where(SavedPlaylist.guild_id == str(playlist.guild_id))
            .where(SavedPlaylist.name == playlist.name)
        )
        await session.commit()
    if result.rowcount == 0:
        raise PlaylistNotFound(playlist.name)


async def _update(
    session_factory: SessionFactory,
    playlist: PlaylistRef,
    change: Callable[[List[PlaylistEntry]], None],
) -> None:
    """Loads the playlist's entries, lets ``change`` edit them in
    place (or raise), and stores the result. A change that leaves
    the entries as they were writes nothing."""
    async with session_factory() as session:
        row = (await session.execute(_lookup(playlist))).scalar_one_or_none()
        if row is None:
            raise PlaylistNotFound(playlist.name)
        songs = _decode(row.songs_json)
        before = list(songs)
        change(songs)
        if songs == before:
            return
        row.songs_json = _encode(songs)
        await session.commit()


def _check_position(songs: List[PlaylistEntry], *positions: int) -> None:
    if min(positions) <= 0 or max(positions) > len(songs):
        raise InvalidPosition(len(songs))


async def add_songs(
    session_factory: SessionFactory,
    playlist: PlaylistRef,
    new_songs: Iterable[PlaylistEntry],
) -> None:
    new_songs = list(new_songs)
    await _update(
        session_factory, playlist, lambda songs: songs.extend(new_songs)
    )


async def remove_song(
    session_factory: SessionFactory, playlist: PlaylistRef, position: int
) -> None:
    """Removes the song at 1-based ``position``."""

    def change(songs):
        _check_position(songs, position)
        if len(songs) == 1:
            raise OnlySongInPlaylist(playlist.name)
        del songs[position - 1]

    await _update(session_factory, playlist, change)


async def move_song(
    session_factory: SessionFactory,
    playlist: PlaylistRef,
    source: int,
    destination: int,
) -> None:
    """Moves the song at 1-based ``source`` to ``destination``."""

    def change(songs):
        _check_position(songs, source, destination)
        songs.insert(destination - 1, songs.pop(source - 1))

    await _update(session_factory, playlist, change)


async def set_title(
    session_factory: SessionFactory,
    playlist: PlaylistRef,
    url: str,
    title: Optional[str],
) -> None:
    """Sets the title of every entry with ``url``. A playlist that no
    longer exists is left alone."""

    def change(songs):
        for i, song in enumerate(songs):
            if song.url == url:
                songs[i] = PlaylistEntry(url, title)

    try:
        await _update(session_factory, playlist, change)
    except PlaylistNotFound:
        pass
