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
- `musicbot/utils.py` - command and button checks (`play_check`, `player_check`, `voice_check`), the joining-command marker (`joining_command`), and voice connect/join (`join_needed`, `join_target`, `join_voice`, `connect_to`).
- `musicbot/sessions.py` - `GuildSessions` (`bot.sessions`): each guild's settings and controller, their creation, lookup (`None` when not registered) and disposal.
- `musicbot/audiocontroller.py` - per-guild playback state machine (volume, loop, inactivity timer, playlist backup).
- `musicbot/playlist.py` / `musicbot/song.py` - queue/history and per-track metadata.
- `musicbot/playlists.py` - saved playlists: the only reader/writer of `SavedPlaylist.songs_json` outside the legacy migration in `settings.py`; callers use `PlaylistEntry`/`PlaylistRef`/`PlaylistContents`/`SavedEntry`.
- `musicbot/truncation.py` - `Truncation` (a cut-short Spotify playlist's reported total and its `TruncationReason`) with the one cut-short rule (`Truncation.of`) and the user notice; imports only `config`, `enum` and `dataclasses`.
- `musicbot/loader.py` - yt-dlp extraction in two worker processes, the load lane and the preload lane; calling it before `init()` raises `LoaderNotRunning`.
- `musicbot/linkutils.py` - URL classification and Spotify resolution.
- `musicbot/library_metadata.py` - library enrichment (Last.fm/Spotify stats and art, embedded covers); the four remote lookups are `CachedLookup` instances, and `clear_caches()` clears them with the local caches.
- `musicbot/cached_lookup.py` - `CachedLookup`: a remote lookup's cache, in-flight dedup (shielded), timeout and failure cooldown, around a backend passed in; imports only the standard library.
- `musicbot/library_browse.py` - the library browse cursor; `LibraryBrowseView` is its Discord adapter.
- `musicbot/views.py` - `OwnedView`: a single-owner view's owner check, silent refusal, message and message edit; `SearchView` and `LibraryView` subclass it.
- `musicbot/settings.py` - SQLAlchemy models, auto-migrations, guild whitelist; the `SETTINGS` table of `SettingDescriptor`s is the single place a guild setting is described (its column aside): it drives defaults, conversion, the `d!setting show` embed and the generated `d!setting` subcommands.
- `musicbot/commands/` - the `music`, `general`, `developer` cogs.
- `musicbot/plugins/button.py` - optional reaction-button plugin (`ENABLE_BUTTON_PLUGIN`).
- `config/config.py` - `Config`: class attributes are the settings schema and defaults, overridden from `.env`.

## Rules

- `Config` is read-only; runtime state goes in the DB - see ADR-0001 (`docs/adr/0001-config-is-read-only.md`).
- A new setting is a `Config` class attribute with a comment directly above it (parsed by `get_comments()`).
- Don't move `_help`/`_help_autocomplete` into `MusicBot`; they stay module-level (`musicbot/bot.py`).
- discord.py's `after=` callback runs on its audio thread; it only hops to the loop (`call_soon_threadsafe`). `next_song` and everything it calls are loop-only (`musicbot/audiocontroller.py`).
- `musicbot/library_browse.py` imports only `musicbot.library` and `typing` (`tests/test_library_browse.py` asserts it) - don't add imports.

## Agent skills

### Issue tracker

GitHub Issues on `maj0rpain/DandelionMusic`, via `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, unchanged (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root (`GLOSSARY.md` is created lazily by `/domain-modeling`). See `docs/agents/domain.md`.
