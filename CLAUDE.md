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

Always pass `--repo maj0rpain/DandelionMusic` to `gh pr create`: bare `gh` resolves to `upstream` (`solaluset`).

## Module map

- `musicbot/__main__.py` / `run.py` - entrypoint; `run.py` is a thin forwarder (PyInstaller entry).
- `musicbot/bot.py` - `MusicBot`, custom `Context`; user-facing replies go through `ctx.send(...)`, not raw channel sends.
- `musicbot/sessions.py` - `GuildSessions` (`bot.sessions`): each guild's settings and controller, their creation, lookup (`None` when not registered) and disposal.
- `musicbot/audiocontroller.py` - per-guild playback state machine (voice, volume, loop, inactivity timer, playlist backup).
- `musicbot/playlist.py` / `musicbot/song.py` - queue/history and per-track metadata.
- `musicbot/playlists.py` - saved playlists: the only reader/writer of `SavedPlaylist.songs_json`; callers use `PlaylistEntry`/`PlaylistRef`.
- `musicbot/loader.py` - yt-dlp extraction in a worker process; calling it before `init()` raises `LoaderNotRunning`.
- `musicbot/linkutils.py` - URL classification and Spotify resolution.
- `musicbot/library_browse.py` - the library browse cursor; `LibraryBrowseView` is its Discord adapter.
- `musicbot/settings.py` - SQLAlchemy models, auto-migrations, `d!settings` converters, guild whitelist.
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

GitHub Issues on `maj0rpain/DandelionMusic`, via `gh` with `--repo maj0rpain/DandelionMusic` pinned explicitly on every call (bare `gh` resolves to the original `solaluset` repo). See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, unchanged (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root (`GLOSSARY.md` is created lazily by `/domain-modeling`). See `docs/agents/domain.md`.
