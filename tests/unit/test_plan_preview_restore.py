"""'Preview info' survives a restart: its last state stands for the last rendered preview."""

import pytest

from custom_components.comexio.sensor import restored_plan_preview

GENERATED_AT = "2026-10-03T05:30:00+00:00"


def test_last_state_becomes_the_last_preview() -> None:
    attributes = {"fub_id": 19, "source": "snapshot:auto:0", "generated_at": GENERATED_AT}

    assert restored_plan_preview("Test1", attributes) == {
        "fub_id": 19,
        "plan_name": "Test1",
        "source": "snapshot:auto:0",
        "generated_at": GENERATED_AT,
    }


def test_state_from_before_the_fub_id_attribute_is_restored_without_it() -> None:
    restored = restored_plan_preview("Test1", {"source": "live", "generated_at": GENERATED_AT})

    assert restored is not None
    assert restored["fub_id"] is None
    assert restored["plan_name"] == "Test1"


@pytest.mark.parametrize(
    ("state", "attributes"),
    [
        ("unknown", {"generated_at": GENERATED_AT}),
        ("unavailable", {"generated_at": GENERATED_AT}),
        ("Test1", {"source": "live", "generated_at": None}),
        ("Test1", {}),
    ],
    ids=["never-rendered", "unavailable", "no-timestamp", "no-attributes"],
)
def test_nothing_to_restore(state: str, attributes: dict) -> None:
    assert restored_plan_preview(state, attributes) is None
