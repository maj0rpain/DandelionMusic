import asyncio
import difflib
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from config import config
from musicbot.audiotags import nice_title, read_tags


class LibrarySong(NamedTuple):
    filename: str
    title: str
    duration: Optional[int]  # seconds
    year: Optional[int]
    genre: Optional[str]
    fmt: str  # file extension, uppercased: "FLAC", "MP3", ...
    bitrate: Optional[int]  # bits per second
    sample_rate: Optional[int]  # Hz
    bit_depth: Optional[int]  # bits per sample


LibraryIndex = Dict[str, Dict[str, List[LibrarySong]]]

_index: LibraryIndex = {}


def _safe_iterdir(path: Path) -> List[Path]:
    """sorted(path.iterdir()), but a single unreadable directory
    (permission-restricted folder, NAS metadata dirs, a stale network
    mount, etc.) is skipped with a warning instead of crashing the
    whole index build - which would otherwise crash bot startup
    (build_index_async() is awaited directly from setup_hook())."""
    try:
        return sorted(path.iterdir())
    except OSError as e:
        print(
            f"library: skipping unreadable directory {path}: {e}",
            file=sys.stderr,
        )
        return []


def _make_song(path: Path) -> LibrarySong:
    """One tag read per file, reused for both the display title and
    the browse-time statistics - the walk is already the expensive
    part of an index build, so nothing here costs extra I/O."""
    tags = read_tags(path)
    return LibrarySong(
        filename=path.name,
        title=nice_title(tags, fallback=path.stem),
        duration=tags.duration,
        year=tags.year,
        genre=tags.genre,
        fmt=path.suffix.lstrip(".").upper(),
        bitrate=tags.bitrate,
        sample_rate=tags.sample_rate,
        bit_depth=tags.bit_depth,
    )


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
        for artist_dir in _safe_iterdir(root):
            if not artist_dir.is_dir():
                continue
            albums: Dict[str, List[LibrarySong]] = {}
            for album_dir in _safe_iterdir(artist_dir):
                if not album_dir.is_dir():
                    continue
                files = sorted(
                    f
                    for f in _safe_iterdir(album_dir)
                    if f.is_file()
                    and f.name.lower().endswith(config.LIBRARY_EXTENSIONS)
                )
                # sorted by filename (not the tag-derived title) so
                # track-number-prefixed filenames ("01 - ...", "02 -
                # ...") keep their natural album order
                songs = [_make_song(f) for f in files]
                if songs:
                    albums[album_dir.name] = songs
            if albums:
                new_index[artist_dir.name] = albums

    _index = new_index
    return _index


def get_index() -> LibraryIndex:
    return _index


class SearchResult(NamedTuple):
    """One ranked match. `album` is None for an artist hit and
    `filename` is set only for a song hit, so a result always carries
    exactly the path components needed to look it back up in the
    index."""

    kind: str  # "artist", "album" or "song"
    artist: str
    album: Optional[str]
    filename: Optional[str]
    label: str  # display text, without the kind emoji
    score: float


# Below this a match is noise rather than a near miss - without a
# floor, a query like "zzzzz" would still return whatever five entries
# happened to share a couple of letters with it.
_SCORE_FLOOR = 0.45

# Tie-break order at equal score: a query matching an album and one of
# its own tracks equally well almost always meant the album, and an
# artist hit (the broadest thing to queue) comes last.
_KIND_ORDER = {"album": 0, "song": 1, "artist": 2}


# `query` reaches search() from a consume-rest command parameter, so a
# prefix invocation can hand it most of a 2000-character message.
# SequenceMatcher is O(len(query) * len(candidate)) and runs once per
# index entry, so an unbounded query is a CPU amplifier any guild
# member could fire repeatedly at the executor the bot also builds its
# index and resolves metadata on: measured against a 53k-entry
# library, 10 characters costs 0.5s and 1900 costs 20s. Nothing anyone
# means to search for is this long.
_MAX_QUERY_LEN = 100


def _score(
    matcher: difflib.SequenceMatcher, query: str, candidate: str
) -> float:
    """`query` must already be casefolded, and `matcher` must already
    have it set as its *second* sequence - SequenceMatcher caches its
    index of that one, so keeping the query there and varying the
    candidate builds the cache once per search instead of once per
    entry (3.6x at the query cap, and the score is unchanged either
    way - ratio() is symmetric here).

    difflib's ratio alone under-rates a short query against a long
    name - "computer" against "OK Computer" is only 0.63, and falls
    further the longer the album title gets - so a containment hit
    gets a floor of its own, scaled by how much of the candidate the
    query accounts for. An exact match therefore scores 1.0."""
    candidate = candidate.casefold()
    if not candidate:
        return 0.0
    matcher.set_seq1(candidate)
    ratio = matcher.ratio()
    if query in candidate:
        # weighted towards coverage rather than a flat containment
        # bonus: one letter appearing inside a six-letter title is a
        # far weaker signal than eight of eleven characters, and a
        # generous flat base would rank the former above a genuine
        # near-miss elsewhere in the library
        return max(ratio, 0.6 + 0.4 * len(query) / len(candidate))
    return ratio


def search(
    index: LibraryIndex, query: str, limit: int = 5
) -> List[SearchResult]:
    """Ranks artists, albums and songs against one query string and
    returns the closest `limit` matches, best first. Synchronous -
    coroutine callers must use search_async()."""
    query = query.casefold().strip()[:_MAX_QUERY_LEN]
    if not query:
        return []

    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(query)

    # scored and filtered as we go: a large library holds tens of
    # thousands of entries and only a handful ever clear the floor, so
    # nothing but a survivor is worth building a SearchResult (and its
    # label) for
    matches: List[SearchResult] = []

    for artist, albums in index.items():
        score = _score(matcher, query, artist)
        if score >= _SCORE_FLOOR:
            matches.append(
                SearchResult("artist", artist, None, None, artist, score)
            )
        for album, songs in albums.items():
            score = _score(matcher, query, album)
            if score >= _SCORE_FLOOR:
                matches.append(
                    SearchResult(
                        "album",
                        artist,
                        album,
                        None,
                        f"{album} \u2014 {artist}",
                        score,
                    )
                )
            for song in songs:
                score = _score(matcher, query, song.title)
                if score >= _SCORE_FLOOR:
                    matches.append(
                        SearchResult(
                            "song",
                            artist,
                            album,
                            song.filename,
                            f"{song.title} \u2014 {artist}",
                            score,
                        )
                    )

    matches.sort(
        key=lambda r: (-r.score, _KIND_ORDER[r.kind], r.label.casefold())
    )
    return matches[:limit]


async def search_async(
    index: LibraryIndex, query: str, limit: int = 5
) -> List[SearchResult]:
    """Coroutine callers must use this, not search() directly - it
    scores every entry in the index, which is tens of thousands of
    SequenceMatcher ratios on a large library, and this codebase's
    rule (see loader.py's _run_sync) is that blocking work never runs
    directly on the event loop."""
    return await asyncio.get_running_loop().run_in_executor(
        None, search, index, query, limit
    )


def song_path(artist: str, album: str, filename: str) -> Path:
    return (
        Path(config.MUSIC_LIBRARY_PATH).resolve() / artist / album / filename
    )


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


class LevelStats(NamedTuple):
    """Aggregate facts about one browse level, derived purely from the
    in-memory index - no filesystem or network access. Every field
    except `tracks` is optional/empty when the underlying files carry
    no usable tags."""

    albums: Optional[int]  # None at the album level
    tracks: int
    runtime: Optional[int]  # seconds
    year_min: Optional[int]
    year_max: Optional[int]
    formats: Tuple[str, ...]  # most common first
    quality: Optional[str]  # stream descriptor, album level
    genres: Tuple[str, ...]  # up to three, most common first


def _quality(songs: List[LibrarySong]) -> Optional[str]:
    """Short stream descriptor for one album - "16-bit / 44.1 kHz" for
    lossless, "320 kbps" for lossy. Picks the most common values so a
    single oddly-encoded bonus track doesn't decide the label."""
    depths = Counter(s.bit_depth for s in songs if s.bit_depth)
    rates = Counter(s.sample_rate for s in songs if s.sample_rate)
    if depths and rates:
        depth = depths.most_common(1)[0][0]
        rate = rates.most_common(1)[0][0] / 1000
        return f"{depth}-bit / {rate:g} kHz"
    bitrates = [s.bitrate for s in songs if s.bitrate]
    if bitrates:
        return f"{round(sum(bitrates) / len(bitrates) / 1000)} kbps"
    return None


def _aggregate(
    songs: List[LibrarySong], albums: Optional[int] = None
) -> LevelStats:
    durations = [s.duration for s in songs if s.duration]
    years = [s.year for s in songs if s.year]
    formats = Counter(s.fmt for s in songs if s.fmt)
    genres = Counter(
        s.genre.strip() for s in songs if s.genre and s.genre.strip()
    )
    return LevelStats(
        albums=albums,
        tracks=len(songs),
        runtime=sum(durations) if durations else None,
        year_min=min(years) if years else None,
        year_max=max(years) if years else None,
        formats=tuple(fmt for fmt, _ in formats.most_common()),
        quality=_quality(songs),
        genres=tuple(genre for genre, _ in genres.most_common(3)),
    )


def artist_stats(index: LibraryIndex, artist: str) -> LevelStats:
    albums = index.get(artist, {})
    songs = [song for album in albums.values() for song in album]
    return _aggregate(songs, albums=len(albums))


def album_stats(index: LibraryIndex, artist: str, album: str) -> LevelStats:
    return _aggregate(index.get(artist, {}).get(album, []))
