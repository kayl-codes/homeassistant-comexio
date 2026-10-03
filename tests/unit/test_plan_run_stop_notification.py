"""function_plan_stop/activate: success only logs (the run-state sensor shows it), failure notifies."""

import logging
from unittest.mock import patch

import pytest

from custom_components.comexio.services import plan_actions


@pytest.mark.parametrize("action", ["Stop", "Activate"])
def test_success_only_logs(action: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=plan_actions.__name__)
    with patch.object(plan_actions.persistent_notification, "async_create") as notify:
        plan_actions._report_run_stop_result(
            None, "Plan 'Licht' (ID 43) ok.\nDuration: 1.0s", success=True, action=action
        )
    notify.assert_not_called()
    assert f"Function Plan {action}: Plan 'Licht' (ID 43) ok., Duration: 1.0s" in caplog.text


def test_success_notifies_when_enabled_in_const() -> None:
    with (
        patch.object(plan_actions, "FUNCTION_PLAN_RUN_STOP_SUCCESS_NOTIFICATION", True),
        patch.object(plan_actions.persistent_notification, "async_create") as notify,
    ):
        plan_actions._report_run_stop_result(None, "ok", success=True, action="Stop")
    assert notify.call_args.kwargs["title"] == "Function Plan Stop — OK"


def test_failure_always_notifies() -> None:
    with patch.object(plan_actions.persistent_notification, "async_create") as notify:
        plan_actions._report_run_stop_result(None, "Stop failed", success=False, action="Stop")
    assert notify.call_args.kwargs["title"] == "Function Plan Stop — Error"
