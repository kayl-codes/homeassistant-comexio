"""Repair issues for the backups of deleted function plans once their retention has passed.

A plan deleted directly in Comexio leaves its backup snapshots behind (an orphaned
(fub_id, plan_name) identity). Once the newest of them is older than the configured
retention, the backup cycle raises one repair per identity instead of deleting it silently;
the repair flow (repairs.py) then deletes or keeps the snapshots on the user's decision.
"""

from datetime import datetime
import hashlib
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN, TIMESTAMP_DISPLAY_FORMAT
from .function_plan_backup import FunctionPlanBackupManager

ISSUE_ORPHANED_PLAN_BACKUPS = "orphaned_plan_backups"
BACKUPS_DOC_URL = "https://github.com/kayl-codes/homeassistant-comexio/blob/master/BACKUPS.md"
NO_CURRENT_PLAN = "—"


def _issue_prefix(server_id: str) -> str:
    return f"{ISSUE_ORPHANED_PLAN_BACKUPS}_{server_id}_"


def _own_issue_ids(hass: HomeAssistant, server_id: str) -> list[str]:
    """Ids of this instance's orphaned-backup repairs.

    The prefix alone is not enough: for server "home" it also matches the repairs of a
    server "home_2", so the rest of the id must be exactly "{fub_id}_{hash}".
    """
    own = re.compile(rf"{re.escape(_issue_prefix(server_id))}\d+_[0-9a-f]{{12}}")
    return [issue_id for domain, issue_id in ir.async_get(hass).issues if domain == DOMAIN and own.fullmatch(issue_id)]


def orphaned_backup_issue_id(server_id: str, fub_id: int, plan_name: str) -> str:
    """Stable repair issue id for one orphaned identity.

    Plan names may contain any character and are not unique, so a hash of the name stands in
    for it next to the fub_id.
    """
    digest = hashlib.sha256(plan_name.encode()).hexdigest()[:12]
    return f"{_issue_prefix(server_id)}{fub_id}_{digest}"


def delete_orphaned_backup_issue(hass: HomeAssistant, server_id: str, fub_id: int, plan_name: str) -> None:
    """Close an identity's repair right away once its backups were deleted by an action.

    Without this it would stay open until the next backup cycle clears it.
    """
    ir.async_delete_issue(hass, DOMAIN, orphaned_backup_issue_id(server_id, fub_id, plan_name))


def delete_all_orphaned_backup_issues(hass: HomeAssistant, server_id: str) -> None:
    """Close every orphaned-backup repair of this instance, e.g. after all backups were deleted."""
    for issue_id in _own_issue_ids(hass, server_id):
        ir.async_delete_issue(hass, DOMAIN, issue_id)


def repair_placeholders(issue_data: dict[str, Any]) -> dict[str, str]:
    """description_placeholders for the fix flow, rebuilt from the issue's data."""
    keys = ("plan_name", "fub_id", "count", "newest", "months", "current_plan")
    return {**{key: str(issue_data.get(key, "?")) for key in keys}, "docs_url": BACKUPS_DOC_URL}


def format_captured_at(captured_at: str | None) -> str:
    """Local display form of a snapshot's captured_at timestamp, "?" if it is unreadable."""
    ts = dt_util.parse_datetime(captured_at) if captured_at else None
    return dt_util.as_local(ts).strftime(TIMESTAMP_DISPLAY_FORMAT) if ts else "?"


async def async_audit_orphaned_backups(
    hass: HomeAssistant,
    *,
    entry_id: str,
    server_id: str,
    manager: FunctionPlanBackupManager,
    fub_data: dict[str, Any],
    cutoff: datetime,
    retention_months: int,
) -> None:
    """Raise a repair per orphaned identity past retention; clear the ones that no longer apply.

    A repair disappears once its plan is live again, its snapshots are gone or the user kept
    them. Without a live plan list nothing is raised or cleared (see async_expired_orphans).
    """
    expired = await manager.async_expired_orphans(fub_data, cutoff)
    if expired is None:
        return
    wanted: set[str] = set()
    for item in expired:
        fub_id, plan_name = item["fub_id"], item["plan_name"]
        issue_id = orphaned_backup_issue_id(server_id, fub_id, plan_name)
        wanted.add(issue_id)
        details = {
            "plan_name": plan_name,
            "fub_id": str(fub_id),
            "count": str(item["count"]),
            "newest": format_captured_at(item["captured_at"]),
            "months": str(retention_months),
            # A plan renamed in Comexio looks exactly like a deleted one whose ID was reused;
            # naming the plan that holds the ID now lets the user tell the two apart.
            "current_plan": str(fub_data.get(str(fub_id), {}).get("Name") or NO_CURRENT_PLAN),
        }
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_ORPHANED_PLAN_BACKUPS,
            translation_placeholders=details,
            learn_more_url=BACKUPS_DOC_URL,
            # The fix flow gets only this data, not the placeholders — it renders the same text.
            data={"entry_id": entry_id, **details},
        )
    for issue_id in _own_issue_ids(hass, server_id):
        if issue_id not in wanted:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
