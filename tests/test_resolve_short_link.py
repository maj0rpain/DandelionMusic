"""linkutils.resolve_short_link against a local aiohttp server."""

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

    async def hit(request):
        hits.append(request.path)
        return web.Response(text="hit")

    def to_chain(request):
        n = int(request.match_info["n"])
        return web.HTTPFound(f"/chain/{n - 1}" if n else "/final")

    def redirect_to(target):
        return lambda r: web.HTTPFound(target.format(port=r.url.port))

    app = web.Application()
    app.router.add_get("/a", lambda r: web.HTTPFound("/b"))
    app.router.add_get("/b", lambda r: web.HTTPFound("/final?si=1"))
    app.router.add_get("/final", lambda r: web.Response(text="ok"))
    app.router.add_get("/gone", lambda r: web.Response(status=404))
    app.router.add_get("/boom", lambda r: web.Response(status=503))
    app.router.add_get("/to-gone", lambda r: web.HTTPFound("/gone"))
    app.router.add_get("/to-boom", lambda r: web.HTTPFound("/boom"))
    app.router.add_get("/hit", hit)
    app.router.add_get(r"/chain/{n:\d+}", to_chain)
    app.router.add_get("/loop", lambda r: web.HTTPFound("/loop"))
    app.router.add_get("/not-modified", lambda r: web.Response(status=304))
    for name, target in REFUSED_TARGETS.items():
        app.router.add_get(f"/{name}", redirect_to(target))
    server = TestServer(app)
    server.hits = hits
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
    """What resolving one path of the local server did."""

    def __init__(self, base, url, error, hits):
        self.base = base
        self.url = url
        self.error = error
        self.hits = hits

    def at(self, path):
        return self.base + path


def _resolve(path):
    async def scenario():
        async with serving() as server:
            base = str(server.make_url("/")).rstrip("/")
            url = error = None
            try:
                url = await linkutils.resolve_short_link(base + path)
            except Exception as exc:
                error = exc
            return Resolved(base, url, error, list(server.hits))

    return run(scenario())


def test_redirect_chain_returns_the_final_url(allow_every_host):
    got = _resolve("/a")

    assert got.url == got.at("/final?si=1")


@pytest.mark.parametrize("path", ["/gone", "/boom", "/to-gone", "/to-boom"])
def test_error_status_raises(path, allow_every_host):
    assert _resolve(path).error is not None


def test_refused_connection_raises():
    async def scenario():
        async with serving() as server:
            url = str(server.make_url("/a"))
            await server.close()
            await linkutils.resolve_short_link(url)

    with pytest.raises(Exception):
        run(scenario())


@pytest.mark.parametrize("path", sorted("/" + n for n in REFUSED_TARGETS))
def test_redirect_to_a_disallowed_target_is_refused(path):
    got = _resolve(path)

    assert isinstance(got.error, linkutils.RefusedRedirect)
    assert got.hits == []


def test_chain_of_ten_redirects_resolves(allow_every_host):
    got = _resolve("/chain/9")

    assert got.url == got.at("/final")


def test_chain_of_eleven_redirects_is_refused(allow_every_host):
    assert isinstance(_resolve("/chain/10").error, linkutils.RefusedRedirect)


def test_redirect_loop_is_refused(allow_every_host):
    assert isinstance(_resolve("/loop").error, linkutils.RefusedRedirect)


def test_redirect_without_location_returns_its_own_url(allow_every_host):
    got = _resolve("/not-modified")

    assert got.url == got.at("/not-modified")
