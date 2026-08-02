# Version: 0.8.1
"""Persistent cache of Comexio's Function Plan element/block-type catalog.

Comexio's admin config page ($FubTypes / $FubModules["5"] / $FubBaseI18N, already
scraped generically by ComexioAPI.get_raw_config()) exposes the full catalog of
element types and "fubBase" logic-block templates (Oder/Und/Nicht/+/-/:/*/...).
This catalog can grow with firmware updates, so it is extracted, sanity-checked and
persisted once per poll cycle (only written on actual change) to serve as a stable
data source for future analyses/rebuild features — no extra HTTP round-trip needed,
since get_raw_config() already fetches this page every cycle for markers/IOs.

Also cached: $FubModules["4"] (timeModule instances — e.g. Einschaltwischer/
Ausschaltverzögerung used in plans like "Zeit Pläne u. Funktionen") and $FubModules["3"]
(timeFunction instances — Comexio's own I18N calls these "Kalenderfunktionen", the
Sonnenaufgang/Sonnenuntergang/Dämmerung/Wochentag triggers shown as C1, C2, ... in
Studio). Unlike the fubBase catalog these are user-configured plan elements, not a
firmware template list, so they follow different persistence rules (see time_modules/
calendar_functions handling below).

NOT cached: constants (type 16) — their value lives directly in the plan element's own
"name" field (reference.ref_id is always 0, and $FubModules["16"] is empty on a live
server), verified against live backup snapshots. There is nothing to look up here.

The persisted catalog is stamped with Comexio's own firmware/frontend version
(extracted from static asset paths, e.g. "/11.0.2/js/cmb_admin.js" — Comexio doesn't
expose a version as a JS var) so a future consumer can tell which firmware state a
cached catalog reflects, and correlate it against the same stamp on plan backup
snapshots (function_plan_backup.py).
"""

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


def _module_group(fub_modules: dict[str, Any], type_id: str) -> dict[str, Any]:
    """Return $FubModules[type_id], normalized to {} when Comexio serializes an empty
    group as a JSON array (`[]`) instead of an object — observed live for unused groups.
    """
    group = fub_modules.get(type_id, {})
    return group if isinstance(group, dict) else {}


def _extract_fub_base_catalog(fub_modules: dict[str, Any], fub_base_i18n: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build {ref_id: {name, short_name, display_name, description, in, out, categories, ...}}.

    Source: $FubModules["5"][ref_id].Name is the internal block code (e.g. "or"),
    which is the lookup key into $FubBaseI18N for the human-readable fields.
    Entries missing a Name or without an I18N match are skipped individually rather
    than failing the whole extraction.

    Geometry fields (for the SVG renderer, function_plan_render.py): "width" is Comexio's
    canvas width in block units (1=narrow Oder/Nicht, 2=Auswahl, 3=Smart-Lighting —
    NOT pixels), "n_in"/"n_out" the exact port count of THIS block variant (the four
    "or" entries are 2/3/4/5-input variants sharing one I18N text), "autogrow" the
    maximum the input count may grow to (0 = fixed). "in_types"/"out_types" list each
    port's data type in Pos order (Comexio Type field: 0=digital, 1=analog) — the
    renderer draws them as triangle vs. circle glyphs like Comexio Studio.
    "in_hide"/"out_hide" are the port positions Studio collapses by default
    (auto_hide) — the renderer omits those rows unless a hidden port is wired.
    """
    catalog: dict[str, dict[str, Any]] = {}
    for ref_id, entry in _module_group(fub_modules, "5").items():
        name = entry.get("Name")
        if not name:
            continue
        i18n = fub_base_i18n.get(name)
        if i18n is None:
            continue
        catalog[ref_id] = {
            "name": name,
            "short_name": i18n.get("short_name"),
            "display_name": i18n.get("name"),
            "description": i18n.get("description"),
            "in": i18n.get("in", {}),
            "out": i18n.get("out", {}),
            "categories": entry.get("categories", []),
            "width": entry.get("Width"),
            "height": entry.get("Height"),
            "autogrow": entry.get("Autogrow"),
            "n_in": len(entry.get("input") or []),
            "n_out": len(entry.get("output") or []),
            "in_types": _port_types(entry.get("input")),
            "out_types": _port_types(entry.get("output")),
            "in_hide": _auto_hide_positions(entry, "in"),
            "out_hide": _auto_hide_positions(entry, "out"),
        }
    return catalog


def _try_int(value: Any) -> int | None:
    """Best-effort int conversion — non-numeric values from Comexio are dropped instead
    of raising, since one malformed port/position must not abort catalog extraction.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _auto_hide_positions(entry: dict[str, Any], side: str) -> list[int]:
    """Port positions Studio hides while a block is collapsed ('Alles anzeigen' off).

    Source: fubBase "auto_hide": {"in": [7, 8, ...], "out": [5, 6]} — matches the
    'autohide' pin class in the Studio DOM. PHP serializes an empty mapping as a
    JSON array, hence the isinstance guard.
    """
    auto_hide = entry.get("auto_hide")
    if not isinstance(auto_hide, dict):
        return []
    return [pos for pos in (_try_int(raw) for raw in auto_hide.get(side) or []) if pos is not None]


def _port_types(ports: list[dict[str, Any]] | None) -> list[int]:
    """Port data types in Pos order (0=digital, 1=analog; Comexio's per-port Type field)."""
    sorted_ports = sorted(ports or [], key=lambda p: _try_int(p.get("Pos")) or 0)
    return [_try_int(port.get("Type")) or 0 for port in sorted_ports]


def _to_bool_flag(value: Any) -> bool:
    """Normalize a Comexio boolean-ish flag — numeric-string values like "0"/"1" are
    compared as integers so "0" isn't misread as truthy by a plain bool() cast.
    """
    as_int = _try_int(value)
    return bool(as_int) if as_int is not None else bool(value)


def _extract_time_modules(fub_modules: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build {ref_id: {name, kind, t1, t2}} from $FubModules["4"].

    These are user-configured timer instances (e.g. Einschaltwischer/Ausschaltverzögerung),
    analogous to Markers rather than a firmware catalog — "kind" (Comexio's "Type" field,
    e.g. "on_delayed"/"off_delayed") is the only part with a fixed vocabulary, "name"/"t1"/
    "t2" are per-instance user configuration that legitimately shrinks when a timer is deleted.
    """
    return {
        ref_id: {
            "name": entry.get("Name"),
            "kind": entry.get("Type"),
            "t1": entry.get("T1"),
            "t2": entry.get("T2"),
        }
        for ref_id, entry in _module_group(fub_modules, "4").items()
    }


def _extract_calendar_functions(fub_modules: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build {ref_id: {name, active, freq, by_day}} from $FubModules["3"].

    User-configured calendar-trigger instances (Sonnenaufgang/-untergang, Dämmerung,
    Wochentag-Zeitpläne — Comexio's I18N key is "timeFunction", Studio shows them as
    C1, C2, ...), analogous to Markers rather than a firmware catalog. "freq" is Comexio's
    raw vocabulary (e.g. "SUN_RISE"/"WEEKLY") — translated to a display label by the
    renderer, not here (catalog.py stores raw data, same convention as time_modules'
    untranslated "kind"). "Start"/"End"/"DtEnd" are deliberately NOT extracted: for
    astro-based Freq values they carry a fixed sentinel epoch identical across every
    instance (not a per-instance offset), so there is no reliable way to derive Studio's
    "Startversatz"/"Beenden an" dialog fields from them without risking a wrong value —
    better to omit than to guess.
    """
    return {
        ref_id: {
            "name": entry.get("Name"),
            "active": _to_bool_flag(entry.get("Active")),
            "freq": entry.get("Freq"),
            "by_day": entry.get("ByDay"),
        }
        for ref_id, entry in _module_group(fub_modules, "3").items()
    }


class FunctionPlanCatalogManager:
    """Cache the Comexio element/block-type catalog, refreshed opportunistically each poll."""

    def __init__(self, hass: HomeAssistant, server_id: str) -> None:
        self._server_id = server_id
        # Storage key keeps the legacy "logikplan" spelling — renaming it would discard
        # the catalog already persisted under .storage/.
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_logikplan_catalog_{server_id}")
        # Always a dict — persistence failures fall back to an empty in-memory catalog
        # instead of raising, per this class's "never raises" contract.
        self._data: dict[str, Any] = {}
        self._loaded = False

    async def _async_ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            loaded = await self._store.async_load()
        except Exception:
            # Give up on this file for the session (rather than retrying every poll and
            # spamming the log) — an empty in-memory catalog is a safe permanent fallback.
            _LOGGER.exception(
                "[%s] Function Plan catalog: failed to load persisted catalog — "
                "falling back to empty in-memory catalog",
                self._server_id,
            )
            self._loaded = True
            return
        if loaded is None:
            # No file yet (first run) — empty catalog is the correct, permanent state.
            self._loaded = True
            return
        if not isinstance(loaded, dict):
            _LOGGER.warning(
                "[%s] Function Plan catalog: persisted catalog has unexpected type %s — "
                "discarding and retrying next poll",
                self._server_id,
                type(loaded).__name__,
            )
            return
        self._data = loaded
        self._loaded = True

    async def async_update_from_raw_config(self, raw_config: dict[str, Any], comexio_version: str | None) -> None:
        """Extract, validate and persist the catalog if it changed and isn't corrupt.

        comexio_version (ComexioAPI.comexio_version, e.g. "11.0.2") is stamped onto the
        persisted catalog so a future consumer can tell which firmware state a given
        fub_base/time_modules snapshot corresponds to — Comexio updates could in theory
        change block templates, and there's no other way to correlate a plan backup
        (see function_plan_backup.py, same stamp) against "the catalog as it was back then".

        Never raises — a catalog problem must never affect the core marker/IO poll.
        """
        fub_types = raw_config.get("FubTypes")
        # PHP serializes an empty mapping as a JSON array (same quirk as _module_group) —
        # normalize so the shrink-guard's len() comparisons and the persisted shape stay a dict.
        if not isinstance(fub_types, dict):
            fub_types = {}
        fub_modules = raw_config.get("FubModules")
        fub_base_i18n = raw_config.get("FubBaseI18N")
        if not fub_types or not fub_modules or fub_base_i18n is None:
            # Admin page didn't expose these vars this cycle — keep the existing cache.
            _LOGGER.debug(
                "[%s] Function Plan catalog: skipping update, required Fub* vars missing or empty "
                "(FubTypes present=%s, FubModules present=%s, FubBaseI18N present=%s)",
                self._server_id,
                bool(fub_types),
                bool(fub_modules),
                fub_base_i18n is not None,
            )
            return

        fub_base = _extract_fub_base_catalog(fub_modules, fub_base_i18n)
        if not fub_base:
            _LOGGER.warning(
                "[%s] Function Plan catalog: fubBase extraction produced no entries — keeping previous catalog",
                self._server_id,
            )
            return
        time_modules = _extract_time_modules(fub_modules)
        calendar_functions = _extract_calendar_functions(fub_modules)

        await self._async_ensure_loaded()
        old_types: dict[str, Any] = self._data.get("fub_types", {})
        old_base: dict[str, Any] = self._data.get("fub_base", {})
        old_time_modules: dict[str, Any] = self._data.get("time_modules", {})
        old_calendar_functions: dict[str, Any] = self._data.get("calendar_functions", {})
        old_version = self._data.get("comexio_version")

        # Shrink guard for fub_types/fub_base only: within the same firmware, a catalog
        # only grows, so a drop in count more likely means a partial/logged-out page
        # scrape than a genuine removal — UNLESS the version stamp itself changed, which
        # means a real firmware update was detected and may legitimately have removed or
        # consolidated block types. time_modules is user configuration (like Markers) and
        # legitimately shrinks when the user deletes a timer — no guard for it.
        # No stored old_version (catalog persisted before version-stamping existed, or
        # first-ever save) means there's no baseline to compare against — treat that as
        # a version change too, so the guard doesn't block a legitimate post-upgrade shrink.
        version_changed = bool(comexio_version) and comexio_version != old_version
        if old_types and len(fub_types) < len(old_types) and not version_changed:
            _LOGGER.warning(
                "[%s] Function Plan catalog: new FubTypes count (%d) < cached (%d) — keeping previous catalog",
                self._server_id,
                len(fub_types),
                len(old_types),
            )
            return
        if old_base and len(fub_base) < len(old_base) and not version_changed:
            _LOGGER.warning(
                "[%s] Function Plan catalog: new fubBase count (%d) < cached (%d) — keeping previous catalog",
                self._server_id,
                len(fub_base),
                len(old_base),
            )
            return

        if (
            fub_types == old_types
            and fub_base == old_base
            and time_modules == old_time_modules
            and calendar_functions == old_calendar_functions
            and comexio_version == old_version
        ):
            return

        self._data = {
            "fetched_at": dt_util.utcnow().isoformat(),
            "comexio_version": comexio_version,
            "fub_types": fub_types,
            "fub_base": fub_base,
            "time_modules": time_modules,
            "calendar_functions": calendar_functions,
        }
        try:
            await self._store.async_save(self._data)
        except Exception:
            _LOGGER.exception(
                "[%s] Function Plan catalog: failed to persist catalog — keeping in-memory only",
                self._server_id,
            )
        _LOGGER.debug(
            "[%s] Function Plan catalog updated (Comexio %s): %d element types, %d fubBase blocks, "
            "%d time modules, %d calendar functions",
            self._server_id,
            comexio_version,
            len(fub_types),
            len(fub_base),
            len(time_modules),
            len(calendar_functions),
        )

    async def async_get_catalog(self) -> dict[str, Any]:
        """Return a copy of the cached catalog (for future consumers: analyses, rebuild).

        A shallow copy is returned so callers can't mutate the manager's internal cache.
        """
        await self._async_ensure_loaded()
        return dict(self._data)
