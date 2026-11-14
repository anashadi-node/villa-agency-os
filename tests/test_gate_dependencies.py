"""Dependency versions live in exactly one file, and CI reads that file.

Why this exists: the gate used to install its tools with an unpinned
``pip install --upgrade pytest pyflakes markdown`` while ``requirements-dev.txt`` pinned older
versions. Both were green, so nothing looked broken — but "green" meant two different toolchains,
and Dependabot opened a PR bumping a pin that CI never used (markdown 3.5.2 → 3.8.1 while CI was
already running 3.11.3). A gate whose toolchain is not recorded cannot be reproduced.

These tests fail when that drift returns, not when a version changes: bumping a pin in the manifest
is always allowed and needs no test edit.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = REPO / "requirements-dev.txt"
WORKFLOW = REPO / ".github" / "workflows" / "check.yml"

# `name`, `name[extra]`, optionally `; env_marker` — then `==version` (a pin) or `==version.*`
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)(\[[^\]]+\])?\s*==\s*(?P<version>[0-9][^\s;#]*)$")
_PACKAGE_WORDS = ("pytest", "pyflakes", "markdown")


def _manifest_entries() -> list[tuple[int, str]]:
    """(line number, text) for every non-comment, non-blank line in the manifest."""
    entries: list[tuple[int, str]] = []
    for number, raw in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), start=1):
        text = raw.split("#", 1)[0].strip()
        if text:
            entries.append((number, text))
    return entries


def test_the_manifest_exists_and_pins_every_entry():
    assert MANIFEST.is_file(), "requirements-dev.txt is the single source of tool versions"
    entries = _manifest_entries()
    assert entries, "requirements-dev.txt declares nothing"
    loose = [f"line {n}: {text}" for n, text in entries if not _PIN.match(text)]
    assert loose == [], f"every entry must be an exact `==` pin, not a range: {loose}"


def test_the_manifest_covers_every_tool_the_gate_uses():
    names = set()
    for _, text in _manifest_entries():
        match = _PIN.match(text)
        if match:
            names.add(match.group("name").lower())
    missing = [tool for tool in _PACKAGE_WORDS if tool not in names]
    assert missing == [], f"the gate uses these tools but the manifest does not pin them: {missing}"


def test_ci_installs_the_manifest_and_names_no_package_itself():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "requirements-dev.txt" in workflow, (
        "CI must install its tools from requirements-dev.txt; an inline package list makes the "
        "gate's toolchain unrecorded"
    )
    # Every `pip install ...` line must either upgrade pip itself or read the manifest.
    offenders = []
    for line in workflow.splitlines():
        stripped = line.strip()
        if "pip install" not in stripped or stripped.startswith("#"):
            continue
        if "-r requirements-dev.txt" in stripped or stripped.endswith("--upgrade pip"):
            continue
        offenders.append(stripped)
    assert offenders == [], f"CI installs packages outside the manifest: {offenders}"
