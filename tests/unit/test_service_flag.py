"""Boolean service fields are read as HA reads them, never by truthiness."""

from pathlib import Path
import re
from types import SimpleNamespace

from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.comexio.services._context import service_flag

SERVICES_DIR = Path(__file__).parents[2] / "custom_components" / "comexio" / "services"


@pytest.mark.parametrize(
    ("data", "expected"),
    [({}, False), ({"flag": True}, True), ({"flag": "false"}, False), ({"flag": "off"}, False), ({"flag": "on"}, True)],
)
def test_a_flag_reads_strings_as_ha_does(data: dict, expected: bool) -> None:
    """(Sourcery) bool("false") is True: include_recent "false" purged the recent orphan backups."""
    assert service_flag(SimpleNamespace(data=data), "flag", False) is expected


def test_a_flag_that_is_no_boolean_is_refused() -> None:
    call = SimpleNamespace(data={"flag": "maybe"})
    with pytest.raises(ServiceValidationError, match="'flag' must be true or false"):
        service_flag(call, "flag", False)


def test_no_service_reads_a_flag_by_truthiness() -> None:
    """The services register without a schema: a flag read with bool() takes the string "false" as True."""
    offenders = [
        f"{path.name}:{number}"
        for path in SERVICES_DIR.glob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"bool\(\s*call\.data", line)
    ]
    assert offenders == [], f"read these flags with service_flag(): {offenders}"
