"""The message-link gate: whether a track that is a message link may be
loaded for the requesting member."""

import discord
import pytest

from config import config
from musicbot import message_link
from musicbot.loader import SongError
from musicbot.yt_dlp_plugins.extractor.discord import DiscordAttachmentsIE
from discord_fakes import coded_error
from helpers import run
from message_link_fakes import (
    FOREIGN_LINK,
    LINK,
    LinkChannel,
    LinkGuild,
    LinkMember,
    LinkThread,
    perms,
)

NOT_FOUND = coded_error(discord.NotFound, 404, 0)
FORBIDDEN = coded_error(discord.Forbidden, 403, 0)


def check(track, guild, user):
    return run(message_link.check_message_link(track, guild, user))


def refused(track, guild, user):
    with pytest.raises(SongError) as raised:
        check(track, guild, user)
    assert str(raised.value) == config.MESSAGE_LINK_FORBIDDEN


def test_a_link_into_another_guild_is_refused():
    refused(FOREIGN_LINK, LinkGuild([LinkChannel()]), LinkMember())


def test_an_unresolved_channel_is_refused():
    refused(LINK, LinkGuild([]), LinkMember())


def test_a_channel_the_member_cannot_view_is_refused():
    channel = LinkChannel(permissions=perms(view_channel=False))
    refused(LINK, LinkGuild([channel]), LinkMember())


def test_a_channel_whose_history_the_member_cannot_read_is_refused():
    channel = LinkChannel(permissions=perms(read_message_history=False))
    refused(LINK, LinkGuild([channel]), LinkMember())


def test_a_readable_channel_passes():
    check(LINK, LinkGuild([LinkChannel()]), LinkMember())


def test_a_readable_thread_passes():
    check(LINK, LinkGuild([LinkThread()]), LinkMember())


def test_a_private_thread_the_member_is_not_in_is_refused():
    thread = LinkThread(private=True, joined_error=NOT_FOUND)
    refused(LINK, LinkGuild([thread]), LinkMember())


def test_a_private_thread_the_member_is_in_passes():
    check(LINK, LinkGuild([LinkThread(private=True)]), LinkMember())


def test_a_private_thread_passes_for_manage_threads():
    thread = LinkThread(
        private=True,
        joined_error=NOT_FOUND,
        permissions=perms(manage_threads=True),
    )
    check(LINK, LinkGuild([thread]), LinkMember())


def test_a_private_thread_whose_member_lookup_is_forbidden_is_refused():
    thread = LinkThread(private=True, joined_error=FORBIDDEN)
    refused(LINK, LinkGuild([thread]), LinkMember())


def test_a_canary_link_is_checked_the_same():
    link = LINK.replace("discord.com", "canary.discord.com")
    refused(
        link.replace("1234", "99"), LinkGuild([LinkChannel()]), LinkMember()
    )
    check(link, LinkGuild([LinkChannel()]), LinkMember())


@pytest.mark.parametrize("wrap", ["<{}>", "  {}  ", "<{}>.", "{}.", "\n{}\n"])
def test_a_wrapped_or_padded_forbidden_link_is_refused(wrap):
    refused(
        wrap.format(FOREIGN_LINK), LinkGuild([LinkChannel()]), LinkMember()
    )


@pytest.mark.parametrize(
    "track",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://cdn.discordapp.com/attachments/1/2/a.mp3",
        "some search words",
    ],
)
def test_a_track_that_is_not_a_message_link_passes_untouched(track):
    check(track, LinkGuild([]), None)


@pytest.mark.parametrize("user", [None, object()])
def test_a_message_link_without_a_member_is_refused(user):
    refused(LINK, LinkGuild([LinkChannel()]), user)


def test_the_extractor_matches_with_the_gates_pattern():
    assert DiscordAttachmentsIE._VALID_URL is message_link.MESSAGE_LINK_PATTERN


def test_the_gate_imports_neither_the_loader_nor_linkutils_at_module_level():
    # module-level imports bind names in the module namespace; the check's
    # own imports are function-local and so bind none
    bound = set(vars(message_link))
    assert not {"SongError", "normalize_track", "loader", "linkutils"} & bound
