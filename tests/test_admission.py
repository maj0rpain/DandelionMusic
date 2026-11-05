"""Request admission: one table of request kind x situation.

Each row's outcome is a refusal with a message, an admission with no
join, or an admission with a connect or a move. The voice fakes are the
connect/move port.
"""

import asyncio

import discord
import pytest

from config import config
from musicbot import admission
from musicbot.admission import RequestKind as K
from musicbot.utils import CheckError
from discord_fakes import (
    BOTS_ONLY,
    ADMISSION_DJ_ROLE,
    HERE,
    PLAYER_BUTTONS,
    PEOPLE,
    fake_controller,
    situation,
)
from helpers import run


NOT_IN_VC = config.USER_NOT_IN_VC_MESSAGE
NO_PERMS = config.VOICE_PERMISSIONS_MISSING
WRONG_CH = config.WRONG_CHANNEL_MESSAGE
NOT_CONNECTED = config.NOT_CONNECTED_MESSAGE
NOT_DJ = config.NOT_A_DJ


def run_request(ctx, kind):
    async def go():
        entry = await admission.check(
            ctx, None if kind in admission.COMMAND_KINDS else kind
        )
        return await entry.enter()

    return run(go())


OPEN_BUTTONS = ["current_song", "queue"]
DJ_BUTTONS = [b for b in PLAYER_BUTTONS if b not in OPEN_BUTTONS]
JOINING = [K.JOINING_COMMAND, K.PICK, K.REACTION]
EVERY = [K.COMMAND, K.JOINING_COMMAND, K.PLAYER_BUTTON, K.PICK, K.REACTION]


def rows():
    out = []
    for kind in JOINING:
        n = kind.name.lower()
        out += [
            (f"{n}: idle, author in voice connects", kind, {}, "connect"),
            (
                f"{n}: idle, author not in voice",
                kind,
                {"author": None},
                NOT_IN_VC,
            ),
            (
                f"{n}: idle, wrong channel",
                kind,
                {"wrong_channel": True},
                WRONG_CH,
            ),
            (
                f"{n}: idle, no connect permission",
                kind,
                {"perms": {"connect": False}},
                NO_PERMS,
            ),
            (
                f"{n}: idle, no speak permission",
                kind,
                {"perms": {"speak": False}},
                NO_PERMS,
            ),
            (
                f"{n}: with the author, no join",
                kind,
                {"bot": HERE, "author": "a", "perms": {"connect": False}},
                "admitted",
            ),
            (
                f"{n}: alone in a bots-only channel moves",
                kind,
                {"bot": BOTS_ONLY},
                "move",
            ),
            (
                f"{n}: bots-only channel, rule off, stays",
                kind,
                {"bot": BOTS_ONLY, "rule": False},
                "admitted",
            ),
            (
                f"{n}: bots-only channel, no permission on target",
                kind,
                {"bot": BOTS_ONLY, "perms": {"speak": False}},
                NO_PERMS,
            ),
            (
                f"{n}: bots-only channel, author not in voice",
                kind,
                {"bot": BOTS_ONLY, "author": None},
                NOT_IN_VC,
            ),
            (
                f"{n}: bot with others, plain member",
                kind,
                {"bot": PEOPLE},
                NOT_IN_VC,
            ),
            (
                f"{n}: bot with others, rule off",
                kind,
                {"bot": PEOPLE, "rule": False},
                "admitted",
            ),
        ]
    for kind in EVERY:
        n = kind.name.lower()
        if kind is K.PLAYER_BUTTON:
            continue
        out += [
            (
                f"{n}: bot with others, author not in voice",
                kind,
                {"bot": PEOPLE, "author": None},
                NOT_IN_VC,
            )
        ]
    for kind in EVERY:
        n = kind.name.lower()
        base = {"bot": PEOPLE}
        out += [
            (
                f"{n}: bot with others, admin",
                kind,
                {**base, "role": "admin"},
                "admitted",
            ),
            (
                f"{n}: bot with others, owner",
                kind,
                {**base, "role": "owner"},
                "admitted",
            ),
            (
                f"{n}: bot with others, DJ-role holder",
                kind,
                {**base, "role": "dj", "dj_role": ADMISSION_DJ_ROLE},
                "admitted",
            ),
            (
                f"{n}: with the author, wrong channel",
                kind,
                {"bot": HERE, "author": "a", "wrong_channel": True},
                WRONG_CH,
            ),
        ]
    # a command that is not a joining one never joins
    out += [
        ("command: idle, no join", K.COMMAND, {}, "admitted"),
        (
            "command: idle, author not in voice",
            K.COMMAND,
            {"author": None},
            "admitted",
        ),
        (
            "command: idle, no permission, no join",
            K.COMMAND,
            {"perms": {"connect": False}},
            "admitted",
        ),
        (
            "command: bots-only channel, no move",
            K.COMMAND,
            {"bot": BOTS_ONLY},
            "admitted",
        ),
        (
            "command: idle, wrong channel",
            K.COMMAND,
            {"wrong_channel": True},
            WRONG_CH,
        ),
    ]
    # player buttons never join
    out += [
        (
            "player_button: bot out of voice",
            K.PLAYER_BUTTON,
            {},
            NOT_CONNECTED,
        ),
        (
            "player_button: with the author",
            K.PLAYER_BUTTON,
            {"bot": HERE, "author": "a"},
            "admitted",
        ),
        (
            "player_button: bots-only channel, no move",
            K.PLAYER_BUTTON,
            {"bot": BOTS_ONLY},
            "admitted",
        ),
        (
            "player_button: bot with others, rule off, author outside",
            K.PLAYER_BUTTON,
            {"bot": PEOPLE, "rule": False},
            "admitted",
        ),
        (
            "player_button: wrong channel",
            K.PLAYER_BUTTON,
            {"bot": HERE, "author": "a", "wrong_channel": True},
            WRONG_CH,
        ),
    ]
    for cid in DJ_BUTTONS:
        out += [
            (
                f"player_button {cid}: plain member under a DJ role",
                K.PLAYER_BUTTON,
                {
                    "bot": HERE,
                    "author": "a",
                    "custom_id": cid,
                    "dj_role": ADMISSION_DJ_ROLE,
                },
                NOT_DJ,
            ),
            (
                f"player_button {cid}: DJ-role holder",
                K.PLAYER_BUTTON,
                {
                    "bot": HERE,
                    "author": "a",
                    "custom_id": cid,
                    "dj_role": ADMISSION_DJ_ROLE,
                    "role": "dj",
                },
                "admitted",
            ),
        ]
    for cid in OPEN_BUTTONS:
        out.append(
            (
                f"player_button {cid}: plain member under a DJ role",
                K.PLAYER_BUTTON,
                {
                    "bot": HERE,
                    "author": "a",
                    "custom_id": cid,
                    "dj_role": ADMISSION_DJ_ROLE,
                },
                "admitted",
            )
        )
    return out


ROWS = rows()


@pytest.mark.parametrize(
    "kind, spec, outcome",
    [r[1:] for r in ROWS],
    ids=[r[0] for r in ROWS],
)
def test_admission_table(kind, spec, outcome, no_sleep):
    guild, ctx = situation(kind, **spec)
    if outcome in ("connect", "move", "admitted"):
        controller = run_request(ctx, kind)
        assert controller is ctx.bot.sessions.controller(guild)
        expected = {
            "connect": [("connect", "b")],
            "move": [("move", "b")],
            "admitted": [],
        }[outcome]
        assert guild.events == expected
    else:
        with pytest.raises(CheckError) as exc:
            run_request(ctx, kind)
        assert str(exc.value) == outcome
        assert guild.events == []


@pytest.mark.parametrize("kind", JOINING, ids=lambda k: k.name)
def test_a_failed_connect_is_refused_and_logged(kind, capsys):
    guild, ctx = situation(kind)
    guild.fail = asyncio.TimeoutError()
    with pytest.raises(CheckError) as exc:
        run_request(ctx, kind)
    assert str(exc.value) == config.VOICE_CONNECT_FAILED
    assert "TimeoutError" in capsys.readouterr().err


@pytest.mark.parametrize("kind", JOINING, ids=lambda k: k.name)
def test_a_failed_move_is_refused_and_logged(kind, capsys, no_sleep):
    guild, ctx = situation(kind, bot=BOTS_ONLY)
    guild.fail = discord.ClientException("Not connected to voice")
    with pytest.raises(CheckError) as exc:
        run_request(ctx, kind)
    assert str(exc.value) == config.VOICE_CONNECT_FAILED
    assert guild.events == [("move", "b")]
    assert "Not connected to voice" in capsys.readouterr().err


@pytest.mark.parametrize("kind", EVERY, ids=lambda k: k.name)
def test_no_controller_is_refused_as_not_ready_before_other_checks(kind):
    # a wrong channel and a missing author would refuse otherwise
    guild, ctx = situation(
        kind, registered=False, wrong_channel=True, author=None
    )
    with pytest.raises(CheckError) as exc:
        run_request(ctx, kind)
    assert str(exc.value) == config.BOT_NOT_READY


@pytest.mark.parametrize("kind", [K.COMMAND, K.JOINING_COMMAND])
def test_a_passed_command_kind_is_a_programming_error(kind):
    _, ctx = situation(kind)
    with pytest.raises(ValueError):
        run(admission.check(ctx, kind))


def test_enter_returns_the_controller_current_when_it_acts():
    guild, ctx = situation(K.JOINING_COMMAND)
    entry = run(admission.check(ctx))
    assert entry.audiocontroller is ctx.bot.sessions.controller(guild)
    ctx.controllers[guild] = fake_controller()
    run(ctx.bot.sessions.reset(guild))
    assert run(entry.enter()) is ctx.bot.sessions.controller(guild)
    assert run(entry.enter()) is not entry.audiocontroller


# --- the Music cog's hooks ------------------------------------------
#
# discord.py runs the cog check, then the command's own checks, then
# the before-invoke hook, and only then the command.


def music_ctx(name, **spec):
    from musicbot.commands.music import Music

    guild, ctx = situation(K.COMMAND, **spec)
    cog = Music(None)
    ctx.command = {c.qualified_name: c for c in cog.walk_commands()}[name]

    async def defer(**kwargs):
        guild.events.append(("defer",))

    ctx.defer = defer
    return guild, ctx, cog


async def invoke_hooks(cog, ctx):
    await cog.cog_check(ctx)
    for command_check in ctx.command.checks:
        await command_check(ctx)
    await cog.cog_before_invoke(ctx)


def test_a_joining_command_refused_by_its_dj_check_never_joins():
    guild, ctx, cog = music_ctx("prev", dj_role=ADMISSION_DJ_ROLE)
    with pytest.raises(CheckError) as exc:
        run(invoke_hooks(cog, ctx))
    assert str(exc.value) == NOT_DJ
    assert guild.events == []


def test_a_joining_command_defers_then_joins_after_its_dj_check():
    guild, ctx, cog = music_ctx("prev", dj_role=ADMISSION_DJ_ROLE, role="dj")
    run(invoke_hooks(cog, ctx))
    assert guild.events == [("defer",), ("connect", "b")]


def test_a_command_without_a_join_neither_defers_nor_joins():
    guild, ctx, cog = music_ctx("history")
    run(invoke_hooks(cog, ctx))
    assert guild.events == []


def test_the_command_acts_on_the_controller_enter_returns():
    guild, ctx, cog = music_ctx("play")
    run(cog.cog_check(ctx))
    first = ctx.audiocontroller
    ctx.controllers[guild] = fake_controller()
    run(ctx.bot.sessions.reset(guild))
    run(cog.cog_before_invoke(ctx))
    assert ctx.audiocontroller is not first
    assert ctx.audiocontroller is ctx.bot.sessions.controller(guild)
    assert ctx.audiocontroller.command_channel is ctx


def test_command_level_checks_read_the_controller_off_the_context():
    from musicbot.commands.music import active_only

    guild, ctx, cog = music_ctx("history")
    ctx.controllers[guild].is_active = lambda: True
    run(cog.cog_check(ctx))
    assert run(active_only.predicate(ctx)) is True
