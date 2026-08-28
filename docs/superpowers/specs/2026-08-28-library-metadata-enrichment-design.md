# Library Browse Metadata Enrichment — Design

## Overview

`d!library browse` currently shows plain folder/tag-derived names in its dropdowns (as of the [local library metadata work](2026-08-27-local-music-library-design.md) — see also the tag-reading follow-up that populated `Uploader`/`Duration`/nicer titles). This adds cover art and a short text summary to the artist- and album-level browse screens, so browsing a local collection looks like browsing a real music service instead of a flat file listing.

This is a follow-up to the existing local library feature — it touches the same `d!library browse` command and reuses the existing (optional) Spotify integration, adding one new optional external integration (Last.fm) for text summaries.

## Goals

- Show a short artist bio when browsing an artist's albums, and a short album summary when browsing an album's songs.
- Show cover art (album art) and an artist photo where available.
- Degrade silently and never break browsing: missing config, network failures, or no match for a given artist/album just omit that field.
- No new *required* configuration — self-hosters who don't set `LASTFM_API_KEY` (or `SPOTIFY_ID`/`SECRET`) simply see less enrichment, never an error.

## Non-goals (explicitly deferred)

- Enriching the artist list (top level) or song list (leaf level) screens — there's no single artist/album to represent there.
- A generic "search any artist/album" command — this only enriches what's already being browsed.
- TTL/invalidation for the metadata cache — external bios/art don't change often enough to justify it now.
- Any change to indexing (`d!library refresh`, `library.py`) — this is purely presentational, resolved lazily per browse screen.

## Data sources & fallback chain

Investigated during brainstorming:

- **Spotify Web API** (via the `spotipy` client already wired up in `linkutils.py`, optional `SPOTIFY_ID`/`SPOTIFY_SECRET`): has real, current cover art and artist photos (`images` field on both `album` and `artist` objects), but **no bio/description text** for either — never exposed in the public Web API.
- **Locally embedded artwork** (via `mutagen`, already a dependency): ID3 `APIC` (MP3), FLAC `Picture` blocks, MP4 `covr` atoms. No network call, no config, and commonly present in a well-tagged personal library. No text summary — files don't carry prose bios.
- **Last.fm** (`artist.getInfo`/`album.getInfo`, new optional `LASTFM_API_KEY`): the source for `bio.summary`/`wiki.summary` text. Also returns an `image` field, but Last.fm stopped licensing real cover art around 2018 — most `image` values now point to a generic placeholder rather than real artwork, so it is *not* a reliable art source.

**Album/artist art fallback chain:** embedded (mutagen) → Spotify (if configured) → Last.fm's `image` field (last resort, since it's free off the same API call already fetching the summary) → no image.

**Summary text:** Last.fm only. No summary shown if `LASTFM_API_KEY` is unset or the lookup fails/has no match.

## Query name resolution (tags over folder names)

The browse tree's grouping stays folder-based (unchanged — see Non-goals), but the *query strings* sent to Spotify/Last.fm prefer tag data over the folder name. A folder name is organizational and can drift from the canonical name (abbreviations, typos, "Various Artists" compilation folders, alternate spellings) — searching Spotify/Last.fm with a bad query string doesn't just risk finding nothing, a fuzzy match can return a wrong-but-similarly-named artist or album, which is worse than an empty field.

Resolution order, both for the artist-level and album-level lookups: **`albumartist` tag → `artist` tag → folder name.** `albumartist` (ID3 `TPE2`, also present in FLAC/MP4 tags) is preferred over the plain `artist` tag because it's what well-tagged files use specifically to normalize a consistent artist name across a whole album (e.g. compilations, or various featured-artist tracks that would otherwise each report a different `artist`).

This reuses the same "first song in the album" file already read for embedded art — one `read_tags()` call resolves the query name and checks for embedded art together. For the artist-level screen (no single album in scope yet), the sample file is the first song of the first album under that artist folder — deterministic, since both `library.py`'s directory walk and each album's song list are already sorted.

`musicbot/audiotags.py`'s `AudioTags` gains two fields to support this — `album: Optional[str]` and `album_artist: Optional[str]` (mutagen easy-mode keys `"album"`/`"albumartist"`) — read the same way `title`/`artist` already are.

Because resolution happens inside `library_metadata.py` (not the caller), the module-level caches are keyed by the *resolved* name, not the folder name — a side benefit: two differently-named folders that share the same tagged artist end up sharing one cache entry instead of two redundant lookups.

## Configuration

One new `Config` class attribute in `config/config.py`, following the `SPOTIFY_ID`/`SPOTIFY_SECRET` pattern (optional, feature silently degrades when unset):

```python
# enables album/artist bio summaries in d!library browse
# (last.fm's artist.getInfo/album.getInfo); get a free key at
# https://www.last.fm/api/account/create
LASTFM_API_KEY = ""
```

No new flag is needed to gate the feature as a whole — it activates progressively based on whatever's already configured (embedded art always attempted; Spotify art fallback active whenever `SPOTIFY_ID`/`SPOTIFY_SECRET` are set; Last.fm summary/last-resort-art active whenever `LASTFM_API_KEY` is set).

## New module: `musicbot/library_metadata.py`

Sits alongside `library.py` (indexing) and `linkutils.py` (existing Spotify integration), the same way `loader.py` sits next to both — this module is the *external lookup* layer, called only from the Discord command layer (`commands/library.py`), never from indexing.

```python
class ArtInfo(NamedTuple):
    url: Optional[str]     # a real URL (Spotify/Last.fm) - use with embed.set_thumbnail(url=...)
    data: Optional[bytes]  # raw embedded bytes - needs a discord.File + attachment:// URI

# artist_folder/album_folder are the folder names, used only as the
# final fallback if tags can't resolve a name (see "Query name
# resolution" above) and, for the artist functions, to key the cache
# when even the fallback comes up empty. sample_file is read once for
# both tag-based name resolution and (album-level) embedded art.
async def get_artist_photo(artist_folder: str, sample_file: Path) -> Optional[ArtInfo]: ...
async def get_artist_summary(artist_folder: str, sample_file: Path) -> Optional[str]: ...
async def get_album_art(artist_folder: str, album_folder: str, sample_file: Path) -> Optional[ArtInfo]: ...
async def get_album_summary(artist_folder: str, album_folder: str, sample_file: Path) -> Optional[str]: ...
```

- **Embedded art**: `musicbot/audiotags.py` gains `read_artwork(path: Path) -> Optional[bytes]`, mirroring `read_tags()`'s never-raises shape (try/except around the mutagen call, `None` on any failure or absence). Called (album-level only) against the same sample file used for name resolution — one disk read represents the whole album, not one per track.
- **Spotify fallback**: reuses the existing `spotify_api` client (`musicbot/linkutils.py`). Since `spotipy` is a synchronous/blocking client, the call is wrapped in `loop.run_in_executor(None, ...)` — a thread, not the `loader.py` `ProcessPoolExecutor`. This is a quick I/O-bound lookup, not CPU-bound extraction work, so it doesn't need that heavier machinery.
- **Last.fm**: a genuine async `aiohttp` call, reusing the session `linkutils.py` already opens via `init_session()`/`get_soup()`. `bio.summary`/`wiki.summary` commonly has a trailing `<a href="...">Read more on Last.fm</a>` — stripped before use — then truncated to ~400 characters (well under Discord's 4096-char embed description limit, and matching the "short summary" ask this feature started from rather than dumping Last.fm's full multi-paragraph bio into a list-browsing screen).
- **Timeout**: each external call (Spotify executor call, Last.fm HTTP call) has a short timeout (~3s) so one slow API can't stall browsing noticeably even behind the interaction defer described below.
- **Caching**: module-level dicts, `_artist_cache: Dict[str, ...]` and `_album_cache: Dict[Tuple[str, str], ...]`, keyed by the *resolved* name from the query-resolution step above (not the raw folder name) and populated on first lookup, kept for the process lifetime. Not tied to `d!library refresh` (which only rebuilds the local file index) — external bios/art don't change often enough to justify invalidation logic now.
- **Failure handling**: any lookup failure (network error, timeout, rate limit, no match, missing config) returns `None` for that specific piece. Nothing here ever raises past its own function — the caller always gets a clean "no data" signal instead of an exception.
- **Logging**: each source-specific helper (`_spotify_album_art()`, `_lastfm_album_info()`, etc.) logs a one-line message to stderr — matching the existing `print(..., file=sys.stderr)` convention (`library.py`'s `_safe_iterdir`, `audiotags.read_tags`) — when a source it actually *attempted* (i.e. configured) returns no match or errors, e.g. `library_metadata: Spotify found no album art for 'Artist - Album'` or `library_metadata: Last.fm lookup failed for 'Artist': <error>`. An unconfigured source (no `LASTFM_API_KEY`/`SPOTIFY_ID`) is never attempted, so it never logs — this is purely for "I set this up and it isn't working" visibility, not spam on setups that only use a subset of sources.

## Browse embed changes (`musicbot/commands/library.py`)

`LibraryBrowseView.embed()` gains enrichment at two of its three levels:

- **Artist-level screen** (listing albums for a chosen artist): `embed.description` = artist bio summary (Last.fm); thumbnail = artist photo (Spotify only — no embedded-file equivalent exists for "artist photo").
- **Album-level screen** (listing songs for a chosen album): `embed.description` = album summary (Last.fm); thumbnail = album art via the fallback chain above.
- **Top-level artist list and the leaf song list are unchanged** — there's no single artist/album to represent at those levels.

**Attachment mechanics:** when the resolved art is raw bytes (the embedded case), it can't be passed directly to `set_thumbnail(url=...)`. It's uploaded as a `discord.File(io.BytesIO(data), filename="cover.jpg")` alongside the message, and the embed references it via `embed.set_thumbnail(url="attachment://cover.jpg")`. When the art is already a URL (Spotify/Last.fm), `set_thumbnail(url=...)` is used directly with no file upload. `render()` threads an `attachments=` list through its `edit_message`/`edit_original_response` calls to support the embedded-bytes case; when there's no embedded art to attach, that list is empty exactly as today.

**Interaction deferral:** `descend()` and `go_back()` currently call `render()`, which does `interaction.response.edit_message(...)` synchronously. Since the enrichment lookup can take longer than Discord's ~3s interaction budget even with the internal 3s timeouts (worst case: one slow call before its own timeout fires), both methods now defer (`interaction.response.defer()`) *before* triggering a render that needs enrichment, then follow up via `interaction.edit_original_response(...)` — mirroring the pattern `queue_pairs()` already uses for its own slower path. This only adds a "thinking…" flicker when descending into an artist or album screen (the two enriched levels), not on every click (page/back at the song level, or the top-level artist list, render exactly as fast as today).

## Error handling

- No `LASTFM_API_KEY` configured: summaries are simply omitted; art still works via embedded/Spotify.
- No `SPOTIFY_ID`/`SPOTIFY_SECRET` configured: Spotify art/photo fallback is skipped; embedded art and Last.fm's last-resort image still apply.
- A lookup times out, errors, or has no match: that specific field (art or summary) is omitted from the embed, browsing continues normally, and a one-line message is logged to stderr (see Logging above) naming the source and the artist/album, so this is diagnosable without needing to reproduce it live.
- No embedded art in any file, no Spotify config, no Last.fm match: the embed just has no thumbnail, same as today.

## Testing / verification approach

This repo has no test suite (per `CLAUDE.md`). Verification follows the pattern established for the tag-reading work:

- `audiotags.read_artwork()` exercised directly against real MP3/FLAC/M4A fixtures with and without embedded art.
- `library_metadata.py`'s caching and fallback-ordering logic exercised with the network calls stubbed out (no live Spotify/Last.fm credentials needed to verify the *ordering* and *graceful-degradation* behavior).
- A manual pass with real `SPOTIFY_ID`/`SPOTIFY_SECRET`/`LASTFM_API_KEY` values (if available) to confirm the live embeds render correctly, since actual API response shapes can't be fully verified from fixtures alone.
- `black -l 79` / `flake8 --ignore E203,W503` clean on all touched files.

## Open follow-ups (not part of this feature)

- TTL/refresh for cached external metadata, if bios/art are ever found to go stale in practice.
- Enriching the artist-list/song-list screens (deferred as a non-goal above) if there's ever a natural single-item representation for them.
