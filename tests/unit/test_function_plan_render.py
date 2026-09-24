"""Function plan SVG preview rendering and the read-only plan health check."""

from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from custom_components.comexio.function_plan_analysis import analyze_function_plan
from custom_components.comexio.function_plan_render import render_plan_svg
from custom_components.comexio.function_plan_render_selfreset import detect_self_reset_cycles
from tests.common import load_json_fixture


class SvgSnapshotExtension(SingleFileSnapshotExtension):
    """Store each SVG snapshot as its own .svg file, so it can be opened and eyeballed directly."""

    file_extension = "svg"
    _write_mode = WriteMode.TEXT


@pytest.fixture
def svg_snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    return snapshot.use_extension(SvgSnapshotExtension)


@pytest.fixture
def plan() -> dict[str, Any]:
    return load_json_fixture("function_plan.json")


def _render(plan: dict[str, Any], **kwargs: Any) -> str:
    return render_plan_svg(
        plan["elements"],
        plan["connections"],
        plan["catalog"],
        plan["markers_by_id"],
        plan["webio_by_id"],
        plan["ios_by_id"],
        title="Test <Plan>",
        **kwargs,
    )


def test_render_fit_to_content(plan: dict[str, Any], svg_snapshot: SnapshotAssertion) -> None:
    assert _render(plan) == svg_snapshot


def test_render_on_paper_canvas(plan: dict[str, Any], svg_snapshot: SnapshotAssertion) -> None:
    assert _render(plan, title_suffix="aktiv · A4", canvas=(870.0, 720.0)) == svg_snapshot


def test_render_escapes_title(plan: dict[str, Any]) -> None:
    svg = _render(plan)

    assert "Test &lt;Plan&gt;" in svg
    assert "<Plan>" not in svg


def test_render_empty_plan_does_not_crash() -> None:
    svg = render_plan_svg({}, {}, {}, {}, {}, {}, title="Leer")

    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")


def test_self_reset_cycle_is_detected(plan: dict[str, Any]) -> None:
    assert detect_self_reset_cycles(plan["elements"], plan["connections"], plan["catalog"]) == [("8", "9")]


def test_analyze_function_plan(plan: dict[str, Any], snapshot: SnapshotAssertion) -> None:
    findings = analyze_function_plan(
        plan["elements"],
        plan["connections"],
        plan["catalog"],
        plan["markers_by_id"],
        plan["webio_by_id"],
        plan["ios_by_id"],
    )

    assert findings == snapshot
