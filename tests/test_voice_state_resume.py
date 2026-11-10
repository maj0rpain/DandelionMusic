"""on_voice_state_update on a real MusicBot: a move while playing
resumes the voice client it read, and only while it is still the
guild's."""

import types


from musicbot.bot import MusicBot
from discord_fakes import VoiceGuild, real_music_bot
from helpers import run


class ResumingClient:
    def __init__(self, playing=True):
        self.playing = playing
        self.resumed = 0
        self.touched = 0

    def is_playing(self):
        self.touched += 1
        return self.playing

    def resume(self):
        self.touched += 1
        self.resumed += 1


class IdleTimerController:
    def __init__(self, on_timer=None):
        self.timers = []
        self.disconnects = []
        self.on_timer = on_timer

    async def start_idle_timer(self, playing):
        self.timers.append(playing)
        if self.on_timer:
            self.on_timer()

    async def udisconnect(self, reason):
        self.disconnects.append(reason)


def move(monkeypatch, no_sleep, client, on_wait=None, on_timer=None):
    """Drive a move of the bot; `on_wait` runs during the one-second
    wait, fired through `no_sleep`'s hook. Asserts the sleep count: one
    when the client plays, zero when it does not (a move waits only
    when playing), so an added sleep fails the test. Returns
    (guild, controller)."""
    guild = VoiceGuild()
    guild.voice_client = client
    controller = IdleTimerController(
        (lambda: on_timer(guild)) if on_timer else None
    )
    bot = real_music_bot()
    me = types.SimpleNamespace(guild=guild)
    monkeypatch.setattr(MusicBot, "user", property(lambda self: me))
    monkeypatch.setattr(bot.sessions, "controller", lambda g: controller)

    if on_wait:
        no_sleep.hook = lambda: on_wait(guild)
    run(bot.on_voice_state_update(me, None, None))
    assert no_sleep.count == (1 if client.playing else 0)
    return guild, controller


def clear(guild):
    guild.voice_client = None


def test_move_while_playing_resumes_the_same_client(monkeypatch, no_sleep):
    client = ResumingClient(playing=True)
    guild, controller = move(monkeypatch, no_sleep, client)
    assert controller.timers == [True]
    assert client.resumed == 1
    assert guild.voice_client is client


def test_move_while_not_playing_resumes_nothing(monkeypatch, no_sleep):
    client = ResumingClient(playing=False)
    _, controller = move(monkeypatch, no_sleep, client)
    assert controller.timers == [False]
    assert client.resumed == 0


def test_cleared_during_the_wait_resumes_nothing(monkeypatch, no_sleep):
    client = ResumingClient()
    _, controller = move(monkeypatch, no_sleep, client, on_wait=clear)
    assert client.resumed == 0
    assert controller.disconnects == []


def test_cleared_while_the_idle_timer_runs_resumes_nothing(
    monkeypatch, no_sleep
):
    client = ResumingClient()
    _, controller = move(monkeypatch, no_sleep, client, on_timer=clear)
    assert client.resumed == 0
    assert controller.disconnects == []


def test_replaced_during_the_wait_leaves_both_clients_alone(
    monkeypatch, no_sleep
):
    old, new = ResumingClient(), ResumingClient()

    def replace(guild):
        guild.voice_client = new

    _, controller = move(monkeypatch, no_sleep, old, on_wait=replace)
    assert old.resumed == 0
    assert new.touched == 0
    assert controller.disconnects == []
