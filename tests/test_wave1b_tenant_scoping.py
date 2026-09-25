"""Wave 1-B RED tests: tenant scope at service/query boundary.

Master Spec §18/§39: backend/domain layer, not frontend, enforces resource scope.
These tests focus on the reservation kernel because it is the inventory writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import reservation_kernel as RK
from primitives import DomainError, FrozenClock, IdFactory

NOW = "2026-09-21T00:00:00+00:00"


def kernel(db: Path, agency_id: str, seed: str) -> RK.ReservationKernel:
    return RK.ReservationKernel.open(
        db,
        agency_id=agency_id,
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed=seed),
    )


def insert_minimal_policy(k: RK.ReservationKernel, policy_id: str = "policy_v1") -> None:
    k.conn.execute(
        """
        INSERT INTO policy_versions (
            policy_version_id, agency_id, property_id, unit_scope, version,
            effective_from, effective_to, currency, deposit_pct, service_fee_pct,
            tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,
            max_hold_extensions, payload, fingerprint, source_doc_ref,
            approved_by, created_at
        ) VALUES (
            ?, ?, 'prop_a', 'property', 1,
            '2026-01-01', '2026-12-31', 'IDR', '30', '5',
            '10', '15', 48, 720, 2, '{}', ?,
            'policy/2026.pdf', 'user:owner', ?
        )
        """,
        (policy_id, k.agency_id, "a" * 64, NOW),
    )


def test_kernel_writes_property_and_unit_under_its_agency_scope(tmp_path):
    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    try:
        a.upsert_property(property_id="prop_a", name="Villa A")
        a.upsert_unit(unit_id="unit_a", property_id="prop_a", name="Unit A")
        prop = a.conn.execute("SELECT agency_id FROM properties WHERE property_id = 'prop_a'").fetchone()
        unit = a.conn.execute("SELECT agency_id FROM units WHERE unit_id = 'unit_a'").fetchone()
        assert prop["agency_id"] == "agency_a"
        assert unit["agency_id"] == "agency_a"
    finally:
        a.close()


def test_cross_agency_unit_creation_is_masked_as_missing_property(tmp_path):
    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    b = kernel(db, "agency_b", "b")
    try:
        a.upsert_property(property_id="prop_a", name="Villa A")
        with pytest.raises(DomainError) as ctx:
            b.upsert_unit(unit_id="unit_b", property_id="prop_a", name="Stolen Unit")
        assert ctx.value.code == "PROPERTY_NOT_FOUND"
    finally:
        a.close()
        b.close()


def test_cross_agency_hold_request_cannot_see_or_lock_other_agency_unit(tmp_path):
    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    b = kernel(db, "agency_b", "b")
    try:
        a.upsert_property(property_id="prop_a", name="Villa A")
        a.upsert_unit(unit_id="unit_a", property_id="prop_a", name="Unit A")
        # Policy still belongs to the default fixture style; the failure we need
        # to observe is earlier: B must not even see A's unit.
        with pytest.raises(DomainError) as ctx:
            b.request_hold(
                unit_id="unit_a",
                check_in="2026-10-01",
                check_out="2026-10-03",
                guest_ref="guest_b",
                policy_version_id="policy_missing",
                idempotency_key="idem_b_hold_1",
            )
        assert ctx.value.code == "UNIT_NOT_FOUND"
    finally:
        a.close()
        b.close()


def test_occupancy_query_does_not_leak_other_agency_rows(tmp_path):
    db = tmp_path / "tenant.db"
    a = kernel(db, "agency_a", "a")
    b = kernel(db, "agency_b", "b")
    try:
        a.upsert_property(property_id="prop_a", name="Villa A")
        a.upsert_unit(unit_id="unit_a", property_id="prop_a", name="Unit A")
        insert_minimal_policy(a)
        a.request_hold(
            unit_id="unit_a",
            check_in="2026-10-01",
            check_out="2026-10-03",
            guest_ref="guest_a",
            policy_version_id="policy_v1",
            idempotency_key="idem_hold_a",
        )
        assert b.occupancy("unit_a", start="2026-10-01", end="2026-10-03") == ()
    finally:
        a.close()
        b.close()
