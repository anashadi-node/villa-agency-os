"""Wave 1-F-C tests: reference integrity and a standing audit of unique keys.

Two holes were found by auditing the DDL after Wave 1-F-B, and both are structural:
a stored reference that the database itself does not check.

1. ``approval_decisions.approval_id`` had no foreign key. The decision log — the
   evidence a human authorised something — could hold a signature for a request
   that does not exist.
2. ``message_drafts.approval_id`` had none either, so a draft could point at an
   approval that was never raised.

The third test is not a test of a fix: it is the audit itself, written down as an
executable guard so the next wave cannot quietly reintroduce a globally-unique
key on caller-supplied data. It is deliberately rule-based (see
``unjustified_global_uniques``) and it checks its own rule against a mutated DDL,
so a guard that silently passes everything is caught here rather than trusted.
"""

from __future__ import annotations

import re

import pytest

import primitives as P
from schema_loader import connect_sqlite

NOW = "2026-09-21T08:00:00+00:00"

#: Uniques that legitimately stay global even though their table carries an
#: agency. Each entry states why — a name without a reason is not acceptable.
JUSTIFIED_GLOBAL_UNIQUE = {
    # The PSP owns this namespace: the same provider cannot issue one intent id
    # to two tenants, so the constraint is about the provider, not about us.
    ("payments", "uq_payments_intent"): "provider-owned intent id namespace",
    # A 256-bit token hash must resolve to exactly one session *platform-wide*:
    # scoping it per agency would only weaken the lookup, not add information.
    ("auth_sessions", "uq_auth_session_token"): "session token hash is global by design",
}

CONSTRAINT_RE = re.compile(
    r"CONSTRAINT\s+(\w+)\s+(PRIMARY KEY|UNIQUE)\s*\(([^)]*)\)", re.IGNORECASE
)
TABLE_RE = re.compile(r"CREATE TABLE\s+(\w+)", re.IGNORECASE)
AGENCY_COLUMN_RE = re.compile(r"^\s*agency_id\s+TEXT", re.MULTILINE)
SLOT_RE = re.compile(r"^\s*(\w+)\s+TEXT\s+NOT NULL,\s*$", re.MULTILINE)


def ddl_statements() -> str:
    from schema_loader import load_statements
    return "\n".join(load_statements("sqlite"))


def parse_uniques(ddl: str) -> list[tuple[str, str, str, tuple[str, ...], bool]]:
    """``(table, constraint, kind, columns, table_carries_agency)`` per constraint."""
    found = []
    for block in re.split(r"\n(?=CREATE TABLE )", ddl):
        table_match = TABLE_RE.search(block)
        if table_match is None:
            continue
        table = table_match.group(1)
        carries_agency = bool(AGENCY_COLUMN_RE.search(block))
        for name, kind, column_text in CONSTRAINT_RE.findall(block):
            columns = tuple(part.strip() for part in column_text.split(",") if part.strip())
            found.append((table, name, kind.upper(), columns, carries_agency))
    return found


def primary_keyed_columns(uniques) -> set[str]:
    """Columns that are the primary key of some table: generated opaque tokens."""
    return {columns[0] for _t, _n, kind, columns, _a in uniques
            if kind == "PRIMARY KEY" and len(columns) == 1}


def unjustified_global_uniques(ddl: str) -> list[str]:
    """Uniques on an agency-bearing table that ignore the tenant without a reason.

    A constraint is justified when it is the table's own primary key (a generated
    token, unique by construction), when one of its columns is another table's
    generated primary key (a parent token, likewise unique), or when it is named
    in :data:`JUSTIFIED_GLOBAL_UNIQUE`.
    """
    uniques = parse_uniques(ddl)
    generated = primary_keyed_columns(uniques)
    offenders = []
    for table, name, kind, columns, carries_agency in uniques:
        if not carries_agency or "agency_id" in columns:
            continue
        if kind == "PRIMARY KEY":
            continue
        if any(column in generated for column in columns):
            continue
        if (table, name) in JUSTIFIED_GLOBAL_UNIQUE:
            continue
        offenders.append(f"{table}.{name}({', '.join(columns)})")
    return offenders


# --------------------------------------------------------------------------- #
# 1. The decision log cannot invent a request
# --------------------------------------------------------------------------- #
def test_a_decision_cannot_reference_an_approval_that_does_not_exist():
    conn = connect_sqlite(":memory:")
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'approval_decisions'"
        ).fetchone()[0]
        assert "REFERENCES approval_requests" in ddl, (
            "approval_decisions has no foreign key to approval_requests"
        )
        with pytest.raises(Exception) as caught:
            conn.execute(
                "INSERT INTO approval_decisions (decision_id, approval_id, approver,"
                " approver_role, decision, note, decided_at, request_hash, signature_hash)"
                " VALUES ('dec_ghost', 'approval_tidak_ada', 'owner_1', 'owner', 'APPROVE',"
                " 'setuju', ?, ?, ?)",
                (NOW, "0" * 64, "1" * 64),
            )
        assert "FOREIGN KEY" in str(caught.value).upper(), (
            "a decision for a non-existent request was accepted"
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 2. A draft cannot point at an approval that was never raised
# --------------------------------------------------------------------------- #
def test_a_draft_cannot_reference_an_approval_that_does_not_exist():
    conn = connect_sqlite(":memory:")
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'message_drafts'"
        ).fetchone()[0]
        assert "REFERENCES approval_requests" in ddl, (
            "message_drafts has no foreign key to approval_requests"
        )
        conn.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES ('agency_fkc', 'FKC', 'ACTIVE', ?, ?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT INTO cases (case_id, agency_id, channel, guest_ref, status, priority,"
            " sla_policy, first_response_due_at, quote_due_at, idempotency_key,"
            " created_at, updated_at) VALUES ('case_fkc', 'agency_fkc', 'website',"
            " 'guest_fkc', 'NEW', 'NORMAL', 'web_v1', '2026-09-21T09:00:00+00:00',"
            " '2026-09-21T10:00:00+00:00', 'idem_fkc_0001', ?, ?)",
            (NOW, NOW),
        )
        with pytest.raises(Exception) as caught:
            conn.execute(
                "INSERT INTO message_drafts (draft_id, case_id, channel, template, locale,"
                " recipient_ref, body_text, body_sha256, grounding, gate_decision, status,"
                " approval_id, created_by, created_at) VALUES ('draft_ghost', 'case_fkc',"
                " 'website', 'tpl_v1', 'id-ID', 'guest_fkc', 'halo', ?, 'UNGROUNDED',"
                " 'ALLOW', 'DRAFT', 'approval_tidak_ada', 'agent_1', ?)",
                ("a" * 64, NOW),
            )
        assert "FOREIGN KEY" in str(caught.value).upper(), (
            "a draft pointing at a non-existent approval was accepted"
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 3. The real flow still works (the guard above must not over-constrain)
# --------------------------------------------------------------------------- #
def test_the_approval_flow_still_writes_its_decisions():
    import approvals as A

    conn = connect_sqlite(":memory:")
    try:
        clock = P.FrozenClock(P.parse_iso(NOW))
        svc = A.ApprovalService(conn, clock, P.IdFactory(seed="fkc"), agency_id="agency_a")
        approval = svc.request(
            subject_type="case",
            subject_id="case_real",
            action="refund.deposit",
            requested_by="agent_a",
            requested_role="agent",
            reason="tamuan membatalkan sebelum kedatangan",
            required_role="owner",
            required_approvals=1,
            idempotency_key="idem_fkc_real_0001",
        )
        decided = svc.decide(
            approval.approval_id,
            approver="owner_1",
            approver_role="owner",
            decision="APPROVE",
            note="disetujui untuk refund penuh",
        )
        assert decided.status == "APPROVED"
        assert len(svc.decisions(approval.approval_id)) == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 4. The audit itself, as a standing guard
# --------------------------------------------------------------------------- #
def test_no_unique_key_on_tenant_data_quietly_ignores_the_agency():
    offenders = unjustified_global_uniques(ddl_statements())
    assert offenders == [], (
        "these unique constraints sit on tenant data but ignore agency_id: "
        + ", ".join(offenders)
    )


def test_the_audit_guard_is_not_vacuous():
    """A guard that cannot fail is not a guard: prove it flags a real violation."""
    mutated = ddl_statements() + """
CREATE TABLE suppliers_bad (
    bad_id TEXT NOT NULL,
    agency_id TEXT NOT NULL DEFAULT 'agency_default',
    external_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT pk_suppliers_bad PRIMARY KEY (bad_id),
    CONSTRAINT uq_bad_external_ref UNIQUE (external_ref)
);
"""
    offenders = unjustified_global_uniques(mutated)
    assert offenders == ["suppliers_bad.uq_bad_external_ref(external_ref)"], (
        f"the guard failed to flag a tenant-data key that ignores agency_id: {offenders}"
    )
