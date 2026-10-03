#!/usr/bin/env python3
"""Regenerate requirements.txt from custom_components/comexio/manifest.json.

manifest.json's "requirements" array is the single source of truth for this
integration's Python dependencies (it's what HA installs at runtime).
requirements.txt exists only so OSV-Scanner has a lockfile-shaped format it
can parse; it must never be hand-edited independently of manifest.json.

The test requirements install the same packages on their own lines; their exact
pins (name==version) are checked against the manifest too, so tests never run
against another library version than HA installs. The CI workflow pins nothing
itself: it installs from requirements.txt.
"""

import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "comexio" / "manifest.json"
REQUIREMENTS_TXT = "requirements.txt"
REQUIREMENTS = ROOT / REQUIREMENTS_TXT
PINNED_ELSEWHERE = [
    ROOT / "tests" / REQUIREMENTS_TXT,
    ROOT / "tests" / "ha" / REQUIREMENTS_TXT,
]


# name[extras] == version, as pip reads it (spaces allowed around ==). A wildcard (==0.3.0.*) stays
# part of the version, so it reads as a drift instead of the exact pin it starts with.
_PIN = re.compile(r"(?<![\w.-])([A-Za-z0-9][\w.-]*)(?:\[[^\]]*\])?\s*==\s*([\w.+!*-]*[\w*])")
# A comment as pip and YAML read it: "#" at line start or after whitespace, to the end of the line.
_COMMENT = re.compile(r"(?:^|(?<=\s))#.*$", re.MULTILINE)


def _normalize(name: str) -> str:
    """PEP 503 name normalisation (Foo_Bar == foo-bar)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _file_mismatches(path: Path, pins: dict[str, tuple[str, str]]) -> list[str]:
    """The manifest pins that path lacks or pins at another version."""
    relative = path.relative_to(ROOT).as_posix()
    if not path.exists():
        return [f"{relative}: file missing"]
    found: dict[str, list[str]] = {}
    # Comments dropped: a commented-out "# pkg==1.0" is no pin.
    text = _COMMENT.sub("", path.read_text(encoding="utf-8"))
    for name, version in _PIN.findall(text):
        found.setdefault(_normalize(name), []).append(version)
    mismatches = []
    for key, (name, version) in pins.items():
        versions = found.get(key, [])
        if not versions:
            mismatches.append(f"{relative}: no {name}=={version} pin")
        mismatches.extend(
            f"{relative}: {name}=={other} (manifest.json: {version})" for other in versions if other != version
        )
    return mismatches


def _pin_mismatches(manifest_requirements: list[str]) -> list[str]:
    """Every exact (==) manifest pin that a file in PINNED_ELSEWHERE lacks or pins at another version."""
    pins = {}
    for requirement in manifest_requirements:
        if match := _PIN.fullmatch(requirement.split(";")[0].strip()):
            pins[_normalize(match[1])] = (match[1], match[2])
    return [mismatch for path in PINNED_ELSEWHERE for mismatch in _file_mismatches(path, pins)]


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    content = "".join(f"{requirement}\n" for requirement in manifest["requirements"])

    exit_code = 0
    for mismatch in _pin_mismatches(manifest["requirements"]):
        print(f"Pin out of sync with manifest.json: {mismatch}", file=sys.stderr)
        exit_code = 1

    if REQUIREMENTS.exists() and REQUIREMENTS.read_text(encoding="utf-8") == content:
        return exit_code

    if check_only:
        print(f"{REQUIREMENTS.relative_to(ROOT)} is out of sync with manifest.json", file=sys.stderr)
        return 1

    REQUIREMENTS.write_text(content, encoding="utf-8", newline="\n")
    print(f"Updated {REQUIREMENTS.relative_to(ROOT)} from manifest.json")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
