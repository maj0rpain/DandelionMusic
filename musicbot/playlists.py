"""Saved playlists: the one module that reads and writes them.

A saved playlist is a ``SavedPlaylist`` row whose ``songs_json`` holds
a JSON list of ``{"url": ..., "title": ...}`` objects. This module is
the only reader and writer of ``SavedPlaylist.songs_json``; it also
reads the legacy bare-string format (an entry stored as just its URL),
and writes today's format at the next edit that changes the playlist.
Callers deal in ``PlaylistEntry`` records and never in the blob.
Every operation on one saved playlist names it
by a ``PlaylistRef``; ``get`` hands back a ``PlaylistContents``, so the
row itself never leaves this module.

Failures the caller must tell apart are raised as the exceptions
below; ``get`` returns ``None`` for a missing playlist instead.
"""

import json
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

from sqlalchemy import delete as sql_delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from musicbot.keyed_locks import KeyedLocks
from musicbot.settings import PLAYLIST_NAME_MAX_LENGTH, SavedPlaylist

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
class SavedEntry:
    """Which saved playlist a queued song came from, and that entry's
    URL exactly as stored (extraction never rewrites it)."""

    ref: PlaylistRef
    url: str


@dataclass(frozen=True)
class PlaylistContents:
    """A saved playlist as ``get`` loads it: its ref and its entries."""

    ref: PlaylistRef
    entries: List[PlaylistEntry]


class SavedPlaylistError(Exception):
    pass


class PlaylistNotFound(SavedPlaylistError):
    pass


class CorruptPlaylist(SavedPlaylistError):
    """The playlist's stored data cannot be read."""


class PlaylistExists(SavedPlaylistError):
    pass


class PlaylistNameTooLong(SavedPlaylistError):
    """A name over `max_length` characters cannot be saved."""

    def __init__(self, name: str, max_length: int):
        super().__init__(f"playlist name over {max_length} characters")
        self.name = name
        self.max_length = max_length


class InvalidPosition(SavedPlaylistError):
    def __init__(self, size: int):
        super().__init__(f"position out of range 1..{size}")
        self.size = size


class OnlySongInPlaylist(SavedPlaylistError):
    pass


# One lock per saved playlist: changes to it run one at a time.
_locks = KeyedLocks()


def _decode_entry(song) -> PlaylistEntry:
    if isinstance(song, str):
        return PlaylistEntry(song, None)
    if isinstance(song, dict) and isinstance(song.get("url"), str):
        title = song.get("title")
        if title is None or isinstance(title, str):
            return PlaylistEntry(song["url"], title)
        raise ValueError(f"entry title is not a string: {song!r}")
    raise ValueError(f"unreadable entry: {song!r}")


def _decode(songs_json: str, playlist: PlaylistRef) -> List[PlaylistEntry]:
    """Raises ``CorruptPlaylist`` (after logging the row) when the blob
    cannot be read; no entry is ever dropped."""
    try:
        songs = json.loads(songs_json)
        if not isinstance(songs, list):
            raise ValueError("not a JSON list")
        return [_decode_entry(song) for song in songs]
    except ValueError as e:
        print(
            f"Unreadable saved playlist {playlist.name!r} "
            f"in guild {playlist.guild_id}: {e}",
            file=sys.stderr,
        )
        raise CorruptPlaylist(playlist.name) from None


def _encode(entries: Iterable[PlaylistEntry]) -> str:
    return json.dumps(
        [{"url": entry.url, "title": entry.title} for entry in entries]
    )


def _matches(playlist: PlaylistRef):
    return (
        SavedPlaylist.guild_id == playlist.guild_id,
        SavedPlaylist.name == playlist.name,
    )


async def _load_row(
    session: AsyncSession, playlist: PlaylistRef
) -> Optional[SavedPlaylist]:
    return (
        await session.execute(select(SavedPlaylist).where(*_matches(playlist)))
    ).scalar_one_or_none()


async def get(
    session_factory: SessionFactory, playlist: PlaylistRef
) -> PlaylistContents:
    async with session_factory() as session:
        row = await _load_row(session, playlist)
        if row is None:
            raise PlaylistNotFound(playlist.name)
        return PlaylistContents(playlist, _decode(row.songs_json, playlist))


async def list_names(
    session_factory: SessionFactory, guild_id: str, prefix: str = ""
) -> List[str]:
    query = select(SavedPlaylist.name).where(
        SavedPlaylist.guild_id == guild_id
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
    if len(playlist.name) > PLAYLIST_NAME_MAX_LENGTH:
        raise PlaylistNameTooLong(playlist.name, PLAYLIST_NAME_MAX_LENGTH)
    async with session_factory() as session:
        session.add(
            SavedPlaylist(
                guild_id=playlist.guild_id,
                name=playlist.name,
                songs_json=_encode(songs),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            # A clash only if the row exists; any other integrity
            # error is not the user's duplicate name.
            await session.rollback()
            async with session_factory() as check:
                exists = await _load_row(check, playlist) is not None
            if exists:
                raise PlaylistExists(playlist.name) from None
            raise


async def delete(
    session_factory: SessionFactory, playlist: PlaylistRef
) -> None:
    async with _locks(playlist), session_factory() as session:
        result = await session.execute(
            sql_delete(SavedPlaylist).where(*_matches(playlist))
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
    async with _locks(playlist), session_factory() as session:
        row = await _load_row(session, playlist)
        if row is None:
            raise PlaylistNotFound(playlist.name)
        songs = _decode(row.songs_json, playlist)
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
