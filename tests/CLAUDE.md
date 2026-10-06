# tests/CLAUDE.md

Run with `uv run --group dev pytest`. CI runs them as the `run-tests` job in `.github/workflows/checks.yml`.

## What the tests cover

Only the parts that need no Discord connection: config loading, `Playlist`, library search/stats, the browse cursor, the tag/expiry/URL parsers, the permission checks, the guild whitelist, the guild session registry, and `AudioController` built through the seam below. There is no integration coverage of the bot itself.

## The controller seam

`tests/controller_seam.py` builds an `AudioController` over a stub `musicbot.loader`, a fake bot/guild/voice client and a `tmp_path` cwd. Use it through the `controller` and `stub_loader` fixtures rather than building a controller by hand.

## Subprocess probes

`tests/test_import_inert.py` runs fresh-interpreter subprocesses to check that importing `musicbot` changes no process-global state, that the entrypoint wraps stdout/stderr before `bot.run()`, and the loader's `init()`/`shutdown()` lifecycle - which starts a real extraction worker.

## The Config/dotenv trap

`Config.load()` calls `load_dotenv()`, and python-dotenv's `find_dotenv()` walks up from the calling module's file, not from cwd. A test that touches `Config` must redirect both `find_dotenv` and `load_dotenv` (see `tests/conftest.py`), or it reads the developer's real `.env`.
