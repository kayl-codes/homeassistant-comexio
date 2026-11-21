"""A Doorstation camera read from the Doorstation itself: the first frame of its stream, and every way that fails."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import socket

import aiohttp
from aiohttp import web
import pytest

from custom_components.comexio import doorstation_stream
from custom_components.comexio.doorstation_stream import doorstation_direct_url, read_direct_frame

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


async def _read(url: str) -> tuple[bytes | None, str | None]:
    async with aiohttp.ClientSession() as session:
        return await read_direct_frame(session, url)


def _expect(handler: Handler, frame: bytes | None, cause: str | None) -> None:
    async def check(url: str) -> None:
        assert await _read(url) == (frame, cause)

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


def test_a_stream_that_stalls_is_a_failure_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doorstation_stream, "DOORSTATION_DIRECT_TIMEOUT_SEC", 0.3)

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(PART_HEAD)
        await asyncio.sleep(30)
        return response

    _expect(handler, None, "no frame in time")


def test_a_doorstation_that_does_not_answer_in_time_is_told_from_one_that_sends_no_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doorstation_stream, "DOORSTATION_DIRECT_TIMEOUT_SEC", 0.3)

    async def handler(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(30)  # not even the head of the answer
        return web.Response()

    _expect(handler, None, "no answer in time")


def test_the_stream_is_asked_for_unencoded() -> None:
    """The byte cap counts bytes of the stream itself only while the Doorstation is not asked to compress."""
    seen: list[str | None] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(request.headers.get("Accept-Encoding"))
        return web.Response(body=PART_HEAD + FRAME)

    _expect(handler, FRAME, None)

    assert seen == ["identity"]


async def _live_start(url: str) -> tuple[bytes, str | None]:
    async with aiohttp.ClientSession() as session, session.get(url) as resp:
        return await doorstation_stream.live_stream_start(resp)


def test_a_live_stream_is_passed_on_once_it_delivered_a_whole_frame_and_with_everything_read() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
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
        return web.Response(body=b"")

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", "the stream ended without a frame")

    _serve(handler, check)


def test_a_live_stream_that_ends_inside_its_first_frame_is_a_failure() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + FRAME[:-1])

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", "the stream ended without a frame")

    _serve(handler, check)


def test_a_live_stream_that_sends_a_head_and_no_picture_is_a_failure_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doorstation_stream, "DOORSTATION_DIRECT_TIMEOUT_SEC", 0.3)

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
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
        response = web.StreamResponse()
        await response.prepare(request)  # the head of the answer, then silence
        await asyncio.sleep(30)
        return response

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", "no frame in time")

    _serve(handler, check)


def test_a_live_stream_with_a_malformed_frame_is_a_failure_that_says_so() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + bytes.fromhex("ffd8ffd9"))

    async def check(url: str) -> None:
        prefix, cause = await _live_start(url)
        assert prefix == b""
        assert cause is not None
        assert cause.startswith("malformed frame (")

    _serve(handler, check)


@pytest.mark.parametrize(
    ("status", "headers", "cause"),
    [(404, {}, "HTTP 404"), (200, {"Content-Encoding": "gzip"}, "the stream came encoded")],
)
def test_a_live_stream_answer_that_is_not_a_stream_is_a_failure(
    status: int, headers: dict[str, str], cause: str
) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(status=status, body=PART_HEAD + FRAME, headers=headers)

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", cause)

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


def test_an_unreachable_doorstation_is_a_failure_that_names_no_address() -> None:
    async def run() -> tuple[bytes | None, str | None]:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # The port is free again: nothing listens, the connection is refused.
        return await _read(f"http://127.0.0.1:{port}/proxy/cam/1/")

    frame, cause = asyncio.run(run())

    assert frame is None
    assert cause == "ClientConnectorError"
    assert "127.0.0.1" not in cause
