"""A Doorstation camera read from the Doorstation itself: the first frame of its stream, and every way that fails."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiohttp import web
import pytest

from custom_components.comexio import doorstation_stream
from custom_components.comexio.doorstation_stream import (
    SESSION_CLOSED_CAUSE,
    doorstation_direct_url,
    forward_live_stream,
    live_stream_content_type,
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


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("secret 192.0.2.7"),
        BrokenPipeError("secret 192.0.2.7"),
        aiohttp.ClientPayloadError("secret 192.0.2.7"),
    ],
    ids=["ConnectionResetError", "BrokenPipeError", "ClientPayloadError"],
)
def test_a_reset_while_the_first_frame_is_read_is_a_failure_cause_not_an_exception(error: Exception) -> None:
    """The raw OS error and aiohttp's own both end the start with a cause (so the caller falls back and pauses)."""
    resp = SimpleNamespace(content=SimpleNamespace(read=AsyncMock(side_effect=error)))

    frame, cause = asyncio.run(doorstation_stream.collect_frame(resp, bytearray()))  # type: ignore[arg-type]

    assert frame is None
    assert cause == type(error).__name__
    assert "192.0.2.7" not in cause


@pytest.mark.parametrize("error", [ConnectionResetError("secret 192.0.2.7"), aiohttp.ClientPayloadError("x")])
def test_a_reset_while_the_still_request_is_made_is_a_failure_cause_not_an_exception(error: Exception) -> None:
    session = MagicMock()
    session.get.side_effect = error

    frame, cause = asyncio.run(read_direct_frame(session, "http://192.0.2.7/proxy/cam/1/"))

    assert frame is None
    assert cause == type(error).__name__


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


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("multipart/x-mixed-replace; boundary=frame", 'multipart/x-mixed-replace;boundary="frame"'),
        ("Multipart/X-Mixed-Replace;boundary=x", 'multipart/x-mixed-replace;boundary="x"'),
        ('multipart/x-mixed-replace; boundary="my-b.1"; charset=utf-8', 'multipart/x-mixed-replace;boundary="my-b.1"'),
        ("multipart/x-mixed-replace; boundary=" + "b" * 70, f'multipart/x-mixed-replace;boundary="{"b" * 70}"'),
        ("multipart/x-mixed-replace", None),
        ("multipart/x-mixed-replace; boundary=", None),
        ("multipart/x-mixed-replace; boundary=" + "b" * 71, None),
        ('multipart/x-mixed-replace; boundary=a"b', None),
        ("multipart/x-mixed-replace; boundary=a<script>", None),
        ("multipart/x-mixed-replace; boundary=a b", None),
        ("multipart/x-mixed-replace; boundary=a\r\nSet-Cookie: x=1", None),
        ("text/html; boundary=frame", None),
        ("", None),
        ('multipart/x-mixed-replace; boundary="frame', None),
        ("multipart/x-mixed-replace; boundary=frame;", 'multipart/x-mixed-replace;boundary="frame"'),
        ("multipart/x-mixed-replace; xboundary=frame", None),
        ('multipart/x-mixed-replace; boundary=" "', None),
        ("multipart/x-mixed-replace; boundary=K", None),
        ("multipart/x-mixed-replace; boundary=a; boundary=b", 'multipart/x-mixed-replace;boundary="a"'),
        ("multipart/x-mixed-replace; BOUNDARY=Frame", 'multipart/x-mixed-replace;boundary="Frame"'),
        (
            "multipart/x-mixed-replace; boundary=a'()+_,-./:=?Z9",
            'multipart/x-mixed-replace;boundary="a\'()+_,-./:=?Z9"',
        ),
        ("multipart/x-mixed-replace; boundary=frame ", 'multipart/x-mixed-replace;boundary="frame"'),
        ("multipart/x-mixed-replace; boundary=\u017f", None),
        ("multipart/x-mixed-replace; boundary=; boundary=ok", None),
        ("multipart/x-mixed-replace; boundary=a<b>; boundary=ok", None),
        ('multipart/x-mixed-replace; boundary="a b"; boundary=ok', None),
    ],
    ids=[
        "plain",
        "case and no blank",
        "quoted with a parameter after it",
        "longest boundary",
        "no boundary",
        "empty boundary",
        "boundary too long",
        "quote inside",
        "markup inside",
        "blank inside",
        "header injection",
        "other type",
        "no header",
        "unterminated quote",
        "trailing semicolon",
        "boundary only as the end of another name",
        "quoted blank",
        "kelvin sign is no ascii letter",
        "duplicate boundary, the first counts",
        "capitals",
        "every special character",
        "trailing blank",
        "long s is no ascii letter",
        "empty first boundary, a usable second one is no way round",
        "unusable first boundary, a usable second one is no way round",
        "quoted blank in the first boundary, a usable second one is no way round",
    ],
)
def test_the_type_a_live_stream_is_passed_on_with_is_rebuilt_from_a_clean_boundary(
    header: str, expected: str | None
) -> None:
    """The frontend never gets the Doorstation's header as it came: a boundary of anything but plain characters is no
    stream (the type and the boundary are all that is taken over)."""
    assert live_stream_content_type(SimpleNamespace(headers={"Content-Type": header})) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("content_type", "cause"),
    [
        ("multipart/x-mixed-replace", "no boundary"),
        ("multipart/x-mixed-replace; boundary=a<b>", "boundary with unsupported characters"),
        ("multipart/x-mixed-replace; xboundary=frame", "no boundary"),
        ("multipart/x-mixed-replace; boundary=", "no boundary"),
    ],
    ids=["no boundary", "unsupported characters", "only the end of another name", "empty boundary"],
)
def test_a_live_stream_answer_without_a_usable_boundary_is_not_passed_on(content_type: str, cause: str) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=PART_HEAD + FRAME, headers={"Content-Type": content_type})

    async def check(url: str) -> None:
        assert await _live_start(url) == (b"", f"the answer is not an MJPEG stream ({cause})")

    _serve(handler, check)


class _Chunks:
    """A stream that hands out `chunks` one by one, then ends, raises `error`, or (stall=True) never answers."""

    def __init__(self, chunks: list[bytes], stall: bool = False, error: Exception | None = None) -> None:
        self._chunks = list(chunks)
        self._stall = stall
        self._error = error

    async def read(self, n: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if self._error is not None:
            raise self._error
        if self._stall:
            await asyncio.sleep(30)
        return b""


class _Forwarded(NamedTuple):
    headers: dict[str, str]
    body: bytes
    ended: list[tuple[str, float, bool]]
    cut_off: bool  # the viewer's connection was closing when the forwarder returned


def _forwarded(stream: _Chunks, running: bool = True) -> _Forwarded:
    """What a viewer gets from `forward_live_stream` for `stream`, and what it told `on_ended`."""
    sent: list[tuple[dict[str, str], bytes]] = []
    ended: list[tuple[str, float, bool]] = []
    closing: list[bool] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        response = await forward_live_stream(
            SimpleNamespace(is_running=running),  # type: ignore[arg-type]
            request,
            stream,  # type: ignore[arg-type]
            'multipart/x-mixed-replace;boundary="frame"',
            lambda why, ran_sec, doorstation_to_blame: ended.append((why, ran_sec, doorstation_to_blame)),
        )
        closing.append(request.transport is None or request.transport.is_closing())
        return response

    async def check(url: str) -> None:
        async with asyncio.timeout(5), aiohttp.ClientSession() as session, session.get(url) as resp:
            try:
                body = await resp.read()
            except aiohttp.ClientError:
                body = b""
            sent.append((dict(resp.headers), body))

    _serve(handler, check)
    return _Forwarded(*sent[0], ended, closing[0])


def test_a_live_stream_is_forwarded_with_headers_that_keep_a_browser_from_running_it() -> None:
    forwarded = _forwarded(_Chunks([PART_HEAD + FRAME, b"more"]))

    assert forwarded.body == PART_HEAD + FRAME + b"more"
    assert forwarded.headers["Content-Type"] == 'multipart/x-mixed-replace;boundary="frame"'
    assert forwarded.headers["X-Content-Type-Options"] == "nosniff"
    assert forwarded.headers["Content-Security-Policy"] == "sandbox"
    assert not forwarded.cut_off


def _clock(monkeypatch: pytest.MonkeyPatch, *readings: float) -> None:
    """The forwarder's clock reads `readings` one by one: start, then the end of every chunk passed on."""
    monkeypatch.setattr(doorstation_stream, "time", SimpleNamespace(monotonic=iter(readings).__next__))


def _reported(forwarded: _Forwarded) -> list[tuple[str, float, bool]]:
    return forwarded.ended


def test_a_live_stream_the_doorstation_ends_is_reported_as_its_doing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clock(monkeypatch, 100.0, 107.5)

    forwarded = _forwarded(_Chunks([PART_HEAD + FRAME]))

    assert _reported(forwarded) == [(doorstation_stream.ENDED_BY_DOORSTATION, 7.5, True)]


def test_a_live_stream_that_stalls_ends_quietly_with_what_came(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doorstation_stream, "DOORSTATION_LIVE_STREAM_READ_TIMEOUT_SEC", 0.05)
    _clock(monkeypatch, 100.0, 100.5)

    forwarded = _forwarded(_Chunks([PART_HEAD + FRAME], stall=True))

    assert forwarded.body == PART_HEAD + FRAME
    # The stall itself is no time the Doorstation delivered: it counts up to the last chunk.
    assert _reported(forwarded) == [(doorstation_stream.ENDED_STALLED, 0.5, True)]


@pytest.mark.parametrize(
    "error",
    [aiohttp.ClientPayloadError("secret 192.0.2.7"), ConnectionResetError("secret 192.0.2.7")],
    ids=["ClientError", "ConnectionError"],
)
def test_a_live_stream_that_breaks_off_names_the_error_class_only(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _clock(monkeypatch, 100.0, 103.0)

    forwarded = _forwarded(_Chunks([PART_HEAD + FRAME], error=error))

    assert forwarded.body == PART_HEAD + FRAME
    assert _reported(forwarded) == [(f"the stream broke off ({type(error).__name__})", 3.0, True)]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("gone"),
        aiohttp.ClientConnectionResetError("gone"),
        aiohttp.ClientPayloadError("gone"),
    ],
    ids=["ConnectionResetError", "ClientConnectionResetError", "ClientError that is no ConnectionError"],
)
def test_a_viewer_that_leaves_is_not_blamed_on_the_doorstation(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    async def refuse(self: web.StreamResponse, data: bytes) -> None:
        raise error

    monkeypatch.setattr(web.StreamResponse, "write", refuse)
    _clock(monkeypatch, 100.0, 104.0)

    forwarded = _forwarded(_Chunks([PART_HEAD + FRAME]))

    # The chunk arrived from the Doorstation (at 104), so it counts as delivered although the viewer could not take it.
    assert _reported(forwarded) == [(doorstation_stream.ENDED_VIEWER_LEFT, 4.0, False)]


def test_a_viewer_that_stops_reading_ends_the_stream_and_is_not_blamed_on_the_doorstation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that never finishes is cut off: it would hold the stream, its count and the Doorstation's connection."""

    async def hang(self: web.StreamResponse, data: bytes) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(web.StreamResponse, "write", hang)
    monkeypatch.setattr(doorstation_stream, "DOORSTATION_LIVE_STREAM_WRITE_TIMEOUT_SEC", 0.05)
    _clock(monkeypatch, 100.0, 103.0)

    forwarded = _forwarded(_Chunks([PART_HEAD + FRAME]))

    # The time the write hung is no time of the Doorstation's: the chunk is stamped when it arrived.
    assert _reported(forwarded) == [(doorstation_stream.ENDED_VIEWER_STALLED, 3.0, False)]
    # aiohttp would drain the returned response against the same full buffer: the connection is aborted instead.
    assert forwarded.cut_off


def test_a_slow_write_within_the_limit_does_not_end_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    original = web.StreamResponse.write

    async def slow(self: web.StreamResponse, data: bytes) -> None:
        await asyncio.sleep(0.01)
        await original(self, data)

    monkeypatch.setattr(web.StreamResponse, "write", slow)
    monkeypatch.setattr(doorstation_stream, "DOORSTATION_LIVE_STREAM_WRITE_TIMEOUT_SEC", 0.5)
    _clock(monkeypatch, 100.0, 101.0)

    forwarded = _forwarded(_Chunks([PART_HEAD + FRAME]))

    assert forwarded.body == PART_HEAD + FRAME
    assert _reported(forwarded) == [(doorstation_stream.ENDED_BY_DOORSTATION, 1.0, True)]
    assert not forwarded.cut_off


def test_a_cancelled_request_is_reported_as_the_viewer_leaving_and_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser closes the tab: Home Assistant cancels the handler."""
    monkeypatch.setattr(web.StreamResponse, "prepare", AsyncMock())
    monkeypatch.setattr(web.StreamResponse, "write", AsyncMock())
    _clock(monkeypatch, 100.0, 102.0)
    ended: list[tuple[str, float, bool]] = []
    forwarding = forward_live_stream(
        SimpleNamespace(is_running=True),  # type: ignore[arg-type]
        MagicMock(),
        _Chunks([b"picture"], error=asyncio.CancelledError()),  # type: ignore[arg-type]
        'multipart/x-mixed-replace;boundary="frame"',
        lambda why, seconds, blame: ended.append((why, seconds, blame)),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(forwarding)

    assert ended == [(doorstation_stream.ENDED_VIEWER_LEFT, 2.0, False)]


def test_a_live_stream_is_not_read_while_home_assistant_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _Chunks([PART_HEAD + FRAME])
    _clock(monkeypatch, 100.0)

    forwarded = _forwarded(stream, running=False)

    assert forwarded.body == b""
    assert forwarded.headers["Content-Security-Policy"] == "sandbox"
    assert _reported(forwarded) == [(doorstation_stream.ENDED_HA_STOPPING, 0.0, False)]
    assert len(stream._chunks) == 1  # nothing was read


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


def test_a_prefetched_stream_drops_what_arrives_after_its_camera_went() -> None:
    """The read can wait long: data that comes in once the camera is gone is stale and must not reach the viewer."""
    alive = [True]

    class _Rest:
        async def read(self, n: int = -1) -> bytes:
            alive[0] = False  # the camera goes while the read waits
            return b"late"

    stream = doorstation_stream.PrefetchedStream(b"first", _Rest(), lambda: alive[0])  # type: ignore[arg-type]

    async def read() -> tuple[bytes, bytes]:
        return await stream.read(100), await stream.read(100)

    assert asyncio.run(read()) == (b"first", b"")


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
