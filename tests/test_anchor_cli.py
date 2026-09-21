"""End-to-end tests for the anchor CLI: the operator-facing entry point.

The module tests prove the guarantees; these prove that a real operator running
the documented command on a real database file gets the documented exit code.
A silent success from a verification tool is worse than no tool.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from audit import AuditLog
from primitives import FrozenClock, IdFactory
from schema_loader import connect_sqlite

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "publish_anchor.py"


def _run(db: Path, anchor_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["VILLA_DB"] = str(db)
    env["VILLA_ANCHOR_DIR"] = str(anchor_dir)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120,
    )


@pytest.fixture()
def live_db(tmp_path: Path) -> Path:
    """A real database file with a real (small) audit chain."""

    db = tmp_path / "villa.db"
    conn = connect_sqlite(str(db))
    try:
        audit = AuditLog(conn, clock=FrozenClock.at("2026-09-21T08:00:00Z"), ids=IdFactory(True, "cli"))
        for index in range(3):
            audit.append(
                action="case.opened",
                entity_type="case",
                entity_id=f"case_{index:04d}",
                actor="agent_ana",
                actor_role="reservation",
                after={"index": index},
            )
    finally:
        conn.close()
    return db


def test_publish_then_verify_succeeds(tmp_path, live_db):
    anchor_dir = tmp_path / "anchors"
    anchor_dir.mkdir()
    published = _run(live_db, anchor_dir, "publish")
    assert published.returncode == 0, published.stderr
    assert "anchor published" in published.stdout
    files = sorted(anchor_dir.glob("anchor-*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body["count"] == 3

    verified = _run(live_db, anchor_dir, "verify")
    assert verified.returncode == 0, verified.stderr
    assert "issues=0" in verified.stdout
    assert "count=3" in verified.stdout

    status = _run(live_db, anchor_dir, "status")
    assert status.returncode == 0, status.stderr
    assert "latest anchor" in status.stdout


def test_verify_fails_loudly_when_the_anchor_file_was_edited(tmp_path, live_db):
    anchor_dir = tmp_path / "anchors"
    anchor_dir.mkdir()
    assert _run(live_db, anchor_dir, "publish").returncode == 0
    path = sorted(anchor_dir.glob("anchor-*.json"))[0]
    body = json.loads(path.read_text(encoding="utf-8"))
    body["count"] = 999
    path.write_text(json.dumps(body, sort_keys=True) + "\n", encoding="utf-8")

    result = _run(live_db, anchor_dir, "verify")
    assert result.returncode == 1
    assert "REFUSED" in result.stderr
    assert "999" in result.stderr or "ANCHOR" in result.stderr


def test_publish_refuses_a_missing_database(tmp_path):
    anchor_dir = tmp_path / "anchors"
    anchor_dir.mkdir()
    result = _run(tmp_path / "tidak-ada.db", anchor_dir, "publish")
    assert result.returncode == 2
    assert "database not found" in result.stderr


def test_publish_refuses_a_missing_anchor_directory(tmp_path, live_db):
    result = _run(live_db, tmp_path / "belum-dibuat", "publish")
    assert result.returncode == 2
    assert "anchor directory not found" in result.stderr


def test_status_without_any_anchor_is_not_an_error(tmp_path, live_db):
    anchor_dir = tmp_path / "anchors"
    anchor_dir.mkdir()
    result = _run(live_db, anchor_dir, "status")
    assert result.returncode == 0
    assert "no published anchor yet" in result.stdout


def test_anchor_command_accepts_a_label(tmp_path, live_db):
    anchor_dir = tmp_path / "anchors"
    anchor_dir.mkdir()
    env = dict(os.environ)
    env.update({
        "VILLA_DB": str(live_db),
        "VILLA_ANCHOR_DIR": str(anchor_dir),
        "VILLA_ANCHOR_LABEL": "minggu-38",
        "PYTHONPATH": str(REPO / "src"),
    })
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "publish"],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    body = json.loads(sorted(anchor_dir.glob("anchor-*.json"))[0].read_text(encoding="utf-8"))
    assert body["label"] == "minggu-38"
