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


def test_redirect_chain_returns_the_final_url(allow_every_host):
    async def scenario():
        async with serving() as server:
            url = await linkutils.resolve_short_link(
                str(server.make_url("/a"))
            )
            return url, str(server.make_url("/final?si=1"))

    url, expected = run(scenario())

    assert url == expected


@pytest.mark.parametrize("path", ["/gone", "/boom", "/to-gone", "/to-boom"])
def test_error_status_raises(path, allow_every_host):
    async def scenario():
        async with serving() as server:
            await linkutils.resolve_short_link(str(server.make_url(path)))

    with pytest.raises(Exception):
        run(scenario())


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
    async def scenario():
        async with serving() as server:
            await linkutils.resolve_short_link(str(server.make_url(path)))

    with pytest.raises(linkutils.RefusedRedirect):
        run(scenario())


@pytest.mark.parametrize("path", ["/to-localhost", "/to-loopback"])
def test_refused_local_redirect_is_never_contacted(path):
    async def scenario():
        async with serving() as server:
            with pytest.raises(linkutils.RefusedRedirect):
                await linkutils.resolve_short_link(str(server.make_url(path)))
            return list(server.hits)

    assert run(scenario()) == []


def test_chain_of_ten_redirects_resolves(allow_every_host):
    async def scenario():
        async with serving() as server:
            url = await linkutils.resolve_short_link(
                str(server.make_url("/chain/9"))
            )
            return url, str(server.make_url("/final"))

    url, expected = run(scenario())

    assert url == expected


def test_chain_of_eleven_redirects_is_refused(allow_every_host):
    async def scenario():
        async with serving() as server:
            await linkutils.resolve_short_link(
                str(server.make_url("/chain/10"))
            )

    with pytest.raises(linkutils.RefusedRedirect):
        run(scenario())


def test_redirect_loop_is_refused(allow_every_host):
    async def scenario():
        async with serving() as server:
            await linkutils.resolve_short_link(str(server.make_url("/loop")))

    with pytest.raises(linkutils.RefusedRedirect):
        run(scenario())


def test_redirect_without_location_returns_its_own_url(allow_every_host):
    async def scenario():
        async with serving() as server:
            url = str(server.make_url("/not-modified"))
            return await linkutils.resolve_short_link(url), url

    got, url = run(scenario())

    assert got == url
