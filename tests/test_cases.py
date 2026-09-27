"""Tests for the guest-request lifecycle in ``src/cases.py``.

Authority chain: ``src/schema/001_initial.sql`` owns the storage contract for
``cases``, ``case_messages``, ``case_events`` and ``sla_breaches``; nothing here
mocks storage. Every test drives the real ``CaseService`` against an in-memory
SQLite database created by ``schema_loader.connect_sqlite`` (the same DDL that
PostgreSQL mirrors), with time from ``primitives.FrozenClock`` and identifiers
from a deterministic ``IdFactory`` so ``seq`` and ``command_id`` assertions stay
exact.

The seven mandatory invariants and the tests that pin them down:

1. one event row per status change, ``seq = max(seq) + 1``, unique
   ``command_id`` per case -> TestEventLogIntegrity.
2. transitions outside ``CASE_TRANSITIONS`` write nothing -> TestEventLogIntegrity.
3. ``CLOSED_*`` refuses ``transition()`` and ``record_inbound()`` -> TestClosedCases.
4. ``content_class`` controls ``body_redacted``; the hash always covers the raw
   body -> TestRedaction.
5. SLA due instants come from ``created_at`` + policy minutes; ``overdue()`` is
   strictly-after and skips answered cases -> TestSlaAndOverdue.
6. validation happens before any row is written -> TestOpenCaseValidation.
7. ``guest_ref`` is validated through the shared ref helper -> TestOpenCaseValidation.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import cases as C
import primitives as P
from schema_loader import connect_sqlite

START = "2026-09-21T08:00:00Z"
# The canonical form the storage layer keeps (``to_iso`` drops the ``Z``).
NOW_ISO = P.to_iso(P.parse_iso(START))

# Channel -> default SLA policy, and the policy timers they imply.
CHANNEL_SLA = {
    "website": "web_v1",
    "email": "email_v1",
    "whatsapp_business": "whatsapp_v1",
    "dashboard": "agent_v1",
    "phone": "agent_v1",
}

SLA_MINUTES = {
    "web_v1": (15, 240),
    "email_v1": (60, 480),
    "whatsapp_v1": (10, 240),
    "agent_v1": (5, 120),
}

PII_BODY = "Halo, email saya budi@example.com dan nomor telepon 081234567890"
PHONE_BODY = "silakan hubungi 081234567890"


def seed_booking(conn, booking_id):
    """Insert the parent chain a ``bookings`` row needs (property -> unit -> policy)."""
    conn.execute(
        "INSERT OR IGNORE INTO properties (property_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('prop_1', 'Villa Satu', 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE',"
        " 'ACTIVATED', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
        " base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES ('unit_1', 'prop_1', 'Unit Satu', 'entire_villa', 2, 2, 2, 4, 'ACTIVE',"
        " '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO policy_versions (policy_version_id, property_id, unit_scope,"
        " version, effective_from, effective_to, currency, deposit_pct, service_fee_pct,"
        " tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,"
        " max_hold_extensions, payload, fingerprint, source_doc_ref, approved_by, created_at)"
        " VALUES ('policy_v1', 'prop_1', 'property', 1, '2026-01-01', '2027-01-01', 'IDR', '30',"
        " '10', '5', '10', 48, 60, 1, '{}', ?, 'doc.pdf', 'ops_1',"
        " '2026-09-01T00:00:00+00:00')",
        ("0" * 64,),
    )
    windows = {"book_1": ("2026-10-01", "2026-10-03"), "book_2": ("2026-11-01", "2026-11-03")}
    check_in, check_out = windows[booking_id]
    conn.execute(
        "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
        " status, guest_ref, adults, children, infants, currency, policy_version_id,"
        " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
        " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
        " updated_at, confirmed_at, cancelled_at)"
        " VALUES (?, 'prop_1', 'unit_1', ?, ?, 2, 'CONFIRMED', 'guest_0001', 2, 0, 0, 'IDR',"
        " 'policy_v1', ?, 'quote_1', 'hold_1', 1500000, 0, 0, 'UNPAID', ?, 1, ?, ?, ?, NULL)",
        (booking_id, check_in, check_out, "0" * 64, "idem_" + booking_id, START, START, START),
    )
    return booking_id


class CaseTestBase(unittest.TestCase):
    """Shared rig: in-memory SQLite + frozen clock + deterministic ids."""

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.clock = P.FrozenClock(P.parse_iso(START))
        self.ids = P.IdFactory(deterministic=True, seed="cases")
        self.svc = C.CaseService(self.conn, clock=self.clock, ids=self.ids)

    # -- helpers ---------------------------------------------------------
    def open_case(self, **overrides) -> C.Case:
        kwargs = {
            "channel": "website",
            "guest_ref": "guest_0001",
            "content_class": "P1",
            "body": "Halo, apakah ada villa kosong akhir bulan?",
            "idempotency_key": "idem_case_0001",
        }
        kwargs.update(overrides)
        case, _message = self.svc.open_case(**kwargs)
        return case

    def counts(self) -> tuple[int, int, int]:
        row = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM cases), (SELECT COUNT(*) FROM case_messages),"
            " (SELECT COUNT(*) FROM case_events)"
        ).fetchone()
        return (int(row[0]), int(row[1]), int(row[2]))

    def event_rows(self, case_id: str) -> list[tuple]:
        return [
            tuple(row)
            for row in self.conn.execute(
                "SELECT seq, kind, from_status, to_status, command_id FROM case_events"
                " WHERE case_id = ? ORDER BY seq",
                (case_id,),
            ).fetchall()
        ]

    def command_rows(self, case_id: str, command_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM case_events WHERE case_id = ? AND command_id = ?",
            (case_id, command_id),
        ).fetchone()
        return int(row[0])

    def message_rows(self, case_id: str) -> list[tuple]:
        return [
            tuple(row)
            for row in self.conn.execute(
                "SELECT direction, channel, content_class, body_redacted, body_sha256,"
                " attachment_count, untrusted FROM case_messages WHERE case_id = ?"
                " ORDER BY case_message_id",
                (case_id,),
            ).fetchall()
        ]

    def refuse(self, code: str, func, *args, **kwargs) -> P.DomainError:
        """Assert ``func`` fails with a DomainError carrying exactly ``code``."""
        with self.assertRaises(P.DomainError) as caught:
            func(*args, **kwargs)
        exc = caught.exception
        self.assertEqual(exc.code, code, f"kode error salah: {exc.code} ({exc})")
        return exc

    def advance(self, **kwargs) -> None:
        self.clock.advance(**kwargs)

    def triage(self, case_id: str, prefix: str) -> None:
        """Drive NEW -> TRIAGED -> MATCHING with unique commands per case."""
        self.svc.transition(
            case_id, "TRIAGED", actor="agent_01", actor_role="agent", reason="triase awal",
            command_id=prefix + "_triase",
        )
        self.svc.transition(
            case_id, "MATCHING", actor="agent_01", actor_role="agent", reason="cari unit",
            command_id=prefix + "_match",
        )


class TestContractSurface(CaseTestBase):
    """The exported constants are a contract for later phases."""

    def test_status_and_channel_tuples_match_the_contract(self):
        self.assertEqual(
            C.CASE_STATUSES,
            (
                "NEW", "TRIAGED", "MATCHING", "AWAITING_EVIDENCE", "QUOTED", "HOLD_PENDING",
                "BOOKED", "ESCALATED", "CLOSED_WON", "CLOSED_LOST", "CLOSED_DUPLICATE",
            ),
        )
        self.assertEqual(
            set(C.CLOSED_STATUSES), {"CLOSED_WON", "CLOSED_LOST", "CLOSED_DUPLICATE"}
        )
        self.assertEqual(set(C.CLOSED_STATUSES) - set(C.CASE_STATUSES), set())
        self.assertEqual(set(C.TERMINAL_STATUSES), set(C.CLOSED_STATUSES))
        self.assertEqual(
            C.CASE_CHANNELS, ("website", "email", "whatsapp_business", "dashboard", "phone")
        )
        self.assertEqual(set(C.PRIORITIES), {"LOW", "NORMAL", "HIGH", "URGENT"})

    def test_sla_policies_expose_the_agreed_timers(self):
        for name, (first, quote) in SLA_MINUTES.items():
            with self.subTest(policy=name):
                policy = C.SLA_POLICIES[name]
                self.assertEqual(policy.name, name)
                self.assertEqual(policy.first_response_minutes, first)
                self.assertEqual(policy.quote_minutes, quote)

    def test_transition_table_covers_every_status_and_fences_closed_ones(self):
        self.assertEqual(set(C.CASE_TRANSITIONS), set(C.CASE_STATUSES))
        for status, targets in C.CASE_TRANSITIONS.items():
            with self.subTest(status=status):
                self.assertEqual(len(set(targets)), len(targets), "target duplikat")
                self.assertEqual(set(targets) - set(C.CASE_STATUSES), set())
                if status != "CLOSED_DUPLICATE":
                    # ``CLOSED_DUPLICATE`` is the one status a case may re-enter
                    # (a manager correcting a wrong duplicate verdict).
                    self.assertNotIn(status, targets)
        for closed in C.CLOSED_STATUSES:
            self.assertEqual(tuple(C.CASE_TRANSITIONS[closed]), ("CLOSED_DUPLICATE",))
        self.assertEqual(
            tuple(C.CASE_TRANSITIONS["ESCALATED"]),
            tuple(s for s in C.CASE_STATUSES if s != "ESCALATED"),
        )


class TestEventLogIntegrity(CaseTestBase):
    """Invariants 1 and 2: the event log is the single source of truth."""

    def test_opening_a_case_writes_one_message_and_one_created_event(self):
        case = self.open_case()
        self.assertEqual(self.counts(), (1, 1, 1))
        rows = self.event_rows(case.case_id)
        self.assertEqual([r[0] for r in rows], [1])
        self.assertEqual(rows[0][1:], ("CREATED", None, "NEW", "idem_case_0001"))
        self.assertEqual(case.status, "NEW")
        self.assertEqual(case.created_at, P.to_iso(self.clock.now()))

    def test_every_status_change_adds_exactly_one_event_with_the_next_seq(self):
        case = self.open_case()
        self.svc.transition(
            case.case_id, "TRIAGED", actor="agent_01", actor_role="agent", reason="triase",
            command_id="cmd_case_0101",
        )
        self.svc.transition(
            case.case_id, "MATCHING", actor="agent_01", actor_role="agent", reason="cocokkan",
            command_id="cmd_case_0102",
        )
        rows = self.event_rows(case.case_id)
        self.assertEqual([r[0] for r in rows], [1, 2, 3])
        self.assertEqual(self.counts(), (1, 1, 3))
        self.assertEqual(rows[1][1:], ("STATUS_CHANGED", "NEW", "TRIAGED", "cmd_case_0101"))
        self.assertEqual(
            rows[2][1:], ("STATUS_CHANGED", "TRIAGED", "MATCHING", "cmd_case_0102")
        )
        events = self.svc.timeline(case.case_id)
        self.assertEqual([e.seq for e in events], [1, 2, 3])
        self.assertEqual(events[1].actor, "agent_01")
        self.assertEqual(events[1].actor_role, "agent")
        self.assertEqual(events[1].reason, "triase")
        self.assertEqual(events[1].occurred_at, P.to_iso(self.clock.now()))
        self.assertEqual(self.svc.get(case.case_id).status, "MATCHING")

    def test_a_replayed_command_id_is_refused_and_writes_no_second_row(self):
        case = self.open_case()
        self.svc.transition(
            case.case_id, "TRIAGED", actor="agent_01", actor_role="agent", reason="triase",
            command_id="cmd_case_0201",
        )
        before = self.counts()
        self.assertEqual(self.command_rows(case.case_id, "cmd_case_0201"), 1)
        self.refuse(
            "DUPLICATE_COMMAND", self.svc.transition, case.case_id, "MATCHING",
            actor="agent_01", actor_role="agent", reason="ulang", command_id="cmd_case_0201",
        )
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.command_rows(case.case_id, "cmd_case_0201"), 1)
        self.assertEqual(self.svc.get(case.case_id).status, "TRIAGED")
        self.assertEqual([r[0] for r in self.event_rows(case.case_id)], [1, 2])

    def test_a_command_id_is_unique_per_case_and_reusable_across_cases(self):
        first = self.open_case(idempotency_key="idem_case_0301")
        second = self.open_case(guest_ref="guest_0002", idempotency_key="idem_case_0302")
        for case in (first, second):
            with self.subTest(case=case.case_id):
                self.svc.transition(
                    case.case_id, "TRIAGED", actor="agent_01", actor_role="agent",
                    reason="triase", command_id="cmd_shared_0001",
                )
                self.assertEqual(self.command_rows(case.case_id, "cmd_shared_0001"), 1)
                self.assertEqual(self.svc.get(case.case_id).status, "TRIAGED")

    def test_a_transition_outside_the_table_is_refused_and_writes_nothing(self):
        case = self.open_case()
        before = self.counts()
        self.refuse(
            "INVALID_CASE_TRANSITION", self.svc.transition, case.case_id, "BOOKED",
            actor="agent_01", actor_role="agent", reason="lompat", command_id="cmd_case_0401",
        )
        self.refuse(
            "INVALID_CASE_TRANSITION", self.svc.transition, case.case_id, "QUOTED",
            actor="agent_01", actor_role="agent", reason="lompat", command_id="cmd_case_0402",
        )
        self.refuse(
            "INVALID_CASE_TRANSITION", self.svc.transition, case.case_id, "NEW",
            actor="agent_01", actor_role="agent", reason="diam", command_id="cmd_case_0403",
        )
        self.refuse(
            "INVALID_CASE_TRANSITION", self.svc.transition, case.case_id, "REFUNDED",
            actor="agent_01", actor_role="agent", reason="ngawur", command_id="cmd_case_0404",
        )
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.svc.get(case.case_id).status, "NEW")

    def test_escalation_is_available_from_new_and_can_return_to_the_funnel(self):
        case = self.open_case()
        self.svc.transition(
            case.case_id, "ESCALATED", actor="agent_01", actor_role="agent",
            reason="komplain sensitif", command_id="cmd_case_0501",
        )
        self.assertEqual(self.svc.get(case.case_id).status, "ESCALATED")
        self.svc.transition(
            case.case_id, "TRIAGED", actor="manager_01", actor_role="manager",
            reason="ambil alih", command_id="cmd_case_0502",
        )
        self.assertEqual(self.svc.get(case.case_id).status, "TRIAGED")
        self.assertEqual(
            [(e.from_status, e.to_status) for e in self.svc.timeline(case.case_id)],
            [(None, "NEW"), ("NEW", "ESCALATED"), ("ESCALATED", "TRIAGED")],
        )


class TestClosedCases(CaseTestBase):
    """Invariant 3: closed cases are immutable except for the duplicate correction."""

    def test_closed_case_refuses_transition_and_inbound(self):
        case = self.open_case()
        self.svc.record_inbound(
            case.case_id, content_class="P1", body="masih ada?", idempotency_key="idem_case_0601"
        )
        closed = self.svc.close(
            case.case_id, outcome="LOST", reason="tamu tidak lanjut", actor="agent_01",
            command_id="cmd_case_0602",
        )
        self.assertEqual(closed.status, "CLOSED_LOST")
        self.assertEqual(closed.closed_at, P.to_iso(self.clock.now()))
        self.assertEqual(closed.close_reason, "tamu tidak lanjut")
        before = self.counts()
        self.refuse(
            "CASE_CLOSED", self.svc.transition, case.case_id, "ESCALATED", actor="agent_01",
            actor_role="agent", reason="hidupkan lagi", command_id="cmd_case_0603",
        )
        self.refuse(
            "CASE_CLOSED", self.svc.record_inbound, case.case_id, content_class="P1",
            body="halo", idempotency_key="idem_case_0604",
        )
        self.refuse(
            "CASE_CLOSED", self.svc.close, case.case_id, outcome="WON", reason="menang",
            actor="agent_01", command_id="cmd_case_0605",
        )
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.svc.get(case.case_id).status, "CLOSED_LOST")

    def test_closed_case_accepts_only_the_duplicate_correction(self):
        case = self.open_case()
        self.svc.close(
            case.case_id, outcome="LOST", reason="duplikat mungkin", actor="agent_01",
            command_id="cmd_case_0701",
        )
        before = self.counts()
        self.refuse(
            "CASE_CLOSED", self.svc.transition, case.case_id, "CLOSED_WON",
            actor="agent_01", actor_role="agent", reason="ubah pikiran", command_id="cmd_case_0702",
        )
        self.assertEqual(self.counts(), before)
        corrected = self.svc.transition(
            case.case_id, "CLOSED_DUPLICATE", actor="manager_01", actor_role="manager",
            reason="ternyata duplikat", command_id="cmd_case_0703",
        )
        self.assertEqual(corrected.status, "CLOSED_DUPLICATE")
        self.assertEqual(self.counts(), (before[0], before[1], before[2] + 1))
        self.assertEqual(self.event_rows(case.case_id)[-1][1:], (
            "STATUS_CHANGED", "CLOSED_LOST", "CLOSED_DUPLICATE", "cmd_case_0703",
        ))

    def test_a_booked_case_closes_as_won_and_then_refuses_everything(self):
        seed_booking(self.conn, "book_1")
        case = self.open_case()
        self.triage(case.case_id, "cmd_case_0801")
        self.svc.mark_quoted(case.case_id, actor="agent_01", command_id="cmd_case_0803")
        self.svc.transition(
            case.case_id, "HOLD_PENDING", actor="agent_01", actor_role="agent", reason="hold",
            command_id="cmd_case_0804",
        )
        self.svc.link_booking(
            case.case_id, "book_1", actor="agent_01", command_id="cmd_case_0805"
        )
        self.svc.transition(
            case.case_id, "BOOKED", actor="agent_01", actor_role="agent", reason="terbayar",
            command_id="cmd_case_0806",
        )
        won = self.svc.close(
            case.case_id, outcome="WON", reason="tamu bayar lunas", actor="agent_01",
            command_id="cmd_case_0807",
        )
        self.assertEqual(won.status, "CLOSED_WON")
        self.assertEqual(won.booking_id, "book_1")
        self.assertEqual(self.counts(), (1, 1, 8))
        # Invariant 3: a CLOSED_* case refuses status moves and inbound
        # messages, and its log stops growing.
        self.refuse(
            "CASE_CLOSED", self.svc.transition, case.case_id, "BOOKED",
            actor="agent_01", actor_role="agent", reason="buka lagi", command_id="cmd_case_0808",
        )
        self.refuse(
            "CASE_CLOSED", self.svc.close, case.case_id, outcome="LOST", reason="ubah pikiran",
            actor="agent_01", command_id="cmd_case_0809",
        )
        self.refuse(
            "CASE_CLOSED", self.svc.record_inbound, case.case_id, content_class="P1",
            body="halo, masih ada?", idempotency_key="idem_case_0810",
        )
        self.assertEqual(self.counts(), (1, 1, 8))
        # ... but the duplicate correction is the single exception.
        corrected = self.svc.transition(
            case.case_id, "CLOSED_DUPLICATE", actor="manager_01", actor_role="manager",
            reason="ternyata bukan penjualan ganda", command_id="cmd_case_0811",
        )
        self.assertEqual(corrected.status, "CLOSED_DUPLICATE")
        self.assertEqual(self.counts(), (1, 1, 9))
        self.refuse(
            "CASE_CLOSED", self.svc.transition, case.case_id, "CLOSED_WON",
            actor="manager_01", actor_role="manager", reason="batalkan koreksi",
            command_id="cmd_case_0812",
        )
        self.assertEqual(self.counts(), (1, 1, 9))


class TestRedaction(CaseTestBase):
    """Invariant 4: P3/P4 never store text; the hash always covers the raw body."""

    def test_p3_and_p4_bodies_are_never_stored_but_still_hashed(self):
        for index, content_class in enumerate(("P3", "P4")):
            with self.subTest(content_class=content_class):
                case = self.open_case(
                    content_class=content_class, body=PII_BODY,
                    idempotency_key="idem_case_090%d" % index,
                )
                message = self.svc.record_inbound(
                    case.case_id, content_class=content_class, body=PII_BODY,
                    idempotency_key="idem_case_091%d" % index,
                )
                self.assertIsNone(message.body_redacted)
                self.assertEqual(message.body_sha256, P.sha256_hex(PII_BODY))
                stored = self.message_rows(case.case_id)
                self.assertEqual(len(stored), 2)
                for row in stored:
                    self.assertEqual(row[2], content_class)
                    self.assertIsNone(row[3])
                    self.assertEqual(row[4], P.sha256_hex(PII_BODY))
                self.assertNotIn(PII_BODY, [row[3] for row in stored])

    def test_p2_body_is_redacted_and_still_hashed_from_the_raw_body(self):
        for index, body in enumerate((PII_BODY, PHONE_BODY)):
            with self.subTest(body=body):
                case = self.open_case(
                    content_class="P2", body=body,
                    idempotency_key="idem_case_100%d" % index,
                )
                self.assertEqual(case.status, "NEW")
                message = self.message_rows(case.case_id)[0]
                self.assertEqual(message[2], "P2")
                self.assertEqual(message[3], P.redact(body))
                self.assertNotEqual(message[3], body)
                self.assertNotIn("budi@example.com", message[3])
                self.assertNotIn("081234567890", message[3])
                self.assertEqual(message[4], P.sha256_hex(body))
                self.assertNotEqual(message[4], P.sha256_hex(P.redact(body)))

    def test_p0_and_p1_bodies_are_stored_verbatim_and_flagged_untrusted(self):
        for index, content_class in enumerate(("P0", "P1")):
            with self.subTest(content_class=content_class):
                case = self.open_case(
                    content_class=content_class, body=PII_BODY, attachments=2,
                    idempotency_key="idem_case_110%d" % index,
                )
                message = self.message_rows(case.case_id)[0]
                self.assertEqual(message[2], content_class)
                self.assertEqual(message[3], PII_BODY)
                self.assertEqual(message[4], P.sha256_hex(PII_BODY))
                self.assertEqual(message[5], 2)
                self.assertEqual(message[6], 1)


class TestSlaAndOverdue(CaseTestBase):
    """Invariant 5: due instants and the strictly-after overdue rule."""

    def test_default_policy_follows_the_channel_and_anchors_on_created_at(self):
        for index, (channel, policy_name) in enumerate(CHANNEL_SLA.items()):
            with self.subTest(channel=channel):
                case = self.open_case(
                    channel=channel, idempotency_key="idem_case_120%d" % index
                )
                first, quote = SLA_MINUTES[policy_name]
                created = P.parse_iso(case.created_at)
                self.assertEqual(case.sla_policy, policy_name)
                self.assertEqual(case.created_at, NOW_ISO)
                self.assertEqual(
                    case.first_response_due_at, P.to_iso(created + timedelta(minutes=first))
                )
                self.assertEqual(
                    case.quote_due_at, P.to_iso(created + timedelta(minutes=quote))
                )
                self.assertIsNone(case.first_responded_at)
                self.assertIsNone(case.quoted_at)

    def test_explicit_policy_overrides_the_channel_default(self):
        case = self.open_case(channel="website", sla_policy="email_v1")
        self.assertEqual(case.sla_policy, "email_v1")
        self.assertEqual(case.first_response_due_at, "2026-09-21T09:00:00+00:00")
        self.assertEqual(case.quote_due_at, "2026-09-21T16:00:00+00:00")

    def test_overdue_starts_strictly_after_the_due_instant(self):
        case = self.open_case(channel="whatsapp_business")
        self.assertEqual(case.first_response_due_at, "2026-09-21T08:10:00+00:00")
        self.assertEqual(case.quote_due_at, "2026-09-21T12:00:00+00:00")

        self.advance(minutes=10)
        self.assertEqual(self.clock.now(), P.parse_iso("2026-09-21T08:10:00Z"))
        self.assertEqual(self.svc.overdue(), [])
        self.assertEqual(self.svc.first_response_overdue(), [])
        self.assertEqual(self.svc.quote_overdue(), [])
        self.assertEqual(self.svc.overdue(now=P.parse_iso("2026-09-21T08:10:00Z")), [])

        self.advance(seconds=1)
        self.assertEqual([c.case_id for c in self.svc.first_response_overdue()], [case.case_id])
        self.assertEqual(self.svc.quote_overdue(), [])
        self.assertEqual([c.case_id for c in self.svc.overdue()], [case.case_id])
        self.assertEqual(
            self.svc.overdue(now=P.parse_iso("2026-09-21T08:10:00Z")), [],
            "now == due_at tidak boleh dianggap overdue",
        )

        self.advance(minutes=229)
        self.assertEqual([c.case_id for c in self.svc.first_response_overdue()], [case.case_id])
        self.assertEqual(self.svc.quote_overdue(), [], "quote_due_at belum lewat")
        self.advance(minutes=1)
        self.assertEqual([c.case_id for c in self.svc.quote_overdue()], [case.case_id])
        self.assertEqual(
            sorted(c.case_id for c in self.svc.overdue()), [case.case_id]
        )

    def test_answered_cases_leave_their_overdue_queue(self):
        case = self.open_case(channel="whatsapp_business")
        self.advance(minutes=11)
        self.assertEqual([c.case_id for c in self.svc.first_response_overdue()], [case.case_id])

        answered = self.svc.mark_first_response(
            case.case_id, actor="agent_01", command_id="cmd_case_1301"
        )
        self.assertEqual(answered.first_responded_at, P.to_iso(self.clock.now()))
        self.assertEqual(answered.status, "NEW")
        self.assertEqual(self.svc.first_response_overdue(), [])
        self.assertEqual([c.case_id for c in self.svc.overdue()], [])

        self.triage(case.case_id, "cmd_case_1302")
        self.advance(minutes=230)
        self.assertEqual([c.case_id for c in self.svc.quote_overdue()], [case.case_id])
        quoted = self.svc.mark_quoted(case.case_id, actor="agent_01", command_id="cmd_case_1304")
        self.assertEqual(quoted.status, "QUOTED")
        self.assertEqual(quoted.quoted_at, P.to_iso(self.clock.now()))
        self.assertEqual(self.svc.quote_overdue(), [])
        self.assertEqual(self.svc.overdue(), [])

    def test_repeated_marks_are_recorded_without_double_answering(self):
        case = self.open_case(channel="whatsapp_business")
        self.svc.mark_first_response(case.case_id, actor="agent_01", command_id="cmd_case_1401")
        before = self.counts()
        self.refuse(
            "CASE_ALREADY_RESPONDED", self.svc.mark_first_response, case.case_id,
            actor="agent_01", command_id="cmd_case_1402",
        )
        self.assertEqual(self.counts(), before)
        self.triage(case.case_id, "cmd_case_1403")
        self.svc.mark_quoted(case.case_id, actor="agent_01", command_id="cmd_case_1405")
        self.advance(minutes=5)
        again = self.svc.mark_quoted(case.case_id, actor="agent_01", command_id="cmd_case_1406")
        self.assertEqual(again.status, "QUOTED")
        self.assertIsNotNone(again.quoted_at)
        self.assertEqual(self.event_rows(case.case_id)[-1][1:], ("NOTE", None, None, "cmd_case_1406"))


class TestOpenCaseValidation(CaseTestBase):
    """Invariants 6 and 7: refuse before writing, and reuse the shared ref helper."""

    def test_unknown_sla_policy_is_refused_before_any_row_is_written(self):
        before = self.counts()
        self.refuse(
            "SLA_POLICY_UNKNOWN", self.svc.open_case, channel="website", guest_ref="guest_0001",
            content_class="P1", body="halo", idempotency_key="idem_case_1501",
            sla_policy="tidak_ada_v9",
        )
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.svc.overdue(), [])

    def test_short_idempotency_key_is_refused_boundary_checked(self):
        before = self.counts()
        for key in ("1234567", ""):
            with self.subTest(key=key):
                self.refuse(
                    "INVALID_IDEMPOTENCY_KEY", self.svc.open_case, channel="website",
                    guest_ref="guest_0001", content_class="P1", body="halo", idempotency_key=key,
                )
        self.assertEqual(self.counts(), before)
        accepted = self.open_case(idempotency_key="12345678")
        self.assertEqual(accepted.status, "NEW")
        self.assertEqual(self.svc.get(accepted.case_id).case_id, accepted.case_id)

    def test_replayed_open_is_refused_and_leaves_a_single_case(self):
        first = self.open_case(idempotency_key="idem_case_1601")
        before = self.counts()
        self.refuse(
            "DUPLICATE_CASE", self.svc.open_case, channel="email", guest_ref="guest_0002",
            content_class="P1", body="halo lagi", idempotency_key="idem_case_1601",
        )
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.svc.get(first.case_id).case_id, first.case_id)

    def test_invalid_opening_inputs_are_refused_without_writing(self):
        cases = (
            ("INVALID_CHANNEL", {"channel": "sms"}),
            ("INVALID_CHANNEL", {"channel": "WHATSAPP_BUSINESS"}),
            ("INVALID_CONTENT_CLASS", {"content_class": "P9"}),
            ("INVALID_CONTENT_CLASS", {"content_class": "p1"}),
            ("INVALID_PRIORITY", {"priority": "WHEN"}),
            ("INVALID_GUEST_REF", {"guest_ref": None}),
            ("INVALID_REF", {"owner_actor": "  "}),
            ("INVALID_ATTACHMENT_COUNT", {"attachments": -1}),
        )
        before = self.counts()
        for index, (code, overrides) in enumerate(cases):
            with self.subTest(code=code, overrides=overrides):
                kwargs = {
                    "channel": "website", "guest_ref": "guest_0001", "content_class": "P1",
                    "body": "halo", "idempotency_key": "idem_case_17%02d" % index,
                }
                kwargs.update(overrides)
                self.refuse(code, self.svc.open_case, **kwargs)
                self.assertEqual(self.counts(), before)

    def test_guest_ref_follows_the_shared_ref_helper(self):
        rejected = (None, "", "   ", "g" * 129, 42)
        for value in rejected:
            with self.subTest(value=value):
                self.refuse(
                    "INVALID_GUEST_REF", self.svc.open_case, channel="website", guest_ref=value,
                    content_class="P1", body="halo", idempotency_key="idem_case_1801",
                )
                with self.assertRaises(P.DomainError):
                    P.require_ref(value, "guest_ref")
        accepted = [("abc", 3), ("g" * 128, 128), ("guest-0001", None)]
        for index, (value, expected_len) in enumerate(accepted):
            with self.subTest(value=value):
                case = self.open_case(guest_ref=value, idempotency_key="idem_case_19%02d" % index)
                self.assertEqual(case.guest_ref, value)
                self.assertEqual(P.require_ref(value, "guest_ref"), value)
                self.assertEqual(P.require_ref(value, "guest_ref"), value)
                if expected_len is not None:
                    self.assertEqual(len(case.guest_ref), expected_len)
        before = self.counts()
        self.refuse(
            "INVALID_GUEST_REF", self.svc.open_case, channel="website", guest_ref="ab",
            content_class="P1", body="halo", idempotency_key="idem_case_1999",
        )
        self.assertEqual(self.counts(), before)


class TestInboundAndTimeline(CaseTestBase):
    """Inbound messages append a message + one event and never move the status."""

    def test_record_inbound_appends_a_message_and_one_event(self):
        case = self.open_case(channel="whatsapp_business")
        message = self.svc.record_inbound(
            case.case_id, content_class="P1", body="sudah saya transfer", attachments=3,
            idempotency_key="idem_case_2001",
        )
        self.assertEqual(message.direction, "INBOUND")
        self.assertEqual(message.channel, "whatsapp_business")
        self.assertEqual(message.sender_ref, "guest_0001")
        self.assertEqual(message.received_at, NOW_ISO)
        self.assertEqual(message.created_at, NOW_ISO)
        self.assertEqual(message.attachments, 3)
        self.assertTrue(message.untrusted)
        self.assertEqual(self.counts(), (1, 2, 2))
        last = self.event_rows(case.case_id)[-1]
        self.assertEqual(last[1:], ("MESSAGE_IN", None, None, "idem_case_2001"))
        self.assertEqual(self.svc.get(case.case_id).status, "NEW")

    def test_record_inbound_honours_explicit_channel_sender_and_received_at(self):
        case = self.open_case(channel="whatsapp_business")
        received = P.parse_iso("2026-09-21T06:30:00Z")
        message = self.svc.record_inbound(
            case.case_id, content_class="P0", body="dari email", channel="email",
            sender_ref="ops_01", external_ref="mail-0001", received_at=received,
            idempotency_key="idem_case_2101",
        )
        self.assertEqual(message.channel, "email")
        self.assertEqual(message.sender_ref, "ops_01")
        self.assertEqual(message.external_ref, "mail-0001")
        self.assertEqual(message.received_at, P.to_iso(received))
        row = self.conn.execute(
            "SELECT channel, external_ref, sender_ref, received_at, content_class"
            " FROM case_messages WHERE case_message_id = ?",
            (message.case_message_id,),
        ).fetchone()
        self.assertEqual(
            tuple(row), ("email", "mail-0001", "ops_01", P.to_iso(received), "P0")
        )

    def test_record_inbound_validations_refuse_without_writing(self):
        case = self.open_case()
        before = self.counts()
        self.refuse(
            "CASE_NOT_FOUND", self.svc.record_inbound, "case_tidak_ada", content_class="P1",
            body="halo", idempotency_key="idem_case_2201",
        )
        self.refuse(
            "INVALID_CONTENT_CLASS", self.svc.record_inbound, case.case_id,
            content_class="P9", body="halo", idempotency_key="idem_case_2202",
        )
        self.refuse(
            "INVALID_IDEMPOTENCY_KEY", self.svc.record_inbound, case.case_id,
            content_class="P1", body="halo", idempotency_key="short",
        )
        self.refuse(
            "INVALID_ATTACHMENT_COUNT", self.svc.record_inbound, case.case_id,
            content_class="P1", body="halo", idempotency_key="idem_case_2203", attachments=-1,
        )
        self.refuse(
            "INVALID_CHANNEL", self.svc.record_inbound, case.case_id, content_class="P1",
            body="halo", idempotency_key="idem_case_2204", channel="sms",
        )
        self.assertEqual(self.counts(), before)
        self.svc.record_inbound(
            case.case_id, content_class="P1", body="halo", idempotency_key="idem_case_2205"
        )
        after = self.counts()
        self.refuse(
            "DUPLICATE_COMMAND", self.svc.record_inbound, case.case_id, content_class="P1",
            body="halo", idempotency_key="idem_case_2205",
        )
        self.assertEqual(self.counts(), after)
        self.assertEqual(after, (before[0], before[1] + 1, before[2] + 1))

    def test_timeline_is_ordered_scoped_and_rejects_unknown_cases(self):
        case = self.open_case()
        self.triage(case.case_id, "cmd_case_23")
        other = self.open_case(guest_ref="guest_0002", idempotency_key="idem_case_2401")
        events = self.svc.timeline(case.case_id)
        self.assertEqual([e.seq for e in events], [1, 2, 3])
        self.assertEqual({e.case_id for e in events}, {case.case_id})
        self.assertEqual(len(self.svc.timeline(other.case_id)), 1)
        self.refuse("CASE_NOT_FOUND", self.svc.timeline, "case_tidak_ada")
        self.refuse("CASE_NOT_FOUND", self.svc.get, "case_tidak_ada")
        self.refuse(
            "CASE_NOT_FOUND", self.svc.transition, "case_tidak_ada", "TRIAGED",
            actor="agent_01", actor_role="agent", reason="x", command_id="cmd_case_2501",
        )
        self.assertEqual([e.command_id for e in events], [
            "idem_case_0001", "cmd_case_23_triase", "cmd_case_23_match",
        ])


class TestBookingLink(CaseTestBase):
    """Booking linkage only accepts bookings that exist."""

    def test_link_booking_records_an_event_and_rejects_unknown_bookings(self):
        seed_booking(self.conn, "book_1")
        case = self.open_case()
        self.triage(case.case_id, "cmd_case_26")
        linked = self.svc.link_booking(
            case.case_id, "book_1", actor="agent_01", command_id="cmd_case_2603"
        )
        self.assertEqual(linked.booking_id, "book_1")
        last = self.svc.timeline(case.case_id)[-1]
        self.assertEqual((last.kind, last.refs), ("BOOKING_LINKED", ("book_1",)))
        before = self.counts()
        self.refuse(
            "BOOKING_NOT_FOUND", self.svc.link_booking, case.case_id, "book_tidak_ada",
            actor="agent_01", command_id="cmd_case_2604",
        )
        self.refuse(
            "INVALID_REF", self.svc.link_booking, case.case_id, "", actor="agent_01",
            command_id="cmd_case_2605",
        )
        self.refuse(
            "CASE_NOT_FOUND", self.svc.link_booking, "case_tidak_ada", "book_1",
            actor="agent_01", command_id="cmd_case_2606",
        )
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.svc.get(case.case_id).booking_id, "book_1")


class TestSourceHygiene(unittest.TestCase):
    """The module may not reach for wall-clock time or ad-hoc randomness."""

    def test_cases_module_uses_clock_and_id_factory_only(self):
        with open("src/cases.py", "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("datetime.now", source)
        self.assertNotIn("utcnow", source)
        self.assertNotIn("time.time", source)
        self.assertNotIn("import random", source)
        self.assertNotIn("import requests", source)
        self.assertNotIn("float(", source)


if __name__ == "__main__":
    unittest.main()
