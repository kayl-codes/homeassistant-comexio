"""Sync result notification text (button.py): counts in words, no signed "+0/-0"."""

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
