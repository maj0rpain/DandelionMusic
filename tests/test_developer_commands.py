"""The developer cog: d!execute's print capture, driven on fake
contexts."""

import asyncio
import types

from musicbot.commands.developer import Developer


class FakeBot:
    def __init__(self, guilds, owner_id=1):
        self.guilds = guilds
        self.owner_id = owner_id

    def get_guild(self, id_):
        return next((g for g in self.guilds if g.id == id_), None)

    async def is_owner(self, user):
        return user.id == self.owner_id


class FakeCtx:
    def __init__(self, bot, author_id=1):
        self.bot = bot
        self.author = types.SimpleNamespace(id=author_id)
        self.sent = []
        self.channel = types.SimpleNamespace(last_message=None)
        self.me = object()

    async def send(self, text):
        self.sent.append(text)


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
