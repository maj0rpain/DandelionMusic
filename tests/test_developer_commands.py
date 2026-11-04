"""The developer cog: whitelist toggling replies, owner checks and
d!execute's print capture, driven on fake contexts."""

import asyncio
import io
import types

import pytest
from discord.ext import commands

from config import config
from musicbot.commands import developer
from musicbot.commands.developer import Developer


class FakeGuild:
    def __init__(self, id_, name="g"):
        self.id = id_
        self.name = name
        self.left = False

    async def leave(self):
        self.left = True


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


@pytest.fixture
def whitelist(monkeypatch):
    ids = set()

    async def get(bot):
        return set(ids)

    async def add(bot, id_):
        if id_ in ids:
            return False
        ids.add(id_)
        return True

    async def remove(bot, id_):
        if id_ not in ids:
            return False
        ids.discard(id_)
        return True

    monkeypatch.setattr(developer, "get_guild_whitelist", get)
    monkeypatch.setattr(developer, "add_to_guild_whitelist", add)
    monkeypatch.setattr(developer, "remove_from_guild_whitelist", remove)
    return ids


def group(bot=None):
    cog = Developer(bot)
    return cog, next(
        c for c in cog.get_commands() if c.name == "guild_whitelist"
    )


def run_sub(name, bot, arg):
    cog, grp = group(bot)
    ctx = FakeCtx(bot)
    asyncio.run(grp.get_command(name).callback(cog, ctx, id=arg))
    return ctx.sent


def test_removing_last_id_disables_and_leaves_nobody(whitelist):
    whitelist.add(10)
    g = FakeGuild(10)
    sent = run_sub("remove", FakeBot([g]), "10")
    assert not g.left
    assert "empty" in sent[0] and "disabled" in sent[0]


def test_removing_one_of_several_leaves_that_guild(whitelist):
    whitelist.update({10, 11})
    g = FakeGuild(10)
    run_sub("remove", FakeBot([g]), "10")
    assert g.left


def test_adding_first_id_lists_guilds_to_be_left(whitelist):
    added, other = FakeGuild(10, "addedname"), FakeGuild(20, "othername")
    sent = run_sub("add", FakeBot([added, other]), "10")
    assert "enabled" in sent[0]
    assert "othername" in sent[0] and "addedname" not in sent[0]
    assert not added.left and not other.left


def test_adding_first_id_with_no_other_guild_has_no_list(whitelist):
    sent = run_sub("add", FakeBot([FakeGuild(10, "addedname")]), "10")
    assert "enabled" in sent[0]
    assert "addedname" not in sent[0]


def test_remove_absent_and_add_present_replies(whitelist):
    whitelist.add(10)
    g = FakeGuild(11)
    assert run_sub("remove", FakeBot([g]), "11") == [
        "That guild is not whitelisted."
    ]
    assert run_sub("add", FakeBot([g]), "10") == [
        "That guild is already whitelisted."
    ]
    assert whitelist == {10} and not g.left


@pytest.mark.parametrize("name", [None, "show", "add", "remove"])
def test_whitelist_commands_admit_extra_owners_only(
    monkeypatch, name
):
    monkeypatch.setattr(config, "EXTRA_OWNERS", [5])
    cog, grp = group()
    cmd = grp if name is None else grp.get_command(name)
    bot = FakeBot([])

    async def check(author_id):
        ctx = FakeCtx(bot, author_id)
        for c in cmd.checks:
            await c(ctx)

    asyncio.run(check(5))
    asyncio.run(check(1))
    with pytest.raises(commands.NotOwner):
        asyncio.run(check(99))


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
