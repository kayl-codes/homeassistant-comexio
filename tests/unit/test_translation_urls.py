"""No translation string contains a URL; hassfest rejects one ("please use description placeholders instead").

hassfest only runs in CI, so a URL in a translation (e.g. an example address in a step description) would
otherwise first show up as a red CI check after the push. A URL belongs in a description placeholder that
the code fills (e.g. `{example_url}`, passed in description_placeholders).
"""

from collections.abc import Iterator
import json
from pathlib import Path
import re

import pytest

TRANSLATIONS = Path(__file__).parents[2] / "custom_components" / "comexio" / "translations"
# Every file, so a language added later is checked too.
TRANSLATION_FILES = sorted(path.name for path in TRANSLATIONS.glob("*.json"))

# Copied from Home Assistant's script/hassfest/translations.py (RE_URL, dev branch as of 2026-10-25). CI runs
# home-assistant/actions/hassfest@master, i.e. HA's current dev code rather than the minimum HA version, so this
# copy can fall behind; hassfest in CI stays authoritative. When it rejects a URL this test let through, copy the
# current pattern from the dev branch again. hassfest allows URLs only in the description of a preview feature,
# which this integration has none of.
RE_URL = re.compile(
    r"(((ftp|ftps|scp|http|https|mqtt|mqtts|socket|socks5):\/\/|www\.)"
    r"[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,5}(:[0-9]{1,5})?(\/.*)?)",
    re.IGNORECASE,
)


def _strings(node: object, path: str) -> Iterator[tuple[str, str]]:
    """Every string value of a translation tree, with its dotted key path."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def test_every_translation_file_is_checked() -> None:
    # An empty glob (a wrong path) would only skip the parametrized test below, not fail it.
    assert {"strings.json", "en.json", "de.json", "fr.json", "es.json"} <= set(TRANSLATION_FILES)


def test_the_walk_reaches_strings_inside_lists() -> None:
    tree = {"step": {"options": ["plain", {"hint": "https://ha.example.org"}]}}
    assert list(_strings(tree, "")) == [
        ("step.options[0]", "plain"),
        ("step.options[1].hint", "https://ha.example.org"),
    ]


@pytest.mark.parametrize("file_name", TRANSLATION_FILES)
def test_no_translation_string_contains_a_url(file_name: str) -> None:
    tree = json.loads((TRANSLATIONS / file_name).read_text(encoding="utf-8"))
    with_url = {path: text for path, text in _strings(tree, "") if RE_URL.search(text)}
    assert not with_url


@pytest.mark.parametrize(
    ("text", "is_url"),
    [
        ("Example: https://ha.example.org:8443", True),
        ("see www.comexio.com", True),
        ("mqtt://broker.local:1883/topic", True),
        ("See HTTPS://HA.EXAMPLE.ORG", True),
        # The host field's error names the scheme without an address, which hassfest accepts.
        ("leave out http://, paths and the like", False),
        ("e.g. {example_url}", False),
    ],
)
def test_the_url_pattern_tells_a_url_from_a_bare_scheme(text: str, is_url: bool) -> None:
    assert bool(RE_URL.search(text)) is is_url
