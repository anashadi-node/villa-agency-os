"""End-to-end tests for the allowance CLI: the command that opens the machine path.

``test_wave4b3_ai_allowance_decision.py`` proves the *semantics* of the published value.
These prove that the documented command, run by a real operator against a real database
file, produces the documented effect and the documented exit code — including refusing to
write a permission that names no approver, and closing again in one revision when the
owner withdraws it.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest

from schema_loader import connect_sqlite

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "publish_ai_allowance.py"
ALLOWANCE = "security.ai.medium_tool_allowance"

#: A window that is open *now*, so the assertions do not age.
TODAY = datetime.date.today()
OPEN_FROM = TODAY.isoformat()
OPEN_TO = (TODAY + datetime.timedelta(days=30)).isoformat()


def _run(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["VILLA_DB"] = str(db)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120,
    )


def _revisions(db: Path) -> int:
    conn = connect_sqlite(str(db))
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM policy_revisions WHERE policy_key = ?",
            (ALLOWANCE,),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "villa.db"
    connect_sqlite(str(path)).close()
    return path


def test_status_on_a_fresh_database_reports_closed(db: Path):
    result = _run(db, "status")
    assert result.returncode == 0, result.stderr
    assert "CLOSED" in result.stdout
    assert "never published" in result.stdout


def test_publish_without_an_approver_is_refused_and_writes_nothing(db: Path):
    result = _run(db, "publish", "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO)
    assert result.returncode == 2
    assert "--approved-by" in result.stderr
    assert _revisions(db) == 0


def test_publish_then_status_shows_the_effective_value(db: Path):
    published = _run(
        db, "publish", "--approved-by", "owner_anas",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
    )
    assert published.returncode == 0, published.stderr
    assert 'version 1 = ["availability.request"]' in published.stdout

    status = _run(db, "status")
    assert status.returncode == 0, status.stderr
    assert 'effective now : ["availability.request"]' in status.stdout
    assert f"{OPEN_FROM} → {OPEN_TO}" in status.stdout


def test_a_different_tool_list_needs_no_code_change(db: Path):
    result = _run(
        db, "publish", "--approved-by", "owner_anas",
        "--tools", "availability.request,quotation.send",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
    )
    assert result.returncode == 0, result.stderr
    assert '["availability.request","quotation.send"]' in result.stdout


def test_withdraw_closes_it_again_in_one_revision(db: Path):
    assert _run(
        db, "publish", "--approved-by", "owner_anas",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
    ).returncode == 0
    assert _run(db, "withdraw", "--approved-by", "owner_anas").returncode == 0

    status = _run(db, "status")
    assert status.returncode == 0, status.stderr
    assert "published and empty: a machine actor is refused" in status.stdout
    assert "(version 2)" in status.stdout
    assert "withdrawn by the owner" in status.stdout
    assert _revisions(db) == 2


def test_a_missing_database_is_a_configuration_error(tmp_path: Path):
    result = _run(tmp_path / "nope.db", "status")
    assert result.returncode == 2
    assert "database not found" in result.stderr
