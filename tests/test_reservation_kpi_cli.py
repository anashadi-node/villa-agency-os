"""End-to-end tests for the reservation-KPI CLI: the read an operator runs.

``test_reservation_kpi.py`` proves the *semantics* of the bundle. These prove the documented
command — run by a real operator against a real database file — prints the bundle with the
documented exit codes: 0 when the database exists, 2 when it does not, and nothing person-
shaped anywhere in the output (a KPI bundle is integers and ratios).
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "reservation_kpi.py"

sys.path.insert(0, str(REPO / "src"))
from primitives import SystemClock  # noqa: E402  (path setup must run first)


def _run(db: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("VILLA_DB", None)
    if db is not None:
        env["VILLA_DB"] = str(db)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120,
    )


def _fresh_db(tmp_path: Path) -> Path:
    """One real database file with a schema and a couple of facts to count."""
    import sys

    sys.path.insert(0, str(REPO / "src"))
    from primitives import FrozenClock, IdFactory
    from reservation_kernel import ReservationKernel
    from test_reservation_kernel import NOW, make_policy, make_rate_card

    db = tmp_path / "villa_kpi.db"
    kernel = ReservationKernel.open(
        str(db), clock=FrozenClock.at(NOW), ids=IdFactory(deterministic=True, seed="kpi")
    )
    kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1")
    kernel.upsert_unit(unit_id="unit_1", property_id="prop_1", name="Unit 1",
                       base_capacity=2, max_guests=6)
    kernel.insert_policy_version(make_policy().as_row())
    rc = make_rate_card()
    kernel.insert_rate_card({
        "rate_card_id": rc.rate_card_id, "property_id": rc.property_id,
        "unit_scope": rc.unit_scope, "name": rc.name, "version": rc.version,
        "effective_from": rc.effective_from, "effective_to": rc.effective_to,
        "currency": getattr(rc, "currency", "IDR"),
        "weekly_discount_pct": str(rc.weekly_discount_pct),
        "monthly_discount_pct": str(rc.monthly_discount_pct),
        "source_doc_ref": rc.source_doc_ref, "approved_by": rc.approved_by, "created_at": NOW,
    })
    # One hour ago, on the same clock the CLI subprocess will read.
    created = SystemClock().now() - timedelta(hours=1)
    recent = created.isoformat()
    first_due = (created + timedelta(hours=1)).isoformat()
    quote_due = (created + timedelta(hours=4)).isoformat()
    kernel.conn.execute(
        "INSERT INTO cases (case_id, agency_id, guest_ref, channel, status, priority,"
        " sla_policy, first_response_due_at, quote_due_at, idempotency_key, created_at, updated_at)"
        " VALUES ('case_cli_1', 'agency_default', 'guest_cli', 'website', 'NEW', 'NORMAL',"
        " 'default', ?, ?, 'idem-case-cli-1', ?, ?)",
        # The CLI is an operator command: it anchors its window on the *real*
        # clock, not the kernel's frozen one. So this row must be placed relative
        # to that clock — pinning it to the fixture's NOW made the test expire the
        # moment the calendar moved past NOW + 28 days (it went red on 2026-10-25,
        # 34 days after NOW).
        (first_due, quote_due, recent, recent),
    )
    kernel.conn.commit()
    return db


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return _fresh_db(tmp_path)


def test_the_command_prints_the_bundle_against_a_real_database(db: Path):
    result = _run(db, "--days", "28")
    assert result.returncode == 0
    assert "RESERVATION KPIs" in result.stdout
    assert "inquiries" in result.stdout and "1" in result.stdout
    assert "overlapping_bookings" in result.stdout


def test_a_missing_database_is_a_configuration_error(db: Path, tmp_path: Path):
    result = _run(tmp_path / "missing.db")
    assert result.returncode == 2
    assert "database not found" in result.stderr


def test_a_zero_or_negative_window_is_a_usage_error(db: Path):
    result = _run(db, "--days", "0")
    assert result.returncode == 2


def test_the_output_carries_no_person_shaped_field(db: Path):
    result = _run(db)
    # guest_ref never appears: the bundle is integers and ratios only.
    assert "guest_cli" not in result.stdout
    assert "case_cli_1" not in result.stdout