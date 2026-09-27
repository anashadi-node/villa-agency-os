"""Tests for ``src/messages.py`` — grounded outbound drafts that never send.

The module under test is deliberately unable to reach a network: it records that a
*human* sent a message, and every number in a body has to be traceable to a row in
the database before that is allowed. The tests below pin that down, including the
refusals: unknown templates, invalid channels and locales, empty or identifying
recipients, unbound placeholders, ungrounded amounts, gate refusals, self-approval,
a missing ``external_ref`` and any attempt to rewrite a body after creation.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # pragma: no cover - mirrors tests/conftest.py
    sys.path.insert(0, str(SRC))

import messages as MSG  # noqa: E402
from primitives import (  # noqa: E402
    DomainError,
    FrozenClock,
    IdFactory,
    sha256_hex,
)
from schema_loader import connect_sqlite  # noqa: E402
from seed_support import SeedMixin, stay  # noqa: E402

NOW = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
CHECK_IN, CHECK_OUT = stay(0)  # 2026-10-01 .. 2026-10-05, four nights
SENDER = "agent_ana"
OWNER = "owner_anas"
CASE_ID = "case_0001"
BOOKING_ID = "book_0001"
UNIT_ID = "unit_0001"
PROPERTY_ID = "prop_0001"
POLICY_ID = "policy_v1"
INVOICE_ID = "inv_0001"
HOLD_ID = "hold_0001"
QUOTE_ID = "quote_0001"
REASON = "jawab permintaan ketersediaan unit"
SEND_NOTE = "setuju, silakan dikirim"
SEND_REASON = "minta izin kirim pesan ke tamu"
DRAFT_ACTIONS = {"message.draft", "message.submit", "message.approve", "message.sent",
                 "send_outbound_message"}

#: Nothing outside the standard library and this repository may be imported by the
#: module: no SMTP client, no HTTP client, no messaging SDK.
ALLOWED_IMPORTS = {
    "__future__", "approvals", "audit", "contextlib", "dataclasses", "datetime",
    "gates", "json", "primitives", "re", "typing", "unicodedata",
}


def rp(minor: int) -> str:
    """IDR has no minor exponent, so 1_000_000 minor is ``Rp 1.000.000``."""
    return "Rp " + f"{minor:,}".replace(",", ".")


def idem(draft_id: str) -> str:
    return f"idem-messages-{draft_id}"


class MessageTestCase(unittest.TestCase, SeedMixin):
    """Real schema in memory, frozen clock, deterministic ids, seeded case."""

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.clock = FrozenClock(NOW)
        self.ids = IdFactory(deterministic=True)
        self.inventory()
        self.case()
        self.booking()
        self.invoice()
        self.hold()
        self.quote()
        self.svc = MSG.MessageService(self.conn, clock=self.clock, ids=self.ids)

    # ------------------------------------------------------------------ rows

    def q(self, sql: str, *params) -> list[tuple]:
        return [tuple(row) for row in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, *params):
        row = self.conn.execute(sql, params).fetchone()
        return tuple(row) if row is not None else None

    def scalar(self, sql: str, *params):
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row is not None else None

    def count(self, table: str) -> int:
        return int(self.scalar(f"SELECT COUNT(*) FROM {table}"))

    def raw_row(self, draft_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM message_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        self.assertIsNotNone(row, f"draft {draft_id} is not in the database")
        return dict(row)

    def set_body(self, draft_id: str, body: str) -> None:
        """Tamper with a stored body the way a rogue script would."""
        self.conn.execute("UPDATE message_drafts SET body_text = ? WHERE draft_id = ?",
                          (body, draft_id))

    def audit_actions(self, draft_id: str) -> list[str]:
        return [row[0] for row in self.q(
            "SELECT action FROM audit_events WHERE entity_id = ? ORDER BY seq", draft_id)]

    # -------------------------------------------------------------- fixtures

    def quote(self) -> None:
        """A priced quote, so ``quote_offer`` has a real row to cite."""
        self._insert(
            "rate_cards", rate_card_id="rc_msg_0001", property_id=PROPERTY_ID,
            unit_scope=UNIT_ID, name="Tarif publik 2026", version=1,
            effective_from=CHECK_IN, effective_to=CHECK_OUT, currency="IDR",
            weekly_discount_pct="0", monthly_discount_pct="0",
            source_doc_ref="rate-card-2026.pdf", approved_by=OWNER,
            created_at=NOW.isoformat(),
        )
        self._insert(
            "quotes", quote_id=QUOTE_ID, property_id=PROPERTY_ID, unit_id=UNIT_ID,
            check_in=CHECK_IN, check_out=CHECK_OUT, nights=4, adults=2, children=0,
            infants=0, currency="IDR", policy_version_id=POLICY_ID,
            policy_fingerprint=sha256_hex("policy_v1"), rate_card_id="rc_msg_0001",
            accommodation_minor=4_800_000, extra_guest_minor=0, cleaning_minor=200_000,
            extras_minor=0, discount_minor=0, subtotal_minor=5_000_000,
            service_fee_minor=0, tax_minor=550_000, total_minor=5_550_000,
            security_deposit_minor=1_000_000, lines="[]",
            fingerprint=sha256_hex("quote_0001"), status="SENT", warnings="[]",
            created_at=(NOW - timedelta(days=1)).isoformat(),
            expires_at="2026-10-15T08:00:00+00:00",
        )

    def intake_field(self, value: str = "Agus Santoso", field_id: str = "ifld_msg_0001") -> str:
        """An accepted intake fact: the canonical source for a guest detail."""
        self._insert(
            "case_messages", case_message_id="cmsg_msg_0001", case_id=CASE_ID,
            direction="INBOUND", channel="website", sender_ref="guest_0001",
            received_at=NOW.isoformat(), content_class="P1",
            body_redacted="Nama saya Agus Santoso",
            body_sha256=sha256_hex("Nama saya Agus Santoso"), untrusted=1,
            created_at=NOW.isoformat(),
        )
        self._insert(
            "intake_proposals", proposal_id="prop_msg_0001", case_id=CASE_ID,
            source_message_id="cmsg_msg_0001", extractor="intake.rule.v1", method="rule",
            produced_at=NOW.isoformat(), status="ACCEPTED", payload="{}",
            fingerprint=sha256_hex("prop_msg_0001"), created_at=NOW.isoformat(),
        )
        self._insert(
            "intake_fields", intake_field_id=field_id, proposal_id="prop_msg_0001",
            case_id=CASE_ID, field_name="guest_name", field_value=value,
            value_type="text", confidence=95, source_message_id="cmsg_msg_0001",
            status="ACCEPTED", value_sha256=sha256_hex(value),
            created_at=NOW.isoformat(), reviewed_by=OWNER, reviewed_at=NOW.isoformat(),
        )
        return field_id

    def bindings_for(self, template: str, **overrides) -> dict:
        b = MSG.Binding
        common = {
            "guest_name": b("Agus Santoso", "text", "case", CASE_ID),
            "unit_code": b(UNIT_ID, "text", "unit", UNIT_ID),
            "check_in": b(CHECK_IN, "date", "booking", BOOKING_ID),
            "check_out": b(CHECK_OUT, "date", "booking", BOOKING_ID),
        }
        if template == "availability_offer":
            data = dict(
                common,
                nights=b("4", "number", "booking", BOOKING_ID),
                nightly_rate=b(rp(1_500_000), "money", "policy_version", POLICY_ID),
                total_amount=b(rp(6_000_000), "money", "policy_version", POLICY_ID),
            )
        elif template == "quote_offer":
            amounts = self.one(
                "SELECT accommodation_minor, cleaning_minor, tax_minor, total_minor "
                "FROM quotes WHERE quote_id = ?", QUOTE_ID)
            data = dict(
                common,
                nights=b("4", "number", "quote", QUOTE_ID),
                accommodation_amount=b(rp(amounts[0]), "money", "quote", QUOTE_ID),
                cleaning_amount=b(rp(amounts[1]), "money", "quote", QUOTE_ID),
                tax_amount=b(rp(amounts[2]), "money", "quote", QUOTE_ID),
                total_amount=b(rp(amounts[3]), "money", "quote", QUOTE_ID),
                valid_until=b("2026-10-15", "date", "quote", QUOTE_ID),
            )
        elif template == "hold_confirmation":
            data = dict(common, hold_expires_at=b("2026-09-22", "date", "hold", HOLD_ID))
        elif template == "payment_reminder":
            data = {
                "guest_name": b("Agus Santoso", "text", "case", CASE_ID),
                "invoice_ref": b(INVOICE_ID, "text", "invoice", INVOICE_ID),
                "amount_due": b(rp(1_000_000), "money", "invoice", INVOICE_ID),
                "invoice_due": b("2026-09-20", "date", "invoice", INVOICE_ID),
                "payment_channel": b("transfer bank", "text", "policy_version", POLICY_ID),
                "account_name": b("PT Anantara Sakti", "text", "policy_version", POLICY_ID),
            }
        elif template == "arrival_instructions":
            data = {
                "guest_name": b("Agus Santoso", "text", "case", CASE_ID),
                "unit_code": b(UNIT_ID, "text", "unit", UNIT_ID),
                "property_name": b("Villa Anantara", "text", "property", PROPERTY_ID),
                "check_in_time": b("14:00", "time", "policy_version", POLICY_ID),
                "host_name": b("Ana", "text", "case", CASE_ID),
                "contact_channel": b("email", "text", "policy_version", POLICY_ID),
                "booking_ref": b(BOOKING_ID, "text", "booking", BOOKING_ID),
            }
        elif template == "complaint_ack":
            data = {
                "guest_name": b("Agus Santoso", "text", "case", CASE_ID),
                "issue_summary": b("kebersihan kolam", "text", "case", CASE_ID),
                "ticket_ref": b("tkt_msg_0001", "text", "case", CASE_ID),
                "owner_name": b("Ana", "text", "case", CASE_ID),
                "resolution_due": b("2026-09-25", "date", "policy_version", POLICY_ID),
                "contact_channel": b("email", "text", "policy_version", POLICY_ID),
            }
        else:
            data = dict(common)
        data.update(overrides)
        return data

    # ------------------------------------------------------------ shorthands

    def make(self, *, template: str = "availability_offer", channel: str = "email",
             recipient_ref: str = "guest_0001", created_by: str = SENDER,
             created_by_role: str = "agent", case_id: str = CASE_ID,
             booking_id: str | None = BOOKING_ID, reason: str = REASON,
             bindings: dict | None = None, **kwargs) -> MSG.MessageDraft:
        return self.svc.draft(
            case_id=case_id,
            template=template,
            channel=channel,
            recipient_ref=recipient_ref,
            bindings=self.bindings_for(template, **(bindings or {})),
            created_by=created_by,
            created_by_role=created_by_role,
            reason=reason,
            booking_id=booking_id,
            **kwargs,
        )

    def submit(self, draft: MSG.MessageDraft, **kwargs):
        params = dict(actor=draft.created_by, actor_role="agent", reason=SEND_REASON,
                      idempotency_key=idem(draft.draft_id))
        params.update(kwargs)
        return self.svc.submit_for_approval(draft.draft_id, **params)

    def approved(self, *, approver: str = OWNER, approver_role: str = "owner", **kwargs):
        draft = self.make(**kwargs)
        self.submit(draft)
        self.svc.approve(draft.draft_id, approver=approver, approver_role=approver_role,
                         note=SEND_NOTE)
        return self.svc.get(draft.draft_id)

    def sent(self, **kwargs):
        draft = self.approved(**kwargs)
        self.svc.mark_sent(draft.draft_id, actor=SENDER, actor_role="agent",
                           external_ref="wa-out-0001")
        return self.svc.get(draft.draft_id)

    def assertRefused(self, code: str, fn, *args, **kwargs):
        with self.assertRaises(DomainError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(code, caught.exception.code, caught.exception)
        return caught.exception


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
class TemplateTests(MessageTestCase):
    def test_all_six_templates_exist(self):
        self.assertEqual(
            set(MSG.TEMPLATES),
            {"availability_offer", "quote_offer", "hold_confirmation",
             "payment_reminder", "arrival_instructions", "complaint_ack"},
        )

    def test_every_template_has_named_placeholders(self):
        for name in MSG.TEMPLATES:
            with self.subTest(template=name):
                placeholders = MSG.template_placeholders(name)
                self.assertTrue(placeholders, name)
                for placeholder in placeholders:
                    self.assertRegex(placeholder, r"^[a-z][a-z0-9_]*$")

    def test_templates_carry_no_literal_digits(self):
        """A template constant must not smuggle a number past grounding."""
        for name, text in MSG.TEMPLATES.items():
            with self.subTest(template=name):
                self.assertEqual([], [c for c in text if c.isdigit()], text)

    def test_unknown_template_is_refused(self):
        self.assertRefused("UNKNOWN_TEMPLATE", MSG.render_template, "villa_brochure", {})
        self.assertRefused("UNKNOWN_TEMPLATE", MSG.template_placeholders, "villa_brochure")
        self.assertRefused("UNKNOWN_TEMPLATE", self.make, template="villa_brochure")

    def test_unknown_template_writes_no_row(self):
        with self.assertRaises(DomainError):
            self.make(template="villa_brochure")
        self.assertEqual(0, self.count("message_drafts"))

    def test_unbound_placeholder_is_refused(self):
        error = self.assertRefused(
            "UNBOUND_PLACEHOLDER", MSG.render_template, "availability_offer",
            {"guest_name": MSG.Binding("Agus", "text", "case", CASE_ID)},
        )
        self.assertIn("nights", str(error))

    def test_unbound_placeholder_writes_no_row(self):
        with self.assertRaises(DomainError):
            self.make(bindings={"nights": MSG.Binding("4", "text", "case", CASE_ID),
                                "total_amount": MSG.Binding("Rp 1", "text", "case", CASE_ID)})
        self.assertEqual(0, self.count("message_drafts"))

    def test_extra_bindings_are_ignored_by_rendering(self):
        body, used = MSG.render_template(
            "arrival_instructions",
            self.bindings_for("arrival_instructions",
                              spare=MSG.Binding("x", "text", "case", CASE_ID)),
        )
        self.assertIn(UNIT_ID, body)
        self.assertEqual(set(MSG.template_placeholders("arrival_instructions")),
                         {binding.placeholder for binding in used})

    def test_rendering_is_repeatable_without_stray_braces(self):
        bindings = self.bindings_for("complaint_ack")
        first = MSG.render_template("complaint_ack", bindings)[0]
        second = MSG.render_template("complaint_ack", bindings)[0]
        self.assertEqual(first, second)
        self.assertNotIn("{", first)
        self.assertNotIn("}", first)
        self.assertNotIn("  ", first)

    def test_unnamed_brace_in_a_template_is_refused(self):
        with mock.patch.dict(MSG.TEMPLATES, {"broken": "Halo {guest_name} {}"}):
            self.assertRefused(
                "INVALID_PLACEHOLDER", MSG.render_template, "broken",
                {"guest_name": MSG.Binding("Agus", "text", "case", CASE_ID)},
            )

    def test_body_equals_the_template_rendered_from_stored_bindings(self):
        """The stored body must be reproducible from the stored bindings."""
        for template in MSG.TEMPLATES:
            with self.subTest(template=template):
                draft = self.make(template=template)
                self.assertEqual(MSG.render_template(template, draft.bindings)[0],
                                 draft.body_text)

    def test_stored_bindings_keep_their_placeholder_names(self):
        draft = self.make()
        self.assertEqual(set(MSG.template_placeholders("availability_offer")),
                         {binding.placeholder for binding in draft.bindings})

    def test_every_template_renders_for_every_channel(self):
        for channel in MSG.CHANNELS:
            with self.subTest(channel=channel):
                self.assertTrue(self.make(channel=channel).body_text)


# --------------------------------------------------------------------------- #
# Claim extraction
# --------------------------------------------------------------------------- #
class ClaimExtractionTests(unittest.TestCase):
    def texts(self, body: str) -> list[str]:
        return [claim.text for claim in MSG.extract_claims(body)]

    def test_money_claim_drops_the_sentence_full_stop(self):
        self.assertEqual(["Rp 5.000.000"], self.texts("harga Rp 5.000.000."))

    def test_money_claim_keeps_internal_separators(self):
        self.assertEqual(["Rp 1.500.000"], self.texts("tarif Rp 1.500.000 per malam"))

    def test_dates_are_claims(self):
        self.assertEqual(["2026-10-05"], self.texts("tinggal sampai 2026-10-05 ya"))

    def test_times_are_claims(self):
        self.assertEqual(["14:00"], self.texts("check-in mulai 14:00 oleh Ana"))

    def test_percentages_are_claims(self):
        self.assertEqual(["10%"], self.texts("diskon 10% untuk minggu ini"))

    def test_bare_numbers_are_claims(self):
        self.assertEqual(["4"], self.texts("menginap 4 malam"))

    def test_money_is_not_split_into_numbers_and_percent(self):
        claims = MSG.extract_claims("total Rp 6.000.000 sampai 2026-10-05")
        self.assertEqual(["Rp 6.000.000", "2026-10-05"], [c.text for c in claims])
        self.assertEqual(["money", "date"], [c.kind for c in claims])

    def test_record_ids_are_not_claims(self):
        self.assertEqual([], self.texts("booking book_0001 di unit_0001, invoice inv_0001"))

    def test_prose_without_numbers_has_no_claims(self):
        self.assertEqual([], self.texts("Halo Agus, terima kasih atas pesannya."))

    def test_claims_are_ordered_and_non_overlapping(self):
        claims = MSG.extract_claims("Rp 5.000.000 untuk 4 malam sejak 2026-10-01")
        spans = [(c.start, c.end) for c in claims]
        self.assertEqual(sorted(spans), spans)
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(end, start)

    def test_claim_offsets_point_at_the_claim_text(self):
        body = "harga Rp 5.000.000 final"
        claim = MSG.extract_claims(body)[0]
        self.assertEqual("Rp 5.000.000", body[claim.start:claim.end])


# --------------------------------------------------------------------------- #
# Grounding
# --------------------------------------------------------------------------- #
class GroundingTests(MessageTestCase):
    def test_price_without_a_binding_is_ungrounded(self):
        result = MSG.ground("harga Rp 5.000.000", (), conn=self.conn)
        self.assertEqual(MSG.UNGROUNDED, result.status)
        self.assertEqual(["Rp 5.000.000"], [c.text for c in result.unmatched])

    def test_prose_without_numbers_is_grounded(self):
        self.assertEqual(MSG.GROUNDED,
                         MSG.ground("Halo Agus, terima kasih.", (), conn=self.conn).status)

    def test_grounding_without_a_connection_can_never_be_grounded(self):
        binding = MSG.Binding("Rp 5.000.000", "money", "invoice", INVOICE_ID)
        result = MSG.ground("harga Rp 5.000.000", [binding], conn=None)
        self.assertEqual(MSG.REVIEW_REQUIRED, result.status)

    def test_matching_binding_with_an_existing_source_is_grounded(self):
        binding = MSG.Binding("Rp 1.000.000", "money", "invoice", INVOICE_ID)
        result = MSG.ground("tagihan Rp 1.000.000", [binding], conn=self.conn)
        self.assertEqual(MSG.GROUNDED, result.status)
        self.assertEqual(("Rp 1.000.000",), result.matched)

    def test_source_id_must_exist_in_the_database(self):
        binding = MSG.Binding("Rp 1.000.000", "money", "invoice", "inv_does_not_exist")
        result = MSG.ground("tagihan Rp 1.000.000", [binding], conn=self.conn)
        self.assertEqual(MSG.UNGROUNDED, result.status)
        self.assertEqual(1, len(result.missing_sources))

    def test_binding_without_a_source_is_ungrounded(self):
        binding = MSG.Binding("Rp 5.000.000", "money")
        result = MSG.ground("harga Rp 5.000.000", [binding], conn=self.conn)
        self.assertEqual(MSG.UNGROUNDED, result.status)
        self.assertEqual(1, len(result.missing_sources))

    def test_unknown_source_type_is_refused(self):
        binding = MSG.Binding("Rp 5.000.000", "money", "screenshot", "x_1")
        self.assertRefused("UNKNOWN_SOURCE_TYPE", MSG.ground, "harga Rp 5.000.000",
                           [binding], conn=self.conn)

    def test_amount_of_the_wrong_kind_is_not_backed(self):
        binding = MSG.Binding("Rp 5.000.000", "number", "policy_version", POLICY_ID)
        self.assertEqual(MSG.UNGROUNDED,
                         MSG.ground("harga Rp 5.000.000", [binding], conn=self.conn).status)

    def test_text_binding_cannot_launder_an_amount(self):
        """A note that happens to contain an amount is not a source for it."""
        binding = MSG.Binding("kebocoran senilai Rp 5.000.000", "text", "case", CASE_ID)
        result = MSG.ground("kebocoran senilai Rp 5.000.000", [binding], conn=self.conn)
        self.assertEqual(MSG.UNGROUNDED, result.status)
        self.assertEqual(["Rp 5.000.000"], [c.text for c in result.unmatched])

    def test_bindings_must_be_binding_objects(self):
        self.assertRefused("UNSOURCED_BINDING", MSG.ground, "harga Rp 5.000.000",
                           {"amount": "Rp 5.000.000"}, conn=self.conn)

    def test_bindings_accept_a_plain_list(self):
        bindings = [MSG.Binding(CHECK_IN, "date", "booking", BOOKING_ID),
                    MSG.Binding("4", "number", "booking", BOOKING_ID)]
        result = MSG.ground(f"{CHECK_IN} selama 4 malam", bindings, conn=self.conn)
        self.assertEqual(MSG.GROUNDED, result.status)
        self.assertEqual((CHECK_IN, "4"), result.matched)

    def test_booking_invoice_hold_policy_case_unit_and_property_sources_resolve(self):
        for source_type, source_id in (
            ("booking", BOOKING_ID), ("invoice", INVOICE_ID), ("hold", HOLD_ID),
            ("policy_version", POLICY_ID), ("case", CASE_ID), ("unit", UNIT_ID),
            ("property", PROPERTY_ID),
        ):
            with self.subTest(source_type=source_type):
                binding = MSG.Binding("Rp 1.000.000", "money", source_type, source_id)
                self.assertTrue(MSG._source_exists(self.conn, binding))

    def test_quote_source_resolves(self):
        self.assertTrue(MSG._source_exists(
            self.conn, MSG.Binding(rp(5_550_000), "money", "quote", QUOTE_ID)))

    def test_intake_field_source_resolves(self):
        field_id = self.intake_field()
        self.assertTrue(MSG._source_exists(
            self.conn, MSG.Binding("Agus Santoso", "text", "intake_field", field_id)))

    def test_intake_field_value_mismatch_is_ungrounded(self):
        field_id = self.intake_field(value="Agus Santoso")
        binding = MSG.Binding("Budi", "text", "intake_field", field_id,
                              field_name="guest_name")
        self.assertEqual(MSG.UNGROUNDED,
                         MSG.ground("Halo Budi", [binding], conn=self.conn).status)

    def test_intake_field_value_match_is_grounded(self):
        field_id = self.intake_field(value="Agus Santoso")
        binding = MSG.Binding("Agus Santoso", "text", "intake_field", field_id,
                              field_name="guest_name")
        self.assertEqual(MSG.GROUNDED,
                         MSG.ground("Halo Agus Santoso", [binding], conn=self.conn).status)

    def test_grounding_replays_the_stored_draft(self):
        draft = self.make(template="payment_reminder")
        result = MSG.ground(draft.body_text, draft.bindings, conn=self.conn)
        self.assertEqual(MSG.GROUNDED, result.status)
        self.assertEqual(tuple(sorted(c.text for c in draft.claims)),
                         tuple(sorted(result.claims and [c.text for c in result.claims])))
        self.assertEqual(6, len(result.bindings))


# --------------------------------------------------------------------------- #
# Draft creation: grounding, gate and validation
# --------------------------------------------------------------------------- #
class DraftValidationTests(MessageTestCase):
    def ungrounded_draft(self) -> MSG.MessageDraft:
        """A real body claiming an amount that no binding sources."""
        return self.make(
            template="complaint_ack",
            bindings={"issue_summary": MSG.Binding(
                "kebocoran senilai Rp 5.000.000", "text", "case", CASE_ID)},
        )

    def gate_refused_draft(self) -> MSG.MessageDraft:
        return self.make(bindings={
            "guest_name": MSG.Binding("081234567890", "text", "case", CASE_ID)})

    def test_grounded_draft_is_stored_with_hash_and_gate_decision(self):
        draft = self.make()
        self.assertEqual(MSG.GROUNDED, draft.grounding)
        self.assertEqual(MSG.DRAFT, draft.status)
        self.assertEqual(sha256_hex(draft.body_text), draft.body_sha256)
        self.assertTrue(draft.gate_allowed)
        self.assertEqual("P1", draft.gate["classification"])

    def test_ungrounded_amount_is_stored_but_not_grounded(self):
        draft = self.ungrounded_draft()
        self.assertEqual(MSG.UNGROUNDED, draft.grounding)
        self.assertIn("Rp 5.000.000", [c.text for c in draft.claims])
        self.assertEqual("DRAFT", self.raw_row(draft.draft_id)["status"])

    def test_ungrounded_draft_cannot_be_submitted(self):
        self.assertRefused("UNGROUNDED_DRAFT", self.submit, self.ungrounded_draft())
        self.assertEqual([], self.q("SELECT * FROM approval_requests"))

    def test_ungrounded_draft_cannot_be_approved(self):
        draft = self.ungrounded_draft()
        self.assertRefused("UNGROUNDED_DRAFT", self.svc.approve, draft.draft_id,
                           approver=OWNER, approver_role="owner", note=SEND_NOTE)
        self.assertEqual("DRAFT", self.raw_row(draft.draft_id)["status"])

    def test_ungrounded_draft_cannot_be_sent(self):
        draft = self.ungrounded_draft()
        self.assertRefused("UNGROUNDED_DRAFT", self.svc.mark_sent, draft.draft_id,
                           actor=SENDER, actor_role="agent", external_ref="wa-1")

    def test_price_without_a_binding_never_reaches_approved(self):
        draft = self.ungrounded_draft()
        self.assertEqual(MSG.UNGROUNDED, draft.grounding)
        self.assertFalse(draft.is_sendable)
        with self.assertRaises(DomainError):
            self.svc.approve(draft.draft_id, approver=OWNER, approver_role="owner",
                             note=SEND_NOTE)
        self.assertNotEqual("APPROVED", self.raw_row(draft.draft_id)["status"])

    def test_missing_source_row_makes_the_draft_ungrounded(self):
        draft = self.make(bindings={
            "total_amount": MSG.Binding(rp(6_000_000), "money", "quote", "quote_missing")})
        self.assertEqual(MSG.UNGROUNDED, draft.grounding)

    def test_unknown_channel_is_refused(self):
        for channel in ("", "telegram", "EMAIL ", "sms", "whatsapp"):
            with self.subTest(channel=channel):
                self.assertRefused("INVALID_CHANNEL", self.make, channel=channel)
        self.assertEqual(0, self.count("message_drafts"))

    def test_empty_recipient_is_refused(self):
        for recipient in ("", "   ", None):
            with self.subTest(recipient=recipient):
                self.assertRefused("INVALID_RECIPIENT", self.make,
                                   recipient_ref=recipient)
        self.assertEqual(0, self.count("message_drafts"))

    def test_recipient_holding_direct_identifiers_is_refused(self):
        for recipient in ("agus@example.com", "+62 812 3456 7890", "081234567890"):
            with self.subTest(recipient=recipient):
                error = self.assertRefused("INVALID_RECIPIENT", self.make,
                                           recipient_ref=recipient)
                self.assertNotIn(recipient, str(error))

    def test_invalid_locale_is_refused(self):
        for locale in ("id", "indonesia", "xx", "", None):
            with self.subTest(locale=locale):
                self.assertRefused("INVALID_LOCALE", self.make, locale=locale)

    def test_unknown_case_is_refused(self):
        self.assertRefused("CASE_NOT_FOUND", self.make, case_id="case_9999")

    def test_unknown_booking_is_refused(self):
        self.assertRefused("BOOKING_NOT_FOUND", self.make, booking_id="book_9999")

    def test_blank_or_machine_author_is_refused(self):
        self.assertRefused("MISSING_FIELD", self.make, created_by="")
        for actor in ("system", "machine:1", "bot:2", "svc-x", MSG.SYSTEM_ACTOR):
            with self.subTest(actor=actor):
                self.assertRefused("INVALID_ACTOR", self.make, created_by=actor)
        self.assertEqual(0, self.count("message_drafts"))

    def test_unknown_role_is_refused(self):
        self.assertRefused("INVALID_ACTOR", self.make, created_by_role="robot")

    def test_body_longer_than_the_limit_is_refused(self):
        self.assertRefused("BODY_TOO_LONG", self.make, bindings={
            "guest_name": MSG.Binding("A" * 9000, "text", "case", CASE_ID)})
        self.assertEqual(0, self.count("message_drafts"))

    def test_every_number_in_the_body_is_sourced(self):
        draft = self.make()
        for binding in draft.bindings:
            if binding.kind in ("money", "date", "number", "time", "percent"):
                self.assertTrue(binding.source_type and binding.source_id, binding)

    def test_draft_records_its_author_and_timestamp(self):
        draft = self.make()
        stored = self.raw_row(draft.draft_id)
        self.assertEqual(SENDER, stored["created_by"])
        self.assertEqual(NOW.isoformat(), stored["created_at"])
        self.assertEqual(CASE_ID, stored["case_id"])
        self.assertEqual(BOOKING_ID, stored["booking_id"])

    def test_draft_creation_is_audited_without_the_raw_body(self):
        draft = self.make()
        self.assertIn("message.draft", self.audit_actions(draft.draft_id))
        payload = "".join(str(row) for row in self.q(
            "SELECT after_state FROM audit_events WHERE entity_id = ?", draft.draft_id))
        self.assertNotIn(draft.body_text, payload)
        self.assertIn(draft.body_sha256, payload)


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #
class GateTests(MessageTestCase):
    def gate_refused_draft(self) -> MSG.MessageDraft:
        return self.make(bindings={
            "guest_name": MSG.Binding("081234567890", "text", "case", CASE_ID)})

    def test_gate_decision_is_recorded_on_the_draft(self):
        draft = self.make()
        payload = json.loads(draft.gate_decision)
        self.assertEqual("classification", payload["decision"]["gate"])
        self.assertEqual("P1", payload["classification"])
        self.assertEqual(MSG.CHANNEL_DESTINATIONS["email"], payload["destination"])
        self.assertEqual("OK", payload["decision"]["code"])

    def test_destination_follows_the_channel(self):
        for channel, destination in MSG.CHANNEL_DESTINATIONS.items():
            with self.subTest(channel=channel):
                draft = self.make(channel=channel)
                self.assertEqual(destination,
                                 json.loads(draft.gate_decision)["destination"])

    def test_body_with_a_direct_identifier_is_refused_by_the_gate(self):
        draft = self.gate_refused_draft()
        self.assertTrue(MSG.contains_direct_identifiers(draft.body_text))
        self.assertFalse(draft.gate_allowed)
        self.assertEqual("MISCLASSIFIED_CONTENT", draft.gate["decision"]["code"])
        self.assertFalse(draft.is_sendable)

    def test_card_data_in_a_body_is_refused_by_the_gate(self):
        draft = self.make(template="complaint_ack", bindings={
            "issue_summary": MSG.Binding("kartu kredit 4111 1111 1111 1111", "text",
                                         "case", CASE_ID)})
        self.assertFalse(draft.gate_allowed)

    def test_gate_refused_draft_cannot_be_submitted(self):
        self.assertRefused("GATE_REFUSED", self.submit, self.gate_refused_draft())
        self.assertEqual([], self.q("SELECT * FROM approval_requests"))

    def test_gate_refused_draft_cannot_be_approved(self):
        draft = self.gate_refused_draft()
        self.assertRefused("GATE_REFUSED", self.svc.approve, draft.draft_id,
                           approver=OWNER, approver_role="owner", note=SEND_NOTE)
        self.assertEqual("DRAFT", self.raw_row(draft.draft_id)["status"])

    def test_gate_refused_draft_cannot_be_sent(self):
        draft = self.gate_refused_draft()
        self.assertRefused("GATE_REFUSED", self.svc.mark_sent, draft.draft_id,
                           actor=SENDER, actor_role="agent", external_ref="wa-1")

    def test_gate_refusal_is_audited_without_the_identifier(self):
        draft = self.gate_refused_draft()
        actions = self.audit_actions(draft.draft_id)
        self.assertTrue(set(actions) <= DRAFT_ACTIONS, actions)
        self.assertIn("message.draft", actions)
        snapshot = "".join(str(row) for row in self.q(
            "SELECT after_state, evidence_ref, reason FROM audit_events "
            "WHERE entity_id = ?", draft.draft_id))
        self.assertNotIn("081234567890", snapshot)


# --------------------------------------------------------------------------- #
# Human approval flow
# --------------------------------------------------------------------------- #
class ApprovalFlowTests(MessageTestCase):
    def test_submit_creates_a_message_send_approval_request(self):
        draft = self.make()
        approval = self.submit(draft)
        self.assertEqual("message_send", approval.subject_type)
        self.assertEqual(draft.draft_id, approval.subject_id)
        self.assertEqual("PENDING", approval.status)
        self.assertEqual("manager", approval.required_role)
        self.assertEqual(
            ("message_send", draft.draft_id, "PENDING", SENDER),
            self.one("SELECT subject_type, subject_id, status, requested_by "
                     "FROM approval_requests WHERE approval_id = ?", approval.approval_id),
        )

    def test_submit_does_not_fake_an_approval_status(self):
        draft = self.make()
        self.submit(draft)
        stored = self.raw_row(draft.draft_id)
        self.assertEqual("PENDING_APPROVAL", stored["status"])
        self.assertIsNone(stored["approved_by"])
        self.assertIsNone(stored["approved_at"])
        self.assertIsNone(stored["approval_id"])
        self.assertNotEqual("APPROVED", stored["status"])

    def test_submit_is_idempotent_for_the_same_key(self):
        draft = self.make()
        first = self.submit(draft)
        second = self.submit(draft)
        self.assertEqual(first.approval_id, second.approval_id)
        self.assertEqual(1, self.count("approval_requests"))

    def test_draft_cannot_be_approved_before_it_is_submitted(self):
        draft = self.make()
        self.assertRefused("DRAFT_NOT_PENDING", self.svc.approve, draft.draft_id,
                           approver=OWNER, approver_role="owner", note=SEND_NOTE)

    def test_approval_by_the_author_is_refused(self):
        draft = self.make()
        self.submit(draft)
        self.assertRefused("SELF_APPROVAL_FORBIDDEN", self.svc.approve, draft.draft_id,
                           approver=SENDER, approver_role="owner", note=SEND_NOTE)
        self.assertEqual("PENDING_APPROVAL", self.raw_row(draft.draft_id)["status"])

    def test_machine_actor_cannot_approve(self):
        draft = self.make()
        self.submit(draft)
        self.assertRefused("INVALID_ACTOR", self.svc.approve, draft.draft_id,
                           approver=MSG.SYSTEM_ACTOR, approver_role="owner",
                           note=SEND_NOTE)

    def test_approval_by_another_human_sets_approved(self):
        draft = self.make()
        approval = self.submit(draft)
        approved = self.svc.approve(draft.draft_id, approver=OWNER,
                                    approver_role="owner", note=SEND_NOTE)
        self.assertEqual("APPROVED", approved.status)
        self.assertEqual(OWNER, approved.approved_by)
        self.assertEqual(NOW.isoformat(), approved.approved_at)
        self.assertEqual(approval.approval_id, self.raw_row(draft.draft_id)["approval_id"])

    def test_status_stays_pending_until_a_human_decides(self):
        draft = self.make()
        approval = self.submit(draft)
        self.assertEqual("PENDING", self.scalar(
            "SELECT status FROM approval_requests WHERE approval_id = ?",
            approval.approval_id))
        self.assertEqual("PENDING_APPROVAL", self.raw_row(draft.draft_id)["status"])
        self.assertEqual([draft.draft_id], [d.draft_id for d in self.svc.pending()])

    def test_role_below_the_required_rank_cannot_approve(self):
        draft = self.make()
        self.submit(draft)
        with self.assertRaises(DomainError):
            self.svc.approve(draft.draft_id, approver="agent_budi", approver_role="agent",
                             note=SEND_NOTE)
        self.assertEqual("PENDING_APPROVAL", self.raw_row(draft.draft_id)["status"])

    def test_rejection_closes_the_draft_without_sending(self):
        draft = self.make()
        self.submit(draft)
        rejected = self.svc.reject(draft.draft_id, approver=OWNER, approver_role="owner",
                                   note="tamu belum konfirmasi tanggal")
        self.assertEqual("REJECTED", rejected.status)
        self.assertRefused("DRAFT_NOT_APPROVED", self.svc.mark_sent, draft.draft_id,
                           actor=SENDER, actor_role="agent", external_ref="wa-1")

    def test_second_approval_decision_is_refused(self):
        draft = self.make()
        self.submit(draft)
        self.svc.approve(draft.draft_id, approver=OWNER, approver_role="owner",
                         note=SEND_NOTE)
        self.assertRefused("DRAFT_NOT_PENDING", self.svc.approve, draft.draft_id,
                           approver=OWNER, approver_role="owner", note=SEND_NOTE)

    def test_approval_flow_is_audited(self):
        draft = self.make()
        self.submit(draft)
        self.svc.approve(draft.draft_id, approver=OWNER, approver_role="owner",
                         note=SEND_NOTE)
        actions = self.audit_actions(draft.draft_id)
        self.assertIn("message.submit", actions)
        self.assertIn("message.approve", actions)


# --------------------------------------------------------------------------- #
# Sending (recorded, never performed)
# --------------------------------------------------------------------------- #
class SendingTests(MessageTestCase):
    def test_mark_sent_requires_an_external_ref(self):
        draft = self.approved()
        for ref in (None, "", "   "):
            with self.subTest(ref=ref):
                self.assertRefused("MISSING_EXTERNAL_REF", self.svc.mark_sent,
                                   draft.draft_id, actor=SENDER, actor_role="agent",
                                   external_ref=ref)
        self.assertEqual("APPROVED", self.raw_row(draft.draft_id)["status"])

    def test_mark_sent_records_the_human_and_the_reference(self):
        draft = self.sent()
        self.assertEqual("SENT", draft.status)
        self.assertEqual(SENDER, draft.sent_by)
        self.assertEqual(NOW.isoformat(), draft.sent_at)
        self.assertEqual("wa-out-0001", draft.external_ref)

    def test_mark_sent_before_approval_is_refused(self):
        draft = self.make()
        self.assertRefused("DRAFT_NOT_APPROVED", self.svc.mark_sent, draft.draft_id,
                           actor=SENDER, actor_role="agent", external_ref="wa-1")

    def test_mark_sent_after_only_a_submission_is_refused(self):
        draft = self.make()
        self.submit(draft)
        self.assertRefused("DRAFT_NOT_APPROVED", self.svc.mark_sent, draft.draft_id,
                           actor=SENDER, actor_role="agent", external_ref="wa-1")

    def test_machine_actor_cannot_mark_sent(self):
        draft = self.approved()
        self.assertRefused("INVALID_ACTOR", self.svc.mark_sent, draft.draft_id,
                           actor=MSG.SYSTEM_ACTOR, actor_role="owner",
                           external_ref="wa-1")

    def test_roles_outside_agent_and_owner_cannot_mark_sent(self):
        draft = self.approved()
        for role in ("system", "manager", "guest", "", "admin"):
            with self.subTest(role=role):
                self.assertRefused("INVALID_SENDER_ROLE", self.svc.mark_sent,
                                   draft.draft_id, actor="someone_else",
                                   actor_role=role, external_ref="wa-1")

    def test_owner_may_record_a_sent_message(self):
        draft = self.approved()
        sent = self.svc.mark_sent(draft.draft_id, actor=OWNER, actor_role="owner",
                                  external_ref="email-out-0001")
        self.assertEqual("SENT", sent.status)
        self.assertEqual(OWNER, sent.sent_by)

    def test_second_send_is_refused(self):
        draft = self.sent()
        self.assertRefused("DRAFT_NOT_APPROVED", self.svc.mark_sent, draft.draft_id,
                           actor=SENDER, actor_role="agent", external_ref="wa-out-0002")

    def test_sending_is_audited(self):
        draft = self.sent()
        self.assertIn("message.sent", self.audit_actions(draft.draft_id))

    def test_sending_writes_no_outbound_case_message(self):
        """The module records a human hand-off; it does not deliver anything."""
        before = self.count("case_messages")
        self.sent()
        self.assertEqual(before, self.count("case_messages"))

    def test_module_imports_nothing_that_can_transport_a_message(self):
        tree = ast.parse((SRC / "messages.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(set(), imported - ALLOWED_IMPORTS)
        for token in ("smtplib", "http.client", "urllib.request", "socket"):
            self.assertNotIn(f"import {token}", (SRC / "messages.py").read_text())
            self.assertNotIn(f"from {token}", (SRC / "messages.py").read_text())


# --------------------------------------------------------------------------- #
# Immutability and tamper detection
# --------------------------------------------------------------------------- #
class ImmutabilityTests(MessageTestCase):
    def tamper(self, draft: MSG.MessageDraft) -> str:
        self.set_body(draft.draft_id, draft.body_text + " Bonus Rp 99.000.000.")
        return self.raw_row(draft.draft_id)["body_text"]

    def test_module_offers_no_body_rewrite(self):
        draft = self.make()
        self.assertRefused("BODY_IMMUTABLE", self.svc.update_body, draft.draft_id,
                           "body baru", actor=SENDER, actor_role="agent")
        self.assertEqual(draft.body_text, self.raw_row(draft.draft_id)["body_text"])

    def test_no_public_method_can_rewrite_a_body(self):
        for name in ("set_body", "edit_body", "rewrite_body", "patch_body", "update_text"):
            self.assertFalse(hasattr(MSG.MessageService, name), name)
        self.assertEqual([], [name for name in dir(MSG.MessageService)
                              if name.startswith(("set_", "edit_", "rewrite_"))])

    def test_tampering_in_the_database_is_detected_by_verify(self):
        draft = self.make()
        self.tamper(draft)
        report = self.svc.verify(draft.draft_id)
        self.assertIn("BODY_TAMPERED", report.mismatches)
        self.assertNotEqual(report.stored_body_sha256, report.recomputed_body_sha256)
        self.assertFalse(report.ok)

    def test_tampered_body_cannot_be_approved(self):
        draft = self.make()
        self.submit(draft)
        self.tamper(draft)
        self.assertRefused("BODY_TAMPERED", self.svc.approve, draft.draft_id,
                           approver=OWNER, approver_role="owner", note=SEND_NOTE)
        self.assertEqual("PENDING_APPROVAL", self.raw_row(draft.draft_id)["status"])

    def test_tampered_body_cannot_be_sent(self):
        draft = self.approved()
        self.tamper(draft)
        self.assertRefused("BODY_TAMPERED", self.svc.mark_sent, draft.draft_id,
                           actor=SENDER, actor_role="agent", external_ref="wa-1")
        self.assertEqual("APPROVED", self.raw_row(draft.draft_id)["status"])

    def test_tampered_body_cannot_be_submitted(self):
        draft = self.make()
        self.tamper(draft)
        self.assertRefused("BODY_TAMPERED", self.submit, draft)
        self.assertEqual([], self.q("SELECT * FROM approval_requests"))

    def test_stored_hash_does_not_follow_a_rewritten_body(self):
        draft = self.make()
        before = self.raw_row(draft.draft_id)["body_sha256"]
        self.set_body(draft.draft_id, "body lain")
        self.assertEqual(before, self.raw_row(draft.draft_id)["body_sha256"])
        self.assertNotEqual(sha256_hex("body lain"), before)

    def test_database_refuses_to_unground_an_approved_draft(self):
        draft = self.approved()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE message_drafts SET grounding = 'UNGROUNDED' "
                              "WHERE draft_id = ?", (draft.draft_id,))

    def test_database_refuses_a_sent_row_without_an_external_ref(self):
        draft = self.approved()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE message_drafts SET status = 'SENT', sent_by = ?, "
                              "sent_at = ? WHERE draft_id = ?",
                              (SENDER, NOW.isoformat(), draft.draft_id))

    def test_database_refuses_approved_without_an_approver(self):
        draft = self.make()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE message_drafts SET status = 'APPROVED' "
                              "WHERE draft_id = ?", (draft.draft_id,))


# --------------------------------------------------------------------------- #
# Queries and verification
# --------------------------------------------------------------------------- #
class QueryTests(MessageTestCase):
    def test_drafts_for_case_returns_that_cases_drafts(self):
        first = self.make()
        self.case(case_id="case_0002")
        second = self.make(template="complaint_ack", case_id="case_0002")
        self.assertEqual([first.draft_id],
                         [d.draft_id for d in self.svc.drafts_for_case(CASE_ID)])
        self.assertEqual([second.draft_id],
                         [d.draft_id for d in self.svc.drafts_for_case("case_0002")])

    def test_drafts_for_an_unknown_case_is_empty(self):
        self.assertEqual((), self.svc.drafts_for_case("case_9999"))

    def test_pending_lists_only_submitted_drafts(self):
        waiting = self.make()
        self.submit(waiting)
        self.make(template="complaint_ack")
        self.sent()
        self.assertEqual([waiting.draft_id],
                         [d.draft_id for d in self.svc.pending()])

    def test_pending_is_empty_once_the_decision_landed(self):
        draft = self.make()
        self.submit(draft)
        self.svc.approve(draft.draft_id, approver=OWNER, approver_role="owner",
                         note=SEND_NOTE)
        self.assertEqual((), self.svc.pending())

    def test_sent_between_windows_the_sent_rows(self):
        draft = self.sent()
        self.assertEqual([draft.draft_id],
                         [d.draft_id for d in self.svc.sent_between(
                             "2026-09-21T00:00:00+00:00", "2026-09-21T23:59:59+00:00")])
        self.assertEqual([], list(self.svc.sent_between(
            "2026-09-22T00:00:00+00:00", "2026-09-23T00:00:00+00:00")))

    def test_sent_between_ignores_drafts_that_were_not_sent(self):
        self.make()
        self.approved()
        self.assertEqual([], list(self.svc.sent_between(
            "2026-09-01T00:00:00+00:00", "2026-09-30T00:00:00+00:00")))

    def test_sent_between_refuses_an_inverted_window(self):
        self.assertRefused("INVALID_RANGE", self.svc.sent_between,
                           "2026-09-22T00:00:00+00:00", "2026-09-21T00:00:00+00:00")

    def test_unknown_draft_is_refused(self):
        self.assertRefused("DRAFT_NOT_FOUND", self.svc.get, "msg_does_not_exist")
        self.assertRefused("DRAFT_NOT_FOUND", self.svc.verify, "msg_does_not_exist")

    def test_verify_accepts_an_untouched_grounded_draft(self):
        draft = self.sent()
        report = self.svc.verify(draft.draft_id)
        self.assertTrue(report.ok)
        self.assertEqual((), report.mismatches)
        self.assertEqual(MSG.GROUNDED, report.stored_grounding)
        self.assertEqual(MSG.GROUNDED, report.recomputed_grounding)
        self.assertTrue(report.stored_gate_allowed)
        self.assertTrue(report.recomputed_gate_allowed)
        self.assertEqual(report.stored_body_sha256, report.recomputed_body_sha256)

    def test_verify_reports_a_grounding_flag_that_does_not_recompute(self):
        draft = self.make()
        self.conn.execute("UPDATE message_drafts SET grounding = 'UNGROUNDED' "
                          "WHERE draft_id = ?", (draft.draft_id,))
        report = self.svc.verify(draft.draft_id)
        self.assertIn("GROUNDING_MISMATCH", report.mismatches)

    def test_verify_reports_a_gate_flag_that_does_not_recompute(self):
        draft = self.make()
        payload = json.loads(draft.gate_decision)
        payload["decision"]["allowed"] = False
        payload["decision"]["code"] = "MISCLASSIFIED_CONTENT"
        self.conn.execute("UPDATE message_drafts SET gate_decision = ? WHERE draft_id = ?",
                          (json.dumps(payload, sort_keys=True), draft.draft_id))
        report = self.svc.verify(draft.draft_id)
        self.assertIn("GATE_MISMATCH", report.mismatches)

    def test_verify_reports_unmatched_claims(self):
        draft = self.make()
        self.conn.execute("UPDATE message_drafts SET claim_bindings = ? WHERE draft_id = ?",
                          (json.dumps({"bindings": [], "claims": []}, sort_keys=True),
                           draft.draft_id))
        report = self.svc.verify(draft.draft_id)
        self.assertIn("UNMATCHED_CLAIMS", report.mismatches)

    def test_verify_reports_the_approval_status(self):
        self.assertEqual("APPROVED", self.svc.verify(self.approved().draft_id).approval_status)

    def test_verify_reports_a_missing_approval_row(self):
        """An APPROVED draft with nothing behind the claim is reported, not trusted.

        Wave 1-F-C added the foreign key that makes a *dangling* ``approval_id``
        impossible, so the reachable form of this lie is now a draft stored
        APPROVED with no approval at all. Both states hit the same branch, and
        ``verify`` must still refuse to believe the status column.
        """
        draft = self.make()
        self.conn.execute(
            "UPDATE message_drafts SET status = 'APPROVED', approved_by = 'owner_1',"
            " approved_at = ?, approval_id = NULL WHERE draft_id = ?",
            (NOW.isoformat(), draft.draft_id),
        )
        self.assertIn("APPROVAL_MISSING", self.svc.verify(draft.draft_id).mismatches)

    def test_draft_ids_are_unique_and_prefixed(self):
        first = self.make()
        second = self.make(template="complaint_ack")
        self.assertNotEqual(first.draft_id, second.draft_id)
        self.assertTrue(first.draft_id.startswith("msg_"))
        self.assertEqual(2, self.count("message_drafts"))


if __name__ == "__main__":
    unittest.main()
