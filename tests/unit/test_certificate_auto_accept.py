"""The conditions of the certificate rule of #294 that need no Home Assistant instance."""

from typing import Any

import pytest

from custom_components.comexio.certificate import pinned_authority
from custom_components.comexio.certificate_auto_accept import firmware_replaces_certificate, parse_firmware
from custom_components.comexio.const import CONF_CERTIFICATE_AUTHORITY, CONF_CERTIFICATE_FINGERPRINT

PINNED = "a" * 64
OTHER = "b" * 64
ISSUER = "CN=Comexio LAN CA"
AKI = "0a1b2c3d"


@pytest.mark.parametrize(
    ("version", "parsed"),
    [
        ("11.1.5", (11, 1, 5)),
        (" 11.0.2 ", (11, 0, 2)),
        ("12", (12,)),
        (None, None),
        ("", None),
        ("11.1.5-beta", None),
        ("11..5", None),
        ("v11.1.5", None),
        ("١١.1.5", None),  # Arabic-Indic digits: not Comexio's spelling, not taken for numbers
    ],
)
def test_the_firmware_version_is_read_as_numbers_or_not_at_all(version: str | None, parsed: Any) -> None:
    assert parse_firmware(version) == parsed


@pytest.mark.parametrize(
    ("version", "replaces"),
    [
        ("11.1.5", True),  # the last firmware known to replace its certificate on its own
        ("11.1.4", True),
        ("11.1", True),
        ("10.9.99", True),
        ("11.1.5.1", False),
        ("11.1.6", False),
        ("11.2.0", False),
        ("12.0.0", False),
        (None, False),  # unknown: the user decides
        ("beta", False),
    ],
)
def test_the_rule_applies_up_to_the_last_firmware_known_to_replace_its_certificate(
    version: str | None, replaces: bool
) -> None:
    assert firmware_replaces_certificate(version) is replaces


def _data(authority: Any) -> dict[str, Any]:
    return {CONF_CERTIFICATE_FINGERPRINT: PINNED, CONF_CERTIFICATE_AUTHORITY: authority}


def test_the_pins_issuer_counts_while_it_was_read_from_the_pin() -> None:
    info = pinned_authority(_data({"fingerprint": PINNED, "issuer": ISSUER, "authority_key_id": AKI}))

    assert info is not None
    assert (info.fingerprint, info.issuer, info.authority_key_id) == (PINNED, ISSUER, AKI)


@pytest.mark.parametrize(
    "authority",
    [
        None,
        {"fingerprint": OTHER, "issuer": ISSUER, "authority_key_id": AKI},  # learned for a pin replaced since
        {"fingerprint": PINNED, "issuer": None, "authority_key_id": AKI},
        {"fingerprint": PINNED, "issuer": ISSUER, "authority_key_id": None},
        {"fingerprint": PINNED, "issuer": "", "authority_key_id": AKI},  # damaged: no issuer to compare
        {"fingerprint": PINNED, "issuer": ISSUER, "authority_key_id": "XYZ"},  # damaged: not lowercase hex
        "CN=Comexio LAN CA",  # not the stored layout
    ],
    ids=["missing", "other pin", "no issuer", "no key id", "empty issuer", "bad key id", "not a mapping"],
)
def test_without_both_values_of_the_pin_itself_there_is_no_issuer_to_compare(authority: Any) -> None:
    assert pinned_authority(_data(authority)) is None


def test_an_entry_without_a_pin_has_no_issuer() -> None:
    authority = {"fingerprint": PINNED, "issuer": ISSUER, "authority_key_id": AKI}

    assert pinned_authority({CONF_CERTIFICATE_AUTHORITY: authority}) is None
