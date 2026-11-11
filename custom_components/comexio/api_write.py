"""Failed value writes (ComexioAPI.set_value): the counter behind the diagnostic sensor (#289)."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.util import dt as dt_util

from .const import API_WRITE_FAILURES_KEPT, one_line

_LOGGER = logging.getLogger(__name__)

ATTR_LAST_FAILURES = "last_failures"
# A target is "<type> <id>"; the id of a write from a service call can be any text, and the state machine keeps it.
_TARGET_MAX_LEN = 80


def failure_class(err: BaseException) -> str:
    """The error's class, with the class of the error behind it: 'ComexioConnectionError (ServerDisconnectedError)'."""
    own = type(err).__name__
    return own if err.__cause__ is None else f"{own} ({type(err.__cause__).__name__})"


@dataclass(frozen=True)
class WriteFailure:
    """One failed API write: when, what it was meant for, and how it failed."""

    at: datetime
    target: str
    error: str

    def as_attribute(self) -> dict[str, str]:
        return {"at": self.at.isoformat(), "target": self.target, "error": self.error}


class ApiWriteStats:
    """What went wrong with the value writes of one config entry since HA started (or the entry was loaded).

    failed counts every write set_value reported as failed, whatever the cause and whichever way it went
    (/api/, or the admin session for an IO like AO*). Kept in memory only: a restart starts at 0.
    """

    def __init__(self) -> None:
        self.failed = 0
        self._last_failures: deque[WriteFailure] = deque(maxlen=API_WRITE_FAILURES_KEPT)
        self._listeners: list[Callable[[], None]] = []

    @property
    def last_failures(self) -> tuple[WriteFailure, ...]:
        """The latest failures, oldest first."""
        return tuple(self._last_failures)

    def record_failure(self, target: str, err: BaseException) -> None:
        self.failed += 1
        self._last_failures.append(
            WriteFailure(dt_util.utcnow(), one_line(target)[:_TARGET_MAX_LEN], failure_class(err))
        )
        self._notify()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call listener after every change; returns the function that removes it again."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def attributes(self) -> dict[str, Any]:
        return {ATTR_LAST_FAILURES: [failure.as_attribute() for failure in self._last_failures]}

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            # A listener is a sensor writing its state: when that fails, the write that was counted must still
            # report its own failure to its caller.
            try:
                listener()
            except Exception:
                _LOGGER.exception("A listener of the API write errors counter failed")
