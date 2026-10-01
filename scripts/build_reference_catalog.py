"""Regenerate custom_components/comexio/reference/<kind>.json from a raw Comexio admin config.

The input is the JSON returned by ComexioAPI.get_raw_config() on a reference server (it holds
only the firmware catalog parts used here — no installation data ends up in the output).

Usage:
    python scripts/build_reference_catalog.py <raw_config.json> <comexio_version>
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components" / "comexio" / "reference_catalog.py"


def _load_module():
    # Loaded by path: importing the package would pull in Home Assistant via __init__.py.
    spec = importlib.util.spec_from_file_location("reference_catalog", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(raw_path: Path, comexio_version: str) -> None:
    reference_catalog = _load_module()
    raw_config = json.loads(raw_path.read_text(encoding="utf-8"))
    reference_catalog.REFERENCE_DIR.mkdir(exist_ok=True)
    for kind in reference_catalog.LIVE_EXTRACTORS:
        content = reference_catalog.build_reference(kind, raw_config, comexio_version)
        out = reference_catalog.REFERENCE_DIR / f"{kind}.json"
        out.write_text(json.dumps(content, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"{out.relative_to(ROOT)}: {len(content['entries'])} entries, {out.stat().st_size} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]), sys.argv[2])
