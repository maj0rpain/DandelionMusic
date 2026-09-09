# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

DandelionMusic (package name `doybot-music`) is a Discord music bot written in Python (discord.py). It plays audio from YouTube, SoundCloud, Spotify, Bandcamp, Twitter, and custom files/links, using `yt-dlp` for extraction and `ffmpeg`/`FFmpegPCMAudio` for playback. Requires Python >= 3.13 (per `pyproject.toml`; CI still uses 3.11 images for the pre-commit/exe-build workflows, which don't install the runtime deps).

## Commands

Dependencies are managed exclusively with [`uv`](https://docs.astral.sh/uv/) (`uv.lock` is the single source of truth — there is no `requirements.txt`).

```bash
# install deps
uv sync

# run the bot (real entrypoint)
uv run python -m musicbot
# or via the thin wrapper README points users at (equivalent; it just forwards)
uv run python run.py
```

Configuration is via a `.env` file (see `.env.sample`) — at minimum `BOT_TOKEN` must be set. `SPOTIFY_ID`/`SPOTIFY_SECRET` are optional (falls back to scraping the Spotify web page when absent). `COOKIE_PATH` (default `config/cookies/cookies.txt`) supplies cookies for restricted content.

Tests live in `tests/` and run with pytest:

```bash
uv run --group dev pytest
```

They cover only the parts that need no Discord connection (config loading and the `.env` writer, `Playlist`, library search/stats, the browse cursor, the tag/expiry/URL parsers, the permission checks); there is no integration coverage of the bot itself. CI runs them as the `run-tests` job in `.github/workflows/checks.yml`.

Note for config tests: `Config.load()` calls `load_dotenv()`, and python-dotenv's `find_dotenv()` walks up from the *calling module's file* rather than from cwd, while `_update_env_files()` opens the relative path `".env"`. A test that touches `Config` must redirect both (see `tests/conftest.py`) or it will read — and `save()` will rewrite — the real `.env`.

### Git remotes / PRs

This repo has two remotes: `origin` (`maj0rpain/DandelionMusic`, the user's fork — branches get pushed here) and `upstream` (`solaluset/DandelionMusic`, the original project, which `gh`'s default repo resolution points at). Always create pull requests against `maj0rpain/DandelionMusic` — pass `--repo maj0rpain/DandelionMusic` explicitly to `gh pr create` rather than relying on `gh`'s default, which resolves to `upstream` and would open the PR against the wrong repo.

### Linting / formatting

Enforced via pre-commit (`.pre-commit-config.yaml`), also run in CI (`.github/workflows/checks.yml`):

```bash
pip install pre-commit
pre-commit run --all
```

Hooks: `black -l 79` (79-char line length, not the black default), `flake8 --ignore E203,W503`, plus a repo-local hook that validates `config/*.json` as JSONC.

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
- `run.py` is a thin wrapper that just `runpy.run_module("musicbot")`. It once re-exec'd itself as a detached background subprocess (a `--run` flag, stdout forwarding, Ctrl+C over stdin), but that was removed in 67fd2b2 for breaking auto-restart. It is kept because it is the PyInstaller entry point (`config/build.py`) and what README tells users to start.
- Track/metadata extraction (`musicbot/loader.py`) runs `yt_dlp.YoutubeDL` in a **separate spawned process** (`ProcessPoolExecutor(1)`) so that CPU-bound extraction never blocks the asyncio event loop; `musicbot.loader.extract_info`/`_load_song` etc. execute there, `preload`/`load_song`/`search_youtube` are the async wrappers called from the event loop via `_run_sync`.

### Config system (`config/config.py`)

`config.Config` is a single class whose **class attributes are the schema and defaults** for every setting. At instantiation (`config = Config()` is imported everywhere as `from config import config`) it:
1. Loads `.env` via `python-dotenv`, overriding class defaults with any matching environment variables (`get_env_var`).
2. Warns about `.env` variables that don't match any known `Config` attribute (`warn_unknown_vars`, called from `__main__`).
3. Resolves `DATABASE_URL` into a SQLAlchemy async URL (`alchemize_url`) and normalizes the driver name (`DATABASE_LIBRARY_NAME`) for the PyInstaller build's hidden-import list. `aiosqlite` (sqlite, the default) is a normal dependency; postgres/mysql users need the matching driver via the `postgres`/`mysql` extras (`uv sync --extra postgres` / `--extra mysql`).
4. Loads localized message strings from `config/en.json` (or other language files found under `CONFIG_DIRS`) via `load_configs`/`join_dicts`, exposed through `config.__getattr__` (e.g. `config.SONGINFO_ERROR`) and `config.get_dict(name)`.
5. **`Config` is read-only.** It has no `save()`: the bot never writes `.env`. It used to, for `d!guild_whitelist`, and that one feature produced five separate bugs (a leaked `BOT_TOKEN` into the committed `.env.sample`, an `EMBED_COLOR` that changed on every save, a tuple setting that made the next startup fail, writes landing in the wrong directory, and a removal that silently did not persist), so the whitelist moved into the database and the whole write-back path was deleted. Don't reintroduce it — runtime state belongs in the DB (`musicbot/settings.py`), not in the config file. `config/build.py` separately dumps per-setting doc comments to `config_comments.json` for the frozen exe.

Adding a new setting means adding a class attribute to `Config` (with a comment directly above it — comments are parsed by `get_comments()` for docs/exe use) — that alone makes it configurable via `.env`.

### Runtime object graph

- `MusicBot` (`musicbot/bot.py`, subclass of `commands.Bot`) owns two guild-keyed dicts: `audio_controllers: Dict[Guild, AudioController]` and `settings: Dict[Guild, GuildSettings]`. It creates the async SQLAlchemy engine/session factory from `config.DATABASE`, runs Alembic autogeneration-based migrations on startup (`musicbot/settings.py:run_migrations` — schema is derived automatically from the ORM models, no migration files to write by hand), and migrates legacy JSON settings/playlists into the DB. `run_migrations` only auto-applies additive ops (`CreateTableOp`/`AddColumnOp`, allow-listed in `SAFE_MIGRATION_OPS`); anything else in the generated diff (a dropped/renamed/altered column or table) raises instead of executing, since that would otherwise silently drop data on every self-hosted deployment's next startup.
- `_help`/`_help_autocomplete` (`bot.py`) are deliberately **module-level functions, not `MusicBot` methods**, even though they're registered on the bot via `add_command()` rather than a `Cog`. discord.py decides how many leading parameters a command callback needs (self/cog + ctx, or just ctx) purely by lexical nesting (`discord.utils.is_inside_class()`, based on `__qualname__`) — independent of whether `.cog` is actually set at runtime. A command added via `add_command()` outside a `Cog` never gets `.cog` set, so if `_help` were defined inside `MusicBot`, discord.py would expect a leading `self` argument that invocation never supplies, crashing every call. They use `ctx.bot`/`interaction.client` to reach the bot instance instead of `self`. Every other command in the codebase lives inside a real `Cog` registered via `add_cog()` (through the `initial_extensions`/`load_extension` path), where this concern doesn't apply.
- A custom `Context` (also in `bot.py`) overrides `send()` to attach/refresh the persistent playback `View` (buttons) on the bot's own message and to route ephemeral/interaction responses correctly; almost all user-facing replies should go through `ctx.send(...)`, not raw channel sends.
- `AudioController` (`musicbot/audiocontroller.py`) is the per-guild playback state machine: owns a `Playlist`, the voice connection lifecycle (`uconnect`/`udisconnect`/`register_voice_channel`), volume, looping, an inactivity `Timer` (`musicbot/utils.py`) that auto-disconnects, and periodically pickles the playlist to `backup/playlist_<guild_id>.pickle` for crash recovery (restored only via the explicit `d!restore` command — nothing restores it automatically on reconnect). It also builds the Discord UI `View` (`MusicButton` instances) shown under the "now playing" message. `next_song()` (its `after=` callback passed to `voice_client.play()`) is invoked by discord.py from its own audio-player **thread**, not the event loop thread; `add_task()` detects this (`asyncio.get_running_loop()` raising) and falls back to `asyncio.run_coroutine_threadsafe` instead of the non-thread-safe `loop.create_task()` — keep this in mind before adding new scheduling calls reachable from that callback.
- `Playlist`/`Song` (`musicbot/playlist.py`, `musicbot/song.py`) hold queue/history and per-track metadata (title, url, playlist membership, expiry-based re-fetch via `_parse_expire` in `loader.py` for expiring stream URLs).
- `musicbot/linkutils.py` classifies an input string into a `SiteTypes` enum or a `yt_dlp` extractor instance (`identify_url`/`get_site_type`) and implements Spotify resolution (via the official API when `SPOTIFY_ID`/`SPOTIFY_SECRET` are set, otherwise by scraping the Spotify webpage with BeautifulSoup) by turning Spotify tracks into a YouTube search.
- `musicbot/library_browse.py` holds the browse cursor (`BrowseCursor`, `Screen`, `Descent`): where a `d!lib browse` session is in the library and every rule for moving it — descending, paging with its clamp, what the current scope stands for, and the `level_revision` counter that lets a slow enrichment tell it is describing a level nobody is looking at any more. It imports `musicbot.library` and `typing`, and nothing else: these rules used to live on `LibraryBrowseView` (`musicbot/commands/library.py`), whose every entry point took a `discord.Interaction`, so the only way to exercise them was a human clicking buttons in Discord — and the same class of bug kept coming back. `LibraryBrowseView` is now an adapter over it, owning the Discord side (components, message edits, deferral, the `_busy` guard, the enrichment and `KIND_EMOJI`) and keeping no copy of the cursor's state. Keep the import list as it is; a `discord` or `config` import there costs the tests (`tests/test_library_browse.py`, which asserts it) their reason to exist.
- `musicbot/commands/` holds the three `commands.Cog`/extension modules loaded by `__main__.py`: `music.py` (playback commands, largest module), `general.py` (settings/utility), `developer.py` (owner-only). `musicbot/plugins/button.py` is the optional reaction-button "click to play" plugin gated by `ENABLE_BUTTON_PLUGIN`.
- `musicbot/settings.py` defines the SQLAlchemy models (`GuildSettings`, `SavedPlaylist`, `WhitelistedGuild` — the guild whitelist, with `get/add_to/remove_from_guild_whitelist` helpers — and `BotState`, a small key/value table currently holding only the marker that records the one-time `GUILD_WHITELIST` env import) and per-setting value converters/validators (`CONFIG_CONVERTERS`) used by the `d!settings` command.

### Discord command surface

Commands are `hybrid_command`s (usable both as `d!`-prefixed text commands and slash commands, gated by `ENABLE_SLASH_COMMANDS`). Prefix, mention-as-prefix and slash command sync are config-driven in `musicbot/__main__.py`/`bot.py`; the guild whitelist is not — it lives in the database (see `musicbot/settings.py`). See `README.md` for the end-user command reference (`d!p`, `d!skip`, `d!q`, `d!loop`, `d!settings`, etc.).

## Workflow

### Planning, implementation and review are separate sessions

A session that produced a plan does not implement it. A session that implemented something does not review it. Each of those boundaries is crossed by writing a handoff document and starting a fresh session against it — not by carrying on in the same context.

**Every handoff goes in `handoffs/`**, named `handoff-<what-the-next-session-does>.md` (e.g. `handoffs/handoff-implement-browse-cursor.md`). This deliberately overrides the `handoff` skill's own instruction to save to the OS temp directory: a file under `/tmp/claude-*/…/scratchpad/` is effectively unfindable from a later session, which is the failure this rule exists to prevent. Everything else that skill says still applies — reference plans, specs, ADRs, issues and commits by path or URL instead of duplicating them, include a "suggested skills" section naming what the next session should invoke, and redact secrets.

`handoffs/` is gitignored (the directory is tracked via `.gitkeep`, its contents are not), so a handoff is working state and never appears in a commit or a PR.

`/handoff` is user-invocable only (`disable-model-invocation: true` in its frontmatter), so an agent cannot call it. At a boundary the agent writes the document into `handoffs/` itself, in that format, and stops.

### The review session runs the branch to green

Review is a loop, not a pass: the fixes written in response to a review are themselves unreviewed code. The session repeats until a round changes nothing.

1. `/code-review` against the base branch.
2. Act on the findings — including deciding a finding warrants no change, with that reasoning in the commit message.
3. Push, then verify CI **on the commit just pushed**: `gh pr checks <n> --repo maj0rpain/DandelionMusic --watch`. Pushing is not a result. A green local `pytest` and `pre-commit run --all` predict CI rather than standing in for it — `checks.yml` runs the hooks under Python 3.11 in `run-checks`, against the 3.13 that `run-tests` and local development use. A red run is a finding: re-enter at step 2.
4. Repeat from step 1. The commits step 2 added are the ones not yet reviewed; the rest of the branch has been.

**Done is a head commit that has been both reviewed clean and seen green**: a review round that produced no code change, over a CI run that concluded passing on that same commit.

## Agent skills

### Issue tracker

GitHub Issues on `maj0rpain/DandelionMusic`, via `gh` with `--repo maj0rpain/DandelionMusic` pinned explicitly on every call (bare `gh` resolves to the original `solaluset` repo). See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, unchanged (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root (neither exists yet; created lazily by `/domain-modeling`). See `docs/agents/domain.md`.
