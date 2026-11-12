"""Tests for ``scripts/secret_scan.sh`` — the gate is only worth what its negative control proves.

The gate found nothing when a dashboard token sat in a tracked unit file, so every pattern that
matters gets a planted secret that must trip it, plus the placeholder forms that must not.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "secret_scan.sh"


def run_scan(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_scan_fails_on_a_token_passed_as_a_command_line_flag(tmp_path):
    """The exact leak the audit found: a literal token after ``--token`` in a tracked unit."""
    unit = tmp_path / "villa-dashboard.service"
    unit.write_text(
        "ExecStart=/usr/bin/python3 src/dashboard.py --db x.db --role owner"
        " --token 0123456789abcdef0123456789abcdef\n",
        encoding="utf-8",
    )
    result = run_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "villa-dashboard.service" in result.stdout


def test_scan_passes_when_the_token_is_a_placeholder_or_a_file(tmp_path):
    """The two shapes this repo ships must stay clean, or the gate blocks every deploy."""
    unit = tmp_path / "villa-dashboard.service"
    unit.write_text(
        "ExecStart=/usr/bin/python3 src/dashboard.py --token ${VILLA_DASHBOARD_TOKEN}\n"
        "ExecStart=/usr/bin/python3 src/dashboard.py --token-file /etc/villa/dashboard.token\n",
        encoding="utf-8",
    )
    result = run_scan(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_scan_fails_on_a_pasted_bearer_token(tmp_path):
    note = tmp_path / "notes.md"
    note.write_text("curl -H 'Authorization: Bearer abcdef0123456789abcdef0123456789' /\n")
    result = run_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr


def test_the_repository_itself_is_clean(tmp_path):
    """Run the gate over a copy of the tracked tree: the shipping state must not trip it."""
    target = tmp_path / "repo"
    target.mkdir()
    for name in ("src", "deploy", "docs", "scripts", "templates", "tests"):
        source = REPO_ROOT / name
        if source.is_dir():
            shutil.copytree(source, target / name)
    result = run_scan(target)
    assert result.returncode == 0, result.stdout + result.stderr
