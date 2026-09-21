"""Tests for :mod:`intake` — deterministic, rule-first intake extraction + review.

The extractor is expected to be a pure function of (text, now): no LLM, no
network, no wall clock. Every proposal carries provenance (the source message
it came from) and every extracted value is immutable once written — only the
review columns may move.

``src/cases.py`` does not exist yet, so fixtures are seeded with direct SQL
against the real schema (``src/schema/001_initial.sql``). That is deliberate:
these tests must not depend on a module that is still being written in
parallel.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone

from primitives import FrozenClock, IdFactory, canonical_json, fingerprint, sha256_hex
from schema_loader import connect_sqlite

from intake import (
    ACCEPT_CONFIDENCE_MIN,
    AMBIGUOUS_CONFIDENCE_MAX,
    AREA_ALIASES,
    DECISIONS,
    FIELD_SPECS,
    MUTABLE_COLUMNS,
    PURPOSES,
    REQUIRED_FIELDS,
    STATUS_ACCEPTED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    VALUE_TYPES,
    IntakeError,
    IntakeService,
    ProposedField,
    parse_rule_based,
    validate_facts,
)

EXTRACTOR = "intake.rule.v1"

# The reference "today" for every test: 21 September 2026, 08:00 UTC.
NOW = "2026-09-21T08:00:00+00:00"

# --- guest messages (redacted inbound WhatsApp-style text) ---------------- #

# Complete, explicit request: dates, party, budget, area, purpose, arrival.
MESSAGE_A = (
    "Halo, saya mau tanya villa di Canggu untuk liburan.\n"
    "\n"
    "Rencana menginap 12-14 Oktober 2026, 2 dewasa 1 anak.\n"
    "Budget sekitar Rp 2.500.000 per malam.\n"
    "Datang sekitar jam 15.00 karena pesawat siang.\n"
    "Ada kolam renang pribadi?"
)

# ISO date + duration + group size + area + workation + flexible dates.
MESSAGE_B = (
    "Selamat siang, saya butuh villa di Ubud untuk kerja remote.\n"
    "\n"
    "Check in 2026-11-05, rencananya 5 malam.\n"
    "Kami 3 orang. Tanggalnya masih fleksibel, budget maksimal 1,5jt per malam."
)

# Deliberately vague: "akhir bulan", unknown party size, no budget. The only
# correct answer for those fields is UNKNOWN — never a guessed date.
MESSAGE_C = (
    "Halo kak, saya mau sewa villa di Seminyak untuk liburan akhir bulan.\n"
    "\n"
    "Jumlah tamu belum tahu jumlah orang, mungkin nanti saya kabari lagi.\n"
    "Budget juga belum ada, secepatnya ya kak.\n"
    "\n"
    "Yang penting ada kolam renang pribadi."
)


def facts(proposed):
    """``{field_name: (value, confidence)}`` in the order the parser emitted."""
    return {p.name: (p.value, p.confidence) for p in proposed}


class RuleExtractionTests(unittest.TestCase):
    """``parse_rule_based`` — pure extraction from guest text."""

    def setUp(self):
        self.clock = FrozenClock(datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc))

    def parse(self, text):
        return parse_rule_based(text, now=self.clock)

    # 1 -------------------------------------------------------------- #
    def test_message_a_extracts_all_explicit_fields(self):
        proposed = self.parse(MESSAGE_A)
        expected = {
            "check_in": (date(2026, 10, 12), 92),
            "check_out": (date(2026, 10, 14), 92),
            "adults": (2, 90),
            "children": (1, 90),
            "area_preference": ("Canggu", 85),
            "budget_minor": (2500000, 90),
            "currency": ("IDR", 80),
            "purpose": ("leisure", 75),
            "special_requests": ("kolam renang pribadi", 60),
            "arrival_time": ("15:00", 80),
        }
        self.assertEqual(facts(proposed), expected)
        # Emission order is the FIELD_SPECS order, so downstream diffs are stable.
        self.assertEqual(
            [p.name for p in proposed],
            [name for name in FIELD_SPECS if name in expected],
        )

    # 2 -------------------------------------------------------------- #
    def test_message_a_spans_point_at_the_source_text(self):
        for p in self.parse(MESSAGE_A):
            with self.subTest(field=p.name):
                self.assertIsInstance(p.span_start, int)
                self.assertIsInstance(p.span_end, int)
                self.assertGreater(p.span_end, p.span_start)
                self.assertLessEqual(p.span_end, len(MESSAGE_A))
                self.assertEqual(MESSAGE_A[p.span_start : p.span_end], p.span_end and MESSAGE_A[p.span_start : p.span_end])
        by_name = {p.name: p for p in self.parse(MESSAGE_A)}
        self.assertEqual(MESSAGE_A[by_name["check_in"].span_start : by_name["check_in"].span_end], "12-14 Oktober 2026")
        self.assertEqual(MESSAGE_A[by_name["area_preference"].span_start : by_name["area_preference"].span_end], "Canggu")
        self.assertEqual(MESSAGE_A[by_name["budget_minor"].span_start : by_name["budget_minor"].span_end], "Rp 2.500.000")
        self.assertEqual(MESSAGE_A[by_name["arrival_time"].span_start : by_name["arrival_time"].span_end], "Datang sekitar jam 15.00")

    # 3 -------------------------------------------------------------- #
    def test_message_b_derives_check_out_from_nights(self):
        proposed = self.parse(MESSAGE_B)
        expected = {
            "check_in": (date(2026, 11, 5), 95),
            "check_out": (date(2026, 11, 10), 92),
            "adults": (3, 70),
            "area_preference": ("Ubud", 85),
            "budget_minor": (1500000, 80),
            "currency": ("IDR", 80),
            "purpose": ("workation", 75),
            "flexible_dates": (True, 70),
        }
        self.assertEqual(facts(proposed), expected)
        by_name = {p.name: p for p in proposed}
        # The derived checkout must come from check_in + the stated 5 nights,
        # and its provenance must be the duration expression, not the checkin.
        self.assertEqual(by_name["check_out"].value - by_name["check_in"].value, timedelta(days=5))
        self.assertEqual(MESSAGE_B[by_name["check_out"].span_start : by_name["check_out"].span_end], "5 malam")
        self.assertEqual(MESSAGE_B[by_name["check_in"].span_start : by_name["check_in"].span_end], "2026-11-05")

    # 4 -------------------------------------------------------------- #
    def test_message_c_ambiguous_fields_stay_unknown(self):
        proposed = self.parse(MESSAGE_C)
        expected = {
            "check_in": (None, 20),
            "adults": (None, 20),
            "area_preference": ("Seminyak", 85),
            "purpose": ("leisure", 75),
            "special_requests": ("kolam renang pribadi", 60),
        }
        self.assertEqual(facts(proposed), expected)

        by_name = {p.name: p for p in proposed}
        # "akhir bulan" is a span in the text, but its value is UNKNOWN.
        self.assertEqual(MESSAGE_C[by_name["check_in"].span_start : by_name["check_in"].span_end], "akhir bulan")
        self.assertEqual(MESSAGE_C[by_name["adults"].span_start : by_name["adults"].span_end], "belum tahu jumlah orang")

        # Ambiguity is not a guess: nothing date-like was invented anywhere.
        self.assertNotIn("check_out", by_name)
        self.assertNotIn("budget_minor", by_name)
        for p in proposed:
            with self.subTest(field=p.name):
                if p.value is None:
                    self.assertLessEqual(p.confidence, AMBIGUOUS_CONFIDENCE_MAX)
                    self.assertLess(p.confidence, ACCEPT_CONFIDENCE_MIN)
                else:
                    self.assertGreaterEqual(p.confidence, ACCEPT_CONFIDENCE_MIN)
                self.assertNotIsInstance(p.value, date)

    # 5 -------------------------------------------------------------- #
    def test_relative_expressions_never_become_dates(self):
        self.assertLess(AMBIGUOUS_CONFIDENCE_MAX, ACCEPT_CONFIDENCE_MIN)
        for text in (
            "akhir bulan ya",
            "weekend nanti bisa?",
            "secepatnya ya kak",
            "bulan depan kami datang",
            "minggu depan baru pasti",
        ):
            with self.subTest(text=text):
                proposed = self.parse(text)
                self.assertEqual(facts(proposed), {"check_in": (None, 20)})
                self.assertLess(proposed[0].confidence, ACCEPT_CONFIDENCE_MIN)

    # 6 -------------------------------------------------------------- #
    def test_message_without_calendar_expression_yields_nothing(self):
        for text in ("Halo, terima kasih.", "Tolong info paketnya ya kak.", ""):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), [])

    # 7 -------------------------------------------------------------- #
    def test_iso_slash_and_indonesian_date_grammars(self):
        cases = [
            ("Masuk 2026-10-12 ya", date(2026, 10, 12), 95),
            ("Masuk 12/10/2026 ya", date(2026, 10, 12), 92),
            ("Masuk 12 Okt 2026 ya", date(2026, 10, 12), 92),
            ("Masuk 12 Okt ya", date(2026, 10, 12), 92),
            ("Masuk 2 Nov ya", date(2026, 11, 2), 92),
            ("Masuk 12 sampai 14 Oktober 2026", date(2026, 10, 12), 92),
        ]
        for text, expected_date, expected_confidence in cases:
            with self.subTest(text=text):
                proposed = self.parse(text)
                names = [p.name for p in proposed]
                self.assertIn("check_in", names)
                self.assertEqual(proposed[0].value, expected_date)
                self.assertEqual(proposed[0].confidence, expected_confidence)

    # 8 -------------------------------------------------------------- #
    def test_date_range_and_duration_agree_on_check_out(self):
        proposed = self.parse("Menginap 12-14 Oktober 2026")
        self.assertEqual(facts(proposed), {"check_in": (date(2026, 10, 12), 92), "check_out": (date(2026, 10, 14), 92)})
        # Duration only (no anchor date) is not a date.
        self.assertEqual(facts(self.parse("Rencananya 3 malam saja")), {})
        # Duration with an anchor derives the checkout.
        derived = self.parse("Check in 2026-10-12, 3 malam")
        self.assertEqual(facts(derived), {"check_in": (date(2026, 10, 12), 95), "check_out": (date(2026, 10, 15), 92)})

    # 9 -------------------------------------------------------------- #
    def test_yearless_date_uses_the_next_occurrence(self):
        # Today is 2026-09-21: October 2026 is still ahead, January is not.
        self.assertEqual(self.parse("Datang 5 Jan")[0].value, date(2027, 1, 5))
        self.assertEqual(self.parse("Datang 5 Okt")[0].value, date(2026, 10, 5))

    # 10 ------------------------------------------------------------- #
    def test_party_size_area_budget_and_purpose_units(self):
        proposed = self.parse(
            "Kami 4 dewasa dan 2 anak serta 1 bayi.\n"
            "Cari di Yogya, budget Rp 1.500.000 per malam untuk acara kantor."
        )
        got = facts(proposed)
        self.assertEqual(got["adults"], (4, 90))
        self.assertEqual(got["children"], (2, 90))
        self.assertEqual(got["infants"][0], 1)
        self.assertGreaterEqual(got["infants"][1], 60)
        self.assertEqual(got["area_preference"], ("Yogyakarta", 85))
        self.assertEqual(got["budget_minor"], (1500000, 90))
        self.assertEqual(got["currency"], ("IDR", 80))
        self.assertEqual(got["purpose"], ("event", 75))

        multiplier = facts(self.parse("Budget 2jt per malam"))
        self.assertEqual(multiplier["budget_minor"], (2000000, 80))
        self.assertEqual(multiplier["currency"], ("IDR", 80))
        group = facts(self.parse("Total 6 orang ya"))
        self.assertEqual(group["adults"], (6, 70))

    # 11 ------------------------------------------------------------- #
    def test_area_aliases_map_to_canonical_region_names(self):
        self.assertEqual(AREA_ALIASES["yogya"], "Yogyakarta")
        self.assertEqual(AREA_ALIASES["seminjak"], "Seminyak")
        for alias, canonical in sorted(AREA_ALIASES.items()):
            with self.subTest(alias=alias):
                proposed = facts(parse_rule_based("cari villa di %s" % alias, now=self.clock))
                self.assertEqual(proposed.get("area_preference"), (canonical, 85))
        # No area mentioned -> no area proposal.
        self.assertNotIn("area_preference", facts(self.parse("Halo, ada villa kosong?")))

    # 12 ------------------------------------------------------------- #
    def test_parse_is_deterministic_and_pure(self):
        first = parse_rule_based(MESSAGE_A, now=self.clock)
        second = parse_rule_based(MESSAGE_A, now=self.clock)
        self.assertEqual(first, second)
        # A different clock changes year inference, not the text it reads.
        later = FrozenClock(datetime(2026, 12, 31, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(parse_rule_based("Datang 12 Okt", now=later)[0].value, date(2027, 10, 12))

    # 13 ------------------------------------------------------------- #
    def test_parse_rejects_non_string_input(self):
        for bad in (None, 42, b"teks", ["teks"]):
            with self.subTest(bad=bad):
                with self.assertRaises(IntakeError) as ctx:
                    parse_rule_based(bad, now=self.clock)
                self.assertEqual(ctx.exception.code, "INVALID_FIELD_VALUE")


class FieldSpecContractTests(unittest.TestCase):
    """FIELD_SPECS is the contract the extractor, validators and review share."""

    def test_field_specs_cover_the_documented_contract(self):
        self.assertEqual(
            list(FIELD_SPECS),
            [
                "check_in",
                "check_out",
                "adults",
                "children",
                "infants",
                "area_preference",
                "budget_minor",
                "currency",
                "purpose",
                "special_requests",
                "arrival_time",
                "flexible_dates",
            ],
        )
        self.assertEqual(REQUIRED_FIELDS, ("check_in", "check_out", "adults"))
        self.assertEqual(FIELD_SPECS["check_in"].value_type, "date")
        self.assertEqual(FIELD_SPECS["check_out"].value_type, "date")
        self.assertEqual(FIELD_SPECS["adults"].value_type, "integer")
        self.assertEqual(FIELD_SPECS["children"].value_type, "integer")
        self.assertEqual(FIELD_SPECS["infants"].value_type, "integer")
        self.assertEqual(FIELD_SPECS["area_preference"].value_type, "text")
        self.assertEqual(FIELD_SPECS["budget_minor"].value_type, "money")
        self.assertEqual(FIELD_SPECS["currency"].value_type, "enum")
        self.assertEqual(FIELD_SPECS["purpose"].value_type, "enum")
        self.assertEqual(FIELD_SPECS["special_requests"].value_type, "text")
        self.assertEqual(FIELD_SPECS["arrival_time"].value_type, "text")
        self.assertEqual(FIELD_SPECS["flexible_dates"].value_type, "boolean")
        for name, spec in FIELD_SPECS.items():
            with self.subTest(field=name):
                self.assertEqual(spec.name, name)
                self.assertIn(spec.value_type, VALUE_TYPES)
                self.assertTrue(spec.description)
                self.assertEqual(spec.required, name in REQUIRED_FIELDS)

    def test_validators_are_callable_and_fail_closed_on_none(self):
        # None means "asked, but UNKNOWN" -- legal to store, never acceptable
        # as a fact. Validators must not blow up on it.
        for name, spec in FIELD_SPECS.items():
            with self.subTest(field=name):
                self.assertTrue(spec.validator(None))

    def test_validators_reject_garbage(self):
        self.assertFalse(FIELD_SPECS["check_in"].validator("2026-13-45"))
        self.assertFalse(FIELD_SPECS["check_in"].validator("besok"))
        self.assertFalse(FIELD_SPECS["adults"].validator(0))
        self.assertFalse(FIELD_SPECS["adults"].validator(-1))
        self.assertFalse(FIELD_SPECS["children"].validator("dua"))
        self.assertFalse(FIELD_SPECS["budget_minor"].validator(0))
        self.assertFalse(FIELD_SPECS["currency"].validator("RUPIAH1"))
        self.assertFalse(FIELD_SPECS["currency"].validator("ID"))
        self.assertFalse(FIELD_SPECS["purpose"].validator("business"))
        self.assertTrue(FIELD_SPECS["purpose"].validator("leisure"))
        self.assertIn("leisure", PURPOSES)
        self.assertTrue(FIELD_SPECS["adults"].validator(2))
        self.assertTrue(FIELD_SPECS["check_in"].validator(date(2026, 10, 12)))


class IntakeBase(unittest.TestCase):
    """Shared fixtures: an in-memory DB, one case, one redacted inbound message."""

    def setUp(self):
        self.conn = connect_sqlite(":memory:")
        self.clock = FrozenClock(datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc))
        self.ids = IdFactory(deterministic=True, seed="t")
        self.svc = IntakeService(self.conn, clock=self.clock, ids=self.ids)
        self.case_id = "case-001"
        self.message_id = "cmsg-001"
        self.seed_case(self.case_id)
        self.seed_message(self.message_id, self.case_id, MESSAGE_A)

    def tearDown(self):
        self.conn.close()

    # -- fixtures -------------------------------------------------------- #
    def seed_case(self, case_id, *, guest_ref="guest-001"):
        self.conn.execute(
            "INSERT INTO cases (case_id, channel, guest_ref, status, priority, sla_policy,"
            " first_response_due_at, quote_due_at, idempotency_key, created_at, updated_at)"
            " VALUES (?, 'whatsapp_business', ?, 'NEW', 'NORMAL', 'sla-std',"
            " '2026-09-21T09:00:00+00:00', '2026-09-21T10:00:00+00:00', ?, ?, ?)",
            (case_id, guest_ref, "idem-" + case_id, NOW, NOW),
        )

    def seed_message(self, message_id, case_id, body):
        self.conn.execute(
            "INSERT INTO case_messages (case_message_id, case_id, direction, channel, sender_ref,"
            " received_at, content_class, body_redacted, body_sha256, attachment_count, untrusted, created_at)"
            " VALUES (?, ?, 'INBOUND', 'whatsapp_business', 'guest-001', ?, 'P2', ?, ?, 0, 1, ?)",
            (message_id, case_id, NOW, body, sha256_hex(body), NOW),
        )

    # -- helpers --------------------------------------------------------- #
    def count(self, table, where="", params=()):
        sql = "SELECT COUNT(*) FROM %s %s" % (table, where)
        return self.conn.execute(sql, params).fetchone()[0]

    def field_count(self):
        return self.count("intake_fields")

    def proposal_count(self):
        return self.count("intake_proposals")

    def field_row(self, intake_field_id):
        return self.conn.execute(
            "SELECT field_value, value_type, confidence, source_message_id,"
            " source_span_start, source_span_end, field_name, value_sha256, status,"
            " reviewed_by, reviewed_at, rejection_reason"
            " FROM intake_fields WHERE intake_field_id = ?",
            (intake_field_id,),
        ).fetchone()

    def propose_message(self, text=MESSAGE_A, *, case_id=None, message_id=None, **kwargs):
        case_id = case_id or self.case_id
        message_id = message_id or self.message_id
        return self.svc.propose(
            case_id,
            source_message_id=message_id,
            fields=parse_rule_based(text, now=self.clock),
            extractor=EXTRACTOR,
            produced_at=self.clock.now(),
            **kwargs,
        )

    def propose_fields(self, fields, *, case_id=None, message_id=None, **kwargs):
        return self.svc.propose(
            case_id or self.case_id,
            source_message_id=message_id or self.message_id,
            fields=fields,
            extractor=EXTRACTOR,
            produced_at=self.clock.now(),
            **kwargs,
        )

    def accept(self, intake_field_id, *, reviewer="agent-1", reviewer_role="sales", reason=None):
        return self.svc.review(
            intake_field_id=intake_field_id,
            decision="ACCEPT",
            reviewer=reviewer,
            reviewer_role=reviewer_role,
            at=self.clock.now(),
            reason=reason,
        )

    def reject(self, intake_field_id, *, reason="harga belum cocok", reviewer="agent-1", reviewer_role="sales"):
        return self.svc.review(
            intake_field_id=intake_field_id,
            decision="REJECT",
            reviewer=reviewer,
            reviewer_role=reviewer_role,
            at=self.clock.now(),
            reason=reason,
        )


class ProposeTests(IntakeBase):
    """``IntakeService.propose`` — provenance, fingerprints, fail-closed writes."""

    # 14 ------------------------------------------------------------- #
    def test_propose_persists_proposal_and_fields_with_provenance(self):
        proposal_id, rows = self.propose_message()
        self.assertTrue(proposal_id.startswith("prop_"))
        self.assertEqual(len(rows), 10)
        self.assertEqual(self.proposal_count(), 1)
        self.assertEqual(self.field_count(), 10)
        self.assertEqual(
            self.count("intake_fields", "WHERE proposal_id = ?", (proposal_id,)),
            10,
        )

        proposal = self.svc.get_proposal(proposal_id)
        self.assertEqual(proposal["case_id"], self.case_id)
        self.assertEqual(proposal["source_message_id"], self.message_id)
        self.assertEqual(proposal["extractor"], EXTRACTOR)
        self.assertEqual(proposal["method"], "rule")
        self.assertEqual(proposal["status"], STATUS_PROPOSED)
        self.assertEqual(proposal["produced_at"], NOW)
        self.assertEqual(proposal["created_at"], NOW)

        seen = set()
        for row in rows:
            with self.subTest(field=row.field_name):
                self.assertTrue(row.intake_field_id.startswith("ifld_"))
                self.assertEqual(row.proposal_id, proposal_id)
                self.assertEqual(row.case_id, self.case_id)
                self.assertEqual(row.source_message_id, self.message_id)
                self.assertEqual(row.status, STATUS_PROPOSED)
                self.assertIsNone(row.reviewed_by)
                self.assertIsNone(row.reviewed_at)
                self.assertIsNone(row.rejection_reason)
                self.assertEqual(row.created_at, NOW)
                self.assertGreaterEqual(row.confidence, 0)
                self.assertLessEqual(row.confidence, 100)
                self.assertEqual(row.value_type, FIELD_SPECS[row.field_name].value_type)
                self.assertEqual(len(row.value_sha256), 64)
                seen.add(row.field_name)
        self.assertEqual(seen, set(FIELD_SPECS).intersection(seen))

    # 15 ------------------------------------------------------------- #
    def test_proposal_fingerprint_is_the_canonical_payload_hash(self):
        proposal_id, _ = self.propose_message()
        proposal = self.svc.get_proposal(proposal_id)
        payload = proposal["payload"]
        self.assertEqual(canonical_json(__import__("json").loads(payload)), payload)
        self.assertEqual(fingerprint(__import__("json").loads(payload)), proposal["fingerprint"])
        self.assertEqual(len(proposal["fingerprint"]), 64)
        decoded = __import__("json").loads(payload)
        self.assertEqual(decoded["case_id"], self.case_id)
        self.assertEqual(decoded["source_message_id"], self.message_id)
        self.assertEqual(decoded["method"], "rule")
        self.assertEqual(len(decoded["fields"]), 10)
        self.assertEqual(
            {f["name"] for f in decoded["fields"]},
            {
                "check_in",
                "check_out",
                "adults",
                "children",
                "area_preference",
                "budget_minor",
                "currency",
                "purpose",
                "special_requests",
                "arrival_time",
            },
        )
        for field in decoded["fields"]:
            self.assertTrue(set(field) >= {"name", "value", "confidence"}, field)

    # 16 ------------------------------------------------------------- #
    def test_propose_rejects_unknown_field(self):
        with self.assertRaises(IntakeError) as ctx:
            self.propose_fields([ProposedField("villa_style", "modern", 90, 0, 6)])
        self.assertEqual(ctx.exception.code, "UNKNOWN_FIELD")
        self.assertEqual(self.proposal_count(), 0)
        self.assertEqual(self.field_count(), 0)

    # 17 ------------------------------------------------------------- #
    def test_invalid_values_are_never_stored(self):
        bad_fields = [
            ProposedField("check_in", "2026-13-45", 95, 0, 10),
            ProposedField("adults", 0, 90, 0, 4),
            ProposedField("currency", "RUPIAH1", 80, 0, 7),
            ProposedField("purpose", "business", 75, 0, 8),
        ]
        for bad in bad_fields:
            with self.subTest(field=bad.name, value=bad.value):
                with self.assertRaises(IntakeError) as ctx:
                    self.propose_fields([bad])
                self.assertEqual(ctx.exception.code, "INVALID_FIELD_VALUE")
                self.assertEqual(self.proposal_count(), 0)
                self.assertEqual(self.field_count(), 0)

    # 18 ------------------------------------------------------------- #
    def test_propose_requires_a_real_source_message(self):
        with self.assertRaises(IntakeError) as ctx:
            self.propose_message(message_id="cmsg-does-not-exist")
        self.assertEqual(ctx.exception.code, "MISSING_PROVENANCE")
        self.assertEqual(self.proposal_count(), 0)
        self.assertEqual(self.field_count(), 0)

    # 19 ------------------------------------------------------------- #
    def test_propose_rejects_a_message_belonging_to_another_case(self):
        self.seed_case("case-002", guest_ref="guest-002")
        self.seed_message("cmsg-002", "case-002", MESSAGE_B)
        with self.assertRaises(IntakeError) as ctx:
            self.propose_message(case_id=self.case_id, message_id="cmsg-002")
        self.assertEqual(ctx.exception.code, "MISSING_PROVENANCE")
        # The same message is fine for the case that actually owns it.
        _, rows = self.propose_message(case_id="case-002", message_id="cmsg-002", text=MESSAGE_B)
        self.assertTrue(rows)
        self.assertEqual(self.field_count(), len(rows))

    # 20 ------------------------------------------------------------- #
    def test_propose_dedupes_field_names_deterministically(self):
        _, rows = self.propose_fields(
            [
                ProposedField("adults", 2, 50, 0, 6),
                ProposedField("adults", 4, 90, 10, 16),
                ProposedField("children", 1, 90, 20, 26),
                ProposedField("children", 3, 90, 30, 36),
            ]
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.count("intake_fields", "WHERE field_name = 'adults'"), 1)
        self.assertEqual(self.count("intake_fields", "WHERE field_name = 'children'"), 1)
        got = {row.field_name: row for row in rows}
        # Highest confidence wins ...
        self.assertEqual(got["adults"].python_value, 4)
        self.assertEqual(got["adults"].confidence, 90)
        # ... ties keep the earliest proposal, so the result is reproducible.
        self.assertEqual(got["children"].python_value, 1)
        self.assertEqual(got["children"].source_span_start, 20)

    # 21 ------------------------------------------------------------- #
    def test_propose_rejects_reversed_span_and_empty_field_list(self):
        with self.assertRaises(IntakeError) as ctx:
            self.propose_fields([ProposedField("adults", 2, 90, 12, 4)])
        self.assertEqual(ctx.exception.code, "INVALID_FIELD_VALUE")
        with self.assertRaises(IntakeError) as empty_ctx:
            self.propose_fields([])
        # Documented choice: a proposal with no field carries no provenance.
        self.assertEqual(empty_ctx.exception.code, "MISSING_PROVENANCE")
        self.assertEqual(self.proposal_count(), 0)
        self.assertEqual(self.field_count(), 0)

    # 22 ------------------------------------------------------------- #
    def test_propose_unknown_case_and_unknown_proposal(self):
        with self.assertRaises(IntakeError) as ctx:
            self.propose_message(case_id="case-nope")
        self.assertEqual(ctx.exception.code, "CASE_NOT_FOUND")
        with self.assertRaises(IntakeError) as ctx:
            self.svc.get_proposal("prop-nope")
        self.assertEqual(ctx.exception.code, "INTAKE_PROPOSAL_NOT_FOUND")
        self.assertEqual(self.proposal_count(), 0)


class ImmutabilityTests(IntakeBase):
    """Provenance-bearing values are write-once; only review columns move."""

    # 23 ------------------------------------------------------------- #
    def test_extracted_values_are_immutable(self):
        _, rows = self.propose_message()
        row = rows[0]
        before = self.field_row(row.intake_field_id)

        illegal = [
            {"field_value": "2099-01-01"},
            {"value_type": "text"},
            {"confidence": 100},
            {"confidence": 1},
            {"source_span_start": 0},
            {"source_span_end": 1},
            {"field_name": "check_out"},
            {"value_sha256": "0" * 64},
            {"source_message_id": "cmsg-002"},
            {"created_at": "2030-01-01T00:00:00+00:00"},
            {"case_id": "case-002"},
            {"intake_field_id": "ifld-nope"},
            {"not_a_column": "x"},
        ]
        for change in illegal:
            with self.subTest(change=change):
                with self.assertRaises(IntakeError) as ctx:
                    self.svc.update_field(row.intake_field_id, changes=change)
                self.assertEqual(ctx.exception.code, "IMMUTABLE_FIELD")
                self.assertEqual(self.field_row(row.intake_field_id), before)

        self.assertEqual(MUTABLE_COLUMNS, ("status", "reviewed_by", "reviewed_at", "rejection_reason"))

    # 24 ------------------------------------------------------------- #
    def test_only_review_columns_can_move(self):
        _, rows = self.propose_message()
        row = rows[0]
        updated = self.svc.update_field(
            row.intake_field_id,
            changes={
                "status": STATUS_REJECTED,
                "reviewed_by": "agent-2",
                "reviewed_at": NOW,
                "rejection_reason": "tanggal bentrok",
            },
        )
        self.assertEqual(updated.status, STATUS_REJECTED)
        self.assertEqual(updated.reviewed_by, "agent-2")
        self.assertEqual(updated.reviewed_at, NOW)
        self.assertEqual(updated.rejection_reason, "tanggal bentrok")
        self.assertEqual(updated.field_value, row.field_value)
        self.assertEqual(updated.confidence, row.confidence)
        with self.assertRaises(IntakeError) as ctx:
            self.svc.update_field("ifld-nope", changes={"status": STATUS_REJECTED})
        self.assertEqual(ctx.exception.code, "INTAKE_FIELD_NOT_FOUND")


class ReviewTests(IntakeBase):
    """Human review: the only way a proposal becomes a fact."""

    def accepted_field(self):
        _, rows = self.propose_fields([ProposedField("adults", 2, 90, 0, 4)])
        return rows[0]

    # 25 ------------------------------------------------------------- #
    def test_accept_records_reviewer_and_status(self):
        row = self.accepted_field()
        reviewed = self.accept(row.intake_field_id, reviewer="agent-7", reviewer_role="reservasi")
        self.assertEqual(reviewed.status, STATUS_ACCEPTED)
        self.assertEqual(reviewed.reviewed_by, "agent-7")
        self.assertEqual(reviewed.reviewed_at, NOW)
        self.assertIsNone(reviewed.rejection_reason)
        self.assertEqual(reviewed.python_value, 2)
        stored = self.field_row(row.intake_field_id)
        self.assertEqual(stored["status"], STATUS_ACCEPTED)
        self.assertEqual(stored["reviewed_by"], "agent-7")
        self.assertEqual(stored["confidence"], 90)
        self.assertEqual(self.svc.effective_status(row.intake_field_id), STATUS_ACCEPTED)

    # 26 ------------------------------------------------------------- #
    def test_accept_refuses_unknown_value_even_when_confidence_matches(self):
        _, rows = self.propose_fields([ProposedField("check_in", None, ACCEPT_CONFIDENCE_MIN, 0, 11)])
        row = rows[0]
        with self.assertRaises(IntakeError) as ctx:
            self.accept(row.intake_field_id)
        self.assertEqual(ctx.exception.code, "LOW_CONFIDENCE")
        self.assertEqual(self.field_row(row.intake_field_id)["status"], STATUS_PROPOSED)
        self.assertEqual(self.count("intake_fields", "WHERE status = 'ACCEPTED'"), 0)
        self.assertEqual(self.svc.accepted_facts(self.case_id), {})

    # 27 ------------------------------------------------------------- #
    def test_accept_enforces_the_confidence_floor(self):
        _, low = self.propose_fields([ProposedField("adults", 2, ACCEPT_CONFIDENCE_MIN - 1, 0, 4)])
        with self.assertRaises(IntakeError) as ctx:
            self.accept(low[0].intake_field_id)
        self.assertEqual(ctx.exception.code, "LOW_CONFIDENCE")
        self.assertEqual(self.field_row(low[0].intake_field_id)["status"], STATUS_PROPOSED)

        _, boundary = self.propose_fields([ProposedField("adults", 3, ACCEPT_CONFIDENCE_MIN, 0, 4)])
        reviewed = self.accept(boundary[0].intake_field_id)
        self.assertEqual(reviewed.status, STATUS_ACCEPTED)
        self.assertEqual(self.svc.accepted_facts(self.case_id), {"adults": 3})

    # 28 ------------------------------------------------------------- #
    def test_low_confidence_message_c_extraction_cannot_be_accepted(self):
        _, rows = self.propose_message(MESSAGE_C)
        by_name = {row.field_name: row for row in rows}
        for name in ("check_in", "adults"):
            with self.subTest(field=name):
                with self.assertRaises(IntakeError) as ctx:
                    self.accept(by_name[name].intake_field_id)
                self.assertEqual(ctx.exception.code, "LOW_CONFIDENCE")
                self.assertEqual(self.field_row(by_name[name].intake_field_id)["status"], STATUS_PROPOSED)
        self.assertEqual(self.svc.accepted_facts(self.case_id), {})

    # 29 ------------------------------------------------------------- #
    def test_reject_requires_a_reason(self):
        row = self.accepted_field()
        for reason in (None, "", "  ", "no"):
            with self.subTest(reason=reason):
                with self.assertRaises(IntakeError) as ctx:
                    self.reject(row.intake_field_id, reason=reason)
                self.assertEqual(ctx.exception.code, "MISSING_REVIEW_REASON")
                stored = self.field_row(row.intake_field_id)
                self.assertEqual(stored["status"], STATUS_PROPOSED)
                self.assertIsNone(stored["rejection_reason"])
                self.assertIsNone(stored["reviewed_by"])

    # 30 ------------------------------------------------------------- #
    def test_reject_records_the_reason(self):
        row = self.accepted_field()
        reviewed = self.reject(row.intake_field_id, reason="tamu koreksi jumlah orang")
        self.assertEqual(reviewed.status, STATUS_REJECTED)
        self.assertEqual(reviewed.rejection_reason, "tamu koreksi jumlah orang")
        self.assertEqual(reviewed.reviewed_by, "agent-1")
        self.assertEqual(reviewed.reviewed_at, NOW)
        self.assertEqual(self.svc.accepted_facts(self.case_id), {})
        self.assertEqual(self.count("intake_fields", "WHERE status = 'REJECTED'"), 1)

    # 31 ------------------------------------------------------------- #
    def test_already_reviewed_fields_are_frozen(self):
        row = self.accepted_field()
        self.accept(row.intake_field_id)
        with self.assertRaises(IntakeError) as ctx:
            self.accept(row.intake_field_id)
        self.assertEqual(ctx.exception.code, "ALREADY_REVIEWED")
        with self.assertRaises(IntakeError) as ctx:
            self.reject(row.intake_field_id, reason="berubah pikiran")
        self.assertEqual(ctx.exception.code, "ALREADY_REVIEWED")
        stored = self.field_row(row.intake_field_id)
        self.assertEqual(stored["status"], STATUS_ACCEPTED)
        self.assertIsNone(stored["rejection_reason"])

        rejected = self.accepted_field()
        self.reject(rejected.intake_field_id)
        with self.assertRaises(IntakeError) as ctx:
            self.accept(rejected.intake_field_id)
        self.assertEqual(ctx.exception.code, "ALREADY_REVIEWED")

    # 32 ------------------------------------------------------------- #
    def test_invalid_decision_and_unknown_field_id(self):
        row = self.accepted_field()
        with self.assertRaises(IntakeError) as ctx:
            self.svc.review(
                intake_field_id=row.intake_field_id,
                decision="MAYBE",
                reviewer="agent-1",
                reviewer_role="sales",
                at=self.clock.now(),
            )
        self.assertEqual(ctx.exception.code, "INVALID_REVIEW_DECISION")
        self.assertEqual(self.field_row(row.intake_field_id)["status"], STATUS_PROPOSED)
        self.assertEqual(DECISIONS, ("ACCEPT", "REJECT"))

        with self.assertRaises(IntakeError) as ctx:
            self.accept("ifld-nope")
        self.assertEqual(ctx.exception.code, "INTAKE_FIELD_NOT_FOUND")
        with self.assertRaises(IntakeError) as ctx:
            self.svc.effective_status("ifld-nope")
        self.assertEqual(ctx.exception.code, "INTAKE_FIELD_NOT_FOUND")

    # 33 ------------------------------------------------------------- #
    def test_reviewer_may_review_their_own_proposal_but_is_recorded(self):
        _, rows = self.propose_message(MESSAGE_A)
        by_name = {row.field_name: row for row in rows}
        first = self.accept(by_name["adults"].intake_field_id, reviewer="agent-1", reviewer_role="sales")
        second = self.reject(
            by_name["arrival_time"].intake_field_id,
            reviewer="agent-1",
            reviewer_role="sales",
            reason="jam masih mungkin berubah",
        )
        self.assertEqual(first.reviewed_by, "agent-1")
        self.assertEqual(second.reviewed_by, "agent-1")
        self.assertEqual(second.status, STATUS_REJECTED)
        self.assertEqual(self.svc.accepted_facts(self.case_id), {"adults": 2})


class DerivedViewTests(IntakeBase):
    """``accepted_facts`` / ``pending`` / ``unknown_required`` / ``validate_facts``."""

    def mixed_review(self):
        _, rows = self.propose_message(MESSAGE_A)
        by_name = {row.field_name: row for row in rows}
        for name in ("check_in", "check_out", "adults"):
            self.accept(by_name[name].intake_field_id)
        self.reject(by_name["arrival_time"].intake_field_id, reason="jam belum pasti")
        return by_name

    # 34 ------------------------------------------------------------- #
    def test_accepted_facts_never_contains_proposed_or_rejected(self):
        by_name = self.mixed_review()
        got = self.svc.accepted_facts(self.case_id)
        self.assertEqual(got, {"check_in": date(2026, 10, 12), "check_out": date(2026, 10, 14), "adults": 2})
        self.assertNotIn("arrival_time", got)
        for name in ("children", "infants", "area_preference", "budget_minor", "currency", "purpose", "special_requests", "flexible_dates"):
            with self.subTest(field=name):
                self.assertNotIn(name, got)
        self.assertIsInstance(got["check_in"], date)
        self.assertIsInstance(got["adults"], int)
        self.assertEqual(self.count("intake_fields", "WHERE status = 'ACCEPTED'"), 3)
        self.assertEqual(by_name["currency"].status, STATUS_PROPOSED)
        self.assertEqual(self.svc.accepted_facts("case-002"), {})

    # 35 ------------------------------------------------------------- #
    def test_accepted_facts_converts_to_python_types(self):
        _, rows = self.propose_message(MESSAGE_B)
        by_name = {row.field_name: row for row in rows}
        for name in ("check_in", "check_out", "adults", "budget_minor", "currency", "flexible_dates"):
            self.accept(by_name[name].intake_field_id)
        got = self.svc.accepted_facts(self.case_id)
        self.assertEqual(
            got,
            {
                "check_in": date(2026, 11, 5),
                "check_out": date(2026, 11, 10),
                "adults": 3,
                "budget_minor": 1500000,
                "currency": "IDR",
                "flexible_dates": True,
            },
        )
        self.assertIsInstance(got["budget_minor"], int)
        self.assertIs(got["flexible_dates"], True)
        self.assertIsInstance(got["currency"], str)
        self.assertEqual(validate_facts(got), ())

    # 36 ------------------------------------------------------------- #
    def test_pending_lists_only_proposed_fields_in_field_specs_order(self):
        # The call is for its effect: it stores the proposal and its field rows.
        self.mixed_review()
        pending = self.svc.pending(self.case_id)
        names = [row.field_name for row in pending]
        self.assertEqual(
            names,
            ["children", "area_preference", "budget_minor", "currency", "purpose", "special_requests"],
        )
        self.assertEqual(names, [name for name in FIELD_SPECS if name in names])
        for row in pending:
            self.assertEqual(row.status, STATUS_PROPOSED)
        self.assertNotIn("adults", names)
        self.assertNotIn("arrival_time", names)
        self.assertEqual([row.field_name for row in self.svc.pending("case-002")], [])
        # Reviewing the rest empties the queue.
        for row in pending:
            self.accept(row.intake_field_id)
        self.assertEqual(self.svc.pending(self.case_id), [])

    # 37 ------------------------------------------------------------- #
    def test_unknown_required_follows_field_specs_order(self):
        _, rows = self.propose_message(MESSAGE_A)
        self.assertEqual(self.svc.unknown_required(self.case_id), ("check_in", "check_out", "adults"))
        by_name = {row.field_name: row for row in rows}
        self.accept(by_name["check_in"].intake_field_id)
        self.assertEqual(self.svc.unknown_required(self.case_id), ("check_out", "adults"))
        self.reject(by_name["adults"].intake_field_id, reason="jumlah tamu masih berubah")
        self.assertEqual(self.svc.unknown_required(self.case_id), ("check_out", "adults"))
        self.accept(by_name["check_out"].intake_field_id)
        self.assertEqual(self.svc.unknown_required(self.case_id), ("adults",))
        self.assertEqual(self.svc.unknown_required("case-002"), REQUIRED_FIELDS)

    # 38 ------------------------------------------------------------- #
    def test_validate_facts_reports_cross_field_violations(self):
        self.assertEqual(validate_facts({}), ())
        self.assertEqual(
            validate_facts(
                {
                    "check_in": date(2026, 10, 12),
                    "check_out": date(2026, 10, 11),
                    "adults": 0,
                    "infants": 3,
                    "budget_minor": 0,
                    "currency": "RUPIAH1",
                }
            ),
            (
                "CHECK_OUT_NOT_AFTER_CHECK_IN",
                "ADULTS_BELOW_MINIMUM",
                "INFANTS_EXCEED_ADULTS",
                "BUDGET_NOT_POSITIVE",
                "INVALID_CURRENCY",
            ),
        )
        # Same-day checkout is not a stay.
        self.assertEqual(
            validate_facts({"check_in": date(2026, 10, 12), "check_out": date(2026, 10, 12)}),
            ("CHECK_OUT_NOT_AFTER_CHECK_IN",),
        )
        # Infants must fit inside the adult count; missing values are not violations.
        self.assertEqual(validate_facts({"adults": 2, "infants": 2}), ())
        self.assertEqual(validate_facts({"adults": 2, "infants": 3}), ("INFANTS_EXCEED_ADULTS",))
        self.assertEqual(validate_facts({"currency": "idr"}), ("INVALID_CURRENCY",))
        self.assertEqual(validate_facts({"budget_minor": -5}), ("BUDGET_NOT_POSITIVE",))

    # 39 ------------------------------------------------------------- #
    def test_validate_facts_accepts_a_consistent_request(self):
        self.assertEqual(
            validate_facts(
                {
                    "check_in": date(2026, 10, 12),
                    "check_out": date(2026, 10, 14),
                    "adults": 2,
                    "children": 1,
                    "infants": 0,
                    "budget_minor": 2500000,
                    "currency": "IDR",
                    "purpose": "leisure",
                    "flexible_dates": False,
                }
            ),
            (),
        )


class SchemaGuardTests(IntakeBase):
    """The DB is the last line of defence; the service must not fight it."""

    # 40 ------------------------------------------------------------- #
    def test_db_check_rejects_non_proposed_status_without_reviewer(self):
        _, rows = self.propose_fields([ProposedField("adults", 2, 90, 0, 4)])
        field_id = rows[0].intake_field_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE intake_fields SET status = 'ACCEPTED' WHERE intake_field_id = ?", (field_id,))
        self.assertEqual(self.field_row(field_id)["status"], STATUS_PROPOSED)

        with self.assertRaises(sqlite3.IntegrityError):
            # reviewer set but never timestamped: still not a review.
            self.conn.execute(
                "UPDATE intake_fields SET status = 'REJECTED', reviewed_by = 'agent-1', rejection_reason = 'nope'"
                " WHERE intake_field_id = ?",
                (field_id,),
            )
        self.assertEqual(self.field_row(field_id)["status"], STATUS_PROPOSED)

        with self.assertRaises(sqlite3.IntegrityError):
            # a rejected field needs a real reason
            self.conn.execute(
                "UPDATE intake_fields SET status = 'REJECTED', reviewed_by = 'agent-1', reviewed_at = ?,"
                " rejection_reason = 'x' WHERE intake_field_id = ?",
                (NOW, field_id),
            )
        self.assertEqual(self.field_row(field_id)["status"], STATUS_PROPOSED)

    # 41 ------------------------------------------------------------- #
    def test_service_never_writes_a_half_reviewed_row(self):
        _, rows = self.propose_message(MESSAGE_A)
        by_name = {row.field_name: row for row in rows}
        field_id = by_name["adults"].intake_field_id

        # A status change without the reviewer columns is refused before SQL runs.
        with self.assertRaises(IntakeError) as ctx:
            self.svc.update_field(field_id, changes={"status": STATUS_ACCEPTED})
        self.assertEqual(ctx.exception.code, "INVALID_FIELD_VALUE")
        with self.assertRaises(IntakeError) as ctx:
            self.svc.update_field(field_id, changes={"status": STATUS_REJECTED})
        self.assertEqual(ctx.exception.code, "INVALID_FIELD_VALUE")
        stored = self.field_row(field_id)
        self.assertEqual(stored["status"], STATUS_PROPOSED)
        self.assertIsNone(stored["reviewed_by"])
        self.assertIsNone(stored["reviewed_at"])
        self.assertEqual(self.count("intake_fields", "WHERE status <> 'PROPOSED'"), 0)

        # And a successful review is all-or-nothing.
        reviewed = self.accept(field_id)
        self.assertEqual(reviewed.status, STATUS_ACCEPTED)
        self.assertEqual(self.field_row(field_id)["reviewed_by"], "agent-1")
        self.assertEqual(self.field_row(field_id)["reviewed_at"], NOW)


if __name__ == "__main__":
    unittest.main()
