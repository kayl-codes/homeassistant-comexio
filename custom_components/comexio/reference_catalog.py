"""Reference catalogs: the Comexio catalog ids this integration was built against, checked live.

Comexio addresses block types by database id (e.g. a Flanke is $FubModules["5"]["113"]). Those
ids are not guaranteed to be identical on every server, so each catalog the integration relies
on ships as a reference file in reference/<kind>.json and is reconciled against the live admin
config on every poll (no extra request: get_raw_config() already fetched it).

Every entry has a stable, firmware-independent key and the id it had on the reference server:

- fub_base:  "<Name>/<input port types>/<output port types>" -> $FubModules["5"] id, port types
             in Pos order, d=digital a=analog. The internal Name alone is not unique (the
             2..5-input "or" variants share one Name), Name plus port signature is. Name is
             language-independent (only $FubBaseI18N changes with the UI language).
             Installed Comexio apps (category "inapp", names like "app<hash>_fub<hash>") are
             installation data, not firmware, and are left out. Their ids are interleaved with
             the firmware blocks' (ids are handed out in installation order), which is why a
             firmware block's id can differ from server to server.
- fub_types: element type name ("marker", "fubBase", ...) -> $FubTypes id.

The code resolves ids by key only (ReferenceCheck.resolve) — never by a hard-coded id. A new
catalog is added by dropping reference/<kind>.json next to the others and registering its live
extractor in LIVE_EXTRACTORS; the file name names the content.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import json
import logging
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlencode

_LOGGER = logging.getLogger(__name__)

REFERENCE_DIR = Path(__file__).parent / "reference"
REFERENCE_FORMAT = 1
KIND_FUB_BASE = "fub_base"
KIND_FUB_TYPES = "fub_types"
KEY_SEPARATOR = "/"
_PORT_TYPE_CODES = {0: "d", 1: "a"}
APP_CATEGORY = "inapp"


class EntryStatus(StrEnum):
    """Result of reconciling one reference entry against the live catalog."""

    OK = "ok"  # same key, same id
    MOVED = "moved"  # same key, different id — usable, the live id is used
    CHANGED = "changed"  # family (e.g. Name) exists, but not with this port signature — blocked
    MISSING = "missing"  # nothing of this family exists live — blocked
    AMBIGUOUS = "ambiguous"  # key exists more than once live — blocked


USABLE_STATUSES = frozenset({EntryStatus.OK, EntryStatus.MOVED})
# Labels for a required entry that has no status to report (see unresolved()).
REASON_NOT_CHECKED = "not checked"
REASON_CHECK_FAILED = "check failed, see log"
REASON_NO_LIVE_CATALOG = "no block catalog from Comexio"


@dataclass(frozen=True)
class ReferenceCatalog:
    """One reference file: {key: id} as seen on the reference server."""

    kind: str
    comexio_version: str | None
    entries: Mapping[str, int]


@dataclass(frozen=True)
class EntryCheck:
    key: str
    status: EntryStatus
    ref_id: int | None
    live_id: int | None


@dataclass(frozen=True)
class CatalogCheck:
    """Reconciliation of one catalog kind."""

    kind: str
    reference_version: str | None
    entries: Mapping[str, EntryCheck]
    new_keys: tuple[str, ...] = ()
    live_available: bool = True

    def counts(self) -> Counter[EntryStatus]:
        return Counter(entry.status for entry in self.entries.values())

    def deviations(self) -> list[EntryCheck]:
        return [entry for entry in self.entries.values() if entry.status is not EntryStatus.OK]


@dataclass(frozen=True)
class ReferenceCheck:
    """Result of one reconciliation run over all reference catalogs."""

    comexio_version: str | None
    catalogs: Mapping[str, CatalogCheck]
    live_ids: Mapping[str, Mapping[str, list[int]]] = field(default_factory=dict)
    duration_ms: float = 0.0
    # Every live $FubModules["5"] id including installed apps — for the plan-element scan.
    fub_base_ids: frozenset[int] = frozenset()

    def resolve(self, kind: str, key: str) -> int | None:
        """Live id for key, or None unless the reference check confirmed it usable (ok/moved).

        Gated on the status too, not just the live ids, so a key absent from a damaged reference
        file is blocked exactly like unresolved() reports it — gate and Repair share one source.
        """
        if self.status(kind, key) not in USABLE_STATUSES:
            return None
        ids = (self.live_ids.get(kind) or {}).get(key) or []
        return ids[0] if len(ids) == 1 else None

    def status(self, kind: str, key: str) -> EntryStatus | None:
        catalog = self.catalogs.get(kind)
        entry = catalog.entries.get(key) if catalog else None
        return entry.status if entry else None

    def has_unusable(self) -> bool:
        """True if any entry is changed/missing/ambiguous — a moved id alone is expected and resolved."""
        return any(
            entry.status not in USABLE_STATUSES for catalog in self.catalogs.values() for entry in catalog.deviations()
        )

    def fingerprint(self) -> tuple:
        """Hashable digest — the caller logs at INFO only when this changes, not every poll."""
        return (
            self.comexio_version,
            tuple(
                (kind, catalog.live_available, tuple(sorted(catalog.counts().items())), catalog.new_keys)
                for kind, catalog in sorted(self.catalogs.items())
            ),
        )

    def summary(self, include_duration: bool = True) -> str:
        """One-line result; without the duration it is stable across polls (Repair issue text)."""
        parts = [_catalog_summary(catalog) for _kind, catalog in sorted(self.catalogs.items())]
        text = f"Comexio {self.comexio_version or '?'}: " + "; ".join(parts)
        return text + f" ({self.duration_ms:.1f} ms)" if include_duration else text


def _catalog_summary(catalog: CatalogCheck) -> str:
    if not catalog.live_available:
        return f"{catalog.kind} live data unavailable"
    counts = catalog.counts()
    text = (
        f"{catalog.kind} {counts[EntryStatus.OK]}/{len(catalog.entries)} ok "
        f"(reference {catalog.reference_version or '?'})"
    )
    others = [f"{counts[status]} {status}" for status in EntryStatus if status is not EntryStatus.OK and counts[status]]
    if catalog.new_keys:
        others.append(f"{len(catalog.new_keys)} new")
    return f"{text}, {', '.join(others)}" if others else text


# ---------------------------------------------------------------------------
# Live extraction: raw admin config -> {key: [ids]} (a list, so duplicates stay visible)
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _port_signature(ports: Any) -> str:
    """Port types in Pos order as a compact string, e.g. "daa" (unknown types keep their number)."""
    if isinstance(ports, dict):  # PHP serializes a non-sequential list as an object
        ports = list(ports.values())
    valid = [port for port in ports if isinstance(port, dict)] if isinstance(ports, list) else []
    ordered = sorted(valid, key=lambda port: _as_int(port.get("Pos")) or 0)
    return "".join(_port_type_code(port) for port in ordered)


def _port_type_code(port: Mapping[str, Any]) -> str:
    type_id = _as_int(port.get("Type"))
    return _PORT_TYPE_CODES[type_id] if type_id in _PORT_TYPE_CODES else str(port.get("Type"))


def fub_base_key(entry: Mapping[str, Any]) -> str | None:
    """Stable key of one $FubModules["5"] entry, or None when it has no internal Name."""
    name = entry.get("Name")
    if not name:
        return None
    return KEY_SEPARATOR.join((str(name), _port_signature(entry.get("input")), _port_signature(entry.get("output"))))


def _group(ids_by_key: dict[str, list[int]], key: str | None, raw_id: Any) -> None:
    live_id = _as_int(raw_id)
    if key and live_id is not None:
        ids_by_key.setdefault(key, []).append(live_id)


def _fub_base_group(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    modules = raw_config.get("FubModules")
    group = modules.get("5") if isinstance(modules, dict) else None
    return group if isinstance(group, dict) else {}


def _is_app(entry: Mapping[str, Any]) -> bool:
    categories = entry.get("categories")
    return isinstance(categories, list) and APP_CATEGORY in categories


def extract_live_fub_base(raw_config: Mapping[str, Any]) -> dict[str, list[int]] | None:
    group = _fub_base_group(raw_config)
    if not group:
        return None
    ids_by_key: dict[str, list[int]] = {}
    for raw_id, entry in group.items():
        if isinstance(entry, dict) and not _is_app(entry):
            _group(ids_by_key, fub_base_key(entry), entry.get("Id", raw_id))
    return ids_by_key


def all_fub_base_ids(raw_config: Mapping[str, Any]) -> frozenset[int]:
    """Every $FubModules["5"] id, installed apps included."""
    ids = (
        _as_int(entry.get("Id", raw_id)) if isinstance(entry, dict) else None
        for raw_id, entry in _fub_base_group(raw_config).items()
    )
    return frozenset(live_id for live_id in ids if live_id is not None)


def extract_live_fub_types(raw_config: Mapping[str, Any]) -> dict[str, list[int]] | None:
    fub_types = raw_config.get("FubTypes")
    if not isinstance(fub_types, dict) or not fub_types:
        return None
    ids_by_key: dict[str, list[int]] = {}
    for raw_id, name in fub_types.items():
        _group(ids_by_key, str(name) if name else None, raw_id)
    return ids_by_key


LIVE_EXTRACTORS: dict[str, Callable[[Mapping[str, Any]], dict[str, list[int]] | None]] = {
    KIND_FUB_BASE: extract_live_fub_base,
    KIND_FUB_TYPES: extract_live_fub_types,
}


def _family(key: str) -> str:
    return key.split(KEY_SEPARATOR, 1)[0]


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _entry_status(key: str, ref_id: int, live: Mapping[str, list[int]], live_families: set[str]) -> EntryCheck:
    ids = live.get(key)
    if ids is None:
        status = EntryStatus.CHANGED if _family(key) in live_families else EntryStatus.MISSING
        return EntryCheck(key, status, ref_id, None)
    if len(ids) > 1:
        return EntryCheck(key, EntryStatus.AMBIGUOUS, ref_id, None)
    status = EntryStatus.OK if ids[0] == ref_id else EntryStatus.MOVED
    return EntryCheck(key, status, ref_id, ids[0])


def reconcile_catalog(reference: ReferenceCatalog, live: Mapping[str, list[int]] | None) -> CatalogCheck:
    if live is None:
        entries = {key: EntryCheck(key, EntryStatus.MISSING, ref_id, None) for key, ref_id in reference.entries.items()}
        return CatalogCheck(reference.kind, reference.comexio_version, entries, live_available=False)
    live_families = {_family(key) for key in live}
    entries = {key: _entry_status(key, ref_id, live, live_families) for key, ref_id in reference.entries.items()}
    new_keys = tuple(sorted(key for key in live if key not in reference.entries))
    return CatalogCheck(reference.kind, reference.comexio_version, entries, new_keys)


def reconcile(
    references: Mapping[str, ReferenceCatalog], raw_config: Mapping[str, Any], comexio_version: str | None
) -> ReferenceCheck:
    """Reconcile every reference catalog against the live raw config (pure, no I/O)."""
    started = time.perf_counter()
    catalogs: dict[str, CatalogCheck] = {}
    live_ids: dict[str, dict[str, list[int]]] = {}
    for kind, reference in references.items():
        extractor = LIVE_EXTRACTORS.get(kind)
        live = extractor(raw_config) if extractor else None
        catalogs[kind] = reconcile_catalog(reference, live)
        live_ids[kind] = live or {}
    fub_base_ids = all_fub_base_ids(raw_config)
    duration_ms = (time.perf_counter() - started) * 1000
    return ReferenceCheck(comexio_version, catalogs, live_ids, duration_ms, fub_base_ids)


def unresolved(
    check: ReferenceCheck | None, required: Iterable[tuple[str, str]], reason: str = REASON_NOT_CHECKED
) -> list[str]:
    """Required (kind, key) pairs that can't be used on this server, as "kind:key (status)" labels.

    reason labels entries without a status — why there's no check result to judge them by.
    """
    labels = []
    for kind, key in required:
        status = check.status(kind, key) if check else None
        if check is None or check.resolve(kind, key) is None:
            labels.append(f"{kind}:{key} ({status or reason})")
    return labels


def format_deviations(check: ReferenceCheck, limit: int = 40) -> list[str]:
    """One line per non-ok entry ("kind key: status ref=113 live=213"), capped at limit."""
    lines = [
        f"{kind} {entry.key}: {entry.status} ref={entry.ref_id} live={entry.live_id}"
        for kind, catalog in sorted(check.catalogs.items())
        for entry in catalog.deviations()
    ]
    if len(lines) > limit:
        lines = [*lines[:limit], f"... {len(lines) - limit} more"]
    return lines


# ---------------------------------------------------------------------------
# Plan elements pointing at block types the live catalog doesn't know
# ---------------------------------------------------------------------------


def find_unknown_fub_base_refs(
    plans: Mapping[Any, Any], live_fub_base_ids: Iterable[int]
) -> list[tuple[str, str, str]]:
    """(fub_id, elem_id, ref_id) of every type-5 plan element whose ref_id isn't in the live catalog.

    Comexio's editor reports exactly these as "Configuration fault in <fub>/<elem>: 5 <ref>" and
    hides them — finding them here puts them into the log instead of only into the editor.
    """
    known = {str(live_id) for live_id in live_fub_base_ids}
    found = []
    for fub_id, plan in plans.items():
        elements = plan.get("elements") if isinstance(plan, dict) else None
        for elem_id, element in (elements or {}).items():
            reference = (element or {}).get("reference") or {}
            if str(reference.get("type")) == "5" and str(reference.get("ref_id")) not in known:
                found.append((str(fub_id), str(elem_id), str(reference.get("ref_id"))))
    return found


# ---------------------------------------------------------------------------
# Reference files
# ---------------------------------------------------------------------------


def parse_reference(kind: str, data: Any) -> ReferenceCatalog:
    """Validate one reference file's content; raises ValueError on a malformed file."""
    if not isinstance(data, dict) or data.get("format") != REFERENCE_FORMAT or data.get("kind") != kind:
        raise ValueError(f"reference/{kind}.json: wrong format/kind header")
    entries = data.get("entries")
    if not isinstance(entries, dict) or not entries:
        raise ValueError(f"reference/{kind}.json: no entries")
    parsed = {str(key): _as_int(ref_id) for key, ref_id in entries.items()}
    bad = [key for key, ref_id in parsed.items() if ref_id is None]
    if bad:
        raise ValueError(f"reference/{kind}.json: non-integer ids for {bad[:5]}")
    return ReferenceCatalog(
        kind, data.get("comexio_version"), {key: ref_id for key, ref_id in parsed.items() if ref_id is not None}
    )


def load_reference_catalogs(directory: Path = REFERENCE_DIR) -> dict[str, ReferenceCatalog]:
    """Load every reference/<kind>.json with a registered extractor (blocking I/O — run in executor).

    A malformed file is logged and skipped; required entries from it then resolve to None, which
    blocks the dependent feature instead of guessing an id.
    """
    catalogs: dict[str, ReferenceCatalog] = {}
    for path in sorted(directory.glob("*.json")):
        kind = path.stem
        if kind not in LIVE_EXTRACTORS:
            _LOGGER.warning("Reference catalog %s has no live extractor — ignored", path.name)
            continue
        try:
            catalogs[kind] = parse_reference(kind, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _LOGGER.exception("Reference catalog %s unusable", path.name)
    for kind in sorted(set(LIVE_EXTRACTORS) - set(catalogs)):
        _LOGGER.error("Reference catalog %s.json missing or unusable — its entries can't be resolved", kind)
    return catalogs


def build_reference(kind: str, raw_config: Mapping[str, Any], comexio_version: str | None) -> dict[str, Any]:
    """Reference file content for kind from a live raw config (used by scripts/build_reference_catalog.py)."""
    live = LIVE_EXTRACTORS[kind](raw_config)
    if not live:
        raise ValueError(f"{kind}: no live entries in this raw config — refusing to write an empty reference")
    duplicates = sorted(key for key, ids in live.items() if len(ids) > 1)
    if duplicates:
        raise ValueError(f"{kind}: keys not unique on the source server: {duplicates}")
    return {
        "format": REFERENCE_FORMAT,
        "kind": kind,
        "comexio_version": comexio_version,
        "entries": {key: ids[0] for key, ids in sorted(live.items())},
    }


# ---------------------------------------------------------------------------
# Pre-filled GitHub issue for a blocked required entry
# ---------------------------------------------------------------------------

# GitHub rejects new-issue URLs much beyond ~8 KB; stay well below so every browser opens it.
ISSUE_URL_MAX_CHARS = 7000
ISSUE_LIST_LIMIT = 30
ISSUE_TRUNCATED_LINE = "_… truncated — please attach the Home Assistant log._"


@dataclass(frozen=True)
class IssueContext:
    """Everything the issue report needs besides the check itself."""

    integration_version: str | None
    ha_version: str | None
    blocked: tuple[str, ...]
    unknown_plan_refs: tuple[tuple[str, str, str], ...] = ()


def _ref_id_owner(check: ReferenceCheck, kind: str, ref_id: int | None) -> str:
    """What the reference id points at on this server: a key, an installed app, or nothing."""
    if ref_id is None:
        return "-"
    owners = [key for key, ids in (check.live_ids.get(kind) or {}).items() if ref_id in ids]
    if owners:
        return ", ".join(owners)
    if kind == KIND_FUB_BASE and ref_id in check.fub_base_ids:
        return "installed app"
    return "free"


def _required_detail_lines(check: ReferenceCheck, required: Iterable[tuple[str, str]]) -> list[str]:
    lines = []
    for kind, key in required:
        catalog = check.catalogs.get(kind)
        entry = catalog.entries.get(key) if catalog else None
        ref_id = entry.ref_id if entry else None
        same_family = sorted(
            f"{live_key} = {ids}"
            for live_key, ids in (check.live_ids.get(kind) or {}).items()
            if _family(live_key) == _family(key)
        )
        lines += [
            f"- `{kind}:{key}`: status `{entry.status if entry else REASON_NOT_CHECKED}`, reference id {ref_id}, "
            f"live id {entry.live_id if entry else None}",
            f"  - reference id {ref_id} on this server: {_ref_id_owner(check, kind, ref_id)}",
            f"  - live blocks named `{_family(key)}`: {', '.join(same_family) or 'none'}",
        ]
    return lines


def _capped(items: list[str], limit: int = ISSUE_LIST_LIMIT) -> list[str]:
    return items if len(items) <= limit else [*items[:limit], f"… {len(items) - limit} more"]


def build_issue_report(
    check: ReferenceCheck | None, required: Iterable[tuple[str, str]], context: IssueContext
) -> tuple[str, str]:
    """(title, markdown body) of a GitHub issue describing why required blocks are unusable.

    Contains only versions and Comexio's firmware catalog keys/ids — no host, credentials,
    entity or marker names — so the user can submit it as-is after reviewing.
    """
    title = f"Comexio block not usable: {', '.join(context.blocked)}"
    lines = [
        '_Created from the Home Assistant repair "Comexio function block not usable"._',
        "",
        "### Versions",
        f"- Integration: {context.integration_version or '?'}",
        f"- Home Assistant: {context.ha_version or '?'}",
        f"- Comexio: {(check.comexio_version if check else None) or '?'}",
        "",
        "### Blocked",
        *[f"- `{label}`" for label in context.blocked],
    ]
    if check is None:
        lines += ["", "Reference check did not run — see the Home Assistant log."]
        return title, "\n".join(lines)
    lines += [
        "",
        "### Required blocks",
        *_required_detail_lines(check, required),
        "",
        "### Summary",
        check.summary(include_duration=False),
        "",
        "### Deviations",
        "```",
        *(format_deviations(check, ISSUE_LIST_LIMIT) or ["none"]),
        "```",
        "",
        "### Live blocks not in the reference",
        "```",
        *(_capped([key for c in check.catalogs.values() for key in c.new_keys]) or ["none"]),
        "```",
    ]
    if context.unknown_plan_refs:
        refs = [f"{fub}/{elem}: 5 {ref}" for fub, elem, ref in context.unknown_plan_refs]
        lines += ["", "### Plan elements with unknown block types", "```", *_capped(refs), "```"]
    return title, "\n".join(lines)


def github_issue_url(issues_url: str, title: str, body: str, max_chars: int = ISSUE_URL_MAX_CHARS) -> str:
    """New-issue URL with title/body pre-filled, the body cut line by line to fit max_chars."""
    lines = body.split("\n")
    while True:
        query = urlencode({"title": title, "body": "\n".join(lines), "labels": "bug"})
        url = f"{issues_url}/new?{query}"
        if len(url) <= max_chars or len(lines) <= 1:
            return url
        lines = [*lines[: -2 if lines[-1] == ISSUE_TRUNCATED_LINE else -1], ISSUE_TRUNCATED_LINE]
