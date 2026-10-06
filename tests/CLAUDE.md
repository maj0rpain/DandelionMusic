# tests/CLAUDE.md

Run with `uv run --group dev pytest`. CI runs them as the `run-tests` job in `.github/workflows/checks.yml`.

## What the tests cover

Only the parts that need no Discord connection: config loading, `Playlist`, saved playlists (`tests/test_playlists.py`, against an in-memory database), `Song` and how it refers to a saved playlist (`tests/test_song.py`), library search/stats, the browse cursor, the tag/expiry/URL parsers, `loader.preload` with `load_song` monkeypatched (`tests/test_loader_preload.py`), the permission and voice checks (voice joined only after them, through fake voice channels), which commands are joining commands (`tests/test_checks.py`: exactly the marked ones; an unmarked command or a player button never connects or moves and never refuses on voice it does not need, and a player button is refused with `NOT_CONNECTED_MESSAGE` while the bot is out of voice; a search pick connects), the guild whitelist, the guild settings descriptor table and the `d!setting show` embed it drives (`tests/test_settings_table.py`, on a fake guild), the guild session registry, the `d!search` results view (`tests/test_search_view.py`), a `MusicButton` click deferring before its checks, with only a joining button (one overriding the `join` hook) joining voice after them (`tests/test_music_button.py`), `AudioController` built through the seam below, and a few command callbacks (`d!reset`, `d!restore`'s replies, the `d!setting <name>` subcommands, generated from that table: their confirmations, error replies, `vc_timeout` edit gate, slash parameters and prefix argument conversion, in `tests/test_settings_update_failure.py`) driven on a fake context. They also cover `MusicBot.close()` joining the loader off the event loop. There is no integration coverage of the bot itself.

## The controller seam

`tests/controller_seam.py` builds an `AudioController` over a stub `musicbot.loader`, a fake bot/guild/voice client and a `tmp_path` cwd. Use it through the `controller` and `stub_loader` fixtures rather than building a controller by hand.

## Subprocess probes

`tests/test_import_inert.py` runs fresh-interpreter subprocesses to check that importing `musicbot` changes no process-global state, that the entrypoint wraps stdout/stderr before `bot.run()`, that `MusicBot` can be built with no current event loop, and the loader's `init()`/`shutdown()` lifecycle - which starts a real extraction worker.

## The Config/dotenv trap

`Config.load()` calls `load_dotenv()`, and python-dotenv's `find_dotenv()` walks up from the calling module's file, not from cwd. A test that touches `Config` must redirect both `find_dotenv` and `load_dotenv` (see `tests/conftest.py`), or it reads the developer's real `.env`.
