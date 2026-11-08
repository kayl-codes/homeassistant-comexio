"""Repair issues of a config entry: raised only while the entry exists, and linked to it where HA supports that.

Home Assistant 2026.11 added config_entry_id to ir.async_create_issue; an issue created with it is removed
together with its entry. The HACS minimum is still 2026.8, and an older Home Assistant does not know the keyword,
so it is passed only when the running version accepts it. Every repair of the integration is raised through
async_create_entry_issue (tests/unit/test_issue_owner.py fails on a direct ir.async_create_issue).
"""

from __future__ import annotations

from collections.abc import Callable
import inspect
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

OWNER_KEYWORD = "config_entry_id"


def accepts_owner_keyword(create_issue: Callable[..., object]) -> bool:
    """Whether create_issue, Home Assistant's ir.async_create_issue, takes the entry link keyword."""
    return OWNER_KEYWORD in inspect.signature(create_issue).parameters


OWNER_KEYWORD_SUPPORTED = accepts_owner_keyword(ir.async_create_issue)


def async_create_entry_issue(
    hass: HomeAssistant,
    entry_id: str,
    issue_id: str,
    *,
    is_fixable: bool,
    severity: ir.IssueSeverity,
    translation_key: str,
    translation_placeholders: dict[str, str] | None = None,
    # Home Assistant annotates scalar values only, but stores the data as JSON; several repairs hand their
    # flow nested counts and item lists.
    data: dict[str, Any] | None = None,
    is_persistent: bool = False,
    learn_more_url: str | None = None,
) -> None:
    """Raise the repair issue_id of config entry entry_id; the keywords are those of ir.async_create_issue.

    Skipped once the entry is gone (a poll or monitor finishing after the removal): its dialog would have no
    coordinator to run on, and Home Assistant refuses a link to an unknown entry. That is an expected end of a
    race, not an error, hence the debug level.
    """
    if hass.config_entries.async_get_entry(entry_id) is None:
        _LOGGER.debug("Repair %s is not raised: config entry %s was removed", issue_id, entry_id)
        return
    owner = {OWNER_KEYWORD: entry_id} if OWNER_KEYWORD_SUPPORTED else {}
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=is_fixable,
        severity=severity,
        translation_key=translation_key,
        translation_placeholders=translation_placeholders,
        data=data,
        is_persistent=is_persistent,
        learn_more_url=learn_more_url,
        **owner,
    )
