"""Importing the loader and the audio controller is inert.

Each check runs in a fresh interpreter, so modules a previous test
imported cannot hide what a bare import pulls in.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PROBE = """
import json
import sys

import musicbot
import musicbot.loader
import musicbot.audiocontroller

print(json.dumps({"bot_loaded": "musicbot.bot" in sys.modules}))
"""


def _import_in_fresh_interpreter(cwd: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")])
    )
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_loader_and_audiocontroller_does_not_load_the_bot(
    tmp_path,
):
    observed = _import_in_fresh_interpreter(tmp_path)

    assert observed["bot_loaded"] is False
