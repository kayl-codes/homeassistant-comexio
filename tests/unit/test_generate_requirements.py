"""scripts/generate_requirements.py: requirements.txt and the exact pins elsewhere follow manifest.json."""

import json
from pathlib import Path
import re
import tomllib
from typing import Any

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


SETUP_PYTHON_ACTION = "actions/setup-python@"
CHECKOUT_ACTION = "actions/checkout@"
WITH_KEY = "with"
CONTINUE_ON_ERROR_KEY = "continue-on-error"
NAME_KEY = "name"
PERMISSIONS_KEY = "permissions"
STEPS_KEY = "steps"
USES_KEY = "uses"
RUN_KEY = "run"


def _workflow() -> str:
    return (gr.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _mypy_job() -> dict[str, Any]:
    """The mypy job, parsed, so a step's own keys (if, continue-on-error, env) are visible to the guards."""
    # Imported here: a module-level `import yaml` here makes the subprocess tests of
    # test_function_plan_render fail with WinError 6 (measured on Windows; cause not found).
    import yaml  # noqa: PLC0415

    workflow = yaml.safe_load(_workflow())
    # Workflow level too: `defaults.run.shell` or `env` there would change the mypy script just the same. YAML 1.1
    # reads the key `on` as True.
    assert set(workflow) <= {NAME_KEY, True, PERMISSIONS_KEY, "jobs"}
    job = workflow["jobs"]["mypy"]
    # Strict allowlist, so a job-level `if`, `continue-on-error`, `defaults` (a run shell), `env`, `strategy` or
    # `container` cannot skip, excuse or change all steps at once. A harmless new key (`needs`, `concurrency`)
    # fails it too: add it here on purpose.
    assert set(job) <= {NAME_KEY, "runs-on", PERMISSIONS_KEY, "timeout-minutes", STEPS_KEY}
    # The mypy script is bash (`set -o pipefail`); on a Windows runner the default shell is PowerShell.
    assert job["runs-on"] == "ubuntu-latest"
    return job


def _mypy_steps() -> list[dict[str, Any]]:
    return _mypy_job()[STEPS_KEY]


def _always_runs(step: dict[str, Any]) -> bool:
    """A step that always runs as written: no condition, no continue-on-error, no env/shell/directory change."""
    return set(step) <= {NAME_KEY, USES_KEY, WITH_KEY, RUN_KEY}


def test_mypy_target_matches_the_ci_interpreter() -> None:
    """[tool.mypy] python_version is the interpreter the mypy job runs on, so a local run checks like CI."""
    # Every setup-python step of the job, so a second one cannot hide a drift, nor a skipped one pose as the real one.
    setups = [step for step in _mypy_steps() if str(step.get(USES_KEY, "")).startswith(SETUP_PYTHON_ACTION)]
    pyproject = tomllib.loads((gr.ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert len(setups) == 1
    assert _always_runs(setups[0])
    version = setups[0][WITH_KEY]["python-version"]
    assert isinstance(version, str), "quote python-version: YAML reads an unquoted 3.10 as the number 3.1"
    assert version == str(pyproject["tool"]["mypy"]["python_version"])


def test_ci_workflow_installs_from_requirements_txt() -> None:
    """CI installs the manifest packages only from a requirements file, never by name.

    The mypy job installs tests/requirements.txt, whose manifest pins the script checks. The raw
    text is scanned for names, comments included: stripping them would need quote-aware YAML and
    shell parsing, and a comment naming a manifest package is cheap to reword. A word that starts
    with "<name>-" (a wheel or sdist file name) counts as naming the package.
    """
    workflow = _workflow()
    manifest = json.loads(gr.MANIFEST.read_text(encoding="utf-8"))
    names = {gr._normalize(re.match(r"[A-Za-z0-9][\w.-]*", r)[0]) for r in manifest["requirements"]}
    words = {gr._normalize(word) for word in re.findall(r"[A-Za-z0-9][\w.-]*", workflow)}
    named = {name for name in names for word in words if word == name or word.startswith(f"{name}-")}

    steps = _mypy_steps()
    # Exactly one checkout, setup and install step (a second one fails on purpose), in this order: checkout (the
    # install reads tests/requirements.txt), interpreter setup, the install step (always runs, not commented out or
    # `if: false`), then the step that runs mypy. No step may run before its input exists.
    checkouts = [i for i, step in enumerate(steps) if str(step.get(USES_KEY, "")).startswith(CHECKOUT_ACTION)]
    setups = [i for i, step in enumerate(steps) if str(step.get(USES_KEY, "")).startswith(SETUP_PYTHON_ACTION)]
    installs = [
        i
        for i, step in enumerate(steps)
        if re.fullmatch(r"pip install -r tests/requirements\.txt mypy==[\d.]+", str(step.get(RUN_KEY, "")).strip())
    ]
    # Any step whose script mentions the mypy command, so a wrapped one (`if false; then mypy ...; fi`) is found too.
    mypy_runs = [i for i, step in enumerate(steps) if "mypy custom_components/comexio" in str(step.get(RUN_KEY, ""))]
    assert len(checkouts) == 1
    assert len(setups) == 1
    assert len(installs) == 1
    assert len(mypy_runs) == 1
    assert _always_runs(steps[checkouts[0]])
    # No `with` on the checkout: a `repository` or `ref` override would check other code than this PR's.
    assert WITH_KEY not in steps[checkouts[0]]
    assert _always_runs(steps[installs[0]])
    # The step is pinned to exactly the known two script lines and to keys that cannot skip or change it: a shell
    # conditional, `|| true`, a second command, `if`, `shell`, `env` or `working-directory` cannot hide a skipped
    # type check. continue-on-error must stay true: the job is non-blocking by design, and making it blocking is a
    # decision this test should force into the open. Strict on purpose: a comment line inside the script fails it
    # too, so move such a comment above the step.
    mypy_step = steps[mypy_runs[0]]
    assert set(mypy_step) <= {NAME_KEY, "id", RUN_KEY, CONTINUE_ON_ERROR_KEY}
    assert mypy_step.get(CONTINUE_ON_ERROR_KEY) is True
    script = [line.strip() for line in mypy_step[RUN_KEY].strip().splitlines()]
    assert script == ["set -o pipefail", "mypy custom_components/comexio | tee mypy-output.txt"]
    # Checkout, setup, install and mypy form one unbroken chain: a step between them could rewrite the checked-out
    # source, or uninstall or shadow the HA stack.
    assert setups[0] == checkouts[0] + 1
    assert installs[0] == setups[0] + 1
    assert mypy_runs[0] == installs[0] + 1
    assert "aiocomexio" in names
    assert not named
