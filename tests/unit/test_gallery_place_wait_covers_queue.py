"""The gallery's place wait covers every thumbnail the waiter cap admits (the queue behind one lock, SH-387)."""

from custom_components.comexio.const import (
    DOORSTATION_GALLERY_MAX_WAITING_READS,
    DOORSTATION_GALLERY_PLACE_WAIT_SEC,
    DOORSTATION_PICTURE_READ_TIMEOUT_SEC,
)


def test_the_place_wait_covers_the_holder_and_every_admitted_waiter() -> None:
    """A thumbnail admitted at the cap waits for the holder and up to the cap's worth of reads, one after another."""
    reads_ahead = DOORSTATION_GALLERY_MAX_WAITING_READS + 1
    assert reads_ahead * DOORSTATION_PICTURE_READ_TIMEOUT_SEC <= DOORSTATION_GALLERY_PLACE_WAIT_SEC
