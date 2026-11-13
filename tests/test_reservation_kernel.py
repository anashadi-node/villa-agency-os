"""Tests for ``src/reservation_kernel.py`` — the no-double-booking guarantee.

The suite is written the way the guarantee is stated: for every way a second
party could try to occupy an already-taken slot, assert the second attempt is
refused and that nothing was written.
"""

from __future__ import annotations

import sqlite3
import unittest

import policy_engine as PE
import quote_calculator as QC
import reservation_kernel as RK
from booking_support import confirm_through_payment
from primitives import DomainError, FrozenClock

NOW = "2026-09-21T00:00:00+00:00"


def make_policy(policy_version_id="policy_v1", unit_scope="property", version=1, **rule_overrides):
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
    rules.update(rule_overrides)
    return PE.PolicyVersion(
        policy_version_id=policy_version_id,
        property_id="prop_1",
        unit_scope=unit_scope,
        version=version,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        rules=PE.PolicyRules(**rules),
        created_at=NOW,
        source_doc_ref="policy/2026-public.pdf",
        approved_by="user:owner-1",
    )


def make_rate_card():
    return QC.RateCard(
        rate_card_id="card_v1",
        property_id="prop_1",
        unit_scope="property",
        name="Public 2026",
        version=1,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        currency="IDR",
        seasons=(
            QC.Season(
                season_id="season_regular",
                name="Regular",
                start_date="2026-01-01",
                end_date="2026-12-31",
                nightly_rate_minor=1_500_000,
            ),
        ),
        source_doc_ref="rates/2026-public.pdf",
        approved_by="user:owner-1",
    )


def make_request(check_in="2026-10-01", check_out="2026-10-03", **overrides):
    base = dict(
        quote_date="2026-09-21",
        check_in=check_in,
        check_out=check_out,
        adults=2,
        children=0,
        infants=0,
        extras=(),
        discount_minor=0,
    )
    base.update(overrides)
    return QC.QuoteRequest(**base)


class KernelTestCase(unittest.TestCase):
    """Shared fixture: one property, two units, one policy, one rate card.

    ``db_path`` is a class attribute so a subclass can point the same fixture at a
    file-backed database. A file is what makes a *second writer* possible: two
    connections to one database are the only way to exercise the overlap guarantee
    under real concurrency (``:memory:`` is private per connection).
    """

    db_path: str = ":memory:"

    def setUp(self):
        self.kernel = RK.ReservationKernel.open(
            self.db_path,
            clock=FrozenClock.at(NOW),
            ids=__import__("primitives").IdFactory(deterministic=True, seed="t"),
        )
        self.addCleanup(self.kernel.close)
        self.kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1")
        for unit_id in ("unit_1", "unit_2"):
            self.kernel.upsert_unit(
                unit_id=unit_id,
                property_id="prop_1",
                name=f"Villa {unit_id}",
                base_capacity=2,
                max_guests=6,
            )
        self.policy = make_policy()
        self.kernel.insert_policy_version(self.policy.as_row())
        self.rate_card = make_rate_card()
        self.kernel.insert_rate_card(
            {
                "rate_card_id": self.rate_card.rate_card_id,
                "property_id": "prop_1",
                "unit_scope": "property",
                "name": self.rate_card.name,
                "version": 1,
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "currency": "IDR",
                "weekly_discount_pct": "0",
                "monthly_discount_pct": "0",
                "source_doc_ref": "rates/2026-public.pdf",
                "approved_by": "user:owner-1",
                "created_at": NOW,
            },
            [
                {
                    "season_id": "season_regular",
                    "rate_card_id": "card_v1",
                    "name": "Regular",
                    "start_date": "2026-01-01",
                    "end_date": "2026-12-31",
                    "nightly_rate_minor": 1_500_000,
                    "min_stay_nights": 1,
                    "priority": 0,
                    "created_at": NOW,
                }
            ],
        )

    # -- helpers ----------------------------------------------------------- #
    def quote(self, unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03", quote_id="quote_t1"):
        return QC.calculate_quote(
            make_request(check_in=check_in, check_out=check_out),
            self.policy,
            self.rate_card,
            QC.UnitFacts(unit_id=unit_id, property_id="prop_1", base_capacity=2, max_guests=6),
            quote_id=quote_id,
            issued_at=NOW,
        )

    def persist_quote(self, **kwargs):
        """Persist the quote the way the platform does when a guest says yes.

        §9's commercial gate means a booking needs the *accepted* version of this quote, so the
        helper records one by default. ``accept=False`` builds the offer nobody has answered yet,
        which is what the refusal tests need.
        """
        accept = kwargs.pop("accept", True)
        quote = self.quote(**kwargs)
        self.kernel.persist_quote(quote)
        if accept:
            self.accept_version(quote)
        return quote

    def accept_version(self, quote, *, version=1, status="ACCEPTED", unit_id=None,
                       check_in=None, check_out=None):
        """Record the version the guest answered — the row `QuotationService.accept` writes.

        Only the version row matters at this layer: the kernel gates on there being an accepted
        version of *this quote for this stay*, while the three snapshots and the acceptance row
        are the quotation module's own richer record (its tests pin those).
        """
        self.kernel.conn.execute(
            "INSERT INTO quotation_versions (quotation_version_id, agency_id, property_id,"
            " unit_id, check_in, check_out, version, quote_id, status, created_by, created_at,"
            " sent_at) VALUES (?, 'agency_default', ?, ?, ?, ?, ?, ?, ?, 'user:owner-1', ?, ?)",
            (
                f"qv_{quote.quote_id}_{version}",
                quote.property_id,
                unit_id or quote.unit_id,
                check_in or quote.check_in,
                check_out or quote.check_out,
                version,
                quote.quote_id,
                status,
                NOW,
                NOW,
            ),
        )

    def hold(self, unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03",
             key="idem_hold_0001", quote_ref=None, policy_version_id="policy_v1", **kwargs):
        return self.kernel.request_hold(
            unit_id=unit_id,
            check_in=check_in,
            check_out=check_out,
            guest_ref="guest_abc123",
            policy_version_id=policy_version_id,
            idempotency_key=key,
            quote_ref=quote_ref,
            actor="user:owner-1",
            actor_role="owner",
            **kwargs,
        )

    def row_count(self, table):
        return self.kernel.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


class TestSchemaAndFixtures(KernelTestCase):
    def test_schema_version_is_applied(self):
        self.assertEqual(self.kernel.schema_version, "001_initial")

    def test_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute(
                "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
                " base_capacity, max_guests, status, created_at, updated_at)"
                " VALUES ('unit_x', 'prop_missing', 'x', 'room', 1, 1, 1, 2, 'active', ?, ?)",
                (NOW, NOW),
            )

    def test_nested_transaction_is_refused(self):
        with self.assertRaises(DomainError) as ctx:
            with self.kernel.transaction():
                with self.kernel.transaction():
                    pass  # pragma: no cover
        self.assertEqual(ctx.exception.code, "KERNEL_NESTED_TRANSACTION")


class TestHoldLifecycle(KernelTestCase):
    def test_hold_is_created_confirmed_with_the_policy_ttl(self):
        outcome = self.hold()
        self.assertEqual(outcome.status, RK.HOLD_CONFIRMED)
        # §7: the expiry carries the zone it was agreed in.
        self.assertEqual(self.kernel.get_hold(outcome.hold_id)["expires_timezone"], "Asia/Makassar")
        self.assertEqual(outcome.nights, 2)
        self.assertFalse(outcome.replayed)
        self.assertEqual(outcome.expires_at, "2026-09-21T12:00:00+00:00")  # 720 minutes
        self.assertEqual(self.row_count("holds"), 1)

    def test_hold_auto_records_evidence_that_authorised_it(self):
        outcome = self.hold()
        evidence = self.kernel.get_evidence(outcome.evidence_id)
        self.assertEqual(evidence["result"], "AVAILABLE")
        self.assertEqual(evidence["can_hold"], 1)

    def test_hold_is_audited_and_the_chain_verifies(self):
        outcome = self.hold()
        trail = self.kernel.audit_trail(entity_id=outcome.hold_id)
        self.assertEqual([row["action"] for row in trail], ["hold.requested"])
        self.assertTrue(self.kernel.verify_audit_chain())

    def test_release_frees_the_slot(self):
        hold = self.hold()
        self.kernel.release_hold(hold.hold_id, reason="guest changed dates")
        second = self.hold(key="idem_hold_0002")
        self.assertEqual(second.status, RK.HOLD_CONFIRMED)

    def test_releasing_twice_is_idempotent(self):
        hold = self.hold()
        self.kernel.release_hold(hold.hold_id, reason="first")
        self.assertEqual(self.kernel.release_hold(hold.hold_id, reason="second"), hold.hold_id)

    def test_release_requires_a_reason(self):
        hold = self.hold()
        with self.assertRaises(DomainError):
            self.kernel.release_hold(hold.hold_id, reason="   ")

    def test_expired_hold_can_no_longer_be_released(self):
        """§7: only an authoritative source may close an unverified expiry."""
        hold = self.hold()
        self.kernel.expire_hold(hold.hold_id)
        with self.assertRaises(DomainError) as ctx:
            self.kernel.release_hold(hold.hold_id, reason="too late")
        self.assertEqual(
            ctx.exception.code, "HOLD_VERIFICATION_REQUIRES_AUTHORITATIVE_EVIDENCE"
        )

    def test_sweep_expires_only_holds_past_their_ttl(self):
        first = self.hold(check_in="2026-10-01", check_out="2026-10-03", key="idem_a_0001")
        second = self.hold(check_in="2026-10-10", check_out="2026-10-12", key="idem_b_0001")
        self.kernel.clock.set("2026-09-21T06:00:00+00:00")
        self.assertEqual(self.kernel.sweep_expired_holds(), ())
        self.kernel.clock.set("2026-09-21T13:00:00+00:00")
        self.assertEqual(self.kernel.sweep_expired_holds(), (first.hold_id, second.hold_id))
        self.assertEqual(
            self.kernel.get_hold(first.hold_id)["status"],
            RK.HOLD_EXPIRED_PENDING_VERIFICATION,
        )
        self.assertEqual(self.kernel.get_hold(first.hold_id)["close_reason"], "ttl_expired")

    def test_swept_hold_keeps_the_slot_until_it_is_verified(self):
        """§7: an expiry the platform has not verified is not a release."""
        first = self.hold()
        self.kernel.clock.set("2026-09-21T13:00:00+00:00")
        self.kernel.sweep_expired_holds()
        with self.assertRaises(DomainError) as ctx:
            self.hold(key="idem_hold_0002")
        self.assertEqual(ctx.exception.code, "SLOT_ALREADY_HELD")
        self.assertEqual(
            self.kernel.get_hold(first.hold_id)["status"],
            RK.HOLD_EXPIRED_PENDING_VERIFICATION,
        )

    def test_extension_extends_the_expiry_and_counts(self):
        """§7: the request moves nothing; the confirmation moves the expiry."""
        hold = self.hold()
        before = self.kernel.get_hold(hold.hold_id)["expires_at"]
        extension_id = self.kernel.request_extension(hold.hold_id)
        self.assertEqual(self.kernel.get_hold(hold.hold_id)["expires_at"], before)
        new_expiry = self.kernel.confirm_extension(extension_id)
        self.assertEqual(new_expiry, "2026-09-22T00:00:00+00:00")
        self.assertEqual(self.kernel.get_hold(hold.hold_id)["extended_count"], 1)

    def test_extension_beyond_the_policy_limit_is_refused(self):
        hold = self.hold()
        for _ in range(2):
            self.kernel.confirm_extension(self.kernel.request_extension(hold.hold_id))
        with self.assertRaises(DomainError) as ctx:
            self.kernel.request_extension(hold.hold_id)
        self.assertEqual(ctx.exception.code, "HOLD_EXTENSION_LIMIT")

    def test_cannot_extend_an_expired_hold(self):
        hold = self.hold()
        self.kernel.clock.set("2026-09-21T13:00:00+00:00")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.request_extension(hold.hold_id)
        self.assertEqual(ctx.exception.code, "HOLD_ALREADY_EXPIRED")

    def test_cannot_extend_a_released_hold(self):
        hold = self.hold()
        self.kernel.release_hold(hold.hold_id, reason="done")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.request_extension(hold.hold_id)
        self.assertEqual(ctx.exception.code, "HOLD_ILLEGAL_TRANSITION")


class TestNoDoubleBooking(KernelTestCase):
    """Every shape of overlap must be refused."""

    def test_overlapping_hold_is_refused(self):
        self.hold(check_in="2026-10-01", check_out="2026-10-03")
        with self.assertRaises(DomainError) as ctx:
            self.hold(check_in="2026-10-02", check_out="2026-10-04", key="idem_hold_0002")
        self.assertEqual(ctx.exception.code, "SLOT_ALREADY_HELD")
        self.assertEqual(ctx.exception.details["conflicting_window"], ("2026-10-01", "2026-10-03"))
        self.assertEqual(self.row_count("holds"), 1)

    def test_identical_slot_is_refused(self):
        self.hold()
        with self.assertRaises(DomainError) as ctx:
            self.hold(key="idem_hold_0002")
        self.assertEqual(ctx.exception.code, "SLOT_ALREADY_HELD")

    def test_fully_enclosing_slot_is_refused(self):
        self.hold(check_in="2026-10-02", check_out="2026-10-04")
        with self.assertRaises(DomainError):
            self.hold(check_in="2026-10-01", check_out="2026-10-05", key="idem_hold_0002")

    def test_back_to_back_stays_are_allowed(self):
        self.hold(check_in="2026-10-01", check_out="2026-10-03")
        second = self.hold(check_in="2026-10-03", check_out="2026-10-05", key="idem_hold_0002")
        self.assertEqual(second.check_in, "2026-10-03")

    def test_other_units_are_unaffected(self):
        self.hold(unit_id="unit_1")
        self.assertEqual(self.hold(unit_id="unit_2", key="idem_hold_0002").unit_id, "unit_2")

    def test_failed_hold_leaves_no_trace_and_no_partial_evidence(self):
        self.hold()
        evidence_before = self.row_count("availability_evidence")
        with self.assertRaises(DomainError):
            self.hold(check_in="2026-10-02", check_out="2026-10-04", key="idem_hold_0002")
        self.assertEqual(self.row_count("availability_evidence"), evidence_before)
        self.assertEqual(self.row_count("audit_events"), 1)
        self.assertTrue(self.kernel.verify_audit_chain())

    def test_booking_blocks_a_later_hold_on_the_same_window(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        confirm_through_payment(self.kernel,
            hold_id=hold.hold_id,
            quote_id=quote.quote_id,
            idempotency_key="idem_book_0001",
            command_id="cmd_0001",
            adults=2,
        )
        with self.assertRaises(DomainError) as ctx:
            self.hold(check_in="2026-10-02", check_out="2026-10-04", key="idem_hold_0009")
        self.assertEqual(ctx.exception.code, "SLOT_ALREADY_BOOKED")

    def test_cancellation_under_review_still_blocks_a_new_hold(self):
        """ADR-0002 Opsi A: the dates stay locked until the decision lands.

        The blocking row is written straight into the table, so the refusal can
        only come from the kernel's own occupancy check — the partial unique index
        guards bookings against bookings, not bookings against holds.
        """
        cases = (
            ("CANCELLATION_REQUESTED", "2026-10-01", "2026-10-03", "2026-10-02", "2026-10-05"),
            ("CANCELLATION_REVIEW", "2026-11-01", "2026-11-03", "2026-11-02", "2026-11-05"),
            ("DISPUTED", "2026-12-01", "2026-12-03", "2026-12-02", "2026-12-05"),
        )
        for state, start, booking_end, hold_start, hold_end in cases:
            with self.subTest(state=state):
                self.kernel.conn.execute(
                    "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out,"
                    " nights, status, guest_ref, adults, children, infants, currency,"
                    " policy_version_id, policy_fingerprint, quote_ref, hold_ref, total_minor,"
                    " amount_paid_minor, security_deposit_minor, payment_status, idempotency_key,"
                    " version_lock, created_at, updated_at, confirmed_at, cancelled_at)"
                    " VALUES (?, 'prop_1', 'unit_1', ?, ?, 2, ?, 'guest_abc123', 2, 0, 0, 'IDR',"
                    " 'policy_v1', ?, NULL, NULL, 1000000, 0, 0, 'UNPAID', ?, 1, ?, ?, ?, NULL)",
                    (
                        f"book_review_{state.lower()}",
                        start,
                        booking_end,
                        state,
                        "0" * 64,
                        f"idem_review_{state.lower()}",
                        NOW,
                        NOW,
                        NOW,
                    ),
                )
                with self.assertRaises(DomainError) as ctx:
                    self.hold(check_in=hold_start, check_out=hold_end,
                              key=f"idem_hold_{state.lower()}")
                self.assertEqual(ctx.exception.code, "SLOT_ALREADY_BOOKED")

    def test_cancelled_booking_releases_the_slot(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        booking = confirm_through_payment(self.kernel,
            hold_id=hold.hold_id,
            quote_id=quote.quote_id,
            idempotency_key="idem_book_0001",
            command_id="cmd_0001",
            adults=2,
        )
        self.kernel.cancel_booking(booking_id=booking.booking_id, reason="guest cancelled", command_id="cmd_0002")
        self.assertEqual(
            self.hold(check_in="2026-10-02", check_out="2026-10-04", key="idem_hold_0009").status,
            RK.HOLD_CONFIRMED,
        )

    def test_occupancy_reports_holds_and_bookings(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        confirm_through_payment(self.kernel,
            hold_id=hold.hold_id,
            quote_id=quote.quote_id,
            idempotency_key="idem_book_0001",
            command_id="cmd_0001",
            adults=2,
        )
        occupied = self.kernel.occupancy("unit_1", start="2026-09-30", end="2026-10-05")
        self.assertEqual([row["kind"] for row in occupied], ["booking"])


class TestCommercialGate(KernelTestCase):
    """§9: a booking needs the version the guest accepted — for the stay it covers."""

    def test_a_booking_needs_the_version_the_guest_accepted(self):
        """An offer that was only sent is not a contract, and the refusal writes nothing."""

        quote = self.persist_quote(accept=False)
        hold = self.hold(quote_ref=quote.quote_id)

        with self.assertRaises(DomainError) as ctx:
            self.kernel.reserve_from_hold(
                hold_id=hold.hold_id,
                quote_id=quote.quote_id,
                idempotency_key="idem_gate_0001",
                command_id="cmd_gate_0001",
                adults=2,
            )
        self.assertEqual(ctx.exception.code, "CONFIRM_NOT_ACCEPTED")
        self.assertEqual(self.row_count("bookings"), 0)
        self.assertEqual(self.kernel.get_hold(hold.hold_id)["status"], "CONFIRMED")

    def test_an_accepted_version_for_other_dates_does_not_book_these(self):
        """A yes for other dates is not a yes for these."""

        quote = self.persist_quote(accept=False)
        self.accept_version(quote, check_in="2027-01-01", check_out="2027-01-05")
        hold = self.hold(quote_ref=quote.quote_id)

        with self.assertRaises(DomainError) as ctx:
            self.kernel.reserve_from_hold(
                hold_id=hold.hold_id,
                quote_id=quote.quote_id,
                idempotency_key="idem_gate_0002",
                command_id="cmd_gate_0002",
                adults=2,
            )
        self.assertEqual(ctx.exception.code, "CONFIRM_ACCEPTED_OTHER_STAY")
        self.assertEqual(self.row_count("bookings"), 0)

    def test_the_accepted_version_is_the_only_difference_that_lets_it_through(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)

        booking = confirm_through_payment(self.kernel,
            hold_id=hold.hold_id,
            quote_id=quote.quote_id,
            idempotency_key="idem_gate_0003",
            command_id="cmd_gate_0003",
            adults=2,
        )
        self.assertEqual(booking.status, "CONFIRMED")
        self.assertEqual(self.row_count("bookings"), 1)


class TestEvidenceGates(KernelTestCase):
    def test_hold_without_can_hold_authorisation_is_refused(self):
        evidence_id = self.kernel.record_evidence(
            unit_id="unit_1", source_type="manager_written", evidence_level="B",
            result="available", can_quote=True, can_hold=False,
        )
        with self.assertRaises(DomainError) as ctx:
            self.hold(evidence_id=evidence_id)
        self.assertEqual(ctx.exception.code, "EVIDENCE_DOES_NOT_AUTHORISE_HOLD")

    def test_hold_on_not_available_evidence_is_refused(self):
        evidence_id = self.kernel.record_evidence(
            unit_id="unit_1",
            source_type="manager_written",
            evidence_level="B",
            result="NOT_AVAILABLE",
        )
        with self.assertRaises(DomainError) as ctx:
            self.hold(evidence_id=evidence_id)
        self.assertEqual(ctx.exception.code, "EVIDENCE_NOT_AVAILABLE")

    def test_stale_evidence_is_refused(self):
        evidence_id = self.kernel.record_evidence(
            unit_id="unit_1", source_type="ical", evidence_level="B", result="available",
            captured_at="2026-09-20T00:00:00+00:00",
            valid_until="2026-09-20T12:00:00+00:00",
        )
        with self.assertRaises(DomainError) as ctx:
            self.hold(evidence_id=evidence_id)
        self.assertEqual(ctx.exception.code, "EVIDENCE_STALE")

    def test_evidence_validity_window_must_point_forward(self):
        with self.assertRaises(DomainError) as ctx:
            self.kernel.record_evidence(
                unit_id="unit_1", source_type="ical", evidence_level="B", result="available",
                captured_at="2026-09-21T00:00:00+00:00",
                valid_until="2026-09-21T00:00:00+00:00",
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID_WINDOW")

    def test_evidence_ttl_defaults_to_a_bounded_window(self):
        evidence_id = self.kernel.record_evidence(
            unit_id="unit_1", source_type="ical", evidence_level="B", result="AVAILABLE"
        )
        row = self.kernel.get_evidence(evidence_id)
        self.assertEqual(row["captured_at"], NOW)
        self.assertEqual(row["valid_until"], "2026-09-22T00:00:00+00:00")

    def test_invalid_source_type_is_refused(self):
        with self.assertRaises(DomainError) as ctx:
            self.kernel.record_evidence(
                unit_id="unit_1", source_type="guesswork", evidence_level="B", result="AVAILABLE"
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID_SOURCE")

    def test_evidence_from_another_unit_is_refused(self):
        evidence_id = self.kernel.record_evidence(
            unit_id="unit_2", source_type="manager_written", evidence_level="B", result="available"
        )
        with self.assertRaises(DomainError) as ctx:
            self.hold(evidence_id=evidence_id)
        self.assertEqual(ctx.exception.code, "EVIDENCE_UNIT_MISMATCH")

    def test_unknown_evidence_is_refused(self):
        with self.assertRaises(DomainError) as ctx:
            self.hold(evidence_id="evid_missing")
        self.assertEqual(ctx.exception.code, "EVIDENCE_NOT_FOUND")

    def test_confirm_authorisation_requires_level_a_or_b(self):
        with self.assertRaises(DomainError) as ctx:
            self.kernel.record_evidence(
                unit_id="unit_1", source_type="memory", evidence_level="C",
                result="available", can_confirm=True,
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_LEVEL_INSUFFICIENT")

    def test_invalid_result_is_refused(self):
        with self.assertRaises(DomainError) as ctx:
            self.kernel.record_evidence(
                unit_id="unit_1", source_type="ical", evidence_level="B", result="maybe"
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID_RESULT")


class TestIdempotency(KernelTestCase):
    def test_same_key_and_payload_replays_the_original_hold(self):
        first = self.hold()
        replay = self.hold()
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.hold_id, first.hold_id)
        self.assertEqual(self.row_count("holds"), 1)
        self.assertEqual(self.row_count("idempotency_keys"), 1)

    def test_same_key_with_a_different_payload_is_a_conflict(self):
        self.hold()
        with self.assertRaises(DomainError) as ctx:
            self.hold(check_in="2026-10-10", check_out="2026-10-12")
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_same_key_for_a_different_operation_is_a_conflict(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id, key="shared_key_1")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.reserve_from_hold(
                hold_id=hold.hold_id,
                quote_id=quote.quote_id,
                idempotency_key="shared_key_1",
                command_id="cmd_0001",
                adults=2,
            )
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_confirmation_replay_returns_the_same_booking(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        first = self.kernel.reserve_from_hold(
            hold_id=hold.hold_id, quote_id=quote.quote_id,
            idempotency_key="idem_book_0001", command_id="cmd_0001", adults=2,
        )
        replay = self.kernel.reserve_from_hold(
            hold_id=hold.hold_id, quote_id=quote.quote_id,
            idempotency_key="idem_book_0001", command_id="cmd_0001", adults=2,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.booking_id, first.booking_id)
        self.assertEqual(self.row_count("bookings"), 1)
        self.assertEqual(self.row_count("booking_state_events"), 1)


class TestConfirmation(KernelTestCase):
    def confirm(self, **overrides):
        quote = overrides.pop("quote", None) or self.persist_quote()
        hold = overrides.pop("hold", None) or self.hold(quote_ref=quote.quote_id)
        params = dict(
            hold_id=hold.hold_id,
            quote_id=quote.quote_id,
            idempotency_key="idem_book_0001",
            command_id="cmd_0001",
            adults=2,
        )
        params.update(overrides)
        return quote, confirm_through_payment(self.kernel, **params)

    def test_confirmation_creates_a_confirmed_booking_with_quote_money(self):
        quote, booking = self.confirm()
        self.assertEqual(booking.status, "CONFIRMED")
        self.assertEqual(booking.total_minor, quote.total.minor_units)
        self.assertEqual(booking.deposit_due_minor + booking.balance_due_minor, booking.total_minor)
        row = self.kernel.get_booking(booking.booking_id)
        self.assertEqual(row["payment_status"], "CLEARED_FULL")  # money arrived before CONFIRMED
        self.assertEqual(row["policy_fingerprint"], quote.policy_fingerprint)
        self.assertEqual(row["quote_ref"], quote.quote_id)
        self.assertIsNotNone(row["confirmed_at"])

    def test_confirmation_converts_the_hold(self):
        quote, booking = self.confirm()
        hold_row = self.kernel.get_hold(self.kernel.get_booking(booking.booking_id)["hold_ref"])
        self.assertEqual(hold_row["status"], RK.HOLD_CONVERTED)
        self.assertEqual(hold_row["close_reason"], "converted_to_booking")

    def test_confirmation_journals_the_state_change(self):
        quote, booking = self.confirm()
        events = self.kernel.state_events(booking.booking_id)
        self.assertEqual(
            [(e["seq"], e["from_state"], e["to_state"], e["command"]) for e in events],
            [
                (1, "HOLD_ACTIVE", "PAYMENT_PENDING", "reserve_booking"),
                (2, "PAYMENT_PENDING", "PAID_IN_FULL", "mark_paid"),
                (3, "PAID_IN_FULL", "CONFIRMED", "confirm_booking"),
            ],
        )
        self.assertEqual(events[0]["command_id"], "cmd_0001")

    def test_confirmation_is_audited_and_the_chain_verifies(self):
        self.confirm()
        actions = [row["action"] for row in self.kernel.audit_trail()]
        booking_actions = [a for a in actions if a.startswith(("hold.", "booking."))]
        self.assertEqual(
            booking_actions,
            ["hold.requested", "booking.reserved", "booking.paid", "hold.converted", "booking.confirmed"],
        )
        self.assertTrue(self.kernel.verify_audit_chain())

    def test_expired_hold_cannot_be_confirmed(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        self.kernel.clock.set("2026-09-21T13:00:00+00:00")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.reserve_from_hold(
                hold_id=hold.hold_id, quote_id=quote.quote_id,
                idempotency_key="idem_book_0001", command_id="cmd_0001", adults=2,
            )
        self.assertEqual(ctx.exception.code, "HOLD_ALREADY_EXPIRED")

    def test_expired_quote_cannot_be_confirmed(self):
        """A quote that lapsed while the hold still ran must not be confirmed."""
        short = make_policy(policy_version_id="policy_short", version=2, quote_valid_hours=1, hold_ttl_minutes=1440)
        self.kernel.insert_policy_version(short.as_row())
        quote = QC.calculate_quote(
            make_request(),
            short,
            self.rate_card,
            QC.UnitFacts(unit_id="unit_1", property_id="prop_1", base_capacity=2, max_guests=6),
            quote_id="quote_short",
            issued_at=NOW,
        )
        self.kernel.persist_quote(quote)
        hold = self.hold(quote_ref=quote.quote_id, policy_version_id="policy_short")
        self.kernel.clock.set("2026-09-21T06:00:00+00:00")  # quote valid 1h, hold valid 24h
        with self.assertRaises(DomainError) as ctx:
            self.kernel.reserve_from_hold(
                hold_id=hold.hold_id, quote_id=quote.quote_id,
                idempotency_key="idem_book_0001", command_id="cmd_0001", adults=2,
            )
        self.assertEqual(ctx.exception.code, "QUOTE_EXPIRED")

    def test_capacity_is_rechecked_at_confirmation(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        with self.assertRaises(DomainError) as ctx:
            self.kernel.reserve_from_hold(
                hold_id=hold.hold_id, quote_id=quote.quote_id,
                idempotency_key="idem_book_0001", command_id="cmd_0001", adults=7,
            )
        self.assertEqual(ctx.exception.code, "CAPACITY_EXCEEDED")

    def test_adults_are_required(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        with self.assertRaises(DomainError) as ctx:
            self.kernel.reserve_from_hold(
                hold_id=hold.hold_id, quote_id=quote.quote_id,
                idempotency_key="idem_book_0001", command_id="cmd_0001", adults=0,
            )
        self.assertEqual(ctx.exception.code, "GUESTS_INVALID")

    def test_payment_status_tracks_the_amount_paid(self):
        """The paid amount is the booking's mirror of verified settlements, never a caller value."""
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        booking = confirm_through_payment(self.kernel,
            hold_id=hold.hold_id, quote_id=quote.quote_id,
            idempotency_key="idem_book_0001", command_id="cmd_0001", adults=2,
        )
        self.assertEqual(self.kernel.get_booking(booking.booking_id)["payment_status"], "CLEARED_FULL")

    def test_quote_from_another_unit_is_refused(self):
        quote = self.persist_quote(unit_id="unit_2", quote_id="quote_u2")
        hold = self.hold(unit_id="unit_1")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.reserve_from_hold(
                hold_id=hold.hold_id, quote_id=quote.quote_id,
                idempotency_key="idem_book_0001", command_id="cmd_0001", adults=2,
            )
        self.assertEqual(ctx.exception.code, "QUOTE_UNIT_MISMATCH")

    def test_unknown_quote_is_refused(self):
        hold = self.hold()
        with self.assertRaises(DomainError) as ctx:
            self.kernel.reserve_from_hold(
                hold_id=hold.hold_id, quote_id="quote_missing",
                idempotency_key="idem_book_0001", command_id="cmd_0001", adults=2,
            )
        self.assertEqual(ctx.exception.code, "QUOTE_NOT_FOUND")


class TestHoldInputValidation(KernelTestCase):
    def test_unknown_unit_is_refused(self):
        with self.assertRaises(DomainError) as ctx:
            self.hold(unit_id="unit_missing")
        self.assertEqual(ctx.exception.code, "UNIT_NOT_FOUND")

    def test_unknown_policy_version_is_refused(self):
        with self.assertRaises(DomainError) as ctx:
            self.kernel.request_hold(
                unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03",
                guest_ref="guest_abc123", policy_version_id="policy_missing",
                idempotency_key="idem_hold_0001",
                actor="user:owner-1",
                actor_role="owner",
            )
        self.assertEqual(ctx.exception.code, "POLICY_NOT_FOUND")

    def test_policy_from_another_property_is_refused(self):
        self.kernel.upsert_property(property_id="prop_other", name="Other villa")
        other = make_policy(policy_version_id="policy_v9")
        object.__setattr__(other, "property_id", "prop_other")
        self.kernel.insert_policy_version(other.as_row())
        with self.assertRaises(DomainError) as ctx:
            self.kernel.request_hold(
                unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03",
                guest_ref="guest_abc123", policy_version_id="policy_v9",
                idempotency_key="idem_hold_0001",
                actor="user:owner-1",
                actor_role="owner",
            )
        self.assertEqual(ctx.exception.code, "POLICY_PROPERTY_MISMATCH")

    def test_quote_priced_with_another_policy_is_refused(self):
        quote = self.persist_quote()
        other = make_policy(policy_version_id="policy_v2", unit_scope="unit")
        self.kernel.insert_policy_version(other.as_row())
        with self.assertRaises(DomainError) as ctx:
            self.kernel.request_hold(
                unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03",
                guest_ref="guest_abc123", policy_version_id="policy_v2",
                idempotency_key="idem_hold_0001", quote_ref=quote.quote_id,
                actor="user:owner-1",
                actor_role="owner",
            )
        self.assertEqual(ctx.exception.code, "QUOTE_POLICY_MISMATCH")

    def test_hold_for_a_non_positive_stay_is_refused(self):
        with self.assertRaises(DomainError):
            self.hold(check_in="2026-10-03", check_out="2026-10-03")


class TestCancellation(KernelTestCase):
    def setUp(self):
        super().setUp()
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        self.booking = confirm_through_payment(self.kernel,
            hold_id=hold.hold_id, quote_id=quote.quote_id,
            idempotency_key="idem_book_0001", command_id="cmd_0001", adults=2,
        )

    def test_cancellation_sets_status_and_journals_the_event(self):
        self.kernel.cancel_booking(
            booking_id=self.booking.booking_id, reason="guest cancelled", command_id="cmd_0002"
        )
        row = self.kernel.get_booking(self.booking.booking_id)
        self.assertEqual(row["status"], "CANCELLED")
        self.assertIsNotNone(row["cancelled_at"])
        events = self.kernel.state_events(self.booking.booking_id)
        self.assertEqual(
            [event["to_state"] for event in events],
            ["PAYMENT_PENDING", "PAID_IN_FULL", "CONFIRMED", "CANCELLED"],
        )
        self.assertEqual(events[-1]["reason"], "guest cancelled")

    def test_cancelling_twice_is_a_no_op(self):
        self.kernel.cancel_booking(
            booking_id=self.booking.booking_id, reason="first", command_id="cmd_0002"
        )
        self.kernel.cancel_booking(
            booking_id=self.booking.booking_id, reason="second", command_id="cmd_0003"
        )
        self.assertEqual(len(self.kernel.state_events(self.booking.booking_id)), 4)

    def test_completed_booking_cannot_be_cancelled(self):
        self.kernel.conn.execute(
            "UPDATE bookings SET status = 'COMPLETED' WHERE booking_id = ?",
            (self.booking.booking_id,),
        )
        with self.assertRaises(DomainError) as ctx:
            self.kernel.cancel_booking(
                booking_id=self.booking.booking_id, reason="too late", command_id="cmd_0002"
            )
        self.assertEqual(ctx.exception.code, "BOOKING_NOT_CANCELLABLE")

    def test_reason_is_required(self):
        with self.assertRaises(DomainError):
            self.kernel.cancel_booking(
                booking_id=self.booking.booking_id, reason="  ", command_id="cmd_0002"
            )

    def test_injected_transition_gate_is_respected(self):
        kernel = RK.ReservationKernel.open(
            ":memory:",
            clock=FrozenClock.at(NOW),
            transition_gate=lambda frm, to, cmd: False,
        )
        self.addCleanup(kernel.close)
        kernel.upsert_property(property_id="prop_1", name="Villa")
        kernel.upsert_unit(unit_id="unit_1", property_id="prop_1", name="Unit", max_guests=4)
        kernel.insert_policy_version(make_policy().as_row())
        kernel.conn.execute(
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
            " status, guest_ref, adults, children, infants, currency, policy_version_id,"
            " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
            " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
            " updated_at, confirmed_at, cancelled_at)"
            " VALUES ('book_x', 'prop_1', 'unit_1', '2026-10-01', '2026-10-03', 2, 'CONFIRMED',"
            " 'guest_abc123', 2, 0, 0, 'IDR', 'policy_v1', ?, NULL, NULL, 1000000, 0, 0,"
            " 'UNPAID', 'idem_x_0001', 1, ?, ?, ?, NULL)",
            ("0" * 64, NOW, NOW, NOW),
        )
        with self.assertRaises(DomainError) as ctx:
            kernel.cancel_booking(booking_id="book_x", reason="x", command_id="cmd_0001")
        self.assertEqual(ctx.exception.code, "BOOKING_TRANSITION_REFUSED")


class TestAppendOnlyGuarantees(KernelTestCase):
    def test_audit_rows_cannot_be_updated(self):
        self.hold()
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute("UPDATE audit_events SET action = 'tampered'")

    def test_audit_rows_cannot_be_deleted(self):
        self.hold()
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute("DELETE FROM audit_events")

    def test_state_events_cannot_be_updated(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        booking = confirm_through_payment(self.kernel,
            hold_id=hold.hold_id, quote_id=quote.quote_id,
            idempotency_key="idem_book_0001", command_id="cmd_0001", adults=2,
        )
        self.assertTrue(booking.booking_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute("UPDATE booking_state_events SET to_state = 'X'")

    def test_policy_versions_cannot_be_updated_or_deleted(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute("UPDATE policy_versions SET deposit_pct = '99'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute("DELETE FROM policy_versions")

    def test_tampered_audit_chain_is_detected(self):
        self.hold()
        self.assertTrue(self.kernel.verify_audit_chain())
        self.kernel.conn.execute(
            "INSERT INTO audit_events (audit_event_id, seq, occurred_at, actor, actor_role, action,"
            " entity_type, entity_id, before_state, after_state, reason, policy_version_id,"
            " evidence_ref, tool, trace_id, prev_hash, hash)"
            " VALUES ('audit_fake', 99, ?, 'system:kernel', 'kernel', 'hold.requested', 'hold',"
            " 'hold_fake', NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (NOW, "0" * 64, "0" * 64),
        )
        self.assertFalse(self.kernel.verify_audit_chain())


class TestReadModels(KernelTestCase):
    def test_persisted_quote_round_trips(self):
        quote = self.persist_quote()
        row = self.kernel.conn.execute(
            "SELECT * FROM quotes WHERE quote_id = ?", (quote.quote_id,)
        ).fetchone()
        self.assertEqual(row["total_minor"], quote.total.minor_units)
        self.assertEqual(row["fingerprint"], quote.fingerprint)
        self.assertEqual(row["status"], "DRAFT")

    def test_unknown_hold_and_booking_lookups_raise(self):
        with self.assertRaises(DomainError) as ctx:
            self.kernel.get_hold("hold_missing")
        self.assertEqual(ctx.exception.code, "HOLD_NOT_FOUND")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.get_booking("book_missing")
        self.assertEqual(ctx.exception.code, "BOOKING_NOT_FOUND")

    def test_state_machine_constants_match_the_schema_enum(self):
        """``OCCUPYING_BOOKING_STATES`` must stay a subset of the CHECK values."""
        import schema_loader

        model = schema_loader.parse_schema_model("sqlite")
        allowed = {value.upper() for value in model.enums["bookings.status"]}
        self.assertTrue(set(RK.OCCUPYING_BOOKING_STATES) <= allowed)
        self.assertTrue(set(RK.BOOKING_STATES) <= allowed)

    def test_every_blocking_status_is_declared_in_the_schema(self):
        import schema_loader

        model = schema_loader.parse_schema_model("sqlite")
        index = model.indexes["uq_bookings_active_slot"]
        predicate = index.predicate or ""
        for status in RK.OCCUPYING_BOOKING_STATES:
            with self.subTest(status=status):
                self.assertIn(status, predicate.upper())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
