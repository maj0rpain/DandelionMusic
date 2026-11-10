"""The player panel over a fake message and channel: its layout, its
refresh and the expired-token recovery, attach, the destination, and
the click callbacks, the drawn view's life (stopped whenever the panel
lets go of its message; the view store holding only the current view),
and a deleted player message forgotten without a log at every edit.
Driven through the panel's public methods only."""

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
        self.store = None
        self.shown = None
        self.strips_saw_finished = []

    def show(self, view):
        """A send carrying `view`, registering it as discord.py does."""
        self.shown = view
        if view is not None and self.store is not None:
            self.store.add_view(view, self.id)

    async def edit(self, *, view):
        if view is None:
            self.strips_saw_finished.append(
                self.shown is not None and self.shown.is_finished()
            )
        if self.errors:
            raise self.errors.pop(0)
        self.edits.append(view)
        if self.store is not None:
            self.store._synced_message_views.pop(self.id, None)
        self.show(view)


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
            message.show(view)
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


def test_a_missing_controller_reads_as_inactive(attached):
    rig, message = attached
    rig.controller = None

    rig.refresh()

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
        return message

    message = PanelMessage()
    run(rig.panel.attach(send))
    run(rig.panel.playback_ended())

    assert seen == [None]
    assert message.edits == []


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


def test_a_message_gone_on_refetch_is_dropped(expired, capsys):
    rig, channel, stale = expired
    channel.fetch_error = coded_error(discord.NotFound, 404, 10008)

    rig.refresh()
    rig.refresh()

    assert capsys.readouterr().err == ""
    assert stale.edits == []
    assert channel.fetches == 1


def test_another_refetch_error_is_logged_and_drops_the_message(
    expired, capsys
):
    rig, channel, stale = expired
    channel.fetch_error = RuntimeError("boom")

    rig.refresh()
    rig.refresh()

    err = capsys.readouterr().err
    assert err.startswith("Failed to refetch the player message:\nTraceback")
    assert err.rstrip().endswith("RuntimeError: boom")
    assert stale.edits == [] and channel.fetches == 1


def test_another_http_error_keeps_the_message_for_the_next_tick(
    attached, capsys
):
    rig, message = attached
    message.errors = [http_error()]
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
            message.show(view)
            return message

        if not once:
            once.append(True)
            if new_snapshot is not None:
                rig.controller.snapshot = new_snapshot
            await rig.panel.attach(send)
        return await original(*args, **kwargs)

    setattr(target, attr, replaced)


def test_a_message_attached_during_a_refetch_survives_its_failure(expired):
    rig, channel, stale = expired
    channel.fetch_error = coded_error(discord.NotFound, 404, 10008)
    newer = PanelMessage()
    attach_during(rig, newer, channel, "fetch_message")

    rig.refresh()
    run(rig.panel.playback_ended())

    assert newer.edits[-1:] == [None]


GONE = [
    pytest.param(
        lambda: coded_error(discord.NotFound, 404, 10008),
        id="not-found-10008",
    ),
    pytest.param(
        lambda: coded_error(discord.NotFound, 404, 10003),
        id="not-found-other-code",
    ),
]
LOST = [
    pytest.param(
        lambda: coded_error(discord.Forbidden, 403, 50001),
        id="forbidden-50001",
    ),
    pytest.param(
        lambda: coded_error(discord.Forbidden, 403, 0),
        id="forbidden-other-code",
    ),
]


def with_lost_count(params, count):
    """`params` (error factories) each with the expected count of
    "Lost access" log lines, keeping their ids."""
    return [
        pytest.param(p.values[0], count, id=p.id, marks=p.marks)
        for p in params
    ]


@pytest.mark.parametrize(
    "error, lost_lines",
    with_lost_count(GONE, 0) + with_lost_count(LOST, 1),
)
def test_an_unreachable_message_is_forgotten_and_a_lost_access_logged(
    attached, capsys, error, lost_lines
):
    rig, message = attached
    old_view = message.shown
    message.errors = [error()]
    rig.controller.snapshot = snap(volume=0)
    rig.refresh()
    rig.controller.snapshot = snap(volume=5)

    rig.refresh()

    assert message.edits == [] and old_view.is_finished()
    err = capsys.readouterr().err
    assert err.count("Lost access to the player message:") == lost_lines


@pytest.mark.parametrize("error", GONE)
def test_a_deleted_message_is_stripped_silently_by_playback_ended(
    attached, capsys, error
):
    rig, message = attached
    view = message.shown
    message.errors = [error()]

    run(rig.panel.playback_ended())

    assert view.is_finished()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("error", GONE)
def test_a_deleted_message_is_stripped_silently_by_attach(
    attached, capsys, error
):
    rig, old = attached
    view = old.shown
    old.errors = [error()]

    rig.attach(PanelMessage())

    assert view.is_finished()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("error", GONE)
def test_a_deleted_message_is_stripped_silently_by_an_inactive_refresh(
    attached, capsys, error
):
    rig, message = attached
    view = message.shown
    message.errors = [error()]
    rig.controller.snapshot = PanelSnapshot.inactive()

    rig.refresh()

    assert view.is_finished()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("error", LOST)
def test_a_lost_access_strip_is_still_logged_once(attached, capsys, error):
    rig, message = attached
    message.errors = [error()]

    run(rig.panel.playback_ended())

    err = capsys.readouterr().err
    assert err.count("Failed to strip the player buttons:") == 1


@pytest.mark.parametrize("error", GONE)
def test_a_message_deleted_after_the_refetch_is_forgotten_silently(
    expired, capsys, error
):
    rig, channel, stale = expired
    fresh = PanelMessage(channel, errors=[error()])
    channel.fetch_result = fresh

    rig.refresh()
    rig.controller.snapshot = snap(volume=10)
    rig.refresh()

    assert fresh.edits == [] and stale.edits == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("error", LOST)
def test_a_lost_access_on_the_refetched_edit_is_still_logged(
    expired, capsys, error
):
    rig, channel, stale = expired
    channel.fetch_result = PanelMessage(channel, errors=[error()])

    rig.refresh()

    err = capsys.readouterr().err
    assert err.count("Failed to update the refetched player message:") == 1


@pytest.mark.parametrize("error", GONE + LOST)
def test_a_message_attached_during_a_failing_edit_survives(attached, error):
    rig, message = attached
    # the attach's take-down strip uses the first error up
    message.errors = [error(), error()]
    newer = PanelMessage()
    rig.controller.snapshot = snap(volume=0)
    attach_during(rig, newer, message, "edit")

    rig.refresh()
    run(rig.panel.playback_ended())

    assert newer.edits[-1:] == [None]


@pytest.mark.parametrize("error", GONE + LOST)
def test_a_message_attached_after_a_forget_is_the_player_message(
    attached, error
):
    rig, message = attached
    message.errors = [error()]
    rig.controller.snapshot = snap(volume=0)
    rig.refresh()
    newer = PanelMessage()
    rig.attach(newer)
    rig.controller.snapshot = snap(volume=5)

    rig.refresh()
    run(rig.panel.playback_ended())

    assert len(newer.edits) == 2 and newer.edits[0] is not None
    assert newer.edits[-1] is None


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


# -- the drawn view's life ---------------------------------------------


def test_playback_ended_stops_the_view_before_the_strip(attached):
    rig, message = attached
    view = message.shown

    run(rig.panel.playback_ended())

    assert view.is_finished()
    assert message.strips_saw_finished == [True]


def test_attach_stops_the_replaced_view_before_the_strip(attached):
    rig, old = attached
    view = old.shown

    rig.attach(PanelMessage())

    assert view.is_finished()
    assert old.strips_saw_finished == [True]


def test_attach_stops_the_replaced_view_when_its_strip_fails(attached):
    rig, old = attached
    view = old.shown
    old.errors = [EXPIRED]

    rig.attach(PanelMessage())

    assert view.is_finished()
    assert old.strips_saw_finished == [True]


def test_an_inactive_refresh_stops_the_view_before_the_strip(attached):
    rig, message = attached
    view = message.shown
    rig.controller.snapshot = PanelSnapshot.inactive()

    rig.refresh()

    assert view.is_finished()
    assert message.strips_saw_finished == [True]


def test_a_refresh_keeps_the_new_view_live_and_leaves_the_old_one(attached):
    rig, message = attached
    old = message.shown
    rig.controller.snapshot = snap(volume=0)

    rig.refresh()
    new = message.edits[0]

    assert not new.is_finished() and not old.is_finished()
    run(rig.panel.playback_ended())
    assert new.is_finished()


def test_a_failed_refresh_edit_leaves_the_old_view_live(attached, capsys):
    rig, message = attached
    old = message.shown
    message.errors = [http_error()]
    rig.controller.snapshot = snap(volume=0)

    rig.refresh()

    assert not old.is_finished()
    run(rig.panel.playback_ended())
    assert old.is_finished()


def edit_taking_down(rig):
    """`(redrawn, edit)`: an edit that takes the panel down before it
    finishes, collecting the views it was asked to draw."""
    redrawn = []

    async def edit(*, view):
        await rig.panel.playback_ended()
        if view is not None:
            redrawn.append(view)

    return redrawn, edit


def test_a_refresh_edit_finishing_after_a_take_down_stops_the_new_view(
    attached,
):
    rig, message = attached
    rig.controller.snapshot = snap(volume=0)

    redrawn, edit_after_take_down = edit_taking_down(rig)

    message.edit = edit_after_take_down

    rig.refresh()

    assert redrawn[0].is_finished()


def test_recovery_forgetting_the_message_stops_the_view_on_not_found(expired):
    rig, channel, stale = expired
    view = stale.shown
    channel.fetch_error = coded_error(discord.NotFound, 404, 10008)

    rig.refresh()

    assert view.is_finished()


def test_recovery_stops_the_view_on_another_refetch_error(expired, capsys):
    rig, channel, stale = expired
    view = stale.shown
    channel.fetch_error = RuntimeError("boom")

    rig.refresh()

    assert view.is_finished()


def test_recovery_stops_the_view_when_the_refetched_edit_fails(
    expired, capsys
):
    rig, channel, stale = expired
    view = stale.shown
    channel.fetch_result = PanelMessage(channel, errors=[RuntimeError("x")])

    rig.refresh()

    assert view.is_finished()


def test_a_recovered_message_takes_down_with_the_new_view(expired):
    rig, channel, stale = expired
    fresh = PanelMessage(channel)
    channel.fetch_result = fresh

    rig.refresh()
    new = fresh.edits[0]
    assert not new.is_finished()
    run(rig.panel.playback_ended())

    assert new.is_finished()


def test_a_recovery_on_a_message_no_longer_current_keeps_the_view_live(
    expired,
):
    rig, channel, stale = expired
    channel.fetch_error = coded_error(discord.NotFound, 404, 10008)
    newer = PanelMessage()
    attach_during(rig, newer, channel, "fetch_message")

    rig.refresh()

    assert not newer.shown.is_finished()


def test_a_recovery_edit_finishing_after_a_take_down_stops_the_new_view(
    expired,
):
    rig, channel, stale = expired
    fresh = PanelMessage(channel)
    channel.fetch_result = fresh

    redrawn, edit_after_take_down = edit_taking_down(rig)

    fresh.edit = edit_after_take_down

    rig.refresh()

    assert redrawn[0].is_finished()


def test_a_take_down_after_a_raising_send_stops_nothing(attached):
    rig, old = attached

    handed = []

    async def send(view):
        handed.append(view)
        raise RuntimeError("no permission")

    with pytest.raises(RuntimeError):
        run(rig.panel.attach(send))

    run(rig.panel.playback_ended())

    assert not handed[0].is_finished()


@pytest.fixture
def store():
    return discord.ui.view.ViewStore(types.SimpleNamespace())


def test_the_view_store_holds_at_most_the_current_view_across_attaches(store):
    rig = Rig()
    for _ in range(4):
        message = PanelMessage()
        message.store = store
        rig.attach(message)
        assert len(store.persistent_views) == 1

    run(rig.panel.playback_ended())

    assert store.persistent_views == []


def test_the_view_store_holds_the_newest_view_across_refreshes(store):
    rig = Rig()
    message = PanelMessage()
    message.store = store
    rig.attach(message)

    for volume in (0, 10, 20):
        rig.controller.snapshot = snap(volume=volume)
        rig.refresh()
        assert store.persistent_views == [message.edits[-1]]
