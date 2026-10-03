"""The go-live smoke: the whole guest path, on a real database, from nothing.

Why this file exists
--------------------
``scripts/go_live_smoke.py`` is the artefact a buyer runs to see the platform do the thing
they are buying. An artefact nobody executes rots — this repository has already paid for
that lesson twice (the demo seeder wrote the old hold shape for two waves with no failing
test, and the live-PostgreSQL class never ran at all). So the command gets a test: the
documented invocation, a real file, the documented exit code, and the *facts* of the
journey read back out of the database and out of the JSON report.

The assertions are deliberately about the end state (a closed booking, a §30 verdict that
is ready, a stay that moved through its states) rather than about how the script is
written, so refactoring the script is allowed and weakening the journey is not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from schema_loader import connect_sqlite

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "go_live_smoke.py"

#: The states a booking passes through from the hold to the end. ``CONFIRMED`` is written by
#: the confirmation path, the next three by the stay commands — the three commands that were
#: missing until this journey could be run end to end.
STAY_STATES = ("HOLD_ACTIVE", "CONFIRMED", "PRE_ARRIVAL", "IN_STAY", "COMPLETED")


def _run(db: Path, report: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--report", str(report)],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=300,
    )


@pytest.fixture()
def journey(tmp_path: Path) -> tuple[Path, dict]:
    db = tmp_path / "go-live.db"
    report_path = tmp_path / "report.json"
    result = _run(db, report_path)
    assert result.returncode == 0, result.stderr
    assert report_path.exists(), result.stdout
    return db, json.loads(report_path.read_text(encoding="utf-8"))


def _count(db: Path, table: str, where: str = "") -> int:
    conn = connect_sqlite(str(db))
    try:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}").fetchone()["n"])
    finally:
        conn.close()


def _scalar(db: Path, sql: str) -> object:
    conn = connect_sqlite(str(db))
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_the_whole_guest_path_runs_and_ends_closed(journey):
    db, report = journey
    facts = report["facts"]
    assert facts["booking_status"] == "CLOSED"
    assert facts["closure_ready"] is True
    assert facts["ledger_ok"] is True
    assert facts["audit_events"] > 0
    status = _scalar(db, "SELECT status FROM bookings LIMIT 1")
    assert status == "CLOSED", "the row the platform kept disagrees with the report"


def test_every_step_of_the_journey_passed(journey):
    _, report = journey
    steps = report["steps"]
    assert [step["step"] for step in steps] == list(range(1, len(steps) + 1))
    refused = [step for step in steps if "refused" in step]
    assert refused == []
    assert len(steps) == 16


def test_the_stay_moved_through_all_of_its_states(journey):
    _, report = journey
    events = report["facts"]["stay_events"]
    assert events == [f"{a}->{b}" for a, b in zip(STAY_STATES, STAY_STATES[1:])]


def test_every_closure_check_that_30_names_is_satisfied(journey):
    _, report = journey
    checks = report["facts"]["closure_checks"]
    assert checks, "the verdict must carry the list §30 asks for"
    failed = [check for check in checks if not check["ok"]]
    assert failed == []


def test_the_platform_refused_the_two_things_it_must_refuse(journey):
    """A journey that never gets a refusal has not tested the doors.

    The forged webhook is refused *before* anything is written — an unverified payload is not
    "data we marked untrusted", it is data the platform never saw — so the evidence is a
    count: two messages landed (the opening one and the signed reply), not three. The second
    hold on the same window is refused by the overlap rule, so exactly one hold exists.
    """
    db, _ = journey
    assert _count(db, "case_messages") == 2, "a forged webhook left a row behind"
    assert _count(db, "holds") == 1, "only the winning hold exists"
    assert _count(db, "bookings") == 1


def test_the_money_path_recorded_one_invoice_and_one_payment(journey):
    db, report = journey
    facts = report["facts"]
    assert _count(db, "invoices") == 1
    assert _count(db, "payments") == 1
    assert _count(db, "payment_events") == 1
    assert facts["total_minor"] > 0
    assert facts["currency"] == "IDR"


def test_a_second_run_over_a_used_database_is_refused(tmp_path):
    """The proof is 'from nothing': re-running over a journey would mix two stories."""
    db = tmp_path / "go-live.db"
    report = tmp_path / "report.json"
    first = _run(db, report)
    assert first.returncode == 0, first.stderr
    second = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--report", str(report)],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )
    assert second.returncode == 2, second.stdout + second.stderr
    assert "already holds a journey" in second.stderr
