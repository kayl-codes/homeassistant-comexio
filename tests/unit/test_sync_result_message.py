"""Sync result notification text (button.py): counts in words, no signed "+0/-0"."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.comexio.button import ComexioSyncButton, _format_counts


def test_format_counts_uses_words_not_signs() -> None:
    assert _format_counts(0, 0, 0, 0) == "0 added, 0 updated, 0 renamed, 0 removed"
    assert _format_counts(3, 1, 2, 4) == "3 added, 1 updated, 2 renamed, 4 removed"


def test_per_class_note_lists_only_changed_classes_in_words() -> None:
    per_class = {
        "marker": {"added": 2, "updated": 0, "renamed": 0, "removed": 1},
        "io": {"added": 0, "updated": 1, "renamed": 0, "removed": 0},
        "knx": {"added": 0, "updated": 0, "renamed": 0, "removed": 0},
    }

    note = ComexioSyncButton._build_per_class_note(per_class)

    assert "2 added, 0 updated, 0 renamed, 1 removed" in note
    assert "0 added, 1 updated, 0 renamed, 0 removed" in note
    assert note.count("\n") == 2
    assert "+" not in note
    assert "-" not in note


class _FakeWebioApi:
    """Records Web-IO writes; the ones named in `failing` report failure (False)."""

    def __init__(self, failing: set[str]) -> None:
        self.failing = failing
        self.calls: list[tuple[str, object]] = []

    async def delete_single_command(self, cmd_id, device_id) -> bool:
        self.calls.append(("delete", cmd_id))
        return cmd_id not in self.failing

    async def save_single_command(self, base_id, device_id, payload, existing_cmd_id=None) -> bool:
        self.calls.append(("save", existing_cmd_id))
        return payload["Name"] not in self.failing


def _task(t_type: str, name: str, cmd_id: str | None = None) -> dict:
    return {"type": t_type, "item": {"name": name, "id": cmd_id, "payload": {"Name": name}}}


def _empty_result() -> dict:
    return {"added": 0, "removed": 0, "updated": 0, "renamed": 0, "created_names": []}


@pytest.mark.parametrize(
    ("task", "counter"),
    [
        (_task("rename", "M1 Light", "11"), "renamed"),
        (_task("delete", "M2 Old", "12"), "removed"),
        (_task("type", "M3 Dimmer", "13"), "updated"),
        (_task("create", "M4 New"), "added"),
    ],
)
def test_delta_task_counts_only_successful_writes(task: dict, counter: str) -> None:
    ok_result, failed_result, failed_writes = _empty_result(), _empty_result(), []
    failing_key = task["item"]["id"] if task["type"] == "delete" else task["item"]["name"]

    asyncio.run(ComexioSyncButton._apply_delta_task(_FakeWebioApi(set()), "7", "3", task, ok_result, []))
    asyncio.run(
        ComexioSyncButton._apply_delta_task(_FakeWebioApi({failing_key}), "7", "3", task, failed_result, failed_writes)
    )

    assert ok_result[counter] == 1
    assert failed_result[counter] == 0
    assert failed_writes == [f"{task['type']} {task['item']['name']}"]
    # A failed create must not reach the function plan wiring pass.
    assert failed_result["created_names"] == []


def test_delta_task_create_has_no_command_id_update_keeps_it() -> None:
    api = _FakeWebioApi(set())
    result = _empty_result()

    asyncio.run(ComexioSyncButton._apply_delta_task(api, "7", "3", _task("create", "M4 New", "99"), result, []))
    asyncio.run(ComexioSyncButton._apply_delta_task(api, "7", "3", _task("rename", "M1 Light", "11"), result, []))

    assert api.calls == [("save", None), ("save", "11")]
    assert result["created_names"] == ["M4 New"]


def _message(**overrides) -> str:
    kwargs = {
        "added": 0,
        "updated": 0,
        "renamed": 0,
        "removed": 0,
        "recreated_classes": [],
        "updated_ip": False,
        "duration_str": "0:05 min",
    } | overrides
    owner = SimpleNamespace(_build_per_class_note=ComexioSyncButton._build_per_class_note)
    return ComexioSyncButton._build_sync_result_message(owner, **kwargs)


def test_failed_writes_turn_the_result_into_an_error_report() -> None:
    msg = _message(removed=2, failed_writes=["delete M2 Old", "Marker: server address update"])

    assert "Sync Finished with errors" in msg
    assert "2 Web-IO write(s) failed: delete M2 Old, Marker: server address update." in msg


def test_failed_write_does_not_read_as_address_updated() -> None:
    # One class updated its address, another class's update failed: no "all good" headline.
    msg = _message(updated_ip=True, failed_writes=["IO: server address update"])

    assert "Server Address updated" not in msg
    assert "Sync Finished with errors" in msg


def test_failed_writes_note_caps_the_named_list() -> None:
    msg = _message(failed_writes=[f"create M{i}" for i in range(13)])

    assert "13 Web-IO write(s) failed" in msg
    assert "create M9" in msg
    assert "create M10" not in msg
    assert "(+3 more)" in msg


def test_clean_sync_keeps_the_success_headline() -> None:
    msg = _message(added=1, failed_writes=[])

    assert "**Comexio Sync Finished**" in msg
    assert "failed" not in msg


def test_failed_address_update_alone_does_not_claim_nothing_was_needed() -> None:
    msg = _message(failed_writes=["Marker: server address update"])

    assert "no changes applied" in msg
    assert "no changes needed" not in msg
