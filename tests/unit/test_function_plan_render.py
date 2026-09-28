"""Function plan SVG preview rendering and the read-only plan health check."""

import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from custom_components.comexio.function_plan_analysis import analyze_function_plan
from custom_components.comexio.function_plan_render import render_plan_svg
from custom_components.comexio.function_plan_render_flow import render_flow_svg
from custom_components.comexio.function_plan_render_selfreset import detect_self_reset_cycles
from custom_components.comexio.function_plan_render_values import element_id_sort_key
from tests.common import load_json_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]

# Renders the fixture plan's flow diagram in a fresh interpreter and writes the SVG to stdout.
_FLOW_RENDER_SCRIPT = """
import sys
from custom_components.comexio.function_plan_render_flow import render_flow_svg
from tests.common import load_json_fixture

plan = load_json_fixture("function_plan.json")
svg, _ = render_flow_svg(
    plan["elements"],
    plan["connections"],
    plan["catalog"],
    plan["markers_by_id"],
    plan["webio_by_id"],
    plan["ios_by_id"],
    title="T",
)
sys.stdout.buffer.write(svg.encode("utf-8"))
"""

# Marker "1" feeds two on_pulse timers ("2", "10") that both reset it — two self-reset
# cycles whose order came from set iteration (PYTHONHASHSEED) before the fix.
_SELF_RESET_SCRIPT = """
from custom_components.comexio.function_plan_render_selfreset import detect_self_reset_cycles

elements = {
    "1": {"reference": {"type": 2, "ref_id": 1}},
    "2": {"reference": {"type": 4, "ref_id": 1}},
    "10": {"reference": {"type": 4, "ref_id": 1}},
}
connections = {
    str(i): {"input": {"FubElementId": src}, "output": [{"FubElementId": dst}]}
    for i, (src, dst) in enumerate([(1, 2), (1, 10), (2, 1), (10, 1)])
}
catalog = {"time_modules": {"1": {"kind": "on_pulse"}}}
print(detect_self_reset_cycles(elements, connections, catalog))
"""

_HASH_SEEDS = ("1", "2", "3", "4", "5", "6")


def _run_with_hash_seed(script: str, seed: str) -> bytes:
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONHASHSEED": seed},
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


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


def test_render_plan_id_marks_root_svg(plan: dict[str, Any]) -> None:
    svg = _render(plan, plan_id='7"<x>')

    assert svg.startswith("<svg ")
    assert 'data-plan-id="7&quot;&lt;x&gt;"' in svg.split(">", 1)[0]
    assert "data-plan-id" not in _render(plan)


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


def _render_flow(plan: dict[str, Any]) -> tuple[str, int]:
    return render_flow_svg(
        plan["elements"],
        plan["connections"],
        plan["catalog"],
        plan["markers_by_id"],
        plan["webio_by_id"],
        plan["ios_by_id"],
        title="T",
    )


def test_render_flow_diagram(plan: dict[str, Any], svg_snapshot: SnapshotAssertion) -> None:
    svg, skipped = _render_flow(plan)

    assert skipped == 1
    assert "1 unverdrahtete Element(e) ausgeblendet" in svg
    assert svg.count('class="flow-edge"') == 6
    assert "⟲ M4 Klingel [TRIG]</text>" in svg
    assert "⟲ T1 Taster Reset</text>" in svg
    assert svg == svg_snapshot


def test_render_flow_is_independent_of_hash_seed() -> None:
    """Set iteration order depends on PYTHONHASHSEED, i.e. changes with every HA restart — the
    flow diagram (layering of a self-reset cycle, edge order, data-net ids) must not."""
    outputs = [_run_with_hash_seed(_FLOW_RENDER_SCRIPT, seed) for seed in ("1", "2", "3")]

    assert outputs[0]
    assert outputs[1] == outputs[0]
    assert outputs[2] == outputs[0]


@pytest.mark.parametrize("seed", _HASH_SEEDS)
def test_self_reset_cycle_order_is_independent_of_hash_seed(seed: str) -> None:
    output = _run_with_hash_seed(_SELF_RESET_SCRIPT, seed)

    assert output.decode("utf-8").strip() == "[('1', '2'), ('1', '10')]"


def test_element_id_sort_key_orders_numerically_and_never_raises() -> None:
    ids = ["10", "b", "2", "²", "1"]

    assert sorted(ids, key=element_id_sort_key) == ["1", "2", "10", "b", "²"]
