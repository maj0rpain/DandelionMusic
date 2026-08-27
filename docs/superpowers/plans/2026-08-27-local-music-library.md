# Local Music Library Browsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Discord users browse a local `Artist/Album/song.ext` music collection via an interactive click-through menu and queue a song, album, or artist at any point while browsing.

**Architecture:** A new `musicbot/library.py` module walks the configured folder once at startup into an in-memory `{artist: {album: [filenames]}}` index (manually refreshable via a command, never live-watched). Local tracks are represented as `file://` URIs and a new `SiteTypes.LOCAL_LIBRARY`, so they flow through the bot's *existing* queue/playback/playlist-save-load pipeline unchanged. A new `musicbot/commands/library.py` Cog provides `d!library refresh` and `d!library browse`, the latter driving a single stateful `discord.ui.View` that redraws itself as the user descends artist → album → song.

**Tech Stack:** Python 3.13+, discord.py (`discord.ui.View`/`Select`/`Button`), existing project modules (`config`, `musicbot.loader`, `musicbot.linkutils`, `musicbot.audiocontroller`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-27-local-music-library-design.md`

## Global Constraints

- No test suite exists in this repo (confirmed in `CLAUDE.md`) — every task's "test" step is a standalone verification script run via `uv run python -c "..."`, not pytest. Follow this project's own conventions, not the generic pytest examples in the writing-plans skill.
- Lint: `black -l 79` and `flake8 --ignore E203,W503`, pinned in `.pre-commit-config.yaml` to `black==25.1.0`/`flake8==7.3.0` specifically — **always check against those exact pinned versions** (`uv tool run --from black==25.1.0 black ...`), not whatever `uv tool run --from black black` resolves to latest, which can produce false-positive diffs against a newer/different formatting version.
- Every new user-facing string goes in `config/en.json` as a `config.SOME_KEY` constant, never a hardcoded string in command code — matches every existing command in this codebase.
- `ENABLE_LOCAL_LIBRARY` defaults to `False`; nothing in this plan should change behavior for a deployment that doesn't set it.
- Local library queue actions must call `utils.play_check(ctx)` before queueing (mirrors what `Music.cog_check` already does for `d!play`, including the voice-channel auto-join) — but merely *opening* the browse menu must not require being in a voice channel.
- `ephemeral=True` only works for interaction-based (slash) invocations. Since `d!library browse` is a hybrid command also usable as plain `d!library browse` text, and a plain text message can never be ephemeral, the browse response is ephemeral only when `ctx.interaction is not None`; either way, the view must reject interactions from anyone but the original invoker (`interaction_check`), since a non-ephemeral text-command response is visible/clickable by the whole channel.

---

## Task 1: Config and user-facing messages

**Files:**
- Modify: `config/config.py`
- Modify: `config/en.json`
- Modify: `.env.sample`

**Interfaces:**
- Produces: `config.ENABLE_LOCAL_LIBRARY: bool`, `config.MUSIC_LIBRARY_PATH: str`, `config.LIBRARY_FILE_MISSING`, `config.LIBRARY_NOT_CONFIGURED`, `config.LIBRARY_EMPTY`, `config.LIBRARY_REFRESHED` (a `.format(artists=, albums=, songs=)` template — confirmed safe: `config/utils.py`'s `Formatter`/`string.Template.safe_substitute` step that runs on every `en.json` string at load time does not touch `{}`-style braces, only its own `$`-style delimiter, so plain `str.format()` at the call site is safe), `config.HELP_LIBRARY_SHORT`, `config.HELP_LIBRARY_LONG`, `config.HELP_LIBRARY_REFRESH_SHORT`, `config.HELP_LIBRARY_REFRESH_LONG`, `config.HELP_LIBRARY_BROWSE_SHORT`, `config.HELP_LIBRARY_BROWSE_LONG`.

- [ ] **Step 1: Add the two config attributes**

In `config/config.py`, add near `ENABLE_BUTTON_PLUGIN` (keep the existing comment-above-attribute convention used throughout this class — comments are parsed by `get_comments()` for docs/exe use):

```python
    # enables browsing/queueing a local music library via Discord UI
    ENABLE_LOCAL_LIBRARY = False

    # root folder for the local music library, expected to contain
    # Artist/Album/song.ext (extensions from SUPPORTED_EXTENSIONS)
    MUSIC_LIBRARY_PATH = ""
```

- [ ] **Step 2: Add the new messages to `config/en.json`**

Add these keys (alongside the existing `SONGINFO_*`/`HELP_*` keys, same flat structure):

```json
  "LIBRARY_FILE_MISSING": "Error: This library file no longer exists on disk. Try running `d!library refresh`.",
  "LIBRARY_NOT_CONFIGURED": "The local library isn't configured. Set MUSIC_LIBRARY_PATH in your .env file.",
  "LIBRARY_EMPTY": "No music found in the library. Check MUSIC_LIBRARY_PATH and run `d!library refresh`.",
  "LIBRARY_REFRESHED": "Library refreshed: {artists} artists, {albums} albums, {songs} songs.",
  "HELP_LIBRARY_SHORT": "Browse and queue local music library",
  "HELP_LIBRARY_LONG": "Browse the local music library and queue a song, album, or artist.",
  "HELP_LIBRARY_REFRESH_SHORT": "Rescan the local music library",
  "HELP_LIBRARY_REFRESH_LONG": "Rescans MUSIC_LIBRARY_PATH and rebuilds the browsable index.",
  "HELP_LIBRARY_BROWSE_SHORT": "Browse the local music library",
  "HELP_LIBRARY_BROWSE_LONG": "Opens an interactive browser to queue a song, album, or artist from the local music library."
```

Validate the JSON is well-formed (this file is parsed as JSONC, and a trailing-comma or bracket mistake breaks every config string in the bot):

```bash
uv run python -c "import jsonc; jsonc.load(open('config/en.json')); print('en.json OK')"
```

- [ ] **Step 3: Add the two env vars to `.env.sample`**

Read the file first to match its exact existing comment-then-`KEY=value` style (e.g. the block ending in `GUILD_WHITELIST=[]`), then append:

```
# enables browsing/queueing a local music library via Discord UI
ENABLE_LOCAL_LIBRARY=False

# root folder for the local music library, expected to contain
# Artist/Album/song.ext
MUSIC_LIBRARY_PATH=
```

- [ ] **Step 4: Verify config loads with the new defaults**

```bash
uv run python -c "
from unittest.mock import patch
import os
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    from config import config
    assert config.ENABLE_LOCAL_LIBRARY is False
    assert config.MUSIC_LIBRARY_PATH == ''
    assert 'artists' in config.LIBRARY_REFRESHED
    print(config.LIBRARY_REFRESHED.format(artists=3, albums=10, songs=120))
"
```

Expected output: `Library refreshed: 3 artists, 10 albums, 120 songs.` with no errors.

- [ ] **Step 5: Lint and commit**

```bash
uv tool run pre-commit run --files config/config.py config/en.json .env.sample
git add config/config.py config/en.json .env.sample
git commit -m "Add config and messages for local music library browsing"
```

---

## Task 2: Library index module

**Files:**
- Create: `musicbot/library.py`
- Modify: `musicbot/bot.py` (wire `build_index()` into startup)

**Interfaces:**
- Consumes: `config.MUSIC_LIBRARY_PATH: str`, `config.SUPPORTED_EXTENSIONS: tuple`.
- Produces: `library.LibraryIndex = Dict[str, Dict[str, List[str]]]`, `library.build_index() -> LibraryIndex`, `library.get_index() -> LibraryIndex`, `library.song_path(artist: str, album: str, filename: str) -> pathlib.Path`, `library.song_uri(artist: str, album: str, filename: str) -> str`, `library.counts(index: LibraryIndex) -> Tuple[int, int, int]` (artists, albums, songs). These four functions/the `LibraryIndex` alias are what every later task imports from `musicbot.library`.

- [ ] **Step 1: Write a verification script demonstrating the module doesn't exist yet**

```bash
uv run python -c "from musicbot import library" 2>&1
```

Expected: `ModuleNotFoundError: No module named 'musicbot.library'`

- [ ] **Step 2: Create `musicbot/library.py`**

```python
from pathlib import Path
from typing import Dict, List, Tuple

from config import config

LibraryIndex = Dict[str, Dict[str, List[str]]]

_index: LibraryIndex = {}


def build_index() -> LibraryIndex:
    """Walks config.MUSIC_LIBRARY_PATH (expected layout:
    Artist/Album/song.ext) and rebuilds the in-memory index.
    Returns the new index."""
    global _index
    new_index: LibraryIndex = {}
    root = Path(config.MUSIC_LIBRARY_PATH) if config.MUSIC_LIBRARY_PATH else None

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
        len(songs)
        for albums in index.values()
        for songs in albums.values()
    )
    return artist_count, album_count, song_count
```

- [ ] **Step 3: Verify indexing against a real temp directory tree**

```bash
uv run python -c "
import os, tempfile
from unittest.mock import patch
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    from config import config
    from musicbot import library

with tempfile.TemporaryDirectory() as tmp:
    os.makedirs(f'{tmp}/Artist A/Album 1')
    os.makedirs(f'{tmp}/Artist A/Album 2')
    os.makedirs(f'{tmp}/Artist B/Album 1')
    os.makedirs(f'{tmp}/Artist A/NotAnAlbumJustAFile')  # dir with no valid songs
    open(f'{tmp}/Artist A/Album 1/song1.mp3', 'w').close()
    open(f'{tmp}/Artist A/Album 1/song2.mp3', 'w').close()
    open(f'{tmp}/Artist A/Album 1/notes.txt', 'w').close()  # unsupported ext
    open(f'{tmp}/Artist A/Album 2/song3.mp3', 'w').close()
    open(f'{tmp}/Artist B/Album 1/song4.mp3', 'w').close()

    config.MUSIC_LIBRARY_PATH = tmp
    index = library.build_index()

    assert set(index.keys()) == {'Artist A', 'Artist B'}, index.keys()
    assert set(index['Artist A'].keys()) == {'Album 1', 'Album 2'}, index['Artist A']
    assert index['Artist A']['Album 1'] == ['song1.mp3', 'song2.mp3']
    assert index['Artist A']['Album 2'] == ['song3.mp3']
    assert index['Artist B']['Album 1'] == ['song4.mp3']
    assert 'NotAnAlbumJustAFile' not in index.get('Artist A', {})

    assert library.counts(index) == (2, 3, 4)

    uri = library.song_uri('Artist A', 'Album 1', 'song1.mp3')
    assert uri == library.song_path('Artist A', 'Album 1', 'song1.mp3').as_uri()
    assert uri.startswith('file://')

    # unconfigured / missing path -> empty index, not a crash
    config.MUSIC_LIBRARY_PATH = ''
    assert library.build_index() == {}
    config.MUSIC_LIBRARY_PATH = '/does/not/exist'
    assert library.build_index() == {}

print('library index tests passed')
"
```

Expected: `library index tests passed` with no assertion errors.

- [ ] **Step 4: Wire `build_index()` into bot startup**

In `musicbot/bot.py`, add the import (alongside the other `from musicbot import ...` lines) and call it in `setup_hook`, before extensions load so the index exists before any command can be invoked:

```python
from musicbot import library
```

```python
    async def setup_hook(self):
        if config.ENABLE_LOCAL_LIBRARY:
            library.build_index()
        for extension in self.initial_extensions:
            await self.load_extension(extension)
        if config.ENABLE_SLASH_COMMANDS:
            await self.tree.sync()
```

- [ ] **Step 5: Verify syntax and imports**

```bash
uv run python -c "import ast; ast.parse(open('musicbot/bot.py').read()); print('syntax OK')"
uv run python -c "
from unittest.mock import patch
import os
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    import musicbot.bot
    import musicbot.library
print('imports OK')
"
```

- [ ] **Step 6: Lint and commit**

```bash
uv tool run pre-commit run --files musicbot/library.py musicbot/bot.py
git add musicbot/library.py musicbot/bot.py
git commit -m "Add local music library index module"
```

---

## Task 3: SiteTypes.LOCAL_LIBRARY and identify_url()

**Files:**
- Modify: `musicbot/linkutils.py`

**Interfaces:**
- Produces: `SiteTypes.LOCAL_LIBRARY` (new enum member). `identify_url(url: str)` now returns this for any `file://`-scheme string.

- [ ] **Step 1: Verify current behavior (baseline before the change)**

```bash
uv run python -c "
from unittest.mock import patch
import os
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    from musicbot.linkutils import identify_url, SiteTypes
    # file:// URIs currently fall through to CUSTOM (matches by extension),
    # not a dedicated type - this is what we're about to change
    print(identify_url('file:///tmp/Music/Artist/Album/song.mp3'))
"
```

Expected: `SiteTypes.CUSTOM` (confirms the starting point this task changes).

- [ ] **Step 2: Add the new `SiteTypes` member**

In `musicbot/linkutils.py`, in the `SiteTypes` enum:

```python
class SiteTypes(Enum):
    SPOTIFY = auto()
    YT_DLP = auto()
    CUSTOM = auto()
    LOCAL_LIBRARY = auto()
    UNKNOWN = auto()
    NOT_URL = auto()
```

- [ ] **Step 3: Detect it in `identify_url()`, before the generic `CUSTOM` check**

```python
def identify_url(url: str) -> Union[SiteTypes, ExtractorT]:
    if not url_regex.fullmatch(url):
        return SiteTypes.NOT_URL

    if spotify_regex.match(url):
        return SiteTypes.SPOTIFY

    if url.startswith("file://"):
        return SiteTypes.LOCAL_LIBRARY

    if ie := get_ie(url):
        return ie

    if urlparse(url).path.lower().endswith(config.SUPPORTED_EXTENSIONS):
        return SiteTypes.CUSTOM

    # If no match
    return SiteTypes.UNKNOWN
```

- [ ] **Step 4: Verify the new classification, and that nothing else regressed**

```bash
uv run python -c "
from unittest.mock import patch
import os
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    from musicbot.linkutils import identify_url, SiteTypes

    assert identify_url('file:///tmp/Music/Artist/Album/song.mp3') == SiteTypes.LOCAL_LIBRARY
    assert identify_url('file:///tmp/Music/Artist/Album/song.mp3') != SiteTypes.CUSTOM

    # existing classifications must be unaffected
    assert identify_url('https://example.com/some/file.mp3') == SiteTypes.CUSTOM
    assert identify_url('not a url at all, just search text') == SiteTypes.NOT_URL
    assert identify_url('https://open.spotify.com/track/abc123') == SiteTypes.SPOTIFY

print('identify_url tests passed')
"
```

Expected: `identify_url tests passed`.

- [ ] **Step 5: Lint and commit**

```bash
uv tool run pre-commit run --files musicbot/linkutils.py
git add musicbot/linkutils.py
git commit -m "Add SiteTypes.LOCAL_LIBRARY for file:// URIs"
```

---

## Task 4: Load local-library tracks in loader.py

**Files:**
- Modify: `musicbot/loader.py`

**Interfaces:**
- Consumes: `SiteTypes.LOCAL_LIBRARY` (Task 3), `config.LIBRARY_FILE_MISSING` (Task 1).
- Produces: `_load_song()` now handles `SiteTypes.LOCAL_LIBRARY` tracks, raising `SongError(config.LIBRARY_FILE_MISSING)` for a missing file — this is what every later "queue this" call site relies on to detect and report a stale index entry.

- [ ] **Step 1: Add the `pathlib` import**

`musicbot/loader.py` doesn't import `Path` yet. Add it near the other stdlib imports at the top:

```python
from pathlib import Path
```

- [ ] **Step 2: Add the `LOCAL_LIBRARY` branch to `_load_song()`**

Add this `elif` alongside the existing `elif host == SiteTypes.CUSTOM:` branch (same function, same `if`/`elif` chain), before the final `else:  # host is info extractor` branch:

```python
    elif host == SiteTypes.LOCAL_LIBRARY:
        path = Path.from_uri(track)
        if not path.is_file():
            raise SongError(config.LIBRARY_FILE_MISSING)
        data = {"url": track, "webpage_url": track, "title": path.stem}
```

- [ ] **Step 3: Verify against a real temp file, present and missing**

```bash
uv run python -c "
import os, tempfile
from pathlib import Path
from unittest.mock import patch
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    from musicbot.loader import _load_song, SongError

with tempfile.TemporaryDirectory() as tmp:
    song_path = Path(tmp) / 'My Song.mp3'
    song_path.write_text('fake audio data')
    uri = song_path.as_uri()

    song = _load_song(uri)
    assert song.title == 'My Song', song.title
    assert song.url == uri
    assert song.webpage_url == uri

    missing_uri = (Path(tmp) / 'Deleted Song.mp3').as_uri()
    try:
        _load_song(missing_uri)
        assert False, 'expected SongError for missing file'
    except SongError as e:
        print('correctly raised SongError:', e)

print('loader local-library tests passed')
"
```

Expected: prints the `SongError` message text, then `loader local-library tests passed`.

- [ ] **Step 4: Lint and commit**

```bash
uv tool run pre-commit run --files musicbot/loader.py
git add musicbot/loader.py
git commit -m "Load local library tracks in loader._load_song()"
```

---

## Task 5: Library Cog skeleton and `d!library refresh`

**Files:**
- Create: `musicbot/commands/library.py`

**Interfaces:**
- Consumes: `library.build_index()`, `library.counts()` (Task 2), `config.LIBRARY_NOT_CONFIGURED`, `config.LIBRARY_REFRESHED` (Task 1), `utils.dj_check` (existing).
- Produces: `Library` Cog with a `library` `hybrid_group` and `library refresh` subcommand. `LibraryBrowseView` and `library browse` are added in Tasks 6-8, in this same file.

- [ ] **Step 1: Create the Cog with just `refresh`**

```python
from discord.ext import commands

from config import config
from musicbot import library
from musicbot.bot import MusicBot
from musicbot.utils import dj_check


class Library(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    async def cog_check(self, ctx):
        ctx.audiocontroller = ctx.bot.audio_controllers[ctx.guild]
        return True

    @commands.hybrid_group(
        name="library",
        description=config.HELP_LIBRARY_SHORT,
        help=config.HELP_LIBRARY_LONG,
        invoke_without_command=True,
    )
    async def _library(self, ctx):
        await ctx.send("Use subcommands: `refresh`, `browse`.")

    @_library.command(
        name="refresh",
        description=config.HELP_LIBRARY_REFRESH_SHORT,
        help=config.HELP_LIBRARY_REFRESH_LONG,
    )
    @commands.check(dj_check)
    async def _library_refresh(self, ctx):
        if not config.MUSIC_LIBRARY_PATH:
            await ctx.send(config.LIBRARY_NOT_CONFIGURED)
            return
        index = library.build_index()
        artists, albums, songs = library.counts(index)
        await ctx.send(
            config.LIBRARY_REFRESHED.format(
                artists=artists, albums=albums, songs=songs
            )
        )


async def setup(bot: MusicBot):
    await bot.add_cog(Library(bot))
```

- [ ] **Step 2: Verify syntax and import**

```bash
uv run python -c "import ast; ast.parse(open('musicbot/commands/library.py').read()); print('syntax OK')"
uv run python -c "
from unittest.mock import patch
import os
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    import musicbot.commands.library
print('import OK')
"
```

- [ ] **Step 3: Verify `refresh` end-to-end against a real MusicBot + temp library**

```bash
uv run python -c "
import asyncio, os, tempfile
from unittest.mock import patch, MagicMock, AsyncMock
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    import discord
    from config import config
    from musicbot.bot import MusicBot

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(f'{tmp}/Artist A/Album 1')
        open(f'{tmp}/Artist A/Album 1/song1.mp3', 'w').close()
        config.MUSIC_LIBRARY_PATH = tmp

        bot = MusicBot(
            initial_extensions=[],
            command_prefix='d!',
            case_insensitive=True,
            intents=discord.Intents.default(),
        )
        await bot.load_extension('musicbot.commands.library')

        guild = MagicMock()
        bot.audio_controllers[guild] = MagicMock()

        ctx = MagicMock()
        ctx.bot = bot
        ctx.guild = guild
        ctx.send = AsyncMock()

        cmd = bot.get_command('library refresh')
        await cmd.callback(bot.get_cog('Library'), ctx)

        ctx.send.assert_called_once()
        message = ctx.send.call_args[0][0]
        print('refresh replied:', message)
        assert '1 artists, 1 albums, 1 songs' in message

print('OK')

asyncio.run(main())
"
```

Expected: prints the refresh reply text containing `1 artists, 1 albums, 1 songs`, then `OK`.

- [ ] **Step 4: Lint and commit**

```bash
uv tool run pre-commit run --files musicbot/commands/library.py
git add musicbot/commands/library.py
git commit -m "Add Library cog with d!library refresh"
```

---

## Task 6: Browse View — navigation

**Files:**
- Modify: `musicbot/commands/library.py`

**Interfaces:**
- Consumes: `library.get_index()` (Task 2).
- Produces: `LibraryBrowseView` (a `discord.ui.View` subclass) with `.artist: Optional[str]`, `.album: Optional[str]`, `.page: int`, `.entries() -> List[str]`, `.title() -> str`, `.embed() -> discord.Embed`, `.build_items()`, `.render(interaction)`, `.descend(interaction, chosen: str)`, `.go_back(interaction)`. Queueing (`.queue_pairs`, the `QueueLevelButton` callback, and what `descend()` does at the song level) is added in Task 7 — for this task, selecting a song at the leaf level is a no-op placeholder that Task 7 replaces (the one narrow exception to "no placeholders" in this plan, since Task 7 exists specifically to fill it in the very next task and the interface above states exactly what replaces it).

- [ ] **Step 1: Add pagination constant and navigation UI classes**

Add to `musicbot/commands/library.py`, above the `Library` Cog class:

```python
from typing import List, Optional

import discord

PAGE_SIZE = 25


class LibrarySelect(discord.ui.Select):
    def __init__(self, entries: List[str], browse_view: "LibraryBrowseView"):
        self._entries = entries
        self.browse_view = browse_view
        super().__init__(
            placeholder="Choose...",
            options=[
                discord.SelectOption(label=entry[:100], value=str(i))
                for i, entry in enumerate(entries)
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        chosen = self._entries[int(self.values[0])]
        await self.browse_view.descend(interaction, chosen)


class BackButton(discord.ui.Button):
    def __init__(self, browse_view: "LibraryBrowseView"):
        super().__init__(label="Back", style=discord.ButtonStyle.grey, row=1)
        self.browse_view = browse_view

    async def callback(self, interaction: discord.Interaction):
        await self.browse_view.go_back(interaction)


class PageButton(discord.ui.Button):
    def __init__(self, browse_view: "LibraryBrowseView", delta: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.blurple, row=2)
        self.browse_view = browse_view
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        self.browse_view.page += self.delta
        await self.browse_view.render(interaction)
```

- [ ] **Step 2: Add the `LibraryBrowseView` class**

```python
class LibraryBrowseView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.index = library.get_index()
        self.artist: Optional[str] = None
        self.album: Optional[str] = None
        self.page = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This browser belongs to someone else.", ephemeral=True
            )
            return False
        return True

    def entries(self) -> List[str]:
        if self.artist is None:
            return sorted(self.index.keys())
        if self.album is None:
            return sorted(self.index.get(self.artist, {}).keys())
        return sorted(self.index.get(self.artist, {}).get(self.album, []))

    def title(self) -> str:
        if self.artist is None:
            return "Music Library — Artists"
        if self.album is None:
            return f"Music Library — {self.artist} — Albums"
        return f"Music Library — {self.artist} / {self.album} — Songs"

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=self.title(), color=config.EMBED_COLOR)
        if not self.entries():
            embed.description = config.LIBRARY_EMPTY
        return embed

    def build_items(self):
        self.clear_items()
        entries = self.entries()
        page_entries = entries[
            self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE
        ]
        if page_entries:
            self.add_item(LibrarySelect(page_entries, self))
        if self.artist is not None:
            self.add_item(BackButton(self))
        if self.page > 0:
            self.add_item(PageButton(self, -1, "◀ Prev"))
        if (self.page + 1) * PAGE_SIZE < len(entries):
            self.add_item(PageButton(self, 1, "Next ▶"))

    async def render(self, interaction: discord.Interaction):
        self.build_items()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def descend(self, interaction: discord.Interaction, chosen: str):
        self.page = 0
        if self.artist is None:
            self.artist = chosen
            await self.render(interaction)
        elif self.album is None:
            self.album = chosen
            await self.render(interaction)
        else:
            # Task 7 replaces this with queueing the chosen song.
            await interaction.response.send_message(
                f"(queueing {chosen!r} - implemented in Task 7)",
                ephemeral=True,
            )

    async def go_back(self, interaction: discord.Interaction):
        self.page = 0
        if self.album is not None:
            self.album = None
        else:
            self.artist = None
        await self.render(interaction)
```

Add the needed imports at the top of the file (combine with Step 1's `from typing import List, Optional` / `import discord`):

```python
from config import config
from musicbot import library
```

(`config` and `library` are both already imported for the `Library` Cog from Task 5 — check before duplicating the import line.)

- [ ] **Step 3: Verify navigation state transitions directly**

```bash
uv run python -c "
import asyncio, os
from unittest.mock import patch, MagicMock, AsyncMock
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    from musicbot.commands.library import LibraryBrowseView
    from musicbot import library

async def main():
    library._index = {
        'Artist A': {'Album 1': ['song1.mp3', 'song2.mp3'], 'Album 2': ['song3.mp3']},
        'Artist B': {'Album 1': ['song4.mp3']},
    }
    ctx = MagicMock()
    ctx.author.id = 111

    view = LibraryBrowseView(ctx)
    assert view.entries() == ['Artist A', 'Artist B']
    assert view.title() == 'Music Library — Artists'

    interaction = MagicMock()
    interaction.user.id = 111
    interaction.response.edit_message = AsyncMock()

    await view.descend(interaction, 'Artist A')
    assert view.artist == 'Artist A'
    assert view.entries() == ['Album 1', 'Album 2']

    await view.descend(interaction, 'Album 1')
    assert view.album == 'Album 1'
    assert view.entries() == ['song1.mp3', 'song2.mp3']

    await view.go_back(interaction)
    assert view.album is None
    assert view.artist == 'Artist A'

    await view.go_back(interaction)
    assert view.artist is None

    # interaction_check rejects a different user
    other = MagicMock()
    other.user.id = 999
    other.response.send_message = AsyncMock()
    assert await view.interaction_check(other) is False
    other.response.send_message.assert_called_once()
    assert await view.interaction_check(interaction) is True

print('navigation tests passed')

asyncio.run(main())
"
```

Expected: `navigation tests passed`.

- [ ] **Step 4: Lint and commit**

```bash
uv tool run pre-commit run --files musicbot/commands/library.py
git add musicbot/commands/library.py
git commit -m "Add browse view navigation (artist/album/song, back, paging)"
```

---

## Task 7: Browse View — queueing

**Files:**
- Modify: `musicbot/commands/library.py`

**Interfaces:**
- Consumes: `library.song_uri()` (Task 2), `AudioController.process_song(track: str, user=...)` (existing, from `musicbot/audiocontroller.py`), `utils.play_check(ctx)` / `utils.CheckError` (existing), `SongError` (from `musicbot.loader`, Task 4).
- Produces: `LibraryBrowseView.queue_pairs(interaction, pairs: List[Tuple[str, str]])`, replaces the Task 6 placeholder in `descend()`, adds `QueueLevelButton` wired into `build_items()`.

- [ ] **Step 1: Add the import and the `QueueLevelButton` class**

```python
from musicbot.loader import SongError
from musicbot.utils import CheckError, play_check
```

```python
class QueueLevelButton(discord.ui.Button):
    def __init__(self, browse_view: "LibraryBrowseView"):
        label = (
            "Queue this Album"
            if browse_view.album is not None
            else "Queue this Artist"
        )
        super().__init__(label=label, style=discord.ButtonStyle.green, row=1)
        self.browse_view = browse_view

    async def callback(self, interaction: discord.Interaction):
        await self.browse_view.queue_current_level(interaction)
```

- [ ] **Step 2: Add queueing methods to `LibraryBrowseView`, and wire the button in**

Replace the `descend()` placeholder's `else` branch:

```python
    async def descend(self, interaction: discord.Interaction, chosen: str):
        self.page = 0
        if self.artist is None:
            self.artist = chosen
            await self.render(interaction)
        elif self.album is None:
            self.album = chosen
            await self.render(interaction)
        else:
            await self.queue_pairs(interaction, [(self.album, chosen)])
```

Add `QueueLevelButton` to `build_items()` (only meaningful once an artist is chosen):

```python
        if self.artist is not None:
            self.add_item(QueueLevelButton(self))
            self.add_item(BackButton(self))
```

(replaces the single `if self.artist is not None: self.add_item(BackButton(self))` line from Task 6)

Add the queueing methods:

```python
    async def queue_current_level(self, interaction: discord.Interaction):
        if self.album is not None:
            pairs = [
                (self.album, song)
                for song in self.index.get(self.artist, {}).get(self.album, [])
            ]
        else:
            pairs = [
                (album, song)
                for album, songs in self.index.get(self.artist, {}).items()
                for song in songs
            ]
        await self.queue_pairs(interaction, pairs)

    async def queue_pairs(self, interaction, pairs):
        try:
            await play_check(self.ctx)
        except CheckError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        queued = 0
        missing = []
        for album, filename in pairs:
            uri = library.song_uri(self.artist, album, filename)
            try:
                await self.ctx.audiocontroller.process_song(
                    uri, user=self.ctx.author
                )
                queued += 1
            except SongError:
                missing.append(filename)

        message = f"Queued {queued} song(s)."
        if missing:
            shown = ", ".join(missing[:5])
            message += f" {len(missing)} skipped (file not found): {shown}"
            if len(missing) > 5:
                message += f", and {len(missing) - 5} more"
            message += ". Try `d!library refresh`."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
```

- [ ] **Step 3: Verify queueing directly, including the missing-file path**

```bash
uv run python -c "
import asyncio, os
from unittest.mock import patch, MagicMock, AsyncMock
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    from musicbot.commands.library import LibraryBrowseView
    from musicbot import library
    from musicbot.loader import SongError

async def main():
    library._index = {
        'Artist A': {'Album 1': ['song1.mp3', 'song2.mp3']},
    }

    ctx = MagicMock()
    ctx.author.id = 111
    ctx.audiocontroller.process_song = AsyncMock(side_effect=[None, SongError('missing')])

    view = LibraryBrowseView(ctx)
    view.artist = 'Artist A'

    interaction = MagicMock()
    interaction.user.id = 111
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()

    with patch('musicbot.commands.library.play_check', AsyncMock(return_value=True)):
        await view.queue_current_level(interaction)

    interaction.response.send_message.assert_called_once()
    message = interaction.response.send_message.call_args[0][0]
    print('queue result message:', message)
    assert 'Queued 1 song(s)' in message
    assert '1 skipped' in message
    assert 'song2.mp3' in message
    assert ctx.audiocontroller.process_song.call_count == 2

print('queueing tests passed')

asyncio.run(main())
"
```

Expected: prints the summary message showing 1 queued / 1 skipped naming `song2.mp3`, then `queueing tests passed`.

- [ ] **Step 4: Lint and commit**

```bash
uv tool run pre-commit run --files musicbot/commands/library.py
git add musicbot/commands/library.py
git commit -m "Add queueing (song/album/artist) to the browse view"
```

---

## Task 8: `d!library browse` command and extension wiring

**Files:**
- Modify: `musicbot/commands/library.py` (add the `browse` subcommand)
- Modify: `musicbot/__main__.py` (conditionally load the extension)

**Interfaces:**
- Consumes: `LibraryBrowseView` (Tasks 6-7), `config.ENABLE_LOCAL_LIBRARY` (Task 1).
- Produces: `d!library browse` / `/library browse` hybrid command; `musicbot.commands.library` is loaded as an extension whenever `config.ENABLE_LOCAL_LIBRARY` is set.

- [ ] **Step 1: Add the `browse` subcommand to the `Library` Cog**

```python
    @_library.command(
        name="browse",
        description=config.HELP_LIBRARY_BROWSE_SHORT,
        help=config.HELP_LIBRARY_BROWSE_LONG,
    )
    async def _library_browse(self, ctx):
        if not config.MUSIC_LIBRARY_PATH:
            await ctx.send(config.LIBRARY_NOT_CONFIGURED)
            return
        if not library.get_index():
            await ctx.send(config.LIBRARY_EMPTY)
            return

        view = LibraryBrowseView(ctx)
        kwargs = {"embed": view.embed(), "view": view}
        # ephemeral only makes sense for an interaction (slash) response -
        # a plain text message from a prefix command can't be ephemeral
        if ctx.interaction is not None:
            kwargs["ephemeral"] = True
        await ctx.send(**kwargs)
```

- [ ] **Step 2: Load the extension conditionally in `musicbot/__main__.py`**

Find the existing block that conditionally appends `musicbot.plugins.button` when `config.ENABLE_BUTTON_PLUGIN` is set, and add a matching block for the library extension:

```python
if config.ENABLE_LOCAL_LIBRARY:
    initial_extensions.append("musicbot.commands.library")
```

- [ ] **Step 3: Verify syntax and full import chain**

```bash
uv run python -c "import ast; ast.parse(open('musicbot/commands/library.py').read()); ast.parse(open('musicbot/__main__.py').read()); print('syntax OK')"
uv run python -c "
from unittest.mock import patch
import os
with patch.dict(os.environ, {'BOT_TOKEN': 'x', 'ENABLE_LOCAL_LIBRARY': 'True'}):
    import musicbot.__main__ as m
    assert 'musicbot.commands.library' in m.initial_extensions
print('extension conditionally loaded OK')
"
```

- [ ] **Step 4: End-to-end verification — full browse flow against a real MusicBot**

```bash
uv run python -c "
import asyncio, os, tempfile
from unittest.mock import patch, MagicMock, AsyncMock
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    import discord
    from config import config
    from musicbot.bot import MusicBot
    from musicbot import library

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(f'{tmp}/Artist A/Album 1')
        open(f'{tmp}/Artist A/Album 1/song1.mp3', 'w').close()
        config.MUSIC_LIBRARY_PATH = tmp
        library.build_index()

        bot = MusicBot(
            initial_extensions=[],
            command_prefix='d!',
            case_insensitive=True,
            intents=discord.Intents.default(),
        )
        await bot.load_extension('musicbot.commands.library')

        guild = MagicMock()
        ac = MagicMock()
        ac.process_song = AsyncMock()
        bot.audio_controllers[guild] = ac

        ctx = MagicMock()
        ctx.bot = bot
        ctx.guild = guild
        ctx.author.id = 111
        ctx.interaction = None  # simulate a plain text invocation
        ctx.send = AsyncMock()

        cmd = bot.get_command('library browse')
        cog = bot.get_cog('Library')
        await cmd.callback(cog, ctx)

        ctx.send.assert_called_once()
        call_kwargs = ctx.send.call_args.kwargs
        assert 'ephemeral' not in call_kwargs, 'text invocation must not pass ephemeral'
        view = call_kwargs['view']
        assert view.entries() == ['Artist A']

        # simulate clicking through to the song and queueing it, exactly
        # as the LibrarySelect callback would
        interaction = MagicMock()
        interaction.user.id = 111
        interaction.response.edit_message = AsyncMock()
        with patch('musicbot.commands.library.play_check', AsyncMock(return_value=True)):
            await view.descend(interaction, 'Artist A')
            await view.descend(interaction, 'Album 1')
            interaction.response.is_done.return_value = False
            interaction.response.send_message = AsyncMock()
            await view.descend(interaction, 'song1.mp3')

        ac.process_song.assert_called_once()
        call_args = ac.process_song.call_args
        assert call_args.args[0].endswith('song1.mp3')
        assert call_args.kwargs['user'] is ctx.author

print('end-to-end browse flow verified')

asyncio.run(main())
"
```

Expected: `end-to-end browse flow verified`, no assertion errors.

- [ ] **Step 5: Verify the spec's core claim — playlist save/load needs no changes for local tracks**

The design spec's payoff for representing local tracks as `file://` URIs is that `d!playlist save`/`load` already round-trip any `{url, title}` pair with zero code changes. That claim is only indirectly covered so far (Task 3 tests `identify_url()` in isolation) — verify it end-to-end through the actual playlist save/load code paths:

```bash
uv run python -c "
import asyncio, json, os, tempfile
from unittest.mock import patch, MagicMock, AsyncMock
with patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
    from config import config
    from musicbot import library
    from musicbot.song import Song
    from musicbot.linkutils import get_site_type

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(f'{tmp}/Artist A/Album 1')
        open(f'{tmp}/Artist A/Album 1/song1.mp3', 'w').close()
        config.MUSIC_LIBRARY_PATH = tmp

        uri = library.song_uri('Artist A', 'Album 1', 'song1.mp3')

        # mirrors _playlist_save's exact serialization
        # (musicbot/commands/music.py: {'url': song.webpage_url, 'title': song.title})
        original = Song(get_site_type(uri), webpage_url=uri, url=uri, title='song1')
        songs_json = json.dumps([
            {'url': original.webpage_url, 'title': original.title}
        ])

        # mirrors _playlist_load's exact reconstruction
        # (Song(get_site_type(song_data['url']), song_data['url'], title=song_data['title'], playlist=playlist))
        song_data = json.loads(songs_json)[0]
        reloaded = Song(
            get_site_type(song_data['url']), song_data['url'], title=song_data['title']
        )

        from musicbot.linkutils import SiteTypes
        assert reloaded.host == SiteTypes.LOCAL_LIBRARY, reloaded.host
        assert reloaded.webpage_url == uri
        assert reloaded.title == 'song1'

print('playlist save/load round-trip verified for local-library tracks')

asyncio.run(main())
"
```

Expected: `playlist save/load round-trip verified for local-library tracks`, no assertion errors. If `reloaded.host` isn't `SiteTypes.LOCAL_LIBRARY`, the spec's "no playlist changes needed" claim is wrong and Tasks 3-4 need to be revisited before continuing.

- [ ] **Step 6: Full-repo lint check and manual smoke test note**

```bash
uv tool run pre-commit run --files musicbot/commands/library.py musicbot/__main__.py
```

This plan's verification is all mocked/unit-level (no test suite, no live Discord connection, matching this repo's existing testing approach per `CLAUDE.md`). Before merging, do one real manual check: set `ENABLE_LOCAL_LIBRARY=True` and `MUSIC_LIBRARY_PATH` to a real folder with a couple of test files, run the bot against a real Discord server, and click through `d!library browse` end to end (both as `d!library browse` and `/library browse`) to confirm the actual Discord UI renders and behaves as designed — the mocked tests above prove the logic is correct, not that Discord's UI renders it as expected.

- [ ] **Step 7: Commit**

```bash
git add musicbot/commands/library.py musicbot/__main__.py
git commit -m "Add d!library browse command and conditional extension loading"
```
