"""Runs the reference catalog check each poll and reports it (log + Repair issue).

The pure reconciliation lives in reference_catalog.py; this module adds the Home Assistant side:
loading the reference files off the event loop, publishing the result to the API (which resolves
block-type ids from it), logging only on change, and a Repair issue while a block the
integration writes is not usable on this server. The Repair carries a link that opens a GitHub
issue pre-filled with the diagnosis (reference_catalog.build_issue_report), so the user can
report it with one click.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .api import ComexioAPI
from .const import DOMAIN, FUB_BASE_KEY_FLANKE, ISSUE_TRACKER_URL
from .reference_catalog import (
    KIND_FUB_BASE,
    REASON_CHECK_FAILED,
    REASON_NO_LIVE_CATALOG,
    IssueContext,
    ReferenceCatalog,
    ReferenceCheck,
    build_issue_report,
    find_unknown_fub_base_refs,
    format_deviations,
    github_issue_url,
    load_reference_catalogs,
    reconcile,
    unresolved,
)

_LOGGER = logging.getLogger(__name__)

# (kind, key) of every catalog entry the integration writes into plans — unusable ones raise a Repair.
REFERENCE_REQUIRED: tuple[tuple[str, str], ...] = ((KIND_FUB_BASE, FUB_BASE_KEY_FLANKE),)
ISSUE_TRANSLATION_KEY = "reference_catalog_mismatch"
UNKNOWN_REFS_LOG_LIMIT = 20


class ReferenceCatalogMonitor:
    """Per-entry state of the reference check: loaded files, last logged result, issue flag."""

    def __init__(self, hass: HomeAssistant, api: ComexioAPI, server_id: str) -> None:
        self._hass = hass
        self._api = api
        self._server_id = server_id
        self._references: dict[str, ReferenceCatalog] | None = None
        self._fingerprint: tuple | None = None
        self._issue_active = False
        self._blocked: list[str] = []
        self._integration_version: str | None = None
        self._unknown_refs: list[tuple[str, str, str]] | None = None

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
            self._update_issue(unresolved(None, REFERENCE_REQUIRED, REASON_CHECK_FAILED))

    def _handle_missing_live_catalog(self) -> None:
        """No block catalog in this poll: keep the last result only if it is for the same firmware.

        Block ids can move with a firmware update, so a result from another version (or none yet,
        right after setup) must not be trusted — that fails closed and is reported, not silent.
        """
        previous = self._api.reference_check
        if previous is not None and previous.comexio_version == self._api.comexio_version:
            _LOGGER.debug(
                "[%s] Reference catalogs: no block catalog in this poll — keeping last result", self._server_id
            )
            return
        _LOGGER.warning(
            "[%s] Reference catalogs: Comexio %s sent no block catalog — dependent features blocked until it does",
            self._server_id,
            self._api.comexio_version,
        )
        self._api.reference_check = None
        self._update_issue(unresolved(None, REFERENCE_REQUIRED, REASON_NO_LIVE_CATALOG))

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

    def _update_issue(self, blocked: list[str]) -> None:
        self._blocked = blocked
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
                translation_key=ISSUE_TRANSLATION_KEY,
                learn_more_url=issue_url,
                translation_placeholders={
                    "server_id": self._server_id,
                    "version": self._api.comexio_version or "?",
                    "blocks": ", ".join(blocked),
                    "issue_url": issue_url,
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
            # Unconditional (a no-op without an issue): an issue raised before an entry reload
            # must also be cleared by the fresh monitor, whose flag starts out False.
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
            self._issue_active = False

    def check_plans(self, plans: Mapping[Any, Any]) -> None:
        """Log plan elements referencing block types the live catalog doesn't know (on change only)."""
        check = self._api.reference_check
        if check is None or not check.fub_base_ids:
            return
        found = find_unknown_fub_base_refs(plans, check.fub_base_ids)
        if found == self._unknown_refs:
            return
        self._unknown_refs = found
        if self._blocked:
            self._update_issue(self._blocked)  # add the plan findings to the pre-filled report
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
