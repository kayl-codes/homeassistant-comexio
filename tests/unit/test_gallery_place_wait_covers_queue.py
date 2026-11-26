"""Guard on the gallery's place wait: it covers the queue the waiter cap admits (SH-387, a formula check only)."""

from custom_components.comexio.const import (
    DOORSTATION_GALLERY_MAX_WAITING_READS,
    DOORSTATION_GALLERY_PLACE_WAIT_SEC,
    DOORSTATION_PICTURE_LIST_TIMEOUT_SEC,
    DOORSTATION_PICTURE_READ_TIMEOUT_SEC,
)


def test_the_place_wait_is_at_least_the_queue_the_waiter_cap_admits() -> None:
    """The holder plus the cap's reads, one after another, fit in the place wait. Timing is not measured here."""
    # The longest hold of a place: a folder list (10 s) holds it longer than a picture (8 s).
    longest_hold = max(DOORSTATION_PICTURE_READ_TIMEOUT_SEC, DOORSTATION_PICTURE_LIST_TIMEOUT_SEC)
    reads_ahead = DOORSTATION_GALLERY_MAX_WAITING_READS + 1
    assert reads_ahead * longest_hold <= DOORSTATION_GALLERY_PLACE_WAIT_SEC
