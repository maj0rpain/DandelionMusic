"""Which Music and General commands need a DJ.

With a DJ role set, a command that changes shared playback, removes or
reorders what is queued, or changes saved playlists needs a
DJ; a read-only command, or one that only adds songs or brings the bot
in, does not. The table test pins the set; the behaviour tests show a
refused non-DJ leaves everything untouched.
"""

import inspect
import types

import pytest

from config import config
from musicbot.admission import RequestKind as K
from musicbot.commands.general import General
from musicbot.commands.music import Music
from musicbot.utils import CheckError
from discord_fakes import ADMISSION_DJ_ROLE, HERE, situation
from helpers import run

NEEDS_A_DJ = {
    # Music
    "loop",
    "shuffle",
    "pause",
    "stop",
    "move",
    "remove",
    "skip",
    "restore",
    "clear",
    "prev",
    "volume",
    "playlist save",
    "playlist remove",
    "playlist add_song",
    "playlist remove_song",
    "playlist move_song",
    # General
    "disconnect",
    "reset",
}


async def run_checks(cmd, ctx):
    """The command's own checks, in discord.py's order."""
    for check in cmd.checks:
        result = check(ctx)
        if inspect.isawaitable(result):
            await result


def refusal_of(cmd, ctx):
    """The CheckError message a command's own checks raise, or None."""
    try:
        run(run_checks(cmd, ctx))
    except CheckError as e:
        return str(e)
    return None


def test_the_commands_needing_a_dj_are_exactly_the_listed_ones(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PLAYLISTS", True)
    refused = set()
    for cog in (Music, General):
        for cmd in cog(None).walk_commands():
            _, ctx = situation(K.COMMAND, dj_role=ADMISSION_DJ_ROLE)
            ctx.audiocontroller = types.SimpleNamespace(is_active=lambda: True)
            if refusal_of(cmd, ctx) == config.NOT_A_DJ:
                refused.add(cmd.qualified_name)
    assert refused == NEEDS_A_DJ


def gated(name, role=None, dj_role=ADMISSION_DJ_ROLE):
    """(guild, ctx, calls, deferred) for General's command `name`."""
    guild, ctx = situation(
        K.JOINING_COMMAND if name == "reset" else K.COMMAND,
        bot=None if name == "reset" else HERE,
        author="b" if name == "reset" else "a",
        role=role,
        dj_role=dj_role,
    )
    cog = Music if name == "stop" else General
    ctx.command = {c.qualified_name: c for c in cog(None).walk_commands()}[
        name
    ]
    calls = []
    deferred = []
    sent = []

    async def send(content=None, **kwargs):
        sent.append(content)

    async def defer(**kwargs):
        deferred.append(True)

    async def udisconnect(reason):
        calls.append(("udisconnect", reason))
        return True

    async def dispose(reason):
        calls.append(("dispose", reason))

    controller = ctx.controllers[guild]
    controller.udisconnect = udisconnect
    controller.dispose = dispose
    controller.stop = lambda: calls.append(("stop",))
    ctx.audiocontroller = controller
    ctx.send = send
    ctx.defer = defer
    ctx.sent = sent
    return guild, ctx, calls, deferred


def invoke(name, ctx):
    """Check-then-invoke; the refusal message, or None."""
    cog = Music if name == "stop" else General
    cb = getattr(cog, f"_{name}").callback

    async def scenario():
        await run_checks(ctx.command, ctx)
        await cb(cog(None), ctx)

    try:
        run(scenario())
    except CheckError as e:
        return str(e)
    return None


@pytest.mark.parametrize("name", ["stop", "disconnect", "reset"])
def test_a_non_dj_is_refused_and_nothing_happens(name):
    guild, ctx, calls, deferred = gated(name)
    controller = ctx.controllers[guild]
    assert invoke(name, ctx) == config.NOT_A_DJ
    assert calls == []
    assert deferred == []
    assert ctx.sent == []
    assert ctx.bot.sessions.controller(guild) is controller
    assert guild.events == []


@pytest.mark.parametrize("name", ["stop", "disconnect", "reset"])
@pytest.mark.parametrize(
    "role, dj_role",
    [
        ("dj", ADMISSION_DJ_ROLE),
        ("admin", ADMISSION_DJ_ROLE),
        ("owner", ADMISSION_DJ_ROLE),
        (None, None),
    ],
    ids=["dj", "admin", "owner", "no-dj-role-set"],
)
def test_a_dj_admin_owner_or_anyone_without_a_dj_role_is_admitted(
    name, role, dj_role, no_sleep
):
    _, ctx, calls, _ = gated(name, role=role, dj_role=dj_role)
    ctx.guild.events.clear()
    assert invoke(name, ctx) is None
    assert calls
