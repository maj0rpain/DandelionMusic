"""Library replies a slash caller gets ephemerally and a prefix caller
gets plainly, driven through the command callbacks."""

from types import SimpleNamespace

import discord

from config import config
from musicbot.commands import library as lc
from helpers import run


def make_ctx(slash):
    sent = []

    async def send(*args, **kwargs):
        sent.append(kwargs)
        return "sent"

    class Typing:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1),
        interaction=object() if slash else None,
        send=send,
        typing=lambda **kw: Typing(),
    )
    return ctx, sent


INDEX = {"A": {"B": ["t"]}}


def _search(monkeypatch, ctx, results):
    monkeypatch.setattr(lc.library, "get_index", lambda: INDEX)

    async def search_async(index, query):
        return results

    monkeypatch.setattr(lc.library, "search_async", search_async)
    run(lc.Library._library_search.callback(lc.Library(None), ctx, query="q"))


def test_no_results_reply_is_private_and_mention_free(monkeypatch):
    for slash in (True, False):
        ctx, sent = make_ctx(slash)
        _search(monkeypatch, ctx, [])
        (kw,) = sent
        assert ("ephemeral" in kw) is slash
        assert kw["content"] == config.LIBRARY_SEARCH_NO_RESULTS.format(
            query="q"
        )
        assert isinstance(kw["allowed_mentions"], discord.AllowedMentions)
        assert (
            kw["allowed_mentions"].to_dict()
            == discord.AllowedMentions.none().to_dict()
        )


def test_search_results_reply_is_private_only_for_slash(monkeypatch):
    class FakeView:
        message = None

        def __init__(self, *a):
            pass

        def embed(self):
            return "embed"

    monkeypatch.setattr(lc, "LibrarySearchView", FakeView)
    for slash in (True, False):
        ctx, sent = make_ctx(slash)
        _search(monkeypatch, ctx, ["r"])
        (kw,) = sent
        assert ("ephemeral" in kw) is slash
        assert kw["view"].message == "sent"


def test_browse_view_is_private_only_for_slash(monkeypatch):
    monkeypatch.setattr(lc.library, "get_index", lambda: INDEX)

    class FakeView:
        session = SimpleNamespace(landed=lambda f: None)

        def embed(self, frame):
            return "embed"

    view = FakeView()
    monkeypatch.setattr(
        lc.LibraryBrowseView, "open", lambda ctx, index: (view, "f")
    )
    for slash in (True, False):
        ctx, sent = make_ctx(slash)
        run(lc.Library._library_browse.callback(lc.Library(None), ctx))
        (kw,) = sent
        assert ("ephemeral" in kw) is slash
        assert view.message == "sent"
