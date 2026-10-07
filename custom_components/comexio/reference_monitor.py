"""Runs the reference catalog check each poll and reports it (log + Repair issue).

The pure reconciliation lives in aiocomexio.reference_catalog; this module adds the Home Assistant side:
loading the reference files off the event loop, publishing the result to the API (which resolves
block-type ids from it), logging only on change, and a Repair issue while a block the
integration writes is not usable on this server. The Repair carries a link that opens a GitHub
issue pre-filled with the diagnosis (build_issue_report below), so the user can report it with
one click. A check that could not run at all (exception, no live block catalog) raises the same
Repair with a text that says so, instead of claiming a firmware incompatibility.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import urlencode

from aiocomexio.reference_catalog import (
    KIND_FUB_BASE,
    REASON_CHECK_FAILED,
    REASON_NO_LIVE_CATALOG,
    REASON_NOT_CHECKED,
    ReferenceCatalog,
    ReferenceCheck,
    find_unknown_fub_base_refs,
    format_deviations,
    key_family,
    load_reference_catalogs,
    reconcile,
    unresolved,
)
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .api import ComexioAPI
from .const import DOMAIN, FUB_BASE_KEY_FLANKE, ISSUE_TRACKER_URL, REFERENCE_MISSING_CATALOG_POLLS_BEFORE_ISSUE

_LOGGER = logging.getLogger(__name__)

# (kind, key) of every catalog entry the integration writes into plans — unusable ones raise a Repair.
REFERENCE_REQUIRED: tuple[tuple[str, str], ...] = ((KIND_FUB_BASE, FUB_BASE_KEY_FLANKE),)
ISSUE_TRANSLATION_KEY = "reference_catalog_mismatch"
# Same issue id, other text: the check did not run, so nothing is known about the firmware.
ISSUE_TRANSLATION_KEY_UNCHECKED = "reference_catalog_unchecked"
UNKNOWN_REFS_LOG_LIMIT = 20

# GitHub rejects new-issue URLs much beyond ~8 KB; stay well below so every browser opens it.
ISSUE_URL_MAX_CHARS = 7000
ISSUE_LIST_LIMIT = 30
ISSUE_TRUNCATED_LINE = "_… truncated — please attach the Home Assistant log._"


class ReferenceCatalogMonitor:
    """Per-entry state of the reference check: loaded files, last logged result, issue flag."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: ComexioAPI,
        server_id: str,
        listed_plans: Callable[[], Mapping[Any, Any] | None] | None = None,
    ) -> None:
        """listed_plans returns the plans the latest poll lists (None until a full poll read them)."""
        self._hass = hass
        self._api = api
        self._server_id = server_id
        self._listed_plans = listed_plans
        self._references: dict[str, ReferenceCatalog] | None = None
        self._fingerprint: tuple | None = None
        self._issue_active = False
        self._blocked: list[str] = []
        self._check_not_run: str | None = None
        self._integration_version: str | None = None
        self._unknown_refs: list[tuple[str, str, str]] | None = None
        self._missing_catalog_polls = 0

    @property
    def _issue_id(self) -> str:
        return f"{ISSUE_TRANSLATION_KEY}_{self._server_id}"

    async def async_check(self, raw_config: Mapping[str, Any]) -> None:
        """Reconcile against this poll's raw config and publish the result to the API.

        Never raises into the poll. A config without the block catalog (failed or partial fetch)
        keeps the previous result instead of reporting every block as missing.

        Deliberately broad except: this is a diagnostic over scraped, untrusted admin-page data
        (same reasoning as FunctionPlanCatalogManager.async_update_from_raw_config). On an
        unexpected error the check result is cleared, which blocks the dependent features and
        raises the Repair — failing closed, never silently.
        """
        try:
            if self._references is None:
                self._references = await self._hass.async_add_executor_job(load_reference_catalogs)
            if self._integration_version is None:
                # Own guard: a failed lookup is retried next poll instead of leaving "?" in the report.
                self._integration_version = await self._async_integration_version()
            check = reconcile(self._references, raw_config, self._api.comexio_version)
            if not check.fub_base_ids:
                self._handle_missing_live_catalog()
                return
            self._api.reference_check = check
            self._log(check)
            self._update_issue(unresolved(check, REFERENCE_REQUIRED))
        except Exception:
            _LOGGER.exception("[%s] Reference catalog check failed — dependent features blocked", self._server_id)
            self._api.reference_check = None
            self._update_issue(unresolved(None, REFERENCE_REQUIRED, REASON_CHECK_FAILED), REASON_CHECK_FAILED)

    def _handle_missing_live_catalog(self) -> None:
        """No block catalog in this poll: keep the last result only if it is for the same firmware.

        Block ids can move with a firmware update, so a result from another version (or none yet,
        right after setup) must not be trusted — that fails closed and is reported, not silent. The
        features are blocked at once. The Repair follows at once too once a check has succeeded
        since setup (e.g. a firmware update); before that it waits for
        REFERENCE_MISSING_CATALOG_POLLS_BEFORE_ISSUE polls without a catalog, so a partial first
        fetch right after setup doesn't flash a Repair that the next poll removes again. Counted in
        polls, not time: the scan interval can be up to a day, which only this setup case waits for.
        """
        previous = self._api.reference_check
        if previous is not None and previous.comexio_version == self._api.comexio_version:
            _LOGGER.debug(
                "[%s] Reference catalogs: no block catalog in this poll — keeping last result", self._server_id
            )
            return
        self._missing_catalog_polls += 1
        _LOGGER.warning(
            "[%s] Reference catalogs: Comexio %s sent no block catalog — dependent features blocked until it does",
            self._server_id,
            self._api.comexio_version,
        )
        self._api.reference_check = None
        checked_before = self._fingerprint is not None  # set by _log on every successful check
        if checked_before or self._missing_catalog_polls >= REFERENCE_MISSING_CATALOG_POLLS_BEFORE_ISSUE:
            self._update_issue(unresolved(None, REFERENCE_REQUIRED, REASON_NO_LIVE_CATALOG), REASON_NO_LIVE_CATALOG)

    def _log(self, check: ReferenceCheck) -> None:
        fingerprint = check.fingerprint()
        if fingerprint == self._fingerprint:
            _LOGGER.debug("[%s] Reference catalogs: %s", self._server_id, check.summary())
            return
        self._fingerprint = fingerprint
        _LOGGER.info("[%s] Reference catalogs: %s", self._server_id, check.summary())
        if deviations := format_deviations(check):
            # Moved ids are the norm (block ids follow installation order) and resolved via the
            # stable key, so they're INFO; only blocks that can't be used are worth a WARNING.
            _LOGGER.log(
                logging.WARNING if check.has_unusable() else logging.INFO,
                "[%s] Reference catalog deviations on Comexio %s:\n  %s",
                self._server_id,
                check.comexio_version,
                "\n  ".join(deviations),
            )

    async def _async_integration_version(self) -> str | None:
        try:
            version = (await async_get_integration(self._hass, DOMAIN)).version
        except IntegrationNotFound:
            return None
        return str(version) if version is not None else None

    def _issue_url(self) -> str:
        context = IssueContext(
            integration_version=self._integration_version,
            ha_version=HA_VERSION,
            blocked=tuple(self._blocked),
            unknown_plan_refs=tuple(self._unknown_refs or ()),
        )
        title, body = build_issue_report(self._api.reference_check, REFERENCE_REQUIRED, context)
        return github_issue_url(ISSUE_TRACKER_URL, title, body)

    def _update_issue(self, blocked: list[str], check_not_run: str | None = None) -> None:
        """Raise/refresh or clear the Repair; check_not_run is the reason when the check itself didn't run."""
        self._blocked = blocked
        self._check_not_run = check_not_run
        if blocked:
            issue_url = self._issue_url()
            # Re-created on every poll while blocked: async_create_issue updates the existing
            # issue in place, so the pre-filled report always reflects the latest check.
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_TRANSLATION_KEY_UNCHECKED if check_not_run else ISSUE_TRANSLATION_KEY,
                learn_more_url=issue_url,
                translation_placeholders={
                    "server_id": self._server_id,
                    "version": self._api.comexio_version or "?",
                    "blocks": ", ".join(blocked),
                    "issue_url": issue_url,
                    "reason": check_not_run or "",
                },
            )
            if not self._issue_active:
                _LOGGER.error(
                    "[%s] Blocks not usable on this Comexio (%s), dependent features disabled: %s",
                    self._server_id,
                    self._api.comexio_version,
                    ", ".join(blocked),
                )
            self._issue_active = True
        else:
            if self._issue_active:
                _LOGGER.info(
                    "[%s] Blocks usable again on this Comexio (%s), dependent features enabled",
                    self._server_id,
                    self._api.comexio_version,
                )
            # Unconditional (a no-op without an issue): an issue raised before an entry reload
            # must also be cleared by the fresh monitor, whose flag starts out False.
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
            self._issue_active = False

    def _refs_of_unloaded_plans(
        self, loaded: Mapping[Any, Any], present: Mapping[Any, Any]
    ) -> list[tuple[str, str, str]]:
        """The last findings of the plans still present but not in `loaded` — nothing to judge them by anew."""
        if not self._unknown_refs:
            return []
        loaded_ids = {str(fub_id) for fub_id in loaded}
        present_ids = {str(fub_id) for fub_id in present} - loaded_ids
        return [ref for ref in self._unknown_refs if ref[0] in present_ids]

    def check_plans(self, plans: Mapping[Any, Any]) -> None:
        """Log plan elements referencing block types the live catalog doesn't know (on change only).

        Without a catalog check the findings of plans no longer present are still dropped: deleted
        plans reference nothing. Present means listed by the latest poll, not merely in `plans`:
        a bulk snapshot can miss a live plan, which cannot be judged and keeps its last findings.
        """
        check = self._api.reference_check
        live_ids = check.fub_base_ids if check is not None else None
        listed = self._listed_plans() if self._listed_plans is not None else None
        present_plans = plans if listed is None else listed
        if live_ids:
            # Sorted: carried-over findings must not read as a change when a load misses or brings back a plan.
            found = sorted(
                find_unknown_fub_base_refs(plans, live_ids) + self._refs_of_unloaded_plans(plans, present_plans)
            )
        elif not present_plans:
            found = []
        elif self._unknown_refs is None:
            return  # never judged: no findings to drop, and no catalog to judge the plans by
        else:
            found = self._refs_of_unloaded_plans({}, present_plans)
        if found == self._unknown_refs:
            return
        self._unknown_refs = found
        if self._blocked:
            # Add the plan findings to the pre-filled report, keeping why the check didn't run.
            self._update_issue(self._blocked, self._check_not_run)
        if not found and present_plans and not live_ids:
            _LOGGER.info(
                "[%s] Function plans: findings of deleted plans dropped; the remaining plans are not checked "
                "for unknown block types until the block catalog is available",
                self._server_id,
            )
            return
        if not found:
            _LOGGER.info("[%s] Function plans: no elements with unknown block types", self._server_id)
            return
        shown = [f"{fub}/{elem}: 5 {ref}" for fub, elem, ref in found[:UNKNOWN_REFS_LOG_LIMIT]]
        if len(found) > UNKNOWN_REFS_LOG_LIMIT:
            shown.append(f"... {len(found) - UNKNOWN_REFS_LOG_LIMIT} more")
        _LOGGER.warning(
            "[%s] Function plans: %d element(s) reference a block type unknown to Comexio %s "
            "(shown as 'Configuration fault' in the editor): %s",
            self._server_id,
            len(found),
            self._api.comexio_version,
            ", ".join(shown),
        )


# ---------------------------------------------------------------------------
# Pre-filled GitHub issue for a blocked required entry
# ---------------------------------------------------------------------------


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
    if owners := [key for key, ids in (check.live_ids.get(kind) or {}).items() if ref_id in ids]:
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
            if key_family(live_key) == key_family(key)
        )
        lines += [
            f"- `{kind}:{key}`: status `{entry.status if entry else REASON_NOT_CHECKED}`, reference id {ref_id}, "
            f"live id {entry.live_id if entry else None}",
            f"  - reference id {ref_id} on this server: {_ref_id_owner(check, kind, ref_id)}",
            f"  - live blocks named `{key_family(key)}`: {', '.join(same_family) or 'none'}",
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
    """New-issue URL with title/body pre-filled, the body cut line by line to fit max_chars.

    Ends at a body of only the truncation marker, so even a single over-long line is dropped.
    """
    lines = body.split("\n")
    while True:
        query = urlencode({"title": title, "body": "\n".join(lines), "labels": "bug"})
        url = f"{issues_url}/new?{query}"
        if len(url) <= max_chars or lines == [ISSUE_TRUNCATED_LINE]:
            return url
        lines = [*lines[: -2 if lines[-1] == ISSUE_TRUNCATED_LINE else -1], ISSUE_TRUNCATED_LINE]
