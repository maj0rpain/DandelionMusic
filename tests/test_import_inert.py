"""Importing musicbot changes no process-global state; the loader's
process-wide setup happens only through its explicit init()/shutdown().

Each check runs in a fresh interpreter, so modules a previous test
imported - and state a previous test set up - cannot hide what a bare
import does.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from config.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent

IMPORT_PROBE = """
import atexit
import json
import multiprocessing
import multiprocessing.context
import sys

# logging, multiprocessing.util and certifi (through yt_dlp) each
# register an exit handler of their own when imported; take them first
# so what is left to count is what musicbot registers
import logging
import multiprocessing.util
import yt_dlp

stdout, stderr = sys.stdout, sys.stderr
baseline_callbacks = atexit._ncallbacks()

import musicbot
import musicbot.loader
import musicbot.audiocontroller
from musicbot import linkutils, loader

print(json.dumps({
    "stdout_untouched": sys.stdout is stdout,
    "stderr_untouched": sys.stderr is stderr,
    "atexit_callbacks": atexit._ncallbacks() - baseline_callbacks,
    "executor_built": loader._executor is not None,
    "sessions": len(linkutils._sessions),
    "children": len(multiprocessing.active_children()),
    "spawn_process_is_stdlib": (
        multiprocessing.get_context("spawn").Process
        is multiprocessing.context.SpawnProcess
    ),
    "bot_loaded": "musicbot.bot" in sys.modules,
}))
"""

LIFECYCLE_PROBE = """
import asyncio
import json
import multiprocessing

from musicbot import loader


def worker_state_is_set():
    from musicbot import loader

    return loader._loop is not None and loader._downloader is not None


async def _observe_call(call):
    try:
        await call()
    except Exception as e:
        raised = type(e).__name__
        is_runtime_error = isinstance(e, RuntimeError)
    else:
        raised, is_runtime_error = None, False
    return {
        "raised": raised,
        "is_runtime_error": is_runtime_error,
        "default_executor_unused": (
            asyncio.get_running_loop()._default_executor is None
        ),
    }


def calls_without_worker():
    return {
        "load_song": asyncio.run(
            _observe_call(
                lambda: loader.load_song("https://example.com/a.mp3")
            )
        ),
        "search_youtube": asyncio.run(
            _observe_call(lambda: loader.search_youtube("a song"))
        ),
    }


if __name__ == "__main__":
    before_init = calls_without_worker()
    loader.shutdown()  # safe without a prior init()
    loader.init()
    worker_ready = loader._executor.submit(worker_state_is_set).result()
    children_after_init = len(multiprocessing.active_children())
    loader.init()
    children_after_second_init = len(multiprocessing.active_children())
    run_sync_reaches_worker = asyncio.run(
        loader._run_sync(worker_state_is_set)
    )
    loader.shutdown()
    children_after_shutdown = len(multiprocessing.active_children())
    after_shutdown = calls_without_worker()
    loader.shutdown()  # and a second time
    print(json.dumps({
        "before_init": before_init,
        "after_shutdown": after_shutdown,
        "run_sync_reaches_worker": run_sync_reaches_worker,
        "worker_ready": worker_ready,
        "children_after_init": children_after_init,
        "children_after_second_init": children_after_second_init,
        "children_after_shutdown": children_after_shutdown,
    }))
"""


ENTRYPOINT_PROBE = """
import json
import runpy
import sys

from musicbot import utils
from musicbot.bot import MusicBot

seen = {}


def run_without_connecting(self, *args, **kwargs):
    # by now discord.py would build its logging handler on sys.stderr
    seen["stdout_wrapped"] = isinstance(sys.stdout, utils.OutputWrapper)
    seen["stderr_wrapped"] = isinstance(sys.stderr, utils.OutputWrapper)


# the network and the ffmpeg binary are the boundaries stubbed here
MusicBot.run = run_without_connecting
utils.check_dependencies = lambda: None
runpy.run_module("musicbot", run_name="__main__")
print(json.dumps(seen))
"""


CONSTRUCTION_PROBE = """
import json
import warnings

import discord

from musicbot.bot import MusicBot

# on 3.13 a loop-needing call with no current loop only warns; 3.14
# raises, so a warning here is the 3.14 crash
with warnings.catch_warnings():
    # only the no-loop warning: 3.14 deprecates other asyncio calls
    # discord.py makes while the bot is built
    warnings.filterwarnings(
        "error", "There is no current event loop", DeprecationWarning
    )
    try:
        MusicBot(
            [], command_prefix="d!", intents=discord.Intents.default()
        )
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
    else:
        raised = None
print(json.dumps({"raised": raised}))
"""


def _run_in_fresh_interpreter(cwd: Path, script: str, as_file=False):
    # without the settings an earlier test's load_dotenv() may have left
    # in os.environ - a half-set Spotify credential, say, makes the
    # import print a traceback
    leaked = {*Config.as_dict(), "SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET"}
    env = {k: v for k, v in os.environ.items() if k not in leaked}
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")])
    )
    script = textwrap.dedent(script)
    if as_file:
        # spawn workers re-import the parent's main module, which -c
        # does not leave them to find
        probe = cwd / "probe.py"
        probe.write_text(script)
        command = [sys.executable, str(probe)]
    else:
        command = [sys.executable, "-c", script]
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _observed(result) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def import_result(tmp_path_factory):
    return _run_in_fresh_interpreter(
        tmp_path_factory.mktemp("import"), IMPORT_PROBE
    )


@pytest.fixture(scope="module")
def lifecycle(tmp_path_factory):
    return _observed(
        _run_in_fresh_interpreter(
            tmp_path_factory.mktemp("lifecycle"),
            LIFECYCLE_PROBE,
            as_file=True,
        )
    )


def test_importing_musicbot_exits_cleanly_with_empty_stderr(import_result):
    assert (import_result.returncode, import_result.stderr) == (0, "")


def test_importing_musicbot_does_not_load_the_bot(import_result):
    assert _observed(import_result)["bot_loaded"] is False


def test_importing_musicbot_leaves_stdout_and_stderr_alone(import_result):
    observed = _observed(import_result)
    assert (observed["stdout_untouched"], observed["stderr_untouched"]) == (
        True,
        True,
    )


def test_importing_musicbot_registers_no_exit_handlers(import_result):
    assert _observed(import_result)["atexit_callbacks"] == 0


def test_importing_musicbot_builds_no_process_pool(import_result):
    assert _observed(import_result)["executor_built"] is False


def test_importing_musicbot_opens_no_http_session(import_result):
    assert _observed(import_result)["sessions"] == 0


def test_importing_musicbot_spawns_no_process(import_result):
    assert _observed(import_result)["children"] == 0


def test_importing_musicbot_leaves_the_spawn_process_class_alone(
    import_result,
):
    assert _observed(import_result)["spawn_process_is_stdlib"] is True


def test_entrypoint_wraps_stdout_and_stderr_before_running_the_bot(
    tmp_path,
):
    observed = _observed(_run_in_fresh_interpreter(tmp_path, ENTRYPOINT_PROBE))
    assert (observed["stdout_wrapped"], observed["stderr_wrapped"]) == (
        True,
        True,
    )


def test_music_bot_can_be_built_with_no_current_event_loop(tmp_path):
    observed = _observed(
        _run_in_fresh_interpreter(tmp_path, CONSTRUCTION_PROBE)
    )
    assert observed["raised"] is None


def test_loader_lifecycle_init_readies_the_worker(lifecycle):
    assert lifecycle["worker_ready"] is True


def test_loader_lifecycle_second_init_spawns_no_second_worker(lifecycle):
    assert (
        lifecycle["children_after_init"],
        lifecycle["children_after_second_init"],
    ) == (1, 1)


def test_loader_lifecycle_shutdown_leaves_no_children(lifecycle):
    assert lifecycle["children_after_shutdown"] == 0


@pytest.mark.parametrize("when", ["before_init", "after_shutdown"])
@pytest.mark.parametrize("call", ["load_song", "search_youtube"])
def test_loader_lifecycle_call_outside_init_raises_loader_not_running(
    lifecycle, when, call
):
    observed = lifecycle[when][call]
    assert (observed["raised"], observed["is_runtime_error"]) == (
        "LoaderNotRunning",
        True,
    )


@pytest.mark.parametrize("when", ["before_init", "after_shutdown"])
@pytest.mark.parametrize("call", ["load_song", "search_youtube"])
def test_loader_lifecycle_call_outside_init_uses_no_thread_pool(
    lifecycle, when, call
):
    assert lifecycle[when][call]["default_executor_unused"] is True


def test_loader_lifecycle_run_sync_reaches_the_worker_after_init(lifecycle):
    assert lifecycle["run_sync_reaches_worker"] is True
