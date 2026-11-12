"""End-to-end tests for the §30 closure-facts CLI: how a tenant's staff records the facts.

``test_deposit_resolutions.py`` and ``test_supplier_settlements.py`` prove the *semantics*
of the two facts. These prove that the documented command, run by a real operator against a
real database file, produces the documented effect and the documented exit code — including
refusing to write into an unnamed database, refusing a fact with no evidence, and refusing
what the store refuses (exit 1, nothing changed).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from primitives import FrozenClock, IdFactory
from reservation_kernel import ReservationKernel
from schema_loader import connect_sqlite
from test_reservation_kernel import NOW, make_policy

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "closure_facts.py"
BOOKING = "book_cli"
BASE_ARGS = (
    "--booking", BOOKING,
    "--decided-by", "staff_finance",
    "--actor-role", "finance_manager",
    "--evidence-ref", "transfer_sup_001",
    "--idempotency-key", "cli-e2e-1",
)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """A real database file: the full schema, one property/unit/policy, one finished booking
    holding a deposit — the state a closing agent sits in at check-out."""

    path = tmp_path / "villa.db"
    kernel = ReservationKernel.open(
        str(path), clock=FrozenClock.at(NOW), ids=IdFactory(deterministic=True, seed="cli")
    )
    kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1")
    kernel.upsert_unit(unit_id="unit_1", property_id="prop_1", name="Villa unit_1",
                       base_capacity=2, max_guests=6)
    kernel.insert_policy_version(make_policy().as_row())
    kernel.conn.execute(
        "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
        " status, guest_ref, adults, children, infants, currency, policy_version_id,"
        " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
        " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
        " updated_at, confirmed_at, cancelled_at)"
        " VALUES (?, 'prop_1', 'unit_1', '2026-10-01', '2026-10-03', 2, 'COMPLETED',"
        " 'guest_cli', 2, 0, 0, 'IDR', 'policy_v1', ?, NULL, NULL, 1000000, 0, 2000000,"
        " 'UNPAID', 'idem_cli_booking', 1, ?, ?, ?, NULL)",
        (BOOKING, "0" * 64, NOW, NOW, NOW),
    )
    kernel.conn.commit()
    kernel.close()
    return path


def _run(
    db: Path | None, *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the CLI. ``db=None`` leaves VILLA_DB unset — the case a write must refuse.

    ``cwd`` defaults to the repository. Pointing it at a directory with no
    ``data/villa.db`` reproduces a fresh checkout: the target must still be named first.
    """

    env = dict(os.environ)
    env.pop("VILLA_DB", None)
    if db is not None:
        env["VILLA_DB"] = str(db)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=str(cwd or REPO), timeout=120,
    )


def test_status_on_the_fixture_shows_the_two_facts_block_by_name(db: Path):
    result = _run(db, "status", "--booking", BOOKING)
    assert result.returncode == 0
    assert "BLOCKED" in result.stdout
    assert "supplier_reconciliation" in result.stdout
    assert "security_deposit_resolved" in result.stdout


def test_a_write_without_villa_db_is_a_configuration_error(db: Path):
    result = _run(None, "deposit", *BASE_ARGS, "--action", "APPLIED",
                  "--amount", "2000000", "--currency", "IDR")
    assert result.returncode == 2
    assert "VILLA_DB" in result.stderr


def test_a_write_without_villa_db_names_the_target_even_with_no_default_database(
    db: Path, tmp_path: Path
):
    """A fresh checkout has no data/villa.db. The refusal must name VILLA_DB, not the file.

    The existence check used to answer first, so the operator was told "database not
    found" while the CLI had already fallen back to a default path a write must not guess.
    GitHub Actions caught this on a checkout with no data/villa.db; this test catches it
    anywhere.
    """

    result = _run(None, "deposit", *BASE_ARGS, "--action", "APPLIED",
                  "--amount", "2000000", "--currency", "IDR", cwd=tmp_path)
    assert result.returncode == 2
    assert "VILLA_DB must be set explicitly" in result.stderr
    assert "database not found" not in result.stderr


def test_a_write_with_no_evidence_is_refused_before_anything_is_written(db: Path):
    result = _run(db, "settlement", "--booking", BOOKING, "--action", "DUE",
                  "--amount", "7000000", "--currency", "IDR",
                  "--decided-by", "staff_finance", "--actor-role", "finance_manager")
    assert result.returncode == 2
    conn = connect_sqlite(str(db))
    assert conn.execute("SELECT COUNT(*) AS n FROM supplier_settlements").fetchone()["n"] == 0


def test_a_machine_actor_is_refused_by_the_store_exit_1(db: Path):
    result = _run(db, "deposit", "--booking", BOOKING, "--action", "APPLIED",
                  "--amount", "2000000", "--currency", "IDR",
                  "--decided-by", "ai:reservation-agent", "--actor-role", "ai_service",
                  "--evidence-ref", "transfer_sup_001", "--idempotency-key", "cli-e2e-machine")
    assert result.returncode == 1
    assert "REFUSED" in result.stderr
    conn = connect_sqlite(str(db))
    assert conn.execute("SELECT COUNT(*) AS n FROM deposit_resolutions").fetchone()["n"] == 0


def test_deposit_and_settlement_land_and_the_gate_answers_ok(db: Path):
    assert _run(db, "settlement", *BASE_ARGS, "--action", "DUE", "--amount", "7000000",
                "--currency", "IDR").returncode == 0
    assert _run(db, "deposit", *BASE_ARGS, "--action", "APPLIED", "--amount", "2000000",
                "--currency", "IDR", "--idempotency-key", "cli-e2e-dep").returncode == 0
    assert _run(db, "settlement", *BASE_ARGS, "--action", "PAID", "--amount", "7000000",
                "--currency", "IDR", "--idempotency-key", "cli-e2e-paid").returncode == 0

    status = _run(db, "status", "--booking", BOOKING)
    assert status.returncode == 0
    assert "is settled in IDR" in status.stdout
    assert "is resolved in IDR" in status.stdout
    assert "supplier_reconciliation" not in status.stdout.split("blockers")[-1]
    assert "security_deposit_resolved" not in status.stdout.split("blockers")[-1]


def test_the_store_refusal_exits_1_and_writes_nothing(db: Path):
    assert _run(db, "settlement", *BASE_ARGS, "--action", "DUE", "--amount", "1000000",
                "--currency", "IDR").returncode == 0
    result = _run(db, "settlement", *BASE_ARGS, "--action", "PAID", "--amount", "2000000",
                  "--currency", "IDR", "--idempotency-key", "cli-e2e-overpaid")
    assert result.returncode == 1
    assert "SUPPLIER_OVER_PAID" in result.stderr
    conn = connect_sqlite(str(db))
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM supplier_settlements WHERE action = 'PAID'"
    ).fetchone()["n"] == 0


def test_a_missing_database_is_a_configuration_error():
    result = _run(Path("/tmp/villa-agency-os-no-such-db.db"), "status", "--booking", BOOKING)
    assert result.returncode == 2
    assert "database not found" in result.stderr


if __name__ == "__main__":
    pytest.main([__file__])