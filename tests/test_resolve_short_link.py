"""linkutils.resolve_short_link against a local aiohttp server."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from musicbot import linkutils

from helpers import run


@asynccontextmanager
async def serving():
    async def asleep(request):
        await asyncio.Event().wait()

    app = web.Application()
    app.router.add_get("/a", lambda r: web.HTTPFound("/b"))
    app.router.add_get("/b", lambda r: web.HTTPFound("/final?si=1"))
    app.router.add_get("/final", lambda r: web.Response(text="ok"))
    app.router.add_get("/gone", lambda r: web.Response(status=404))
    app.router.add_get("/boom", lambda r: web.Response(status=503))
    app.router.add_get("/to-gone", lambda r: web.HTTPFound("/gone"))
    app.router.add_get("/to-boom", lambda r: web.HTTPFound("/boom"))
    app.router.add_get("/slow", asleep)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()
        await linkutils.stop()


def test_redirect_chain_returns_the_final_url():
    async def scenario():
        async with serving() as server:
            url = await linkutils.resolve_short_link(
                str(server.make_url("/a"))
            )
            return url, str(server.make_url("/final?si=1"))

    url, expected = run(scenario())

    assert url == expected


@pytest.mark.parametrize("path", ["/gone", "/boom", "/to-gone", "/to-boom"])
def test_error_status_raises(path):
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


def test_slow_server_raises(monkeypatch):
    monkeypatch.setattr(linkutils, "HTTP_TIMEOUT", 0.05)

    async def scenario():
        async with serving() as server:
            await linkutils.resolve_short_link(str(server.make_url("/slow")))

    with pytest.raises(asyncio.TimeoutError):
        run(scenario())
