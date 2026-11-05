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

from config.config import ENV_ALIASES, Config

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
    "pools_built": bool(loader._pools),
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


def worker_stdio_is_wrapped():
    import sys

    from musicbot import utils

    return {
        "stdout": isinstance(sys.stdout, utils.OutputWrapper),
        "stderr": isinstance(sys.stderr, utils.OutputWrapper),
    }


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
    stdio_after_init = worker_stdio_is_wrapped()
    worker_ready = all(
        loader._pools[lane].submit(worker_state_is_set).result()
        for lane in loader.Lane
    )
    worker_stdios = [
        loader._pools[lane].submit(worker_stdio_is_wrapped).result()
        for lane in loader.Lane
    ]
    worker_stdio = (
        worker_stdios[0]
        if all(w == worker_stdios[0] for w in worker_stdios)
        else worker_stdios
    )
    children_after_init = len(multiprocessing.active_children())
    loader.init()
    children_after_second_init = len(multiprocessing.active_children())
    run_sync_reaches_worker = asyncio.run(
        loader._run_sync(loader.Lane.PRELOAD, worker_state_is_set)
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
        "stdio_after_init": stdio_after_init,
        "worker_stdio": worker_stdio,
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


# Run first in every probe, before anything imports config: config.config
# does `from dotenv import find_dotenv`, so the stub must already be on
# dotenv when it does. At module level, so a spawn worker re-importing
# the probe file runs it too.
NO_DOTENV_PRELUDE = """\
import os
import dotenv

dotenv.find_dotenv = lambda *a, **kw: os.devnull
"""


def _run_in_fresh_interpreter(cwd: Path, script: str, as_file=False):
    # without the settings exported in the shell that runs pytest, and
    # with find_dotenv stubbed to os.devnull by NO_DOTENV_PRELUDE - a
    # probe run as a file, and each worker it spawns, would otherwise
    # find the developer's real .env by walking up from config/config.py
    leaked = {*Config.as_dict(), *ENV_ALIASES}
    env = {k: v for k, v in os.environ.items() if k not in leaked}
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")])
    )
    script = NO_DOTENV_PRELUDE + textwrap.dedent(script)
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
    assert _observed(import_result)["pools_built"] is False


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


def test_loader_lifecycle_init_leaves_stdout_and_stderr_unwrapped(lifecycle):
    assert lifecycle["stdio_after_init"] == {"stdout": False, "stderr": False}


def test_loader_lifecycle_worker_wraps_its_own_stdout_and_stderr(lifecycle):
    assert lifecycle["worker_stdio"] == {"stdout": True, "stderr": True}


def test_loader_lifecycle_second_init_spawns_no_second_worker(lifecycle):
    assert (
        lifecycle["children_after_init"],
        lifecycle["children_after_second_init"],
    ) == (2, 2)


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


ENV_PATH_PROBE = """
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from config import config


def worker_env_path():
    from config import config

    return config._env_path


if __name__ == "__main__":
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(1, mp_context=ctx) as pool:
        worker = pool.submit(worker_env_path).result()
    print(json.dumps({"main": config._env_path, "worker": worker}))
"""

ENV_PATH_C_PROBE = """
import json

from config import config

print(json.dumps({"main": config._env_path}))
"""


@pytest.mark.parametrize("as_file", [True, False], ids=["file", "-c"])
def test_probe_and_its_workers_resolve_no_dotenv(tmp_path, as_file):
    script = ENV_PATH_PROBE if as_file else ENV_PATH_C_PROBE
    observed = _observed(
        _run_in_fresh_interpreter(tmp_path, script, as_file=as_file)
    )
    assert observed["main"] == os.devnull
    if as_file:
        assert observed["worker"] == os.devnull


def test_linkutils_does_not_import_the_loader():
    import ast
    from pathlib import Path

    from musicbot import linkutils

    names = set()
    for node in ast.walk(ast.parse(Path(linkutils.__file__).read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    assert "musicbot.loader" not in names
