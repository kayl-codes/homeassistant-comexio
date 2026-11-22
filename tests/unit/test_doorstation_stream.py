"""A Doorstation camera read from the Doorstation itself: the first frame of its stream, and every way that fails."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
from aiohttp import web
import pytest

from custom_components.comexio import doorstation_stream
from custom_components.comexio.doorstation_stream import (
    SESSION_CLOSED_CAUSE,
    doorstation_direct_url,
    read_direct_frame,
)

# The smallest JPEG aiocomexio's frame reader accepts: SOI, one segment, a scan of one component with three bytes of
# data, EOI. The multipart head in front of it is what the Doorstation sends before each image.
FRAME = bytes.fromhex("ffd8 ffe0 0004 4a46 ffda 0008 01 0100 003f00 123456 ffd9".replace(" ", ""))
PART_HEAD = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _serve(handler: Handler, check: Callable[[str], Awaitable[None]]) -> None:
    """Run `check(url)` against a local server that answers every request with `handler`."""

    async def run() -> None:
        app = web.Application()
        app.router.add_get("/proxy/cam/{camera_id}/", handler)
        # A handler that keeps its stream open is cancelled at once when the server goes (no minute of waiting).
        runner = web.AppRunner(app, shutdown_timeout=0.1, handler_cancellation=True)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]  # noqa: SLF001
        try:
            await check(f"http://127.0.0.1:{port}/proxy/cam/1/")
        finally:
            await runner.cleanup()

    asyncio.run(run())


async def _read(url: str, **kwargs: float) -> tuple[bytes | None, str | None]:
    async with aiohttp.ClientSession() as session:
        return await read_direct_frame(session, url, **kwargs)


def _expect(handler: Handler, frame: bytes | None, cause: str | None, **kwargs: float) -> None:
    async def check(url: str) -> None:
        assert await _read(url, **kwargs) == (frame, cause)

    _serve(handler, check)


def test_a_redirect_is_a_failure_and_is_not_followed() -> None:
    """The answer comes over plain HTTP: a device on the network must not make Home Assistant call another address."""
    followed: list[str] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.match_info["camera_id"] == "1":
            raise web.HTTPFound("/proxy/cam/2/")
        followed.append(request.path)
        return web.Response(body=PART_HEAD + FRAME)

    async def check(url: str) -> None:
        assert await _read(url) == (None, "HTTP 302")
        assert not followed

    _serve(handler, check)


def test_the_url_is_the_doorstations_own_camera_one_whatever_the_io_servers_camera_id_is() -> None:
    # The IO-Server knows the camera of a real Doorstation as 7; the Doorstation itself serves it as camera 1.
    assert doorstation_direct_url("192.0.2.10") == "http://192.0.2.10/proxy/cam/1/"


def test_a_stream_that_never_ends_gives_its_first_frame_at_once() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"})
        await response.prepare(request)
        # The head and the frame arrive in small pieces, and the stream goes on after the frame like a live one.
        data = PART_HEAD + FRAME
        for start in range(0, len(data), 7):
            await response.write(data[start : start + 7])
        await asyncio.sleep(30)
        return response

    _expect(handler, FRAME, None)


@pytest.mark.parametrize(
    ("status", "cause"),
    [(404, "HTTP 404"), (503, "HTTP 503")],
)
def test_an_error_status_is_a_failure(status: int, cause: str) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(status=status)

    _expect(handler, None, cause)


def test_an_encoded_stream_is_refused() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + FRAME, headers={"Content-Encoding": "gzip"})

    _expect(handler, None, "the stream came encoded")


def test_a_stream_that_ends_without_a_frame_is_a_failure() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + FRAME[:-1])

    _expect(handler, None, "the stream ended without a frame")


def test_a_malformed_frame_is_a_failure_that_says_so() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        # An end marker before any scan: an image without picture data.
        return web.Response(body=PART_HEAD + bytes.fromhex("ffd8ffd9"))

    async def check(url: str) -> None:
        frame, cause = await _read(url)
        assert frame is None
        assert cause is not None
        assert cause.startswith("malformed frame (")

    _serve(handler, check)


def test_a_stream_that_stalls_is_a_failure_after_the_budget_it_is_given() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(PART_HEAD)
        await asyncio.sleep(30)
        return response

    _expect(handler, None, "no frame in time", budget_sec=0.3)


def test_a_doorstation_that_does_not_answer_in_time_is_told_from_one_that_sends_no_frame() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(30)  # not even the head of the answer
        return web.Response()

    _expect(handler, None, "no answer in time", budget_sec=0.3)


def test_the_stream_is_asked_for_unencoded() -> None:
    """The byte cap counts bytes of the stream itself only while the Doorstation is not asked to compress."""
    seen: list[str | None] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(request.headers.get("Accept-Encoding"))
        return web.Response(body=PART_HEAD + FRAME)

    _expect(handler, FRAME, None)

    assert seen == ["identity"]


def test_the_stream_is_not_decompressed_by_aiohttp() -> None:
    """A compressed answer is not inflated before `response_problem` refuses it (the byte cap counts raw bytes)."""

    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + FRAME)

    async def check(url: str) -> None:
        async with aiohttp.ClientSession() as session:
            spy = MagicMock(wraps=session)
            assert await read_direct_frame(spy, url) == (FRAME, None)
        assert spy.get.call_args.kwargs["auto_decompress"] is False

    _serve(handler, check)


def test_a_stream_without_a_frame_is_given_up_at_the_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doorstation_stream, "DOORSTATION_DIRECT_MAX_BYTES", 1000)

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        # Bytes that never make a frame, more of them than the cap, and the stream does not end.
        await response.write(b"x" * 5000)
        await asyncio.sleep(30)
        return response

    _expect(handler, None, "no frame in the first bytes of the stream")


def test_a_closed_session_is_a_failure_and_not_an_exception() -> None:
    """Home Assistant closes its shared session while it stops: aiohttp then refuses the request with RuntimeError."""

    async def run() -> tuple[bytes | None, str | None]:
        session = aiohttp.ClientSession()
        await session.close()
        return await read_direct_frame(session, "http://192.0.2.10/proxy/cam/1/")

    assert asyncio.run(run()) == (None, SESSION_CLOSED_CAUSE)


@pytest.mark.parametrize(
    ("session_closed", "message"),
    [(False, "Session is closed"), (True, "a bug"), (False, "a bug")],
)
def test_any_other_runtime_error_is_not_swallowed(session_closed: bool, message: str) -> None:
    """Only aiohttp's refusal on a closed session is a failure cause; any other RuntimeError is a bug, visible."""
    session = MagicMock()
    session.closed = session_closed
    session.get.side_effect = RuntimeError(message)

    read = read_direct_frame(session, "http://192.0.2.10/proxy/cam/1/")

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(read)


def test_an_unreachable_doorstation_is_a_failure_that_names_no_address() -> None:
    # aiohttp's own error for a refused connection, whose message names the address: a fixed failure instead of a
    # port that was free a moment ago, which another process could take in between. The connection key is a
    # stand-in with the three fields the error reads (the real key's constructor changes between aiohttp versions).
    key = SimpleNamespace(host="192.0.2.10", port=80, ssl=True)
    refused = aiohttp.ClientConnectorError(key, OSError(111, "refused"))  # type: ignore[arg-type]
    assert "192.0.2.10" in str(refused)  # the text is what the cause must not carry
    session = MagicMock()
    session.get.side_effect = refused

    frame, cause = asyncio.run(read_direct_frame(session, "http://192.0.2.10/proxy/cam/1/"))

    assert frame is None
    assert cause == "ClientConnectorError"
    assert "192.0.2.10" not in cause
