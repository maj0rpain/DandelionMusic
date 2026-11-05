"""General's voice commands go through request admission: the command
channel and user_must_be_in_vc rules apply, and d!reset admits twice."""

import asyncio

import pytest
from discord.ext import commands

from config import config
from musicbot.commands.general import General
from musicbot.utils import CheckError
from helpers import run
from test_admission import HERE, PEOPLE, situation
from test_admission import K


async def _no_sleep(_seconds):
    pass


def prepare(name, kind, **kw):
    """(guild, ctx, sent, disconnects) for running General's command
    `name` with the controller and replies recorded."""
    guild, ctx = situation(kind, **kw)
    ctx.command = {c.qualified_name: c for c in General(None).walk_commands()}[
        name
    ]
    sent = []
    disconnects = []

    async def send(content, **kwargs):
        sent.append(content)

    async def defer(**kwargs):
        pass

    async def udisconnect(reason):
        disconnects.append(reason)
        return True

    ctx.send = send
    ctx.defer = defer
    ctx.controllers[guild].udisconnect = udisconnect
    return guild, ctx, sent, disconnects


def invoke(name, ctx):
    callback = getattr(General, f"_{name}").callback
    try:
        run(callback(General(None), ctx))
    except CheckError as e:
        return str(e)
    return None


COMMANDS = [
    ("connect", K.JOINING_COMMAND),
    ("disconnect", K.COMMAND),
    ("reset", K.JOINING_COMMAND),
]


@pytest.mark.parametrize("name, kind", COMMANDS)
def test_voice_commands_refuse_a_wrong_command_channel(name, kind):
    _, ctx, sent, _ = prepare(name, kind, bot=HERE, wrong_channel=True)
    assert invoke(name, ctx) == config.WRONG_CHANNEL_MESSAGE
    assert sent == []


def test_other_commands_have_no_cog_hooks():
    assert General.cog_check is commands.Cog.cog_check
    assert General.cog_before_invoke is commands.Cog.cog_before_invoke


def test_disconnect_is_admitted_from_outside_the_bots_channel_rule_off():
    _, ctx, sent, disconnects = prepare(
        "disconnect", K.COMMAND, bot=PEOPLE, author="b", rule=False
    )
    assert invoke("disconnect", ctx) is None
    assert disconnects == ["command"]
    assert sent == ["Disconnected."]


@pytest.mark.parametrize("name, kind", COMMANDS)
def test_voice_commands_refuse_an_outsider_rule_on(name, kind):
    _, ctx, _, disconnects = prepare(name, kind, bot=PEOPLE, author="b")
    assert invoke(name, ctx) == config.USER_NOT_IN_VC_MESSAGE
    assert disconnects == []


@pytest.mark.parametrize("role", ["admin", "owner", "dj"])
def test_disconnect_admits_an_admin_owner_or_dj(role):
    _, ctx, _, disconnects = prepare(
        "disconnect",
        K.COMMAND,
        bot=PEOPLE,
        author="b",
        role=role,
        dj_role=99,
    )
    assert invoke("disconnect", ctx) is None
    assert disconnects == ["command"]


def test_reset_refused_by_its_first_check_disposes_nothing():
    guild, ctx, _, disconnects = prepare(
        "reset", K.JOINING_COMMAND, bot=PEOPLE, author="b"
    )
    old = ctx.bot.sessions.controller(guild)
    assert invoke("reset", ctx) == config.USER_NOT_IN_VC_MESSAGE
    assert ctx.bot.sessions.controller(guild) is old
    assert disconnects == []


def reset_with_voice_after(post):
    """Runs d!reset for an author in "b" with the bot idle; `post` is
    called after the reset to change the voice state."""
    guild, ctx, sent, _ = prepare(
        "reset", K.JOINING_COMMAND, bot=None, author="b"
    )
    sessions = ctx.bot.sessions
    real = sessions.reset

    async def reset(g, reason):
        result = await real(g, reason)
        post(guild, ctx)
        return result

    sessions.reset = reset
    return guild, ctx, sent, invoke("reset", ctx)


def test_reset_joins_from_the_post_reset_voice_state():
    guild, ctx, sent, refusal = reset_with_voice_after(lambda g, c: None)
    assert refusal is None
    assert guild.events == [("connect", "b")]
    assert sent == [":white_check_mark: Connected to b"]


def test_reset_still_refuses_when_the_bot_is_in_voice_after_it(
    monkeypatch,
):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def post(guild, ctx):
        guild.put_bot_in(guild.channel("a", humans=1))

    guild, _, sent, refusal = reset_with_voice_after(post)
    assert refusal == config.ALREADY_CONNECTED_MESSAGE
    assert sent == []


def test_a_refusal_after_the_reset_keeps_the_reset():
    def post(guild, ctx):
        ctx.author.voice = None

    guild, ctx, sent, refusal = reset_with_voice_after(post)
    assert refusal == config.USER_NOT_IN_VC_MESSAGE
    assert sent == []
    assert guild.events == []
