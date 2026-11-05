"""What a MusicButton click does - the player-controller buttons and
the d!search result buttons alike.

The click is acknowledged before admission runs: a pick acting in a
joining command's name joins voice in `enter()`, and a slow join must
not push the response past Discord's 3-second deadline. Refusals then
reach the clicking user as an ephemeral followup, through the bot's
real Context.send.

Each test runs its coroutine through asyncio.run(): constructing a
discord.ui.Button item needs no loop, but the callbacks are async and
the dev group has no pytest-asyncio.
"""

from types import SimpleNamespace

import discord
import pytest

from config import config
from musicbot.audiocontroller import MusicButton
from musicbot.admission import RequestKind
from musicbot.bot import Context
from discord_fakes import FakeInteraction, VoiceGuild, make_bot
from helpers import run

DJ_ROLE = 99
CHANNEL = 7


class FakeContext(Context):
    """Enough of a component-click Context for the real
    Context.send and admission: the guild has a DJ role the author
    lacks and the author is neither an administrator nor the bot
    owner. The bot is in the author's voice channel; `out_of_voice()`
    takes it out."""

    def __init__(self):
        self.guild = VoiceGuild()
        lobby = self.guild.channel("lobby", humans=1)
        self.guild.put_bot_in(lobby)
        self.author = SimpleNamespace(
            id=1, roles=[], voice=SimpleNamespace(channel=lobby)
        )
        self.channel = SimpleNamespace(
            id=CHANNEL,
            permissions_for=lambda member: SimpleNamespace(
                administrator=False
            ),
        )
        self.settings = SimpleNamespace(
            dj_role=DJ_ROLE,
            user_must_be_in_vc=True,
            command_channel=None,
            vc_timeout=True,
        )
        self.bot = make_bot(
            {
                self.guild: SimpleNamespace(
                    name="current", command_channel=None
                )
            },
            settings={self.guild: self.settings},
        )
        self.interaction = None

    def out_of_voice(self):
        self.guild.voice_client = None


def click_interaction(custom_id="x"):
    """A click's interaction, and its context."""
    ctx = FakeContext()
    inter = FakeInteraction(ctx, custom_id)
    ctx.interaction = inter
    return inter, ctx


def sent_texts_and_kwargs(sent):
    return [(str(a[0]), k) for a, k in sent]


def pick(action):
    return MusicButton(action, RequestKind.PICK)


def test_the_click_is_deferred_before_the_join():
    inter, ctx = click_interaction()
    ctx.out_of_voice()
    real_defer = inter.response.defer

    async def defer(**kwargs):
        ctx.guild.events.append("defer")
        await real_defer(**kwargs)

    inter.response.defer = defer

    run(pick(lambda ctx: None).callback(inter))
    assert ctx.guild.events == ["defer", ("connect", "lobby")]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), discord.ClientException("from the action")],
    ids=["action failure", "client exception from the action"],
)
def test_an_action_failure_is_reraised_not_refused(error):
    inter, ctx = click_interaction()

    def action(ctx):
        raise error

    try:
        run(MusicButton(action).callback(inter))
    except Exception as e:
        assert e is error
    else:
        raise AssertionError("not re-raised")
    assert inter.followups == []


def test_a_player_button_is_refused_while_the_bot_is_out_of_voice():
    inter, ctx = click_interaction()
    ctx.out_of_voice()
    ran = []

    run(MusicButton(ran.append).callback(inter))
    assert sent_texts_and_kwargs(inter.followups) == [
        (config.NOT_CONNECTED_MESSAGE, {"ephemeral": True})
    ]
    assert inter.messages == []
    assert ran == []
    assert ctx.guild.events == []


def test_a_dj_only_button_is_refused_to_a_non_dj():
    inter, ctx = click_interaction(custom_id="next")
    ran = []

    run(MusicButton(ran.append).callback(inter))
    assert sent_texts_and_kwargs(inter.followups) == [
        (config.NOT_A_DJ, {"ephemeral": True})
    ]
    assert inter.messages == []
    assert ran == []
    assert ctx.guild.events == []


def test_an_admitted_click_runs_the_buttons_action():
    inter, ctx = click_interaction()
    ran = []

    run(MusicButton(ran.append).callback(inter))
    assert ran == [ctx]


def test_the_action_runs_on_the_guilds_current_controller():
    inter, ctx = click_interaction()
    seen = []

    run(
        MusicButton(lambda ctx: seen.append(ctx.audiocontroller)).callback(
            inter
        )
    )
    assert [c.name for c in seen] == ["current"]


def test_an_admitted_click_on_a_pick_joins_before_its_action():
    inter, ctx = click_interaction()
    ctx.out_of_voice()

    def action(ctx):
        ctx.guild.events.append("action")

    run(pick(action).callback(inter))
    assert ctx.guild.events == [("connect", "lobby"), "action"]


def test_a_failed_join_is_an_ephemeral_followup_and_skips_the_action(
    capsys,
):
    inter, ctx = click_interaction()
    ctx.out_of_voice()
    ctx.guild.fail = discord.ClientException("x")
    ran = []

    run(pick(ran.append).callback(inter))
    assert sent_texts_and_kwargs(inter.followups) == [
        (config.VOICE_CONNECT_FAILED, {"ephemeral": True})
    ]
    assert ran == []
