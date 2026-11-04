"""What a MusicButton click does - the player-controller buttons and
the d!search result buttons alike.

The click is acknowledged before any check runs: a pick acting in a
joining command's name can join voice through its join hook, and a slow
join must not push the response past Discord's 3-second deadline.
Refusals then reach the clicking user as an ephemeral followup, through
the bot's real Context.send.

Each test runs its coroutine through asyncio.run(): constructing a
discord.ui.Button item needs no loop, but the callbacks are async and
the dev group has no pytest-asyncio.
"""

from types import SimpleNamespace

import discord
import pytest

from config import config
from musicbot.audiocontroller import MusicButton
from musicbot.bot import Context
from musicbot.utils import CheckError, join_voice
from discord_fakes import FakeInteraction, VoiceGuild, make_bot
from helpers import run

DJ_ROLE = 99


class FakeContext(Context):
    """Enough of a component-click Context for the real
    Context.send and dj_check: no controller is registered, so a send
    goes to the interaction; the guild has a DJ role the author lacks
    and the author is neither an administrator nor the bot owner.
    The bot is out of voice and the author is in a voice channel."""

    def __init__(self):
        self.guild = VoiceGuild()
        self.author = SimpleNamespace(
            id=1,
            roles=[],
            voice=SimpleNamespace(channel=self.guild.channel("lobby")),
        )
        self.channel = SimpleNamespace(
            permissions_for=lambda member: SimpleNamespace(administrator=False)
        )
        self.bot = make_bot(
            settings={
                self.guild: SimpleNamespace(
                    dj_role=DJ_ROLE, user_must_be_in_vc=True
                )
            }
        )
        self.interaction = None


def click_interaction(custom_id="x"):
    """A click's interaction, and its context."""
    ctx = FakeContext()
    inter = FakeInteraction(ctx, custom_id)
    ctx.interaction = inter
    return inter, ctx


async def admit(ctx):
    return True


async def refuse(ctx):
    raise CheckError("Join a voice channel first.")


def sent_texts_and_kwargs(sent):
    return [(str(a[0]), k) for a, k in sent]


def test_the_click_is_deferred_before_the_check_runs():
    inter, ctx = click_interaction()
    deferred_at_check = []

    async def check(ctx):
        deferred_at_check.append(inter.deferred)
        return True

    async def action(ctx):
        pass

    run(MusicButton(action, check=check).callback(inter))
    assert deferred_at_check == [True]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), CheckError("from the action")],
    ids=["action failure", "check error from the action"],
)
def test_an_action_failure_is_reraised_not_refused(error):
    inter, ctx = click_interaction()

    def action(ctx):
        raise error

    try:
        run(MusicButton(action, check=admit).callback(inter))
    except Exception as e:
        assert e is error
    else:
        raise AssertionError("not re-raised")
    assert inter.followups == []


def test_a_refused_check_is_an_ephemeral_followup():
    inter, ctx = click_interaction()
    ran = []

    run(MusicButton(ran.append, check=refuse).callback(inter))
    assert sent_texts_and_kwargs(inter.followups) == [
        ("Join a voice channel first.", {"ephemeral": True})
    ]
    assert inter.messages == []
    assert ran == []


def test_a_refused_dj_check_on_a_controller_button_is_an_ephemeral_followup():
    inter, ctx = click_interaction(custom_id="next")
    ran = []

    run(MusicButton(ran.append, check=admit).callback(inter))
    assert [k for _, k in inter.followups] == [{"ephemeral": True}]
    assert inter.messages == []
    assert ran == []


def test_an_admitted_click_runs_the_buttons_action():
    inter, ctx = click_interaction()
    ran = []

    run(MusicButton(ran.append, check=admit).callback(inter))
    assert ran == [ctx]


def test_a_refused_dj_only_click_does_not_join_voice():
    inter, ctx = click_interaction(custom_id="next")

    run(MusicButton(lambda ctx: None, check=admit).callback(inter))
    assert sent_texts_and_kwargs(inter.followups) == [
        (config.NOT_A_DJ, {"ephemeral": True})
    ]
    assert ctx.guild.events == []


class PickButton(MusicButton):
    """A pick acting in a joining command's name, as a search pick
    does: it overrides the join hook to join voice."""

    async def join(self, ctx):
        await join_voice(ctx)


def test_an_admitted_click_on_a_pick_joins_before_its_action():
    inter, ctx = click_interaction()

    def action(ctx):
        ctx.guild.events.append("action")

    run(PickButton(action, check=admit).callback(inter))
    assert ctx.guild.events == [("connect", "lobby"), "action"]


def test_a_failed_join_is_an_ephemeral_followup_and_skips_the_action(
    capsys,
):
    inter, ctx = click_interaction()
    ctx.guild.fail = discord.ClientException("x")
    ran = []

    run(PickButton(ran.append, check=admit).callback(inter))
    assert sent_texts_and_kwargs(inter.followups) == [
        (config.VOICE_CONNECT_FAILED, {"ephemeral": True})
    ]
    assert ran == []
