"""The developer cog: d!execute's print capture, driven on fake
contexts."""

import asyncio
import types

from musicbot.commands.developer import OUTPUT_FORMAT, Developer
from developer_fakes import FakeBot, FakeCtx
from helpers import run

PREFIX, SUFFIX = OUTPUT_FORMAT.split("{}")


def execute(code):
    cog = Developer(None)
    ctx = FakeCtx(FakeBot([]))
    run(cog._execute.callback(cog, ctx, code=code))
    return ctx.sent


def test_execute_captures_own_print():
    assert "hello" in execute("print('hello')")[0]


def test_execute_honours_print_file():
    sent = execute("import io\nprint('x', file=io.StringIO())")
    assert sent == ["No output."]


def test_execute_ignores_concurrent_prints(capsys):
    cog = Developer(None)
    ctx = FakeCtx(FakeBot([]))

    async def main():
        started, done = asyncio.Event(), asyncio.Event()
        ctx.started, ctx.done = started, done

        async def other():
            await started.wait()
            print("OTHER")
            done.set()

        t = asyncio.ensure_future(other())
        await cog._execute.callback(
            cog,
            ctx,
            code="ctx.started.set()\nawait ctx.done.wait()\nprint('mine')",
        )
        await t

    run(main())
    assert "mine" in ctx.sent[0] and "OTHER" not in ctx.sent[0]
    assert "OTHER" in capsys.readouterr().out


def test_execute_sends_long_output_as_ordered_code_block_pages():
    sent = execute("for i in range(600):\n    print(f'line {i:04d}')")
    assert len(sent) > 1
    assert all(p.startswith(PREFIX) and p.endswith(SUFFIX) for p in sent)
    body = "\n".join(p[len(PREFIX) : -len(SUFFIX)] for p in sent)
    assert body.splitlines() == [f"line {i:04d}" for i in range(600)]


def test_execute_sends_short_output_as_one_code_block_page():
    assert execute("print('hi')") == [OUTPUT_FORMAT.format("hi")]


def test_execute_no_output_sends_nothing_after_bots_own_message():
    cog = Developer(None)
    ctx = FakeCtx(FakeBot([]))
    ctx.channel.last_message = types.SimpleNamespace(author=ctx.me)
    run(cog._execute.callback(cog, ctx, code="pass"))
    assert ctx.sent == []


def test_execute_no_output_replies_after_another_authors_message():
    cog = Developer(None)
    ctx = FakeCtx(FakeBot([]))
    ctx.channel.last_message = types.SimpleNamespace(author=object())
    run(cog._execute.callback(cog, ctx, code="pass"))
    assert ctx.sent == ["No output."]
