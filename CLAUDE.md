# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

DandelionMusic (package name `doybot-music`) is a Discord music bot written in Python (discord.py). It plays audio from YouTube, SoundCloud, Spotify, Bandcamp, Twitter, and custom files/links, using `yt-dlp` for extraction and `ffmpeg`/`FFmpegPCMAudio` for playback. Requires Python >= 3.13 (per `pyproject.toml`; CI still uses 3.11 images for the pre-commit/exe-build workflows, which don't install the runtime deps).

## Commands

Dependencies are managed exclusively with [`uv`](https://docs.astral.sh/uv/) (`uv.lock` is the single source of truth — there is no `requirements.txt`).

```bash
# install deps
uv sync

# run the bot (foreground, real entrypoint)
uv run python -m musicbot
# or via the background-launcher wrapper (spawns a detached subprocess, `d!shutdown`/Ctrl+C to stop)
uv run python run.py
```

Configuration is via a `.env` file (see `.env.sample`) — at minimum `BOT_TOKEN` must be set. `SPOTIFY_ID`/`SPOTIFY_SECRET` are optional (falls back to scraping the Spotify web page when absent). `COOKIE_PATH` (default `config/cookies/cookies.txt`) supplies cookies for restricted content.

There is no test suite in this repo (no `tests/` directory, no test runner configured).

### Linting / formatting

Enforced via pre-commit (`.pre-commit-config.yaml`), also run in CI (`.github/workflows/checks.yml`):

```bash
pip install pre-commit
pre-commit run --all
```

Hooks: `black -l 79` (79-char line length, not the black default), `flake8 --ignore E203,W503`, plus repo-local hooks that validate `config/*.json` as JSONC and regenerate the `ENV ...` block in `Dockerfile` from `config/config.py` whenever it changes.

### Docker

```bash
docker compose up --build
```

`docker-compose.yaml` also starts a `bgutil-provider` sidecar (`brainicism/bgutil-ytdlp-pot-provider`) that yt-dlp's YouTube extractor calls over HTTP (`http://bgutil-provider:4416`) to generate PO tokens — required for reliable YouTube playback.

The `Dockerfile` installs the `uv` binary via a multi-stage `COPY --from=ghcr.io/astral-sh/uv:latest` and runs `uv sync --frozen` against the committed `uv.lock`, so bumping a dependency means updating `pyproject.toml`/`uv.lock` (`uv lock`) — there's no separate `requirements.txt` to keep in sync.

### Building a Windows exe

```bash
uv sync --frozen --group build
uv run python -m config.build
```
Produces `dist/DandelionMusic.exe` via PyInstaller (see `config/build.py` for the bundled data/hidden-imports list). Triggered in CI on tag pushes (`.github/workflows/release.yml`).

## Architecture

### Entry points and process model

- `musicbot/__main__.py` builds the `discord.py` intents/prefix from `config`, constructs `musicbot.bot.MusicBot`, registers command extensions (`musicbot.commands.{music,general,developer}`, plus `musicbot.plugins.button` if `ENABLE_BUTTON_PLUGIN`), and calls `bot.run()`.
- `run.py` is an optional wrapper: it re-execs itself as a detached background subprocess (`--run` flag) and forwards stdout, letting users close the launching terminal; Ctrl+C sends a `shutdown` line over stdin which `config.SHUTDOWN_MESSAGE`-prints and exits the child. This matters mainly for the PyInstaller-built Windows exe.
- Track/metadata extraction (`musicbot/loader.py`) runs `yt_dlp.YoutubeDL` in a **separate spawned process** (`ProcessPoolExecutor(1)`) so that CPU-bound extraction never blocks the asyncio event loop; `musicbot.loader.extract_info`/`_load_song` etc. execute there, `preload`/`load_song`/`search_youtube` are the async wrappers called from the event loop via `_run_sync`.

### Config system (`config/config.py`)

`config.Config` is a single class whose **class attributes are the schema and defaults** for every setting. At instantiation (`config = Config()` is imported everywhere as `from config import config`) it:
1. Loads `.env` via `python-dotenv`, overriding class defaults with any matching environment variables (`get_env_var`).
2. Warns about `.env` variables that don't match any known `Config` attribute (`warn_unknown_vars`, called from `__main__`).
3. Resolves `DATABASE_URL` into a SQLAlchemy async URL (`alchemize_url`) and normalizes the driver name (`DATABASE_LIBRARY_NAME`) for the PyInstaller build's hidden-import list. `aiosqlite` (sqlite, the default) is a normal dependency; postgres/mysql users need the matching driver via the `postgres`/`mysql` extras (`uv sync --extra postgres` / `--extra mysql`).
4. Loads localized message strings from `config/en.json` (or other language files found under `CONFIG_DIRS`) via `load_configs`/`join_dicts`, exposed through `config.__getattr__` (e.g. `config.SONGINFO_ERROR`) and `config.get_dict(name)`.
5. `Config.save()`/`_update_env_files()` can write changed settings back into `.env`/`.env.sample` (called from `musicbot/commands/developer.py`'s owner-only settings commands). `config/build.py` separately dumps per-setting doc comments to `config_comments.json` for the frozen exe.

Adding a new setting means adding a class attribute to `Config` (with a comment directly above it — comments are parsed by `get_comments()` for docs/exe use) — that alone makes it configurable via `.env`.

### Runtime object graph

- `MusicBot` (`musicbot/bot.py`, subclass of `commands.Bot`) owns two guild-keyed dicts: `audio_controllers: Dict[Guild, AudioController]` and `settings: Dict[Guild, GuildSettings]`. It creates the async SQLAlchemy engine/session factory from `config.DATABASE`, runs Alembic autogeneration-based migrations on startup (`musicbot/settings.py:run_migrations` — schema is derived automatically from the ORM models, no migration files to write by hand), and migrates legacy JSON settings/playlists into the DB.
- A custom `Context` (also in `bot.py`) overrides `send()` to attach/refresh the persistent playback `View` (buttons) on the bot's own message and to route ephemeral/interaction responses correctly; almost all user-facing replies should go through `ctx.send(...)`, not raw channel sends.
- `AudioController` (`musicbot/audiocontroller.py`) is the per-guild playback state machine: owns a `Playlist`, the voice connection lifecycle (`uconnect`/`udisconnect`/`register_voice_channel`), volume, looping, an inactivity `Timer` (`musicbot/utils.py`) that auto-disconnects, and periodically pickles the playlist to `backup/playlist_<guild_id>.pickle` for crash recovery. It also builds the Discord UI `View` (`MusicButton` instances) shown under the "now playing" message.
- `Playlist`/`Song` (`musicbot/playlist.py`, `musicbot/song.py`) hold queue/history and per-track metadata (title, url, playlist membership, expiry-based re-fetch via `_parse_expire` in `loader.py` for expiring stream URLs).
- `musicbot/linkutils.py` classifies an input string into a `SiteTypes` enum or a `yt_dlp` extractor instance (`identify_url`/`get_site_type`) and implements Spotify resolution (via the official API when `SPOTIFY_ID`/`SPOTIFY_SECRET` are set, otherwise by scraping the Spotify webpage with BeautifulSoup) by turning Spotify tracks into a YouTube search.
- `musicbot/commands/` holds the three `commands.Cog`/extension modules loaded by `__main__.py`: `music.py` (playback commands, largest module), `general.py` (settings/utility), `developer.py` (owner-only). `musicbot/plugins/button.py` is the optional reaction-button "click to play" plugin gated by `ENABLE_BUTTON_PLUGIN`.
- `musicbot/settings.py` defines the SQLAlchemy models (`GuildSettings`, `SavedPlaylist`) and per-setting value converters/validators (`CONFIG_CONVERTERS`) used by the `d!settings` command.

### Discord command surface

Commands are `hybrid_command`s (usable both as `d!`-prefixed text commands and slash commands, gated by `ENABLE_SLASH_COMMANDS`). Prefix, mention-as-prefix, slash command sync, and per-guild whitelisting are all config-driven in `musicbot/__main__.py`/`bot.py`. See `README.md` for the end-user command reference (`d!p`, `d!skip`, `d!q`, `d!loop`, `d!settings`, etc.).
