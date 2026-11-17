"""Doorstations of the IO-Server: which cameras the integration knows, and for how long it keeps them.

aiocomexio reads the Doorstations from the IO home page (``get_doorstations``). A changed page reads like an
installation without Doorstations (an empty result), and a failed read is no answer at all. Neither may take the
cameras away at once: the tracker keeps what it knew until the page has shown none for several polls in a row.
"""

from collections.abc import Iterable, Mapping
import logging
from typing import Any

from aiocomexio.doorstation import Doorstation

from .const import DOORSTATION_BELL_IDENTIFIERS, DOORSTATION_EMPTY_POLLS_BEFORE_DROP

_LOGGER = logging.getLogger(__name__)


class DoorstationTracker:
    """The Doorstations known from the polls, kept through failed reads and a few empty ones."""

    def __init__(self) -> None:
        self._known: dict[int, Doorstation] = {}
        self._empty_polls = 0

    @property
    def known(self) -> Mapping[int, Doorstation]:
        """The Doorstations the cameras are offered for, keyed by extension id."""
        return self._known

    def update(self, found: Mapping[int, Doorstation]) -> bool:
        """Take the result of one successful read; True if the known Doorstations changed.

        An empty result counts as one empty poll, and the known Doorstations are dropped only at
        DOORSTATION_EMPTY_POLLS_BEFORE_DROP of them in a row: a page that changed reads as no Doorstation at all
        (aiocomexio cannot tell the two apart), and a camera that goes unavailable and returns at once is worse
        than one that stays a few polls too long. Only the empty polls this method sees are counted: a failed read
        never reaches it, so it neither counts nor interrupts the series, and a persistent failure keeps the
        cameras (the camera itself then reports that no image comes). A non-empty result is taken as it is: a
        Doorstation missing from it is gone at once.
        """
        if found:
            self._empty_polls = 0
            changed = self._known != found
            self._known = dict(found)
            return changed
        self._empty_polls += 1
        if self._known and self._empty_polls >= DOORSTATION_EMPTY_POLLS_BEFORE_DROP:
            _LOGGER.info("No Doorstation on the IO home page for %d polls, dropping the known ones", self._empty_polls)
            self._known = {}
            return True
        if self._known:
            _LOGGER.debug("No Doorstation on the IO home page (%d in a row), keeping the known ones", self._empty_polls)
        return False


def doorbell_ios(ios: Iterable[Mapping[str, Any]], doorstation_ext_ids: Iterable[int | str]) -> list[Mapping[str, Any]]:
    """The bell buttons among the IOs: the digital inputs DI1-DI3 of the extensions that are Doorstations.

    An IO of another extension named DI1 (a different module may use the identifier) is no bell. The extension id
    of an IO and the one of a Doorstation are compared as text: the page gives the Doorstation an integer, the IO
    keeps the group key it was read under.
    """
    wanted = {str(ext_id) for ext_id in doorstation_ext_ids}
    return [
        io
        for io in ios
        if str(io.get("ext_id")) in wanted
        and str(io.get("identifier", "")).upper() in DOORSTATION_BELL_IDENTIFIERS
        and io.get("is_binary")
        and io.get("is_input", True)
    ]


def doorstation_ext_name(doorstation: Doorstation, ios: Iterable[Mapping[str, Any]]) -> str | None:
    """The extension name the IOs of this Doorstation carry, so its camera joins their device.

    The IOs name their extension's group key (``ext_id``) and its name; the Doorstation's own ``name`` (the
    ``data-extname`` of the page) is the fallback for one that has no IO in the data (offline, not imported).
    None if neither is known.
    """
    wanted = str(doorstation.ext_id)
    for io in ios:
        if str(io.get("ext_id")) == wanted and (name := io.get("ext_name")):
            return str(name)
    return doorstation.name
