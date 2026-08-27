# Local Music Library Browsing — Design

## Overview

Let users browse a music collection stored on the disk of the machine running the bot — laid out as `MUSIC_LIBRARY_PATH/Artist/Album/song.ext` — and queue a single song, an entire album, or an entire artist, without leaving Discord. Browsing happens through an interactive, click-through UI (dropdowns + buttons), not typed search.

This is an opt-in feature (`ENABLE_LOCAL_LIBRARY`, default off) so self-hosters who don't have a local collection see no change.

## Goals

- Browse Artist → Album → Song via Discord UI components, no typing required.
- Queue at any level: one song, a whole album, or a whole artist.
- Local tracks behave exactly like any other track everywhere else in the bot — same queue, same `d!playlist save`/`load`, same "now playing" / "skipped, here's why" messaging.
- Handle libraries in the thousands of files without blowing Discord's ~3 second interaction response budget.

## Non-goals (explicitly deferred)

- Redesigning `d!playlist` command UX. The user has flagged the current playlist commands as hard to use; this feature makes local files participate in that system as it exists today, and playlist UX is a separate future effort.
- Reading ID3/audio metadata (artist/album/title tags). Directory and file names are the source of truth, matching the `Artist/Album/song.ext` layout described.
- Watching the filesystem for changes. Refresh is a manual command.
- A typed/autocomplete fast path (`/library play artist:X album:Y`). The data layer below supports adding this later without rework, but it isn't part of this feature.

## Configuration

Two new `Config` class attributes in `config/config.py`, following the existing `ENABLE_BUTTON_PLUGIN`/`COOKIE_PATH` pattern:

```python
# enables browsing/queueing a local music library via Discord UI
ENABLE_LOCAL_LIBRARY = False

# root folder, expected to contain Artist/Album/song.ext
MUSIC_LIBRARY_PATH = ""
```

No `CONFIG_DIRS` resolution (that machinery exists for `COOKIE_PATH` to handle PyInstaller's frozen-exe bundling; a music library is always external to the app, so a plain path is enough). Self-hosters running under Docker will need to add a read-only volume mount for whatever path they set `MUSIC_LIBRARY_PATH` to — noted in the PR description and README, not part of this bot's code.

## Indexing

New module `musicbot/library.py`, mirroring the existing separation between `musicbot/loader.py` (data-fetching) and `musicbot/commands/music.py` (Discord layer):

```python
LibraryIndex = Dict[str, Dict[str, List[str]]]  # {artist: {album: [filename, ...]}}

_index: LibraryIndex = {}

def build_index() -> LibraryIndex: ...   # walks MUSIC_LIBRARY_PATH, filters by config.SUPPORTED_EXTENSIONS
def get_index() -> LibraryIndex: ...     # returns the current in-memory index
```

- Built once at startup (in `MusicBot.setup_hook`, gated on `config.ENABLE_LOCAL_LIBRARY`) via a plain two-level `os.walk`/`os.scandir`: top-level dirs are artists, their subdirs are albums, files inside matching `config.SUPPORTED_EXTENSIONS` are songs. No metadata reads, so a few thousand files is expected to index in well under a second.
- Rebuilt on demand via a new `d!library refresh` command, gated by `dj_check` (same tier as `d!playlist save`).
- Lives as plain module-level state, not per-guild — the library is one filesystem shared across every guild the bot serves, unlike `AudioController`/`GuildSettings`.

## Song representation & playback integration

New `SiteTypes.LOCAL_LIBRARY` member in `musicbot/linkutils.py`. Paths are represented as real `file://` URIs (`pathlib.Path.as_uri()` / `Path.from_uri()`, both available on the project's Python 3.13+ floor), which the existing `url_regex` already matches — no changes needed to the "is this a URL" gate.

`identify_url()` gains one check before the generic `CUSTOM` extension check:

```python
if url.startswith("file://"):
    return SiteTypes.LOCAL_LIBRARY
```

`loader.py`'s `_load_song()` gains a matching branch alongside the existing `SiteTypes.CUSTOM` one:

```python
elif host == SiteTypes.LOCAL_LIBRARY:
    path = Path.from_uri(track)
    if not path.is_file():
        raise SongError(config.LIBRARY_FILE_MISSING)
    data = {"url": track, "webpage_url": track, "title": path.stem}
```

The existence check is the one piece of new error-handling this needs: `SiteTypes.CUSTOM` (arbitrary pasted URLs) has no equivalent check today, but here the bot *owns* the index, so a missing file is either a stale index (file deleted/moved since last `d!library refresh`) or a real bug — worth a clear message either way, extending the "explain why a song was skipped instead of failing silently" work already in the bot (`AudioController.play_song`'s existing preload-failure message).

Everything downstream — `AudioController.process_song`/`preload`/`play_song`, `d!playlist save`/`load` (which already round-trip any `{url, title}` pair through `get_site_type`) — needs **no changes**. This is the payoff of representing local tracks as just another `Song`/`SiteTypes` variant: a saved playlist can already freely mix a local track with a YouTube track today, once this lands.

**Queueing action → code path:** every "Queue this X" button constructs the relevant `file://` URI(s) from the index and calls `AudioController.process_song(uri, user=...)` — the exact same entry point `d!play` uses. For "Queue this Album"/"Queue this Artist", this means looping over each matched track's URI and awaiting `process_song()` once per track (local-library loads are pure filesystem checks, no network/subprocess cost, so this is fast even for a large artist). Reusing `process_song()` uniformly (rather than bulk-constructing `Song` objects directly, the way `d!playlist load` does) keeps every local-library queue action showing up in the per-song stdout logging added recently (`{user} queued {track} in guild {guild}`) — bypassing `process_song()` for bulk adds would silently exempt local-library usage from that visibility, which would undercut the point of that feature.

For a bulk queue (album/artist), a per-song `SongError` (e.g. a stale index pointing at a deleted file) is caught and collected rather than aborting the whole batch — the interaction response after a bulk queue reports counts, e.g. "Queued 14 songs. 1 skipped (file not found: *Track Name*) — try `d!library refresh`."

## Discord command surface & browsing UI

New Cog, `musicbot/commands/library.py`, loaded via `initial_extensions` in `musicbot/__main__.py` when `config.ENABLE_LOCAL_LIBRARY` is set (same conditional-loading pattern `ENABLE_BUTTON_PLUGIN` already uses for the button plugin).

A single `hybrid_group`, `library`, matching the existing `d!playlist`/`d!guild_whitelist` pattern (one group per feature area) rather than standalone commands:

- `d!library refresh` (DJ-gated, `dj_check`) — rebuilds the index, replies with a count of artists/albums/songs found.
- `d!library browse` — opens the interactive browser. Opening the browser itself needs no voice-related check (you're just looking); it only lists what's in the index. The check that matters is on the *queue action*: each "Queue this X" button calls `utils.play_check(ctx)` before calling `process_song()` — the same check `Music._play_song` applies before `d!play` queues anything, including its voice-channel auto-join. This mirrors `d!play` exactly at the point where it matters (actually queueing/playing) without gating mere browsing behind being in a voice channel.

**Browser UI**, one `discord.ui.View` subclass reused across all three levels (artist/album/song), redrawing the same ephemeral message rather than sending new ones:

- A `discord.ui.Select` listing the current level's entries (artists, or albums within the chosen artist, or songs within the chosen album). Discord caps a select at 25 options; levels with more than 25 entries get Prev/Next buttons alongside it, paging through the underlying list.
- A "Queue this Artist" / "Queue this Album" button, present at the artist and album levels — queues everything below the current node without descending further.
- Picking a song (leaf level) queues it immediately.
- A "Back" button at every level below the top, returning to the previous list (artist list, or the previous artist's album list).
- The response is ephemeral (`ephemeral=True`), so concurrent browsers from different users never collide and no extra "who's allowed to click this" permission logic is needed on the buttons themselves — Discord already scopes an ephemeral interaction to the user who triggered it.

## Playlist integration

Confirmed no code changes needed: `d!playlist save` already serializes each queued song as a generic `{"url": song.webpage_url, "title": song.title}` pair, and `d!playlist load` already reconstructs songs via `get_site_type(song_data["url"])`, which will correctly return the new `SiteTypes.LOCAL_LIBRARY` for a stored `file://` URI. A saved playlist can mix local and streamed tracks freely.

## Error handling

- `ENABLE_LOCAL_LIBRARY` on but `MUSIC_LIBRARY_PATH` unset/nonexistent: `d!library refresh` and `/library browse` both report a clear configuration error rather than silently showing an empty library.
- Empty library (path exists, no matching files): browsing reports "no music found" instead of an empty/broken dropdown.
- A file referenced by the index (or a saved playlist) has since been deleted/moved: reported per-song via `SongError`/`LIBRARY_FILE_MISSING`, following the pattern above.

## Testing / verification approach

This repo has no test suite (confirmed in `CLAUDE.md`); verification will follow the same approach used for every other change this session — exercising the new functions directly (index building against a temp directory tree, `identify_url`/`_load_song` against fake `file://` URIs, the bulk-queue error-collection logic against a mix of present/missing files) rather than through a real Discord connection, plus confirming every touched module still imports cleanly and passes the project's pinned `black`/`flake8` versions.

## Open follow-ups (not part of this feature)

- A typed autocomplete fast path (`/library play artist:X album:Y song:Z`) for users who know what they want, sharing the same `musicbot/library.py` index.
- Playlist command UX improvements (flagged by the user as a separate, future effort).
