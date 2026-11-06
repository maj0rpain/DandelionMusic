"""The player panel over a fake message and channel: its layout, its
refresh and the expired-token recovery, attach, the destination, and
the click callbacks. Driven through the panel's public methods only."""

import types

import discord
import pytest

from config import config
from musicbot import admission
from musicbot.panel_snapshot import PanelSnapshot
from musicbot.player_panel import PlayerPanel
from discord_fakes import EXPIRED, PLAYER_BUTTONS, coded_error, http_error
from helpers import run


def snap(**overrides):
    values = dict(
        active=True,
        paused=False,
        has_previous=True,
        has_next=True,
        queue_empty=False,
        loop_mode="off",
        volume=70,
        current_song=object(),
    )
    values.update(overrides)
    return PanelSnapshot(**values)


class PanelChannel:
    def __init__(self):
        self.sent = []
        self.fetch_result = None
        self.fetch_error = None
        self.fetches = 0

    async def fetch_message(self, _id):
        self.fetches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.fetch_result


class PanelMessage:
    """Records every view it is edited to; `errors` are raised by its
    next edits, one each."""

    def __init__(self, channel=None, errors=()):
        self.id = 1
        self.channel = channel or PanelChannel()
        self.edits = []
        self.errors = list(errors)

    async def edit(self, *, view):
        if self.errors:
            raise self.errors.pop(0)
        self.edits.append(view)


class PanelController:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.embed = object()

    def panel_snapshot(self):
        return self.snapshot

    def queue_embed(self):
        return self.embed


class Rig:
    def __init__(self, snapshot=None):
        self.controller = PanelController(snapshot or snap())
        self.settings = types.SimpleNamespace(announce_songs=True)
        self.panel = PlayerPanel(
            object(), lambda: self.controller, lambda: self.settings
        )

    def attach(self, message):
        async def send(view):
            return message

        return run(self.panel.attach(send))

    def refresh(self):
        run(self.panel.refresh())


@pytest.fixture
def expired():
    """A panel whose attached player message has an expired token:
    `(rig, channel, stale)`."""
    rig = Rig()
    channel = PanelChannel()
    stale = PanelMessage(channel, errors=[EXPIRED])
    rig.attach(stale)
    rig.controller.snapshot = snap(volume=0)
    return rig, channel, stale


@pytest.fixture
def directed():
    """A panel directed to a send channel: `(rig, channel)`."""
    rig = Rig()
    channel = SendChannel()
    rig.panel.direct_to(channel)
    return rig, channel


@pytest.fixture
def attached():
    """A panel with a player message attached: `(rig, message)`."""
    rig = Rig()
    message = PanelMessage()
    rig.attach(message)
    return rig, message


def buttons(view):
    return {b.custom_id: b for b in view.children}


def layout_of(snapshot):
    sent = []

    async def send(view):
        sent.append(view)
        return PanelMessage()

    rig = Rig(snapshot)
    run(rig.panel.attach(send))
    return sent[0]


def test_the_layout_is_the_player_buttons_in_order():
    view = layout_of(snap())

    assert [b.custom_id for b in view.children] == PLAYER_BUTTONS
    assert [b.row for b in view.children] == [
        None,
        None,
        None,
        None,
        1,
        1,
        1,
        1,
        2,
        2,
    ]


def test_an_inactive_snapshot_has_no_layout():
    assert layout_of(PanelSnapshot.inactive()) is None


def test_the_disabled_rules_follow_the_snapshot():
    view = layout_of(
        snap(
            has_previous=False,
            has_next=False,
            queue_empty=True,
            current_song=None,
        )
    )

    disabled = {i for i, b in buttons(view).items() if b.disabled}
    assert disabled == {
        "prev",
        "next",
        "loop",
        "current_song",
        "shuffle",
        "queue",
    }


def test_a_full_snapshot_disables_nothing_but_the_volume_up():
    view = layout_of(snap(volume=100))

    assert {i for i, b in buttons(view).items() if b.disabled} == {"volume_up"}


def test_volume_down_is_disabled_at_zero_only():
    assert buttons(layout_of(snap(volume=0)))["volume_down"].disabled
    assert not buttons(layout_of(snap(volume=10)))["volume_down"].disabled


def test_the_pause_emoji_follows_the_paused_state():
    assert str(buttons(layout_of(snap()))["pause"].emoji) == "⏸️"
    assert str(buttons(layout_of(snap(paused=True)))["pause"].emoji) == "▶️"


def test_the_loop_button_names_the_loop_mode():
    view = layout_of(snap(loop_mode="all"))

    assert buttons(view)["loop"].label == "Loop: all"


def test_the_playback_buttons_need_a_dj_and_the_rest_do_not():
    view = layout_of(snap())
    kinds = {b.custom_id: b._kind for b in view.children}

    assert kinds == {
        **dict.fromkeys(
            [
                "prev",
                "pause",
                "next",
                "loop",
                "shuffle",
                "stop",
                "volume_down",
                "volume_up",
            ],
            admission.RequestKind.DJ_PLAYER_BUTTON,
        ),
        "current_song": admission.RequestKind.PLAYER_BUTTON,
        "queue": admission.RequestKind.PLAYER_BUTTON,
    }


def test_an_unchanged_snapshot_is_not_edited_again(attached):
    rig, message = attached

    rig.refresh()
    rig.refresh()

    assert message.edits == []


def test_a_changed_snapshot_edits_the_message_once(attached):
    rig, message = attached
    rig.controller.snapshot = snap(volume=0)

    rig.refresh()
    rig.refresh()

    assert len(message.edits) == 1
    assert buttons(message.edits[0])["volume_down"].disabled


def test_an_inactive_snapshot_strips_the_buttons_and_forgets_the_message(
    attached,
):
    rig, message = attached
    rig.controller.snapshot = PanelSnapshot.inactive()

    rig.refresh()
    rig.controller.snapshot = snap()
    rig.refresh()

    assert message.edits == [None]


def test_a_missing_controller_reads_as_inactive():
    settings = types.SimpleNamespace(announce_songs=True)
    controllers = [PanelController(snap())]
    panel = PlayerPanel(object(), lambda: controllers[0], lambda: settings)
    message = PanelMessage()

    async def send(view):
        return message

    run(panel.attach(send))
    controllers[0] = None

    run(panel.refresh())

    assert message.edits == [None]


def test_playback_ended_strips_the_buttons_and_forgets_the_message(attached):
    rig, message = attached

    run(rig.panel.playback_ended())
    rig.controller.snapshot = snap(volume=0)
    rig.refresh()

    assert message.edits == [None]


def test_playback_ended_without_a_message_does_nothing():
    run(Rig().panel.playback_ended())


def test_a_failed_strip_in_playback_ended_is_logged_not_raised(
    attached, capsys
):
    rig, message = attached
    message.errors = [EXPIRED]

    run(rig.panel.playback_ended())

    assert "Failed to strip" in capsys.readouterr().err
    assert message.edits == []


def test_attach_strips_the_previous_message_and_returns_what_was_sent(
    attached,
):
    rig, old = attached
    new = PanelMessage()

    assert rig.attach(new) is new

    assert old.edits == [None]
    run(rig.panel.playback_ended())
    assert new.edits == [None]


def test_attach_sends_no_view_while_inactive():
    rig = Rig(PanelSnapshot.inactive())
    seen = []

    async def send(view):
        seen.append(view)
        return PanelMessage()

    run(rig.panel.attach(send))

    assert seen == [None]


def test_a_failed_strip_in_attach_is_logged_and_the_send_goes_on(
    attached, capsys
):
    rig, old = attached
    old.errors = [EXPIRED]
    new = PanelMessage()

    assert rig.attach(new) is new
    assert "Failed to strip" in capsys.readouterr().err


def test_a_raising_send_reaches_the_caller_and_records_nothing(attached):
    rig, old = attached

    async def send(view):
        raise RuntimeError("no permission")

    with pytest.raises(RuntimeError):
        run(rig.panel.attach(send))
    rig.controller.snapshot = snap(volume=0)
    rig.refresh()

    # the previous message was stripped and nothing replaced it
    assert old.edits == [None]


def test_an_interactions_original_response_is_the_player_message():
    rig = Rig()
    response = PanelMessage()

    class Responded(discord.Interaction):
        def __init__(self):
            pass

        async def original_response(self):
            return response

    interaction = Responded()

    async def send(view):
        return interaction

    assert run(rig.panel.attach(send)) is interaction
    run(rig.panel.playback_ended())

    assert response.edits == [None]


def test_an_expired_token_refetches_the_message_and_retries_once(expired):
    rig, channel, stale = expired
    fresh = PanelMessage(channel)
    channel.fetch_result = fresh

    rig.refresh()

    assert stale.edits == [] and len(fresh.edits) == 1
    # the refetched message is now the player message
    run(rig.panel.playback_ended())
    assert fresh.edits[-1] is None


def test_a_second_expired_token_drops_the_message_after_one_retry(expired):
    rig, channel, stale = expired
    fresh = PanelMessage(channel, errors=[EXPIRED, EXPIRED])
    channel.fetch_result = fresh

    rig.refresh()
    rig.controller.snapshot = snap(volume=10)
    rig.refresh()

    # one retry, then nothing is edited any more
    assert fresh.errors == [EXPIRED] and channel.fetches == 1
    assert stale.edits == [] and fresh.edits == []


def test_another_error_on_the_retry_is_logged_and_drops_the_message(
    expired, capsys
):
    rig, channel, stale = expired
    fresh = PanelMessage(channel, errors=[RuntimeError("boom")])
    channel.fetch_result = fresh

    rig.refresh()
    rig.controller.snapshot = snap(volume=10)
    rig.refresh()
    run(rig.panel.playback_ended())

    assert "Failed to update the refetched" in capsys.readouterr().err
    assert stale.edits == [] and fresh.edits == []


def test_a_message_gone_on_refetch_is_dropped(expired):
    rig, channel, stale = expired
    channel.fetch_error = coded_error(discord.NotFound, 404, 10008)

    rig.refresh()
    rig.refresh()

    assert stale.edits == []
    assert channel.fetches == 1


def test_another_refetch_error_is_logged_and_drops_the_message(
    expired, capsys
):
    rig, channel, stale = expired
    channel.fetch_error = RuntimeError("boom")

    rig.refresh()
    rig.refresh()

    assert "Failed to refetch" in capsys.readouterr().err
    assert stale.edits == [] and channel.fetches == 1


def test_another_http_error_keeps_the_message_for_the_next_tick(capsys):
    rig = Rig()
    message = PanelMessage(errors=[http_error()])
    rig.attach(message)
    rig.controller.snapshot = snap(volume=0)

    rig.refresh()
    rig.refresh()

    assert "Failed to update" in capsys.readouterr().err
    assert len(message.edits) == 1 and message.edits[0] is not None


def attach_during(rig, message, target, attr, new_snapshot=None):
    """Makes `target.<attr>` (an async method) first attach `message` as
    the player message, as a command arriving mid-refresh would."""
    original = getattr(target, attr)
    once = []

    async def replaced(*args, **kwargs):
        async def send(view):
            return message

        if not once:
            once.append(True)
            if new_snapshot is not None:
                rig.controller.snapshot = new_snapshot
            await rig.panel.attach(send)
        return await original(*args, **kwargs)

    setattr(target, attr, replaced)


def test_a_message_attached_during_a_refetch_survives_its_failure():
    rig = Rig()
    channel = PanelChannel()
    channel.fetch_error = coded_error(discord.NotFound, 404, 10008)
    stale = PanelMessage(channel, errors=[EXPIRED])
    newer = PanelMessage()
    rig.attach(stale)
    rig.controller.snapshot = snap(volume=0)
    attach_during(rig, newer, channel, "fetch_message")

    rig.refresh()
    run(rig.panel.playback_ended())

    assert newer.edits[-1:] == [None]


def test_a_message_attached_during_an_edit_is_drawn_as_attached(attached):
    rig, message = attached
    newer = PanelMessage()
    rig.controller.snapshot = snap(volume=0)
    attach_during(rig, newer, message, "edit", snap(volume=5))

    rig.refresh()
    rig.refresh()

    assert newer.edits == []


# -- the destination ---------------------------------------------------


def carries(rig, channel, ephemeral=False, has_view=False):
    return rig.panel.carries_buttons(
        channel=channel, ephemeral=ephemeral, has_view=has_view
    )


def test_a_reply_carries_the_buttons_while_no_destination_is_set():
    assert carries(Rig(), object())


def test_a_reply_in_the_destination_carries_the_buttons():
    rig = Rig()
    here = object()
    rig.panel.direct_to(here)

    assert carries(rig, here)


def test_a_reply_in_another_channel_does_not_carry_the_buttons():
    rig = Rig()
    rig.panel.direct_to(object())

    assert not carries(rig, object())


def test_an_ephemeral_reply_does_not_carry_the_buttons():
    assert not carries(Rig(), object(), ephemeral=True)


def test_a_reply_with_its_own_view_does_not_carry_the_buttons():
    assert not carries(Rig(), object(), has_view=True)


def test_direct_to_replaces_the_destination_by_default():
    rig = Rig()
    first, second = object(), object()
    rig.panel.direct_to(first)
    rig.panel.direct_to(second)

    assert carries(rig, second) and not carries(rig, first)


def test_direct_to_without_replace_only_sets_an_unset_destination():
    rig = Rig()
    first, second = object(), object()
    rig.panel.direct_to(first, replace=False)
    rig.panel.direct_to(second, replace=False)

    assert carries(rig, first) and not carries(rig, second)


# -- the click callbacks -----------------------------------------------


class ClickCtx:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def test_the_current_song_button_posts_the_songs_embed():
    rig = Rig(snap(current_song=Track()))
    ctx = ClickCtx()

    run(rig.panel.current_song(ctx))

    assert ctx.sent == [((), {"embed": ("embed", config.SONGINFO_SONGINFO)})]


def test_the_current_song_button_without_a_song_posts_the_empty_message():
    rig = Rig(PanelSnapshot.inactive())
    ctx = ClickCtx()

    run(rig.panel.current_song(ctx))

    assert ctx.sent == [((config.QUEUE_EMPTY,), {})]


def test_the_queue_button_posts_the_queue_embed():
    rig = Rig()
    ctx = ClickCtx()

    run(rig.panel.queue(ctx))

    assert ctx.sent == [((), {"embed": rig.controller.embed})]


# -- announcements --------------------------------------------------


class SendChannel:
    """A destination channel: records sends, raises `error` when set."""

    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        message = PanelMessage()
        self.sent.append((args, kwargs, message))
        return message


class Track:
    display_name = "Tune"

    def format_output(self, kind):
        return ("embed", kind)


def announce(rig, track=None):
    run(rig.panel.announce(track or Track()))


def skipped(rig):
    run(rig.panel.report_skipped(Track()))


def test_announce_sends_the_now_playing_embed_with_the_buttons(directed):
    rig, channel = directed

    announce(rig)

    ((_, kwargs, _),) = channel.sent
    assert kwargs["embed"] == ("embed", config.SONGINFO_NOW_PLAYING)
    assert [b.custom_id for b in kwargs["view"].children] == PLAYER_BUTTONS


def test_report_skipped_sends_the_notice_with_the_buttons(directed):
    rig, channel = directed

    skipped(rig)

    ((_, kwargs, _),) = channel.sent
    assert "(Skipped: Tune)" in kwargs["content"]
    assert [b.custom_id for b in kwargs["view"].children] == PLAYER_BUTTONS


def test_announce_honours_announce_songs(directed):
    rig, channel = directed
    rig.settings.announce_songs = False

    announce(rig)

    assert channel.sent == []


def test_announcements_send_nothing_without_a_destination(attached):
    rig, message = attached

    announce(rig)
    skipped(rig)

    assert message.edits == []
    run(rig.panel.playback_ended())
    assert message.edits == [None]


def test_an_announcement_strips_the_previous_message_and_replaces_it(
    directed,
):
    rig, channel = directed
    old = PanelMessage()
    rig.attach(old)

    announce(rig)
    skipped(rig)

    assert old.edits == [None]
    first, second = channel.sent[0][2], channel.sent[1][2]
    assert first.edits == [None]
    run(rig.panel.playback_ended())
    assert second.edits == [None]


def test_a_failed_announcement_is_logged_and_not_raised(directed, capsys):
    rig, channel = directed
    channel.error = EXPIRED

    announce(rig)
    skipped(rig)

    assert "Failed to send a playback message" in capsys.readouterr().err


# --- the post-click steps ---


class StepCtx:
    def __init__(self):
        self.author = "alice"
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append(text)


def click_after(custom_id):
    """Awaits the `after` step the layout attached to `custom_id` on an
    attached panel, with the snapshot changed first so a refresh shows
    as an edit: (the click ctx, the panel message)."""
    rig = Rig()
    message = PanelMessage()
    views = []

    async def send(view):
        views.append(view)
        return message

    run(rig.panel.attach(send))
    rig.controller.snapshot = snap(volume=0)
    ctx = StepCtx()
    run(buttons(views[0])[custom_id]._after(ctx))
    return ctx, message


@pytest.mark.parametrize("custom_id", ["prev", "next"])
def test_a_skip_clicks_step_posts_the_notice_and_leaves_the_message(
    custom_id,
):
    ctx, message = click_after(custom_id)

    assert ctx.sent == ["alice Skipped a Song"]
    assert message.edits == []


@pytest.mark.parametrize(
    "custom_id",
    [b for b in PLAYER_BUTTONS if b not in ("prev", "next")],
)
def test_every_other_clicks_step_edits_the_message_once(custom_id):
    ctx, message = click_after(custom_id)

    assert ctx.sent == []
    assert len(message.edits) == 1
    assert buttons(message.edits[0])["volume_down"].disabled
