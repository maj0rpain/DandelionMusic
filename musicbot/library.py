import asyncio
from pathlib import Path
from typing import Dict, List, Tuple

from config import config

LibraryIndex = Dict[str, Dict[str, List[str]]]

_index: LibraryIndex = {}


async def build_index_async() -> LibraryIndex:
    """Coroutine callers (startup, commands) must use this, not
    build_index() directly - it's a blocking filesystem walk, and this
    codebase's rule (see loader.py's _run_sync) is that blocking I/O
    never runs directly on the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, build_index)


def build_index() -> LibraryIndex:
    """Synchronous - only call directly from a thread/process executor
    (via build_index_async) or from non-async test scripts. Walks
    config.MUSIC_LIBRARY_PATH (expected layout:
    Artist/Album/song.ext) and rebuilds the in-memory index.
    Returns the new index."""
    global _index
    new_index: LibraryIndex = {}
    root = (
        Path(config.MUSIC_LIBRARY_PATH) if config.MUSIC_LIBRARY_PATH else None
    )

    if root is not None and root.is_dir():
        for artist_dir in sorted(root.iterdir()):
            if not artist_dir.is_dir():
                continue
            albums: Dict[str, List[str]] = {}
            for album_dir in sorted(artist_dir.iterdir()):
                if not album_dir.is_dir():
                    continue
                songs = sorted(
                    f.name
                    for f in album_dir.iterdir()
                    if f.is_file()
                    and f.name.lower().endswith(config.SUPPORTED_EXTENSIONS)
                )
                if songs:
                    albums[album_dir.name] = songs
            if albums:
                new_index[artist_dir.name] = albums

    _index = new_index
    return _index


def get_index() -> LibraryIndex:
    return _index


def song_path(artist: str, album: str, filename: str) -> Path:
    return Path(config.MUSIC_LIBRARY_PATH) / artist / album / filename


def song_uri(artist: str, album: str, filename: str) -> str:
    return song_path(artist, album, filename).as_uri()


def counts(index: LibraryIndex) -> Tuple[int, int, int]:
    """Returns (artist_count, album_count, song_count)."""
    artist_count = len(index)
    album_count = sum(len(albums) for albums in index.values())
    song_count = sum(
        len(songs) for albums in index.values() for songs in albums.values()
    )
    return artist_count, album_count, song_count
