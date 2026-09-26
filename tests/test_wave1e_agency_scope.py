"""Wave 1-E tests: agency scope for sla_breaches, approval_requests, reconciliation_cases.

Master Spec authority (Villa Agency OS — Hermes Super Dev Suite Master Specification
§18/§39): a backend/domain layer isolates tenants, and every resource that can be read
or written carries the tenant it belongs to.

Wave 1-D closed the read paths over tables that already had ``agency_id``. It left three
*root-level* tables unscoped, because they simply had no column to filter on:

* ``sla_breaches``  — a deadline breach, raised for cases, payments, ops tasks, incidents;
* ``approval_requests`` — the human sign-off queue, including money movements;
* ``reconciliation_cases`` — an unmatched-money exception with an owner and a deadline.

Wave 1-E gives each of them an ``agency_id``, makes the writers tag their rows, and makes
the readers filter. Child rows (``approval_decisions``, ``payment_events``,
``ledger_entries``, ``invoice_lines``, ``case_events`` …) deliberately do **not** get a
second copy of the column: their tenant is derived through the parent row, and a duplicated
column is a second source of truth that can drift.

Data-minimisation note: an IDR amount, a deadline, a playbook code and an approver handle
are all commercial/operational facts. Reading agency A's queue while holding agency B's
scope is a leak even when no name is attached — which is why these assertions are about
row visibility, not only about PII.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import primitives as P  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

START = "2026-09-21T08:00:00Z"
NOW = P.parse_iso(START)
NOW_ISO = P.to_iso(NOW)

#: The three root tables this wave scopes.
ROOT_TABLES = (
    "sla_breaches",
    "approval_requests",
    "reconciliation_cases",
    # WAVE 4-A-2: the availability-check request is a root row and owns its tenant.
    "availability_checks",
)

#: Child tables whose tenant must stay *derived* from their parent row.
DERIVED_TABLES = (
    "approval_decisions",
    "payment_events",
    "ledger_entries",
    "invoice_lines",
    "case_events",
    "case_messages",
    "incident_steps",
    "match_candidates",
    "intake_fields",
    "rate_seasons",
    "message_drafts",
    # WAVE 4-A-2: a check's events are scoped through their request, never duplicated.
    "availability_check_events",
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def conn():
    connection = connect_sqlite(":memory:")
    yield connection
    connection.close()


def columns(conn, table) -> dict:
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def agency() -> str:
    return "agency_a"


def seed_agency(conn, agency_id: str) -> None:
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES (?, ?, 'ACTIVE', ?, ?)"
        " ON CONFLICT(agency_id) DO NOTHING",
        (agency_id, agency_id, NOW_ISO, NOW_ISO),
    )


def seed_property_unit(conn, agency_id: str, suffix: str) -> tuple[str, str]:
    property_id = f"prop_{suffix}"
    unit_id = f"unit_{suffix}"
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
        " province, timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE',"
        " 'ACTIVATED', ?, ?)",
        (property_id, agency_id, f"Villa {suffix}", NOW_ISO, NOW_ISO),
    )
    conn.execute(
        "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
        " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'entire_villa', 2, 2, 2, 4, 'ACTIVE', ?, ?)",
        (unit_id, agency_id, property_id, f"Unit {suffix}", NOW_ISO, NOW_ISO),
    )
    return property_id, unit_id


def seed_breach(conn, agency_id: str, suffix: str, *, kind: str = "OPS_TASK") -> str:
    breach_id = f"breach_{suffix}"
    subject_type = "payment" if kind == "UNMATCHED_PAYMENT" else "ops_task"
    conn.execute(
        "INSERT INTO sla_breaches (breach_id, agency_id, subject_type, subject_id, kind,"
        " due_at, detected_at, breach_minutes, owner_actor, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 30, 'ops_1', 'OPEN', ?)",
        (
            breach_id,
            agency_id,
            subject_type,
            f"task_{suffix}",
            kind,
            NOW_ISO,
            NOW_ISO,
            NOW_ISO,
        ),
    )
    return breach_id


def seed_recon(conn, agency_id: str, suffix: str) -> str:
    recon_id = f"recon_{suffix}"
    conn.execute(
        "INSERT INTO reconciliation_cases (recon_id, agency_id, kind, detected_at, due_at,"
        " status, details, created_at)"
        " VALUES (?, ?, 'UNMATCHED_PAYMENT', ?, ?, 'OPEN', '{}', ?)",
        (recon_id, agency_id, NOW_ISO, "2026-09-22T08:00:00+00:00", NOW_ISO),
    )
    return recon_id


def seed_approval(
    conn,
    agency_id: str,
    suffix: str,
    *,
    expires_at: str,
    requested_at: str = NOW_ISO,
) -> str:
    approval_id = f"appr_{agency_id}_{suffix}"
    conn.execute(
        "INSERT INTO approval_requests (approval_id, agency_id, subject_type, subject_id,"
        " action, risk_level, requested_by, requested_role, requested_at, reason,"
        " evidence_refs, payload, payload_sha256, required_role, required_approvals,"
        " status, expires_at, idempotency_key, created_at)"
        " VALUES (?, ?, 'case', ?, 'refund.deposit', 'MEDIUM', 'agent-1', 'agent', ?, ?,"
        " '[]', '{}', ?, 'owner', 1, 'PENDING', ?, ?, ?)",
        (
            approval_id,
            agency_id,
            f"case_{suffix}",
            requested_at,
            "pembatalan tamu meminta pengembalian dana",
            "0" * 64,
            expires_at,
            f"idem_{suffix}_0001",
            NOW_ISO,
        ),
    )
    return approval_id


# --------------------------------------------------------------------------- #
# 1. Storage contract
# --------------------------------------------------------------------------- #
def test_root_tables_carry_agency_scope(conn):
    """Each root table owns its tenant column, defaulting to the single-tenant agency."""
    for table in ROOT_TABLES:
        found = columns(conn, table)
        assert "agency_id" in found, f"{table} has no agency_id column"
        column = found["agency_id"]
        assert column["notnull"] == 1, f"{table}.agency_id must be NOT NULL"
        assert "agency_default" in str(column["dflt_value"]), (
            f"{table}.agency_id must default to agency_default so existing rows stay valid"
        )
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()["sql"]
        assert "REFERENCES agencies" in ddl, f"{table}.agency_id must reference agencies"


def test_child_tables_derive_scope_instead_of_duplicating_it(conn):
    """A child row's tenant comes from its parent: no second copy that can drift."""
    for table in DERIVED_TABLES:
        assert "agency_id" not in columns(conn, table), (
            f"{table} duplicates the tenant column; derive it from the parent row instead"
        )


def test_scoped_uniqueness_allows_the_same_key_per_agency(conn):
    """Two agencies may each breach the same subject id, and reuse the same idempotency key."""
    seed_agency(conn, "agency_a")
    seed_agency(conn, "agency_b")
    seed_breach(conn, "agency_a", "shared")
    # The subject namespace is per agency: this must NOT be read as a duplicate.
    conn.execute(
        "INSERT INTO sla_breaches (breach_id, agency_id, subject_type, subject_id, kind,"
        " due_at, detected_at, breach_minutes, owner_actor, status, created_at)"
        " VALUES ('breach_b_shared', 'agency_b', 'ops_task', 'task_shared', 'OPS_TASK', ?,"
        " ?, 30, 'ops_2', 'OPEN', ?)",
        (NOW_ISO, NOW_ISO, NOW_ISO),
    )
    assert conn.execute("SELECT COUNT(*) FROM sla_breaches").fetchone()[0] == 2

    seed_approval(conn, "agency_a", "shared", expires_at="2026-09-22T08:00:00+00:00")
    seed_approval(conn, "agency_b", "shared", expires_at="2026-09-22T08:00:00+00:00")
    keys = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT idempotency_key FROM approval_requests"
        ).fetchall()
    ]
    assert keys == ["idem_shared_0001"], "the same key must be reusable across agencies"


# --------------------------------------------------------------------------- #
# 2. ApprovalService
# --------------------------------------------------------------------------- #
def test_approval_service_tags_writes_and_masks_cross_agency_reads(conn):
    import approvals as A

    seed_agency(conn, "agency_a")
    seed_agency(conn, "agency_b")
    clock = P.FrozenClock(NOW)
    svc_a = A.ApprovalService(conn, clock, P.IdFactory(seed="a"), agency_id="agency_a")
    svc_b = A.ApprovalService(conn, clock, P.IdFactory(seed="b"), agency_id="agency_b")

    raised = svc_a.request(
        subject_type="case",
        subject_id="case_x",
        action="refund.deposit",
        requested_by="agent-1",
        requested_role="agent",
        reason="pembatalan tamu meminta pengembalian dana",
        idempotency_key="idem_wave1e_0001",
    )
    stored = conn.execute(
        "SELECT agency_id FROM approval_requests WHERE approval_id = ?", (raised.approval_id,)
    ).fetchone()
    assert stored["agency_id"] == "agency_a"

    # Within its own scope the request is visible and actionable.
    assert svc_a.get(raised.approval_id).approval_id == raised.approval_id
    assert raised.approval_id in [item.approval_id for item in svc_a.outstanding(NOW)]
    assert [a.approval_id for a in svc_a.for_subject("case", "case_x")] == [raised.approval_id]

    # Another agency sees nothing: not the row, not the queue, not the subject history.
    with pytest.raises(A.ApprovalNotFound):
        svc_b.get(raised.approval_id)
    assert svc_b.outstanding(NOW) == []
    assert svc_b.for_subject("case", "case_x") == []

    # The idempotency namespace is per agency, so the same key is free here.
    other = svc_b.request(
        subject_type="case",
        subject_id="case_x",
        action="refund.deposit",
        requested_by="agent-2",
        requested_role="agent",
        reason="pembatalan tamu meminta pengembalian dana",
        idempotency_key="idem_wave1e_0001",
    )
    assert other.approval_id != raised.approval_id


def test_approval_expiry_sweep_only_touches_its_own_agency(conn):
    import approvals as A
    import scheduler as S

    seed_agency(conn, "agency_a")
    seed_agency(conn, "agency_b")
    clock = P.FrozenClock(NOW)
    # A real request, then back-dated: the payload digest is what makes the row
    # verifiable, so a hand-written fixture row would be refused as tampered.
    svc_a = A.ApprovalService(conn, clock, P.IdFactory(seed="a"), agency_id="agency_a")
    approval = svc_a.request(
        subject_type="case",
        subject_id="case_expired",
        action="refund.deposit",
        requested_by="agent_a",
        requested_role="agent",
        reason="tamuan membatalkan reservasi sebelum kedatangan",
        required_role="owner",
        required_approvals=2,
        idempotency_key="idem_wave1e_expired",
    )
    approval_id = approval.approval_id
    conn.execute(
        "UPDATE approval_requests SET requested_at = ?, expires_at = ?"
        " WHERE approval_id = ?",
        ("2026-09-20T07:00:00+00:00", "2026-09-21T07:00:00+00:00", approval_id),
    )
    svc_b = A.ApprovalService(conn, clock, P.IdFactory(seed="b"), agency_id="agency_b")
    S.Scheduler(
        conn, clock=clock, approvals=svc_b, sweeps=("approvals",), agency_id="agency_b"
    ).tick()
    still_pending = conn.execute(
        "SELECT status FROM approval_requests WHERE approval_id = ?", (approval_id,)
    ).fetchone()["status"]
    assert still_pending == "PENDING", "agency_b expired agency_a's request"

    svc_a = A.ApprovalService(conn, clock, P.IdFactory(seed="a2"), agency_id="agency_a")
    S.Scheduler(
        conn, clock=clock, approvals=svc_a, sweeps=("approvals",), agency_id="agency_a"
    ).tick()
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE approval_id = ?", (approval_id,)
    ).fetchone()["status"] == "EXPIRED"


# --------------------------------------------------------------------------- #
# 3. Scheduler breach writer
# --------------------------------------------------------------------------- #
def test_breach_sweep_is_agency_scoped(conn):
    import ops_tasks as OT
    import scheduler as S

    seed_agency(conn, "agency_a")
    seed_agency(conn, "agency_b")
    clock = P.FrozenClock(NOW)
    _, unit_id = seed_property_unit(conn, "agency_a", "a")
    OT.OpsTaskService(conn, clock=clock, ids=P.IdFactory(seed="a"), agency_id="agency_a").create_task(
        kind=OT.KIND_CLEANING,
        assignee="staff_014",
        due_at="2026-09-21T07:00:00Z",
        idempotency_key="idem_wave1e_task1",
        unit_id=unit_id,
    )

    S.Scheduler(conn, clock=clock, sweeps=("ops_sla",), agency_id="agency_b").tick()
    assert conn.execute("SELECT COUNT(*) FROM sla_breaches").fetchone()[0] == 0, (
        "agency_b recorded a breach for agency_a's task"
    )

    S.Scheduler(conn, clock=clock, sweeps=("ops_sla",), agency_id="agency_a").tick()
    rows = conn.execute("SELECT agency_id, subject_id FROM sla_breaches").fetchall()
    assert len(rows) == 1
    assert rows[0]["agency_id"] == "agency_a"
    assert rows[0]["subject_id"].startswith("task")


# --------------------------------------------------------------------------- #
# 4. PaymentService reads
# --------------------------------------------------------------------------- #
def test_payment_reconciliation_reads_are_agency_scoped(conn):
    import payments as PAY

    seed_agency(conn, "agency_a")
    seed_agency(conn, "agency_b")
    clock = P.FrozenClock(NOW)
    recon_id = seed_recon(conn, "agency_a", "a")
    seed_breach(conn, "agency_a", "pay", kind="UNMATCHED_PAYMENT")

    svc_b = PAY.PaymentService(conn, clock, P.IdFactory(seed="b"), agency_id="agency_b")
    assert svc_b.unmatched() == ()
    assert svc_b.breaches() == ()
    with pytest.raises(PAY.ReconciliationNotFound):
        svc_b.get_reconciliation(recon_id)

    svc_a = PAY.PaymentService(conn, clock, P.IdFactory(seed="a"), agency_id="agency_a")
    assert [case.recon_id for case in svc_a.unmatched()] == [recon_id]
    assert svc_a.get_reconciliation(recon_id).recon_id == recon_id
    assert len(svc_a.breaches()) == 1


# --------------------------------------------------------------------------- #
# 5. Runtime agency selection
# --------------------------------------------------------------------------- #
def test_runtime_agency_resolution(monkeypatch):
    monkeypatch.delenv(P.AGENCY_ENV_VAR, raising=False)
    assert P.resolve_agency_id() == P.DEFAULT_AGENCY_ID

    monkeypatch.setenv(P.AGENCY_ENV_VAR, "agency_yogyakarta")
    assert P.resolve_agency_id() == "agency_yogyakarta"

    # An explicit value is what the caller decided; the environment does not override it.
    assert P.resolve_agency_id("agency_bali") == "agency_bali"

    monkeypatch.setenv(P.AGENCY_ENV_VAR, "bukan agency id!")
    with pytest.raises(P.DomainError):
        P.resolve_agency_id()

def test_build_scheduler_wires_a_single_agency_into_every_collaborator(conn, monkeypatch):
    """One tick must not half-serve one tenant: the factory resolves once."""
    import scheduler as S

    monkeypatch.setenv(P.AGENCY_ENV_VAR, "agency_jogja")
    built = S.build_scheduler(conn, clock=P.FrozenClock(NOW), ids=P.IdFactory(seed="wire"))

    assert built.agency_id == "agency_jogja"
    assert built.approvals.agency_id == "agency_jogja"
    assert built.kernel.agency_id == "agency_jogja"


def test_an_explicit_agency_beats_the_environment(conn, monkeypatch):
    import scheduler as S

    monkeypatch.setenv(P.AGENCY_ENV_VAR, "agency_jogja")
    built = S.build_scheduler(
        conn, clock=P.FrozenClock(NOW), ids=P.IdFactory(seed="wire2"), agency_id="agency_bali"
    )

    assert built.agency_id == "agency_bali"

def test_command_center_counters_no_longer_mix_agencies(conn):
    """The counters Wave 1-D had to leave global are scoped now that the columns exist."""
    import dashboard as D

    seed_agency(conn, "agency_a")
    seed_agency(conn, "agency_b")
    for agency in ("agency_a", "agency_b"):
        seed_breach(conn, agency, f"cc_{agency}")
        seed_approval(
            conn, agency, f"cc_{agency}", expires_at="2026-09-21T10:00:00+00:00"
        )
        seed_recon(conn, agency, f"cc_{agency}")

    clock = P.FrozenClock(NOW)
    counts = {}
    for agency in ("agency_a", "agency_b"):
        dashboard = D.Dashboard(
            conn,
            clock=clock,
            actor=f"owner_{agency}",
            actor_role="owner",
            agency_id=agency,
        )
        kpis = dashboard.kpi_values()
        counts[agency] = (
            kpis["open_sla_breaches"],
            kpis["pending_approvals"],
        )

    assert counts["agency_a"] == (1, 1), counts
    assert counts["agency_b"] == (1, 1), counts
