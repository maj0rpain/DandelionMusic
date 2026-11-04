"""The developer cog: d!execute's print capture, driven on fake
contexts."""

import asyncio

from musicbot.commands.developer import Developer
from developer_fakes import FakeBot, FakeCtx


def execute(code):
    cog = Developer(None)
    ctx = FakeCtx(FakeBot([]))
    asyncio.run(cog._execute.callback(cog, ctx, code=code))
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

    asyncio.run(main())
    assert "mine" in ctx.sent[0] and "OTHER" not in ctx.sent[0]
    assert "OTHER" in capsys.readouterr().out
