"""General's voice commands go through request admission: the command
channel and user_must_be_in_vc rules apply, and d!reset admits twice."""

import asyncio

import pytest
from discord.ext import commands

from config import config
from musicbot.admission import RequestKind as K
from musicbot.commands.general import General
from discord_fakes import (
    ADMISSION_DJ_ROLE,
    BOTS_ONLY,
    HERE,
    PEOPLE,
    check_error_message,
    command_of,
    situation,
)


def prepare(name, kind, **kw):
    """(guild, ctx, sent, disconnects) for running General's command
    `name` with the controller and replies recorded."""
    guild, ctx = situation(kind, **kw)
    ctx.command = command_of(General, name)
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
    return check_error_message(callback(General(None), ctx))


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
    disposed = []

    async def dispose(reason):
        disposed.append(reason)

    ctx.bot.sessions.controller(guild).dispose = dispose
    sessions = ctx.bot.sessions
    real = sessions.reset

    async def reset(g, reason):
        result = await real(g, reason)
        post(guild, ctx)
        return result

    sessions.reset = reset
    return guild, ctx, sent, disposed, invoke("reset", ctx)


def test_reset_joins_from_the_post_reset_voice_state():
    guild, ctx, sent, _, refusal = reset_with_voice_after(lambda g, c: None)
    assert refusal is None
    assert guild.events == [("connect", "b")]
    assert sent == [":white_check_mark: Connected to b"]


def test_reset_still_refuses_when_the_bot_is_in_voice_after_it(no_sleep):

    def post(guild, ctx):
        guild.put_bot_in(guild.channel("a", humans=1))

    guild, _, sent, _, refusal = reset_with_voice_after(post)
    assert refusal == config.ALREADY_CONNECTED_MESSAGE
    assert sent == []


def test_a_refusal_after_the_reset_keeps_the_reset():
    def post(guild, ctx):
        ctx.author.voice = None

    guild, ctx, sent, disposed, refusal = reset_with_voice_after(post)
    assert refusal == config.USER_NOT_IN_VC_MESSAGE
    assert sent == []
    assert guild.events == []
    assert disposed == ["reset command"]


def connect(fail=None, **kw):
    """(refusal, sent, events, order) for d!connect in a situation:
    `order` records the context's defer and sends in the order they
    happened."""
    guild, ctx, sent, _ = prepare("connect", K.JOINING_COMMAND, **kw)
    if fail is not None:
        guild.fail = fail
    order = []
    send = ctx.send

    async def defer(**kwargs):
        order.append("defer")

    async def recording_send(content, **kwargs):
        order.append("send")
        await send(content, **kwargs)

    ctx.defer = defer
    ctx.send = recording_send
    refusal = invoke("connect", ctx)
    return refusal, sent, guild.events, order


@pytest.mark.parametrize("rule", [True, False])
def test_connect_out_of_voice_says_connected(rule):
    refusal, sent, events, order = connect(rule=rule)
    assert refusal is None
    assert sent == ["Connected."]
    assert events == [("connect", "b")]
    assert order == ["defer", "send"]


@pytest.mark.parametrize("rule", [True, False])
def test_connect_out_of_a_bots_only_channel_says_moved(rule):
    refusal, sent, events, order = connect(bot=BOTS_ONLY, rule=rule)
    assert refusal is None
    assert sent == ["Moved to your voice channel."]
    assert events == [("move", "b")]
    assert order == ["defer", "send"]


@pytest.mark.parametrize("rule", [True, False])
@pytest.mark.parametrize(
    "spec",
    [
        {"bot": HERE, "author": "a"},
        {"bot": PEOPLE, "author": "b", "role": "admin"},
        {"bot": PEOPLE, "author": "b", "role": "owner"},
        {
            "bot": PEOPLE,
            "author": "b",
            "role": "dj",
            "dj_role": ADMISSION_DJ_ROLE,
        },
        {"bot": BOTS_ONLY, "author": None, "role": "admin"},
    ],
    ids=["with the bot", "admin", "owner", "dj", "not in voice"],
)
def test_connect_with_nothing_to_do_is_refused_as_already_connected(
    rule, spec
):
    refusal, sent, events, order = connect(rule=rule, **spec)
    assert refusal == config.ALREADY_CONNECTED_MESSAGE
    assert sent == []
    assert events == []
    assert order == ["defer"]


@pytest.mark.parametrize("rule", [True, False])
@pytest.mark.parametrize("bot", [None, BOTS_ONLY], ids=["connect", "move"])
def test_a_failed_connect_or_move_is_refused_after_the_defer(
    rule, bot, no_sleep
):
    refusal, sent, _, order = connect(
        fail=asyncio.TimeoutError(), bot=bot, rule=rule
    )
    assert refusal == config.VOICE_CONNECT_FAILED
    assert sent == []
    assert order == ["defer"]


def test_reset_recovers_a_disconnecting_controller():
    guild, ctx, sent, _ = prepare(
        "reset", K.JOINING_COMMAND, bot=None, author="b"
    )
    controller = ctx.bot.sessions.controller(guild)
    controller.is_disconnecting = lambda: True

    async def dispose(reason):
        # the fresh controller is not disconnecting
        controller.is_disconnecting = lambda: False

    controller.dispose = dispose
    assert invoke("reset", ctx) is None
    assert guild.events == [("connect", "b")]


def test_connect_while_disconnecting_is_refused():
    guild, ctx, sent, _ = prepare(
        "connect", K.JOINING_COMMAND, bot=None, author="b"
    )
    ctx.bot.sessions.controller(guild).is_disconnecting = lambda: True
    assert invoke("connect", ctx) == config.DISCONNECTING_MESSAGE
    assert guild.events == []
