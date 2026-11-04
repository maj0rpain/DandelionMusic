"""Owner commands: the run-a-command-and-report-its-output helper.

Drives the module-level helper on a fake context that records each
send, running real child processes via the test interpreter.
"""

import asyncio
import shlex
import sys

from musicbot.commands import developer
from musicbot.commands.developer import (
    OUTPUT_FORMAT,
    Developer,
    run_and_report,
)

PREFIX, SUFFIX = OUTPUT_FORMAT.split("{}")


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


def py(code):
    return [sys.executable, "-c", code]


def report(argv):
    ctx = FakeContext()
    asyncio.run(run_and_report(ctx, argv))
    return ctx.sent


def unwrap(page):
    assert page.startswith(PREFIX) and page.endswith(SUFFIX)
    return page[len(PREFIX) : -len(SUFFIX)]


def test_stderr_only_output_reaches_the_context():
    sent = report(py("import sys; sys.stderr.write('oops')"))
    assert [unwrap(p) for p in sent] == ["oops"]


def test_child_reading_stdin_gets_eof():
    sent = report(py("import sys; print(repr(sys.stdin.read()))"))
    assert [unwrap(p) for p in sent] == ["''"]


def test_git_terminal_prompt_is_off_over_inherited_env(monkeypatch):
    monkeypatch.setenv("OWNER_TEST_VAR", "inherited")
    code = (
        "import os; "
        "print(os.environ['GIT_TERMINAL_PROMPT'], "
        "os.environ['OWNER_TEST_VAR'])"
    )
    sent = report(py(code))
    assert [unwrap(p) for p in sent] == ["0 inherited"]


def test_no_output_gets_exactly_no_output():
    assert report(py("pass")) == ["No output."]


def test_whitespace_only_output_gets_no_output():
    assert report(py("print('  \\n ')")) == ["No output."]


def test_invalid_utf8_is_replaced():
    code = "import sys; sys.stdout.buffer.write(b'a\\xffb')"
    sent = report(py(code))
    assert [unwrap(p) for p in sent] == ["a�b"]


def test_long_output_is_split_within_the_limit():
    lines = [f"line {i:04d} " + "x" * 40 for i in range(200)]
    text = "\n".join(lines)
    sent = report(py(f"print({text!r})"))
    assert len(sent) > 1
    assert all(len(p) <= 2000 for p in sent)
    joined = "".join(unwrap(p) for p in sent)
    expected = "".join(text.split())
    assert "".join(joined.split()) == expected


def test_nonzero_exit_gets_a_final_separate_message():
    argv = py("import sys; print('boom'); sys.exit(3)")
    sent = report(argv)
    assert len(sent) == 2
    assert unwrap(sent[0]) == "boom"
    assert shlex.join(argv) in sent[1]
    assert "3" in sent[1]
    assert PREFIX not in sent[1]


def test_nonzero_exit_without_output_says_no_output_then_exit():
    argv = py("import sys; sys.exit(2)")
    sent = report(argv)
    assert len(sent) == 2
    assert sent[0] == "No output."
    assert shlex.join(argv) in sent[1] and "2" in sent[1]


def test_zero_exit_gets_no_exit_line():
    sent = report(py("print('fine')"))
    assert len(sent) == 1
    assert "exited" not in sent[0]


def test_every_page_is_a_code_block():
    sent = report(py("print('y' * 5000)"))
    assert len(sent) > 1
    for page in sent:
        unwrap(page)


def test_event_loop_stays_free_while_the_child_runs():
    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        task = asyncio.create_task(ticker())
        await run_and_report(FakeContext(), py("import time; time.sleep(1)"))
        task.cancel()
        return ticks

    assert asyncio.run(main()) >= 5


def test_update_says_updating_then_pulls(monkeypatch):
    calls = []

    async def recorder(ctx, argv):
        calls.append((list(ctx.sent), argv))

    monkeypatch.setattr(developer, "run_and_report", recorder)
    ctx = FakeContext()
    asyncio.run(Developer._update.callback(None, ctx))
    assert calls == [(["Updating..."], ["git", "pull"])]


def test_ytdlp_command_is_gone():
    names = {c.name for c in Developer.__cog_commands__}
    assert "ytdlp" not in names
    assert "update" in names
