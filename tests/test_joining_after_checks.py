"""A Music joining command joins voice only once its own checks on its
input have passed.

Seam: the Music cog's hooks in discord.py's order (`invoke_music_hooks`),
then the command callback, over the voice fakes. What is asserted is what
was sent and whether the voice fakes recorded a connect or a move.
"""

import contextlib
import types

import pytest

from config import config
from musicbot import admission
from musicbot.admission import RequestKind as K
from musicbot.audiocontroller import RestoreResult
from musicbot.commands import music
from musicbot.commands.music import Music
from musicbot.loader import SongError
from musicbot.outcome import EnqueueOutcome, OutcomeKind
from musicbot.utils import CheckError
from discord_fakes import BOTS_ONLY, invoke_music_hooks, situation
from helpers import run


class RecordingController:
    """The controller as the commands see it: it records what they ask
    of it. `backup` is what load_backup() loads into the queue."""

    def __init__(self, backup=(), history=(), active=False, readable=True):
        self.name = "controller"
        self.backup = list(backup)
        self.queue = []
        self.history = list(history)
        self.active = active
        self.readable = readable
        self.calls = []
        self.playlist = types.SimpleNamespace(
            has_prev=lambda: bool(self.history)
        )

    def load_backup(self):
        if self.active:
            return RestoreResult.REFUSED_WHILE_ACTIVE
        if not self.readable:
            return RestoreResult.BACKUP_UNREADABLE
        self.queue = list(self.backup)
        if not self.queue:
            return RestoreResult.NOTHING_TO_RESTORE
        return RestoreResult.READY

    async def start_restored(self):
        if not self.queue:
            return RestoreResult.NOTHING_TO_RESTORE
        self.calls.append("start_restored")
        return RestoreResult.RESTORED

    def prev_song(self):
        self.calls.append("prev_song")
        return True

    async def start_idle_timer(self, reset):
        pass

    async def process_song(self, track, user=None, playnext=False):
        self.calls.append(("process_song", track, playnext))
        return EnqueueOutcome(OutcomeKind.START_FAILED)

    async def enqueue(self, songs, batch=None):
        self.calls.append(("enqueue", [s.webpage_url for s in songs]))
        return EnqueueOutcome(OutcomeKind.START_FAILED)


def body_ctx(name, controller=None, **spec):
    """(guild, ctx, cog, sent): a context for Music command `name`, whose
    guild controller is `controller`."""
    guild, ctx = situation(K.COMMAND, **spec)
    cog = Music(None)
    ctx.command = {c.qualified_name: c for c in cog.walk_commands()}[name]
    controller = controller or RecordingController()
    ctx.controllers[guild] = controller
    run(ctx.bot.sessions.reset(guild))
    sent = []

    async def defer(**kwargs):
        # like Context.defer, a second call does nothing
        if ("defer",) not in guild.events:
            guild.events.append(("defer",))

    async def send(content=None, **kwargs):
        sent.append(content)
        return "message"

    ctx.defer = defer
    ctx.send = send
    ctx.message = types.SimpleNamespace(attachments=[], reference=None)
    ctx.bot.DbSession = None
    ctx.bot.sessions.settings(guild).announce_songs = False
    ctx.interaction = None
    return guild, ctx, cog, sent


def joins(guild):
    return [e for e in guild.events if e[0] in ("connect", "move")]


def invoke(cog, ctx, callback, **kwargs):
    run(invoke_music_hooks(cog, ctx))
    run(callback(cog, ctx, **kwargs))


# --- refusals never join ----------------------------------------------


@pytest.mark.parametrize("callback", [Music._play, Music._play_next])
def test_play_with_no_track_and_no_file_does_not_join(callback):
    guild, ctx, cog, sent = body_ctx(callback.name)
    invoke(cog, ctx, callback.callback)
    assert sent == [config.PLAY_ARGS_MISSING]
    assert joins(guild) == []


def test_a_slash_play_with_no_track_and_no_file_does_not_join():
    guild, ctx, cog, sent = body_ctx("play")
    ctx.interaction = object()
    invoke(cog, ctx, Music._play.callback, file=None)
    assert sent == [config.PLAY_ARGS_MISSING]
    assert joins(guild) == []


@pytest.mark.parametrize(
    "controller, reply",
    [
        (RecordingController(), config.QUEUE_EMPTY),
        (RecordingController(readable=False), config.RESTORE_BACKUP_UNREADABLE),
    ],
    ids=["empty backup", "unreadable backup"],
)
def test_restore_with_nothing_to_restore_does_not_join(controller, reply):
    guild, ctx, cog, sent = body_ctx("restore", controller, role="dj")
    invoke(cog, ctx, Music._restore.callback)
    assert sent == [reply]
    assert joins(guild) == []


def test_restore_while_active_does_not_move_the_bot():
    controller = RecordingController(backup=["x"], active=True)
    guild, ctx, cog, sent = body_ctx(
        "restore", controller, bot=BOTS_ONLY, role="dj"
    )
    invoke(cog, ctx, Music._restore.callback)
    assert sent == [config.RESTORE_WHILE_ACTIVE]
    assert joins(guild) == []


def test_prev_with_no_previous_track_does_not_join():
    guild, ctx, cog, sent = body_ctx("prev", RecordingController())
    invoke(cog, ctx, Music._prev.callback)
    assert sent == ["No previous track."]
    assert joins(guild) == []


def test_playlist_load_of_an_unknown_name_does_not_join(monkeypatch):
    async def get(session_factory, ref):
        return None

    monkeypatch.setattr(music.playlists, "get", get)
    guild, ctx, cog, sent = body_ctx("playlist load")
    invoke(cog, ctx, Music._playlist_load.callback, name="nope")
    assert sent == [config.PLAYLIST_NOT_FOUND]
    assert joins(guild) == []


def test_search_with_a_song_error_replies_and_does_not_join(monkeypatch):
    async def search_youtube(query, count):
        raise SongError(config.SEARCH_NO_RESULTS)

    monkeypatch.setattr(music, "search_youtube", search_youtube)
    guild, ctx, cog, sent = body_ctx("search")
    run(invoke_music_hooks(cog, ctx))
    with pytest.raises(SongError) as info:
        run(Music._search.callback(cog, ctx, query="q"))
    assert str(info.value) == config.SEARCH_NO_RESULTS
    assert joins(guild) == []


def search_results(monkeypatch):
    songs = [
        types.SimpleNamespace(
            webpage_url="https://example.com/a", title="A", uploader="u",
            duration=1, thumbnail=None,
        )
    ]

    async def search_youtube(query, count):
        return songs

    monkeypatch.setattr(music, "search_youtube", search_youtube)
    monkeypatch.setattr(
        music.utils, "songs_embed", lambda title, songs: "embed"
    )


def test_search_sends_its_results_without_joining(monkeypatch):
    search_results(monkeypatch)
    guild, ctx, cog, sent = body_ctx("search")
    invoke(cog, ctx, Music._search.callback, query="q")
    assert sent == [None]
    assert joins(guild) == []


def test_a_pick_from_the_search_results_joins_and_plays(monkeypatch):
    search_results(monkeypatch)
    controller = RecordingController()
    guild, ctx, cog, sent = body_ctx("search", controller)
    sent_kwargs = []

    async def send(content=None, **kwargs):
        sent_kwargs.append(kwargs)
        return "message"

    ctx.send = send
    invoke(cog, ctx, Music._search.callback, query="q")
    assert joins(guild) == []
    view = sent_kwargs[0]["view"]

    @contextlib.asynccontextmanager
    async def typing():
        yield

    ctx.channel.typing = typing
    ctx.command = None
    from discord_fakes import FakeInteraction

    inter = FakeInteraction(ctx, "pick")
    run(view.children[0].callback(inter))
    assert joins(guild) == [("connect", "b")]
    assert controller.calls == [
        ("process_song", "https://example.com/a", False)
    ]


# --- commands that still join ----------------------------------------


@pytest.mark.parametrize(
    "callback, playnext",
    [(Music._play, False), (Music._play_next, True)],
)
def test_play_with_a_track_defers_then_connects_then_plays(
    callback, playnext
):
    controller = RecordingController()
    guild, ctx, cog, sent = body_ctx(callback.name, controller)
    invoke(cog, ctx, callback.callback, track="song")
    assert guild.events == [("defer",), ("connect", "b")]
    assert controller.calls == [("process_song", "song", playnext)]


def test_play_with_an_attached_file_connects_and_plays_it():
    controller = RecordingController()
    guild, ctx, cog, sent = body_ctx("play", controller)
    ctx.message = types.SimpleNamespace(
        attachments=[object()], jump_url="https://discord/m", reference=None
    )
    invoke(cog, ctx, Music._play.callback)
    assert joins(guild) == [("connect", "b")]
    assert controller.calls == [("process_song", "https://discord/m", False)]


def test_restore_with_a_backup_defers_then_connects_then_starts():
    controller = RecordingController(backup=["x"])
    guild, ctx, cog, sent = body_ctx("restore", controller, role="dj")
    invoke(cog, ctx, Music._restore.callback)
    assert guild.events == [("defer",), ("connect", "b")]
    assert controller.calls == ["start_restored"]
    assert sent == ["Restored playlist"]


def test_prev_with_history_connects_then_plays_it():
    controller = RecordingController(history=["x"])
    guild, ctx, cog, sent = body_ctx("prev", controller)
    invoke(cog, ctx, Music._prev.callback)
    assert guild.events == [("defer",), ("connect", "b")]
    assert controller.calls == ["prev_song"]


def existing_playlist(monkeypatch):
    contents = types.SimpleNamespace(
        ref=None,
        entries=[types.SimpleNamespace(url="https://example.com/a", title="t")],
    )

    async def get(session_factory, ref):
        return contents

    monkeypatch.setattr(music.playlists, "get", get)


def test_playlist_load_of_an_existing_playlist_connects_then_enqueues(
    monkeypatch,
):
    existing_playlist(monkeypatch)
    controller = RecordingController()
    guild, ctx, cog, sent = body_ctx("playlist load", controller)
    invoke(cog, ctx, Music._playlist_load.callback, name="mix")
    assert guild.events == [("defer",), ("connect", "b")]
    assert controller.calls == [("enqueue", ["https://example.com/a"])]


# --- a failed join ---------------------------------------------------


def test_playlist_load_with_a_failing_connect_enqueues_nothing(monkeypatch):
    existing_playlist(monkeypatch)
    controller = RecordingController()
    guild, ctx, cog, sent = body_ctx("playlist load", controller)
    guild.fail = TimeoutError()
    with pytest.raises(CheckError) as info:
        invoke(cog, ctx, Music._playlist_load.callback, name="mix")
    assert str(info.value) == config.VOICE_CONNECT_FAILED
    assert controller.calls == []


def test_restore_with_a_failing_connect_keeps_the_backup_and_starts_nothing():
    controller = RecordingController(backup=["x"])
    guild, ctx, cog, sent = body_ctx("restore", controller, role="dj")
    guild.fail = TimeoutError()
    with pytest.raises(CheckError) as info:
        invoke(cog, ctx, Music._restore.callback)
    assert str(info.value) == config.VOICE_CONNECT_FAILED
    assert controller.queue == ["x"]
    assert controller.calls == []


def test_restore_reset_race_says_the_queue_is_empty_and_plays_nothing():
    first = RecordingController(backup=["x"])
    guild, ctx, cog, sent = body_ctx("restore", first, role="dj")
    run(invoke_music_hooks(cog, ctx))
    second = RecordingController()
    ctx.controllers[guild] = second
    run(ctx.bot.sessions.reset(guild))
    run(Music._restore.callback(cog, ctx))
    assert sent == [config.QUEUE_EMPTY]
    assert first.calls == [] and second.calls == []
