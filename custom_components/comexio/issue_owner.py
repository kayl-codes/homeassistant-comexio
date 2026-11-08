"""Owner link of the repair issues: Home Assistant removes them together with their config entry.

Home Assistant 2026.11 added config_entry_id to ir.async_create_issue. The HACS minimum is still 2026.8, and an
older Home Assistant does not know the keyword, so it is passed only when the running version accepts it.
"""

from __future__ import annotations

import inspect
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)

OWNER_KEYWORD_SUPPORTED = "config_entry_id" in inspect.signature(ir.async_create_issue).parameters


def issue_owner(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    """Keyword arguments that link a repair issue to its config entry, to be splatted into async_create_issue.

    Empty before Home Assistant 2026.11, and once the entry is gone: HA refuses a link to an unknown entry,
    so such an issue stays unlinked (as before this link existed) instead of failing the poll that raises it.
    """
    if not OWNER_KEYWORD_SUPPORTED:
        return {}
    if hass.config_entries.async_get_entry(entry_id) is None:
        _LOGGER.debug("Repair for removed config entry %s is created unlinked", entry_id)
        return {}
    return {"config_entry_id": entry_id}
