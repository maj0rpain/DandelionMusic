"""linkutils.resolve_redirects against a local aiohttp server."""

from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from musicbot import linkutils

from helpers import run

# Where each refusal route redirects; {port} is the local server's.
REFUSED_TARGETS = {
    "to-localhost": "http://localhost:{port}/hit",
    "to-loopback": "http://127.0.0.1:{port}/hit",
    "to-private": "http://10.0.0.1/x",
    "to-link-local": "http://169.254.169.254/x",
    "to-file": "file:///etc/passwd",
}


@asynccontextmanager
async def serving():
    hits = []
    hits_all = []
    body_sent = []

    async def hit(request):
        hits.append(request.path)
        return web.Response(text="hit")

    async def media(request):
        hits.append(request.path)
        response = web.StreamResponse()
        await response.prepare(request)
        try:
            for _ in range(256):
                await response.write(b"x" * 65536)
            body_sent.append("whole body")
        except ConnectionError:
            pass
        return response

    def to_chain(request):
        n = int(request.match_info["n"])
        return web.HTTPFound(f"/chain/{n - 1}" if n else "/final?si=1")

    def redirect_to(target):
        return lambda r: web.HTTPFound(target.format(port=r.url.port))

    @web.middleware
    async def record(request, handler):
        hits_all.append(request.path)
        return await handler(request)

    app = web.Application(middlewares=[record])
    app.router.add_get(
        "/not-modified-body", lambda r: web.Response(status=300, text="body")
    )
    app.router.add_get("/final", lambda r: web.Response(text="ok"))
    app.router.add_get("/gone", lambda r: web.Response(status=404))
    app.router.add_get("/boom", lambda r: web.Response(status=503))
    app.router.add_get("/to-gone", lambda r: web.HTTPFound("/gone"))
    app.router.add_get("/to-boom", lambda r: web.HTTPFound("/boom"))
    app.router.add_get("/hit", hit)
    app.router.add_get("/media.mp3", media)
    app.router.add_get(r"/chain/{n:\d+}", to_chain)
    app.router.add_get("/not-modified", lambda r: web.Response(status=304))
    for name, target in REFUSED_TARGETS.items():
        app.router.add_get(f"/{name}", redirect_to(target))
    server = TestServer(app)
    server.hits = hits
    server.hits_all = hits_all
    server.body_sent = body_sent
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()
        await linkutils.stop()


@pytest.fixture
def allow_every_host(monkeypatch):
    monkeypatch.setattr(linkutils, "_is_disallowed_host", lambda host: False)


class Resolved:
    """What running a linkutils function on one path of the local server
    did."""

    def __init__(self, base, url, error, hits, body_sent, paths):
        self.base = base
        self.url = url
        self.error = error
        self.hits = hits
        self.body_sent = body_sent
        self.paths = paths

    def at(self, path):
        return self.base + path


def _run_on(function, path):
    async def scenario():
        async with serving() as server:
            base = str(server.make_url("/")).rstrip("/")
            url = error = None
            try:
                url = await function(base + path)
            except Exception as exc:
                error = exc
            return Resolved(
                base,
                url,
                error,
                list(server.hits),
                list(server.body_sent),
                list(server.hits_all),
            )

    return run(scenario())


def _resolve(path):
    return _run_on(linkutils.resolve_redirects, path)


def _soup(path):
    return _run_on(linkutils.get_soup, path)


@pytest.mark.parametrize("path", ["/gone", "/boom", "/to-gone", "/to-boom"])
def test_error_status_raises(path, allow_every_host):
    assert _resolve(path).error is not None


def test_refused_connection_raises():
    async def scenario():
        async with serving() as server:
            url = str(server.make_url("/final"))
            await server.close()
            await linkutils.resolve_redirects(url)

    with pytest.raises(Exception):
        run(scenario())


@pytest.mark.parametrize("path", sorted("/" + n for n in REFUSED_TARGETS))
def test_redirect_to_a_disallowed_target_is_refused(path):
    got = _resolve(path)

    assert isinstance(got.error, linkutils.RefusedRedirect)
    assert got.hits == []


def test_chain_of_ten_redirects_resolves(allow_every_host):
    got = _resolve("/chain/9")

    assert got.url == got.at("/final?si=1")


def test_chain_of_eleven_redirects_is_refused(allow_every_host):
    assert isinstance(_resolve("/chain/10").error, linkutils.RefusedRedirect)


def test_redirect_without_location_returns_its_own_url(allow_every_host):
    got = _resolve("/not-modified")

    assert got.url == got.at("/not-modified")


def test_url_is_returned_as_requested_not_reparsed(allow_every_host):
    got = _resolve("/final?q=a b")

    assert got.url == got.at("/final?q=a b")


def test_direct_media_answer_returns_its_url_after_one_get(allow_every_host):
    got = _resolve("/media.mp3")

    assert got.url == got.at("/media.mp3")
    assert got.hits == ["/media.mp3"]
    assert got.body_sent == []


def test_page_is_parsed_from_the_final_hop_each_hop_requested_once(
    allow_every_host,
):
    got = _soup("/chain/2")

    assert got.error is None
    assert got.url.get_text() == "ok"
    assert got.paths == ["/chain/2", "/chain/1", "/chain/0", "/final"]


def test_page_redirect_to_a_disallowed_target_is_refused():
    got = _soup("/to-localhost")

    assert isinstance(got.error, linkutils.RefusedRedirect)
    assert "/hit" not in got.paths


def test_page_redirect_without_location_is_parsed(allow_every_host):
    got = _soup("/not-modified-body")

    assert got.error is None
    assert got.url.get_text() == "body"
