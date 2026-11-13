"""_dispatch_push: a push without its value key is refused for every kind; an explicit null still goes on."""

from unittest.mock import MagicMock

import pytest

from custom_components.comexio import _WEBHOOK_MISSING_VALUE_LOG_MSG, _dispatch_push

SERVER = "iosrv1"
UPDATERS = ("update_io_by_name", "update_quickbus_io", "update_onewire", "update_knx", "update_marker")

_NO_VALUE_KEY_PAYLOADS = [
    ({"type": "io", "ext": "BASE", "io": "AI1"}, "IO"),
    ({"type": "quickbus", "id": "201"}, "Quickbus"),
    ({"type": "onewire", "id": "OT1"}, "1-Wire"),
    ({"type": "knx", "id": "4"}, "KNX"),
    ({"type": "marker", "id": "5"}, "marker"),
    ({"id": "5"}, "marker"),
]


@pytest.mark.parametrize(("payload", "label"), _NO_VALUE_KEY_PAYLOADS)
def test_a_push_without_a_value_key_is_refused(payload: dict[str, object], label: str) -> None:
    coordinator, security = MagicMock(server_id=SERVER), MagicMock()

    _dispatch_push(coordinator, security, payload)

    for updater in UPDATERS:
        getattr(coordinator, updater).assert_not_called()
    security.warn_refused.assert_called_once_with(
        f"missing-value-{label}", _WEBHOOK_MISSING_VALUE_LOG_MSG, SERVER, label
    )


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"type": "io", "ext": "BASE"}, "missing-io"),
        ({"type": "io", "io": "AI1"}, "missing-io"),
        ({"type": "knx"}, "missing-id-KNX"),
        ({"type": "marker"}, "missing-id-marker"),
    ],
)
def test_a_missing_address_is_named_before_a_missing_value(payload: dict[str, object], kind: str) -> None:
    """Neither the address nor the value is there: the line says the address is missing, once."""
    coordinator, security = MagicMock(server_id=SERVER), MagicMock()

    _dispatch_push(coordinator, security, payload)

    for updater in UPDATERS:
        getattr(coordinator, updater).assert_not_called()
    security.warn_refused.assert_called_once()
    assert security.warn_refused.call_args.args[0] == kind


@pytest.mark.parametrize(
    ("payload", "updater", "call_args"),
    [
        ({"type": "io", "ext": "BASE", "io": "I1", "value": 0}, "update_io_by_name", ("BASE", "I1", 0)),
        ({"type": "io", "ext": "BASE", "io": "I1", "value": ""}, "update_io_by_name", ("BASE", "I1", "")),
        ({"type": "quickbus", "id": "201", "value": 0}, "update_quickbus_io", ("201", 0)),
        ({"type": "onewire", "id": "OT1", "value": 0}, "update_onewire", ("OT1", 0)),
        ({"type": "knx", "id": "4", "value": 0}, "update_knx", ("4", 0)),
        ({"type": "marker", "id": "5", "value": 0}, "update_marker", ("5", 0)),
        ({"type": "marker", "id": "5", "value": ""}, "update_marker", ("5", "")),
    ],
)
def test_a_falsy_value_is_still_a_reading(payload: dict[str, object], updater: str, call_args: tuple) -> None:
    """0 and "" are values (an off switch, an empty text the IO path turns into unknown), not a missing one."""
    coordinator, security = MagicMock(server_id=SERVER), MagicMock()

    _dispatch_push(coordinator, security, payload)

    getattr(coordinator, updater).assert_called_once_with(*call_args)
    security.warn_refused.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "updater", "call_args"),
    [
        ({"type": "io", "ext": "BASE", "io": "AI1", "value": None}, "update_io_by_name", ("BASE", "AI1", None)),
        ({"type": "quickbus", "id": "201", "value": None}, "update_quickbus_io", ("201", None)),
        ({"type": "onewire", "id": "OT1", "value": None}, "update_onewire", ("OT1", None)),
        ({"type": "knx", "id": "4", "value": None}, "update_knx", ("4", None)),
        ({"type": "marker", "id": "5", "value": None}, "update_marker", ("5", None)),
    ],
)
def test_an_explicit_null_goes_on_to_the_coordinator(
    payload: dict[str, object], updater: str, call_args: tuple
) -> None:
    """null says the reading is none: the coordinator stores it as unknown (like "nan"), it is not dropped here."""
    coordinator, security = MagicMock(server_id=SERVER), MagicMock()

    _dispatch_push(coordinator, security, payload)

    getattr(coordinator, updater).assert_called_once_with(*call_args)
    security.warn_refused.assert_not_called()
