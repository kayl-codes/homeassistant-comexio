"""Guard on the gallery's place wait: it covers the queue the waiter cap admits (SH-387, a formula check only)."""

from custom_components.comexio.const import (
    DOORSTATION_GALLERY_LIST_TIMEOUT_SEC,
    DOORSTATION_GALLERY_MAX_WAITING_READS,
    DOORSTATION_GALLERY_PLACE_WAIT_SEC,
    DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC,
)


def test_the_place_wait_is_at_least_the_queue_the_waiter_cap_admits() -> None:
    """The holder plus the cap's reads, one after another, fit in the place wait. Timing is not measured here."""
    # The longest hold of a place is a thumbnail's whole budget (30 s), its wait for the Doorstation lock included:
    # the camera and the picture entities take that lock too. Not the lock hold alone (8 s / 10 s, held_for_sec).
    longest_hold = max(DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC, DOORSTATION_GALLERY_LIST_TIMEOUT_SEC)
    reads_ahead = DOORSTATION_GALLERY_MAX_WAITING_READS + 1
    assert reads_ahead * longest_hold <= DOORSTATION_GALLERY_PLACE_WAIT_SEC
