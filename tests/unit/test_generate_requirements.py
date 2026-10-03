"""scripts/generate_requirements.py: requirements.txt and the exact pins elsewhere follow manifest.json."""

import json
from pathlib import Path
import re

import pytest

from scripts import generate_requirements as gr

MANIFEST_REQUIREMENTS = ["aiocomexio==0.3.0", "idna>=3.15"]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal repo layout in tmp_path, everything in sync with the manifest."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"requirements": MANIFEST_REQUIREMENTS}), encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("".join(f"{r}\n" for r in MANIFEST_REQUIREMENTS), encoding="utf-8")
    pinned = [tmp_path / "tests-requirements.txt", tmp_path / "ci.yml"]
    pinned[0].write_text("homeassistant==2026.8.3\naiocomexio==0.3.0\n", encoding="utf-8")
    pinned[1].write_text("run: pip install --only-binary :all: --no-deps aiocomexio==0.3.0\n", encoding="utf-8")
    monkeypatch.setattr(gr, "ROOT", tmp_path)
    monkeypatch.setattr(gr, "MANIFEST", manifest)
    monkeypatch.setattr(gr, "REQUIREMENTS", requirements)
    monkeypatch.setattr(gr, "PINNED_ELSEWHERE", pinned)
    return tmp_path


def test_in_sync_passes(repo: Path) -> None:
    assert gr.main(["--check"]) == 0


def test_real_repository_is_in_sync() -> None:
    assert gr.main(["--check"]) == 0


@pytest.mark.parametrize(
    "ci_line",
    [
        "pip install --no-deps aiocomexio==0.3.0",
        "pip install --no-deps AioComexio == 0.3.0",  # PEP 503 name, spaces around ==
        "pip install --no-deps aiocomexio[speedups]==0.3.0",
        "pip install --no-deps aiocomexio==0.3.0  # was aiocomexio==0.1.0",  # a comment is no (drifted) pin
        "# was aiocomexio==0.1.0\npip install --no-deps aiocomexio==0.3.0",  # comment line before the pin
        "pip install --no-deps aiocomexio==0.3.0 fooaiocomexio==9.9",  # prefixed name is another package
    ],
)
def test_matching_pin_spellings_pass(repo: Path, ci_line: str) -> None:
    (repo / "ci.yml").write_text(ci_line + "\n", encoding="utf-8")

    assert gr.main(["--check"]) == 0


@pytest.mark.parametrize(
    ("ci_text", "message"),
    [
        ("pip install --no-deps aiocomexio==0.1.0\n", "ci.yml: aiocomexio==0.1.0 (manifest.json: 0.3.0)"),
        ("pip install --no-deps aiocomexio==0.3.0.*\n", "ci.yml: aiocomexio==0.3.0.* (manifest.json: 0.3.0)"),
        ("pip install --no-deps aiocomexio_x==0.3.0\n", "ci.yml: no aiocomexio==0.3.0 pin"),
        ("pip install --no-deps aiocomexio>=0.2\n", "ci.yml: no aiocomexio==0.3.0 pin"),
        ("# pip install --no-deps aiocomexio==0.3.0\n", "ci.yml: no aiocomexio==0.3.0 pin"),
        ("pip install --no-deps other  # aiocomexio==0.3.0\n", "ci.yml: no aiocomexio==0.3.0 pin"),
        (
            "# pip install --no-deps aiocomexio==0.3.0\npip install --no-deps other\n",
            "ci.yml: no aiocomexio==0.3.0 pin",
        ),
    ],
)
def test_drifted_or_missing_pin_fails(
    repo: Path, capsys: pytest.CaptureFixture[str], ci_text: str, message: str
) -> None:
    """requirements.txt itself is in sync, but a pin elsewhere is not: --check still fails."""
    (repo / "ci.yml").write_text(ci_text, encoding="utf-8")

    assert gr.main(["--check"]) == 1
    assert message in capsys.readouterr().err


def test_missing_pinned_file_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "ci.yml").unlink()

    assert gr.main(["--check"]) == 1
    assert "ci.yml: file missing" in capsys.readouterr().err


def test_out_of_sync_requirements_txt_is_rewritten(repo: Path) -> None:
    (repo / "requirements.txt").write_text("aiocomexio==0.2.0\n", encoding="utf-8")

    assert gr.main(["--check"]) == 1
    assert gr.main([]) == 1
    assert (repo / "requirements.txt").read_text(encoding="utf-8") == "aiocomexio==0.3.0\nidna>=3.15\n"
    assert gr.main(["--check"]) == 0


def test_ci_workflow_installs_from_requirements_txt() -> None:
    """CI installs the manifest packages only from requirements.txt, never by name."""
    workflow = gr._COMMENT.sub("", (gr.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    manifest = json.loads(gr.MANIFEST.read_text(encoding="utf-8"))
    names = {gr._normalize(re.match(r"[A-Za-z0-9][\w.-]*", r)[0]) for r in manifest["requirements"]}
    words = {gr._normalize(word) for word in re.findall(r"[A-Za-z0-9][\w.-]*", workflow)}

    assert "--no-deps -r requirements.txt" in workflow
    assert "aiocomexio" in names
    assert not names & words
