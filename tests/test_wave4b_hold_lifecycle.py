"""WAVE 4-B — the §7 hold lifecycle.

§7 states three rules about holds, and every test below is one of them:

1. "Hold expiry dan extension selalu menyimpan timezone" — expiry and extension
   always store a timezone;
2. "EXTENSION_REQUESTED tidak memperpanjang expiry sampai supplier mengonfirmasi"
   — a requested extension does not move the expiry;
3. "Expired supplier hold masuk EXPIRED_PENDING_VERIFICATION, bukan otomatis
   RELEASED kecuali authoritative integration menjamin status" — an expired hold
   is pending verification, and only an authoritative source may release it.

The vocabulary tests pin the module against the schema, because a hold state the
CHECK constraint rejects is a state that cannot be written at all.
"""

from __future__ import annotations

import re
import sqlite3
import unittest
from pathlib import Path

import hold_states as HS
import observability as OBS
import policy_engine as PE
import reservation_kernel as RK
from primitives import DomainError, FrozenClock, IdFactory

NOW = "2026-09-21T00:00:00+00:00"
STALE = "2026-09-20T21:00:00+00:00"  # three hours before NOW: past level A's 900s
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "src" / "schema"

#: Appendix A, verbatim. ``NOT_REQUESTED`` is the absence of a hold row, not a
#: state of one, so a row can never be in it.
APPENDIX_A_HOLD_STATES = (
    "REQUESTED",
    "AWAITING_SUPPLIER",
    "CONFIRMED",
    "REJECTED",
    "EXTENSION_REQUESTED",
    "RELEASE_REQUESTED",
    "RELEASE_CONFIRMED",
    "EXPIRED_PENDING_VERIFICATION",
)


def _schema(name: str) -> str:
    return (SCHEMA_DIR / name).read_text()


def _holds_block(sql: str) -> str:
    match = re.search(r"CREATE TABLE holds \((.*?)\n\);", sql, re.S)
    assert match is not None, "the holds table is not in the schema"
    return match.group(1)


def _values(text: str, pattern: str) -> tuple[str, ...]:
    match = re.search(pattern, text, re.S)
    assert match is not None, f"predicate not found: {pattern}"
    return tuple(value.strip().strip("'") for value in match.group(1).split(","))


def _sqlite_status_enum(sql: str) -> tuple[str, ...]:
    return _values(_holds_block(sql), r"status\s+TEXT\s+NOT NULL CHECK \(status IN \(([^)]*)\)\)")


def _sqlite_index_predicate(sql: str) -> tuple[str, ...]:
    return _values(sql, r"uq_holds_active_slot.*?WHERE status IN \(([^)]*)\)")


def _pg_index_predicate(sql: str) -> tuple[str, ...]:
    return _values(sql, r"uq_holds_active_slot.*?WHERE status IN \(([^)]*)\)")


def _pg_exclusion_predicate(sql: str) -> tuple[str, ...]:
    return _values(sql, r"ex_holds_no_overlap.*?WHERE \(status IN \(([^)]*)\)\)")


def make_policy(**overrides):
    rules = dict(
        currency="IDR",
        deposit_pct="30",
        service_fee_pct="5",
        tax_pbjt_pct="10",
        max_discount_pct="15",
        quote_valid_hours=48,
        hold_ttl_minutes=720,
        max_hold_extensions=2,
        payload={
            "extra_guest_fee_minor": 250_000,
            "cleaning_fee_minor": 500_000,
            "security_deposit_minor": 2_000_000,
            "min_stay_nights": 2,
            "cancellation_ladder": [{"min_days_before": 0, "refund_pct": "100"}],
        },
    )
    rules.update(overrides)
    return PE.PolicyVersion(
        policy_version_id="policy_v1",
        property_id="prop_1",
        unit_scope="property",
        version=1,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        rules=PE.PolicyRules(**rules),
        created_at=NOW,
        source_doc_ref="policy/2026-public.pdf",
        approved_by="user:owner-1",
    )


class HoldTestCase(unittest.TestCase):
    """One property (Asia/Makassar), one unit, one policy."""

    def setUp(self):
        self.kernel = RK.ReservationKernel.open(
            ":memory:",
            clock=FrozenClock.at(NOW),
            ids=IdFactory(deterministic=True, seed="w4b"),
        )
        self.addCleanup(self.kernel.close)
        self.kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1")
        self.kernel.upsert_unit(
            unit_id="unit_1", property_id="prop_1", name="Villa unit_1", base_capacity=2, max_guests=6
        )
        self.kernel.insert_policy_version(make_policy().as_row())

    # -- helpers ----------------------------------------------------------- #
    def evidence(self, *, unit_id="unit_1", source_type="property_api", level="A",
                 result="AVAILABLE", captured_at=None, ttl_hours=24, can_confirm=False):
        return self.kernel.record_evidence(
            unit_id=unit_id,
            source_type=source_type,
            evidence_level=level,
            result=result,
            captured_at=captured_at,
            ttl_hours=ttl_hours,
            can_confirm=can_confirm,
        )

    def hold(self, *, unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03",
             key="idem_hold_0001", evidence_id=None):
        return self.kernel.request_hold(
            unit_id=unit_id,
            check_in=check_in,
            check_out=check_out,
            guest_ref="guest_abc123",
            policy_version_id="policy_v1",
            idempotency_key=key,
            evidence_id=self.evidence() if evidence_id is None else evidence_id,
            actor="user:owner-1",
            actor_role="owner",
        )

    def status_of(self, hold_id):
        row = self.kernel.conn.execute(
            "SELECT status FROM holds WHERE hold_id = ?", (hold_id,)
        ).fetchone()
        return row["status"]

    def hold_row(self, hold_id):
        return self.kernel.conn.execute(
            "SELECT * FROM holds WHERE hold_id = ?", (hold_id,)
        ).fetchone()

    def audit_actions(self):
        return [
            row["action"]
            for row in self.kernel.conn.execute("SELECT action FROM audit_events ORDER BY seq")
        ]

    def extension_row(self, extension_id):
        return self.kernel.conn.execute(
            "SELECT * FROM hold_extension_requests WHERE extension_id = ?", (extension_id,)
        ).fetchone()

    def audit_rows(self, entity_id):
        return self.kernel.conn.execute(
            "SELECT * FROM audit_events WHERE entity_id = ? ORDER BY seq", (entity_id,)
        ).fetchall()


class TestHoldVocabulary(unittest.TestCase):
    """The vocabulary is one thing, asserted in every place it is written."""

    def test_the_ddl_status_check_is_the_canonical_vocabulary(self):
        self.assertEqual(_sqlite_status_enum(_schema("001_initial.sql")), HS.HOLD_STATES)

    def test_the_vocabulary_is_appendix_a_plus_one_documented_extension(self):
        self.assertEqual(
            tuple(s for s in HS.HOLD_STATES if s != HS.HOLD_CONVERTED), APPENDIX_A_HOLD_STATES
        )

    def test_convert_is_the_only_state_the_spec_does_not_name(self):
        self.assertEqual(set(HS.HOLD_STATES) - set(APPENDIX_A_HOLD_STATES), {HS.HOLD_CONVERTED})

    def test_the_legacy_vocabulary_is_gone(self):
        for legacy in ("ACTIVE", "RELEASED", "EXPIRED"):
            self.assertNotIn(legacy, HS.HOLD_STATES, f"{legacy} is not a canonical hold state")

    def test_the_partial_index_predicate_is_the_occupying_set(self):
        self.assertEqual(_sqlite_index_predicate(_schema("001_initial.sql")), HS.OCCUPYING_HOLD_STATES)

    def test_the_pg_twin_carries_the_same_predicate(self):
        self.assertEqual(_pg_index_predicate(_schema("001_initial.pg.sql")), HS.OCCUPYING_HOLD_STATES)

    def test_the_pg_exclusion_constraint_carries_the_same_predicate(self):
        self.assertEqual(
            _pg_exclusion_predicate(_schema("002_pg_range_exclusions.pg.sql")),
            HS.OCCUPYING_HOLD_STATES,
        )

    def test_occupying_and_closed_states_partition_the_vocabulary(self):
        self.assertEqual(set(HS.OCCUPYING_HOLD_STATES) | set(HS.CLOSED_HOLD_STATES), set(HS.HOLD_STATES))
        # The one state that both blocks the slot and is over: an expiry nobody verified.
        self.assertEqual(
            set(HS.OCCUPYING_HOLD_STATES) & set(HS.CLOSED_HOLD_STATES),
            {HS.HOLD_EXPIRED_PENDING_VERIFICATION},
        )

    def test_live_states_are_the_occupying_states_minus_the_unverified_expiry(self):
        self.assertEqual(
            set(HS.LIVE_HOLD_STATES),
            set(HS.OCCUPYING_HOLD_STATES) - {HS.HOLD_EXPIRED_PENDING_VERIFICATION},
        )

    def test_observability_shares_the_predicate(self):
        self.assertEqual(OBS.OCCUPYING_HOLD_STATUSES, HS.OCCUPYING_HOLD_STATES)
        source = (SCHEMA_DIR.parent / "observability.py").read_text()
        self.assertNotIn("FROM holds WHERE status = 'ACTIVE'", source)

    def test_can_transition_refuses_unknown_states(self):
        self.assertFalse(HS.can_transition("NOPE", HS.HOLD_CONFIRMED))
        self.assertFalse(HS.can_transition(HS.HOLD_CONFIRMED, "NOPE"))

    def test_a_closed_state_is_terminal(self):
        for state in (HS.HOLD_REJECTED, HS.HOLD_RELEASE_CONFIRMED, HS.HOLD_CONVERTED):
            for target in HS.HOLD_STATES:
                self.assertFalse(HS.can_transition(state, target), f"{state} -> {target}")

    def test_a_confirmed_hold_may_expire_extend_release_or_convert(self):
        for target in (
            HS.HOLD_EXTENSION_REQUESTED,
            HS.HOLD_RELEASE_REQUESTED,
            HS.HOLD_CONVERTED,
            HS.HOLD_EXPIRED_PENDING_VERIFICATION,
        ):
            self.assertTrue(HS.can_transition(HS.HOLD_CONFIRMED, target), target)

    def test_a_release_request_may_be_refused_by_the_supplier(self):
        self.assertTrue(HS.can_transition(HS.HOLD_RELEASE_REQUESTED, HS.HOLD_CONFIRMED))
        self.assertTrue(HS.can_transition(HS.HOLD_RELEASE_REQUESTED, HS.HOLD_RELEASE_CONFIRMED))

    def test_resolve_timezone_accepts_an_iana_name(self):
        self.assertEqual(HS.resolve_timezone("Asia/Makassar"), "Asia/Makassar")

    def test_resolve_timezone_refuses_a_name_that_is_not_a_zone(self):
        with self.assertRaises(DomainError) as ctx:
            HS.resolve_timezone("Nowhere/Fake")
        self.assertEqual(ctx.exception.code, "HOLD_INVALID_TIMEZONE")

    def test_resolve_timezone_refuses_an_empty_name(self):
        with self.assertRaises(DomainError):
            HS.resolve_timezone("   ")


class TestHoldCreation(HoldTestCase):
    def test_a_hold_is_created_confirmed_with_the_policy_ttl(self):
        outcome = self.hold()
        self.assertEqual(outcome.status, RK.HOLD_CONFIRMED)
        self.assertEqual(self.status_of(outcome.hold_id), RK.HOLD_CONFIRMED)
        self.assertEqual(outcome.expires_at, "2026-09-21T12:00:00+00:00")  # 720 minutes

    def test_a_hold_stores_the_property_timezone_with_its_expiry(self):
        outcome = self.hold()
        self.assertEqual(self.hold_row(outcome.hold_id)["expires_timezone"], "Asia/Makassar")

    def test_a_hold_in_a_utc_property_stores_utc(self):
        self.kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1", timezone="UTC")
        outcome = self.hold()
        self.assertEqual(self.hold_row(outcome.hold_id)["expires_timezone"], "UTC")

    def test_a_second_hold_on_the_same_slot_is_refused(self):
        self.hold()
        with self.assertRaises(DomainError) as ctx:
            self.hold(key="idem_hold_0002")
        self.assertEqual(ctx.exception.code, "SLOT_ALREADY_HELD")

    def test_the_partial_index_refuses_a_second_row_at_the_database_level(self):
        self.hold()
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute(
                "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights,"
                " status, guest_ref, policy_version_id, evidence_id, idempotency_key, created_at,"
                " expires_at, expires_timezone, extended_count)"
                " VALUES ('hold_x', 'unit_1', 'prop_1', '2026-10-01', '2026-10-03', 2, ?,"
                " 'guest_abc123', 'policy_v1', ?, 'idem_hold_0009', ?, ?, 'Asia/Makassar', 0)",
                (RK.HOLD_CONFIRMED, self.evidence(), NOW, NOW),
            )


class TestExpiryIsNotAFact(HoldTestCase):
    """§7: an expired supplier hold is pending verification, never auto-released."""

    def test_the_sweep_marks_the_hold_pending_verification(self):
        hold_id = self.hold().hold_id
        swept = self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        self.assertEqual(swept, (hold_id,))
        self.assertEqual(self.status_of(hold_id), RK.HOLD_EXPIRED_PENDING_VERIFICATION)

    def test_expire_hold_marks_the_same_state(self):
        hold_id = self.hold().hold_id
        self.assertEqual(self.kernel.expire_hold(hold_id), hold_id)
        self.assertEqual(self.status_of(hold_id), RK.HOLD_EXPIRED_PENDING_VERIFICATION)
        self.assertEqual(self.status_of(hold_id), RK.HOLD_EXPIRED_PENDING_VERIFICATION)

    def test_the_sweep_records_a_closing_time_and_reason(self):
        hold_id = self.hold().hold_id
        self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        row = self.hold_row(hold_id)
        self.assertEqual(row["closed_at"], "2026-09-21T13:00:00+00:00")
        self.assertEqual(row["close_reason"], "ttl_expired")

    def test_the_sweep_keeps_the_expiry_timezone(self):
        hold_id = self.hold().hold_id
        self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        self.assertEqual(self.hold_row(hold_id)["expires_timezone"], "Asia/Makassar")

    def test_the_sweep_is_idempotent(self):
        hold_id = self.hold().hold_id
        moment = "2026-09-21T13:00:00+00:00"
        self.assertEqual(self.kernel.sweep_expired_holds(now=moment), (hold_id,))
        self.assertEqual(self.kernel.sweep_expired_holds(now=moment), ())

    def test_the_sweep_ignores_a_hold_that_is_still_in_force(self):
        self.hold()
        self.assertEqual(self.kernel.sweep_expired_holds(now="2026-09-21T11:59:00+00:00"), ())

    def test_an_unverified_expiry_still_occupies_its_slot(self):
        hold_id = self.hold().hold_id
        self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        occupancy = self.kernel.occupancy("unit_1", start="2026-10-01", end="2026-10-03")
        self.assertIn(hold_id, [row["id"] for row in occupancy])
        with self.assertRaises(DomainError) as ctx:
            self.hold(key="idem_hold_0002")
        self.assertEqual(ctx.exception.code, "SLOT_ALREADY_HELD")

    def test_verify_releases_the_slot_when_an_authoritative_source_confirms(self):
        hold_id = self.hold().hold_id
        self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        fresh = self.evidence(source_type="booking_engine", level="A", can_confirm=True)
        self.assertEqual(self.kernel.verify_expired_hold(hold_id, evidence_id=fresh), hold_id)
        self.assertEqual(self.status_of(hold_id), RK.HOLD_RELEASE_CONFIRMED)
        self.assertEqual(self.status_of(hold_id), RK.HOLD_RELEASE_CONFIRMED)
        # The positive control: the slot is free again, so a new hold may take it.
        self.assertEqual(self.hold(key="idem_hold_0003").status, RK.HOLD_CONFIRMED)

    def test_verify_refuses_a_source_that_is_not_authoritative(self):
        hold_id = self.hold().hold_id
        self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        written = self.evidence(source_type="manager_written", level="B")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.verify_expired_hold(hold_id, evidence_id=written)
        self.assertEqual(ctx.exception.code, "HOLD_VERIFICATION_REQUIRES_AUTHORITATIVE_EVIDENCE")

    def test_verify_refuses_stale_evidence(self):
        hold_id = self.hold().hold_id
        self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        old = self.evidence(source_type="property_api", level="A", captured_at=STALE)
        with self.assertRaises(DomainError) as ctx:
            self.kernel.verify_expired_hold(hold_id, evidence_id=old)
        self.assertEqual(ctx.exception.code, "HOLD_VERIFICATION_EVIDENCE_STALE")

    def test_verify_refuses_evidence_about_another_unit(self):
        hold_id = self.hold().hold_id
        self.kernel.upsert_unit(
            unit_id="unit_2", property_id="prop_1", name="Villa unit_2", base_capacity=2, max_guests=6
        )
        self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        elsewhere = self.evidence(unit_id="unit_2", source_type="property_api", level="A")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.verify_expired_hold(hold_id, evidence_id=elsewhere)
        self.assertEqual(ctx.exception.code, "HOLD_VERIFICATION_UNIT_MISMATCH")

    def test_verify_refuses_a_hold_that_is_not_pending_verification(self):
        hold_id = self.hold().hold_id
        fresh = self.evidence(source_type="property_api", level="A", can_confirm=True)
        with self.assertRaises(DomainError) as ctx:
            self.kernel.verify_expired_hold(hold_id, evidence_id=fresh)
        self.assertEqual(ctx.exception.code, "HOLD_NOT_PENDING_VERIFICATION")

    def test_verify_records_the_evidence_it_acted_on(self):
        hold_id = self.hold().hold_id
        self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        fresh = self.evidence(source_type="property_api", level="A", can_confirm=True)
        self.kernel.verify_expired_hold(hold_id, evidence_id=fresh)
        row = self.audit_rows(hold_id)[-1]
        self.assertEqual(row["action"], "hold.release_confirmed")
        self.assertEqual(row["evidence_ref"], fresh)


class TestHoldTransitions(HoldTestCase):
    def test_release_writes_release_confirmed(self):
        hold_id = self.hold().hold_id
        self.assertEqual(
            self.kernel.release_hold(hold_id, reason="guest changed dates"), hold_id
        )
        self.assertEqual(self.status_of(hold_id), RK.HOLD_RELEASE_CONFIRMED)
        self.assertEqual(self.status_of(hold_id), RK.HOLD_RELEASE_CONFIRMED)

    def test_a_released_hold_cannot_be_extended(self):
        hold_id = self.hold().hold_id
        self.kernel.release_hold(hold_id, reason="guest changed dates")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.request_extension(hold_id)
        self.assertEqual(ctx.exception.code, "HOLD_ILLEGAL_TRANSITION")

    def test_a_released_hold_frees_its_slot(self):
        hold_id = self.hold().hold_id
        self.kernel.release_hold(hold_id, reason="guest changed dates")
        self.assertEqual(self.hold(key="idem_hold_0004").status, RK.HOLD_CONFIRMED)

    def test_every_transition_is_audited_with_its_before_and_after(self):
        hold_id = self.hold().hold_id
        self.kernel.release_hold(hold_id, reason="guest changed dates", actor="user:owner-1")
        rows = self.audit_rows(hold_id)
        self.assertEqual([row["action"] for row in rows], ["hold.requested", "hold.released"])
        self.assertIn(RK.HOLD_CONFIRMED, rows[-1]["before_state"])
        self.assertIn(RK.HOLD_RELEASE_CONFIRMED, rows[-1]["after_state"])


class TestExtensionRequests(HoldTestCase):
    """§7 rule 2: a requested extension does not move the expiry."""

    def test_a_requested_extension_does_not_move_the_expiry(self):
        outcome = self.hold()
        before = self.hold_row(outcome.hold_id)["expires_at"]
        self.kernel.request_extension(outcome.hold_id)
        self.assertEqual(self.hold_row(outcome.hold_id)["expires_at"], before)

    def test_a_requested_extension_marks_the_hold_and_still_occupies_its_slot(self):
        hold_id = self.hold().hold_id
        self.kernel.request_extension(hold_id)
        self.assertEqual(self.status_of(hold_id), RK.HOLD_EXTENSION_REQUESTED)
        with self.assertRaises(DomainError) as ctx:
            self.hold(key="idem_hold_0002")
        self.assertEqual(ctx.exception.code, "SLOT_ALREADY_HELD")

    def test_the_request_records_its_minutes_and_its_timezone(self):
        hold_id = self.hold().hold_id
        extension_id = self.kernel.request_extension(hold_id, minutes=60)
        row = self.extension_row(extension_id)
        self.assertEqual(row["status"], "REQUESTED")
        self.assertEqual(row["requested_minutes"], 60)
        self.assertEqual(row["requested_timezone"], "Asia/Makassar")
        self.assertEqual(row["seq"], 1)

    def test_a_request_with_a_bogus_timezone_is_refused(self):
        hold_id = self.hold().hold_id
        with self.assertRaises(DomainError) as ctx:
            self.kernel.request_extension(hold_id, timezone="Nowhere/Fake")
        self.assertEqual(ctx.exception.code, "HOLD_INVALID_TIMEZONE")

    def test_confirming_the_extension_moves_the_expiry_by_the_requested_minutes(self):
        hold_id = self.hold().hold_id
        extension_id = self.kernel.request_extension(hold_id, minutes=60)
        self.assertEqual(self.kernel.confirm_extension(extension_id), "2026-09-21T13:00:00+00:00")
        row = self.hold_row(hold_id)
        self.assertEqual(row["expires_at"], "2026-09-21T13:00:00+00:00")
        self.assertEqual(row["extended_count"], 1)
        self.assertEqual(row["status"], RK.HOLD_CONFIRMED)

    def test_a_confirmed_extension_is_recorded_as_decided(self):
        hold_id = self.hold().hold_id
        extension_id = self.kernel.request_extension(hold_id, minutes=60)
        self.kernel.confirm_extension(extension_id, actor="user:owner-1")
        row = self.extension_row(extension_id)
        self.assertEqual(row["status"], "CONFIRMED")
        self.assertEqual(row["decided_at"], NOW)
        self.assertEqual(row["decided_by"], "user:owner-1")

    def test_rejecting_the_extension_leaves_the_expiry_where_it_was(self):
        outcome = self.hold()
        before = self.hold_row(outcome.hold_id)["expires_at"]
        extension_id = self.kernel.request_extension(outcome.hold_id)
        self.assertEqual(
            self.kernel.reject_extension(extension_id, reason="fully booked"), before
        )
        row = self.hold_row(outcome.hold_id)
        self.assertEqual(row["expires_at"], before)
        self.assertEqual(row["status"], RK.HOLD_CONFIRMED)
        self.assertEqual(row["extended_count"], 0)

    def test_an_answered_extension_cannot_be_answered_twice(self):
        hold_id = self.hold().hold_id
        extension_id = self.kernel.request_extension(hold_id)
        self.kernel.confirm_extension(extension_id)
        with self.assertRaises(DomainError) as ctx:
            self.kernel.reject_extension(extension_id, reason="too late")
        self.assertEqual(ctx.exception.code, "EXTENSION_ALREADY_DECIDED")

    def test_the_policy_extension_limit_is_enforced_at_the_request(self):
        hold_id = self.hold().hold_id
        for _ in range(2):  # the fixture policy allows two extensions
            self.kernel.confirm_extension(self.kernel.request_extension(hold_id))
        with self.assertRaises(DomainError) as ctx:
            self.kernel.request_extension(hold_id)
        self.assertEqual(ctx.exception.code, "HOLD_EXTENSION_LIMIT")

    def test_the_extension_flow_is_audited_with_both_expiries(self):
        hold_id = self.hold().hold_id
        extension_id = self.kernel.request_extension(hold_id, minutes=60)
        self.kernel.confirm_extension(extension_id)
        rows = self.audit_rows(hold_id)
        self.assertEqual(
            [row["action"] for row in rows],
            [
                "hold.requested",
                "hold.extension_requested",
                "hold.extension_confirmed",
                "hold.extension_answered",
            ],
        )
        self.assertIn("2026-09-21T12:00:00+00:00", rows[2]["before_state"])
        self.assertIn("2026-09-21T13:00:00+00:00", rows[2]["after_state"])

    def test_an_extension_whose_hold_expired_cannot_be_confirmed(self):
        hold_id = self.hold().hold_id
        extension_id = self.kernel.request_extension(hold_id, minutes=60)
        self.kernel.sweep_expired_holds(now="2026-09-21T13:00:00+00:00")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.confirm_extension(extension_id)
        self.assertEqual(ctx.exception.code, "HOLD_ILLEGAL_TRANSITION")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
