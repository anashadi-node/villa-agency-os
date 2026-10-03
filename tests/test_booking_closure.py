"""§30's booking closure gate — what must be true before a booking may be CLOSED.

Authority chain: ``src/booking_closure.py`` owns the gate (§30's seven conditions), the kernel owns
the writer that refuses on it, and ``src/schema/001_initial.sql`` owns the storage contract. The
fixtures here seed **the facts each check reads** — deliberately, because that is the module's job:
it judges recorded facts, not provenance.

The claims:

1. every §30 condition is asked, in the spec's order, and the answer is the blocker list;
2. the facts no module records yet (supplier settlement, the deposit's resolution) fail
   **closed** and say so by name instead of passing quietly; the required-documents list is
   tenant data declared through one policy key (``operations.booking.required_document_kinds``,
   ADR-0054) — an unpublished key fails closed like the rest, a published one is judged, a kind
   the platform cannot record fails closed by name, and an empty published list is a decision;
3. a settled invoice with no open reconciliation case satisfies the financial check, and an open
   dispute or a short payment reopens it;
4. an open case, a live guest follow-up or an open incident naming the booking blocks closure — and
   an incident that does not name it does not;
5. the kernel refuses to close with the blocker list in the refusal (never a bare no) and writes
   nothing, and writes the closure once the gate is satisfied — once.
"""

from __future__ import annotations

import unittest
from unittest import mock

import booking_closure as CLOSURE
import policy_engine as PE
from primitives import DomainError, FrozenClock
from test_reservation_kernel import KernelTestCase, NOW

AGENCY = "agency_default"
BOOKING = "book_close_1"

#: Deadlines the schema insists on (``ck_recon_due``, ``ck_cases_sla_window``): far enough ahead of
#: the frozen clock that the seeded facts stay valid whatever ``NOW`` is.
LATER = "2030-01-01T00:00:00+00:00"
LATER_AFTER = "2030-01-02T00:00:00+00:00"


class BookingClosureTestCase(KernelTestCase):
    """The kernel fixture plus the facts §30 reads: a finished booking and whatever it records."""

    # -- the booking -------------------------------------------------------- #

    def booked(
        self,
        *,
        booking_id: str = BOOKING,
        status: str = "COMPLETED",
        total_minor: int = 1_000_000,
        deposit_minor: int = 0,
    ) -> str:
        """One finished booking, seeded straight into the row §30 reads.

        No quote/hold dance: the gate judges the facts a booking carries, and a test of the gate
        wants to set those facts, not replay the path that produced them.
        """

        self.kernel.conn.execute(
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
            " status, guest_ref, adults, children, infants, currency, policy_version_id,"
            " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
            " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
            " updated_at, confirmed_at, cancelled_at)"
            " VALUES (?, 'prop_1', 'unit_1', '2026-10-01', '2026-10-03', 2, ?, 'guest_closure',"
            " 2, 0, 0, 'IDR', 'policy_v1', ?, NULL, NULL, ?, 0, ?, 'UNPAID', ?, 1, ?, ?, ?, NULL)",
            (
                booking_id,
                status,
                "0" * 64,
                total_minor,
                deposit_minor,
                f"idem_{booking_id}",
                NOW,
                NOW,
                NOW,
            ),
        )
        return booking_id

    # -- the money ---------------------------------------------------------- #

    def invoiced(
        self,
        *,
        booking_id: str = BOOKING,
        invoice_id: str = "inv_close_1",
        total_minor: int = 1_000_000,
        settled_minor: int = 0,
        payment_id: str = "pay_close_1",
    ) -> str:
        """An issued invoice for the booking and, when asked, a settled payment against it."""

        self.kernel.conn.execute(
            "INSERT INTO invoices (invoice_id, invoice_number, booking_id, policy_fingerprint,"
            " currency, subtotal_minor, tax_minor, service_minor, total_minor, amount_paid_minor,"
            " status, payment_status, issued_at, due_at, created_at)"
            " VALUES (?, ?, ?, ?, 'IDR', ?, 0, 0, ?, 0, 'ISSUED', 'UNPAID',"
            " '2026-10-03T10:00:00+00:00', '2026-10-10T10:00:00+00:00', ?)",
            (
                invoice_id,
                f"INV-{invoice_id}",
                booking_id,
                "0" * 64,
                total_minor,
                total_minor,
                NOW,
            ),
        )
        if settled_minor:
            self.settle(
                invoice_id=invoice_id,
                amount_minor=settled_minor,
                payment_id=payment_id,
            )
        return invoice_id

    def settle(
        self,
        *,
        invoice_id: str,
        amount_minor: int,
        payment_id: str,
        status: str = "SUCCEEDED",
    ) -> str:
        """A payment in the estate's own sense of settled: ``SUCCEEDED``, which carries a time."""

        self.kernel.conn.execute(
            "INSERT INTO payments (payment_id, invoice_id, provider, provider_intent_id, method,"
            " currency, amount_minor, status, created_at, settled_at)"
            " VALUES (?, ?, 'manual_bank', ?, 'BANK_TRANSFER', 'IDR', ?, ?, ?, ?)",
            (
                payment_id,
                invoice_id,
                f"intent_{payment_id}",
                amount_minor,
                status,
                NOW,
                None if status != "SUCCEEDED" else NOW,
            ),
        )
        return payment_id

    def dispute_open(self, *, invoice_id: str, recon_id: str = "recon_close_1") -> str:
        """The money is *disputed*: an OPEN reconciliation case is not a reconciled payment."""

        self.kernel.conn.execute(
            "INSERT INTO reconciliation_cases (recon_id, kind, invoice_id, detected_at, due_at,"
            " status, details, created_at)"
            " VALUES (?, 'AMOUNT_MISMATCH', ?, ?, ?, 'OPEN', '{}', ?)",
            (recon_id, invoice_id, NOW, LATER, NOW),
        )
        return recon_id

    # -- the guest's open matters ------------------------------------------- #

    def case_open(self, *, booking_id: str = BOOKING, case_id: str = "case_close_1") -> str:
        """An open case for the booking: an open guest matter, whatever its channel."""

        self.kernel.conn.execute(
            "INSERT INTO cases (case_id, channel, guest_ref, status, priority, sla_policy,"
            " first_response_due_at, quote_due_at, idempotency_key, created_at, updated_at,"
            " booking_id) VALUES (?, 'website', 'guest_closure', 'BOOKED', 'NORMAL', 'sla_default',"
            " ?, ?, ?, ?, ?, ?)",
            (case_id, LATER, LATER_AFTER, f"idem_{case_id}", NOW, NOW, booking_id),
        )
        return case_id

    def case_close(self, *, case_id: str = "case_close_1") -> None:
        self.kernel.conn.execute(
            "UPDATE cases SET status = 'CLOSED_WON', closed_at = ?,"
            " close_reason = 'resolved with the guest' WHERE case_id = ?",
            (NOW, case_id),
        )

    def follow_up_live(self, *, booking_id: str = BOOKING, task_id: str = "task_close_1") -> str:
        """The work RES-W06/RES-W09 file for a guest: live until a person finishes it."""

        self.kernel.conn.execute(
            "INSERT INTO ops_tasks (task_id, kind, booking_id, due_at, priority, status,"
            " idempotency_key, created_at, updated_at)"
            " VALUES (?, 'GUEST_FOLLOWUP', ?, ?, 'NORMAL', 'OPEN', ?, ?, ?)",
            (task_id, booking_id, NOW, f"idem_{task_id}", NOW, NOW),
        )
        return task_id

    def follow_up_done(self, *, task_id: str = "task_close_1") -> None:
        self.kernel.conn.execute(
            "UPDATE ops_tasks SET status = 'DONE', completed_at = ?, updated_at = ?"
            " WHERE task_id = ?",
            (NOW, NOW, task_id),
        )

    # -- incidents ---------------------------------------------------------- #

    def incident(
        self,
        *,
        incident_id: str = "inc_close_1",
        affected_refs: str = "[]",
        status: str = "OPEN",
    ) -> str:
        self.kernel.conn.execute(
            "INSERT INTO incidents (incident_id, severity, title, detected_at, detected_by,"
            " source, playbook, affected_refs, status, created_at)"
            " VALUES (?, 'SEV3', 'guest reported a broken pump', ?, 'user:ops-1', 'guest_report',"
            " 'standard-escalation', ?, ?, ?)",
            (incident_id, NOW, affected_refs, status, NOW),
        )
        return incident_id

    # -- documents ---------------------------------------------------------- #

    def document(self, *, booking_id: str = BOOKING, kind: str = "CONFIRMATION") -> str:
        self.kernel.conn.execute(
            "INSERT INTO documents (document_id, document_key, kind, version, format, booking_id,"
            " content_class, redaction_profile, storage_ref, body_sha256, status, generated_by,"
            " generated_at, created_at) VALUES ('doc_close_1', 'booking-confirmation', ?, 1,"
            " 'HTML', ?, 'P3', 'guest-default', 'obj/guest-default/doc_close_1.html', ?,"
            " 'GENERATED', 'user:ops-1', ?, ?)",
            (kind, booking_id, "a" * 64, NOW, NOW),
        )
        return "doc_close_1"

    # -- reads -------------------------------------------------------------- #

    def verdict(self, booking_id: str = BOOKING):
        return CLOSURE.evaluate(self.kernel.conn, booking_id=booking_id, agency_id=AGENCY)

    def check(self, booking_id: str, name: str) -> CLOSURE.ClosureCheck:
        by_name = {check.name: check for check in self.verdict(booking_id).checks}
        return by_name[name]

    def status_of(self, booking_id: str = BOOKING) -> str:
        return str(self.kernel.get_booking(booking_id)["status"])

    def count(self, table: str) -> int:
        row = self.kernel.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])


class TestClosureGate(BookingClosureTestCase):
    """The evaluator: §30's questions, answered from recorded facts only."""

    def test_the_gate_asks_every_condition_in_spec_order_and_names_the_blockers(self):
        """Nothing but a finished booking: the answer is §30's blocker list, in §30's order."""

        booking = self.booked()

        verdict = self.verdict(booking)

        self.assertFalse(verdict.ready)
        self.assertEqual(verdict.status, "COMPLETED")
        self.assertEqual(tuple(check.name for check in verdict.checks), CLOSURE.CHECKS)
        self.assertEqual(len(verdict.checks), 7)
        # §30's own order, and only the conditions this platform cannot satisfy yet.
        self.assertEqual(
            verdict.blockers,
            (
                CLOSURE.CHECK_FINANCIAL,
                CLOSURE.CHECK_SUPPLIER,
                CLOSURE.CHECK_DOCUMENTS,
            ),
        )
        by_name = {check.name: check for check in verdict.checks}
        for name in (
            CLOSURE.CHECK_CHECKED_OUT,
            CLOSURE.CHECK_GUEST_ISSUES,
            CLOSURE.CHECK_DEPOSIT,
            CLOSURE.CHECK_INCIDENTS,
        ):
            with self.subTest(name=name):
                self.assertTrue(by_name[name].ok, by_name[name].detail)
        # The verdict is data a UI can render, blockers included.
        self.assertEqual(verdict.as_dict()["blockers"], list(verdict.blockers))

    def test_the_facts_no_module_owns_block_the_closure_and_say_so_by_name(self):
        """A gate that guessed would close a booking on a fact nobody recorded."""

        booking = self.booked()

        supplier = self.check(booking, CLOSURE.CHECK_SUPPLIER)
        self.assertFalse(supplier.ok)
        self.assertEqual(supplier.detail, CLOSURE.SUPPLIER_GAP)
        self.assertIn("supplier_route", supplier.detail)

        documents = self.check(booking, CLOSURE.CHECK_DOCUMENTS)
        self.assertFalse(documents.ok)
        self.assertIn(CLOSURE.DOCUMENTS_GAP, documents.detail)
        self.assertIn("recorded for this booking: none", documents.detail)

        self.document(booking_id=booking, kind="RECEIPT")
        documents = self.check(booking, CLOSURE.CHECK_DOCUMENTS)
        self.assertIn("recorded for this booking: RECEIPT", documents.detail)
        self.assertFalse(documents.ok, "no policy declares the required kinds, so this fails closed")

    def test_a_settled_invoice_satisfies_the_financial_check_until_the_money_is_disputed(self):
        booking = self.booked()
        invoice = self.invoiced(booking_id=booking, total_minor=1_000_000, settled_minor=1_000_000)

        financial = self.check(booking, CLOSURE.CHECK_FINANCIAL)
        self.assertTrue(financial.ok, financial.detail)
        self.assertIn("settled 1000000 of 1000000 minor", financial.detail)
        self.assertIn("open reconciliation cases: 0", financial.detail)

        self.dispute_open(invoice_id=invoice)
        financial = self.check(booking, CLOSURE.CHECK_FINANCIAL)
        self.assertFalse(financial.ok)
        self.assertIn("open reconciliation cases: 1", financial.detail)

    def test_a_short_payment_or_no_invoice_at_all_is_not_settled(self):
        partial = self.booked(booking_id="book_close_2")
        self.invoiced(
            booking_id=partial,
            invoice_id="inv_close_2",
            total_minor=1_000_000,
            settled_minor=500_000,
            payment_id="pay_close_2",
        )
        financial = self.check(partial, CLOSURE.CHECK_FINANCIAL)
        self.assertFalse(financial.ok)
        self.assertIn("settled 500000 of 1000000 minor", financial.detail)

        uninvoiced = self.booked(booking_id="book_close_3")
        self.assertIn(
            "no issued invoice covers this booking",
            self.check(uninvoiced, CLOSURE.CHECK_FINANCIAL).detail,
        )

        # A booking that totals nothing needs no invoice, and that is not a blocker.
        free = self.booked(booking_id="book_close_4", total_minor=0)
        self.assertTrue(self.check(free, CLOSURE.CHECK_FINANCIAL).ok)

    def test_an_open_guest_matter_a_live_follow_up_or_an_open_incident_blocks_closure(self):
        booking = self.booked()
        self.case_open(booking_id=booking)
        guests = self.check(booking, CLOSURE.CHECK_GUEST_ISSUES)
        self.assertFalse(guests.ok)
        self.assertIn("open cases: 1", guests.detail)

        self.case_close()
        self.follow_up_live(booking_id=booking)
        guests = self.check(booking, CLOSURE.CHECK_GUEST_ISSUES)
        self.assertFalse(guests.ok)
        self.assertIn("live guest follow-ups: 1", guests.detail)

        self.follow_up_done()
        self.assertTrue(self.check(booking, CLOSURE.CHECK_GUEST_ISSUES).ok)

        self.incident(affected_refs=f'["{booking}", "case_close_1"]')
        incidents = self.check(booking, CLOSURE.CHECK_INCIDENTS)
        self.assertFalse(incidents.ok)
        self.assertIn("inc_close_1", incidents.detail)

    def test_an_incident_that_does_not_name_this_booking_does_not_block_it(self):
        """Attribution is by reference: an incident elsewhere is not this booking's incident."""

        booking = self.booked()
        self.incident(incident_id="inc_other", affected_refs='["book_someone_else"]')
        self.incident(incident_id="inc_unparsable", affected_refs="not json")

        self.assertTrue(self.check(booking, CLOSURE.CHECK_INCIDENTS).ok)

        self.incident(incident_id="inc_ours", affected_refs=f'["{booking}"]')
        self.assertFalse(self.check(booking, CLOSURE.CHECK_INCIDENTS).ok)

    def test_a_deposit_on_hold_blocks_closure_until_someone_records_its_resolution(self):
        booking = self.booked(deposit_minor=5_000_000)

        deposit = self.check(booking, CLOSURE.CHECK_DEPOSIT)
        self.assertFalse(deposit.ok)
        self.assertIn("5000000 minor held", deposit.detail)
        self.assertIn("returned, forfeited or applied", deposit.detail)

        self.kernel.conn.execute(
            "UPDATE bookings SET security_deposit_minor = 0 WHERE booking_id = ?", (booking,)
        )
        self.assertTrue(self.check(booking, CLOSURE.CHECK_DEPOSIT).ok)

    # -- the required-documents policy key (ADR-0054) ------------------------ #

    def publish_documents(self, kinds):
        """The tenant declares its required document kinds through one policy revision."""

        return PE.PolicyResolver(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        ).publish(
            CLOSURE.REQUIRED_DOCUMENT_KINDS_KEY,
            list(kinds),
            effective_from="2026-09-20",
            source_doc_ref="ADR-0054: tenant declares the documents a closed booking needs",
            approved_by="owner_anas",
        )

    def test_a_published_list_the_booking_satisfies_passes_and_names_its_revision(self):
        booking = self.booked()
        self.document(booking_id=booking, kind="CONFIRMATION")
        revision = self.publish_documents(["CONFIRMATION"])

        check = self.check(booking, CLOSURE.CHECK_DOCUMENTS)
        self.assertTrue(check.ok)
        self.assertIn(revision.policy_revision_id, check.detail)
        verdict = self.verdict(booking)
        self.assertNotIn(CLOSURE.CHECK_DOCUMENTS, verdict.blockers)

    def test_a_published_list_with_an_unrecorded_kind_names_it(self):
        booking = self.booked()
        self.document(booking_id=booking, kind="CONFIRMATION")
        self.publish_documents(["CONFIRMATION", "INVOICE"])

        check = self.check(booking, CLOSURE.CHECK_DOCUMENTS)
        self.assertFalse(check.ok)
        self.assertIn("INVOICE", check.detail)
        self.assertIn("recorded so far: CONFIRMATION", check.detail)
        self.assertIn(CLOSURE.CHECK_DOCUMENTS, self.verdict(booking).blockers)

    def test_a_kind_the_platform_cannot_record_fails_closed_by_name(self):
        booking = self.booked()
        self.publish_documents(["PASSPORT_SCAN"])

        check = self.check(booking, CLOSURE.CHECK_DOCUMENTS)
        self.assertFalse(check.ok)
        self.assertIn("PASSPORT_SCAN", check.detail)
        self.assertIn("cannot record", check.detail)

    def test_an_empty_published_list_is_a_decision_not_silence(self):
        booking = self.booked()
        self.publish_documents([])

        check = self.check(booking, CLOSURE.CHECK_DOCUMENTS)
        self.assertTrue(check.ok)
        self.assertIn("requires no document kinds", check.detail)

    def test_the_gate_refuses_a_booking_it_cannot_find(self):
        with self.assertRaises(CLOSURE.BookingNotFound) as raised:
            self.verdict("book_never_existed")
        self.assertEqual(raised.exception.code, "BOOKING_NOT_FOUND")
        self.assertEqual(raised.exception.details["booking_id"], "book_never_existed")


class TestClosureWriter(BookingClosureTestCase):
    """The kernel: it refuses with the list, and it closes once — nothing in between."""

    def test_the_kernel_refuses_to_close_and_hands_back_the_blocker_list(self):
        booking = self.booked()

        with self.assertRaises(DomainError) as raised:
            self.kernel.close_booking(booking_id=booking, command_id="cmd_close_refused")

        self.assertEqual(raised.exception.code, "BOOKING_NOT_CLOSABLE")
        self.assertEqual(raised.exception.details["status"], "COMPLETED")
        self.assertEqual(
            raised.exception.details["blockers"],
            [CLOSURE.CHECK_FINANCIAL, CLOSURE.CHECK_SUPPLIER, CLOSURE.CHECK_DOCUMENTS],
        )
        # Refusals write nothing.
        self.assertEqual(self.status_of(booking), "COMPLETED")
        self.assertEqual(self.count("booking_state_events"), 0)
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id = ? AND action = 'booking.closed'",
                (booking,),
            ).fetchone()[0],
            0,
        )

    def test_the_kernel_writes_the_closure_once_the_gate_is_satisfied(self):
        """The gate's verdict is injected: what is under test here is the writer, not §30 again."""

        booking = self.booked()
        ready = CLOSURE.ClosureVerdict(
            booking_id=booking,
            status="COMPLETED",
            ready=True,
            checks=(),
            blockers=(),
        )

        with mock.patch.object(CLOSURE, "evaluate", return_value=ready):
            closed = self.kernel.close_booking(
                booking_id=booking,
                command_id="cmd_close_ok",
                reason="every recorded condition is satisfied",
                actor="user:owner-1",
                actor_role="owner",
            )

        self.assertEqual(closed, booking)
        self.assertEqual(self.status_of(booking), "CLOSED")
        events = self.kernel.conn.execute(
            "SELECT from_state, to_state, command, reason FROM booking_state_events"
        ).fetchall()
        self.assertEqual(
            [(row["from_state"], row["to_state"], row["command"]) for row in events],
            [("COMPLETED", "CLOSED", "close_booking")],
        )
        self.assertEqual(events[0]["reason"], "every recorded condition is satisfied")
        actions = {
            str(row["action"])
            for row in self.kernel.conn.execute(
                "SELECT action FROM audit_events WHERE entity_id = ?", (booking,)
            ).fetchall()
        }
        self.assertIn("booking.closed", actions)

        # Closing twice is a no-op: no second event, no second audit line.
        again = self.kernel.close_booking(booking_id=booking, command_id="cmd_close_again")
        self.assertEqual(again, booking)
        self.assertEqual(self.count("booking_state_events"), 1)

    def test_closing_a_booking_that_never_existed_is_refused(self):
        with self.assertRaises(DomainError) as raised:
            self.kernel.close_booking(booking_id="book_never_existed", command_id="cmd_close_ghost")
        self.assertEqual(raised.exception.code, "BOOKING_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
