"""Comexio 1-Wire sensor states kept between polls.

Pure state, no I/O: the coordinator feeds each status poll in through apply_status() (see
coordinator._async_onewire_tick); the configured sensors come from the config poll.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from aiocomexio.config import TYPE_DIGITAL
from aiocomexio.onewire import OneWireSensor, OneWireStatus

from .const import ONEWIRE_FAIL_STREAK_THRESHOLD, one_line

# Problem kinds of OneWireState.problems(), also the problem sensor's attribute names.
PROBLEM_READ_ERROR = "read_error"
PROBLEM_NOT_FOUND = "not_found"
# Configured, but the 1-Wire monitor does not list it at all.
PROBLEM_MISSING = "missing"

# What a status poll changed, as OneWireState.apply_status() reports it; the caller logs each once.
TRANSITION_DROPPED = "dropped"
TRANSITION_RECOVERED = "recovered"


def onewire_platform(sensor: OneWireSensor) -> str:
    """Platform of a 1-Wire sensor's entity: binary_sensor for a digital one, else sensor (also for an unknown kind)."""
    return "binary_sensor" if sensor.kind == TYPE_DIGITAL else "sensor"


def onewire_entity_name(sensor: OneWireSensor) -> str:
    """Entity name of a 1-Wire sensor: its address and, if Comexio has one, its name ("OT1 Kitchen")."""
    label = one_line(sensor.name)
    return f"{sensor.address} {label}" if label else sensor.address


@dataclass
class OneWireState:
    """Latest 1-Wire status per address; statuses is None while unknown.

    A failed poll keeps the last statuses until ONEWIRE_FAIL_STREAK_THRESHOLD polls in a row failed,
    then drops them — a lasting outage shows as "unavailable", not as a stale value.
    """

    statuses: dict[str, OneWireStatus] | None = None
    _fail_streak: int = field(default=0, repr=False)

    def apply_status(self, statuses: Mapping[str, OneWireStatus] | None) -> str | None:
        """Take one status poll (None = it failed) and report the change, if any.

        TRANSITION_DROPPED once ONEWIRE_FAIL_STREAK_THRESHOLD polls in a row failed (also before any poll worked: the
        statuses stay unknown then), TRANSITION_RECOVERED on the first poll that works after that, else None — so an
        outage is logged once when it starts and once when it ends.
        """
        if statuses is None:
            self._fail_streak += 1
            if self._fail_streak == ONEWIRE_FAIL_STREAK_THRESHOLD:
                self.statuses = None
                return TRANSITION_DROPPED
            return None
        recovered = self._fail_streak >= ONEWIRE_FAIL_STREAK_THRESHOLD
        self._fail_streak = 0
        self.statuses = dict(statuses)
        return TRANSITION_RECOVERED if recovered else None

    def status(self, address: str) -> OneWireStatus | None:
        """The last status of ``address``, None while unknown or not listed by the monitor."""
        return None if self.statuses is None else self.statuses.get(address)

    def problems(self, sensors: Mapping[str, OneWireSensor]) -> dict[str, list[str]] | None:
        """The configured sensors with a problem, per kind (sorted addresses); None while the statuses are unknown."""
        if self.statuses is None:
            return None
        found: dict[str, list[str]] = {PROBLEM_READ_ERROR: [], PROBLEM_NOT_FOUND: [], PROBLEM_MISSING: []}
        for address in sorted(sensors):
            if (status := self.statuses.get(address)) is None:
                found[PROBLEM_MISSING].append(address)
            elif not status.visible:
                found[PROBLEM_NOT_FOUND].append(address)
            elif not status.online:
                found[PROBLEM_READ_ERROR].append(address)
        return found
