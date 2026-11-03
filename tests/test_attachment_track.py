from types import SimpleNamespace as NS

from musicbot.commands.music import _attachment_track

ATT = [NS(url="cdn")]


def msg(attachments=(), reference=None, jump="msg-jump"):
    return NS(
        attachments=list(attachments), reference=reference, jump_url=jump
    )


def ref(resolved):
    return NS(resolved=resolved)


def ctx(message, interaction=None):
    return NS(message=message, interaction=interaction)


def test_slash_file_uses_file_url_not_synthetic_message():
    c = ctx(msg(ATT, jump="synthetic"), interaction=object())
    assert _attachment_track(c, NS(url="cdn-url")) == "cdn-url"


def test_prefix_attachments_use_message_jump_url():
    assert _attachment_track(ctx(msg(ATT)), None) == "msg-jump"


def test_prefix_reply_uses_referenced_jump_url():
    r = ref(msg(ATT, jump="ref-jump"))
    assert _attachment_track(ctx(msg(reference=r)), None) == "ref-jump"


def test_slash_without_file_is_none():
    assert _attachment_track(ctx(msg(), interaction=object()), None) is None


def test_own_attachments_win_over_reply():
    r = ref(msg(ATT, jump="ref-jump"))
    assert _attachment_track(ctx(msg(ATT, r)), None) == "msg-jump"


def test_unresolved_reference_is_none():
    assert _attachment_track(ctx(msg(reference=ref(None))), None) is None


def test_deleted_reference_is_none():
    deleted = NS(id=1)
    assert _attachment_track(ctx(msg(reference=ref(deleted))), None) is None


def test_nothing_is_none():
    assert _attachment_track(ctx(msg()), None) is None
