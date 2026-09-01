#!/usr/bin/env python3
"""Regenerate requirements.txt from custom_components/comexio/manifest.json.

manifest.json's "requirements" array is the single source of truth for this
integration's Python dependencies (it's what HA installs at runtime).
requirements.txt exists only so OSV-Scanner has a lockfile-shaped format it
can parse; it must never be hand-edited independently of manifest.json.
"""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "comexio" / "manifest.json"
REQUIREMENTS = ROOT / "requirements.txt"


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    content = "".join(f"{requirement}\n" for requirement in manifest["requirements"])

    if REQUIREMENTS.exists() and REQUIREMENTS.read_text(encoding="utf-8") == content:
        return 0

    if check_only:
        print(f"{REQUIREMENTS.relative_to(ROOT)} is out of sync with manifest.json", file=sys.stderr)
        return 1

    REQUIREMENTS.write_text(content, encoding="utf-8", newline="\n")
    print(f"Updated {REQUIREMENTS.relative_to(ROOT)} from manifest.json")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
