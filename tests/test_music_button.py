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
from musicbot import admission
from musicbot.utils import CheckError
from musicbot.views import MusicButton, admit_click
from musicbot.admission import RequestKind
from musicbot.bot import Context
from discord_fakes import (
    ADMISSION_CHANNEL,
    ADMISSION_DJ_ROLE,
    FakeInteraction,
    VoiceGuild,
    admission_settings,
    make_bot,
)
from helpers import run


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
            id=ADMISSION_CHANNEL,
            permissions_for=lambda member: SimpleNamespace(
                administrator=False
            ),
        )
        self.settings = admission_settings(dj_role=ADMISSION_DJ_ROLE)
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


def test_after_is_awaited_with_the_clicks_ctx_after_the_action():
    inter, ctx = click_interaction()
    events = []

    async def after(c):
        events.append(("after", c))

    run(
        MusicButton(lambda c: events.append("action"), after=after).callback(
            inter
        )
    )
    assert events == ["action", ("after", ctx)]


def test_a_refused_click_runs_no_after():
    inter, ctx = click_interaction()
    ctx.out_of_voice()
    ran = []

    async def after(c):
        ran.append(c)

    run(MusicButton(lambda c: None, after=after).callback(inter))
    assert ran == []


def test_a_button_without_after_does_nothing_after_its_action():
    inter, ctx = click_interaction()

    run(MusicButton(lambda c: None).callback(inter))
    assert inter.followups == []


def test_an_exception_from_after_reaches_on_failure():
    inter, ctx = click_interaction()
    error = RuntimeError("after failed")
    seen = []

    async def after(c):
        raise error

    class Button(MusicButton):
        async def on_failure(self, ctx, err):
            seen.append(err)

    run(Button(lambda c: None, after=after).callback(inter))
    assert seen == [error]


def test_a_failed_defer_is_reported_with_the_clicks_ctx_and_no_check(
    monkeypatch,
):
    inter, ctx = click_interaction()
    error = RuntimeError("defer failed")
    checked = []
    seen = []

    async def defer(**kwargs):
        raise error

    async def check(ctx, kind):
        checked.append(kind)

    inter.response.defer = defer
    monkeypatch.setattr(admission, "check", check)

    class Button(MusicButton):
        async def on_failure(self, ctx, err):
            seen.append((ctx, err))

    run(Button(lambda c: None).callback(inter))
    assert (seen, checked) == ([(ctx, error)], [])


# --- admit_click -------------------------------------------------------


def _recording_check(monkeypatch, controller=None, refuse=None):
    calls = []
    events = []

    async def check(ctx, kind):
        calls.append((ctx, kind))
        if refuse is not None:
            raise refuse

        async def enter():
            events.append("enter")
            return controller

        return SimpleNamespace(enter=enter)

    monkeypatch.setattr(admission, "check", check)
    return calls, events


def test_admit_click_checks_the_ctx_and_kind_it_is_given(monkeypatch):
    inter, ctx = click_interaction()
    calls, _ = _recording_check(monkeypatch)

    run(admit_click(inter, RequestKind.PICK))
    assert calls == [(ctx, RequestKind.PICK)]


def test_admit_click_enters_after_an_admit_and_returns_the_controller(
    monkeypatch,
):
    inter, ctx = click_interaction()
    controller = object()
    _, events = _recording_check(monkeypatch, controller)

    result = run(admit_click(inter, RequestKind.PICK))
    assert (result, events, ctx.audiocontroller) == (
        (ctx, controller),
        ["enter"],
        controller,
    )


def test_admit_click_raises_a_refusal_and_makes_no_join(monkeypatch):
    inter, ctx = click_interaction()
    _, events = _recording_check(monkeypatch, refuse=CheckError("no"))

    with pytest.raises(CheckError):
        run(admit_click(inter, RequestKind.PICK))
    assert events == []


def test_admit_click_neither_acknowledges_nor_replies(monkeypatch):
    inter, ctx = click_interaction()
    _recording_check(monkeypatch, refuse=CheckError("no"))

    with pytest.raises(CheckError):
        run(admit_click(inter, RequestKind.PICK))
    assert (inter.deferred, inter.messages, inter.followups) == (
        False,
        [],
        [],
    )


def test_admit_click_uses_a_passed_ctx_without_building_one(monkeypatch):
    inter, built = click_interaction()
    other = SimpleNamespace()
    controller = object()
    built_calls = []

    async def get_context(i):
        built_calls.append(i)
        return built

    inter.client.get_context = get_context
    calls, _ = _recording_check(monkeypatch, controller)

    result = run(admit_click(inter, RequestKind.PICK, ctx=other))
    assert (result, [c for c, _ in calls], built_calls) == (
        (other, controller),
        [other],
        [],
    )
    assert other.audiocontroller is controller
