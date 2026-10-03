"""scripts/generate_requirements.py: requirements.txt and the exact pins elsewhere follow manifest.json."""

import json
from pathlib import Path

import pytest

from scripts import generate_requirements as gr

MANIFEST_REQUIREMENTS = ["aiocomexio==0.3.0", "idna>=3.15"]


STEPS = "jobs:\n  test:\n    steps:\n"


def _run(script: str) -> str:
    """A workflow whose one step runs script (a literal block, so the shell text is taken as is)."""
    body = "".join(f"          {line}\n" for line in script.splitlines())
    return f"{STEPS}      - run: |\n{body}"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal repo layout in tmp_path, everything in sync with the manifest."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"requirements": MANIFEST_REQUIREMENTS}), encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("".join(f"{r}\n" for r in MANIFEST_REQUIREMENTS), encoding="utf-8")
    pinned = [tmp_path / "tests-requirements.txt", tmp_path / "ci.yml"]
    pinned[0].write_text("homeassistant==2026.8.3\naiocomexio==0.3.0\n", encoding="utf-8")
    pinned[1].write_text(_run("pip install --only-binary :all: --no-deps aiocomexio==0.3.0"), encoding="utf-8")
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
    "ci_text",
    [
        _run("pip install --no-deps aiocomexio==0.3.0"),
        _run("pip install --no-deps AioComexio == 0.3.0"),  # PEP 503 name, spaces around ==
        _run("pip install --no-deps aiocomexio[speedups]==0.3.0"),
        _run("pip install --no-deps aiocomexio==0.3.0  # was aiocomexio==0.1.0"),  # a comment is no (drifted) pin
        _run("# was aiocomexio==0.1.0\npip install --no-deps aiocomexio==0.3.0"),  # comment line before the pin
        _run("pip install --no-deps aiocomexio==0.3.0 fooaiocomexio==9.9"),  # prefixed name is another package
        _run("python -m pip install --no-deps aiocomexio==0.3.0"),
        _run("pip install -U pip && pip install --no-deps aiocomexio==0.3.0"),  # second command on the line
        _run("cd tests && (pip install --no-deps aiocomexio==0.3.0)"),  # subshell
        _run("pip3 install --no-deps aiocomexio==0.3.0"),
        _run("pip install --no-deps \\\n  aiocomexio==0.3.0"),  # shell line continuation
        _run("pip \\\n  install aiocomexio==0.3.0"),  # continuation inside the command prefix
        # a separator inside quotes splits nothing
        _run("pip install --index-url 'https://example.org/?a=1&b=2' aiocomexio==0.3.0"),
        _run("pip install aiocomexio==0.3.0 2>&1 | tee log"),  # redirect and pipe after the pin
        STEPS + "      - run: 'pip install --only-binary :all: aiocomexio==0.3.0'\n",  # quoted YAML run value
        STEPS + "      - run: >-\n          pip install --no-deps\n          aiocomexio==0.3.0\n",  # folded YAML block
        # a run: that is no step's script (a defaults mapping) is skipped
        "defaults:\n  run:\n    shell: bash\n" + _run("pip install aiocomexio==0.3.0"),
        _run("# note \\\npip install aiocomexio==0.3.0"),  # a comment line ending in a backslash continues nothing
        # non-string values (on: true, null, numbers, booleans) are no text to scan
        "on:\n  workflow_dispatch:\n" + STEPS + "      - run: pip install aiocomexio==0.3.0\n"
        "        timeout-minutes: 5\n        continue-on-error: true\n",
    ],
)
def test_matching_pin_spellings_pass(repo: Path, ci_text: str) -> None:
    (repo / "ci.yml").write_text(ci_text, encoding="utf-8")

    assert gr.main(["--check"]) == 0


@pytest.mark.parametrize(
    ("ci_text", "message"),
    [
        (_run("pip install --no-deps aiocomexio==0.1.0"), "ci.yml: aiocomexio==0.1.0 (manifest.json: 0.3.0)"),
        (_run("pip install --no-deps aiocomexio==0.3.0.*"), "ci.yml: aiocomexio==0.3.0.* (manifest.json: 0.3.0)"),
        (_run("pip install --no-deps aiocomexio_x==0.3.0"), "ci.yml: no aiocomexio==0.3.0 pin"),
        (_run("pip install --no-deps 'aiocomexio>=0.2'"), "ci.yml: no aiocomexio==0.3.0 pin"),
        (_run("# pip install --no-deps aiocomexio==0.3.0"), "ci.yml: no aiocomexio==0.3.0 pin"),
        (_run("pip install --no-deps other  # aiocomexio==0.3.0"), "ci.yml: no aiocomexio==0.3.0 pin"),
        (
            STEPS
            + "      # - run: pip install --no-deps aiocomexio==0.3.0\n      - run: pip install --no-deps other\n",
            "ci.yml: no aiocomexio==0.3.0 pin",
        ),
        # no pip install, or "pip install" not in command position: no install of the pin
        (_run('echo "aiocomexio==0.3.0"'), "ci.yml: no aiocomexio==0.3.0 pin"),
        ("env:\n  PIN: aiocomexio==0.3.0\n", "ci.yml: no aiocomexio==0.3.0 pin"),
        (_run('echo "pip install aiocomexio==0.3.0"'), "ci.yml: no aiocomexio==0.3.0 pin"),
        (_run('echo "x; pip install aiocomexio==0.3.0"'), "ci.yml: no aiocomexio==0.3.0 pin"),  # quoted separator
        (
            STEPS + "      - name: pip install aiocomexio==0.3.0\n        run: pip install other\n",
            "ci.yml: no aiocomexio==0.3.0 pin",
        ),
        (  # a run: key outside a step (here an action input) is no script
            STEPS + "      - uses: some/action@v1\n        with:\n          run: pip install aiocomexio==0.3.0\n",
            "ci.yml: no aiocomexio==0.3.0 pin",
        ),
        # the pin in the next command
        (_run('pip install other && echo "aiocomexio==0.3.0"'), "ci.yml: no aiocomexio==0.3.0 pin"),
        (_run('pip install other; echo "aiocomexio==0.3.0"'), "ci.yml: no aiocomexio==0.3.0 pin"),
        (_run("pip install other | tee aiocomexio==0.3.0"), "ci.yml: no aiocomexio==0.3.0 pin"),
        (_run("pip install other\necho aiocomexio==0.3.0"), "ci.yml: no aiocomexio==0.3.0 pin"),
        (_run("pip install other  # comment\necho aiocomexio==0.3.0"), "ci.yml: no aiocomexio==0.3.0 pin"),
        # A drift counts anywhere, even in an install spelling the command filter does not recognise
        (
            _run("pip install --no-deps aiocomexio==0.3.0\nuv pip install --system aiocomexio==0.2.0"),
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (  # a second, drifted install next to the right one
            _run("pip install --no-deps aiocomexio==0.3.0\npip3 install --no-deps aiocomexio==0.2.0"),
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (
            _run("pip install --no-deps aiocomexio==0.3.0\npip install --no-deps \\\n  aiocomexio==0.2.0"),
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (  # a quoted " #" reads as a comment to the raw-text scan, but the parsed install still drifts
            _run('pip install aiocomexio==0.3.0\necho "note #"; pip install aiocomexio==0.2.0'),
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (  # ... and in an install spelling the command filter does not recognise
            _run('pip install aiocomexio==0.3.0\necho "note #"; uv pip install --system aiocomexio==0.2.0'),
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (  # a quoted " #" in a YAML value is no comment either
            "env:\n  NOTE: 'see #1, aiocomexio==0.2.0'\n" + _run("pip install aiocomexio==0.3.0"),
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (  # shlex reads a mid-word "#" as a comment, the shell does not
            _run("pip install aiocomexio==0.3.0\necho x#y aiocomexio==0.2.0"),
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (  # ... not even next to a quoted " #" that the comment regex cuts at
            _run(
                'pip install aiocomexio==0.3.0\necho "build #1"; VER=${GITHUB_REF#refs/tags/}; '
                "uv pip install aiocomexio==0.2.0"
            ),
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (  # a comment line ending in a backslash continues nothing
            _run('pip install aiocomexio==0.3.0\n# note \\\necho "x #"; uv pip install aiocomexio==0.2.0'),
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (  # a quoted " #" in a value inside the steps list
            STEPS + "      - run: pip install aiocomexio==0.3.0\n      - uses: x/y@v1\n        with:\n"
            "          note: 'see #1, aiocomexio==0.2.0'\n",
            "ci.yml: aiocomexio==0.2.0 (manifest.json: 0.3.0)",
        ),
        (  # only a step's run: script is read as shell: a "#" line in an action input is plain text
            STEPS + "      - run: pip install aiocomexio==0.3.0\n      - uses: x/y@v1\n        with:\n"
            "          script: |\n            # was aiocomexio==0.1.0\n            true\n",
            "ci.yml: aiocomexio==0.1.0 (manifest.json: 0.3.0)",
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


@pytest.mark.parametrize(
    "ci_text",
    [
        "run: pip install --only-binary :all: aiocomexio==0.3.0\n",  # ": " in a plain YAML scalar
        _run("echo 'unclosed\npip install aiocomexio==0.3.0"),
    ],
)
def test_unparsable_workflow_fails(repo: Path, capsys: pytest.CaptureFixture[str], ci_text: str) -> None:
    (repo / "ci.yml").write_text(ci_text, encoding="utf-8")

    assert gr.main(["--check"]) == 1
    assert "ci.yml: cannot be parsed" in capsys.readouterr().err


def test_unclosed_quote_names_the_step(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "ci.yml").write_text(_run("echo 'unclosed"), encoding="utf-8")

    assert gr.main(["--check"]) == 1
    assert "ci.yml: cannot be parsed (jobs.test.steps[0] (unnamed): No closing quotation)" in capsys.readouterr().err


def test_drifted_requirements_file_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A requirements file is no workflow: any name==version in it is the pin."""
    (repo / "tests-requirements.txt").write_text("aiocomexio==0.2.0\n", encoding="utf-8")

    assert gr.main(["--check"]) == 1
    assert "tests-requirements.txt: aiocomexio==0.2.0 (manifest.json: 0.3.0)" in capsys.readouterr().err


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
