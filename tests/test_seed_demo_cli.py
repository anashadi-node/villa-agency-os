"""End-to-end tests for the demo seeder: the path a buyer demo depends on.

The seeder writes example data into a real database file, and until this file existed
**nothing ran it**. The WAVE 4-B hold lifecycle (ADR-0020) added a NOT NULL
``expires_timezone`` and replaced the hold vocabulary; the seeder kept writing the old
shape for two waves without a single failing test, and the first symptom was
``IntegrityError`` in front of a demo. A demo that cannot be seeded is a demo that
cannot be shown, so the seeder gets the same treatment as the anchor CLI: the
documented command, a real file, the documented exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from approvals import _payload_digest
from marketing_site import PublicReadModel
from primitives import SystemClock
from schema_loader import connect_sqlite

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "seed_demo.py"

FLOW_TABLES = ("properties", "units", "holds", "bookings", "invoices", "payments")
GUARDED = (("properties", "property_id"), ("units", "unit_id"),
           ("bookings", "booking_id"), ("cases", "case_id"))


def _run(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120,
    )


def _count(db: Path, table: str, where: str = "") -> int:
    conn = connect_sqlite(str(db))
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}").fetchone()
        return int(row["n"])
    finally:
        conn.close()


@pytest.fixture()
def seeded(tmp_path: Path) -> Path:
    db = tmp_path / "demo.db"
    result = _run(db)
    assert result.returncode == 0, result.stderr
    return db


def test_a_fresh_path_is_seeded_end_to_end(seeded: Path):
    for table in FLOW_TABLES:
        assert _count(seeded, table) > 0, f"{table} is empty after seeding"


def test_the_demo_hold_carries_what_the_hold_contract_requires(seeded: Path):
    """The exact regression that went unnoticed: a new NOT NULL column and a new vocabulary."""
    conn = connect_sqlite(str(seeded))
    try:
        row = conn.execute("SELECT status, expires_timezone FROM holds").fetchone()
    finally:
        conn.close()
    assert row["status"] == "CONFIRMED"
    assert row["expires_timezone"]


def test_every_identity_is_prefixed_so_preflight_can_refuse_a_production_database(seeded: Path):
    """``preflight.py`` recognises a demo database by this prefix; it has to hold."""
    conn = connect_sqlite(str(seeded))
    try:
        for table, column in GUARDED:
            stray = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} NOT LIKE 'demo_%'"
            ).fetchone()["n"]
            assert int(stray) == 0, f"{table} holds {stray} row(s) without the demo prefix"
    finally:
        conn.close()


def test_seeding_twice_refuses_and_leaves_the_first_dataset_untouched(seeded: Path):
    before = _count(seeded, "properties")
    second = _run(seeded)
    assert second.returncode == 1
    assert "demo dataset" in second.stderr
    assert _count(seeded, "properties") == before


def _scalar(conn, sql: str, *params) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def test_the_demo_can_never_rot_into_hiding_a_working_feature(seeded: Path):
    """The demo is anchored at seed time, so its time-sensitive rows are still live.

    A frozen calendar anchor rots in the quiet direction: expired availability evidence
    sends the public availability hint to its neutral fallback (a shipped feature then
    reads as "not built"), an expired approval empties the approval queue, and a past
    quote due date paints the SLA as breached on arrival. Asserted against the real
    clock, which is what a buyer's browser uses.
    """
    conn = connect_sqlite(str(seeded))
    now = SystemClock().now().isoformat()
    try:
        assert _scalar(
            conn, "SELECT COUNT(*) FROM availability_evidence WHERE valid_until > ?", now
        ) > 0, "every availability evidence row is already expired"
        assert _scalar(
            conn, "SELECT COUNT(*) FROM approval_requests"
            " WHERE status = 'PENDING' AND expires_at > ?", now
        ) > 0, "the pending decision has already expired"
        assert _scalar(
            conn, "SELECT COUNT(*) FROM cases WHERE quote_due_at > ?", now
        ) > 0, "every case is already past its quote due date"

        model = PublicReadModel(conn, "agency_default")
        villas = model.sellable_villas()
        assert villas, "the demo has no sellable villa to show"
        hints = [model.availability_hint(villa["property_id"], now) for villa in villas]
        assert any("not published here" not in hint for hint in hints), (
            "every demo villa falls back to the neutral availability sentence, so the "
            f"hint feature is invisible in the demo: {hints}"
        )
    finally:
        conn.close()


def test_a_database_holding_real_rows_is_refused(seeded: Path):
    """A non-demo identity anywhere in the guarded tables stops the seeder."""
    conn = connect_sqlite(str(seeded))
    try:
        # A real property alongside the demo ones — the state the guard exists for. The
        # row is copied from a demo row so the test does not have to know the vocabulary
        # the CHECK constraints expect.
        conn.execute(
            "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
            " province, timezone, status, contract_pack_status,"
            " contract_pack_evaluated_at, created_at, updated_at)"
            " SELECT 'prop_real', agency_id, name, property_type, region, province,"
            " timezone, status, contract_pack_status, contract_pack_evaluated_at,"
            " created_at, updated_at FROM properties"
            " WHERE property_id LIKE 'demo_%' ORDER BY property_id LIMIT 1"
        )
        conn.commit()
    finally:
        conn.close()

    result = _run(seeded)
    assert result.returncode == 1
    assert "non-demo rows" in result.stderr


def _scheduler(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(REPO / "src" / "scheduler.py"), "--db", str(db), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120,
    )


def test_the_demo_approval_is_intact_so_the_scheduler_can_run(seeded: Path):
    """The demo approval must carry its real digest, or the documented demo step refuses.

    Found 2026-09-28 by the fresh-database audit: the seeder wrote a placeholder
    ``payload_sha256`` (``"9" * 64``), so ``ApprovalService`` refused the request as tampered —
    and ``scheduler.py``, which the README and SALES_PACKAGE both show as a demo step, refused
    the **whole** tick with ``APPROVAL_TAMPERED``. The pending decision could never be approved
    either, so the demo of the approval queue was dead too. Pinned through the service's own
    digest, not by re-reading the constant the seeder wrote.
    """
    conn = connect_sqlite(str(seeded))
    try:
        row = conn.execute(
            "SELECT payload, payload_sha256 FROM approval_requests WHERE approval_id = ?",
            ("demo_appr_1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "the demo dataset no longer carries its pending decision"
    assert row["payload_sha256"] == _payload_digest(json.loads(row["payload"]))

    run = _scheduler(seeded)
    assert "APPROVAL_TAMPERED" not in run.stdout + run.stderr


def test_dry_run_reports_without_writing_rows(tmp_path: Path):
    db = tmp_path / "demo.db"
    result = _run(db, "--dry-run")
    assert result.returncode == 0, result.stderr
    for table in FLOW_TABLES:
        assert _count(db, table) == 0, f"{table} was written by --dry-run"
