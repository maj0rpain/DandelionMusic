# CLAUDE.md

DandelionMusic (package `doybot-music`): a Discord music bot (discord.py, `yt-dlp`, ffmpeg). Requires Python >= 3.13.

## Commands

Dependencies are managed only with [`uv`](https://docs.astral.sh/uv/); `uv.lock` is the single source of truth (no `requirements.txt`).

```bash
uv sync                          # install deps
uv run python -m musicbot        # run the bot (run.py forwards here)
uv run --group dev pytest        # tests (see tests/CLAUDE.md)
docker compose up --build        # run in Docker
uv run --group dev pre-commit install  # once per clone: run the hooks on every commit
uv run --group dev pre-commit run --all  # black -l 79, flake8 --ignore E203,W503
```

Setup: copy `.env.sample` to `.env`; `BOT_TOKEN` is required.

## Module map

- `musicbot/__main__.py` / `run.py` - entrypoint; `run.py` is a thin forwarder (PyInstaller entry).
- `musicbot/bot.py` - `MusicBot`, custom `Context`; user-facing replies go through `ctx.send(...)`, not raw channel sends.
- `musicbot/admission.py` - request admission: per request kind, the checks, the join and the current controller; every Music command, General's voice commands, player button, pick and reaction goes through it.
- `musicbot/utils.py` - the joining-command marker (`joining_command`), the DJ and owner checks, `settings_admin_check` (administrator or application owner only, for `d!setting`), `CheckError`, and the voice connect port (`connect_to`, `check_voice_permissions`).
- `musicbot/sessions.py` - `GuildSessions` (`bot.sessions`): each guild's settings, controller and player panel, their creation, lookup (`None` when not registered) and disposal.
- `musicbot/audiocontroller.py` - per-guild controller over the queue: volume, loop, inactivity timer, preload sweep; its public methods delegate the start/advance/stop state machine to `Playback`.
- `musicbot/player_panel.py` - `PlayerPanel` (one per guild, built by `GuildSessions`): the destination channel, announcements, the player message, its refresh and its stale-message/expired-token recovery; implements the controller's panel port. `PanelSnapshot` (defined in `musicbot/panel_snapshot.py` so the controller can build one without importing the panel) is the controller's read-only state it draws from.
- `musicbot/playback.py` - `Playback` (one per controller) and `PlaybackState`: the start token, pending start, picked-next song, playing flag, disconnect guard, the `after=` hop, the one failed-track path (a track that fails to preload or ends on its own having played nothing, found by its audio probe), and when the playlist backup is written; depends only on ports the controller injects (callables, the `timer` and the `log_label`), never `bot`, `guild` or `Playlist`.
- `musicbot/playlist.py` / `musicbot/song.py` - queue/history and per-track metadata.
- `musicbot/playlists.py` - saved playlists: the only reader/writer of `SavedPlaylist.songs_json`, which it also reads in the legacy bare-string format; callers use `PlaylistEntry`/`PlaylistRef`/`PlaylistContents`/`SavedEntry`.
- `musicbot/truncation.py` - `Truncation` (a cut-short Spotify playlist's reported total and its `TruncationReason`) with the one cut-short rule (`Truncation.of`) and the user notice; imports only `config`, `enum` and `dataclasses`.
- `musicbot/outcome.py` - `EnqueueOutcome` (what adding a request's songs did) and `render`, the one function that turns it into a `Reply` or nothing; imports neither the controller nor voice machinery.
- `musicbot/loader.py` - yt-dlp extraction in two worker processes, the load lane and the preload lane; calling it before `init()` raises `LoaderNotRunning`.
- `musicbot/message_link.py` - the message-link gate (`check_message_link`, `MESSAGE_LINK_PATTERN`): refuses a Discord message link the requesting member cannot read; imports nothing from `musicbot.loader` or `musicbot.linkutils` at module level.
- `musicbot/linkutils.py` - URL classification, short-link resolution (`SHORT_LINK_HOSTS`, `resolve_short_link`) and Spotify resolution.
- `musicbot/library_metadata.py` - library enrichment (Last.fm/Spotify stats and art, embedded covers): `Enricher(clock, spotify, lastfm_session)` holds the six `CachedLookup` lookups (Last.fm and Spotify artist and album, the tag read and the artwork read, the latter with a `ByteBoundedStore`), and the public `get_enrichment(index, artist, album)` delegates to `default_enricher`; an index rebuild clears them by itself (the generation check).
- `musicbot/cached_lookup.py` - `CachedLookup`: a remote lookup's cache, in-flight dedup (shielded), timeout and failure cooldown, around a backend passed in and an optional passed-in store (a `dict` by default; `ByteBoundedStore` keeps the art cache's byte-budget and entry-cap eviction); imports only the standard library.
- `musicbot/library_browse.py` - the library browse cursor.
- `musicbot/library_session.py` - `BrowseSession` and `Frame`: the browse cursor (private to the session) plus what the browse message shows (drawn enrichment, attachment key, undo, one synchronous frame per draw, each from one screen); Discord-free. `LibraryBrowseView` is its Discord adapter.
- `musicbot/library_display.py` - the library display helpers: `KIND_EMOJI` and `trim()` (UTF-16 code units), which the browser and the search results share, and `NAME_TITLE_LIMIT`, the browse title's trim limit; Discord-free, imports nothing from the project.
- `musicbot/views.py` - `OwnedView`: a single-owner view's owner check, silent refusal, message and message edit; `SearchView` and `LibraryView` subclass it. `MusicButton`: the admission-checked button the player panel and search picks use.
- `musicbot/settings.py` - SQLAlchemy models, auto-migrations; the `SETTINGS` table of `SettingDescriptor`s is the single place a guild setting is described (its column aside): it drives defaults, conversion, the `d!setting show` embed and the generated `d!setting` subcommands.
- `musicbot/guild_whitelist.py` - the guild whitelist: admission (`load` -> `Whitelist.admits`), `add`/`remove` returning a `WhitelistChange` (outcome and guilds to leave), and the one-time `GUILD_WHITELIST` env import; the `WhitelistedGuild` table stays in `settings.py`.
- `musicbot/commands/` - the `music`, `general`, `developer` cogs.
- `musicbot/plugins/button.py` - optional reaction-button plugin (`ENABLE_BUTTON_PLUGIN`).
- `config/config.py` - `Config`: class attributes are the settings schema and defaults, overridden from `.env`.

## Rules

- `Config` is read-only; runtime state goes in the DB - see ADR-0001 (`docs/adr/0001-config-is-read-only.md`).
- A new setting is a `Config` class attribute with a comment directly above it (parsed by `get_comments()`).
- Don't move `_help`/`_help_autocomplete` into `MusicBot`; they stay module-level (`musicbot/bot.py`).
- discord.py's `after=` callback runs on its audio thread; it only hops to the loop (`call_soon_threadsafe`). `Playback`'s track-ended handling and everything it calls are loop-only (`musicbot/playback.py`).
- `musicbot/audiocontroller.py` imports nothing from `discord.ui`, and sends no user-facing message; the player panel does (a test asserts the import).
- `musicbot/library_browse.py` imports only `musicbot.library` and `typing` (`tests/test_library_browse.py` asserts it) - don't add imports.
- `musicbot/library_session.py` imports only `musicbot.library`, `musicbot.library_browse`, `musicbot.library_display` and `typing`, plus `musicbot.library_metadata` inside `if TYPE_CHECKING:` only (`tests/test_library_session.py` asserts it) - don't add imports.
- `musicbot/library_display.py` imports nothing from the project (`tests/test_library_display.py` asserts it) - don't add imports.
- `musicbot/linkutils.py` imports nothing from `musicbot.loader` (`tests/test_linkutils.py` asserts it) - don't add that import.

## Agent skills

### Issue tracker

GitHub Issues on `maj0rpain/DandelionMusic`, via `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, unchanged (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root (`GLOSSARY.md` is created lazily by `/domain-modeling`). See `docs/agents/domain.md`.
