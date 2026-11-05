"""The guild whitelist, which now lives in the database.

It used to be config.GUILD_WHITELIST, rewritten into .env by
Config.save(). That mechanism produced five separate bugs - a leaked
BOT_TOKEN, a colour that changed on every save, a tuple setting that
broke the next startup, writes landing in the wrong directory, and a
removal that did not persist - so the whole write-back path is gone and
these tests cover what replaced it.
"""

import types

import pytest
from discord.ext import commands
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from config import config
from musicbot.commands import developer
from musicbot.commands.developer import Developer
from musicbot.settings import (
    ENV_WHITELIST_IMPORTED,
    BotState,
    add_to_guild_whitelist,
    get_guild_whitelist,
    import_env_whitelist,
    remove_from_guild_whitelist,
    run_migrations,
)
from developer_fakes import FakeBot, FakeCtx
from helpers import run


@pytest.fixture
def bot(tmp_path):
    """A stand-in exposing just the DbSession the helpers use, backed
    by a real sqlite file with the schema applied."""
    # NullPool because each helper below runs under its own
    # run(), a fresh event loop each time: a pooled connection would
    # be reused across event loops, which happens to work with
    # aiosqlite but is the classic shape of a connection bound to a
    # closed loop.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}",
        poolclass=NullPool,
    )
    stub = types.SimpleNamespace(
        DbSession=sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
    )

    async def setup():
        async with engine.connect() as conn:
            await conn.run_sync(run_migrations)

    run(setup())
    return stub


class TestAddAndRemove:
    def test_starts_empty(self, bot):
        assert run(get_guild_whitelist(bot)) == set()

    def test_add_then_read_back(self, bot):
        assert run(add_to_guild_whitelist(bot, 111)) is True
        assert run(get_guild_whitelist(bot)) == {111}

    def test_adding_twice_reports_false(self, bot):
        run(add_to_guild_whitelist(bot, 111))
        assert run(add_to_guild_whitelist(bot, 111)) is False
        assert run(get_guild_whitelist(bot)) == {111}

    def test_remove(self, bot):
        run(add_to_guild_whitelist(bot, 111))
        assert run(remove_from_guild_whitelist(bot, 111)) is True
        assert run(get_guild_whitelist(bot)) == set()

    def test_removing_an_absent_guild_reports_false(self, bot):
        assert run(remove_from_guild_whitelist(bot, 999)) is False

    def test_ids_survive_as_ints(self, bot):
        """Stored as strings to dodge integer overflow, like every
        other Discord id in the schema - but callers compare against
        guild.id, which is an int."""
        big = 609212921897025557
        run(add_to_guild_whitelist(bot, big))
        assert run(get_guild_whitelist(bot)) == {big}


class TestEnvImport:
    def test_imports_the_env_var_once(self, bot, monkeypatch):
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111, 222])
        run(import_env_whitelist(bot))
        assert run(get_guild_whitelist(bot)) == {111, 222}

    def test_import_is_idempotent(self, bot, monkeypatch):
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111])
        run(import_env_whitelist(bot))
        run(import_env_whitelist(bot))
        assert run(get_guild_whitelist(bot)) == {111}

    def test_emptying_the_whitelist_survives_a_restart(self, bot, monkeypatch):
        """The reason the marker row exists. Keying the import off "the
        table is empty" would resurrect every id from .env the next time
        the bot started, which is the same class of bug as the removal
        that used not to persist."""
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111, 222])
        run(import_env_whitelist(bot))
        for guild_id in (111, 222):
            run(remove_from_guild_whitelist(bot, guild_id))

        run(import_env_whitelist(bot))  # the next startup

        assert run(get_guild_whitelist(bot)) == set()

    def test_tolerates_rows_that_already_exist(self, bot, monkeypatch):
        """A missing marker does not imply an empty table: two processes
        sharing one database can both pass the guard, and a restored
        backup can have rows without the marker. Inserting blindly
        raised IntegrityError out of MusicBot.start() and the bot never
        logged in."""
        run(add_to_guild_whitelist(bot, 111))
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111, 222])

        run(import_env_whitelist(bot))  # must not raise

        assert run(get_guild_whitelist(bot)) == {111, 222}

    def test_marker_is_written_even_with_an_empty_env_var(
        self, bot, monkeypatch
    ):
        monkeypatch.setattr(config, "GUILD_WHITELIST", [])
        run(import_env_whitelist(bot))

        async def marker():
            async with bot.DbSession() as session:
                return await session.get(BotState, ENV_WHITELIST_IMPORTED)

        assert run(marker()) is not None

    def test_a_later_env_edit_is_ignored(self, bot, monkeypatch):
        """Once imported the variable is inert - the database owns the
        whitelist, and two sources of truth is what this replaced."""
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111])
        run(import_env_whitelist(bot))

        monkeypatch.setattr(config, "GUILD_WHITELIST", [111, 333])
        run(import_env_whitelist(bot))

        assert run(get_guild_whitelist(bot)) == {111}


def test_config_no_longer_writes_anything():
    """Config is read-only now. save() and the .env writer are gone."""
    for gone in ("save", "_update_env_files", "_replace_env_var"):
        assert not hasattr(config, gone), f"{gone} is still there"


def test_commands_use_the_database():
    import inspect

    from musicbot.commands.developer import Developer

    for cmd in (
        Developer._guild_whitelist_add,
        Developer._guild_whitelist_remove,
    ):
        src = inspect.getsource(cmd.callback)
        assert "guild_whitelist(ctx.bot" in src
        assert "config." not in src


class FakeGuild:
    def __init__(self, id_, name="g"):
        self.id = id_
        self.name = name
        self.left = False

    async def leave(self):
        self.left = True


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
    run(grp.get_command(name).callback(cog, ctx, id=arg))
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
def test_whitelist_commands_admit_every_owner_and_refuse_others(
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

    run(check(5))
    run(check(1))
    with pytest.raises(commands.NotOwner):
        run(check(99))
