"""Wave 1-F tests: tenant scope for the idempotency ledger itself.

Master Spec §18/§39: the domain layer, not the frontend, owns resource scope. Two
agencies are separate businesses, so a key one of them chose must not collide with
a key the other chose — and a lookup must never resolve to the other tenant's row.

Before this wave `idempotency_keys` was keyed by the key alone, so the second agency
to pick a common string (`hold-2026-10-01`) was refused as an IDEMPOTENCY_CONFLICT it
did not cause, and `ops_tasks.idempotency_key` had the same global shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ops_tasks as OT
import reservation_kernel as RK
from primitives import DomainError, FrozenClock, IdFactory

NOW = "2026-09-21T00:00:00+00:00"
SHARED_KEY = "idem_shared_hold_0001"


def kernel(db, agency_id: str, seed: str) -> RK.ReservationKernel:
    return RK.ReservationKernel.open(
        db,
        agency_id=agency_id,
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed=seed),
    )


def give_inventory(k: RK.ReservationKernel, suffix: str) -> str:
    """A property, a unit and an approved policy, all inside ``k``'s agency."""
    k.upsert_property(property_id=f"prop_{suffix}", name=f"Villa {suffix}")
    k.upsert_unit(unit_id=f"unit_{suffix}", property_id=f"prop_{suffix}", name=f"Unit {suffix}")
    policy_id = f"policy_{suffix}"
    k.conn.execute(
        """
        INSERT INTO policy_versions (
            policy_version_id, agency_id, property_id, unit_scope, version,
            effective_from, effective_to, currency, deposit_pct, service_fee_pct,
            tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,
            max_hold_extensions, payload, fingerprint, source_doc_ref,
            approved_by, created_at
        ) VALUES (
            ?, ?, ?, 'property', 1,
            '2026-01-01', '2026-12-31', 'IDR', '30', '5',
            '10', '15', 48, 720, 2, '{}', ?,
            'policy/2026.pdf', 'user:owner', ?
        )
        """,
        (policy_id, k.agency_id, f"prop_{suffix}", "a" * 64, NOW),
    )
    return policy_id


def request_hold(k: RK.ReservationKernel, suffix: str, key: str = SHARED_KEY, guest: str | None = None):
    return k.request_hold(
        unit_id=f"unit_{suffix}",
        check_in="2026-10-01",
        check_out="2026-10-03",
        guest_ref=guest or f"guest_{suffix}",
        policy_version_id=f"policy_{suffix}",
        idempotency_key=key,
    )


# --------------------------------------------------------------------------- #
# 1. The storage contract
# --------------------------------------------------------------------------- #
def test_the_idempotency_ledger_is_keyed_per_agency(tmp_path):
    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    try:
        columns = {
            row["name"]: row for row in a.conn.execute("PRAGMA table_info(idempotency_keys)")
        }
        assert "agency_id" in columns, "idempotency_keys has no agency_id column"
        assert columns["agency_id"]["notnull"] == 1
        assert columns["agency_id"]["pk"] >= 1, "agency_id is not part of the primary key"
        assert columns["idempotency_key"]["pk"] >= 1
        ddl = a.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idempotency_keys'"
        ).fetchone()[0]
        assert "REFERENCES agencies" in ddl, "idempotency_keys does not reference agencies"
    finally:
        a.close()


def test_ops_task_idempotency_is_scoped_per_agency(tmp_path):
    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    b = kernel(db, "agency_b", "b")
    clock = FrozenClock.at(NOW)
    try:
        a.upsert_property(property_id="prop_a", name="Villa A")
        a.upsert_unit(unit_id="unit_a", property_id="prop_a", name="Unit A")
        b.upsert_property(property_id="prop_b", name="Villa B")
        b.upsert_unit(unit_id="unit_b", property_id="prop_b", name="Unit B")
        common = dict(
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at="2026-09-22T00:00:00Z",
            idempotency_key="idem_shared_task_0001",
        )
        task_a = OT.OpsTaskService(a.conn, clock=clock, agency_id="agency_a").create_task(
            unit_id="unit_a", **common
        )
        task_b = OT.OpsTaskService(b.conn, clock=clock, agency_id="agency_b").create_task(
            unit_id="unit_b", **common
        )
        assert task_a.task_id != task_b.task_id
        stored = a.conn.execute(
            "SELECT agency_id, COUNT(*) AS n FROM ops_tasks"
            " WHERE idempotency_key = ? GROUP BY agency_id ORDER BY agency_id",
            ("idem_shared_task_0001",),
        ).fetchall()
        assert [(row["agency_id"], row["n"]) for row in stored] == [("agency_a", 1), ("agency_b", 1)]
    finally:
        a.close()
        b.close()


# --------------------------------------------------------------------------- #
# 2. The kernel's real write path
# --------------------------------------------------------------------------- #
def test_two_agencies_can_hold_with_the_same_idempotency_key(tmp_path):
    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    b = kernel(db, "agency_b", "b")
    try:
        give_inventory(a, "a")
        give_inventory(b, "b")

        hold_a = request_hold(a, "a")
        hold_b = request_hold(b, "b")

        assert hold_a.hold_id != hold_b.hold_id
        ledger = a.conn.execute(
            "SELECT agency_id FROM idempotency_keys WHERE idempotency_key = ?"
            " ORDER BY agency_id",
            (SHARED_KEY,),
        ).fetchall()
        assert [row["agency_id"] for row in ledger] == ["agency_a", "agency_b"]
        tagged = a.conn.execute(
            "SELECT agency_id FROM holds WHERE hold_id = ?", (hold_b.hold_id,)
        ).fetchone()
        assert tagged["agency_id"] == "agency_b"
    finally:
        a.close()
        b.close()


def test_a_replayed_hold_resolves_inside_its_own_agency(tmp_path):
    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    b = kernel(db, "agency_b", "b")
    try:
        give_inventory(a, "a")
        give_inventory(b, "b")
        first = request_hold(a, "a")
        other = request_hold(b, "b")

        replay = request_hold(a, "a")

        assert replay.hold_id == first.hold_id, "the replay did not resolve to its own hold"
        assert replay.hold_id != other.hold_id
        total_holds = a.conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0]
        assert total_holds == 2, "a replay wrote a third hold"
    finally:
        a.close()
        b.close()


def test_a_key_reused_for_a_different_request_still_conflicts(tmp_path):
    """Scoping must not weaken the guarantee it exists to provide."""
    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    try:
        give_inventory(a, "a")
        request_hold(a, "a")
        with pytest.raises(DomainError) as ctx:
            a.request_hold(
                unit_id="unit_a",
                check_in="2026-11-01",
                check_out="2026-11-03",
                guest_ref="guest_other",
                policy_version_id="policy_a",
                idempotency_key=SHARED_KEY,
            )
        assert ctx.value.code == "IDEMPOTENCY_CONFLICT"
    finally:
        a.close()

def test_two_agencies_can_open_a_case_with_the_same_key(tmp_path):
    """`cases.idempotency_key` is caller-supplied too, so it is scoped as well."""
    import cases as C

    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    b = kernel(db, "agency_b", "b")
    clock = FrozenClock.at(NOW)
    try:
        common = dict(
            channel="website",
            priority=C.DEFAULT_PRIORITY,
            sla_policy="web_v1",
            sender_ref="sender_0001",
            content_class="P0",
            body="mau tanya ketersediaan villa",
            idempotency_key="idem_shared_case_0001",
            received_at=NOW,
        )
        case_a, _ = C.CaseService(
            a.conn, clock, IdFactory(seed="ca"), agency_id="agency_a"
        ).open_case(guest_ref="guest_a", **common)
        case_b, _ = C.CaseService(
            b.conn, clock, IdFactory(seed="cb"), agency_id="agency_b"
        ).open_case(guest_ref="guest_b", **common)
        assert case_a.case_id != case_b.case_id
        stored = a.conn.execute(
            "SELECT agency_id FROM cases WHERE idempotency_key = ? ORDER BY agency_id",
            ("idem_shared_case_0001",),
        ).fetchall()
        assert [row["agency_id"] for row in stored] == ["agency_a", "agency_b"]
    finally:
        a.close()
        b.close()
