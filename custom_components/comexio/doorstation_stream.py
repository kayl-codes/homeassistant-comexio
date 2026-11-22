"""A Doorstation camera read from the Doorstation itself, not through the IO-Server.

The Doorstation serves its camera as an MJPEG stream under ``http://<its address>/proxy/cam/1/``, with no
login (confirmed on a real installation, kayl-codes/homeassistant-comexio#180). aiocomexio reads the stream only
over the IO-Server and its admin session, so the first frame is read here, over a plain Home Assistant web session,
with aiocomexio's JPEG frame reader. Nothing here touches Home Assistant, so it is tested without it.
"""

import asyncio
from http import HTTPStatus

from aiocomexio.doorstation import JpegFrameReader, doorstation_stream_path
from aiocomexio.exceptions import ComexioDataError
import aiohttp

from .const import (
    DOORSTATION_DIRECT_MAX_BYTES,
    DOORSTATION_DIRECT_TIMEOUT_SEC,
    DOORSTATION_OWN_CAMERA_ID,
    one_line,
)

# The stream is asked for unencoded, so the byte cap counts bytes of the stream itself (a decompressed chunk would be
# allocated before any cap could apply); a stream that comes encoded anyway is refused before a byte of it is read.
# The request is made with auto_decompress=False for the same reason: aiohttp would otherwise inflate what arrives
# before `response_problem` has looked at Content-Encoding.
DIRECT_REQUEST_HEADERS = {"Accept-Encoding": "identity"}
# The request to the Doorstation is made with allow_redirects=False: its answer comes over plain HTTP, and a
# redirect would make Home Assistant call whatever address a device on the network names. A redirect counts as
# an error status (response_problem).
_CHUNK_BYTES = 64 * 1024
_AIOHTTP_SESSION_CLOSED = "Session is closed"  # the text of aiohttp's refusal of a request on a closed session
SESSION_CLOSED_CAUSE = "the session was closed"


def doorstation_direct_url(address: str) -> str:
    """The URL of the camera stream on the Doorstation at `address` (an IPv4 address, see ExtensionInfo.ip).

    The Doorstation numbers its own cameras from 1. The IO-Server's camera id of the Doorstation is another
    number (the extension id), valid only on the IO-Server's proxy.
    """
    return f"http://{address}{doorstation_stream_path(DOORSTATION_OWN_CAMERA_ID)}"


def response_problem(resp: aiohttp.ClientResponse) -> str | None:
    """Why the answer of the Doorstation is not a stream to read (a short text, no address in it), None if it is."""
    if resp.status != HTTPStatus.OK:
        return f"HTTP {resp.status}"
    if resp.headers.get(aiohttp.hdrs.CONTENT_ENCODING):
        return "the stream came encoded"
    return None


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
        # aiohttp refuses a request on a closed session with this RuntimeError (Home Assistant closes its shared
        # session when it stops); any other RuntimeError is a bug and goes through, a closed session or not.
        if not session.closed or str(err) != _AIOHTTP_SESSION_CLOSED:
            raise
        return None, SESSION_CLOSED_CAUSE
