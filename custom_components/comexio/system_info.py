"""Comexio system info readings (free storage, time server, boot time) kept between polls.

Pure state, no I/O: the coordinator's system info poll feeds every result in through apply().
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from aiocomexio import SystemInfo, TimeServerSettings

from .const import SYSTEM_BOOT_TIME_TOLERANCE_SEC, SYSTEM_INFO_FAIL_STREAK_THRESHOLD


@dataclass
class SystemInfoState:
    """Latest system info readings; each is None while unknown.

    A failed fetch keeps the last readings until SYSTEM_INFO_FAIL_STREAK_THRESHOLD polls in a row
    failed, then drops them — a lasting outage shows as "unknown", not as a stale value.
    """

    free_storage_percent: float | None = None
    time_server: TimeServerSettings | None = None
    boot_time: datetime | None = None
    _info_fail_streak: int = 0
    _uptime_fail_streak: int = 0

    def apply(self, info: SystemInfo | None, uptime: timedelta | None, now: datetime) -> bool:
        """Take one poll's results (None = that fetch failed); True once a reading just turned unknown."""
        dropped = False
        if info is None:
            self._info_fail_streak += 1
            if self._info_fail_streak == SYSTEM_INFO_FAIL_STREAK_THRESHOLD:
                self.free_storage_percent = None
                self.time_server = None
                dropped = True
        else:
            self._info_fail_streak = 0
            self.free_storage_percent = info.free_storage_percent
            self.time_server = info.time_server
        if uptime is None:
            self._uptime_fail_streak += 1
            if self._uptime_fail_streak == SYSTEM_INFO_FAIL_STREAK_THRESHOLD:
                self.boot_time = None
                dropped = True
        else:
            self._uptime_fail_streak = 0
            self.boot_time = stable_boot_time(self.boot_time, now - uptime)
        return dropped

    @property
    def time_server_problem(self) -> bool | None:
        """True if Comexio does not set its clock from a time server (disabled or no server entered)."""
        if self.time_server is None:
            return None
        return not self.time_server.enabled or not self.time_server.server


def stable_boot_time(known: datetime | None, estimate: datetime) -> datetime:
    """The known boot time while the new estimate lies within SYSTEM_BOOT_TIME_TOLERANCE_SEC of it.

    now (HA's clock) - uptime shifts by the request latency and the uptime's one-second resolution
    on every poll; a reboot moves it further, as does HA's own clock being set.
    """
    if known is not None and abs((estimate - known).total_seconds()) <= SYSTEM_BOOT_TIME_TOLERANCE_SEC:
        return known
    return estimate
