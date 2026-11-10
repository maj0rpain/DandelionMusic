import asyncio
import difflib
import heapq
import re
import sys
from collections import Counter
import os
from pathlib import Path, PurePath, PureWindowsPath
from typing import Dict, List, NamedTuple, Optional, Tuple, TypeVar
from urllib.parse import unquote_to_bytes

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
# The configured library root, resolved once per index build (off the
# event loop); None when there is no usable library.
_root: Optional[Path] = None
# bumped by build_index right after a new index is swapped in
_generation = 0


_WALK_WARNING = "library: skipping unreadable"
_TRACK_WARNING = "library: cannot read track"


def _guarded(operation, path: Path, prefix: str, fallback):
    """operation(path), but an OSError (permission-restricted folder,
    NAS metadata dirs, a stale network mount, etc.) prints
    "<prefix> <path>: <error>" to stderr and returns the fallback
    instead of crashing the whole index build. Startup additionally
    tolerates a failed build (setup_hook catches it and starts with an
    empty index)."""
    try:
        return operation(path)
    except OSError as e:
        print(f"{prefix} {path}: {e}", file=sys.stderr)
        return fallback


def _safe_iterdir(path: Path) -> List[Path]:
    """sorted(path.iterdir()); a directory that cannot be listed is
    skipped with a warning."""
    return _guarded(lambda p: sorted(p.iterdir()), path, _WALK_WARNING, [])


def _safe_is_dir(path: Path) -> bool:
    """path.is_dir(); an entry that cannot be stat'ed counts as
    absent, with a warning."""
    return _guarded(Path.is_dir, path, _WALK_WARNING, False)


def _safe_is_file(path: Path) -> bool:
    """path.is_file(); an entry that cannot be stat'ed counts as
    absent, with a warning."""
    return _guarded(Path.is_file, path, _WALK_WARNING, False)


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
    global _index, _root, _generation
    new_index: LibraryIndex = {}
    root = (
        Path(config.MUSIC_LIBRARY_PATH) if config.MUSIC_LIBRARY_PATH else None
    )
    root_is_dir = root is not None and _safe_is_dir(root)

    new_root: Optional[Path] = None
    if root_is_dir:
        for artist_dir in _safe_iterdir(root):
            if not _safe_is_dir(artist_dir):
                continue
            albums: Dict[str, List[LibrarySong]] = {}
            for album_dir in _safe_iterdir(artist_dir):
                if not _safe_is_dir(album_dir):
                    continue
                files = sorted(
                    f
                    for f in _safe_iterdir(album_dir)
                    if _safe_is_file(f)
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
        try:
            new_root = root.resolve()
        except OSError:
            new_root = None

    _index = new_index
    _root = new_root
    _generation += 1
    return _index


def get_index() -> LibraryIndex:
    return _index


def index_generation() -> int:
    """How many indexes build_index has swapped in; it rises with each
    build, after get_index() already returns the new index."""
    return _generation


def albums_of(index: LibraryIndex, artist: str) -> List[str]:
    """The artist's album names in browse order (str.casefold); empty
    for a missing artist. The one album-ordering rule, shared by the
    browser's listing, sample_track and tracks_for (the discography
    queue)."""
    return sorted(index.get(artist, {}), key=str.casefold)


def sample_track(
    index: LibraryIndex, artist: str, album: Optional[str]
) -> Optional[Tuple[str, str, str]]:
    """One track to read tags and artwork off, for a scope of an index
    snapshot: the album's first song at the album level, the first song
    of the artist's first album (in albums_of order, as the
    browser lists them) at the artist level. None for a missing artist
    or album, or one with no songs."""
    albums = index.get(artist)
    if not albums:
        return None
    if album is None:
        album = albums_of(index, artist)[0]
    songs = albums.get(album)
    if not songs:
        return None
    return (artist, album, songs[0].filename)


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
# prefix invocation can hand it most of a 2000-character message, and
# nothing anyone means to search for is that long.
#
# This used to be the defence against a CPU amplifier: SequenceMatcher
# is O(len(query) * len(candidate)) and ran in full against every index
# entry, so against a 53k-entry library 10 characters cost 0.5s and
# 1900 cost 20s - something any guild member could fire repeatedly.
# That is no longer the shape of the cost. Now that _score() screens on
# difflib's cheap bounds, a very long query is the *cheapest* case,
# because real_quick_ratio() rules out an ordinary-length candidate on
# the length difference alone in O(1): the same 1900 characters
# measure at 45ms, against 81ms for a short query. The cap stays for
# what it still does - bounding the work over whatever candidates do
# survive screening, and bounding what a caller can make the bot hold
# - but it is no longer what keeps a long query cheap.
_MAX_QUERY_LEN = 100


def _word_pattern(query: str) -> Optional[re.Pattern]:
    r"""A \b-anchored matcher for the query, or None when it doesn't
    both start and end with word characters - \b sits between a word
    and a non-word character, so a query like "!!!" (a real band name)
    could never match one and has to fall through to plain
    containment."""
    if not re.match(r"\w", query) or not re.search(r"\w\Z", query):
        return None
    return re.compile(rf"\b{re.escape(query)}\b")


def _with_bonus(matcher: difflib.SequenceMatcher, bonus: float) -> float:
    """max(ratio, bonus) for the two bonus tiers, without paying for
    ratio() when it cannot win. real_quick_ratio() is an upper bound on
    it costing O(1) against its O(len * len), so a bonus that already
    exceeds the bound is the answer outright - which is the usual case
    for a short query, where the bonus is high and the ratio is low."""
    if matcher.real_quick_ratio() <= bonus:
        return bonus
    return max(matcher.ratio(), bonus)


def _score(
    matcher: difflib.SequenceMatcher,
    word: Optional[re.Pattern],
    query: str,
    candidate: str,
    cutoff: float,
) -> float:
    """`query` must already be casefolded, and `matcher` must already
    have it set as its *second* sequence - SequenceMatcher caches its
    index of that one, so keeping the query there and varying the
    candidate builds the cache once per search instead of once per
    entry (1.4x on a realistic query, 19x at the cap - quick_ratio()
    below leans on the same cached index, so the screening made this
    orientation matter more than it already did).

    Do not "simplify" this back to SequenceMatcher(None, query,
    candidate) for readability: ratio() is NOT symmetric - measured
    over random pairs it differs about half the time, and it differs
    on ordinary library text too - so swapping the operands silently
    moves borderline entries across _SCORE_FLOOR. The orientation is
    load-bearing, not incidental.

    difflib's ratio alone under-rates a short query against a long
    name - "computer" against "OK Computer" is only 0.63, and falls
    further the longer the album title gets - so a hit inside the
    candidate gets a floor of its own. An exact match scores 1.0.

    Matching a whole word outranks matching a fragment, because
    coverage alone reads a short query as a better hit the shorter the
    candidate is: "the" covers most of "Theo" and little of "The
    Beatles", so without this tier a search for "the" buries every
    real band under whatever short unrelated names happen to contain
    those letters.

    Returns 0.0, rather than the true score, for any candidate that
    cannot reach `cutoff` - see the bounds check below. Callers only
    ever compare the result against _SCORE_FLOOR and rank what
    survives, so a candidate ruled out here is one that could not have
    appeared in the results anyway."""
    candidate = candidate.casefold()
    if not candidate:
        return 0.0
    matcher.set_seq1(candidate)
    # Both bonus tiers float their candidate above the floor on their
    # own (0.9 and 0.6 against 0.45), so they are tested before
    # anything is spent on ruling the candidate out - and they are
    # tested first among themselves in that order, since a word match
    # outranks a fragment. Only the exact score is still in question.
    if word is not None and word.search(candidate):
        return _with_bonus(matcher, 0.9 + 0.1 * len(query) / len(candidate))
    if query in candidate:
        # weighted towards coverage rather than a flat containment
        # bonus: one letter appearing inside a six-letter title is a
        # far weaker signal than eight of eleven characters, and a
        # generous flat base would rank the former above a genuine
        # near-miss elsewhere in the library
        return _with_bonus(matcher, 0.6 + 0.4 * len(query) / len(candidate))
    # Neither bonus applies, so the score is the ratio alone and the
    # only thing that matters is whether it can reach the cutoff.
    # real_quick_ratio() and quick_ratio() are upper bounds on ratio()
    # costing O(1) and O(len) against its O(len * len), so a candidate
    # they rule out never pays for the full comparison - and on a real
    # library that is nearly all of them. This is difflib's own idiom;
    # get_close_matches() screens exactly this way.
    #
    # It is what keeps a search off the GIL: scoring is pure Python and
    # runs once per index entry, so time spent in here is time the
    # event loop - and with it the audio sender thread - is contending
    # for the interpreter, whatever executor the search was handed to.
    if matcher.real_quick_ratio() < cutoff or matcher.quick_ratio() < cutoff:
        return 0.0
    return matcher.ratio()


def search(
    index: LibraryIndex, query: str, limit: int = 5
) -> List[SearchResult]:
    """Ranks artists, albums and songs against one query string and
    returns the closest `limit` matches, best first. Synchronous -
    coroutine callers must use search_async()."""
    query = query.casefold().strip()[:_MAX_QUERY_LEN]
    if not query:
        return []
    # keep() below indexes the heap of the best `limit` scores, which
    # a non-positive limit would leave permanently empty
    if limit <= 0:
        return []

    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(query)
    # compiled once per search, not once per candidate
    word = _word_pattern(query)

    # scored and filtered as we go: a large library holds tens of
    # thousands of entries and only a handful ever clear the floor, so
    # nothing but a survivor is worth building a SearchResult (and its
    # label) for
    matches: List[SearchResult] = []

    # The screening cutoff _score() prunes against. It starts at the
    # floor and rises: once `limit` results are in hand, a candidate
    # that cannot reach the weakest of them cannot appear in the
    # returned list either, so there is no reason to score it exactly.
    # This is what makes the O(1) bound above worth having - against
    # the bare floor it rules out almost nothing for a query of
    # ordinary length, and against a cutoff that has climbed to 0.8 it
    # rules out nearly everything.
    #
    # `best` is a min-heap of the top `limit` scores, so best[0] is
    # that weakest one. The comparison in _score() is strict, so an
    # entry that ties it still survives to be ranked - which matters,
    # because equal scores are separated by the tie-break below rather
    # than by arrival order.
    cutoff = _SCORE_FLOOR
    best: List[float] = []

    def keep(score: float) -> None:
        nonlocal cutoff
        if len(best) < limit:
            heapq.heappush(best, score)
        elif score > best[0]:
            heapq.heapreplace(best, score)
        else:
            return
        if len(best) == limit:
            cutoff = best[0]

    for artist, albums in index.items():
        score = _score(matcher, word, query, artist, cutoff)
        if score >= _SCORE_FLOOR:
            matches.append(
                SearchResult("artist", artist, None, None, artist, score)
            )
            keep(score)
        for album, songs in albums.items():
            score = _score(matcher, word, query, album, cutoff)
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
                keep(score)
            for song in songs:
                score = _score(matcher, word, query, song.title, cutoff)
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
                    keep(score)

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


FILE_SCHEME = "file://"


def library_root() -> Path:
    """The root build_index stored, else the resolved
    config.MUSIC_LIBRARY_PATH."""
    root = _root
    if root is None:
        root = Path(config.MUSIC_LIBRARY_PATH).resolve()
    return root


_P = TypeVar("_P", bound=PurePath)


def _track_path(root: _P, artist: str, album: str, filename: str) -> _P:
    """A library track's path under `root`, in `root`'s flavour."""
    return root / artist / album / filename


def song_path(artist: str, album: str, filename: str) -> Path:
    return _track_path(library_root(), artist, album, filename)


def song_uri(artist: str, album: str, filename: str) -> str:
    return song_path(artist, album, filename).as_uri()


class LocalTrack(NamedTuple):
    """A library track handed to the controller: the `song_uri` it
    loads by and the name it is reported by when it cannot be loaded."""

    uri: str
    name: str

    @classmethod
    def of(cls, artist: str, album: str, filename: str) -> "LocalTrack":
        """The track for an (artist, album, filename) triple."""
        return cls(song_uri(artist, album, filename), filename)


def library_path_from_uri(uri: str, root: PurePath) -> Optional[PurePath]:
    r"""`root / artist / album / filename` for a local-library URI, or
    None if refused. Pure: it touches no filesystem.

    The flavour (POSIX or Windows) is that of `root`, and sets the
    path rules. POSIX: only an empty or `localhost` authority is
    accepted, any other is refused, and only `/` separates segments
    (`\` is an ordinary filename character). Windows: an empty or
    `localhost` authority takes a drive URI (`file:///C:/...`, the
    `/` before the drive dropped); any other authority is a UNC host
    (`file://nas/share/...` is `\\nas\share\...`, never with a drive
    after it); `/` and `\` both separate segments; and comparison
    ignores case. Refused on both: a remainder that still starts with
    `//` once the authority is stripped, the drive-bar form (`C|`,
    also encoded) and a `.` or `..` segment.

    Containment is lexical, on the path as written, not on its
    resolved target. Symlinks under the library root are trusted as
    the owner's own extension of the library (an artist folder linked
    in from another disk), and the kernel follows them when the file is
    opened; the indexer follows them the same way. To keep this a real
    boundary - identify_url sends any user-typed file:// URI here - a
    `.` or `..` segment is refused (`Artist/../../x` would otherwise
    climb out through a symlinked folder), as is any depth other than
    the index's own Artist/Album/file.

    The URI is decoded exactly once, by hand, and both the `.` check and
    the containment checks run on that value: building a path silently
    drops `.`, and urlparse would split a raw `?` or `#` off as
    query/fragment.
    """
    if not uri.startswith(FILE_SCHEME):
        return None
    rest = uri[len(FILE_SCHEME) :]
    slash = rest.find("/")
    if slash < 0:
        return None
    authority, tail = rest[:slash], rest[slash:]
    if tail.startswith("//"):
        return None
    decoded = os.fsdecode(unquote_to_bytes(tail))
    if isinstance(root, PureWindowsPath):
        path = _parse_windows_uri(authority, decoded, type(root))
    else:
        path = _parse_posix_uri(authority, decoded, type(root))
    if path is None:
        return None
    if not path.is_absolute() or not path.is_relative_to(root):
        return None
    try:
        artist, album, filename = path.relative_to(root).parts
    except ValueError:
        return None
    song = _track_path(root, artist, album, filename)
    return song if song == path else None


def _is_local_authority(authority: str) -> bool:
    """Whether a URI authority names this machine (empty or localhost)."""
    return authority in ("", "localhost")


def _has_dot_segment(segments: list[str]) -> bool:
    """Whether any path segment is `.` or `..`."""
    return any(seg in (".", "..") for seg in segments)


def _parse_posix_uri(
    authority: str, decoded: str, flavour: type[PurePath]
) -> Optional[PurePath]:
    """The candidate path, or None if refused."""
    if not _is_local_authority(authority):
        return None
    if _has_dot_segment(decoded.split("/")):
        return None
    return flavour(decoded)


def _parse_windows_uri(
    authority: str, decoded: str, flavour: type[PurePath]
) -> Optional[PurePath]:
    """The candidate path, or None if refused."""
    host = ""
    if not _is_local_authority(authority):
        host = os.fsdecode(unquote_to_bytes(authority))
        if any(c in host for c in "/\\:|"):
            return None
    has_drive_letter = decoded[2:3] == ":"
    if decoded[1:2] in ("\\", "/") or decoded[2:3] == "|":
        return None
    if host and has_drive_letter:
        return None
    segments = [p for seg in decoded.split("/") for p in seg.split("\\")]
    if _has_dot_segment(segments):
        return None
    if host:
        return flavour("\\\\" + host + decoded.replace("/", "\\"))
    if has_drive_letter:
        return flavour(decoded[1:])
    return flavour(decoded)


def track_path_from_uri(uri: str) -> Optional[Path]:
    """The playable path of a local-library URI, or None if refused.

    `library_path_from_uri` does the parsing and every refusal (see
    its doc for the per-flavour authority and separator rules); this
    adds the one filesystem check, that the file exists.
    """
    path = library_path_from_uri(uri, library_root())
    if path is None:
        return None
    path = Path(path)
    is_file = _guarded(Path.is_file, path, _TRACK_WARNING, False)
    return path if is_file else None


def tracks_for(
    index: LibraryIndex,
    artist: str,
    album: Optional[str] = None,
    filename: Optional[str] = None,
) -> List[Tuple[str, str, str]]:
    """The (artist, album, filename) triples one scope stands for: a
    single song when `filename` is given, one album's tracks when only
    `album` is, and the artist's whole discography when neither.

    A song is returned as itself, without being looked up in the
    index. That is load-bearing rather than an oversight: a stale
    entry - a file deleted from disk since the last build_index() -
    has to reach process_local_tracks() so that queue_songs() (see
    commands/library.py) can name it in its "skipped (file not found)"
    report and tell the user to ask the owner to run `d!lib refresh`.
    Dropping it here would give them "Queued 0 song(s)." and no reason
    why.

    `filename` without `album` raises ValueError - the triple it would
    have to build has no album component, so it could not name a file.
    """
    if filename is not None:
        if album is None:
            raise ValueError("tracks_for(): filename requires album")
        return [(artist, album, filename)]
    albums = index.get(artist, {})
    if album is not None:
        return [
            (artist, album, song.filename) for song in albums.get(album, [])
        ]
    return [
        (artist, album_name, song.filename)
        for album_name in albums_of(index, artist)
        for song in albums[album_name]
    ]


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
