"""Tests for the money path in ``src/payments.py``.

Authority chain: ``src/schema/001_initial.sql`` owns the storage contract for
``payments``/``payment_events``/``ledger_entries``/``reconciliation_cases``/
``sla_breaches``, ``src/invoicing.py`` owns the invoice an intent is raised
against, and this module owns everything between a guest being asked to pay and
the invoice reporting it as paid. Nothing here mocks storage: every test drives
the real ``PaymentService`` against an in-memory SQLite database built from the
same DDL PostgreSQL mirrors, with a ``FrozenClock`` and a deterministic
``IdFactory``.

The invariants these tests pin down, and where:

1. An intent exists only for the exact outstanding amount, in the invoice's own
   currency, for a live invoice — and never carries guest data or card data
   -> TestCreateIntentValidation.
2. Every webhook is recorded, including the refusals; the signature is
   ``HMAC-SHA256`` compared with ``hmac.compare_digest``; the raw payload is
   never stored (sha256 + a redacted summary only)
   -> TestWebhookSignature, TestWebhookProcessing.
3. One ``(provider, provider_event_id)`` is applied once; only a verified
   success settles, and it settles the invoice *through* ``InvoiceService``
   inside the same transaction -> TestWebhookProcessing.
4. The ledger is double-entry and append-only: batches balance, ``seq`` rises,
   every row chains to its predecessor, and ``verify_ledger`` recomputes the
   chain -> TestLedger.
5. Unmatched money, mismatched money and money against a dead intent are never
   confirmed: they open a ``reconciliation_cases`` row (OPEN, +48h) plus an
   ``UNMATCHED_PAYMENT`` breach, and only ``MATCHED`` ever posts money
   -> TestReconciliation.
6. A refund needs an approval that really is ``APPROVED`` (read back from the
   database, cross-checked against its decisions) and never exceeds the settled
   amount -> TestRefund.
7. The read side (:meth:`PaymentService.pending_intents`,
   :meth:`PaymentService.unmatched`, :meth:`PaymentService.ledger_balance`) is
   as strict as the write side -> TestQueries.

Stdlib only: ``unittest``, ``sqlite3``, ``hmac``, ``hashlib``, ``json``, ``re``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # pragma: no cover - mirrors tests/conftest.py
    sys.path.insert(0, str(SRC))

import payments as PAY  # noqa: E402
import primitives as P  # noqa: E402
from approvals import ApprovalExpired, ApprovalService  # noqa: E402
from audit import AuditLog  # noqa: E402
from invoicing import Invoice, InvoiceService  # noqa: E402
from schema_loader import connect_sqlite, read_schema  # noqa: E402

START = "2026-09-21T08:00:00+00:00"
HASH = "a" * 64
SECRET = "dummy-webhook-secret"
PROVIDER = "dummy"
#: Direct guest identifiers that must never reach storage.
GUEST_NAME = "Budi Santoso"
GUEST_EMAIL = "budi@example.com"
GUEST_PHONE = "081234567890"
#: A Luhn-valid test PAN — refused, never stored, never echoed in a URL.
PAN = "4111111111111111"


# --------------------------------------------------------------------------- #
# The DDL, parsed as an independent source of truth
# --------------------------------------------------------------------------- #
def _ddl() -> str:
    return read_schema("sqlite")


def _table_body(table: str) -> str:
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", _ddl(), re.S)
    assert match is not None, f"no CREATE TABLE for {table}"
    return match.group(1)


def _table_columns(table: str) -> tuple[str, ...]:
    columns: list[str] = []
    for line in _table_body(table).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("CONSTRAINT"):
            continue
        name = stripped.split()[0]
        if re.fullmatch(r"[a-z][a-z0-9_]*", name):
            columns.append(name)
    return tuple(columns)


def _check_literals(column: str, table: str) -> set[str]:
    match = re.search(
        rf"{column}\s+TEXT\s+NOT NULL CHECK \({column} IN \((.*?)\)\)",
        _table_body(table),
        re.S,
    )
    assert match is not None, f"no CHECK IN (...) for {table}.{column}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _all_text_values(conn, table: str, *, with_column: bool = False) -> list:
    """Every text column value of a table, flattened — used for PII sweeps."""
    columns = [row[0] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    values: list = []
    for row in conn.execute(f"SELECT * FROM {table}").fetchall():
        for index, value in enumerate(tuple(row)):
            if isinstance(value, str):
                values.append((columns[index], value) if with_column else value)
    return values


def seed_booking(conn, booking_id: str = "book_1") -> str:
    """Insert the parent chain a ``bookings`` row needs (property -> unit -> policy)."""
    conn.execute(
        "INSERT OR IGNORE INTO properties (property_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('prop_1', 'Villa Satu', 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE',"
        " 'ACTIVATED', ?, ?)",
        (START, START),
    )
    conn.execute(
        "INSERT OR IGNORE INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
        " base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES ('unit_1', 'prop_1', 'Unit Satu', 'entire_villa', 2, 2, 2, 4, 'ACTIVE', ?, ?)",
        (START, START),
    )
    conn.execute(
        "INSERT OR IGNORE INTO policy_versions (policy_version_id, property_id, unit_scope,"
        " version, effective_from, effective_to, currency, deposit_pct, service_fee_pct,"
        " tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,"
        " max_hold_extensions, payload, fingerprint, source_doc_ref, approved_by, created_at)"
        " VALUES ('policy_v1', 'prop_1', 'property', 1, '2026-01-01', '2027-01-01', 'IDR', '30',"
        " '10', '5', '10', 48, 60, 1, '{}', ?, 'doc.pdf', 'ops_1', ?)",
        (HASH, START),
    )
    conn.execute(
        "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
        " status, guest_ref, adults, children, infants, currency, policy_version_id,"
        " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
        " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
        " updated_at, confirmed_at, cancelled_at)"
        " VALUES (?, 'prop_1', 'unit_1', '2026-10-01', '2026-10-03', 2, 'CONFIRMED', 'guest_0001',"
        " 2, 0, 0, 'IDR', 'policy_v1', ?, 'quote_1', 'hold_1', 1500000, 0, 0, 'UNPAID', ?, 1, ?,"
        " ?, ?, NULL)",
        (booking_id, HASH, "idem_" + booking_id, START, START, START),
    )
    return booking_id


# --------------------------------------------------------------------------- #
# Shared rig
# --------------------------------------------------------------------------- #
class PaymentsTestBase(unittest.TestCase):
    """In-memory SQLite + frozen clock + deterministic ids + a real invoice."""

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.clock = P.FrozenClock(P.parse_iso(START))
        self.ids = P.IdFactory(deterministic=True, seed="pay")
        self.invoices = InvoiceService(self.conn, self.clock, self.ids)
        self.approvals = ApprovalService(self.conn, self.clock, self.ids)
        self.service = PAY.PaymentService(self.conn, self.clock, self.ids, provider=PROVIDER)
        seed_booking(self.conn, "book_1")

    # -- helpers -----------------------------------------------------------
    def issue_invoice(
        self,
        *,
        currency: str = "IDR",
        nightly: int = 1_000_000,
        nights: int = 2,
        tax: int = 100_000,
        service: int = 200_000,
        booking_id: str = "book_1",
        key: str = "idem_inv_0001",
    ) -> Invoice:
        lines = [
            {
                "kind": "NIGHTLY",
                "description": f"{nights} nights stay",
                "quantity": nights,
                "unit_amount_minor": nightly,
            }
        ]
        if tax:
            lines.append(
                {"kind": "TAX", "description": "PBJT tax", "quantity": 1, "unit_amount_minor": tax}
            )
        if service:
            lines.append(
                {
                    "kind": "SERVICE",
                    "description": "service fee",
                    "quantity": 1,
                    "unit_amount_minor": service,
                }
            )
        return self.invoices.issue(
            booking_id=booking_id,
            currency=currency,
            policy_fingerprint=HASH,
            lines=lines,
            idempotency_key=key,
        )

    def raise_intent(self, invoice: Invoice, **over):
        kwargs = {
            "invoice_id": invoice.invoice_id,
            "amount_minor": invoice.outstanding().minor_units,
            "currency": invoice.currency,
            "idempotency_key": "idem_pay_0001",
            "created_by": "agent_0001",
        }
        kwargs.update(over)
        return self.service.create_intent(**kwargs)

    def payload(self, **over) -> str:
        body = {
            "provider_intent_id": "pi_placeholder",
            "amount_minor": 2_300_000,
            "currency": "IDR",
            "status": "settled",
        }
        body.update(over)
        return json.dumps(body, sort_keys=True)

    def sign(self, payload_raw: str, secret: str = SECRET) -> str:
        return hmac.new(secret.encode(), payload_raw.encode(), hashlib.sha256).hexdigest()

    def deliver(
        self,
        payload_raw: str,
        *,
        event_id: str = "evt_000001",
        event_type: str = "payment.succeeded",
        signature: str | None = None,
        secret: str = SECRET,
        provider: str = PROVIDER,
        received_at=None,
    ):
        return self.service.handle_webhook(
            provider=provider,
            event_id=event_id,
            event_type=event_type,
            payload_raw=payload_raw,
            signature=self.sign(payload_raw, secret) if signature is None else signature,
            secret=secret,
            received_at=received_at,
        )

    def settle(self, invoice: Invoice, *, event_id: str = "evt_000001", intent=None):
        intent = intent if intent is not None else self.raise_intent(invoice)
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id,
            amount_minor=intent.amount_minor,
            currency=intent.currency,
        )
        return intent, self.deliver(payload_raw, event_id=event_id)

    def approve_refund(
        self,
        payment_id: str,
        *,
        amount: int = 500_000,
        currency: str = "IDR",
        key: str = "idem_refund_0001",
        approvers=(("owner_0001", "owner"), ("owner_0002", "owner")),
        requested_by: str = "agent_0001",
        expires_minutes=None,
    ):
        approval = self.approvals.request(
            subject_type="refund",
            subject_id=payment_id,
            action="refund.guest",
            requested_by=requested_by,
            requested_role="agent",
            reason="guest cancelled the stay after settling the invoice",
            required_role="owner",
            required_approvals=2,
            risk_level="HIGH",
            money=P.Money(amount, currency),
            expires_minutes=expires_minutes,
            idempotency_key=key,
        )
        for approver, role in approvers:
            approval = self.approvals.decide(
                approval.approval_id,
                approver=approver,
                approver_role=role,
                decision="APPROVE",
                note="checked against the booking and the ledger",
            )
        return approval

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def scalar(self, sql: str, params: tuple = ()):
        return self.conn.execute(sql, params).fetchone()[0]

    def ledger_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM ledger_entries"))

    def event_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM payment_events"))

    def paid(self, invoice: Invoice) -> int:
        return self.invoices.get(invoice.invoice_id).amount_paid_minor


# --------------------------------------------------------------------------- #
# 1. The DDL is the contract
# --------------------------------------------------------------------------- #
class TestVocabularyMatchesTheDdl(PaymentsTestBase):
    def test_payment_statuses_match_the_ddl_check(self):
        self.assertEqual(set(PAY.PAYMENT_STATUSES), _check_literals("status", "payments"))

    def test_ledger_accounts_match_the_ddl_check(self):
        self.assertEqual(set(PAY.LEDGER_ACCOUNTS), _check_literals("account", "ledger_entries"))

    def test_ledger_directions_match_the_ddl_check(self):
        self.assertEqual(set(PAY.LEDGER_DIRECTIONS), _check_literals("direction", "ledger_entries"))

    def test_reconciliation_kinds_match_the_ddl_check(self):
        self.assertEqual(
            set(PAY.RECONCILIATION_KINDS), _check_literals("kind", "reconciliation_cases")
        )

    def test_reconciliation_statuses_match_the_ddl_check(self):
        self.assertEqual(
            set(PAY.RECONCILIATION_STATUSES), _check_literals("status", "reconciliation_cases")
        )

    def test_the_unmatched_breach_kind_exists_in_the_ddl(self):
        self.assertIn(PAY.UNMATCHED_PAYMENT_BREACH, _check_literals("kind", "sla_breaches"))

    def test_payment_columns_match_the_ddl(self):
        self.assertEqual(set(PAY.PAYMENT_COLUMNS), set(_table_columns("payments")))

    def test_event_columns_match_the_ddl(self):
        self.assertEqual(set(PAY.EVENT_COLUMNS), set(_table_columns("payment_events")))

    def test_ledger_columns_match_the_ddl(self):
        self.assertEqual(set(PAY.LEDGER_COLUMNS), set(_table_columns("ledger_entries")))

    def test_reconciliation_columns_match_the_ddl(self):
        self.assertEqual(set(PAY.RECON_COLUMNS), set(_table_columns("reconciliation_cases")))

    def test_the_ledger_hash_fields_exist_in_the_table(self):
        self.assertLessEqual(set(PAY.LEDGER_HASH_FIELDS), set(PAY.LEDGER_COLUMNS))

    def test_append_only_triggers_are_declared_by_the_schema(self):
        triggers = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        self.assertEqual(set(PAY.APPEND_ONLY_TRIGGERS), set(PAY.APPEND_ONLY_TRIGGERS) & triggers)

    def test_error_codes_are_unique_and_exported(self):
        codes = [
            obj.code
            for obj in vars(PAY).values()
            if isinstance(obj, type) and issubclass(obj, P.DomainError)
        ]
        self.assertEqual(len(PAY.ERROR_CODES), len(set(PAY.ERROR_CODES)))
        for code in PAY.ERROR_CODES:
            self.assertIn(code, codes)

    def test_the_public_api_is_exported_and_importable(self):
        for name in PAY.__all__:
            self.assertTrue(hasattr(PAY, name), f"{name} is missing")


# --------------------------------------------------------------------------- #
# 2. Raising an intent
# --------------------------------------------------------------------------- #
class TestCreateIntentValidation(PaymentsTestBase):
    def test_an_intent_is_raised_for_the_outstanding_amount(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        self.assertEqual(intent.status, "PENDING")
        self.assertEqual(intent.amount_minor, invoice.total_minor)
        self.assertEqual(intent.currency, "IDR")
        self.assertEqual(intent.invoice_id, invoice.invoice_id)
        self.assertTrue(intent.payment_id.startswith("payment_"))

    def test_the_payment_row_matches_the_returned_intent(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payment = self.service.get_payment(intent.payment_id)
        self.assertEqual(payment.status, "PENDING")
        self.assertEqual(payment.method, PAY.PAYMENT_METHOD_HOSTED)
        self.assertEqual(payment.amount_minor, invoice.total_minor)
        self.assertEqual(payment.provider, PROVIDER)
        self.assertIsNone(payment.settled_at)
        self.assertEqual(self.service.for_invoice(invoice.invoice_id), (payment,))

    def test_the_expiry_is_created_plus_the_requested_minutes(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice, expires_minutes=45)
        expected = P.to_iso(P.parse_iso(intent.created_at) + P.timedelta(minutes=45))
        self.assertEqual(intent.expires_at, expected)

    def test_the_hosted_url_carries_no_pii(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        url = intent.hosted_url
        for leak in (GUEST_NAME, GUEST_EMAIL, GUEST_PHONE, "guest_0001", invoice.booking_id):
            self.assertNotIn(leak, url)
        self.assertFalse(PAY.contains_card_data(url))
        self.assertEqual(url, url.strip())

    def test_the_hosted_url_is_https_and_tokenised(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        self.assertTrue(intent.hosted_url.startswith("https://"))
        self.assertIn(intent.payment_id, intent.hosted_url)
        token = intent.hosted_url.split("token=")[1]
        self.assertEqual(len(token), 32)
        self.assertTrue(all(char in "0123456789abcdef" for char in token))

    def test_a_short_idempotency_key_is_refused(self):
        invoice = self.issue_invoice()
        with self.assertRaises(PAY.InvalidIdempotencyKey) as caught:
            self.raise_intent(invoice, idempotency_key="short")
        self.assertEqual(caught.exception.code, PAY.INVALID_IDEMPOTENCY_KEY)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM payments"), 0)

    def test_the_currency_must_be_the_invoice_currency(self):
        invoice = self.issue_invoice(currency="USD", nightly=100_000, tax=0, service=0)
        with self.assertRaises(PAY.PaymentCurrencyMismatch) as caught:
            self.raise_intent(invoice, currency="IDR")
        self.assertEqual(caught.exception.code, PAY.CURRENCY_MISMATCH)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM payments"), 0)

    def test_an_unknown_currency_is_refused(self):
        invoice = self.issue_invoice()
        with self.assertRaises(P.DomainError) as caught:
            self.raise_intent(invoice, currency="XYZ")
        self.assertEqual(caught.exception.code, PAY.INVALID_INTENT_REQUEST)

    def test_the_amount_must_equal_the_outstanding(self):
        invoice = self.issue_invoice()
        with self.assertRaises(PAY.PaymentAmountMismatch) as caught:
            self.raise_intent(invoice, amount_minor=invoice.total_minor - 1)
        self.assertEqual(caught.exception.code, PAY.AMOUNT_MISMATCH)
        self.assertEqual(caught.exception.details["outstanding_minor"], invoice.total_minor)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM payments"), 0)

    def test_a_settled_invoice_owes_nothing_so_it_gets_no_intent(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        with self.assertRaises(PAY.PaymentAmountMismatch):
            self.raise_intent(invoice, amount_minor=invoice.total_minor)

    def test_the_amount_must_be_a_positive_integer(self):
        invoice = self.issue_invoice()
        for bad in (0, -1, "100", 1.5, True):
            with self.subTest(amount=bad):
                with self.assertRaises(P.DomainError):
                    self.raise_intent(invoice, amount_minor=bad)

    def test_a_void_invoice_cannot_be_paid(self):
        invoice = self.issue_invoice()
        self.invoices.void(invoice_id=invoice.invoice_id, reason="duplicate of INV-202609-00009")
        with self.assertRaises(PAY.InvoiceNotPayable) as caught:
            self.raise_intent(invoice)
        self.assertEqual(caught.exception.code, PAY.INVOICE_ALREADY_VOID)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM payments"), 0)

    def test_an_unknown_invoice_is_refused(self):
        self.issue_invoice()
        with self.assertRaises(P.DomainError) as caught:
            self.service.create_intent(
                invoice_id="inv_does_not_exist",
                amount_minor=1,
                currency="IDR",
                idempotency_key="idem_pay_0001",
                created_by="agent_0001",
            )
        self.assertIn(caught.exception.code, {"INVOICE_NOT_FOUND", "ENTITY_NOT_FOUND"})

    def test_expires_minutes_is_bounded(self):
        invoice = self.issue_invoice()
        for bad in (0, -5, True, PAY.MAX_EXPIRES_MINUTES + 1, "30"):
            with self.subTest(expires_minutes=bad):
                with self.assertRaises(PAY.InvalidPaymentIntent):
                    self.raise_intent(invoice, expires_minutes=bad)

    def test_a_replay_returns_the_same_intent(self):
        invoice = self.issue_invoice()
        first = self.raise_intent(invoice)
        second = self.raise_intent(invoice)
        self.assertEqual(first.payment_id, second.payment_id)
        self.assertTrue(second.idempotent_replay)
        self.assertFalse(first.idempotent_replay)
        self.assertEqual(first.hosted_url, second.hosted_url)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM payments"), 1)

    def test_a_reused_key_with_another_payload_is_a_conflict(self):
        invoice = self.issue_invoice()
        self.raise_intent(invoice)
        with self.assertRaises(PAY.PaymentIdempotencyConflict) as caught:
            self.raise_intent(invoice, expires_minutes=60)
        self.assertEqual(caught.exception.code, PAY.IDEMPOTENCY_CONFLICT)

    def test_card_number_in_created_by_is_refused(self):
        invoice = self.issue_invoice()
        with self.assertRaises(PAY.CardDataForbidden) as caught:
            self.raise_intent(invoice, created_by="agent_" + PAN)
        self.assertEqual(caught.exception.code, PAY.CARD_DATA_FORBIDDEN)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM payments"), 0)

    def test_a_card_field_name_is_refused(self):
        with self.assertRaises(PAY.CardDataForbidden):
            PAY.assert_no_card_data({"card_number": PAN})
        with self.assertRaises(PAY.CardDataForbidden):
            PAY.assert_no_card_data({"cvc": "123"})
        PAY.assert_no_card_data({"amount_minor": 2_300_000, "invoice_id": "inv_0001"})

    def test_nothing_card_shaped_is_ever_stored(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        digests = {"entry_hash", "prev_hash", "payload_sha256", "request_hash", "hash"}
        for table in ("payments", "payment_events", "ledger_entries"):
            for column, value in _all_text_values(self.conn, table, with_column=True):
                if column in digests:
                    # a 64-hex digest is never personally identifying
                    continue
                with self.subTest(table=table, column=column, value=value):
                    self.assertFalse(PAY.contains_card_data(value))

    def test_a_pan_is_recognised_but_padding_is_not(self):
        self.assertTrue(PAY.contains_card_data(PAN))
        self.assertTrue(PAY.contains_card_data("card 4242 4242 4242 4242 ok"))
        self.assertFalse(PAY.contains_card_data("0" * 64))
        self.assertFalse(PAY.contains_card_data("1" * 32))
        self.assertFalse(PAY.contains_card_data("2_300_000"))
        self.assertFalse(PAY.contains_card_data(None))

    def test_a_provider_needs_at_least_three_characters(self):
        with self.assertRaises(P.DomainError) as caught:
            PAY.PaymentService(self.conn, self.clock, self.ids, provider="ab")
        self.assertEqual(caught.exception.code, PAY.INVALID_WEBHOOK)


# --------------------------------------------------------------------------- #
# 3. Webhook signatures
# --------------------------------------------------------------------------- #
class TestWebhookSignature(PaymentsTestBase):
    def test_a_wrong_signature_is_recorded_then_refused(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        with self.assertRaises(PAY.WebhookSignatureInvalid) as caught:
            self.deliver(payload_raw, signature="0" * 64)
        self.assertEqual(caught.exception.code, PAY.WEBHOOK_SIGNATURE_INVALID)
        rows = self.rows("SELECT * FROM payment_events")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature_verified"], 0)
        self.assertEqual(rows[0]["applied"], 0)
        self.assertGreaterEqual(len(rows[0]["rejection_reason"]), PAY.MIN_REJECTION_REASON)

    def test_a_wrong_signature_moves_no_money(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        with self.assertRaises(PAY.WebhookSignatureInvalid):
            self.deliver(payload_raw, signature="f" * 64)
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(self.paid(invoice), 0)
        self.assertEqual(self.service.get_payment(intent.payment_id).status, "PENDING")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reconciliation_cases"), 0)

    def test_a_signature_for_another_payload_is_refused(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        signed = self.payload(provider_intent_id=intent.provider_intent_id, amount_minor=1)
        other = self.payload(provider_intent_id=intent.provider_intent_id, amount_minor=999)
        with self.assertRaises(PAY.WebhookSignatureInvalid):
            self.deliver(other, signature=self.sign(signed))
        self.assertEqual(self.ledger_count(), 0)

    def test_the_signature_is_compared_in_constant_time(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        real = hmac.compare_digest
        seen: list[tuple] = []

        def spy(expected, given):
            seen.append((expected, given))
            return real(expected, given)

        with mock.patch("hmac.compare_digest", side_effect=spy):
            self.deliver(payload_raw)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], self.sign(payload_raw))

    def test_the_digest_is_hmac_sha256_over_the_exact_payload(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        expected = hmac.new(
            SECRET.encode(), payload_raw.encode(), hashlib.sha256
        ).hexdigest()
        event = self.deliver(payload_raw, signature=expected)
        self.assertTrue(event.verified)
        self.assertTrue(event.was_applied)
        # any other construction of the digest is refused
        wrong = hmac.new(SECRET.encode(), payload_raw.encode(), hashlib.sha1).hexdigest()
        with self.assertRaises(PAY.WebhookSignatureInvalid):
            self.deliver(payload_raw, event_id="evt_000002", signature=wrong)

    def test_a_non_string_signature_is_refused(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        with self.assertRaises(PAY.InvalidWebhook):
            self.service.handle_webhook(
                provider=PROVIDER,
                event_id="evt_nonstr01",
                event_type="payment.succeeded",
                payload_raw=payload_raw,
                signature=12345,
                secret=SECRET,
            )
        self.assertEqual(self.event_count(), 0)

    def test_an_empty_secret_is_refused(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        with self.assertRaises(P.DomainError):
            self.deliver(payload_raw, secret="")

    def test_a_webhook_for_another_provider_is_refused(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        with self.assertRaises(PAY.WebhookProviderMismatch) as caught:
            self.deliver(payload_raw, provider="other_psp")
        self.assertEqual(caught.exception.code, PAY.PROVIDER_MISMATCH)
        self.assertEqual(self.event_count(), 0)

    def test_a_too_short_event_id_is_refused(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        with self.assertRaises(PAY.InvalidWebhook) as caught:
            self.deliver(payload_raw, event_id="ev_1")
        self.assertEqual(caught.exception.code, PAY.INVALID_WEBHOOK)
        self.assertEqual(self.event_count(), 0)

    def test_card_data_in_a_payload_is_refused_and_never_stored(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(
            # unsafe-scan: allow (probe penolakan: modul harus MENOLAK data kartu,
            # jadi nilainya memang harus ada di berkas test ini)
            provider_intent_id=intent.provider_intent_id, card_number=PAN, cvc="737",  # unsafe-scan: allow
        )
        with self.assertRaises(PAY.CardDataForbidden) as caught:
            self.deliver(payload_raw)
        self.assertEqual(caught.exception.code, PAY.CARD_DATA_FORBIDDEN)
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(self.paid(invoice), 0)
        rows = self.rows("SELECT * FROM payment_events")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature_verified"], 1)
        self.assertEqual(rows[0]["applied"], 0)
        self.assertNotIn(PAN, json.dumps(rows))
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM reconciliation_cases WHERE kind = 'UNVERIFIED_WEBHOOK'"),
            1,
        )

    def test_the_raw_payload_is_never_stored(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id,
            guest_name=GUEST_NAME,
            email=GUEST_EMAIL,
            phone=GUEST_PHONE,
            note="guest asked to pay by card at the desk",
        )
        self.deliver(payload_raw)
        stored = _all_text_values(self.conn, "payment_events")
        blob = "\n".join(stored)
        for leak in (GUEST_NAME, GUEST_EMAIL, GUEST_PHONE, payload_raw):
            self.assertNotIn(leak, blob)
        row = self.rows("SELECT * FROM payment_events")[0]
        self.assertNotIn("guest_name", row["payload_redacted"])
        self.assertIn("intent=", row["payload_redacted"])

    def test_the_stored_hash_is_the_hash_of_the_payload(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        event = self.deliver(payload_raw, event_id="evt_hash0001")
        expected = P.sha256_hex(PAY.PAYLOAD_HASH_DOMAIN, PROVIDER, "evt_hash0001", payload_raw)
        self.assertEqual(event.payload_sha256, expected)
        self.assertEqual(len(event.payload_sha256), 64)


# --------------------------------------------------------------------------- #
# 4. Webhook processing
# --------------------------------------------------------------------------- #
class TestWebhookProcessing(PaymentsTestBase):
    def test_a_replayed_event_is_processed_once(self):
        invoice = self.issue_invoice()
        intent, first = self.settle(invoice)
        again = self.deliver(
            self.payload(
                provider_intent_id=intent.provider_intent_id,
                amount_minor=intent.amount_minor,
            ),
            event_id=first.provider_event_id,
        )
        self.assertTrue(again.duplicate)
        self.assertEqual(again.event_id, first.event_id)
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(self.ledger_count(), 4)
        self.assertEqual(self.paid(invoice), invoice.total_minor)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM reconciliation_cases"), 0
        )

    def test_a_replay_of_a_refused_event_returns_the_stored_row(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        with self.assertRaises(PAY.WebhookSignatureInvalid) as first:
            self.deliver(payload_raw, event_id="evt_refused1", signature="1" * 64)
        again = self.deliver(payload_raw, event_id="evt_refused1", signature="1" * 64)
        self.assertTrue(again.duplicate)
        self.assertTrue(first.exception.details)
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(again.provider_event_id, "evt_refused1")

    def test_a_success_event_settles_the_payment_and_the_invoice(self):
        invoice = self.issue_invoice()
        intent, event = self.settle(invoice)
        self.assertTrue(event.was_applied)
        self.assertEqual(event.payment_id, intent.payment_id)
        self.assertEqual(event.amount_minor, intent.amount_minor)
        payment = self.service.get_payment(intent.payment_id)
        self.assertEqual(payment.status, "SUCCEEDED")
        self.assertIsNotNone(payment.settled_at)
        settled = self.invoices.get(invoice.invoice_id)
        self.assertEqual(settled.amount_paid_minor, invoice.total_minor)
        self.assertEqual(settled.payment_status, "PAID")
        self.assertEqual(settled.status, "ISSUED")

    def test_settlement_goes_through_the_invoice_service(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        actions = [
            row["action"]
            for row in self.rows("SELECT action FROM audit_events ORDER BY seq")
        ]
        self.assertIn("invoice.settlement_recorded", actions)
        self.assertIn(PAY.AUDIT_PAYMENT_SETTLED, actions)
        # only the *derived* columns moved; the invoice itself is untouched
        row = self.rows("SELECT * FROM invoices WHERE invoice_id = ?", (invoice.invoice_id,))[0]
        self.assertEqual(row["invoice_number"], invoice.invoice_number)
        self.assertEqual(row["issued_at"], invoice.issued_at)
        self.assertEqual(row["due_at"], invoice.due_at)
        self.assertEqual(row["total_minor"], invoice.total_minor)

    def test_the_invoice_is_moved_by_the_invoice_service_and_nothing_else(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id, amount_minor=intent.amount_minor
        )
        calls: list[tuple] = []
        original = InvoiceService.record_settlement

        def spy(invoice_service, invoice_id, **kwargs):
            calls.append((invoice_id, kwargs["amount_paid_minor"], kwargs["payment_status"]))
            return original(invoice_service, invoice_id, **kwargs)

        with mock.patch.object(InvoiceService, "record_settlement", new=spy):
            self.deliver(payload_raw, event_id="evt_delegate")
        # no other route touched the invoice: the delegated call is the only move
        self.assertEqual(calls, [(invoice.invoice_id, invoice.total_minor, "PAID")])

    def test_an_injected_audit_log_is_used(self):
        invoice = self.issue_invoice()
        shared = AuditLog(self.conn, clock=self.clock, ids=self.ids)
        service = PAY.PaymentService(
            self.conn, self.clock, self.ids, shared, provider=PROVIDER
        )
        intent = service.create_intent(
            invoice_id=invoice.invoice_id,
            amount_minor=invoice.total_minor,
            currency="IDR",
            idempotency_key="idem_pay_0001",
            created_by="agent_0001",
        )
        self.assertIsNotNone(intent.payment_id)
        self.assertGreater(shared.count(), 0)

    def test_the_trigger_guard_can_be_lifted_explicitly(self):
        self.conn.execute("DROP TRIGGER trg_ledger_entries_no_delete")
        with self.assertRaises(PAY.LedgerAppendOnlyUnenforced):
            PAY.PaymentService(self.conn, self.clock, self.ids, provider=PROVIDER)
        relaxed = PAY.PaymentService(
            self.conn, self.clock, self.ids, provider=PROVIDER, enforce_triggers=False
        )
        self.assertEqual(relaxed.provider, PROVIDER)
        self.assertEqual(relaxed.ledger_balance("IDR").entry_count, 0)

    def test_a_settlement_writes_a_balanced_batch_on_the_declared_accounts(self):
        invoice = self.issue_invoice()
        intent, _ = self.settle(invoice)
        entries = self.service.entries(payment_id=intent.payment_id)
        debits = sum(e.amount_minor for e in entries if e.direction == "DEBIT")
        credits = sum(e.amount_minor for e in entries if e.direction == "CREDIT")
        self.assertEqual(debits, credits)
        self.assertEqual(debits, intent.amount_minor)
        accounts = {e.account for e in entries}
        self.assertEqual(accounts, {"CASH_IN", "TAX_PAYABLE", "SERVICE_FEE", "OWNER_PAYABLE"})
        self.assertEqual(
            {e.account: (e.direction, e.amount_minor) for e in entries}["CASH_IN"],
            ("DEBIT", intent.amount_minor),
        )
        self.assertTrue(self.service.ledger_balance("IDR").balanced)

    def test_a_failed_event_marks_the_payment_failed_without_money(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id, status="failed"
        )
        event = self.deliver(payload_raw, event_type="payment.failed", event_id="evt_fail0001")
        self.assertTrue(event.was_applied)
        self.assertEqual(self.service.get_payment(intent.payment_id).status, "FAILED")
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(self.paid(invoice), 0)

    def test_an_expired_event_marks_the_payment_expired(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        self.deliver(payload_raw, event_type="payment.expired", event_id="evt_exp00001")
        self.assertEqual(self.service.get_payment(intent.payment_id).status, "EXPIRED")
        self.assertEqual(self.ledger_count(), 0)

    def test_a_pending_event_only_touches_the_provider_status(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        self.deliver(payload_raw, event_type="payment.pending", event_id="evt_pend0001")
        payment = self.service.get_payment(intent.payment_id)
        self.assertEqual(payment.status, "PENDING")
        self.assertEqual(payment.provider_status, "payment.pending")
        self.assertEqual(self.ledger_count(), 0)

    def test_an_unsupported_event_type_is_recorded_and_not_applied(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(provider_intent_id=intent.provider_intent_id)
        event = self.deliver(
            payload_raw, event_type="payment.teleported", event_id="evt_weird001"
        )
        self.assertFalse(event.was_applied)
        self.assertTrue(event.verified)
        self.assertIn("unsupported", event.rejection_reason)
        self.assertEqual(self.service.get_payment(intent.payment_id).status, "PENDING")
        self.assertEqual(self.ledger_count(), 0)

    def test_an_amount_mismatch_is_not_confirmed(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id,
            amount_minor=intent.amount_minor - 500_000,
        )
        event = self.deliver(payload_raw, event_id="evt_mismatch")
        self.assertFalse(event.was_applied)
        self.assertIsNotNone(event.recon_id)
        self.assertEqual(self.service.get_payment(intent.payment_id).status, "PENDING")
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(self.paid(invoice), 0)
        case = self.service.get_reconciliation(event.recon_id)
        self.assertEqual(case.kind, "AMOUNT_MISMATCH")
        self.assertEqual(case.status, "OPEN")
        self.assertEqual(case.payment_id, intent.payment_id)
        self.assertEqual(case.invoice_id, invoice.invoice_id)
        self.assertEqual(case.details_dict["declared_amount_minor"], intent.amount_minor - 500_000)

    def test_a_payload_currency_mismatch_is_not_confirmed(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id,
            amount_minor=intent.amount_minor,
            currency="USD",
        )
        event = self.deliver(payload_raw, event_id="evt_curmism")
        self.assertFalse(event.was_applied)
        self.assertEqual(self.service.get_reconciliation(event.recon_id).kind, "AMOUNT_MISMATCH")
        self.assertEqual(self.ledger_count(), 0)

    def test_a_second_event_cannot_settle_the_same_payment_twice(self):
        invoice = self.issue_invoice()
        intent, _ = self.settle(invoice, event_id="evt_first001")
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id, amount_minor=intent.amount_minor
        )
        event = self.deliver(payload_raw, event_id="evt_second01")
        self.assertFalse(event.was_applied)
        self.assertEqual(self.service.get_reconciliation(event.recon_id).kind, "DUPLICATE_EVENT")
        self.assertEqual(self.ledger_count(), 4)
        self.assertEqual(self.paid(invoice), invoice.total_minor)

    def test_money_for_a_dead_intent_needs_a_human(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice, expires_minutes=30)
        self.clock.advance(minutes=31)
        self.assertEqual(self.service.pending_intents(), ())
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id, amount_minor=intent.amount_minor
        )
        event = self.deliver(payload_raw, event_id="evt_late0001")
        self.assertFalse(event.was_applied)
        case = self.service.get_reconciliation(event.recon_id)
        self.assertEqual(case.kind, "UNMATCHED_PAYMENT")
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(self.paid(invoice), 0)
        breach = self.service.breaches(subject_id=intent.payment_id)[0]
        self.assertEqual(breach["kind"], PAY.UNMATCHED_PAYMENT_BREACH)
        self.assertGreater(breach["breach_minutes"], 0)

    def test_a_second_intent_cannot_settle_the_same_invoice_twice(self):
        invoice = self.issue_invoice()
        first = self.raise_intent(invoice, idempotency_key="idem_pay_0001")
        second = self.raise_intent(invoice, idempotency_key="idem_pay_0002")
        self.settle(invoice, event_id="evt_first001", intent=first)
        payload_raw = self.payload(
            provider_intent_id=second.provider_intent_id, amount_minor=second.amount_minor
        )
        event = self.deliver(payload_raw, event_id="evt_second01")
        self.assertFalse(event.was_applied)
        self.assertEqual(self.service.get_reconciliation(event.recon_id).kind, "AMOUNT_MISMATCH")
        self.assertEqual(self.paid(invoice), invoice.total_minor)
        self.assertEqual(self.ledger_count(), 4)

    def test_an_event_without_a_matching_intent_opens_a_case(self):
        self.issue_invoice()  # seeded for its side effect
        payload_raw = self.payload(provider_intent_id="pi_nothing_here", amount_minor=150_000)
        event = self.deliver(payload_raw, event_id="evt_orphan01")
        self.assertFalse(event.was_applied)
        self.assertIsNone(event.payment_id)
        case = self.service.get_reconciliation(event.recon_id)
        self.assertEqual(case.kind, "UNMATCHED_PAYMENT")
        self.assertIsNone(case.payment_id)
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM payments"), 0)


# --------------------------------------------------------------------------- #
# 5. The ledger
# --------------------------------------------------------------------------- #
class TestLedger(PaymentsTestBase):
    def entry(self, **over):
        base = {
            "account": "CASH_IN",
            "direction": "DEBIT",
            "amount_minor": 100_000,
            "currency": "IDR",
            "memo": "probe entry",
        }
        base.update(over)
        return base

    def test_a_batch_must_balance(self):
        with self.assertRaises(PAY.LedgerUnbalanced) as caught:
            PAY.post_journal(
                self.conn,
                entries=[
                    self.entry(),
                    self.entry(account="OWNER_PAYABLE", direction="CREDIT", amount_minor=99_999),
                ],
                occurred_at=START,
                ids=self.ids,
            )
        self.assertEqual(caught.exception.code, PAY.LEDGER_UNBALANCED)
        self.assertEqual(caught.exception.details["debits_minor"], 100_000)
        self.assertEqual(caught.exception.details["credits_minor"], 99_999)

    def test_an_unbalanced_batch_writes_nothing(self):
        with self.assertRaises(PAY.LedgerUnbalanced):
            PAY.post_journal(
                self.conn,
                entries=[self.entry(), self.entry(direction="CREDIT", amount_minor=1)],
                occurred_at=START,
                ids=self.ids,
            )
        self.assertEqual(self.ledger_count(), 0)

    def test_an_unknown_account_is_refused(self):
        with self.assertRaises(PAY.LedgerEntryInvalid):
            PAY.post_journal(
                self.conn,
                entries=[self.entry(account="PETTY_CASH"), self.entry(direction="CREDIT")],
                occurred_at=START,
                ids=self.ids,
            )
        self.assertEqual(self.ledger_count(), 0)

    def test_an_unknown_direction_is_refused(self):
        with self.assertRaises(PAY.LedgerEntryInvalid):
            PAY.post_journal(
                self.conn,
                entries=[self.entry(direction="SIDEWAYS"), self.entry(direction="CREDIT")],
                occurred_at=START,
                ids=self.ids,
            )

    def test_a_non_positive_amount_is_refused(self):
        for bad in (0, -1, "100", True):
            with self.subTest(amount=bad):
                with self.assertRaises(PAY.LedgerEntryInvalid):
                    PAY.post_journal(
                        self.conn,
                        entries=[self.entry(amount_minor=bad), self.entry(direction="CREDIT")],
                        occurred_at=START,
                        ids=self.ids,
                    )

    def test_a_mixed_currency_batch_is_refused(self):
        with self.assertRaises(PAY.LedgerEntryInvalid):
            PAY.post_journal(
                self.conn,
                entries=[self.entry(), self.entry(direction="CREDIT", currency="USD")],
                occurred_at=START,
                ids=self.ids,
            )

    def test_an_empty_batch_is_refused(self):
        with self.assertRaises(PAY.LedgerEntryInvalid):
            PAY.post_journal(self.conn, entries=[], occurred_at=START, ids=self.ids)

    def test_a_short_memo_is_refused(self):
        with self.assertRaises(PAY.LedgerEntryInvalid):
            PAY.post_journal(
                self.conn,
                entries=[self.entry(memo="x"), self.entry(direction="CREDIT")],
                occurred_at=START,
                ids=self.ids,
            )

    def test_seq_is_contiguous_and_the_first_row_is_genesis(self):
        first = self.issue_invoice(key="idem_inv_0001")
        second = self.issue_invoice(key="idem_inv_0002", booking_id="book_1")
        self.settle(first, event_id="evt_first001", intent=self.raise_intent(first, idempotency_key="idem_pay_0001"))
        self.settle(second, event_id="evt_second01", intent=self.raise_intent(second, idempotency_key="idem_pay_0002"))
        entries = self.service.entries()
        self.assertEqual([e.seq for e in entries], list(range(1, len(entries) + 1)))
        self.assertEqual(entries[0].prev_hash, PAY.GENESIS_HASH)
        self.assertEqual(entries[1].prev_hash, entries[0].entry_hash)
        self.assertEqual(entries[-1].prev_hash, entries[-2].entry_hash)

    def test_the_chain_verifies_after_settlement_and_refund(self):
        invoice = self.issue_invoice()
        intent, _ = self.settle(invoice)
        approval = self.approve_refund(intent.payment_id, amount=500_000)
        self.service.refund(
            payment_id=intent.payment_id,
            amount_minor=500_000,
            approval_id=approval.approval_id,
            actor="accountant_0001",
            actor_role="accountant",
            reason="partial refund after the guest shortened the stay",
        )
        report = PAY.verify_ledger(self.conn)
        self.assertTrue(report.ok, report.reason)
        self.assertEqual(report.checked, 6)
        self.assertEqual(report.head_hash, self.service.entries()[-1].entry_hash)
        self.assertIsNone(report.first_broken_seq)

    def test_a_tampered_amount_breaks_the_chain(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        self.conn.execute("DROP TRIGGER trg_ledger_entries_no_update")
        self.conn.execute(
            "UPDATE ledger_entries SET amount_minor = amount_minor + 1 WHERE seq = 2"
        )
        report = PAY.verify_ledger(self.conn)
        self.assertFalse(report.ok)
        self.assertEqual(report.first_broken_seq, 2)
        self.assertIn("entry_hash", report.reason)

    def test_a_broken_link_is_detected(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        self.conn.execute("DROP TRIGGER trg_ledger_entries_no_update")
        self.conn.execute("UPDATE ledger_entries SET prev_hash = ? WHERE seq = 3", ("b" * 64,))
        report = PAY.verify_ledger(self.conn)
        self.assertFalse(report.ok)
        self.assertEqual(report.first_broken_seq, 3)
        self.assertIn("prev_hash", report.reason)

    def test_the_verifier_can_raise_instead_of_reporting(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        self.conn.execute("DROP TRIGGER trg_ledger_entries_no_update")
        self.conn.execute("UPDATE ledger_entries SET memo = 'tampered memo' WHERE seq = 1")
        with self.assertRaises(PAY.LedgerChainBroken) as caught:
            PAY.verify_ledger(self.conn, raise_on_break=True)
        self.assertEqual(caught.exception.code, PAY.LEDGER_CHAIN_BROKEN)
        self.assertEqual(caught.exception.details["first_broken_seq"], 1)

    def test_a_sequence_gap_is_detected(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        self.conn.execute("DROP TRIGGER trg_ledger_entries_no_update")
        self.conn.execute("UPDATE ledger_entries SET seq = 9 WHERE seq = 4")
        report = PAY.verify_ledger(self.conn)
        self.assertFalse(report.ok)
        self.assertIn("sequence gap", report.reason)

    def test_ledger_rows_are_append_only(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.conn.execute("UPDATE ledger_entries SET amount_minor = 1 WHERE seq = 1")
        self.assertIn("append-only", str(caught.exception))
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.conn.execute("DELETE FROM ledger_entries")
        self.assertIn("append-only", str(caught.exception))
        self.assertEqual(self.ledger_count(), 4)

    def test_payment_events_are_append_only(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE payment_events SET applied = 0")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM payment_events")

    def test_the_balance_is_per_account_and_always_balanced(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        balance = self.service.ledger_balance("IDR")
        self.assertTrue(balance.balanced)
        self.assertEqual(balance.debit_total_minor, balance.credit_total_minor)
        self.assertEqual(balance.debit_total_minor, invoice.total_minor)
        self.assertEqual(balance.entry_count, 4)
        self.assertEqual(balance.by_account["CASH_IN"]["DEBIT"], invoice.total_minor)
        self.assertEqual(balance.by_account["TAX_PAYABLE"]["CREDIT"], invoice.tax_minor)
        self.assertEqual(balance.by_account["SERVICE_FEE"]["CREDIT"], invoice.service_minor)

    def test_an_unbalanced_ledger_raises(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        self.conn.execute("DROP TRIGGER trg_ledger_entries_no_update")
        self.conn.execute(
            "UPDATE ledger_entries SET amount_minor = amount_minor + 1 WHERE account = 'CASH_IN'"
        )
        with self.assertRaises(PAY.LedgerUnbalanced) as caught:
            self.service.ledger_balance("IDR")
        self.assertEqual(caught.exception.code, PAY.LEDGER_UNBALANCED)
        self.assertFalse(PAY.verify_ledger(self.conn).ok)

    def test_the_balance_of_an_untouched_currency_is_zero(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        balance = self.service.ledger_balance("USD")
        self.assertEqual(balance.debit_total_minor, 0)
        self.assertTrue(balance.balanced)

    def test_the_guard_refuses_a_database_without_triggers(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        self.conn.execute("DROP TRIGGER trg_ledger_entries_no_delete")
        with self.assertRaises(PAY.LedgerAppendOnlyUnenforced) as caught:
            PAY.PaymentService(self.conn, self.clock, self.ids, provider=PROVIDER)
        self.assertEqual(caught.exception.code, PAY.LEDGER_APPEND_ONLY_UNENFORCED)
        self.assertTrue(caught.exception.details["missing"])

    def test_the_entry_hash_is_stable_and_keyed_on_the_predecessor(self):
        body = {
            "entry_id": "entry_0001",
            "seq": 1,
            "account": "CASH_IN",
            "direction": "DEBIT",
            "amount_minor": 100,
            "currency": "IDR",
            "booking_id": None,
            "invoice_id": None,
            "payment_id": None,
            "occurred_at": START,
            "memo": "probe entry",
        }
        digest = PAY.compute_entry_hash(PAY.GENESIS_HASH, body)
        self.assertEqual(digest, PAY.compute_entry_hash(PAY.GENESIS_HASH, dict(body)))
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, PAY.compute_entry_hash("b" * 64, body))
        self.assertNotEqual(digest, PAY.compute_entry_hash(PAY.GENESIS_HASH, {**body, "amount_minor": 101}))

    def test_the_hash_covers_the_declared_fields_only(self):
        fields = set(PAY.LEDGER_HASH_FIELDS)
        self.assertIn("amount_minor", fields)
        self.assertIn("occurred_at", fields)
        self.assertNotIn("entry_hash", fields)
        self.assertNotIn("prev_hash", fields)


# --------------------------------------------------------------------------- #
# 6. Reconciliation
# --------------------------------------------------------------------------- #
class TestReconciliation(PaymentsTestBase):
    def orphan_event(self, *, invoice: Invoice | None = None, amount: int = 2_300_000, event_id="evt_orphan01"):
        body = {"provider_intent_id": "pi_nobody_home", "amount_minor": amount, "currency": "IDR"}
        if invoice is not None:
            body["invoice_id"] = invoice.invoice_id
        return self.deliver(self.payload(**body), event_id=event_id)

    def test_unmatched_money_is_not_confirmed(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        event = self.orphan_event()
        self.assertFalse(event.was_applied)
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(self.paid(invoice), 0)
        self.assertEqual(self.service.get_payment(intent.payment_id).status, "PENDING")

    def test_a_case_carries_its_kind_and_the_declared_facts(self):
        event = self.orphan_event(amount=1_500_000)
        case = self.service.get_reconciliation(event.recon_id)
        self.assertEqual(case.kind, "UNMATCHED_PAYMENT")
        self.assertEqual(case.status, "OPEN")
        self.assertEqual(case.payment_event_id, event.event_id)
        self.assertEqual(case.details_dict["declared_amount_minor"], 1_500_000)
        self.assertEqual(case.details_dict["provider_event_id"], "evt_orphan01")
        # only whitelisted facts are kept; nothing raw, nothing guest-shaped
        allowed = {
            "declared_amount_minor",
            "declared_currency",
            "declared_invoice_id",
            "provider_event_id",
            "event_type",
            "reason",
        }
        self.assertLessEqual(set(case.details_dict), allowed)

    def test_the_deadline_is_forty_eight_hours(self):
        event = self.orphan_event()
        case = self.service.get_reconciliation(event.recon_id)
        expected = P.to_iso(
            P.parse_iso(case.detected_at) + P.timedelta(hours=PAY.RECONCILIATION_SLA_HOURS)
        )
        self.assertEqual(case.due_at, expected)
        self.assertGreater(case.due_at, case.detected_at)

    def test_the_case_points_at_the_declared_invoice_only_when_it_exists(self):
        invoice = self.issue_invoice()
        event = self.orphan_event(invoice=invoice, event_id="evt_orphan02")
        self.assertEqual(self.service.get_reconciliation(event.recon_id).invoice_id, invoice.invoice_id)
        other = self.orphan_event(event_id="evt_orphan03", amount=10)
        self.assertIsNone(self.service.get_reconciliation(other.recon_id).invoice_id)

    def test_every_case_raises_an_unmatched_payment_breach(self):
        event = self.orphan_event()
        breaches = self.service.breaches()
        self.assertEqual(len(breaches), 1)
        self.assertEqual(breaches[0]["kind"], PAY.UNMATCHED_PAYMENT_BREACH)
        self.assertEqual(breaches[0]["subject_type"], "payment")
        self.assertEqual(breaches[0]["status"], "OPEN")
        self.assertEqual(breaches[0]["subject_id"], event.event_id)

    def test_breaches_never_claim_an_unpassed_deadline(self):
        self.orphan_event()
        for row in self.service.breaches():
            with self.subTest(subject=row["subject_id"]):
                self.assertGreaterEqual(row["detected_at"], row["due_at"])
                self.assertGreaterEqual(row["breach_minutes"], 0)

    def test_a_breach_is_only_raised_once_per_payment(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id, amount_minor=intent.amount_minor - 1
        )
        self.deliver(payload_raw, event_id="evt_mismatch")
        self.deliver(payload_raw, event_id="evt_mismatch2")
        breaches = self.service.breaches(subject_id=intent.payment_id)
        self.assertEqual(len(breaches), 1)

    def test_resolving_needs_a_note(self):
        event = self.orphan_event()
        for bad in ("", "  ", "ok", None):
            with self.subTest(note=bad):
                with self.assertRaises(P.DomainError):
                    self.service.resolve_reconciliation(
                        recon_id=event.recon_id, resolution="UNMATCHED", note=bad, actor="ops_0001"
                    )
        self.assertTrue(self.service.get_reconciliation(event.recon_id).is_open)

    def test_resolving_an_unknown_case_is_refused(self):
        with self.assertRaises(PAY.ReconciliationNotFound) as caught:
            self.service.resolve_reconciliation(
                recon_id="recon_nope", resolution="UNMATCHED", note="nothing there", actor="ops_0001"
            )
        self.assertEqual(caught.exception.code, PAY.RECON_NOT_FOUND)

    def test_an_unknown_resolution_is_refused(self):
        event = self.orphan_event()
        with self.assertRaises(PAY.InvalidResolution) as caught:
            self.service.resolve_reconciliation(
                recon_id=event.recon_id,
                resolution="SHRUGGED",
                note="no idea what happened",
                actor="ops_0001",
            )
        self.assertEqual(caught.exception.code, PAY.INVALID_RESOLUTION)
        self.assertTrue(self.service.get_reconciliation(event.recon_id).is_open)

    def test_unmatched_resolution_closes_the_case_without_money(self):
        event = self.orphan_event()
        case = self.service.resolve_reconciliation(
            recon_id=event.recon_id,
            resolution="UNMATCHED",
            note="returned to sender by the bank",
            actor="ops_0001",
        )
        self.assertEqual(case.status, "RESOLVED")
        self.assertEqual(case.resolution_note, "returned to sender by the bank")
        self.assertEqual(case.owner_actor, "ops_0001")
        self.assertIsNotNone(case.resolved_at)
        self.assertEqual(self.ledger_count(), 0)
        breach = self.service.breaches()[0]
        self.assertEqual(breach["status"], "RESOLVED")
        self.assertEqual(breach["resolution_note"], "returned to sender by the bank")

    def test_waived_resolution_is_recorded_as_waived(self):
        event = self.orphan_event()
        case = self.service.resolve_reconciliation(
            recon_id=event.recon_id,
            resolution="WAIVED",
            note="bank fee, below the investigation threshold",
            actor="ops_0001",
        )
        self.assertEqual(case.status, "WAIVED")
        self.assertEqual(self.ledger_count(), 0)

    def test_a_closed_case_cannot_be_resolved_twice(self):
        event = self.orphan_event()
        self.service.resolve_reconciliation(
            recon_id=event.recon_id, resolution="UNMATCHED", note="investigated", actor="ops_0001"
        )
        with self.assertRaises(PAY.ReconciliationClosed) as caught:
            self.service.resolve_reconciliation(
                recon_id=event.recon_id,
                resolution="MATCHED",
                note="trying again",
                actor="ops_0001",
            )
        self.assertEqual(caught.exception.code, PAY.RECON_ALREADY_RESOLVED)

    def test_matched_without_a_real_invoice_is_refused(self):
        invoice = self.issue_invoice()
        event = self.orphan_event()
        with self.assertRaises(PAY.ReconciliationInvoiceMissing) as caught:
            self.service.resolve_reconciliation(
                recon_id=event.recon_id,
                resolution="MATCHED",
                note="matched to an invoice we cannot name",
                actor="ops_0001",
            )
        self.assertEqual(caught.exception.code, PAY.RECON_INVOICE_MISSING)
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(self.paid(invoice), 0)
        self.assertTrue(self.service.get_reconciliation(event.recon_id).is_open)

    def test_matched_against_a_void_invoice_is_refused(self):
        invoice = self.issue_invoice()
        event = self.orphan_event(invoice=invoice)
        self.invoices.void(invoice_id=invoice.invoice_id, reason="issued to the wrong booking")
        with self.assertRaises(PAY.ReconciliationInvoiceMissing):
            self.service.resolve_reconciliation(
                recon_id=event.recon_id,
                resolution="MATCHED",
                note="should not post to a void invoice",
                actor="ops_0001",
            )
        self.assertEqual(self.ledger_count(), 0)

    def test_matched_posts_the_received_money_onto_the_invoice(self):
        invoice = self.issue_invoice()
        event = self.orphan_event(invoice=invoice)
        case = self.service.resolve_reconciliation(
            recon_id=event.recon_id,
            resolution="MATCHED",
            note="bank statement shows this is the villa invoice",
            actor="accountant_0001",
        )
        self.assertEqual(case.status, "RESOLVED")
        self.assertIsNotNone(case.payment_id)
        payment = self.service.get_payment(case.payment_id)
        self.assertEqual(payment.status, "SUCCEEDED")
        self.assertEqual(payment.method, PAY.PAYMENT_METHOD_MANUAL)
        self.assertEqual(payment.amount_minor, 2_300_000)
        self.assertEqual(self.paid(invoice), 2_300_000)
        self.assertEqual(self.invoices.get(invoice.invoice_id).payment_status, "PAID")
        self.assertTrue(self.service.ledger_balance("IDR").balanced)
        self.assertTrue(PAY.verify_ledger(self.conn).ok)
        self.assertEqual(self.service.breaches()[0]["status"], "RESOLVED")

    def test_matched_twice_on_the_same_money_is_refused(self):
        invoice = self.issue_invoice()
        event = self.orphan_event(invoice=invoice)
        self.service.resolve_reconciliation(
            recon_id=event.recon_id, resolution="MATCHED", note="matched to the invoice", actor="ops_0001"
        )
        second = self.orphan_event(invoice=invoice, event_id="evt_orphan02")
        with self.assertRaises(PAY.PaymentAmountMismatch):
            self.service.resolve_reconciliation(
                recon_id=second.recon_id,
                resolution="MATCHED",
                note="same money, second claim",
                actor="ops_0001",
            )
        self.assertEqual(self.paid(invoice), invoice.total_minor)

    def test_unmatched_filters_on_the_deadline(self):
        event = self.orphan_event()
        self.assertEqual(len(self.service.unmatched()), 1)
        self.assertEqual(self.service.unmatched(now=START), ())
        self.clock.advance(hours=49)
        overdue = self.service.unmatched(now=self.clock.now())
        self.assertEqual([case.recon_id for case in overdue], [event.recon_id])

    def test_the_case_is_audited_when_it_opens_and_closes(self):
        event = self.orphan_event()
        actions = [row["action"] for row in self.rows("SELECT action FROM audit_events ORDER BY seq")]
        self.assertIn(PAY.AUDIT_RECONCILIATION_OPENED, actions)
        self.service.resolve_reconciliation(
            recon_id=event.recon_id, resolution="UNMATCHED", note="returned by the bank", actor="ops_0001"
        )
        actions = [row["action"] for row in self.rows("SELECT action FROM audit_events ORDER BY seq")]
        self.assertIn(PAY.AUDIT_RECONCILIATION_RESOLVED, actions)


# --------------------------------------------------------------------------- #
# 7. Refunds
# --------------------------------------------------------------------------- #
class TestRefund(PaymentsTestBase):
    def settled(self, *, amount: int | None = None):
        invoice = self.issue_invoice()
        intent, _ = self.settle(invoice)
        return invoice, intent

    def refund(self, payment_id: str, approval_id: Any, amount: Any = 500_000, **over):
        kwargs = {
            "payment_id": payment_id,
            "amount_minor": amount,
            "approval_id": approval_id,
            "actor": "accountant_0001",
            "actor_role": "accountant",
            "reason": "guest shortened the stay after settling",
        }
        kwargs.update(over)
        return self.service.refund(**kwargs)

    def test_a_refund_needs_a_settled_payment(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        approval = self.approve_refund(intent.payment_id)
        with self.assertRaises(PAY.PaymentNotSettled) as caught:
            self.refund(intent.payment_id, approval.approval_id)
        self.assertEqual(caught.exception.code, PAY.PAYMENT_NOT_SETTLED)
        self.assertEqual(self.ledger_count(), 0)

    def test_a_pending_approval_is_not_enough(self):
        _, intent = self.settled()
        approval = self.approvals.request(
            subject_type="refund",
            subject_id=intent.payment_id,
            action="refund.guest",
            requested_by="agent_0001",
            requested_role="agent",
            reason="guest cancelled the stay after settling",
            required_role="owner",
            required_approvals=2,
            money=P.Money(500_000, "IDR"),
            idempotency_key="idem_refund_0001",
        )
        with self.assertRaises(PAY.RefundNotApproved) as caught:
            self.refund(intent.payment_id, approval.approval_id)
        self.assertEqual(caught.exception.code, PAY.REFUND_APPROVAL_REQUIRED)
        self.assertEqual(self.ledger_count(), 4)

    def test_a_single_approver_is_not_enough_for_a_refund(self):
        _, intent = self.settled()
        approval = self.approvals.request(
            subject_type="refund",
            subject_id=intent.payment_id,
            action="refund.guest",
            requested_by="agent_0001",
            requested_role="agent",
            reason="guest cancelled the stay after settling",
            required_role="owner",
            required_approvals=2,
            money=P.Money(500_000, "IDR"),
            idempotency_key="idem_refund_0001",
        )
        approval = self.approvals.decide(
            approval.approval_id,
            approver="owner_0001",
            approver_role="owner",
            decision="APPROVE",
            note="first pair of eyes",
        )
        self.assertEqual(approval.status, "PENDING")
        with self.assertRaises(PAY.RefundNotApproved):
            self.refund(intent.payment_id, approval.approval_id)

    def test_an_unknown_approval_is_refused(self):
        _, intent = self.settled()
        with self.assertRaises(PAY.RefundNotApproved):
            self.refund(intent.payment_id, "approval_nope")
        self.assertEqual(self.ledger_count(), 4)

    def test_a_missing_approval_id_is_refused(self):
        _, intent = self.settled()
        for bad in ("", "   ", None, 12):
            with self.subTest(approval_id=bad):
                with self.assertRaises(PAY.RefundNotApproved) as caught:
                    self.refund(intent.payment_id, bad)
                self.assertEqual(caught.exception.code, PAY.REFUND_APPROVAL_REQUIRED)

    def test_an_approval_for_another_payment_is_refused(self):
        first_invoice = self.issue_invoice(key="idem_inv_0001")
        _, first_intent = self.settle(
            first_invoice,
            event_id="evt_first001",
            intent=self.raise_intent(first_invoice, idempotency_key="idem_pay_0001"),
        )
        second_invoice = self.issue_invoice(key="idem_inv_0002")
        _, second_intent = self.settle(
            second_invoice,
            event_id="evt_second01",
            intent=self.raise_intent(second_invoice, idempotency_key="idem_pay_0002"),
        )
        approval = self.approve_refund(first_intent.payment_id)
        with self.assertRaises(PAY.RefundNotApproved) as caught:
            self.refund(second_intent.payment_id, approval.approval_id)
        self.assertEqual(caught.exception.details["payment_id"], second_intent.payment_id)
        self.assertEqual(self.ledger_count(), 8)

    def test_a_rejected_approval_is_refused(self):
        _, intent = self.settled()
        approval = self.approvals.request(
            subject_type="refund",
            subject_id=intent.payment_id,
            action="refund.guest",
            requested_by="agent_0001",
            requested_role="agent",
            reason="guest cancelled the stay after settling",
            required_role="owner",
            required_approvals=2,
            money=P.Money(500_000, "IDR"),
            idempotency_key="idem_refund_0001",
        )
        approval = self.approvals.decide(
            approval.approval_id,
            approver="owner_0001",
            approver_role="owner",
            decision="REJECT",
            note="the guest already stayed the full period",
        )
        self.assertEqual(approval.status, "REJECTED")
        with self.assertRaises(PAY.RefundNotApproved):
            self.refund(intent.payment_id, approval.approval_id)

    def test_an_expired_approval_is_refused(self):
        _, intent = self.settled()
        approval = self.approvals.request(
            subject_type="refund",
            subject_id=intent.payment_id,
            action="refund.guest",
            requested_by="agent_0001",
            requested_role="agent",
            reason="guest cancelled the stay after settling",
            required_role="owner",
            required_approvals=2,
            money=P.Money(500_000, "IDR"),
            expires_minutes=10,
            idempotency_key="idem_refund_0001",
        )
        self.clock.advance(minutes=11)
        with self.assertRaises(ApprovalExpired):
            self.approvals.decide(
                approval.approval_id,
                approver="owner_0001",
                approver_role="owner",
                decision="APPROVE",
                note="late decision",
            )
        self.assertEqual(
            self.scalar(
                "SELECT status FROM approval_requests WHERE approval_id = ?",
                (approval.approval_id,),
            ),
            "EXPIRED",
        )
        with self.assertRaises(PAY.RefundNotApproved):
            self.refund(intent.payment_id, approval.approval_id)

    def test_an_approval_that_covers_less_is_refused(self):
        _, intent = self.settled()
        approval = self.approve_refund(intent.payment_id, amount=400_000)
        with self.assertRaises(PAY.RefundNotApproved) as caught:
            self.refund(intent.payment_id, approval.approval_id, amount=500_000)
        self.assertEqual(caught.exception.details["approved_minor"], 400_000)
        self.assertEqual(self.ledger_count(), 4)

    def test_an_approval_in_another_currency_is_refused(self):
        _, intent = self.settled()
        approval = self.approve_refund(intent.payment_id, amount=500_000, currency="USD")
        with self.assertRaises(PAY.RefundNotApproved) as caught:
            self.refund(intent.payment_id, approval.approval_id, amount=500_000)
        self.assertEqual(caught.exception.details["approved_currency"], "USD")

    def test_an_approved_status_without_decisions_is_not_trusted(self):
        _, intent = self.settled()
        approval = self.approvals.request(
            subject_type="refund",
            subject_id=intent.payment_id,
            action="refund.guest",
            requested_by="agent_0001",
            requested_role="agent",
            reason="guest cancelled the stay after settling",
            required_role="owner",
            required_approvals=2,
            money=P.Money(500_000, "IDR"),
            idempotency_key="idem_refund_0001",
        )
        # Someone flips the status column without any decision: the module reads
        # the decision log too, so this must not buy a refund.
        self.conn.execute(
            "UPDATE approval_requests SET status = 'APPROVED', closed_at = ? WHERE approval_id = ?",
            (START, approval.approval_id),
        )
        with self.assertRaises(PAY.RefundNotApproved) as caught:
            self.refund(intent.payment_id, approval.approval_id)
        self.assertEqual(caught.exception.details["approvals"], 0)
        self.assertEqual(self.ledger_count(), 4)

    def test_more_than_was_settled_is_refused(self):
        _, intent = self.settled()
        approval = self.approve_refund(intent.payment_id, amount=intent.amount_minor)
        with self.assertRaises(PAY.RefundExceedsPayment) as caught:
            self.refund(intent.payment_id, approval.approval_id, amount=intent.amount_minor + 1)
        self.assertEqual(caught.exception.code, PAY.REFUND_EXCEEDS_PAYMENT)
        self.assertEqual(caught.exception.details["settled_minor"], intent.amount_minor)
        self.assertEqual(self.ledger_count(), 4)

    def test_partial_refunds_cannot_add_up_to_more_than_settled(self):
        _, intent = self.settled()
        approval = self.approve_refund(intent.payment_id, amount=2_000_000)
        first = self.refund(intent.payment_id, approval.approval_id, amount=2_000_000)
        self.assertEqual(first.refunded_total_minor, 2_000_000)
        self.assertFalse(first.fully_refunded)
        with self.assertRaises(PAY.RefundExceedsPayment) as caught:
            self.refund(intent.payment_id, approval.approval_id, amount=500_000)
        self.assertEqual(caught.exception.details["refunded_minor"], 2_000_000)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM ledger_entries WHERE account = 'REFUND_OUT'"), 1)

    def test_a_refund_writes_refund_out_and_lowers_the_invoice(self):
        invoice, intent = self.settled()
        approval = self.approve_refund(intent.payment_id, amount=500_000)
        refund = self.refund(intent.payment_id, approval.approval_id, amount=500_000)
        self.assertEqual(refund.payment_id, intent.payment_id)
        self.assertEqual(refund.amount_minor, 500_000)
        self.assertEqual(refund.approval_id, approval.approval_id)
        self.assertEqual(refund.currency, "IDR")
        entries = self.service.entries(payment_id=intent.payment_id)
        refund_rows = [e for e in entries if e.account in ("REFUND_OUT", "CASH_IN") and e.seq > 4]
        self.assertEqual(len(refund_rows), 2)
        self.assertEqual(
            {(e.account, e.direction, e.amount_minor) for e in refund_rows},
            {("REFUND_OUT", "DEBIT", 500_000), ("CASH_IN", "CREDIT", 500_000)},
        )
        self.assertEqual(self.paid(invoice), invoice.total_minor - 500_000)
        self.assertEqual(self.invoices.get(invoice.invoice_id).payment_status, "REFUNDED")
        balance = self.service.ledger_balance("IDR")
        self.assertTrue(balance.balanced)
        self.assertEqual(balance.by_account["REFUND_OUT"]["DEBIT"], 500_000)
        self.assertTrue(PAY.verify_ledger(self.conn).ok)

    def test_a_full_refund_marks_the_payment_refunded(self):
        invoice, intent = self.settled()
        approval = self.approve_refund(intent.payment_id, amount=intent.amount_minor)
        refund = self.refund(intent.payment_id, approval.approval_id, amount=intent.amount_minor)
        self.assertTrue(refund.fully_refunded)
        self.assertEqual(self.service.get_payment(intent.payment_id).status, "REFUNDED")
        self.assertEqual(self.paid(invoice), 0)

    def test_a_refunded_payment_cannot_be_settled_again(self):
        invoice, intent = self.settled()
        approval = self.approve_refund(intent.payment_id, amount=intent.amount_minor)
        self.refund(intent.payment_id, approval.approval_id, amount=intent.amount_minor)
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id, amount_minor=intent.amount_minor
        )
        event = self.deliver(payload_raw, event_id="evt_again001")
        self.assertFalse(event.was_applied)
        self.assertEqual(self.paid(invoice), 0)

    def test_a_refund_needs_a_reason(self):
        _, intent = self.settled()
        approval = self.approve_refund(intent.payment_id)
        for bad in ("", "  ", None):
            with self.subTest(reason=bad):
                with self.assertRaises(P.DomainError):
                    self.refund(intent.payment_id, approval.approval_id, reason=bad)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM ledger_entries WHERE account = 'REFUND_OUT'"), 0)

    def test_a_refund_amount_must_be_positive(self):
        _, intent = self.settled()
        approval = self.approve_refund(intent.payment_id)
        for bad in (0, -1, True, "500"):
            with self.subTest(amount=bad):
                with self.assertRaises(P.DomainError):
                    self.refund(intent.payment_id, approval.approval_id, amount=bad)

    def test_the_refund_is_audited_with_its_approval(self):
        _, intent = self.settled()
        approval = self.approve_refund(intent.payment_id)
        self.refund(intent.payment_id, approval.approval_id)
        row = self.rows(
            "SELECT * FROM audit_events WHERE action = ?", (PAY.AUDIT_REFUND_RECORDED,)
        )[0]
        self.assertEqual(row["entity_id"], intent.payment_id)
        self.assertEqual(row["actor"], "accountant_0001")
        self.assertIn(approval.approval_id, row["after_state"])

    def test_the_refund_moves_the_invoice_through_the_invoice_service(self):
        invoice, intent = self.settled()
        approval = self.approve_refund(intent.payment_id, amount=500_000)
        calls: list[tuple] = []
        original = InvoiceService.record_settlement

        def spy(invoice_service, invoice_id, **kwargs):
            calls.append((invoice_id, kwargs["amount_paid_minor"], kwargs["payment_status"]))
            return original(invoice_service, invoice_id, **kwargs)

        with mock.patch.object(InvoiceService, "record_settlement", new=spy):
            self.refund(intent.payment_id, approval.approval_id, amount=500_000)
        self.assertEqual(
            calls, [(invoice.invoice_id, invoice.total_minor - 500_000, "REFUNDED")]
        )


# --------------------------------------------------------------------------- #
# 8. The read side
# --------------------------------------------------------------------------- #
class TestQueries(PaymentsTestBase):
    def test_get_payment_of_an_unknown_id_is_refused(self):
        with self.assertRaises(PAY.PaymentNotFound) as caught:
            self.service.get_payment("payment_nope")
        self.assertEqual(caught.exception.code, PAY.PAYMENT_NOT_FOUND)

    def test_for_invoice_of_an_unknown_invoice_is_empty(self):
        self.assertEqual(self.service.for_invoice("inv_unknown"), ())

    def test_pending_intents_keeps_the_live_ones_and_expires_the_rest(self):
        invoice = self.issue_invoice()
        stale = self.raise_intent(invoice, idempotency_key="idem_pay_0001")
        self.clock.advance(minutes=31)
        live = self.raise_intent(invoice, idempotency_key="idem_pay_0002")
        pending = self.service.pending_intents()
        self.assertEqual([p.payment_id for p in pending], [live.payment_id])
        self.assertEqual(self.service.get_payment(stale.payment_id).status, "EXPIRED")
        self.assertEqual(
            self.service.get_payment(stale.payment_id).provider_status, "intent_expired"
        )
        actions = [row["action"] for row in self.rows("SELECT action FROM audit_events ORDER BY seq")]
        self.assertIn(PAY.AUDIT_INTENT_EXPIRED, actions)

    def test_an_expired_intent_reports_no_live_hosted_url(self):
        invoice = self.issue_invoice()
        self.raise_intent(invoice)
        self.clock.advance(minutes=31)
        self.service.pending_intents()
        replay = self.raise_intent(invoice)
        self.assertEqual(replay.status, "EXPIRED")
        self.assertEqual(replay.hosted_url, "")

    def test_events_for_payment_lists_only_its_webhooks(self):
        invoice = self.issue_invoice()
        intent, first = self.settle(invoice)
        payload_raw = self.payload(
            provider_intent_id=intent.provider_intent_id, amount_minor=intent.amount_minor
        )
        second = self.deliver(payload_raw, event_id="evt_second01")
        events = self.service.events_for_payment(intent.payment_id)
        self.assertEqual([e.event_id for e in events], [first.event_id, second.event_id])
        self.assertEqual(self.service.events_for_payment("payment_nope"), ())

    def test_entries_can_be_filtered(self):
        invoice = self.issue_invoice()
        intent, _ = self.settle(invoice)
        self.assertEqual(len(self.service.entries(payment_id=intent.payment_id)), 4)
        self.assertEqual(len(self.service.entries(invoice_id=invoice.invoice_id)), 4)
        self.assertEqual(
            len(self.service.entries(payment_id=intent.payment_id, invoice_id=invoice.invoice_id)),
            4,
        )
        self.assertEqual(self.service.entries(payment_id="payment_nope"), ())
        self.assertEqual(len(self.service.entries()), 4)

    def test_a_payment_exposes_its_money_and_expiry(self):
        invoice = self.issue_invoice()
        intent = self.raise_intent(invoice)
        payment = self.service.get_payment(intent.payment_id)
        self.assertTrue(payment.is_pending)
        self.assertFalse(payment.is_settled)
        self.assertEqual(payment.amount, P.Money(invoice.total_minor, "IDR"))
        self.assertTrue(payment.is_expired_at(P.to_iso(P.parse_iso(intent.expires_at) + P.timedelta(seconds=1))))
        self.assertFalse(payment.is_expired_at(intent.created_at))

    def test_breaches_only_speak_about_unmatched_payments(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        self.assertEqual(self.service.breaches(), ())
        self.deliver(
            self.payload(provider_intent_id="pi_orphan_0001", amount_minor=1),
            event_id="evt_orphan01",
        )
        self.assertEqual(len(self.service.breaches()), 1)

    def test_the_service_verifier_matches_the_module_verifier(self):
        invoice = self.issue_invoice()
        self.settle(invoice)
        self.assertEqual(self.service.verify().as_dict(), PAY.verify_ledger(self.conn).as_dict())

    def test_the_balance_rejects_an_unknown_currency(self):
        with self.assertRaises(P.DomainError):
            self.service.ledger_balance("XYZ")


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
