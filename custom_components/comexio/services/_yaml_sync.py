# Version: 0.7.6
"""services.yaml dynamic-dropdown rewriting + HA live schema cache refresh.

Split out of the former monolithic services.py (Sourcery: "too large, multi-purpose") —
keeps the (fub_id / snapshot) select options embedded in services.yaml in sync with live
coordinator data, since HA's Developer Tools "Call Service" UI resolves a field's selector
from the YAML at service-registration time rather than dynamically per call.
"""

import asyncio
from collections.abc import Iterator
import logging
import pathlib

from homeassistant.core import HomeAssistant
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.util import dt as dt_util
import yaml

from ..const import DOMAIN, FUNCTION_PLAN_SERVICE_NAMES, TIMESTAMP_DISPLAY_FORMAT
from ..coordinator import ComexioCoordinator
from ._context import format_plan_label
from ._grid import _is_managed_cluster_plan

_LOGGER = logging.getLogger(__name__)

# Order matches FUNCTION_PLAN_SERVICE_NAMES (const.py): connect, sort, stop, activate, visualize.
_, _SORT_SERVICE_NAME, _, _, _SVC_VISUALIZE = FUNCTION_PLAN_SERVICE_NAMES
_SERVICES_YAML_PATH = pathlib.Path(__file__).parent.parent / "services.yaml"
# Serializes the two independent read-modify-write rewrites of services.yaml below
# (_update_services_yaml_plans and _refresh_service_descriptions) so a run of one can't
# clobber the other's changes with a stale on-disk snapshot.
_SERVICES_YAML_LOCK = asyncio.Lock()


def _apply_single_instance_default(content: dict, entry_ids: list[str]) -> None:
    """Pre-fill the config_entry field in the Actions UI when exactly one Comexio instance exists.

    With multiple instances the field is left without a default, forcing an explicit choice.
    """
    single_entry = entry_ids[0] if len(entry_ids) == 1 else None
    for svc in content.values():
        entry_field = (svc or {}).get("fields", {}).get("config_entry")
        if not entry_field:
            continue
        if single_entry:
            entry_field["default"] = single_entry
        else:
            entry_field.pop("default", None)


def _rewrite_services_yaml_plans(
    plan_options: list[str], sortable_plan_options: list[str], entry_ids: list[str]
) -> None:
    """Blocking read/modify/write of services.yaml; run via executor job only.

    services.yaml is rewritten (rather than deriving fub_id options purely at runtime) because HA's
    service-call schema is static: the Developer Tools "Call Service" UI resolves a field's selector
    from the YAML at service-registration time, and selectors don't support a per-call dynamic option
    list bound to live coordinator data. The select entity (select.py) is the live source of truth;
    this rewrite just keeps the YAML-declared dropdown in sync with it whenever the plan set changes.

    logikplan_sort gets the managed-cluster-only subset — its runtime handler already rejects any
    other plan via _is_managed_cluster_plan(), so offering the full list would let the picker suggest
    a plan that's guaranteed to be refused afterward.
    """
    try:
        content = yaml.safe_load(_SERVICES_YAML_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        _LOGGER.warning("services.yaml missing or invalid, skipping plan option rewrite: %s", exc)
        return
    for svc in FUNCTION_PLAN_SERVICE_NAMES:
        fub_field = content.get(svc, {}).get("fields", {}).get("fub_id")
        if fub_field:
            options = sortable_plan_options if svc == _SORT_SERVICE_NAME else plan_options
            fub_field["selector"] = {"select": {"options": options, "custom_value": True}}
    _apply_single_instance_default(content, entry_ids)
    _SERVICES_YAML_PATH.write_text(
        yaml.dump(content, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _iter_active_coordinators(hass: HomeAssistant) -> Iterator[tuple[str, ComexioCoordinator]]:
    """Yield (entry_id, coordinator) for every live Comexio config entry in hass.data[DOMAIN].

    Shared by every function here that needs to scan all coordinators — hass.data[DOMAIN] also
    holds non-coordinator bookkeeping entries (webhook IDs, the cached service-description key),
    hence the isinstance filter.
    """
    for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
        if isinstance(coordinator, ComexioCoordinator):
            yield entry_id, coordinator


def _collect_plan_options(hass: HomeAssistant) -> tuple[list[str], list[str], list[str]]:
    """Collect sorted fub_id dropdown labels (full set + managed-only subset) and config-entry IDs.

    Labels use the same "<name> (ID <fid>)" format as the select entity, so duplicate
    plan names across coordinators still resolve unambiguously via _resolve_fub_id().
    """
    plan_labels: set[str] = set()
    sortable_plan_labels: set[str] = set()
    entry_ids: list[str] = []
    for entry_id, coordinator in _iter_active_coordinators(hass):
        entry_ids.append(entry_id)
        for fub_id, fub in coordinator.api.fub_data.items():
            name = fub.get("Name", "")
            if not name:
                continue
            label = format_plan_label(name, fub_id)
            plan_labels.add(label)
            if _is_managed_cluster_plan(coordinator, fub_id):
                sortable_plan_labels.add(label)
    return sorted(plan_labels, key=str.lower), sorted(sortable_plan_labels, key=str.lower), entry_ids


async def _update_services_yaml_plans(hass: HomeAssistant) -> None:
    """Rewrite fub_id select options in services.yaml with current plan labels from all active coordinators."""
    plan_options, sortable_plan_options, entry_ids = _collect_plan_options(hass)
    if not plan_options:
        _LOGGER.debug("_update_services_yaml_plans: no plans available, skipping")
        return

    try:
        async with _SERVICES_YAML_LOCK:
            await hass.async_add_executor_job(
                _rewrite_services_yaml_plans, plan_options, sortable_plan_options, entry_ids
            )
        _LOGGER.debug(
            "Updated services.yaml: %d function plan options (labels) written (%d sortable)",
            len(plan_options),
            len(sortable_plan_options),
        )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not update services.yaml with plan labels: %s", exc)


def _build_restore_options(restore_plans: list[tuple[int, str, bool, int]]) -> list[dict]:
    """{label, value} dropdown options for the fub_id field of the restore-related services."""
    return [
        {
            "label": (
                f"{name} — fub {fub_id} ({'live' if is_live else 'deleted'}, "
                f"{count} snapshot{'s' if count != 1 else ''})"
            ),
            "value": f"{fub_id}:{name}",
        }
        for fub_id, name, is_live, count in sorted(restore_plans, key=lambda p: (p[1].lower(), p[0]))
    ]


_SNAPSHOT_OPTION_MAX_OP_LEN = 30


def _snapshot_option_row(kind: str, entry: dict) -> tuple[str, float, dict]:
    """One (sort_key, -epoch, option) row for _build_snapshot_options."""
    ts = dt_util.parse_datetime(str(entry.get("captured_at", "")))
    epoch = ts.timestamp() if ts else 0.0
    ts_label = dt_util.as_local(ts).strftime(TIMESTAMP_DISPLAY_FORMAT) if ts else "?"
    operation = str(entry.get("operation", ""))
    # Some operations embed a full marker-ID list (e.g. add_marker_pairs) — that's
    # useful in the log but far too long for a dropdown label, so truncate it here.
    if len(operation) > _SNAPSHOT_OPTION_MAX_OP_LEN:
        operation = operation[: _SNAPSHOT_OPTION_MAX_OP_LEN - 1] + "…"
    op_suffix = f" ({operation})" if kind == "change" and operation else ""
    label = (
        f"{entry.get('plan_name')} — fub {entry.get('fub_id')} — {kind}[{entry.get('slot')}] — {ts_label}{op_suffix}"
    )
    value = f"{entry.get('fub_id')}:{kind}:{entry.get('slot')}:{entry.get('plan_name')}"
    return str(entry.get("plan_name", "")).lower(), -epoch, {"label": label, "value": value}


def _build_snapshot_options(backups: dict[str, list[dict]]) -> list[dict]:
    """Build one dropdown option per stored snapshot for the combined 'snapshot' picker.

    value: 'fub_id:kind:slot:plan_name' (parsed back by _parse_snapshot_field) — plan_name is
    included because fub_id alone is not a stable identity (a reused ID can carry two
    different plans' snapshots at once).
    """
    rows = [_snapshot_option_row(kind, entry) for kind in ("auto", "change") for entry in backups.get(kind, [])]
    rows.sort(key=lambda r: (r[0], r[1]))
    return [option for _, _, option in rows]


def _set_yaml_field_options(
    content: dict, service: str, field: str, options: list, *, custom_value: bool = True
) -> None:
    # custom_value=False for the {label, value} dict-option fields (fub_id/snapshot on the
    # restore-related services): with custom_value=True, HA's frontend shows the raw
    # submitted value instead of the matching label once an option is picked — acceptable
    # for the plain-string options elsewhere, but defeats the point of a readable label here.
    target_field = content.get(service, {}).get("fields", {}).get(field)
    if target_field:
        target_field["selector"] = {"select": {"options": options, "custom_value": custom_value}}


_BACKUP_DYNAMIC_SERVICES = ("function_plan_restore", "function_plan_list_backups", "function_plan_delete_backups")
# function_plan_visualize's fub_id dropdown stays owned by _rewrite_services_yaml_plans (plain
# plan labels, not the composite fub_id:name backup identity) — only its 'snapshot' field
# (added for the SVG-format/backup-snapshot preview, Cluster 4) is populated here, alongside
# the three backup services above.
_SNAPSHOT_DYNAMIC_SERVICES = (*_BACKUP_DYNAMIC_SERVICES, _SVC_VISUALIZE)
_SERVICE_DESCRIPTIONS_CACHE_KEY = "_service_descriptions_cache"


def _update_services_yaml_backup_options(
    restore_plans: list[tuple[int, str, bool, int]], snapshot_options: list[dict]
) -> dict | None:
    """Blocking read/modify/write of services.yaml's backup-related dropdowns; executor job only.

    Trimmed sibling of _rewrite_services_yaml_plans (which independently owns the
    function_plan_* services' fub_id dropdown) — only touches the fub_id/snapshot fields of the
    three backup services (plus function_plan_visualize's snapshot field), so that function is
    left completely untouched.
    """
    try:
        content = yaml.safe_load(_SERVICES_YAML_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        _LOGGER.warning("services.yaml missing or invalid, skipping backup option rewrite: %s", exc)
        return None
    restore_options = _build_restore_options(restore_plans)
    for svc in _BACKUP_DYNAMIC_SERVICES:
        _set_yaml_field_options(content, svc, "fub_id", restore_options, custom_value=False)
    for svc in _SNAPSHOT_DYNAMIC_SERVICES:
        _set_yaml_field_options(content, svc, "snapshot", snapshot_options, custom_value=False)
    try:
        _SERVICES_YAML_PATH.write_text(
            yaml.dump(content, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
    except OSError as exc:
        _LOGGER.warning("Could not write services.yaml backup option rewrite: %s", exc)
        return None
    return content


async def _refresh_service_descriptions(hass: HomeAssistant) -> None:
    """Rewrite services.yaml's backup dropdowns and push them into HA's live schema cache.

    Re-registering an already-registered service does NOT make Home Assistant reload its
    cached description — only async_set_service_schema writes directly into that cache and
    is guaranteed to take effect immediately. Called by coordinator.py right after it writes a
    new auto-/change-backup, and by this module's own backup service handlers —
    i.e. up to once per poll cycle plus once per restore/delete/purge call. The computed
    options are cached and compared first, so the blocking file rewrite and schema push
    are skipped whenever nothing actually changed since the last refresh.
    """
    restore_plans: list[tuple[int, str, bool, int]] = []
    snapshot_options: list[dict] = []
    for _entry_id, coordinator in _iter_active_coordinators(hass):
        live_fub_ids = {int(k) for k in coordinator.api.fub_data}
        for fub_id, name, count in await coordinator.function_plan_backup.async_backed_up_plans():
            restore_plans.append((fub_id, name, fub_id in live_fub_ids, count))
        backups = await coordinator.function_plan_backup.async_list_backups()
        snapshot_options.extend(_build_snapshot_options(backups))
    cache_key = (tuple(restore_plans), tuple((opt["label"], opt["value"]) for opt in snapshot_options))
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_SERVICE_DESCRIPTIONS_CACHE_KEY) == cache_key:
        return
    async with _SERVICES_YAML_LOCK:
        content = await hass.async_add_executor_job(
            _update_services_yaml_backup_options, restore_plans, snapshot_options
        )
    if not content:
        return
    domain_data[_SERVICE_DESCRIPTIONS_CACHE_KEY] = cache_key
    for svc in _SNAPSHOT_DYNAMIC_SERVICES:
        if schema := content.get(svc):
            async_set_service_schema(hass, DOMAIN, svc, schema)
