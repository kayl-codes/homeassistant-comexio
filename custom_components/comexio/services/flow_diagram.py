# Version: 0.1.0
"""function_plan_flow_diagram — on-demand signal-flow diagram for a Function Plan.

Same source resolution and "always return a dict" contract as function_plan_analyze (see that
module's docstring for why: function_plan_visualize's text-diagram path returns None, which
crashes an MCP caller that requests return_response — this handler avoids that trap since the
Plan Preview card always requests a response). Renders via
function_plan_render_flow.render_flow_svg instead of the Studio-position render_plan_svg — the
SVG comes back inline in the response, not written to a file (unlike the cached/polled preview
path behind function_plan_visualize's format='svg').
"""

from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall

from ..function_plan_backup import snapshot_label_maps
from ..function_plan_render_flow import render_flow_svg
from .plan_actions import _resolve_visualize_live_source, _resolve_visualize_snapshot_source

_TITLE_FLOW_ERR = "Function Plan Flow Diagram — Error"


def _empty_result(error: str) -> dict[str, Any]:
    return {
        "fub_id": None,
        "plan_name": None,
        "source": None,
        "element_count": 0,
        "skipped_count": 0,
        "svg": None,
        "error": error,
    }


async def _handle_function_plan_flow_diagram(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """Render a live plan or a stored backup snapshot as a topologically-layered signal-flow
    diagram (input -> logic -> output columns) instead of Comexio Studio's physical layout.

    Data resolution mirrors handle_function_plan_visualize/analyze exactly (same 'snapshot'
    field, same live/backup source split) — only the rendering differs.
    """
    snapshot_raw = call.data.get("snapshot")
    source_result = (
        await _resolve_visualize_snapshot_source(hass, call, str(snapshot_raw), _TITLE_FLOW_ERR)
        if snapshot_raw
        else await _resolve_visualize_live_source(hass, call, _TITLE_FLOW_ERR)
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

    svg, skipped_count = render_flow_svg(
        elements, connections, catalog, markers_by_id, webio_by_id, ios_by_id, plan_name
    )
    return {
        "fub_id": fub_id,
        "plan_name": plan_name,
        "source": source,
        "element_count": len(elements),
        "skipped_count": skipped_count,
        "svg": svg,
        "error": None,
    }
