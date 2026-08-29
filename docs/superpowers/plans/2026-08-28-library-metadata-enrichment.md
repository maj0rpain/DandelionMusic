# Library Browse Metadata Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cover art and a short bio/summary to `d!library browse`'s artist- and album-level screens, sourced from embedded file art, Spotify, and Last.fm, with tag-based query resolution so lookups aren't thrown off by folder naming.

**Architecture:** A new module `musicbot/library_metadata.py` sits alongside `musicbot/library.py` (indexing) and `musicbot/linkutils.py` (existing Spotify integration) as the external-lookup layer, exposing four async functions (`get_artist_photo`, `get_artist_summary`, `get_album_art`, `get_album_summary`) that the Discord command layer (`musicbot/commands/library.py`) calls once per artist/album selection and caches on the view. Each lookup resolves a query name from embedded tags (preferring `albumartist` over `artist` over the folder name), then tries embedded art → Spotify → Last.fm in order, with a dedicated thread pool for blocking calls, in-flight deduplication, and per-source failure logging.

**Tech Stack:** Python 3.13+, discord.py 2.7.1, `mutagen` (tag/artwork reading), `spotipy` (Spotify), `aiohttp` (Last.fm), `beautifulsoup4` (HTML stripping) — all already project dependencies, no new ones needed.

**Spec:** `docs/superpowers/specs/2026-08-28-library-metadata-enrichment-design.md`

## Global Constraints

- No test suite in this repo (no `tests/` dir, no pytest dependency). Every task's verification step is a runnable `uv run python -c "..."` script or shell command against real generated fixtures or stubbed network calls — not a pytest invocation.
- `black -l 79` (79-char lines) and `flake8 --ignore E203,W503` must pass on every touched file before it's considered done. Neither is a project dependency (they only exist inside pre-commit's isolated hook environments per `CLAUDE.md`), so every lint step invokes them via `uv tool run black@25.1.0 -l 79 --target-version py313 --check ...` / `uv tool run flake8@7.3.0 --ignore E203,W503 ...` — a bare `uv run black`/`uv run flake8` fails with "No such file or directory".
- Run everything through `uv run ...` (project code/dependencies) or `uv tool run ...` (black/flake8, which live outside the project's own dependency set) — never a bare `python`/`pip`/`black`/`flake8` (per `CLAUDE.md`, deps are managed exclusively with `uv`).
- No new dependencies: `mutagen==1.47.0`, `beautifulsoup4==4.15.0`, `aiohttp~=3.14.3`, `spotipy==2.26.0` are already in `pyproject.toml`.
- Every new `Config` class attribute needs a comment directly above it — `Config.get_comments()` parses that comment for docs/exe use (per `CLAUDE.md`).
- Only commit when a task's steps say to (each task ends with a commit step, matching how this feature's earlier commits in this session were made) — never batch multiple tasks into one commit.

---

## Task 1: Extend `AudioTags` with `album`/`album_artist` fields

**Files:**
- Modify: `musicbot/audiotags.py:8-35`

**Interfaces:**
- Consumes: nothing new (extends the existing `AudioTags`/`read_tags()` from the prior tag-reading feature).
- Produces: `AudioTags` gains `album: Optional[str]` and `album_artist: Optional[str]`; `read_tags()` populates them from mutagen's easy-mode `"album"`/`"albumartist"` keys. Task 4 depends on these two new fields.

- [ ] **Step 1: Replace `AudioTags` and `read_tags()`**

Replace the current contents of `musicbot/audiotags.py` lines 8-35 with:

```python
class AudioTags(NamedTuple):
    title: Optional[str]
    artist: Optional[str]
    duration: Optional[int]
    album: Optional[str]
    album_artist: Optional[str]


def read_tags(path: Path) -> AudioTags:
    """Best-effort tag read for a local library file. Never raises -
    an unreadable/corrupt file or a format mutagen can't parse just
    yields all-None, so callers fall back to filename-derived data."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as e:
        print(f"audiotags: failed to read {path}: {e}", file=sys.stderr)
        return AudioTags(
            title=None,
            artist=None,
            duration=None,
            album=None,
            album_artist=None,
        )

    if audio is None:
        return AudioTags(
            title=None,
            artist=None,
            duration=None,
            album=None,
            album_artist=None,
        )

    title = (audio.get("title") or [None])[0]
    artist = (audio.get("artist") or [None])[0]
    album = (audio.get("album") or [None])[0]
    album_artist = (audio.get("albumartist") or [None])[0]
    duration = (
        round(audio.info.length)
        if audio.info is not None and audio.info.length
        else None
    )

    return AudioTags(
        title=title,
        artist=artist,
        duration=duration,
        album=album,
        album_artist=album_artist,
    )
```

- [ ] **Step 2: Verify against real fixtures**

```bash
WORKDIR=$(mktemp -d)
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" \
  -metadata title="Song Title" -metadata artist="Track Artist" \
  -metadata album="The Album" -metadata album_artist="Album Artist" \
  "$WORKDIR/tagged.mp3" -loglevel error
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" \
  "$WORKDIR/untagged.mp3" -loglevel error

uv run python -c "
from pathlib import Path
from musicbot.audiotags import read_tags

tagged = read_tags(Path('$WORKDIR/tagged.mp3'))
assert tagged.title == 'Song Title', tagged
assert tagged.artist == 'Track Artist', tagged
assert tagged.album == 'The Album', tagged
assert tagged.album_artist == 'Album Artist', tagged
assert tagged.duration == 1, tagged

untagged = read_tags(Path('$WORKDIR/untagged.mp3'))
assert untagged.title is None, untagged
assert untagged.album is None, untagged
assert untagged.album_artist is None, untagged
assert untagged.duration == 1, untagged

print('OK')
"
```

Expected: prints `OK` with no assertion errors.

- [ ] **Step 3: Lint**

```bash
uv tool run black@25.1.0 -l 79 --target-version py313 --check musicbot/audiotags.py
uv tool run flake8@7.3.0 --ignore E203,W503 musicbot/audiotags.py
```

- [ ] **Step 4: Commit**

```bash
git add musicbot/audiotags.py
git commit -m "Add album/album_artist fields to AudioTags"
```

---

## Task 2: Add `read_artwork()` for embedded cover art extraction

**Files:**
- Modify: `musicbot/audiotags.py` (imports + new function)

**Interfaces:**
- Consumes: nothing new.
- Produces: `read_artwork(path: Path, max_bytes: int = 5*1024*1024) -> Optional[Tuple[bytes, str]]` — `(image_bytes, extension)` or `None`. Task 7 depends on this.

- [ ] **Step 1: Add imports**

At the top of `musicbot/audiotags.py`, change:

```python
import sys
from pathlib import Path
from typing import NamedTuple, Optional

from mutagen import File as MutagenFile
```

to:

```python
import sys
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
```

- [ ] **Step 2: Add `read_artwork()`**

Append to the end of `musicbot/audiotags.py`:

```python
def read_artwork(
    path: Path, max_bytes: int = 5 * 1024 * 1024
) -> Optional[Tuple[bytes, str]]:
    """Best-effort embedded cover art extraction. Never raises - any
    failure, absence, or oversized result (larger than max_bytes, e.g.
    a multi-MB liner-note scan) just yields None. Needs its own
    non-easy-mode parse - easy mode (used by read_tags()) doesn't
    expose embedded pictures for MP3 (EasyID3 has no APIC access)."""
    try:
        audio = MutagenFile(path)
    except Exception as e:
        print(
            f"audiotags: failed to read artwork from {path}: {e}",
            file=sys.stderr,
        )
        return None

    data: Optional[bytes] = None
    mime: Optional[str] = None

    if isinstance(audio, MP3):
        apics = audio.tags.getall("APIC") if audio.tags else []
        if apics:
            data, mime = apics[0].data, apics[0].mime
    elif isinstance(audio, FLAC):
        if audio.pictures:
            data, mime = audio.pictures[0].data, audio.pictures[0].mime
    elif isinstance(audio, MP4):
        covr = audio.tags.get("covr") if audio.tags else None
        if covr:
            cover = covr[0]
            mime = (
                "image/png"
                if cover.imageformat == cover.FORMAT_PNG
                else "image/jpeg"
            )
            data = bytes(cover)

    if not data or not mime or len(data) > max_bytes:
        return None

    return data, mime.rpartition("/")[2]
```

- [ ] **Step 3: Verify against real fixtures (MP3/FLAC/M4A, with and without art)**

```bash
WORKDIR=$(mktemp -d)
ffmpeg -y -f lavfi -i color=c=blue:s=16x16 -frames:v 1 \
  "$WORKDIR/cover.png" -loglevel error

ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" -i "$WORKDIR/cover.png" \
  -map 0:a -map 1:v -c:v mjpeg -disposition:v attached_pic \
  "$WORKDIR/with_art.mp3" -loglevel error
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" -i "$WORKDIR/cover.png" \
  -map 0:a -map 1:v -c:a flac -c:v png -disposition:v attached_pic \
  "$WORKDIR/with_art.flac" -loglevel error
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" -i "$WORKDIR/cover.png" \
  -map 0:a -map 1:v -c:a aac -c:v mjpeg -disposition:v attached_pic \
  "$WORKDIR/with_art.m4a" -loglevel error
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" \
  "$WORKDIR/no_art.mp3" -loglevel error

uv run python -c "
from pathlib import Path
from musicbot.audiotags import read_artwork

mp3 = read_artwork(Path('$WORKDIR/with_art.mp3'))
assert mp3 is not None and mp3[1] == 'jpeg' and len(mp3[0]) > 0, mp3

flac = read_artwork(Path('$WORKDIR/with_art.flac'))
assert flac is not None and flac[1] == 'png' and len(flac[0]) > 0, flac

m4a = read_artwork(Path('$WORKDIR/with_art.m4a'))
assert m4a is not None and m4a[1] == 'jpeg' and len(m4a[0]) > 0, m4a

assert read_artwork(Path('$WORKDIR/no_art.mp3')) is None
assert read_artwork(Path('$WORKDIR/does_not_exist.mp3')) is None

# size cap rejects even real art
assert read_artwork(Path('$WORKDIR/with_art.mp3'), max_bytes=1) is None

print('OK')
"
```

Expected: prints `OK` with no assertion errors.

- [ ] **Step 4: Lint**

```bash
uv tool run black@25.1.0 -l 79 --target-version py313 --check musicbot/audiotags.py
uv tool run flake8@7.3.0 --ignore E203,W503 musicbot/audiotags.py
```

- [ ] **Step 5: Commit**

```bash
git add musicbot/audiotags.py
git commit -m "Add read_artwork() for embedded cover art extraction"
```

---

## Task 3: Add `LASTFM_API_KEY` config and `linkutils.get_session()` accessor

**Files:**
- Modify: `config/config.py:69-81`
- Modify: `musicbot/linkutils.py:71-75`

**Interfaces:**
- Consumes: nothing new.
- Produces: `config.LASTFM_API_KEY: str` (default `""`); `linkutils.get_session() -> aiohttp.ClientSession`. Task 5 depends on both.

- [ ] **Step 1: Add `LASTFM_API_KEY` to `Config`**

In `config/config.py`, after the `LIBRARY_EXTENSIONS` tuple (ends at line 80, right before the `EMBED_COLOR` line), insert:

```python

    # enables album/artist bio summaries in d!library browse
    # (last.fm's artist.getInfo/album.getInfo); get a free key at
    # https://www.last.fm/api/account/create
    LASTFM_API_KEY = ""
```

- [ ] **Step 2: Add `get_session()` accessor to `linkutils.py`**

In `musicbot/linkutils.py`, after the `stop()` function (currently lines 71-74):

```python
async def stop():
    await _session.close()
    # according to aiohttp docs, we need to wait a little after closing session
    await asyncio.sleep(0.5)
```

add immediately below it:

```python
def get_session() -> ClientSession:
    """Dynamic accessor for the module-private _session - a plain
    `from linkutils import _session` at another module's top level
    would bind None forever, since _session is only set later by the
    async init() call at bot startup, not at import time."""
    return _session
```

- [ ] **Step 3: Verify**

```bash
uv run python -c "
from config import config
assert config.LASTFM_API_KEY == ''
print('config OK')
"

uv run python -c "
import asyncio
from musicbot import linkutils

async def main():
    await linkutils.init()
    session = linkutils.get_session()
    assert session is not None
    assert session is linkutils._session
    await linkutils.stop()

asyncio.run(main())
print('get_session OK')
"
```

Expected: both scripts print their `OK` line.

- [ ] **Step 4: Lint**

```bash
uv tool run black@25.1.0 -l 79 --target-version py313 --check config/config.py musicbot/linkutils.py
uv tool run flake8@7.3.0 --ignore E203,W503 config/config.py musicbot/linkutils.py
```

- [ ] **Step 5: Commit**

```bash
git add config/config.py musicbot/linkutils.py
git commit -m "Add LASTFM_API_KEY config and linkutils.get_session()"
```

---

## Task 4: Create `musicbot/library_metadata.py` — scaffolding and name resolution

**Files:**
- Create: `musicbot/library_metadata.py`

**Interfaces:**
- Consumes: `musicbot.audiotags.read_tags(path) -> AudioTags` (Task 1).
- Produces: `ArtInfo` NamedTuple (`url`, `data`, `extension`); module-level `_executor: ThreadPoolExecutor`; `async def _resolve_names(artist_folder: str, album_folder: Optional[str], sample_file: Path) -> Tuple[str, Optional[str]]`. Tasks 5, 6, 7 depend on `_executor`; Task 7 depends on `_resolve_names` and `ArtInfo`.

Note on imports across Tasks 4-7: this file is built incrementally, and each of these tasks' lint step must pass on its own — so each task's Step 1 only imports names it actually uses *at that point*, and later tasks extend the import block rather than everything being front-loaded here. This task does need `sys` (for the timeout-fallback log line below), but not yet `Dict`, `BeautifulSoup`, `config`, `linkutils`, `spotify_api`, or `read_artwork`; Tasks 5-7 add those as they're needed.

- [ ] **Step 1: Write the module**

Create `musicbot/library_metadata.py`:

```python
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from musicbot.audiotags import read_tags

# Dedicated pool (not the loop's shared default executor) for the
# blocking calls this module makes - mutagen reads and spotipy's
# synchronous HTTP client. asyncio.wait_for()'s timeout only abandons
# the await, not the underlying thread, so a string of slow/hung
# external calls could otherwise pile up threads in whatever pool the
# rest of the bot shares.
_executor = ThreadPoolExecutor(max_workers=4)


class ArtInfo(NamedTuple):
    url: Optional[str]  # a real URL - use with embed.set_thumbnail(url=...)
    data: Optional[bytes]  # raw embedded bytes - needs discord.File
    extension: Optional[str]  # "jpeg"/"png"/etc, set only when data is set


async def _resolve_names(
    artist_folder: str, album_folder: Optional[str], sample_file: Path
) -> Tuple[str, Optional[str]]:
    """Resolves the query name(s) to use for external lookups,
    preferring tag data over the (possibly organizational-only,
    non-canonical) folder name: albumartist tag > artist tag > folder
    name for the artist, album tag > folder name for the album. A
    hung/slow read (e.g. MUSIC_LIBRARY_PATH on a NAS mount) times out
    and falls back to the folder names rather than blocking forever."""
    loop = asyncio.get_running_loop()
    artist_name = artist_folder
    album_name = album_folder
    try:
        tags = await asyncio.wait_for(
            loop.run_in_executor(_executor, read_tags, sample_file),
            timeout=3,
        )
    except asyncio.TimeoutError:
        print(
            f"library_metadata: reading tags from {sample_file} timed out",
            file=sys.stderr,
        )
    else:
        artist_name = tags.album_artist or tags.artist or artist_folder
        if album_folder is not None:
            album_name = tags.album or album_folder
    return artist_name, album_name
```

- [ ] **Step 2: Verify import, name resolution ordering, and the timeout fallback**

Note: these scripts use a literal dummy path (`/tmp/dummy.mp3`), not `Path(__file__)` — `python -c "..."` never defines `__file__` in `__main__`, so `Path(__file__)` would raise `NameError`. The file doesn't need to exist since `read_tags` is mocked in every case.

```bash
uv run python -c "import musicbot.library_metadata; print('import OK')"

uv run python -c "
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
from musicbot import library_metadata
from musicbot.audiotags import AudioTags

DUMMY = Path('/tmp/dummy.mp3')

async def main():
    with patch.object(
        library_metadata,
        'read_tags',
        return_value=AudioTags(
            title='T', artist='Artist Tag', duration=1,
            album='Album Tag', album_artist='AlbumArtist Tag',
        ),
    ):
        artist, album = await library_metadata._resolve_names(
            'Folder Artist', 'Folder Album', DUMMY
        )
    assert artist == 'AlbumArtist Tag', artist
    assert album == 'Album Tag', album

    with patch.object(
        library_metadata,
        'read_tags',
        return_value=AudioTags(
            title=None, artist=None, duration=None,
            album=None, album_artist=None,
        ),
    ):
        artist, album = await library_metadata._resolve_names(
            'Folder Artist', 'Folder Album', DUMMY
        )
    assert artist == 'Folder Artist', artist
    assert album == 'Folder Album', album

    with patch.object(
        library_metadata,
        'read_tags',
        return_value=AudioTags(
            title=None, artist='Artist Tag', duration=None,
            album=None, album_artist=None,
        ),
    ):
        artist, album = await library_metadata._resolve_names(
            'Folder Artist', None, DUMMY
        )
    assert artist == 'Artist Tag', artist
    assert album is None, album

    # a hung/timed-out read falls back to folder names instead of
    # blocking forever (simulated by forcing wait_for to time out,
    # rather than actually waiting out a real 3s timeout)
    with patch.object(
        library_metadata.asyncio,
        'wait_for',
        AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        artist, album = await library_metadata._resolve_names(
            'Folder Artist', 'Folder Album', DUMMY
        )
    assert artist == 'Folder Artist', artist
    assert album == 'Folder Album', album

    print('OK')

asyncio.run(main())
"
```

Expected: both scripts print `import OK` and `OK` respectively.

- [ ] **Step 3: Lint**

```bash
uv tool run black@25.1.0 -l 79 --target-version py313 --check musicbot/library_metadata.py
uv tool run flake8@7.3.0 --ignore E203,W503 musicbot/library_metadata.py
```

- [ ] **Step 4: Commit**

```bash
git add musicbot/library_metadata.py
git commit -m "Add library_metadata.py scaffolding and tag-based name resolution"
```

---

## Task 5: Last.fm integration (summary text + last-resort art)

**Files:**
- Modify: `musicbot/library_metadata.py` (append)

**Interfaces:**
- Consumes: `config.LASTFM_API_KEY` (Task 3), `linkutils.get_session()` (Task 3), `_executor` is not used here (this is genuinely async, no thread needed).
- Produces: `_LastfmInfo` NamedTuple (`summary`, `image_url`); `async def _lastfm_info(method: str, params: Dict[str, str], key: object) -> Optional[_LastfmInfo]`. Task 7 depends on `_lastfm_info` and `_LastfmInfo`.

- [ ] **Step 1: Extend the import block**

At the top of `musicbot/library_metadata.py`, change:

```python
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from musicbot.audiotags import read_tags
```

to:

```python
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Tuple

from bs4 import BeautifulSoup

from config import config
from musicbot import linkutils
from musicbot.audiotags import read_tags
```

(`sys` was already added in Task 4; this step adds `Dict`, `BeautifulSoup`, `config`, and `linkutils`.)

- [ ] **Step 2: Append Last.fm integration**

Append to `musicbot/library_metadata.py`:

```python
class _LastfmInfo(NamedTuple):
    summary: Optional[str]
    image_url: Optional[str]


_lastfm_cache: Dict[object, Optional[_LastfmInfo]] = {}
_lastfm_futures: Dict[object, asyncio.Future] = {}


def _clean_summary(raw: str, max_length: int = 400) -> Optional[str]:
    text = BeautifulSoup(raw, "html.parser").get_text().strip()
    if not text:
        return None
    if len(text) <= max_length:
        return text
    return text[:max_length].rsplit(" ", 1)[0] + "…"


async def _fetch_lastfm(method: str, params: Dict[str, str]) -> Optional[dict]:
    session = linkutils.get_session()
    query = {
        "method": method,
        "api_key": config.LASTFM_API_KEY,
        "format": "json",
        **params,
    }
    async with session.get(
        "https://ws.audioscrobbler.com/2.0/", params=query
    ) as response:
        if response.status != 200:
            return None
        data = await response.json(content_type=None)
    # Last.fm returns HTTP 200 even for a bad/expired API key or
    # rate limiting - the failure only shows up as an "error" field
    # in the body, which would otherwise silently look like "no match"
    if "error" in data:
        raise RuntimeError(f"{data.get('message')} (code {data.get('error')})")
    return data


async def _lastfm_info(
    method: str, params: Dict[str, str], key: object
) -> Optional[_LastfmInfo]:
    if not config.LASTFM_API_KEY:
        return None
    if key in _lastfm_cache:
        return _lastfm_cache[key]
    future = _lastfm_futures.get(key)
    if future:
        return await future
    _lastfm_futures[key] = asyncio.get_running_loop().create_future()

    result: Optional[_LastfmInfo] = None
    # a timeout/exception is transient and must not be cached as a
    # permanent "no match" - only a real fetched-successfully outcome
    # (match or genuine no-match) is worth remembering for the rest
    # of the process's lifetime
    transient_failure = False
    try:
        try:
            data = await asyncio.wait_for(
                _fetch_lastfm(method, params), timeout=3
            )
        except asyncio.TimeoutError:
            print(
                f"library_metadata: Last.fm {method} timed out"
                f" for {params}",
                file=sys.stderr,
            )
            transient_failure = True
            data = None
        except Exception as e:
            print(
                f"library_metadata: Last.fm {method} failed"
                f" for {params}: {e}",
                file=sys.stderr,
            )
            transient_failure = True
            data = None

        node = (data.get("artist") or data.get("album")) if data else None
        if node:
            bio = (node.get("bio") or {}).get("summary") or (
                node.get("wiki") or {}
            ).get("summary")
            summary = _clean_summary(bio) if bio else None

            images = node.get("image") or []
            image_url = next(
                (img["#text"] for img in reversed(images) if img.get("#text")),
                None,
            )
            if summary or image_url:
                result = _LastfmInfo(summary=summary, image_url=image_url)

        if result is None and not transient_failure:
            print(
                f"library_metadata: Last.fm {method} found no match"
                f" for {params}",
                file=sys.stderr,
            )

        if not transient_failure:
            _lastfm_cache[key] = result
    finally:
        _lastfm_futures.pop(key).set_result(result)

    return result
```

- [ ] **Step 3: Verify caching, dedup, unconfigured behavior, HTML stripping, transient-failure handling, and the error-body case**

```bash
uv run python -c "
import asyncio
from unittest.mock import AsyncMock, patch
from config import config
from musicbot import library_metadata

async def main():
    # unconfigured: no call at all
    config.LASTFM_API_KEY = ''
    with patch.object(
        library_metadata,
        '_fetch_lastfm',
        AsyncMock(side_effect=AssertionError('should not be called')),
    ):
        result = await library_metadata._lastfm_info(
            'artist.getInfo', {'artist': 'X'}, 'X'
        )
    assert result is None

    config.LASTFM_API_KEY = 'fake-key'
    library_metadata._lastfm_cache.clear()
    library_metadata._lastfm_futures.clear()

    call_count = 0

    async def fake_fetch(method, params):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return {
            'artist': {
                'bio': {
                    'summary': '<a href=\"x\">link</a>Some <b>bio</b> text.'
                },
                'image': [
                    {'size': 'small', '#text': ''},
                    {'size': 'large', '#text': 'http://example.com/a.jpg'},
                ],
            }
        }

    with patch.object(library_metadata, '_fetch_lastfm', fake_fetch):
        r1, r2 = await asyncio.gather(
            library_metadata._lastfm_info(
                'artist.getInfo', {'artist': 'X'}, 'X'
            ),
            library_metadata._lastfm_info(
                'artist.getInfo', {'artist': 'X'}, 'X'
            ),
        )
        assert call_count == 1, call_count
        assert r1 == r2
        assert r1.summary == 'linkSome bio text.', repr(r1.summary)
        assert r1.image_url == 'http://example.com/a.jpg', r1.image_url

        r3 = await library_metadata._lastfm_info(
            'artist.getInfo', {'artist': 'X'}, 'X'
        )
        assert call_count == 1, call_count
        assert r3 == r1

    # a transient failure is NOT cached - a later retry re-fetches
    # rather than being permanently stuck as 'no match'
    library_metadata._lastfm_cache.clear()
    library_metadata._lastfm_futures.clear()
    attempt = 0

    async def flaky_fetch(method, params):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise RuntimeError('boom')
        return {'artist': {'bio': {'summary': 'ok'}, 'image': []}}

    with patch.object(library_metadata, '_fetch_lastfm', flaky_fetch):
        first = await library_metadata._lastfm_info(
            'artist.getInfo', {'artist': 'Y'}, 'Y'
        )
        assert first is None, first
        assert 'Y' not in library_metadata._lastfm_cache
        second = await library_metadata._lastfm_info(
            'artist.getInfo', {'artist': 'Y'}, 'Y'
        )
        assert second is not None and second.summary == 'ok', second
        assert 'Y' in library_metadata._lastfm_cache

    # HTTP 200 with an error body is a real failure, not silent
    # 'no match' - _fetch_lastfm must raise so _lastfm_info logs it
    async def error_body_fetch(method, params):
        raise RuntimeError('Invalid API key (code 10)')

    library_metadata._lastfm_cache.clear()
    library_metadata._lastfm_futures.clear()
    with patch.object(library_metadata, '_fetch_lastfm', error_body_fetch):
        result = await library_metadata._lastfm_info(
            'artist.getInfo', {'artist': 'Z'}, 'Z'
        )
        assert result is None
        assert 'Z' not in library_metadata._lastfm_cache

    print('OK')

asyncio.run(main())
"
```

Expected: prints `OK` with no assertion errors. (`call_count == 1` after two concurrent calls proves dedup; staying at `1` after the third call proves caching. The `flaky_fetch`/`error_body_fetch` blocks confirm transient failures and Last.fm error responses are logged and retried rather than being permanently cached as a false "no match" — see `_fetch_lastfm`'s own `"error" in data` check, which is what actually raises for a real Last.fm error response; this script stubs `_fetch_lastfm` directly to isolate `_lastfm_info`'s caching behavior from that check.)

- [ ] **Step 4: Lint**

```bash
uv tool run black@25.1.0 -l 79 --target-version py313 --check musicbot/library_metadata.py
uv tool run flake8@7.3.0 --ignore E203,W503 musicbot/library_metadata.py
```

- [ ] **Step 5: Commit**

```bash
git add musicbot/library_metadata.py
git commit -m "Add Last.fm integration to library_metadata.py"
```

---

## Task 6: Spotify integration (art/photo fallback)

**Files:**
- Modify: `musicbot/library_metadata.py` (append)

**Interfaces:**
- Consumes: `linkutils.spotify_api` (existing), `_executor` (Task 4).
- Produces: `_spotify_artist_image_sync(name: str) -> Optional[str]`, `_spotify_album_image_sync(artist_name: str, album_name: str) -> Optional[str]`, `async def _spotify_image(fn, key: object, label: str) -> Optional[str]`. Task 7 depends on all three.

- [ ] **Step 1: Add the `spotify_api` import**

At the top of `musicbot/library_metadata.py`, change:

```python
from config import config
from musicbot import linkutils
from musicbot.audiotags import read_tags
```

to:

```python
from config import config
from musicbot import linkutils
from musicbot.linkutils import spotify_api
from musicbot.audiotags import read_tags
```

- [ ] **Step 2: Append Spotify integration**

Append to `musicbot/library_metadata.py`:

```python
_spotify_cache: Dict[object, Optional[str]] = {}
_spotify_futures: Dict[object, asyncio.Future] = {}


def _spotify_artist_image_sync(name: str) -> Optional[str]:
    results = spotify_api.search(q=name, type="artist", limit=1)
    items = results.get("artists", {}).get("items", [])
    if not items:
        return None
    images = items[0].get("images", [])
    return images[0]["url"] if images else None


def _spotify_album_image_sync(
    artist_name: str, album_name: str
) -> Optional[str]:
    query = f"artist:{artist_name} album:{album_name}"
    results = spotify_api.search(q=query, type="album", limit=1)
    items = results.get("albums", {}).get("items", [])
    if not items:
        return None
    images = items[0].get("images", [])
    return images[0]["url"] if images else None


async def _spotify_image(fn, key: object, label: str) -> Optional[str]:
    if spotify_api is None:
        return None
    if key in _spotify_cache:
        return _spotify_cache[key]
    future = _spotify_futures.get(key)
    if future:
        return await future
    _spotify_futures[key] = asyncio.get_running_loop().create_future()

    result: Optional[str] = None
    # see _lastfm_info's identical reasoning - a timeout/exception is
    # transient and must not be cached as a permanent "no match"
    transient_failure = False
    try:
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(_executor, fn), timeout=3
            )
        except asyncio.TimeoutError:
            print(
                f"library_metadata: Spotify {label} timed out for {key}",
                file=sys.stderr,
            )
            transient_failure = True
        except Exception as e:
            print(
                f"library_metadata: Spotify {label} failed for {key}: {e}",
                file=sys.stderr,
            )
            transient_failure = True
        else:
            if result is None:
                print(
                    f"library_metadata: Spotify found no {label}"
                    f" for {key}",
                    file=sys.stderr,
                )
        if not transient_failure:
            _spotify_cache[key] = result
    finally:
        _spotify_futures.pop(key).set_result(result)

    return result
```

- [ ] **Step 3: Verify caching, dedup, unconfigured behavior, and transient-failure handling**

```bash
uv run python -c "
import asyncio
from unittest.mock import patch
from musicbot import library_metadata

async def main():
    # unconfigured: spotify_api is None -> no call
    with patch.object(library_metadata, 'spotify_api', None):
        result = await library_metadata._spotify_image(
            lambda: (_ for _ in ()).throw(AssertionError('should not run')),
            'X',
            'artist photo',
        )
    assert result is None

    library_metadata._spotify_cache.clear()
    library_metadata._spotify_futures.clear()

    call_count = 0

    def fake_lookup():
        nonlocal call_count
        call_count += 1
        return 'http://example.com/spotify.jpg'

    with patch.object(library_metadata, 'spotify_api', object()):
        r1, r2 = await asyncio.gather(
            library_metadata._spotify_image(
                fake_lookup, 'X', 'artist photo'
            ),
            library_metadata._spotify_image(
                fake_lookup, 'X', 'artist photo'
            ),
        )
        assert call_count == 1, call_count
        assert r1 == r2 == 'http://example.com/spotify.jpg'

        r3 = await library_metadata._spotify_image(
            fake_lookup, 'X', 'artist photo'
        )
        assert call_count == 1, call_count

    # a transient failure is NOT cached - a later retry re-fetches
    library_metadata._spotify_cache.clear()
    library_metadata._spotify_futures.clear()
    attempt = 0

    def flaky_lookup():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise RuntimeError('boom')
        return 'http://example.com/ok.jpg'

    with patch.object(library_metadata, 'spotify_api', object()):
        first = await library_metadata._spotify_image(
            flaky_lookup, 'W', 'artist photo'
        )
        assert first is None, first
        assert 'W' not in library_metadata._spotify_cache
        second = await library_metadata._spotify_image(
            flaky_lookup, 'W', 'artist photo'
        )
        assert second == 'http://example.com/ok.jpg', second
        assert 'W' in library_metadata._spotify_cache

    print('OK')

asyncio.run(main())
"
```

Expected: prints `OK` with no assertion errors.

- [ ] **Step 4: Lint**

```bash
uv tool run black@25.1.0 -l 79 --target-version py313 --check musicbot/library_metadata.py
uv tool run flake8@7.3.0 --ignore E203,W503 musicbot/library_metadata.py
```

- [ ] **Step 5: Commit**

```bash
git add musicbot/library_metadata.py
git commit -m "Add Spotify integration to library_metadata.py"
```

---

## Task 7: Public API — the embedded/Spotify/Last.fm fallback chain

**Files:**
- Modify: `musicbot/library_metadata.py` (append)

**Interfaces:**
- Consumes: `read_artwork` (Task 2), `_executor`/`ArtInfo`/`_resolve_names` (Task 4), `_lastfm_info`/`_LastfmInfo` (Task 5), `_spotify_image`/`_spotify_artist_image_sync`/`_spotify_album_image_sync` (Task 6).
- Produces: `async def get_artist_photo(artist_folder: str, sample_file: Path) -> Optional[ArtInfo]`, `async def get_artist_summary(artist_folder: str, sample_file: Path) -> Optional[str]`, `async def get_album_art(artist_folder: str, album_folder: str, sample_file: Path) -> Optional[ArtInfo]`, `async def get_album_summary(artist_folder: str, album_folder: str, sample_file: Path) -> Optional[str]`. Task 8 depends on all four.

- [ ] **Step 1: Add the `read_artwork` import**

At the top of `musicbot/library_metadata.py`, change:

```python
from musicbot.audiotags import read_tags
```

to:

```python
from musicbot.audiotags import read_artwork, read_tags
```

- [ ] **Step 2: Append the public API**

Append to `musicbot/library_metadata.py`:

```python
async def get_artist_photo(
    artist_folder: str, sample_file: Path
) -> Optional[ArtInfo]:
    artist_name, _ = await _resolve_names(artist_folder, None, sample_file)

    url = await _spotify_image(
        lambda: _spotify_artist_image_sync(artist_name),
        artist_name,
        "artist photo",
    )
    if url:
        return ArtInfo(url=url, data=None, extension=None)

    info = await _lastfm_info(
        "artist.getInfo", {"artist": artist_name}, artist_name
    )
    if info and info.image_url:
        return ArtInfo(url=info.image_url, data=None, extension=None)

    return None


async def get_artist_summary(
    artist_folder: str, sample_file: Path
) -> Optional[str]:
    artist_name, _ = await _resolve_names(artist_folder, None, sample_file)
    info = await _lastfm_info(
        "artist.getInfo", {"artist": artist_name}, artist_name
    )
    return info.summary if info else None


async def get_album_art(
    artist_folder: str, album_folder: str, sample_file: Path
) -> Optional[ArtInfo]:
    artist_name, album_name = await _resolve_names(
        artist_folder, album_folder, sample_file
    )

    loop = asyncio.get_running_loop()
    try:
        embedded = await asyncio.wait_for(
            loop.run_in_executor(_executor, read_artwork, sample_file),
            timeout=3,
        )
    except asyncio.TimeoutError:
        print(
            f"library_metadata: reading artwork from {sample_file}"
            " timed out",
            file=sys.stderr,
        )
        embedded = None
    if embedded is not None:
        data, extension = embedded
        return ArtInfo(url=None, data=data, extension=extension)

    key = (artist_name, album_name)
    url = await _spotify_image(
        lambda: _spotify_album_image_sync(artist_name, album_name),
        key,
        "album art",
    )
    if url:
        return ArtInfo(url=url, data=None, extension=None)

    info = await _lastfm_info(
        "album.getInfo", {"artist": artist_name, "album": album_name}, key
    )
    if info and info.image_url:
        return ArtInfo(url=info.image_url, data=None, extension=None)

    return None


async def get_album_summary(
    artist_folder: str, album_folder: str, sample_file: Path
) -> Optional[str]:
    artist_name, album_name = await _resolve_names(
        artist_folder, album_folder, sample_file
    )
    info = await _lastfm_info(
        "album.getInfo",
        {"artist": artist_name, "album": album_name},
        (artist_name, album_name),
    )
    return info.summary if info else None
```

- [ ] **Step 3: Verify the fallback chain order end-to-end**

```bash
uv run python -c "
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
from musicbot import library_metadata
from musicbot.audiotags import AudioTags

DUMMY = Path('/tmp/dummy.mp3')

async def main():
    tags = AudioTags(
        title=None, artist='A', duration=1, album='Alb', album_artist=None
    )

    # embedded art wins over everything
    with patch.object(
        library_metadata, 'read_tags', return_value=tags
    ), patch.object(
        library_metadata, 'read_artwork', return_value=(b'bytes', 'jpeg')
    ):
        art = await library_metadata.get_album_art(
            'FolderArtist', 'FolderAlbum', DUMMY
        )
    assert art.data == b'bytes' and art.extension == 'jpeg', art
    assert art.url is None, art

    # no embedded art -> falls through to spotify
    with patch.object(
        library_metadata, 'read_tags', return_value=tags
    ), patch.object(
        library_metadata, 'read_artwork', return_value=None
    ), patch.object(
        library_metadata,
        '_spotify_image',
        AsyncMock(return_value='http://s.example/x.jpg'),
    ), patch.object(
        library_metadata,
        '_lastfm_info',
        AsyncMock(side_effect=AssertionError('should not be called')),
    ):
        art = await library_metadata.get_album_art(
            'FolderArtist', 'FolderAlbum', DUMMY
        )
    assert art.url == 'http://s.example/x.jpg' and art.data is None, art

    # no embedded, no spotify -> falls through to last.fm
    lastfm_result = library_metadata._LastfmInfo(
        summary='bio', image_url='http://l.example/y.jpg'
    )
    with patch.object(
        library_metadata, 'read_tags', return_value=tags
    ), patch.object(
        library_metadata, 'read_artwork', return_value=None
    ), patch.object(
        library_metadata, '_spotify_image', AsyncMock(return_value=None)
    ), patch.object(
        library_metadata,
        '_lastfm_info',
        AsyncMock(return_value=lastfm_result),
    ):
        art = await library_metadata.get_album_art(
            'FolderArtist', 'FolderAlbum', DUMMY
        )
        summary = await library_metadata.get_album_summary(
            'FolderArtist', 'FolderAlbum', DUMMY
        )
    assert art.url == 'http://l.example/y.jpg', art
    assert summary == 'bio', summary

    # nothing anywhere -> None
    with patch.object(
        library_metadata, 'read_tags', return_value=tags
    ), patch.object(
        library_metadata, 'read_artwork', return_value=None
    ), patch.object(
        library_metadata, '_spotify_image', AsyncMock(return_value=None)
    ), patch.object(
        library_metadata, '_lastfm_info', AsyncMock(return_value=None)
    ):
        art = await library_metadata.get_album_art(
            'FolderArtist', 'FolderAlbum', DUMMY
        )
    assert art is None, art

    # artist-level functions wire through too
    artist_tags = AudioTags(
        title=None, artist='A', duration=1, album=None, album_artist='AA'
    )
    with patch.object(
        library_metadata, 'read_tags', return_value=artist_tags
    ), patch.object(
        library_metadata, '_spotify_image', AsyncMock(return_value=None)
    ), patch.object(
        library_metadata,
        '_lastfm_info',
        AsyncMock(
            return_value=library_metadata._LastfmInfo(
                summary='artist bio', image_url='http://l.example/artist.jpg'
            )
        ),
    ):
        photo = await library_metadata.get_artist_photo(
            'FolderArtist', DUMMY
        )
        summary = await library_metadata.get_artist_summary(
            'FolderArtist', DUMMY
        )
    assert photo.url == 'http://l.example/artist.jpg', photo
    assert summary == 'artist bio', summary

    print('OK')

asyncio.run(main())
"
```

Expected: prints `OK` with no assertion errors.

- [ ] **Step 4: Lint**

```bash
uv tool run black@25.1.0 -l 79 --target-version py313 --check musicbot/library_metadata.py
uv tool run flake8@7.3.0 --ignore E203,W503 musicbot/library_metadata.py
```

- [ ] **Step 5: Commit**

```bash
git add musicbot/library_metadata.py
git commit -m "Add library_metadata public API with embedded/Spotify/Last.fm fallback"
```

---

## Task 8: Wire enrichment into `LibraryBrowseView`

**Files:**
- Modify: `musicbot/commands/library.py`

**Interfaces:**
- Consumes: `library_metadata.get_artist_photo`/`get_artist_summary`/`get_album_art`/`get_album_summary`/`ArtInfo` (Task 7); `library.LibrarySong`/`library.song_path` (existing).
- Produces: `LibraryBrowseView` now renders enrichment; no new public interface consumed by later tasks.

- [ ] **Step 1: Update imports**

Replace:

```python
from typing import List, Optional

import discord
from discord.ext import commands

from config import config
from musicbot import library
from musicbot.bot import MusicBot
from musicbot.loader import SongError
from musicbot.utils import CheckError, dj_check, play_check
```

with:

```python
import io
from pathlib import Path
from typing import List, NamedTuple, Optional

import discord
from discord.ext import commands

from config import config
from musicbot import library, library_metadata
from musicbot.bot import MusicBot
from musicbot.loader import SongError
from musicbot.utils import CheckError, dj_check, play_check
```

- [ ] **Step 2: Add the `_Enrichment` type**

After `PAGE_SIZE = 25`, add:

```python
class _Enrichment(NamedTuple):
    summary: Optional[str]
    art: Optional[library_metadata.ArtInfo]
```

- [ ] **Step 3: Update `__init__` and `interaction_check`**

Inside the existing `class LibraryBrowseView(discord.ui.View):` block (do not duplicate the class line — this replaces only the `__init__` and `interaction_check` method bodies already in the file):

```python
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.index = library.get_index()
        self.artist: Optional[str] = None
        self.album: Optional[str] = None
        self.page = 0
        self._enrichment: Optional[_Enrichment] = None
        # guards against a second click landing while a deferred
        # descend()/go_back() is still resolving enrichment - deferring
        # a component interaction clears its click spinner and
        # re-enables the view immediately, it does not show a
        # "thinking" placeholder, so without this two overlapping
        # resolves could land out of order
        self._busy: bool = False
        # set by the caller after sending (Task 8); needed so
        # on_timeout() can disable the stale buttons/select. Left None
        # for the slash-command path, where ctx.interaction is used
        # instead (see on_timeout below).
        self.message: Optional[discord.Message] = None
        self.build_items()

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This browser belongs to someone else.", ephemeral=True
            )
            return False
        if self._busy:
            await interaction.response.send_message(
                "Still loading, please wait…", ephemeral=True
            )
            return False
        return True
```

- [ ] **Step 4: Add `_first_sample_file()` and `_resolve_enrichment()`**

After the existing `_songs()` method, add:

```python
    def _first_sample_file(self) -> Optional[Path]:
        albums = self.index.get(self.artist, {})
        if not albums:
            return None
        first_album = sorted(albums.keys())[0]
        songs = albums[first_album]
        if not songs:
            return None
        return library.song_path(self.artist, first_album, songs[0].filename)

    async def _resolve_enrichment(self) -> Optional[_Enrichment]:
        if self.artist is None:
            return None
        if self.album is None:
            sample = self._first_sample_file()
            if sample is None:
                return None
            summary = await library_metadata.get_artist_summary(
                self.artist, sample
            )
            art = await library_metadata.get_artist_photo(self.artist, sample)
        else:
            songs = self._songs()
            if not songs:
                return None
            sample = library.song_path(
                self.artist, self.album, songs[0].filename
            )
            summary = await library_metadata.get_album_summary(
                self.artist, self.album, sample
            )
            art = await library_metadata.get_album_art(
                self.artist, self.album, sample
            )
        return _Enrichment(summary=summary, art=art)
```

- [ ] **Step 5: Update `embed()`, add `_attachments()`, update `render()`**

Replace the `embed()` method and the block from `build_items()` through `render()`:

```python
    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=self.title(), color=config.EMBED_COLOR)
        if not self.entries():
            embed.description = config.LIBRARY_EMPTY
        if self._enrichment:
            if self._enrichment.summary:
                embed.description = self._enrichment.summary
            art = self._enrichment.art
            if art and art.url:
                embed.set_thumbnail(url=art.url)
            elif art and art.data:
                embed.set_thumbnail(url=f"attachment://cover.{art.extension}")
        return embed

    def _attachments(self) -> List[discord.File]:
        art = self._enrichment.art if self._enrichment else None
        if art and art.data:
            return [
                discord.File(
                    io.BytesIO(art.data), filename=f"cover.{art.extension}"
                )
            ]
        return []

    def build_items(self):
        self.clear_items()
        entries = self.entries()
        labels = self.labels()
        page = slice(self.page * PAGE_SIZE, (self.page + 1) * PAGE_SIZE)
        page_entries = entries[page]
        page_labels = labels[page]
        if page_entries:
            self.add_item(LibrarySelect(page_entries, page_labels, self))
        if self.artist is not None:
            self.add_item(QueueLevelButton(self))
            self.add_item(BackButton(self))
        if self.page > 0:
            self.add_item(PageButton(self, -1, "◀ Prev"))
        if (self.page + 1) * PAGE_SIZE < len(entries):
            self.add_item(PageButton(self, 1, "Next ▶"))

    async def render(
        self, interaction: discord.Interaction, use_followup: bool = False
    ):
        self.build_items()
        kwargs = {
            "embed": self.embed(),
            "view": self,
            "attachments": self._attachments(),
        }
        if use_followup:
            await interaction.edit_original_response(**kwargs)
        else:
            await interaction.response.edit_message(**kwargs)
```

- [ ] **Step 6: Update `descend()`/`go_back()`, add `_enter_level()`**

Replace `descend()` and `go_back()`:

```python
    async def descend(self, interaction: discord.Interaction, chosen: str):
        self.page = 0
        if self.artist is None:
            self.artist = chosen
            await self._enter_level(interaction)
        elif self.album is None:
            self.album = chosen
            await self._enter_level(interaction)
        else:
            await self.queue_pairs(interaction, [(self.album, chosen)])

    async def go_back(self, interaction: discord.Interaction):
        self.page = 0
        if self.album is not None:
            self.album = None
        else:
            self.artist = None
        await self._enter_level(interaction)

    async def _enter_level(self, interaction: discord.Interaction):
        self._busy = True
        try:
            await interaction.response.defer()
            self._enrichment = await self._resolve_enrichment()
            await self.render(interaction, use_followup=True)
        finally:
            self._busy = False
```

(`queue_current_level()`, `queue_pairs()`, the `Library` cog, and everything else in the file are unchanged.)

- [ ] **Step 7: Verify the pure-logic pieces without a live Discord connection**

```bash
uv run python -c "import musicbot.commands.library; print('import OK')"

uv run python -c "
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from musicbot import library
from musicbot.commands.library import LibraryBrowseView, _Enrichment
from musicbot import library_metadata

library._index = {
    'Artist': {
        'Album': [
            library.LibrarySong(
                filename='01 - Song.mp3', title='Artist - Song'
            )
        ]
    }
}

ctx = SimpleNamespace(author=SimpleNamespace(id=1), interaction=None)
view = LibraryBrowseView(ctx)
view.artist = 'Artist'

async def main():
    with patch.object(
        library_metadata,
        'get_artist_summary',
        AsyncMock(return_value='A short bio'),
    ), patch.object(
        library_metadata,
        'get_artist_photo',
        AsyncMock(
            return_value=library_metadata.ArtInfo(
                url='http://example.com/a.jpg', data=None, extension=None
            )
        ),
    ):
        enrichment = await view._resolve_enrichment()

    assert enrichment.summary == 'A short bio', enrichment
    assert enrichment.art.url == 'http://example.com/a.jpg', enrichment

    view._enrichment = enrichment
    embed = view.embed()
    assert embed.description == 'A short bio', embed.description
    assert embed.thumbnail.url == 'http://example.com/a.jpg'
    assert view._attachments() == []

    # embedded-art case produces a real discord.File attachment
    view._enrichment = _Enrichment(
        summary=None,
        art=library_metadata.ArtInfo(
            url=None, data=b'bytes', extension='png'
        ),
    )
    embed2 = view.embed()
    assert embed2.thumbnail.url == 'attachment://cover.png', embed2.thumbnail.url
    attachments = view._attachments()
    assert len(attachments) == 1 and attachments[0].filename == 'cover.png'

    print('OK')

asyncio.run(main())
"
```

Expected: both scripts print their `OK` line. (This exercises `_resolve_enrichment()`, `embed()`, and `_attachments()` directly — it does not exercise the live interaction flow, see Step 8.)

- [ ] **Step 8: Manual verification in a real Discord server (required, not optional)**

This codebase has no automated way to simulate real Discord interactions (`defer()`/`edit_original_response()` timing, a second click landing mid-resolve). Before considering this task done:

1. Set `MUSIC_LIBRARY_PATH` to a real folder with a couple of tagged artists/albums, run `d!library refresh`.
2. Run `d!library browse`, descend into an artist: confirm the screen shows a bio/photo (or gracefully shows neither if no `LASTFM_API_KEY`/`SPOTIFY_ID` are configured) without erroring.
3. Descend into an album with embedded cover art: confirm the thumbnail renders (this exercises the `discord.File`/`attachment://` path that Step 7 can't).
4. Click rapidly between two albums/artists before the first screen finishes loading: confirm you get "Still loading, please wait…" instead of a corrupted/out-of-order screen.
5. Page through a >25-item list (if you have one) and confirm it's instant (no defer/spinner) — this confirms `PageButton` isn't re-resolving enrichment.

- [ ] **Step 9: Lint**

```bash
uv tool run black@25.1.0 -l 79 --target-version py313 --check musicbot/commands/library.py
uv tool run flake8@7.3.0 --ignore E203,W503 musicbot/commands/library.py
```

- [ ] **Step 10: Commit**

```bash
git add musicbot/commands/library.py
git commit -m "Wire cover art and bio enrichment into d!library browse"
```

---

## Task 9: Update README

**Files:**
- Modify: `README.md:16-25` (API Keys section)
- Modify: `README.md:160-175` (Local Library Commands section)

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing consumed by other tasks.

- [ ] **Step 1: Document `LASTFM_API_KEY` in the API Keys section**

In the `#### API Keys` list (currently lines 18-23):

```markdown
#### API Keys
* Discord - https://discord.com/developers
* Spotify (optional) - https://developer.spotify.com/dashboard/
  - Client ID
  - Client Secret
  - Note: Limited to 50 playlist items without API
```

add a new bullet after the Spotify one:

```markdown
* Last.fm (optional) - https://www.last.fm/api/account/create
  - API Key
  - Only used for artist/album bio summaries in `d!library browse`
```

- [ ] **Step 2: Mention enrichment in the Local Library Commands section**

Change:

```markdown
Requires `ENABLE_LOCAL_LIBRARY=True` and `MUSIC_LIBRARY_PATH` set to a folder laid out as `Artist/Album/song.ext` (see `.env.sample`).
```

to:

```markdown
Requires `ENABLE_LOCAL_LIBRARY=True` and `MUSIC_LIBRARY_PATH` set to a folder laid out as `Artist/Album/song.ext` (see `.env.sample`).

`d!library browse`'s artist and album screens show cover art and a short bio, sourced from artwork embedded in the audio files themselves, falling back to Spotify and Last.fm when configured (both optional - the browser works without either).
```

- [ ] **Step 3: Verify the text landed correctly**

```bash
grep -n "Last.fm" README.md
```

Expected: two matches, one in the API Keys section and one in the Local Library Commands section.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document Last.fm config and library browse enrichment in README"
```

---

## Self-Review Notes

- **Spec coverage:** every spec section has a task — data sources/fallback chain (Task 7), query name resolution (Tasks 1, 4), configuration (Task 3), the new module's blocking-I/O boundary/dedicated executor/embedded-art/Spotify/Last.fm/caching/dedup/failure-handling/logging (Tasks 2, 4, 5, 6), browse embed changes including attachment mechanics/resolve-once-per-selection/interaction deferral/busy-flag (Task 8).
- **Type consistency verified:** `ArtInfo` (Task 4) is constructed identically in Task 7 and consumed identically in Task 8; `_LastfmInfo` (Task 5) flows into Task 7 unchanged; `read_artwork`'s `Optional[Tuple[bytes, str]]` return (Task 2) is unpacked the same way in Task 7; the four public function signatures declared in Task 4/7's Interfaces blocks match exactly how Task 8 calls them.
- **No placeholders:** every step has real, complete code or a concrete runnable command — nothing deferred to "similar to Task N" or "add error handling here."
