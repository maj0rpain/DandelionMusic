"""What a MusicButton click does - the player-controller buttons and
the d!search result buttons alike.

The click is acknowledged before any check runs: a check can join
voice, and a slow join must not push the response past Discord's
3-second deadline. Refusals then reach the clicking user as an
ephemeral followup, through the bot's real Context.send.

Each test runs its coroutine through asyncio.run(): constructing a
discord.ui.Button item needs no loop, but the callbacks are async and
the dev group has no pytest-asyncio.
"""

import asyncio
from types import SimpleNamespace

from musicbot.audiocontroller import MusicButton
from musicbot.bot import Context
from musicbot.utils import CheckError

DJ_ROLE = 99


class FakeContext:
    """Enough of a component-click Context for the real
    Context.send and dj_check: no controller is registered, so a send
    goes to the interaction; the guild has a DJ role the author lacks
    and the author is neither an administrator nor the bot owner."""

    def __init__(self, interaction):
        self.interaction = interaction
        self.guild = interaction.guild
        self.author = SimpleNamespace(id=1, roles=[])
        self.channel = SimpleNamespace(
            permissions_for=lambda member: SimpleNamespace(administrator=False)
        )

        async def is_owner(user):
            return False

        self.bot = SimpleNamespace(
            is_owner=is_owner,
            sessions=SimpleNamespace(
                controller=lambda guild: None,
                settings=lambda guild: SimpleNamespace(dj_role=DJ_ROLE),
            ),
        )

    async def send(self, *args, **kwargs):
        return await Context.send(self, *args, **kwargs)


class FakeInteraction:
    """Records the direct response and every followup."""

    def __init__(self, custom_id="x"):
        self.guild = SimpleNamespace(id=1)
        self.user = SimpleNamespace(id=1)
        self.data = {"custom_id": custom_id}
        self.deferred = False
        self.messages = []
        self.followups = []
        self.ctx = FakeContext(self)

        async def send_message(*args, **kwargs):
            self.messages.append((args, kwargs))

        async def defer(**kwargs):
            self.deferred = True

        self.response = SimpleNamespace(
            send_message=send_message,
            defer=defer,
            is_done=lambda: self.deferred or bool(self.messages),
        )

        async def followup_send(*args, **kwargs):
            self.followups.append((args, kwargs))

        self.followup = SimpleNamespace(send=followup_send)

        async def get_context(inter):
            return self.ctx

        self.client = SimpleNamespace(
            get_context=get_context,
            sessions=self.ctx.bot.sessions,
        )


def run(coro):
    return asyncio.run(coro)


async def admit(ctx):
    return True


async def refuse(ctx):
    raise CheckError("Join a voice channel first.")


def as_text(sent):
    return [(str(a[0]), k) for a, k in sent]


def test_the_click_is_deferred_before_the_check_runs():
    inter = FakeInteraction()
    deferred_at_check = []

    async def check(ctx):
        deferred_at_check.append(inter.deferred)
        return True

    async def action(ctx):
        pass

    run(MusicButton(action, check=check).callback(inter))
    assert deferred_at_check == [True]


def test_a_refused_check_is_an_ephemeral_followup():
    inter = FakeInteraction()
    ran = []

    run(MusicButton(ran.append, check=refuse).callback(inter))
    assert as_text(inter.followups) == [
        ("Join a voice channel first.", {"ephemeral": True})
    ]
    assert inter.messages == []
    assert ran == []


def test_a_refused_dj_check_on_a_controller_button_is_an_ephemeral_followup():
    inter = FakeInteraction(custom_id="next")
    ran = []

    run(MusicButton(ran.append, check=admit).callback(inter))
    assert [k for _, k in inter.followups] == [{"ephemeral": True}]
    assert inter.messages == []
    assert ran == []


def test_an_admitted_click_runs_the_buttons_action():
    inter = FakeInteraction()
    ran = []

    run(MusicButton(ran.append, check=admit).callback(inter))
    assert ran == [inter.ctx]
