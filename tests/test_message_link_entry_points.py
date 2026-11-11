"""Each entry point that reaches `AudioController.process_song` refuses a
forbidden message link with `MESSAGE_LINK_FORBIDDEN` and never loads it;
an allowed link still loads."""

import types

import pytest

from config import config
from musicbot import loader, playlists
from musicbot.commands import music
from musicbot.commands.music import Music
from musicbot.plugins.button import Button
from musicbot.song import LoadResult
from controller_helpers import (
    _fake_ffmpeg,
    _no_idle_timer,
    _run,
    _song,
    command_ctx,
)
from discord_fakes import (
    ReactionEffects,
    VoiceGuild,
    admission_settings,
    make_bot,
)
from helpers import run
from message_link_fakes import (
    FOREIGN_LINK,
    GUILD_ID,
    LINK,
    LinkChannel,
    LinkMember,
    CountingThread,
    admitted_pair,
    perms,
)

COMMANDS = ["_play", "_play_next"]


def admitted(checked):
    """The (track, admitted link) of each checked track, one or in a
    1-tuple of load arguments."""
    flat = [c[0] if isinstance(c, tuple) else c for c in checked]
    return [admitted_pair(c) for c in flat]


def loaded(stub_loader):
    return [a for name, a in stub_loader.calls if name == "load_song"]


def stub_load_and_channel(controller, stub_loader, monkeypatch):
    monkeypatch.setattr(music, "SongError", stub_loader.SongError)
    _fake_ffmpeg(monkeypatch)
    _no_idle_timer(controller, monkeypatch)
    stub_loader.results["load_song"] = LoadResult.of_song(
        _song(controller, "a")
    )
    controller.guild.get_channel_or_thread = lambda _id: LinkChannel(
        permissions=perms()
    )


def invoke(name, controller, track=None, own_message=None):
    ctx, sent = command_ctx(controller)
    ctx.author = LinkMember(guild=controller.guild)
    ctx.interaction = None
    ctx.message = own_message or types.SimpleNamespace(
        attachments=[], reference=None
    )
    command = getattr(Music, name)
    _run(
        controller,
        lambda: command.callback(Music(None), ctx, track=track),
    )
    return sent


@pytest.mark.parametrize("name", COMMANDS)
def test_a_forbidden_link_is_refused_and_not_loaded(
    name, controller, stub_loader, monkeypatch
):
    stub_load_and_channel(controller, stub_loader, monkeypatch)
    sent = invoke(name, controller, FOREIGN_LINK)
    assert sent == [config.MESSAGE_LINK_FORBIDDEN]
    assert loaded(stub_loader) == []


@pytest.mark.parametrize("name", COMMANDS)
def test_an_allowed_link_loads(name, controller, stub_loader, monkeypatch):
    stub_load_and_channel(controller, stub_loader, monkeypatch)
    invoke(name, controller, LINK)
    assert admitted(loaded(stub_loader)) == [(LINK, LINK)]


def test_the_gate_runs_once_per_play_request(
    controller, stub_loader, monkeypatch
):
    stub_load_and_channel(controller, stub_loader, monkeypatch)
    lookups = []

    thread = CountingThread(lookups, private=True)
    controller.guild.get_channel_or_thread = lambda _id: thread
    invoke("_play", controller, LINK)
    assert admitted(loaded(stub_loader)) == [(LINK, LINK)]
    assert len(lookups) == 1


def message(jump_url, attachments=True, reference=None):
    return types.SimpleNamespace(
        attachments=[types.SimpleNamespace(url="cdn")] if attachments else [],
        reference=reference,
        jump_url=jump_url,
    )


def reply_to(jump_url):
    return types.SimpleNamespace(resolved=message(jump_url), message_id=1)


def test_replying_to_a_message_in_another_guild_is_refused(
    controller, stub_loader, monkeypatch
):
    stub_load_and_channel(controller, stub_loader, monkeypatch)
    own = message(
        "unused", attachments=False, reference=reply_to(FOREIGN_LINK)
    )
    sent = invoke("_play", controller, own_message=own)
    assert sent == [config.MESSAGE_LINK_FORBIDDEN]
    assert loaded(stub_loader) == []


def test_replying_to_a_readable_message_in_the_guild_loads(
    controller, stub_loader, monkeypatch
):
    stub_load_and_channel(controller, stub_loader, monkeypatch)
    own = message("unused", attachments=False, reference=reply_to(LINK))
    invoke("_play", controller, own_message=own)
    assert admitted(loaded(stub_loader)) == [(LINK, LINK)]


def test_a_play_message_with_its_own_attachment_loads(
    controller, stub_loader, monkeypatch
):
    stub_load_and_channel(controller, stub_loader, monkeypatch)
    invoke("_play", controller, own_message=message(LINK))
    assert admitted(loaded(stub_loader)) == [(LINK, LINK)]


def add_song(controller, stub_loader, monkeypatch, link, channel=None):
    stub_load_and_channel(controller, stub_loader, monkeypatch)
    if channel is not None:
        controller.guild.get_channel_or_thread = lambda _id: channel
    saved = []
    loads = []

    async def load_song(track):
        loads.append(track)
        return LoadResult.of_song(_song(controller, "a"))

    monkeypatch.setattr(music.loader, "load_song", load_song)

    async def add_songs(_factory, _ref, entries):
        saved.extend(entries)

    monkeypatch.setattr(playlists, "add_songs", add_songs)
    ctx, sent = command_ctx(controller)
    ctx.author = LinkMember(guild=controller.guild)
    _run(
        controller,
        lambda: Music._playlist_add_song.callback(Music(None), ctx, "p", link),
    )
    return sent, saved, loads


def test_playlist_add_song_refuses_a_forbidden_link_and_saves_nothing(
    controller, stub_loader, monkeypatch
):
    sent, saved, loads = add_song(
        controller, stub_loader, monkeypatch, FOREIGN_LINK
    )
    assert sent == [config.MESSAGE_LINK_FORBIDDEN]
    assert loads == []
    assert saved == []


def test_the_gate_runs_once_per_playlist_add_song(
    controller, stub_loader, monkeypatch
):
    lookups = []

    _sent, saved, loads = add_song(
        controller,
        stub_loader,
        monkeypatch,
        LINK,
        channel=CountingThread(lookups, private=True),
    )
    assert admitted(loads) == [(LINK, LINK)]
    assert len(saved) == 1
    assert len(lookups) == 1


def test_playlist_add_song_saves_an_allowed_link(
    controller, stub_loader, monkeypatch
):
    sent, saved, loads = add_song(controller, stub_loader, monkeypatch, LINK)
    assert admitted(loads) == [(LINK, LINK)]
    assert len(saved) == 1
    assert sent == [config.PLAYLIST_UPDATED]


class PluginController:
    """The real `AudioController.process_song`, over a guild-and-load
    stand-in: nothing else of the controller is reached by a link that
    is refused or whose load fails."""

    def is_disconnecting(self):
        return False

    def __init__(self, guild):
        from musicbot.audiocontroller import AudioController

        self.guild = guild
        self._process = AudioController.process_song

    async def process_song(self, url, user=None):
        return await self._process(self, url, user=user)


def react(link, monkeypatch, content=None, command_channel=None, channel=None):
    loads = []

    async def load_song(track):
        loads.append(track)
        raise loader.SongError("loaded")

    monkeypatch.setattr(loader, "load_song", load_song)
    sent = []
    msg = types.SimpleNamespace(content=content or link)

    channel = channel or LinkChannel(channel_id=2)

    async def fetch_message(_id):
        return msg

    channel.fetch_message = fetch_message

    class Guild(VoiceGuild):
        name = "guild"

        def __init__(self):
            super().__init__()
            self.id = GUILD_ID

        def get_channel_or_thread(self, channel_id):
            return channel

    guild = Guild()
    mine = guild.channel("mine", humans=1)
    controller = PluginController(guild)
    bot = make_bot(
        {guild: controller},
        settings={
            guild: admission_settings(
                button_emote="x",
                command_channel=command_channel,
                user_must_be_in_vc=True,
                dj_role=None,
                vc_timeout=True,
            )
        },
    )
    bot.get_guild = lambda _id: guild

    async def send(content=None, **kwargs):
        sent.append(content)
        guild.events.append(("reply", content))

    async def get_context(_message):
        return types.SimpleNamespace(
            send=send,
            bot=bot,
            guild=guild,
            command=None,
            author=None,
            channel=types.SimpleNamespace(id=2),
        )

    bot.get_context = get_context
    member = LinkMember(
        roles=[], voice=types.SimpleNamespace(channel=mine), guild=guild
    )
    event = types.SimpleNamespace(
        guild_id=GUILD_ID,
        channel_id=2,
        message_id=3,
        member=member,
        emoji=types.SimpleNamespace(name="x", id=None),
    )
    run(Button(bot).on_raw_reaction_add(event))
    directed = bot.sessions.panel(guild).directed
    return ReactionEffects(loads, sent, guild.events, directed)


def test_the_plugin_refuses_a_forbidden_link_and_does_not_load(monkeypatch):
    seen = react(FOREIGN_LINK, monkeypatch, command_channel=2)
    assert (seen.processed, seen.directed) == ([], [])
    assert seen.events == [("reply", config.MESSAGE_LINK_FORBIDDEN)]


def test_one_forbidden_link_among_several_reactions_still_queues_the_rest(
    monkeypatch,
):
    other = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    foreign2 = FOREIGN_LINK.replace("/5", "/6")
    seen = react(
        FOREIGN_LINK,
        monkeypatch,
        content=f"{FOREIGN_LINK} {other} {foreign2}",
    )
    assert [c.track for c in seen.processed] == [other]
    assert seen.sent == [config.MESSAGE_LINK_FORBIDDEN] * 2 + ["loaded"]
    connect = next(i for i, e in enumerate(seen.events) if e[0] == "connect")
    refusals = [
        i
        for i, e in enumerate(seen.events)
        if e == ("reply", config.MESSAGE_LINK_FORBIDDEN)
    ]
    assert len(refusals) == 2 and max(refusals) < connect


def test_the_gate_runs_once_per_link_in_a_reaction(monkeypatch):
    other = LINK.replace("/5", "/6")
    lookups = []
    seen = react(
        LINK,
        monkeypatch,
        content=f"{LINK} {other}",
        channel=CountingThread(lookups, channel_id=2, private=True),
    )
    assert admitted(seen.processed) == [(LINK, LINK), (other, other)]
    assert len(lookups) == 2


def test_the_plugin_loads_an_allowed_link(monkeypatch):
    seen = react(LINK, monkeypatch, command_channel=2)
    assert admitted(seen.processed) == [(LINK, LINK)]
    assert len(seen.directed) == 1


def test_a_reaction_refused_by_admission_sends_no_message_link_reply(
    monkeypatch,
):
    seen = react(FOREIGN_LINK, monkeypatch, command_channel=3)
    assert seen == ReactionEffects([], [], [], [])
