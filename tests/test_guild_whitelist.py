"""The guild whitelist, which now lives in the database.

It used to be config.GUILD_WHITELIST, rewritten into .env by
Config.save(). That mechanism produced five separate bugs - a leaked
BOT_TOKEN, a colour that changed on every save, a tuple setting that
broke the next startup, writes landing in the wrong directory, and a
removal that did not persist - so the whole write-back path is gone and
these tests cover what replaced it.
"""

import pytest
from discord.ext import commands

from config import config
from musicbot.commands.developer import Developer
from musicbot.guild_whitelist import (
    Outcome,
    WhitelistChange,
    add,
    import_env_whitelist,
    load,
    remove,
)
from musicbot.settings import (
    ENV_WHITELIST_IMPORTED,
    BotState,
)
from musicbot import settings
from db_seam import in_memory_db, seed_whitelist
from developer_fakes import FakeBot, FakeCtx, LeavingGuild
from helpers import run


def with_db(scenario):
    """Run `scenario(factory)` against a fresh in-memory database."""

    async def go():
        async with in_memory_db(settings) as factory:
            return await scenario(factory)

    return run(go())


class TestAdmission:
    def test_empty_whitelist_is_disabled_and_admits_every_guild(self):
        whitelist = with_db(load)
        assert not whitelist.enabled
        assert whitelist.admits(111)
        assert list(whitelist) == []

    def test_listed_whitelist_admits_only_listed_guilds(self):
        async def scenario(factory):
            await seed_whitelist(factory, 222, 111)
            return await load(factory)

        whitelist = with_db(scenario)
        assert whitelist.enabled
        assert whitelist.admits(111) and whitelist.admits(222)
        assert not whitelist.admits(333)
        assert list(whitelist) == [111, 222]

    def test_ids_survive_as_ints(self):
        """Stored as strings to dodge integer overflow, like every
        other Discord id in the schema - but callers compare against
        guild.id, which is an int."""
        big = 609212921897025557

        async def scenario(factory):
            await seed_whitelist(factory, big)
            return await load(factory)

        whitelist = with_db(scenario)
        assert list(whitelist) == [big]
        assert whitelist.admits(big)


class TestChanges:
    def test_first_add_enables_and_leaves_the_other_present_guilds(self):
        async def scenario(factory):
            change = await add(factory, 10, [10, 20, 30])
            return change, list(await load(factory))

        change, listed = with_db(scenario)
        assert change == WhitelistChange(Outcome.ENABLED, (20, 30))
        assert listed == [10]

    def test_enabling_with_no_other_guild_leaves_none(self):
        change = with_db(lambda f: add(f, 10, [10]))
        assert change == WhitelistChange(Outcome.ENABLED, ())

    def test_add_to_an_enabled_whitelist_updates_and_leaves_none(self):
        async def scenario(factory):
            await seed_whitelist(factory, 10)
            change = await add(factory, 20, [10, 20, 30])
            return change, list(await load(factory))

        change, listed = with_db(scenario)
        assert change == WhitelistChange(Outcome.UPDATED, ())
        assert listed == [10, 20]

    def test_add_of_a_listed_guild_changes_nothing(self):
        async def scenario(factory):
            await seed_whitelist(factory, 10)
            change = await add(factory, 10, [10, 20])
            return change, list(await load(factory))

        change, listed = with_db(scenario)
        assert change == WhitelistChange(Outcome.ALREADY_LISTED, ())
        assert listed == [10]

    def test_guard_refuses_and_writes_nothing(self):
        async def scenario(factory):
            change = await add(factory, 20, [10, 20], protect=10)
            return change, await load(factory)

        change, whitelist = with_db(scenario)
        assert change == WhitelistChange(Outcome.WOULD_LEAVE_HERE, ())
        assert not whitelist.enabled

    def test_guard_allows_adding_the_protected_guild(self):
        change = with_db(lambda f: add(f, 10, [10, 20], protect=10))
        assert change == WhitelistChange(Outcome.ENABLED, (20,))

    def test_guard_does_not_apply_from_a_dm(self):
        change = with_db(lambda f: add(f, 20, [10, 20], protect=None))
        assert change == WhitelistChange(Outcome.ENABLED, (10,))

    def test_guard_does_not_apply_on_an_enabled_whitelist(self):
        async def scenario(factory):
            await seed_whitelist(factory, 10)
            return await add(factory, 20, [10, 20, 30], protect=30)

        change = with_db(scenario)
        assert change == WhitelistChange(Outcome.UPDATED, ())

    def test_remove_of_one_of_several_leaves_that_guild_if_present(self):
        async def scenario(factory):
            await seed_whitelist(factory, 10, 11)
            change = await remove(factory, 10, [10, 30])
            return change, list(await load(factory))

        change, listed = with_db(scenario)
        assert change == WhitelistChange(Outcome.UPDATED, (10,))
        assert listed == [11]

    def test_remove_of_a_guild_not_present_leaves_none(self):
        async def scenario(factory):
            await seed_whitelist(factory, 10, 11)
            return await remove(factory, 10, [30])

        change = with_db(scenario)
        assert change == WhitelistChange(Outcome.UPDATED, ())

    def test_remove_of_the_last_id_disables_and_leaves_none(self):
        async def scenario(factory):
            await seed_whitelist(factory, 10)
            change = await remove(factory, 10, [10, 30])
            return change, await load(factory)

        change, whitelist = with_db(scenario)
        assert change == WhitelistChange(Outcome.DISABLED, ())
        assert not whitelist.enabled

    def test_remove_of_an_unlisted_guild_changes_nothing(self):
        async def scenario(factory):
            await seed_whitelist(factory, 10)
            change = await remove(factory, 11, [10, 11])
            return change, list(await load(factory))

        change, listed = with_db(scenario)
        assert change == WhitelistChange(Outcome.NOT_LISTED, ())
        assert listed == [10]

    def test_remove_from_an_empty_whitelist_is_not_listed(self):
        change = with_db(lambda f: remove(f, 10, [10]))
        assert change.outcome is Outcome.NOT_LISTED

    def test_large_ids_survive_as_ints(self):
        big = 609212921897025557

        async def scenario(factory):
            await add(factory, big, [big])
            return list(await load(factory))

        assert with_db(scenario) == [big]


class TestEnvImport:
    def test_imports_the_env_var_once(self, monkeypatch):
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111, 222])

        async def scenario(factory):
            await import_env_whitelist(factory)
            return list(await load(factory))

        assert with_db(scenario) == [111, 222]

    def test_import_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111])

        async def scenario(factory):
            await import_env_whitelist(factory)
            await import_env_whitelist(factory)
            return list(await load(factory))

        assert with_db(scenario) == [111]

    def test_emptying_the_whitelist_survives_a_restart(self, monkeypatch):
        """The reason the marker row exists. Keying the import off "the
        table is empty" would resurrect every id from .env the next time
        the bot started, which is the same class of bug as the removal
        that used not to persist."""
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111, 222])

        async def scenario(factory):
            await import_env_whitelist(factory)
            for guild_id in (111, 222):
                await remove(factory, guild_id, [])
            await import_env_whitelist(factory)  # the next startup
            return await load(factory)

        assert not with_db(scenario).enabled

    def test_tolerates_rows_that_already_exist(self, monkeypatch):
        """A missing marker does not imply an empty table: two processes
        sharing one database can both pass the guard, and a restored
        backup can have rows without the marker. Inserting blindly
        raised IntegrityError out of MusicBot.start() and the bot never
        logged in."""
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111, 222])

        async def scenario(factory):
            await seed_whitelist(factory, 111)
            await import_env_whitelist(factory)  # must not raise
            return list(await load(factory))

        assert with_db(scenario) == [111, 222]

    def test_marker_is_written_even_with_an_empty_env_var(self, monkeypatch):
        monkeypatch.setattr(config, "GUILD_WHITELIST", [])

        async def scenario(factory):
            await import_env_whitelist(factory)
            async with factory() as session:
                return await session.get(BotState, ENV_WHITELIST_IMPORTED)

        assert with_db(scenario) is not None

    def test_a_later_env_edit_is_ignored(self, monkeypatch):
        """Once imported the variable is inert - the database owns the
        whitelist, and two sources of truth is what this replaced."""
        monkeypatch.setattr(config, "GUILD_WHITELIST", [111])

        async def scenario(factory):
            await import_env_whitelist(factory)
            monkeypatch.setattr(config, "GUILD_WHITELIST", [111, 333])
            await import_env_whitelist(factory)
            return list(await load(factory))

        assert with_db(scenario) == [111]


class OrderedCtx(FakeCtx):
    def __init__(self, bot, log, guild=None):
        super().__init__(bot, guild=guild)
        self.log = log

    async def send(self, text):
        self.log.append(("send", text))
        await super().send(text)


def group(bot=None):
    cog = Developer(bot)
    return cog, next(
        c for c in cog.get_commands() if c.name == "guild_whitelist"
    )


def command(guilds, listed, name, arg=None, invoking=None):
    """Run `d!gw <name> <arg>` over a real in-memory database holding
    `listed`; return (replies, event log, ids listed afterwards)."""
    log = []
    for g in guilds:
        g.log = log

    async def scenario(factory):
        await seed_whitelist(factory, *listed)
        bot = FakeBot(guilds, DbSession=factory)
        cog, grp = group(bot)
        ctx = OrderedCtx(bot, log, guild=invoking)
        if name is None:
            # discord.py rebinds the cog's show callback to its Command,
            # which only knows its cog once the cog is added to a bot
            cog._show_guild_whitelist_callback.cog = cog
            await grp.callback(cog, ctx)
        else:
            kwargs = {} if arg is None else {"id": arg}
            await grp.get_command(name).callback(cog, ctx, **kwargs)
        return ctx.sent, list(await load(factory))

    sent, after = with_db(scenario)
    return sent, log, after


def left(guilds):
    return [g.id for g in guilds if g.left]


class TestShow:
    @pytest.mark.parametrize("name", [None, "show"])
    def test_disabled(self, name):
        sent, _, _ = command([LeavingGuild(1)], [], name)
        assert sent == ["Whitelist is disabled."]

    @pytest.mark.parametrize("name", [None, "show"])
    def test_lists_ids_with_the_names_of_present_guilds(self, name):
        sent, _, _ = command([LeavingGuild(10, "ten")], [11, 10], name)
        assert sent == ["```\n10 ten\n11```"]


class TestAddCommand:
    def test_enabling_leaves_the_other_guilds_after_replying(self):
        added, other = LeavingGuild(10, "addedname"), LeavingGuild(
            20, "othername"
        )
        guilds = [added, other]
        sent, log, after = command(guilds, [], "add", "10")
        assert sent == [
            "Whitelist updated, the whitelist is now enabled."
            " Leaving these guilds:\n20 othername"
        ]
        assert log[0][0] == "send" and log[1:] == [("leave", 20)]
        assert left(guilds) == [20] and after == [10]

    def test_enabling_with_no_other_guild_names_none(self):
        g = LeavingGuild(10, "addedname")
        sent, _, _ = command([g], [], "add", "10")
        assert sent == ["Whitelist updated, the whitelist is now enabled."]
        assert not g.left

    def test_updating_an_enabled_whitelist_leaves_nobody(self):
        guilds = [LeavingGuild(10), LeavingGuild(20)]
        sent, _, after = command(guilds, [10], "add", "30")
        assert sent == ["Whitelist updated."]
        assert left(guilds) == [] and after == [10, 30]

    def test_already_listed(self):
        guilds = [LeavingGuild(10)]
        sent, _, after = command(guilds, [10], "add", "10")
        assert sent == ["That guild is already whitelisted."]
        assert left(guilds) == [] and after == [10]

    def test_refused_when_it_would_leave_the_invoking_guild(self):
        here, other = LeavingGuild(10), LeavingGuild(20)
        sent, _, after = command([here, other], [], "add", "20", invoking=here)
        assert sent == [
            "That would make the bot leave this guild, so nothing was"
            " changed. Add this guild's id first."
        ]
        assert left([here, other]) == [] and after == []

    def test_from_a_dm_there_is_no_guard(self):
        guilds = [LeavingGuild(10), LeavingGuild(20)]
        sent, _, after = command(guilds, [], "add", "20", invoking=None)
        assert sent[0].startswith("Whitelist updated, the whitelist is now")
        assert left(guilds) == [10] and after == [20]

    def test_a_failed_leave_is_logged_and_the_rest_are_left(self, capsys):
        bad, good = LeavingGuild(10, "badname", fail=True), LeavingGuild(20)
        keep = LeavingGuild(30)
        command([bad, good, keep], [], "add", "30")
        assert good.left and not bad.left
        assert "badname" in capsys.readouterr().err


class TestRemoveCommand:
    def test_one_of_several_replies_then_leaves_that_guild(self):
        guilds = [LeavingGuild(10), LeavingGuild(11)]
        sent, log, after = command(guilds, [10, 11], "remove", "10")
        assert sent == ["Whitelist updated, leaving the guild."]
        assert log[0][0] == "send" and log[1:] == [("leave", 10)]
        assert left(guilds) == [10] and after == [11]

    def test_takes_the_last_word_of_the_argument(self):
        guilds = [LeavingGuild(10)]
        _, _, after = command(guilds, [10, 11], "remove", "name 10")
        assert left(guilds) == [10] and after == [11]

    def test_one_of_several_not_present_leaves_nobody(self):
        guilds = [LeavingGuild(12)]
        sent, _, _ = command(guilds, [10, 11], "remove", "10")
        assert sent == ["Whitelist updated."] and left(guilds) == []

    def test_last_id_disables_and_leaves_nobody(self):
        g = LeavingGuild(10)
        sent, _, after = command([g], [10], "remove", "10")
        assert sent == [
            "Whitelist updated. The whitelist is now empty and"
            " therefore disabled."
        ]
        assert not g.left and after == []

    def test_the_lockout_guard_does_not_apply_to_remove(self):
        here = LeavingGuild(10)
        other = LeavingGuild(11)
        sent, _, after = command(
            [here, other], [10, 11], "remove", "10", invoking=here
        )
        assert sent == ["Whitelist updated, leaving the guild."]
        assert left([here, other]) == [10] and after == [11]

    def test_not_listed(self):
        g = LeavingGuild(11)
        sent, _, after = command([g], [10], "remove", "11")
        assert sent == ["That guild is not whitelisted."]
        assert not g.left and after == [10]


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


def test_leave_guilds_skips_an_id_the_bot_is_not_in_silently(capsys):
    present = LeavingGuild(10)
    bot = FakeBot([present])
    run(bot.leave_guilds([99, 10]))
    assert present.left
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
