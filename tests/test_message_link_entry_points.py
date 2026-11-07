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
from discord_fakes import VoiceGuild, admission_settings, make_bot
from helpers import run
from message_link_fakes import (
    FOREIGN_LINK,
    GUILD_ID,
    LINK,
    LinkChannel,
    LinkMember,
    perms,
)

COMMANDS = ["_play", "_play_next"]


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
    assert loaded(stub_loader) == [(LINK,)]


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
    assert loaded(stub_loader) == [(LINK,)]


def test_a_play_message_with_its_own_attachment_loads(
    controller, stub_loader, monkeypatch
):
    stub_load_and_channel(controller, stub_loader, monkeypatch)
    invoke("_play", controller, own_message=message(LINK))
    assert loaded(stub_loader) == [(LINK,)]


def add_song(controller, stub_loader, monkeypatch, link):
    stub_load_and_channel(controller, stub_loader, monkeypatch)
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


def test_playlist_add_song_saves_an_allowed_link(
    controller, stub_loader, monkeypatch
):
    sent, saved, loads = add_song(controller, stub_loader, monkeypatch, LINK)
    assert loads == [LINK]
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


def react(link, monkeypatch):
    loads = []

    async def load_song(track):
        loads.append(track)
        raise loader.SongError("loaded")

    monkeypatch.setattr(loader, "load_song", load_song)
    sent = []
    msg = types.SimpleNamespace(content=link)

    class Channel(LinkChannel):
        async def fetch_message(self, _id):
            return msg

    channel = Channel(2, perms())

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
                user_must_be_in_vc=True,
                dj_role=None,
                vc_timeout=True,
            )
        },
    )
    bot.get_guild = lambda _id: guild

    async def send(content=None, **kwargs):
        sent.append(content)

    async def get_context(_message):
        return types.SimpleNamespace(
            send=send, bot=bot, guild=guild, command=None, author=None
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
    return loads, sent


def test_the_plugin_refuses_a_forbidden_link_and_does_not_load(monkeypatch):
    loads, sent = react(FOREIGN_LINK, monkeypatch)
    assert sent == [config.MESSAGE_LINK_FORBIDDEN]
    assert loads == []


def test_the_plugin_loads_an_allowed_link(monkeypatch):
    loads, _sent = react(LINK, monkeypatch)
    assert loads == [LINK]
