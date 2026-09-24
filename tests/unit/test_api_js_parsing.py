"""Scraping of Comexio's inline `var $Name = {...}` JS object literals out of admin pages."""

import pytest

from custom_components.comexio.api import (
    _COMEXIO_VERSION_RE,
    ComexioAPI,
    _extract_js_object_literal,
    _normalize_js_like_object,
)
from tests.common import load_fixture


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1,}', '{"a": 1}'),
        ('{"a": [1, 2, ], }', '{"a": [1, 2 ] }'),
        ('{"a": 1,\n  }', '{"a": 1\n  }'),
        ('{"a": 1}', '{"a": 1}'),
    ],
)
def test_normalize_js_like_object_strips_trailing_commas(raw: str, expected: str) -> None:
    assert _normalize_js_like_object(raw) == expected


def test_extract_js_object_literal_returns_balanced_object_and_end_index() -> None:
    text = 'x = {"a": {"b": 1}} ; tail'
    start = text.index("{")

    literal, end = _extract_js_object_literal(text, start)

    assert literal == '{"a": {"b": 1}}'
    assert text[end:] == " ; tail"


@pytest.mark.parametrize(
    "literal",
    [
        '{"name": "brace } inside"}',
        '{"name": "open { inside"}',
        "{'name': 'single } quoted'}",
        '{"name": "escaped \\" quote }"}',
    ],
)
def test_extract_js_object_literal_ignores_braces_inside_strings(literal: str) -> None:
    assert _extract_js_object_literal(literal + " trailing", 0) == (literal, len(literal))


@pytest.mark.parametrize(
    ("text", "start"),
    [
        ('{"a": 1', 0),  # unterminated
        ('x = {"a": 1}', 0),  # start does not point at "{"
        ("{}", 5),  # start out of range
    ],
)
def test_extract_js_object_literal_returns_none_when_not_extractable(text: str, start: int) -> None:
    assert _extract_js_object_literal(text, start) == (None, start)


def test_scrape_js_vars_parses_every_valid_object_literal() -> None:
    result = ComexioAPI._scrape_js_vars(load_fixture("function_module_page.html"), page_label="test")

    assert result == {
        "Paper": {"2": {"Id": 2, "Name": "A4", "MMX": 297, "MMY": 210}},
        "Fubs": {"1": {"Id": 1, "Name": 'Plan {mit} "Klammern"', "Paper": 2}},
        "FubModules": {"2": {"1": {"Id": 1, "Name": "Licht, Wohnen", "Type": 1}}, "10": []},
    }


def test_scrape_js_vars_skips_invalid_json_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    result = ComexioAPI._scrape_js_vars(load_fixture("function_module_page.html"), page_label="test")

    assert "Broken" not in result
    assert "Failed to decode JSON for variable $Broken on test page" in caplog.text


def test_scrape_js_vars_ignores_declarations_outside_script_blocks() -> None:
    html = '<p>var $Outside = {"a": 1};</p><script>var $Inside = {"b": 2};</script>'

    assert ComexioAPI._scrape_js_vars(html, page_label="test") == {"Inside": {"b": 2}}


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<script src="/11.0.2/js/cmb_admin.js"></script>', "11.0.2"),
        (
            '<script src="/10.4.17/module/admin/function_function_module/js/cmb_function_function_module.js">',
            "10.4.17",
        ),
        ('<script src="/js/cmb_admin.js"></script>', None),
        ('<script src="/11.0.2/js/other.js"></script>', None),
    ],
)
def test_comexio_version_regex(html: str, expected: str | None) -> None:
    match = _COMEXIO_VERSION_RE.search(html)

    assert (match.group(1) if match else None) == expected
