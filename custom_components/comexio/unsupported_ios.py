"""IOs that get no entity yet because their type is not supported (#196): rule, Repair, GitHub report.

Comexio's IO type table marks some meter readings as outputs ($IOInputTypes input false), e.g. the
Smart Meter's energy counters E1-E3 (Wh). As a writable number such an IO would offer a set point
for a reading, so an output with a unit of IO_MEASUREMENT_ONLY_UNITS gets no entity at all. A
Repair names those IOs; its link opens a GitHub issue pre-filled with the extension and IO types
(no names, host or credentials) and asks for the diagnostics file, so the type can be supported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import logging
from typing import TYPE_CHECKING, Any

from aiocomexio import __version__ as AIOCOMEXIO_VERSION
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN, IO_MEASUREMENT_ONLY_UNITS, ISSUE_TRACKER_URL, ISSUE_UNSUPPORTED_IO_TYPES, one_line
from .reference_monitor import async_integration_version, github_issue_url

if TYPE_CHECKING:
    from .api import ComexioAPI

_LOGGER = logging.getLogger(__name__)

REASON_MEASUREMENT_OUTPUT = "meter reading marked as output"
# Hardware fields of an extension worth reporting; its serial number (Identifier) and its free-text
# Location stay out.
EXTENSION_META_KEYS = ("Id", "Name", "TypeId", "Mode", "Pos", "ConnectionMethod", "Wireless")
# Fields of an IO row of $FubModules["1"] as Comexio delivers them; the free-text Description stays
# out. A field not listed here is reported by name only (RAW_IO_OTHER_KEYS), its value stays out.
RAW_IO_KEYS = (
    "Id",
    "ExtensionId",
    "extensionName",
    "Identifier",
    "InOutputTypeId",
    "InOutputIdentifierTypeId",
    "CategoryId",
    "IntIsOutput",
    "Active",
    "ConnectionType",
    "Flags",
    "Timeout",
    "TimeoutGroup",
)
RAW_IO_PRIVATE_KEYS = frozenset({"Description"})
RAW_IO_OTHER_KEYS = "other_keys"
# Value types copied from a listed field; a list or object (text of any shape) is reported by name.
RAW_SCALAR_TYPES = (str, int, float, bool, type(None))
# IOs the Repair text lists by name; the rest is counted.
REPAIR_IO_LIST_LIMIT = 30


def unsupported_io_issue_id(server_id: str) -> str:
    """The Repair's issue id of one server."""
    return f"{ISSUE_UNSUPPORTED_IO_TYPES}_{server_id}"


def unsupported_io_reason(io: Mapping[str, Any]) -> str | None:
    """Why an IO gets no entity, None if it gets one."""
    if io.get("is_binary") or io.get("is_input", True):
        return None
    return REASON_MEASUREMENT_OUTPUT if io.get("unit") in IO_MEASUREMENT_ONLY_UNITS else None


def io_platform(io: Mapping[str, Any]) -> str | None:
    """The platform of an IO's entity, None if it gets none (unsupported_io_reason)."""
    if unsupported_io_reason(io):
        return None
    is_input = io.get("is_input", True)
    if io.get("is_binary"):
        return "binary_sensor" if is_input else "switch"
    return "sensor" if is_input else "number"


def _raw_extensions(raw_config: Mapping[str, Any]) -> Mapping[str, Any]:
    """The raw $FubModules["1"] (extension id -> extension meta and IO rows), empty if unreadable."""
    modules = raw_config.get("FubModules")
    extensions = modules.get("1") if isinstance(modules, Mapping) else None
    return extensions if isinstance(extensions, Mapping) else {}


def _scalar_fields(row: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """The listed fields of a row whose value is a scalar."""
    return {key: row[key] for key in keys if key in row and isinstance(row[key], RAW_SCALAR_TYPES)}


def _raw_io_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One IO row's scalar RAW_IO_KEYS fields, plus the names of the other fields (Description aside)."""
    entry = _scalar_fields(row, RAW_IO_KEYS)
    if other := sorted(str(key) for key in row if key not in entry and key not in RAW_IO_PRIVATE_KEYS):
        entry[RAW_IO_OTHER_KEYS] = other
    return entry


def raw_io_rows(raw_config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every IO row of the raw $FubModules["1"] with its RAW_IO_KEYS fields, imported by HA or not."""
    rows: list[dict[str, Any]] = []
    for content in _raw_extensions(raw_config).values():
        inoutput = content.get("inoutput") if isinstance(content, Mapping) else None
        if isinstance(inoutput, Mapping):
            rows += [_raw_io_row(row) for row in inoutput.values() if isinstance(row, Mapping)]
    return rows


def extension_metas(raw_config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Extension name -> its EXTENSION_META_KEYS fields, from the raw $FubModules["1"]."""
    metas: dict[str, dict[str, Any]] = {}
    for ext_id, content in _raw_extensions(raw_config).items():
        meta = content.get("extension") if isinstance(content, Mapping) else None
        if not isinstance(meta, Mapping):
            meta = {}
        name = meta.get("Name")
        # The fallback name is the one aiocomexio's parse_config gives the IOs' ext_name.
        metas[name if isinstance(name, str) else f"Ext{ext_id}"] = _scalar_fields(meta, EXTENSION_META_KEYS)
    return metas


def io_type_rows(api: ComexioAPI, type_id: Any) -> dict[str, Any]:
    """The IO type's rows as Comexio delivers them ($IOTypesBinary, $IOInputTypes), unparsed."""
    return {"type": api.io_types.get(str(type_id)), "input": api.io_input_types.get(str(type_id))}


def build_unsupported_io_report(
    ios: Iterable[Mapping[str, Any]],
    extensions: Mapping[str, Mapping[str, Any]],
    api: ComexioAPI,
    integration_version: str | None,
) -> tuple[str, str]:
    """(title, markdown body) of a GitHub issue about IOs without an entity.

    Holds versions, type ids and identifiers (E1, Q2, ...) only — no extension or IO names, host or
    credentials — so the user can submit it as-is after reviewing.
    """
    groups: dict[tuple[Any, Any], list[str]] = {}
    for io in ios:
        ext_type = extensions.get(str(io.get("ext_name")), {}).get("TypeId")
        groups.setdefault((ext_type, io.get("type_id_raw")), []).append(one_line(io.get("identifier")))
    ext_types = sorted({str(group_key[0]) for group_key in groups})
    lines = [
        '_Created from the Home Assistant repair "Comexio IOs without an entity"._',
        "_Erstellt aus der Home-Assistant-Reparaturmeldung „Comexio-IOs ohne Entität“._",
        "",
        "### Versions / Versionen",
        f"- Integration: {integration_version or '?'}",
        f"- aiocomexio: {AIOCOMEXIO_VERSION}",
        f"- Home Assistant: {HA_VERSION}",
        f"- Comexio: {api.comexio_version or '?'}",
        "",
        "### IOs without an entity / IOs ohne Entität",
    ]
    for (ext_type, type_id), identifiers in groups.items():
        lines += [
            f"- Extension type {ext_type}, IO type {type_id}: {', '.join(identifiers)}",
            f"  - `{json.dumps(io_type_rows(api, type_id), ensure_ascii=False, default=str)}`",
        ]
    lines += [
        "",
        "### Diagnostics and debug log / Diagnosedaten und Debug-Log",
        "Please attach the diagnostics file: Settings → Devices & services → Comexio → ⋮ → Download diagnostics.",
        "It contains the extension names of your installation — please review it before attaching.",
        "",
        "And a debug log: on the same page the top ⋮ menu → Enable debug logging, then the entry's ⋮ → Reload,",
        "wait until the integration is loaded and choose the top ⋮ menu → Disable debug logging;",
        "Home Assistant downloads the log.",
        "The log may contain names and addresses of your installation — please review it before attaching.",
        "",
        "---",
        "",
        "Bitte die Diagnosedatei anhängen: Einstellungen → Geräte & Dienste → Comexio → ⋮ →",
        "Diagnosedaten herunterladen.",
        "Sie enthält die Namen der Erweiterungen der Installation — bitte vor dem Anhängen durchsehen.",
        "",
        "Und ein Debug-Log: auf derselben Seite oberes ⋮-Menü → Debug-Protokollierung aktivieren,",
        "dann ⋮ des Eintrags → Neu laden, warten, bis die Integration geladen ist,",
        "und im oberen ⋮-Menü Debug-Protokollierung deaktivieren;",
        "Home Assistant lädt das Log herunter.",
        "Das Log kann Namen und Adressen der Installation enthalten — bitte vor dem Anhängen durchsehen.",
    ]
    return f"Comexio IO types not supported (extension type {', '.join(ext_types)})", "\n".join(lines)


def _repair_io_list(ios: list[Mapping[str, Any]]) -> str:
    """The IOs by extension, e.g. "SmartMeter: E1, E2; EO1: Q3", for the Repair text."""
    by_extension: dict[str, list[str]] = {}
    for io in ios[:REPAIR_IO_LIST_LIMIT]:
        by_extension.setdefault(one_line(io.get("ext_name")), []).append(one_line(io.get("identifier")))
    text = "; ".join(f"{ext}: {', '.join(identifiers)}" for ext, identifiers in by_extension.items())
    return text if len(ios) <= REPAIR_IO_LIST_LIMIT else f"{text}; … {len(ios) - REPAIR_IO_LIST_LIMIT} more"


class UnsupportedIoMonitor:
    """Per-entry state: the extensions of the last readable poll and the Repair for IOs without an entity."""

    def __init__(self, hass: HomeAssistant, api: ComexioAPI, server_id: str) -> None:
        self._hass = hass
        self._api = api
        self._server_id = server_id
        self._integration_version: str | None = None
        self._reported: tuple[str, ...] = ()
        self._last_error: str | None = None
        # The raw Comexio data of the last readable poll and when it was read (None: never), for the diagnostics.
        self.extensions: dict[str, dict[str, Any]] = {}
        self.raw_ios: list[dict[str, Any]] = []
        self.raw_read_at: str | None = None
        # The exception type of the last failed check (None: it succeeded); its text may quote a row.
        self.last_error_type: str | None = None

    @property
    def issue_id(self) -> str:
        return unsupported_io_issue_id(self._server_id)

    async def async_update(self, raw_config: Mapping[str, Any], ios: Iterable[Mapping[str, Any]]) -> None:
        """Raise, refresh or clear the Repair from this poll's IOs; never raises.

        A side check: a malformed row must not fail the poll that delivers every entity's value. A
        poll without $FubModules (a failed scrape) is no check: it keeps the last state, error included.
        """
        if not raw_config.get("FubModules"):
            _LOGGER.debug("[%s] No module list on the config page, keeping the last state", self._server_id)
            return
        try:
            await self._async_update(raw_config, ios)
        except Exception as err:
            # Once per cause with the traceback: a row broken on every poll must not flood the log.
            if (cause := f"{type(err).__name__}: {err}") != self._last_error:
                _LOGGER.exception("[%s] Check for IOs without an entity failed", self._server_id)
            else:
                _LOGGER.debug("[%s] Check for IOs without an entity failed again: %s", self._server_id, cause)
            self._last_error, self.last_error_type = cause, type(err).__name__
        else:
            self._last_error = self.last_error_type = None

    async def _async_update(self, raw_config: Mapping[str, Any], ios: Iterable[Mapping[str, Any]]) -> None:
        """A poll without the IO type table keeps the last Repair state, but still reads the raw data.

        Its IO list says nothing about the types (no table: no units, every IO an input), and
        clearing the Repair would only flash it until the next poll.
        """
        # Both read before either is stored: a failure leaves the last poll's pair intact.
        extensions, raw_ios = extension_metas(raw_config), raw_io_rows(raw_config)
        self.extensions, self.raw_ios = extensions, raw_ios
        self.raw_read_at = dt_util.utcnow().isoformat()
        if not self._api.io_types:
            _LOGGER.debug("[%s] No IO type table on the config page, keeping the last state", self._server_id)
            return
        unsupported = [io for io in ios if unsupported_io_reason(io)]
        labels = tuple(f"{one_line(io.get('ext_name'))} {one_line(io.get('identifier'))}" for io in unsupported)
        if not unsupported:
            if self._reported:
                _LOGGER.info("[%s] Repair cleared: no imported IO of an unsupported type left", self._server_id)
            # Unconditional (a no-op without an issue): an issue raised before an entry reload
            # must also be cleared by the fresh monitor, whose state starts out empty.
            ir.async_delete_issue(self._hass, DOMAIN, self.issue_id)
            self._reported = ()
            return
        if self._integration_version is None:
            self._integration_version = await async_integration_version(self._hass)
        title, body = build_unsupported_io_report(unsupported, self.extensions, self._api, self._integration_version)
        issue_url = github_issue_url(ISSUE_TRACKER_URL, title, body)
        # Re-created on every poll: async_create_issue updates the existing issue in place.
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self.issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_UNSUPPORTED_IO_TYPES,
            learn_more_url=issue_url,
            translation_placeholders={
                "server_id": self._server_id,
                "units": ", ".join(sorted(IO_MEASUREMENT_ONLY_UNITS)),
                "ios": _repair_io_list(unsupported),
                "issue_url": issue_url,
            },
        )
        if labels != self._reported:
            _LOGGER.warning(
                "[%s] %d IOs get no entity, their type is not supported yet (see Repairs): %s",
                self._server_id,
                len(labels),
                ", ".join(labels),
            )
        self._reported = labels
