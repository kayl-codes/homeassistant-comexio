"""A Doorstation camera read from the Doorstation itself, not through the IO-Server.

The Doorstation serves its camera as an MJPEG stream under ``http://<its address>/proxy/cam/1/``, with no
login (confirmed on a real installation, kayl-codes/homeassistant-comexio#180). aiocomexio reads the stream only
over the IO-Server and its admin session, so the first frame is read here, over a plain Home Assistant web session,
with aiocomexio's JPEG frame reader; the live stream is checked here as well (status, encoding, media type, first
frame) before the camera passes it on. Apart from the forwarder's `hass.is_running` check nothing here touches
Home Assistant, so it is tested without it.
"""

import asyncio
from collections.abc import Callable
from http import HTTPStatus
import re
import time

from aiocomexio.doorstation import JpegFrameReader, doorstation_stream_path
from aiocomexio.exceptions import ComexioDataError
import aiohttp
from aiohttp import web
from homeassistant.core import HomeAssistant

from .const import (
    DOORSTATION_DIRECT_MAX_BYTES,
    DOORSTATION_DIRECT_TIMEOUT_SEC,
    DOORSTATION_LIVE_STREAM_READ_TIMEOUT_SEC,
    DOORSTATION_OWN_CAMERA_ID,
    one_line,
)

# The stream is asked for unencoded, so the byte cap counts bytes of the stream itself (a decompressed chunk would be
# allocated before any cap could apply); a stream that comes encoded anyway is refused before a byte of it is read.
# The request is made with auto_decompress=False for the same reason: aiohttp would otherwise inflate what arrives
# before `response_problem` has looked at Content-Encoding.
DIRECT_REQUEST_HEADERS = {"Accept-Encoding": "identity"}
# Both requests to the Doorstation (the still read and the live stream) are made with allow_redirects=False: its answer
# comes over plain HTTP, and a redirect would make Home Assistant call whatever address a device on the network
# names. A redirect counts as an error status (response_problem).
_CHUNK_BYTES = 64 * 1024
_AIOHTTP_SESSION_CLOSED = "Session is closed"  # the text of aiohttp's refusal of a request on a closed session
SESSION_CLOSED_CAUSE = "the session was closed"
# The only type a live stream is passed on with. The answer comes over plain HTTP from a device on the network: a type
# like text/html would let a browser run what the body holds on Home Assistant's own origin. The frontend gets a header
# built here (the type and a boundary of plain characters), never the Doorstation's own, and the response carries
# headers that keep a browser from running anything in it anyway (`forward_live_stream`).
LIVE_STREAM_MEDIA_TYPE = "multipart/x-mixed-replace"
# RFC 2046 boundary characters without the space, at most 70 of them, quoted or not (a quote must be closed). ASCII
# only: with IGNORECASE alone `[A-Za-z]` also matches U+212A and U+017F. A deliberate approximation: the first
# `boundary=` parameter wins, and a quoted value of another parameter that holds the text is not told apart.
_BOUNDARY = re.compile(
    r"""[;\s]boundary=(?:"([0-9A-Za-z'()+_,\-./:=?]{1,70})"|([0-9A-Za-z'()+_,\-./:=?]{1,70}))\s*(?:;|$)""",
    re.ASCII | re.IGNORECASE,
)
# A `boundary=` parameter with some value, usable or not: tells "no boundary" from "a boundary we do not take over".
_BOUNDARY_PARAM = re.compile(r"[;\s]boundary=\s*[^;\s]", re.ASCII | re.IGNORECASE)
LIVE_STREAM_RESPONSE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}


def doorstation_direct_url(address: str) -> str:
    """The URL of the camera stream on the Doorstation at `address` (an IPv4 address, see ExtensionInfo.ip).

    The Doorstation numbers its own cameras from 1. The IO-Server's camera id of the Doorstation is another
    number (the extension id), valid only on the IO-Server's proxy.
    """
    return f"http://{address}{doorstation_stream_path(DOORSTATION_OWN_CAMERA_ID)}"


def response_problem(resp: aiohttp.ClientResponse) -> str | None:
    """Why the answer of the Doorstation is not a stream to read (a short text, no address in it), None if it is.

    Home Assistant's own proxy helper passes any answer on as it comes, an error page included, so the live stream
    checks the answer with this before it hands it over.
    """
    if resp.status != HTTPStatus.OK:
        return f"HTTP {resp.status}"
    if resp.headers.get(aiohttp.hdrs.CONTENT_ENCODING):
        return "the stream came encoded"
    return None


def _content_type_header(resp: aiohttp.ClientResponse) -> str:
    return resp.headers.get(aiohttp.hdrs.CONTENT_TYPE, "")


def _media_type(header: str) -> str:
    return header.split(";")[0].strip().lower()


def live_stream_content_type(resp: aiohttp.ClientResponse) -> str | None:
    """The Content-Type a live stream is passed on with (the media type and the Doorstation's boundary, rebuilt from
    plain characters only), None if the answer is no multipart stream with a usable boundary."""
    header = _content_type_header(resp)
    if _media_type(header) != LIVE_STREAM_MEDIA_TYPE or (found := _BOUNDARY.search(header)) is None:
        return None
    return f'{LIVE_STREAM_MEDIA_TYPE};boundary="{found.group(1) or found.group(2)}"'


def live_stream_problem(resp: aiohttp.ClientResponse) -> str | None:
    """Why the answer is not to be passed on to the frontend as a live stream, None if it may be."""
    if (problem := response_problem(resp)) is not None:
        return problem
    if live_stream_content_type(resp) is not None:
        return None
    # The type comes from a device on the network: one line, cut short, before it reaches a log.
    header = _content_type_header(resp)
    media_type = _media_type(header)
    if media_type == LIVE_STREAM_MEDIA_TYPE:
        detail = "boundary with unsupported characters" if _BOUNDARY_PARAM.search(header) else "no boundary"
        return f"the answer is not an MJPEG stream ({detail})"
    return f"the answer is not an MJPEG stream (Content-Type {one_line(media_type)[:40] or 'missing'})"


def closed_session_refusal(session: aiohttp.ClientSession, err: RuntimeError) -> bool:
    """True if `err` is aiohttp's refusal of a request on a closed session (Home Assistant closes its shared session
    when it stops); any other RuntimeError is a bug, a closed session or not."""
    return session.closed and str(err) == _AIOHTTP_SESSION_CLOSED


async def collect_frame(resp: aiohttp.ClientResponse, received: bytearray) -> tuple[bytes | None, str | None]:
    """Read the answer into `received` until it holds a complete JPEG frame: (frame, None) or (None, failure cause).

    Gives up after DOORSTATION_DIRECT_MAX_BYTES without a frame. No time limit of its own (the caller sets one: a
    TimeoutError goes through). The cause is a short text with no address in it, fit for a log line.
    """
    reader = JpegFrameReader()
    try:
        while (left := DOORSTATION_DIRECT_MAX_BYTES - len(received)) > 0 and (
            chunk := await resp.content.read(min(left, _CHUNK_BYTES))
        ):
            received += chunk
            if (frame := reader.frame(received)) is not None:
                return frame, None
    except ComexioDataError as err:
        return None, f"malformed frame ({one_line(str(err))})"
    except aiohttp.ClientError as err:
        return None, type(err).__name__
    if len(received) >= DOORSTATION_DIRECT_MAX_BYTES:
        return None, "no frame in the first bytes of the stream"
    return None, "the stream ended without a frame"


async def live_stream_start(resp: aiohttp.ClientResponse) -> tuple[bytes, str | None]:
    """What was read of an answer that is to be passed on as a live stream, or b"" and the failure cause.

    The stream is only handed to the frontend once it has delivered a complete frame: an answer of 200 that then
    sends nothing, ends, or sends no picture is not a stream, and the frontend must not get an empty one. Everything
    read is returned (the frame and what came with it), to be passed on first. Waits DOORSTATION_DIRECT_TIMEOUT_SEC.
    """
    if (problem := live_stream_problem(resp)) is not None:
        return b"", problem
    received = bytearray()
    try:
        async with asyncio.timeout(DOORSTATION_DIRECT_TIMEOUT_SEC):
            frame, cause = await collect_frame(resp, received)
    except TimeoutError:
        return b"", "no frame in time"
    return (b"", cause) if frame is None else (bytes(received), None)


class PrefetchedStream:
    """A stream whose first bytes were read already: they come first, then the rest of the stream.

    It ends (empty read) once `alive` says no, so a stream does not outlive the camera it belongs to.
    """

    def __init__(self, first: bytes, rest: aiohttp.StreamReader, alive: Callable[[], bool] = lambda: True) -> None:
        self._first = first
        self._rest = rest
        self._alive = alive

    async def read(self, n: int = -1) -> bytes:
        """The prefetched bytes at the first call (whole, whatever n), the stream's own after that; b"" when over."""
        if not self._alive():
            return b""
        if self._first:
            first, self._first = self._first, b""
            return first
        return await self._rest.read(n)


# (why it ended, seconds the Doorstation delivered data, whether the Doorstation is to blame)
StreamEnded = Callable[[str, float, bool], None]
ENDED_BY_DOORSTATION = "the Doorstation ended the stream"
ENDED_STALLED = "the Doorstation stopped sending"
ENDED_VIEWER_LEFT = "the viewer left"
ENDED_HA_STOPPING = "Home Assistant is stopping"


class _Delivery:
    """How long a stream delivered data: from its start to the last chunk passed on. A stall at the end (the read
    timeout) is not part of it, so a device that sends one picture and then goes quiet delivered for about 0 s."""

    def __init__(self) -> None:
        self._started = self._last_data = time.monotonic()

    def chunk_passed_on(self) -> None:
        self._last_data = time.monotonic()

    @property
    def seconds(self) -> float:
        return self._last_data - self._started


async def _pump(
    hass: HomeAssistant, response: web.StreamResponse, stream: aiohttp.StreamReader, delivery: _Delivery
) -> tuple[str, bool]:
    """Pass `stream` on to `response` until it ends: (why, whether the Doorstation is to blame). No address in it."""
    while hass.is_running:
        try:
            async with asyncio.timeout(DOORSTATION_LIVE_STREAM_READ_TIMEOUT_SEC):
                data = await stream.read(_CHUNK_BYTES)
        except TimeoutError:
            return ENDED_STALLED, True
        except aiohttp.ClientError as err:
            return f"the stream broke off ({type(err).__name__})", True
        if not data:
            return ENDED_BY_DOORSTATION, True
        try:
            await response.write(data)
        except (aiohttp.ClientError, ConnectionError):
            return ENDED_VIEWER_LEFT, False
        delivery.chunk_passed_on()
    return ENDED_HA_STOPPING, False


async def forward_live_stream(
    hass: HomeAssistant,
    request: web.BaseRequest,
    stream: aiohttp.StreamReader,
    content_type: str,
    on_ended: StreamEnded,
) -> web.StreamResponse:
    """Pass the stream on to the frontend, like Home Assistant's proxy helper but with protective headers.

    The body is a device's, passed on as it comes: `nosniff` keeps a browser from taking it for another type, and the
    `sandbox` policy keeps a part that claims to be a page from running anything on Home Assistant's origin if the
    stream is opened on its own instead of in an image. The helper cannot add headers, hence this copy. Ends quietly
    (the frontend reconnects) when the Doorstation stalls (DOORSTATION_LIVE_STREAM_READ_TIMEOUT_SEC), breaks off or
    ends the stream, when the viewer leaves, and while Home Assistant stops; `on_ended` is told which of them it was,
    how long the Doorstation delivered data and whether it is to blame, so the caller can tell a device that sends one
    picture and drops or goes quiet every time from a viewer that closed the tab. A cancelled request (the viewer
    closed the tab) is reported as the viewer leaving, then the cancellation goes on.
    """
    response = web.StreamResponse(headers=LIVE_STREAM_RESPONSE_HEADERS)
    response.content_type = content_type
    await response.prepare(request)
    delivery = _Delivery()
    try:
        why, doorstation_to_blame = await _pump(hass, response, stream, delivery)
    except asyncio.CancelledError:
        on_ended(ENDED_VIEWER_LEFT, delivery.seconds, False)
        raise
    on_ended(why, delivery.seconds, doorstation_to_blame)
    return response


async def read_direct_frame(
    session: aiohttp.ClientSession, url: str, budget_sec: float = DOORSTATION_DIRECT_TIMEOUT_SEC
) -> tuple[bytes | None, str | None]:
    """The first complete JPEG frame of the stream at `url`, or None and the failure cause.

    Gives up after `budget_sec` seconds (a caller that has used up part of its budget passes the rest) and after
    DOORSTATION_DIRECT_MAX_BYTES without a frame. The cause is a short text with no address in it, fit for a log
    line.
    """
    answered = False
    try:
        async with (
            asyncio.timeout(budget_sec),
            session.get(url, headers=DIRECT_REQUEST_HEADERS, allow_redirects=False, auto_decompress=False) as resp,
        ):
            answered = True
            if (problem := response_problem(resp)) is not None:
                return None, problem
            return await collect_frame(resp, bytearray())
    except TimeoutError:
        return None, "no frame in time" if answered else "no answer in time"
    except aiohttp.ClientError as err:
        return None, type(err).__name__
    except RuntimeError as err:
        if not closed_session_refusal(session, err):
            raise
        return None, SESSION_CLOSED_CAUSE
