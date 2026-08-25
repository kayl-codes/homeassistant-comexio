# Version: 0.9.4
"""function_plan_analyze — manual, read-only "plan health check" for a Function Plan.

Never scheduled/automatic and never posts a persistent_notification on success: the
service response IS the result, rendered by the Plan Preview card in a popup dialog
(comexio-plan-card.js's .analysis-dialog) — mirrors function_plan_search's
response-only pattern (services/misc.py). Always returns a dict (never None), even on
a resolution failure, unlike function_plan_visualize's text-diagram path — that one's
None return crashes an MCP caller that requests return_response (see project notes);
this handler avoids the same trap since the card always requests a response.
"""

from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall

from ..function_plan_analysis import analyze_function_plan
from ..function_plan_backup import snapshot_label_maps
from .plan_actions import _resolve_visualize_live_source, _resolve_visualize_snapshot_source

_TITLE_ANALYZE_ERR = "Function Plan Analyze — Error"


def _empty_result(error: str) -> dict[str, Any]:
    return {
        "fub_id": None,
        "plan_name": None,
        "source": None,
        "element_count": 0,
        "connection_count": 0,
        "finding_count": 0,
        "findings": [],
        "error": error,
    }


async def _handle_function_plan_analyze(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """Analyze a live plan or a stored backup snapshot for likely wiring issues.

    Data resolution mirrors handle_function_plan_visualize exactly (same 'snapshot' field,
    same live/backup source split) — the analysis just runs a findings pass instead of
    rendering, over the identical elements/connections/catalog shape.
    """
    snapshot_raw = call.data.get("snapshot")
    source_result = (
        await _resolve_visualize_snapshot_source(hass, call, str(snapshot_raw), _TITLE_ANALYZE_ERR)
        if snapshot_raw
        else await _resolve_visualize_live_source(hass, call, _TITLE_ANALYZE_ERR)
    )
    if source_result is None:
        # The resolver already posted a persistent_notification describing the failure.
        return _empty_result("Plan could not be resolved — see notification for details.")
    coordinator, _api, fub_id, plan_name, elements, connections, source, label_metadata = source_result

    markers_by_id, webio_by_id, ios_by_id = coordinator.function_plan_label_maps()
    if label_metadata:
        markers_by_id, webio_by_id, ios_by_id = snapshot_label_maps(
            label_metadata, markers_by_id, webio_by_id, ios_by_id
        )
    catalog = await coordinator.function_plan_catalog.async_get_catalog()

    findings = analyze_function_plan(elements, connections, catalog, markers_by_id, webio_by_id, ios_by_id)
    return {
        "fub_id": fub_id,
        "plan_name": plan_name,
        "source": source,
        "element_count": len(elements),
        "connection_count": len(connections),
        "finding_count": len(findings),
        "findings": findings,
    }
