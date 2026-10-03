#!/usr/bin/env python3
"""Regenerate requirements.txt from custom_components/comexio/manifest.json.

manifest.json's "requirements" array is the single source of truth for this
integration's Python dependencies (it's what HA installs at runtime).
requirements.txt exists only so OSV-Scanner has a lockfile-shaped format it
can parse; it must never be hand-edited independently of manifest.json.

The test requirements and the CI workflow install the same packages on their own
lines; their exact pins (name==version) are checked against the manifest too, so
tests and mypy never run against another library version than HA installs.
"""

import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "comexio" / "manifest.json"
REQUIREMENTS_TXT = "requirements.txt"
REQUIREMENTS = ROOT / REQUIREMENTS_TXT
PINNED_ELSEWHERE = [
    ROOT / "tests" / REQUIREMENTS_TXT,
    ROOT / "tests" / "ha" / REQUIREMENTS_TXT,
    ROOT / ".github" / "workflows" / "ci.yml",
]


# name[extras] == version, as pip reads it (spaces allowed around ==). A wildcard (==0.3.0.*) stays
# part of the version, so it reads as a drift instead of the exact pin it starts with.
_PIN = re.compile(r"(?<![\w.-])([A-Za-z0-9][\w.-]*)(?:\[[^\]]*\])?\s*==\s*([\w.+!*-]*[\w*])")
# A comment as pip and YAML read it: "#" at line start or after whitespace, to the end of the line.
_COMMENT = re.compile(r"(?:^|(?<=\s))#.*$", re.MULTILINE)
# In a workflow only a pip install command in a step's run script pins; a name==version elsewhere (a step
# name, an env value, an echo) does not. The YAML parser resolves quoting and folded blocks, shlex the shell
# quoting: a separator inside quotes ("echo 'x; pip install a==1'", "--index-url 'u?a=1&b=2'") splits nothing.
_PIP_INSTALLS = [
    ["pip", "install"],
    ["pip3", "install"],
    ["python", "-m", "pip", "install"],
    ["python3", "-m", "pip", "install"],
]
# Shell control operators (;, &&, |, subshell parentheses, redirects) and line ends end a command.
_SHELL_OPERATORS = "();<>|&\n"
# A shell line continuation: the next line belongs to the same command. A comment line continues nothing.
_CONTINUATION = re.compile(r"\\\r?\n")
_COMMENT_LINE = re.compile(r"^[ \t]*#.*$", re.MULTILINE)
# The shell starts a comment only at a word start; shlex at any "#" (${REF#refs/}, a URL fragment).
_MID_WORD_HASH = re.compile(r"(?<=[^\s();<>|&])#")
_HASH_PLACEHOLDER = "\0"


def _normalize(name: str) -> str:
    """PEP 503 name normalisation (Foo_Bar == foo-bar)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins_by_name(text: str) -> dict[str, list[str]]:
    """Every name==version in text, versions grouped by normalised name."""
    found: dict[str, list[str]] = {}
    for name, version in _PIN.findall(text):
        found.setdefault(_normalize(name), []).append(version)
    return found


class _ParseError(Exception):
    """A workflow that YAML or shlex cannot read."""


def _as_dict(value: object) -> dict[Any, Any]:
    return value if isinstance(value, dict) else {}


def _run_scripts(workflow: object) -> list[tuple[str, str]]:
    """(step, script) for the run: script of every step in a parsed workflow (jobs.<job>.steps[].run)."""
    scripts = []
    for job_id, job in _as_dict(_as_dict(workflow).get("jobs")).items():
        for index, item in enumerate(_as_dict(job).get("steps") or []):
            step = _as_dict(item)
            if isinstance(step.get("run"), str):
                scripts.append((f"jobs.{job_id}.steps[{index}] ({step.get('name', 'unnamed')})", step["run"]))
    return scripts


def _shell_commands(step: str, script: str) -> list[list[str]]:
    """The simple commands of a shell script as word lists, quoting resolved and comments dropped."""
    # shlex's comment skip consumes the line end after a comment, so every line end is doubled to keep one.
    script = _CONTINUATION.sub(" ", _COMMENT_LINE.sub("", script)).replace("\n", "\n\n")
    script = _MID_WORD_HASH.sub(_HASH_PLACEHOLDER, script)
    lexer = shlex.shlex(script, posix=True, punctuation_chars=_SHELL_OPERATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    try:
        for token in lexer:
            if token.strip(_SHELL_OPERATORS):
                commands[-1].append(token.replace(_HASH_PLACEHOLDER, "#"))
            elif commands[-1]:
                commands.append([])
    except ValueError as err:  # an unclosed quote
        raise _ParseError(f"{step}: {err}") from err
    return commands


def _scalars(node: object) -> list[str]:
    """Every string value in a parsed YAML document."""
    if isinstance(node, dict):
        return [text for value in node.values() for text in _scalars(value)]
    if isinstance(node, list):
        return [text for item in node for text in _scalars(item)]
    return [node] if isinstance(node, str) else []


def _workflow_pins(workflow: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(every pin in the workflow, the pins a pip install command in a step's run: script installs).

    YAML and shell comments are dropped as the parsers read them, so a quoted " #" hides nothing.
    """
    try:
        parsed = yaml.safe_load(workflow)
    except yaml.YAMLError as err:
        raise _ParseError(" ".join(str(err).split())) from err
    scripts = _run_scripts(parsed)
    commands = [command for step, script in scripts for command in _shell_commands(step, script)]
    installs = [command for command in commands if any(command[: len(p)] == p for p in _PIP_INSTALLS)]
    run_texts = {script for _, script in scripts}
    texts = [text for text in _scalars(parsed) if text not in run_texts] + [" ".join(c) for c in commands]
    return _pins_by_name("\n".join(texts)), _pins_by_name("\n".join(" ".join(c) for c in installs))


def _file_mismatches(path: Path, pins: dict[str, tuple[str, str]]) -> list[str]:
    """The manifest pins that path lacks or pins at another version."""
    relative = path.relative_to(ROOT).as_posix()
    if not path.exists():
        return [f"{relative}: file missing"]
    raw = path.read_text(encoding="utf-8")
    # Comments dropped: a commented-out "# pkg==1.0" is no pin. Asymmetric on purpose: any other version
    # anywhere in the file is a drift (an install spelling the command filter misses must not slip through),
    # but in a workflow only a recognised pip install counts as the pin. In a workflow the drift scan also
    # reads the parsed YAML values and shell words, since the comment regex takes a quoted " #" for a comment.
    # Only a step's run: script is read as shell: a "#" line in any other block value (an action input, a
    # multi-line env value) is plain text to YAML, so a version in it is a drift.
    written = _pins_by_name(_COMMENT.sub("", raw))
    installed = written
    try:
        if path.suffix in {".yml", ".yaml"}:
            parsed, installed = _workflow_pins(raw)
            written = {key: written.get(key, []) + parsed.get(key, []) for key in written.keys() | parsed.keys()}
    except _ParseError as err:
        return [f"{relative}: cannot be parsed ({err})"]
    mismatches = []
    for key, (name, version) in pins.items():
        versions = dict.fromkeys(written.get(key, []))
        drifted = [other for other in versions if other != version]
        if not drifted and version not in installed.get(key, []):
            mismatches.append(f"{relative}: no {name}=={version} pin")
        mismatches.extend(f"{relative}: {name}=={other} (manifest.json: {version})" for other in drifted)
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
