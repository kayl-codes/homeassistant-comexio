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
LIVE_HEADERS = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}

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


async def _live_start(url: str) -> tuple[bytes, str | None]:
    async with aiohttp.ClientSession() as session, session.get(url, allow_redirects=False) as resp:
        return await doorstation_stream.live_stream_start(resp)


def test_a_live_stream_is_passed_on_once_it_delivered_a_whole_frame_and_with_everything_read() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers=LIVE_HEADERS)
        await response.prepare(request)
        await response.write(PART_HEAD + FRAME + b"--frame\r\n")
        await asyncio.sleep(30)
        return response

    async def check(url: str) -> None:
        prefix, cause = await _live_start(url)
        assert cause is None
        # The frame and what came with it are handed on first: the frontend sees the stream from its start.
        assert prefix.startswith(PART_HEAD + FRAME)

    _serve(handler, check)


def test_a_live_stream_that_ends_without_data_is_a_failure() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=b"", headers=LIVE_HEADERS)

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", "the stream ended without a frame")

    _serve(handler, check)


def test_a_live_stream_that_ends_inside_its_first_frame_is_a_failure() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + FRAME[:-1], headers=LIVE_HEADERS)

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", "the stream ended without a frame")

    _serve(handler, check)


def test_a_live_stream_that_sends_a_head_and_no_picture_is_a_failure_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doorstation_stream, "DOORSTATION_DIRECT_TIMEOUT_SEC", 0.3)

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers=LIVE_HEADERS)
        await response.prepare(request)
        await response.write(PART_HEAD)  # data, but no frame, then silence
        await asyncio.sleep(30)
        return response

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", "no frame in time")

    _serve(handler, check)


def test_a_live_stream_that_sends_nothing_is_a_failure_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doorstation_stream, "DOORSTATION_DIRECT_TIMEOUT_SEC", 0.3)

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers=LIVE_HEADERS)
        await response.prepare(request)  # the head of the answer, then silence
        await asyncio.sleep(30)
        return response

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", "no frame in time")

    _serve(handler, check)


def test_a_live_stream_with_a_malformed_frame_is_a_failure_that_says_so() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + bytes.fromhex("ffd8ffd9"), headers=LIVE_HEADERS)

    async def check(url: str) -> None:
        prefix, cause = await _live_start(url)
        assert prefix == b""
        assert cause is not None
        assert cause.startswith("malformed frame (")

    _serve(handler, check)


@pytest.mark.parametrize(
    ("status", "headers", "cause"),
    [
        (404, {}, "HTTP 404"),
        (302, {**LIVE_HEADERS, "Location": "http://192.0.2.99/"}, "HTTP 302"),  # a redirect is an error status
        (200, {**LIVE_HEADERS, "Content-Encoding": "gzip"}, "the stream came encoded"),
    ],
)
def test_a_live_stream_answer_that_is_not_a_stream_is_a_failure(
    status: int, headers: dict[str, str], cause: str
) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(status=status, body=PART_HEAD + FRAME, headers=headers)

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", cause)

    _serve(handler, check)


@pytest.mark.parametrize(
    ("headers", "shown"),
    [
        ({}, "application/octet-stream"),  # aiohttp's default for an answer with no type of its own
        ({"Content-Type": "text/html"}, "text/html"),
        ({"Content-Type": "image/jpeg"}, "image/jpeg"),
        ({"Content-Type": "multipart/mixed; boundary=frame"}, "multipart/mixed"),
    ],
    ids=["octet-stream", "html", "jpeg", "other multipart"],
)
def test_a_live_stream_answer_of_another_type_is_not_passed_on(headers: dict[str, str], shown: str) -> None:
    """The type goes to the frontend as the Doorstation sent it: only an MJPEG stream may go through."""

    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + FRAME, headers=headers)

    async def check(url: str) -> None:
        # The cause names the type (a different one is the first thing to know), without its parameters.
        assert await _live_start(url) == (b"", f"the answer is not an MJPEG stream (Content-Type {shown})")

    _serve(handler, check)


def test_the_type_is_read_whatever_its_case_and_parameters() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + FRAME, headers={"Content-Type": "Multipart/X-Mixed-Replace;boundary=x"})

    async def check(url: str) -> None:
        prefix, cause = await _live_start(url)
        assert cause is None
        assert prefix.startswith(PART_HEAD + FRAME)

    _serve(handler, check)


def test_a_prefetched_stream_hands_out_what_was_read_first_and_then_the_rest() -> None:
    class _Rest:
        async def read(self, n: int = -1) -> bytes:
            return b"rest"

    stream = doorstation_stream.PrefetchedStream(b"first", _Rest())  # type: ignore[arg-type]

    async def read_all() -> list[bytes]:
        return [await stream.read(1), await stream.read(100), await stream.read(100)]

    # The prefetched bytes come whole, whatever the size asked for.
    assert asyncio.run(read_all()) == [b"first", b"rest", b"rest"]


def test_a_prefetched_stream_ends_once_its_camera_is_gone() -> None:
    class _Rest:
        async def read(self, n: int = -1) -> bytes:
            return b"rest"

    alive = [True]
    stream = doorstation_stream.PrefetchedStream(b"first", _Rest(), lambda: alive[0])  # type: ignore[arg-type]

    async def read_all() -> list[bytes]:
        out = [await stream.read(100)]
        alive[0] = False
        out += [await stream.read(100), await stream.read(100)]
        return out

    assert asyncio.run(read_all()) == [b"first", b"", b""]


def test_a_prefetched_stream_hands_the_size_asked_for_on_and_gives_nothing_of_a_camera_that_is_gone() -> None:
    """read() without a size reads a live stream to its end, which never comes: the size must reach the stream."""
    asked: list[int] = []

    class _Rest:
        async def read(self, n: int = -1) -> bytes:
            asked.append(n)
            return b"rest"

    stream = doorstation_stream.PrefetchedStream(b"first", _Rest())  # type: ignore[arg-type]
    gone = doorstation_stream.PrefetchedStream(b"first", _Rest(), lambda: False)  # type: ignore[arg-type]

    async def read() -> tuple[bytes, bytes, bytes]:
        return await stream.read(1), await stream.read(4096), await gone.read(4096)

    # The camera was gone before the very first read: not even what was read ahead is handed out.
    assert asyncio.run(read()) == (b"first", b"rest", b"")
    assert asked == [4096]


def _answer(status: int, headers: dict[str, str]) -> MagicMock:
    return MagicMock(status=status, headers=headers)


def test_a_type_from_the_network_is_cut_short_and_kept_to_one_line_before_it_reaches_a_log() -> None:
    hostile = "x" * 60 + "\nWARNING forged log line"

    problem = doorstation_stream.live_stream_problem(_answer(200, {"Content-Type": hostile}))

    assert problem == f"the answer is not an MJPEG stream (Content-Type {'x' * 40})"


def test_an_answer_with_no_type_at_all_is_named_so() -> None:
    problem = doorstation_stream.live_stream_problem(_answer(200, {}))

    assert problem == "the answer is not an MJPEG stream (Content-Type missing)"
