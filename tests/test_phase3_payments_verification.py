"""Independent adversarial probe for ``src/payments.py`` + ``src/invoicing.py``.

This file is *not* owned by the authors of the modules it tests. It was written
from the storage contract (``src/schema/001_initial.sql``) and from the public
service APIs only, and every single test re-reads the result from SQLite with a
``SELECT`` — a returned object or an exception type never counts as evidence on
its own. Nothing here is mocked, no dependency outside the standard library is
used, and no module under test is modified.

The properties this probe tries to falsify (each is at least one test, most are
several):

1. A webhook whose signature is wrong or absent is *recorded as evidence* and
   never applied: ``payment_events.signature_verified = 0``, ``applied = 0``, a
   rejection reason, no ``ledger_entries`` row, and an invoice whose
   ``status``/``payment_status``/``amount_paid_minor`` are byte-identical.
2. The same ``(provider, provider_event_id)`` delivered twice yields exactly one
   ``payment_events`` row and one ledger batch; a *cloned* event (new id) cannot
   settle the same invoice twice either.
3. Settlement is idempotent: recording it twice neither doubles
   ``invoices.amount_paid_minor`` nor appends ledger rows, and a settled invoice
   can never raise a second intent.
4. After every flow the ledger is balanced (sum of DEBIT == sum of CREDIT),
   every ``entry_hash`` recomputes from the stored row (independent oracle, not
   the module's own helper), the chain links to its predecessor, and the ledger
   is append-only at the database level.
5. Money that cannot be matched opens a ``reconciliation_cases`` row that is
   ``OPEN`` with ``due_at`` exactly 48 hours after ``detected_at``, with an owner,
   an ``UNMATCHED_PAYMENT`` breach, and an invoice that is still unpaid with no
   ledger row.
6. ``invoices.total_minor`` always equals ``subtotal + tax + service``, the tax
   bucket is taken from the property version's ``tax_pbjt_pct`` (two policies
   with different percentages really do produce different tax), the stored
   ``invoice_lines`` sum back to the stored buckets, a ``VOID`` invoice can never
   hold paid money, and one idempotency key can never issue two invoices.
7. No column in the money trail can hold card data: the module's ``INSERT``
   statements name provider identifiers only, a payload carrying a PAN is
   refused, and no PAN (or any Luhn-valid run) reaches any text column.
8. Money stays an exact integer in the database (``typeof(col) == 'integer'``);
   a float or a bool is refused on every write path.

The last class in this file, :class:`TestPreviouslyReproducedDefectsStayFixed`,
is regression cover for three defects this probe first reproduced from the
outside and reported RED, with the SQL evidence in the assertion message:

* ``invoicing.record_settlement`` mirrored a *stale* invoice onto ``bookings``
  (``_sync_booking(current)`` instead of the just-updated row): after a full
  settlement the booking stayed ``UNPAID`` with ``amount_paid_minor = 0``.
* ``payments._post_settlement`` recomputed the tax share from the *whole*
  invoice on every instalment (``tax = min(invoice.tax_minor, amount)``), so two
  partial settlements credited ``TAX_PAYABLE`` twice (200_000 for an invoice
  whose tax is 100_000) while ``OWNER_PAYABLE`` was short by the same amount.
  The journal still balanced, so only per-account totals revealed it.
* ``invoicing`` could not write at all without ``payments`` having been imported
  first, because ``payments`` patched ``primitives.Money`` at import time.

All three have since been repaired — the assertions below are the *correct*
contract, so they now pass and act as regression cover. They were left strong (no
``skip``, no ``expectedFailure``) and were re-checked against a throwaway copy of
the repaired modules carrying the old logic again, where both money defects go
red with the original evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import audit as A  # noqa: E402
import invoicing as INV  # noqa: E402
import payments as PAY  # noqa: E402
import policy_engine as PE  # noqa: E402
import primitives as P  # noqa: E402
import quote_calculator as QC  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402
from seed_support import SeedMixin  # noqa: E402

# The frozen test instants (mirroring tests/seed_support.py).
START = "2026-09-21T08:00:00+00:00"
CREATED = "2026-09-18T08:00:00+00:00"
PROVIDER = "dummy"
SECRET = "dummy-webhook-secret"
CURRENCY = "IDR"
HASH = "a" * 64
#: A declared invoice shared by the seeded rows: 900_000 + 100_000 tax.
SEEDED_INVOICE = "inv_0001"
SEEDED_BOOKING = "book_0001"
SEEDED_TOTAL = 1_000_000
SEEDED_TAX = 100_000
#: A Luhn-valid test PAN. It must never be stored, echoed or hashed into a column.
PAN = "4111111111111111"

# Restated on purpose: the oracle below must not borrow the module's own
# constants, otherwise "the chain verifies" would only prove the module agrees
# with itself.
GENESIS_HASH = "0" * 64
LEDGER_HASH_DOMAIN = "ledger_entry:v1"
LEDGER_HASH_FIELDS = (
    "entry_id",
    "seq",
    "account",
    "direction",
    "amount_minor",
    "currency",
    "booking_id",
    "invoice_id",
    "payment_id",
    "occurred_at",
    "memo",
)
EVENT_HASH_DOMAIN = "payment_event:v1"

#: Tables of the money trail, swept for card data and for float money.
MONEY_TABLES = (
    "invoices",
    "invoice_lines",
    "payments",
    "payment_events",
    "ledger_entries",
    "reconciliation_cases",
    "bookings",
    "quotes",
)

_PAN_RUN = re.compile(r"\d(?:[ -]?\d){12,18}")
_INSERT = re.compile(r"INSERT INTO\s+([a-z_]+)\s*\(([^)]*)\)")
_CARD_TOKENS = re.compile(r"card|pan|cvc|cvv|ccnum|ccnumber|expiry")
#: Column names that merely *contain* "card" because they reference the pricing
#: catalogue (``rate_cards``), not a cardholder. Nothing else may look card-ish.
CARD_COLUMN_ALLOWLIST = frozenset({"ratecardid", "ratecardfingerprint"})


def _luhn_holds(digits: str) -> bool:
    """An independent Luhn checksum — the probe's own, not the module's."""
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def contains_pan(value: Any) -> bool:
    """True when a string carries something that could be a card number."""
    if not isinstance(value, str):
        return False
    for match in _PAN_RUN.finditer(value):
        digits = re.sub(r"[ -]", "", match.group(0))
        if not 13 <= len(digits) <= 19 or len(set(digits)) < 2:
            continue
        if _luhn_holds(digits):
            return True
    return False


class MoneyProbe(unittest.TestCase, SeedMixin):
    """In-memory SQLite + frozen clock + a seeded booking and invoice."""

    conn: Any

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.clock = P.FrozenClock.at(START)
        self.ids = P.IdFactory(deterministic=True, seed="phase3probe")
        self.invoices = INV.InvoiceService(self.conn, clock=self.clock, ids=self.ids)
        self.payments = PAY.PaymentService(
            self.conn, clock=self.clock, ids=self.ids, provider=PROVIDER
        )
        # Row builders from tests/seed_support.py — no hand-rolled seeder here.
        self.inventory()
        self.booking()
        self.invoice()

    # ------------------------------------------------------------------ reads #
    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict:
        found = self.rows(sql, params)
        self.assertEqual(len(found), 1, f"expected exactly one row for {sql!r}, got {found!r}")
        return found[0]

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def count(self, table: str) -> int:
        return int(self.scalar(f"SELECT count(*) FROM {table}"))

    def invoice_row(self, invoice_id: str = SEEDED_INVOICE) -> dict:
        return self.one("SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,))

    def payment_row(self, payment_id: str) -> dict:
        return self.one("SELECT * FROM payments WHERE payment_id = ?", (payment_id,))

    def event_row(self, provider_event_id: str) -> dict:
        return self.one(
            "SELECT * FROM payment_events WHERE provider = ? AND provider_event_id = ?",
            (PROVIDER, provider_event_id),
        )

    def recon_rows(self, kind: str | None = None) -> list[dict]:
        if kind is None:
            return self.rows("SELECT * FROM reconciliation_cases ORDER BY detected_at, recon_id")
        return self.rows(
            "SELECT * FROM reconciliation_cases WHERE kind = ? ORDER BY recon_id", (kind,)
        )

    def snapshot(self) -> dict:
        """Row counts plus the money sums: the witness for 'nothing was written'."""
        state = {
            table: self.count(table)
            for table in (
                "payments",
                "payment_events",
                "ledger_entries",
                "reconciliation_cases",
                "sla_breaches",
                "invoices",
                "invoice_lines",
                "idempotency_keys",
                "audit_events",
            )
        }
        state["invoice_paid_minor"] = int(
            self.scalar("SELECT coalesce(sum(amount_paid_minor), 0) FROM invoices")
        )
        state["booking_paid_minor"] = int(
            self.scalar("SELECT coalesce(sum(amount_paid_minor), 0) FROM bookings")
        )
        state["ledger_head"] = self.scalar(
            "SELECT entry_hash FROM ledger_entries ORDER BY seq DESC LIMIT 1"
        )
        state["ledger_seq"] = self.scalar("SELECT max(seq) FROM ledger_entries")
        return state

    def assert_unchanged(self, before: dict, message: str = "") -> None:
        self.assertEqual(self.snapshot(), before, message or "the refused call wrote rows")

    # ------------------------------------------------------- ledger oracle --- #
    def ledger_rows(self) -> list[dict]:
        return self.rows("SELECT * FROM ledger_entries ORDER BY seq")

    def assert_ledger_evidence(self) -> list[dict]:
        """Recompute the chain and the balance straight from the stored rows.

        Deliberately does not call ``payments.verify_ledger`` for the verdict: the
        hash is recomputed here from ``primitives.sha256_hex`` with the domain and
        the field list restated above, so a module that quietly changed its own
        hashing or its own verifier cannot hide behind itself.
        """
        rows = self.ledger_rows()
        debits = credits = 0
        per_currency: dict[str, list[int]] = {}
        prev_hash = GENESIS_HASH
        for expected_seq, row in enumerate(rows, start=1):
            self.assertEqual(row["seq"], expected_seq, "ledger seq must be gapless for the chain")
            self.assertEqual(
                row["prev_hash"], prev_hash, f"row {row['seq']} does not link to its predecessor"
            )
            body = {name: row[name] for name in LEDGER_HASH_FIELDS}
            recomputed = P.sha256_hex(LEDGER_HASH_DOMAIN, prev_hash, P.canonical_json(body))
            self.assertEqual(
                recomputed,
                row["entry_hash"],
                f"row {row['seq']} does not hash to its stored entry_hash",
            )
            bucket = per_currency.setdefault(row["currency"], [0, 0])
            if row["direction"] == "DEBIT":
                debits += row["amount_minor"]
                bucket[0] += row["amount_minor"]
            else:
                credits += row["amount_minor"]
                bucket[1] += row["amount_minor"]
            prev_hash = row["entry_hash"]
        for currency, (currency_debits, currency_credits) in per_currency.items():
            self.assertEqual(
                currency_debits,
                currency_credits,
                f"ledger is not balanced in {currency}: {currency_debits} != {currency_credits}",
            )
        sql_debits = int(
            self.scalar(
                "SELECT coalesce(sum(amount_minor), 0) FROM ledger_entries WHERE direction = 'DEBIT'"
            )
        )
        sql_credits = int(
            self.scalar(
                "SELECT coalesce(sum(amount_minor), 0) FROM ledger_entries WHERE direction = 'CREDIT'"
            )
        )
        self.assertEqual(sql_debits, sql_credits, "SQL totals disagree: the ledger is unbalanced")
        self.assertEqual(sql_debits, debits, "the SQL sum and the oracle disagree")
        self.assertEqual(sql_credits, credits, "the SQL sum and the oracle disagree")
        return rows

    def ledger_credits(self) -> dict[str, int]:
        return {
            row["account"]: int(row["total"])
            for row in self.rows(
                "SELECT account, sum(amount_minor) AS total FROM ledger_entries"
                " WHERE direction = 'CREDIT' GROUP BY account"
            )
        }

    def audit_report(self) -> Any:
        report = A.AuditLog(self.conn, clock=self.clock, ids=self.ids).verify()
        self.assertGreater(report.count, 0, "the audit chain is empty after a write")
        return report

    # -------------------------------------------------------- webhook rig ---- #
    def payload(self, **over: Any) -> str:
        body: dict[str, Any] = {
            "provider_intent_id": "pi_ghost",
            "amount_minor": SEEDED_TOTAL,
            "currency": CURRENCY,
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
    ) -> Any:
        return self.payments.handle_webhook(
            provider=provider,
            event_id=event_id,
            event_type=event_type,
            payload_raw=payload_raw,
            signature=self.sign(payload_raw, secret) if signature is None else signature,
            secret=secret,
        )

    def raise_intent(
        self,
        *,
        invoice_id: str = SEEDED_INVOICE,
        amount_minor: int | None = None,
        key: str = "idem_pay_0001",
        created_by: str = "agent_0001",
    ) -> Any:
        if amount_minor is None:
            amount_minor = self.invoice_row(invoice_id)["total_minor"] - self.invoice_row(
                invoice_id
            )["amount_paid_minor"]
        return self.payments.create_intent(
            invoice_id=invoice_id,
            amount_minor=amount_minor,
            currency=self.invoice_row(invoice_id)["currency"],
            idempotency_key=key,
            created_by=created_by,
        )

    def settle(self, *, event_id: str = "evt_000001", intent: Any = None) -> tuple[Any, Any]:
        intent = intent if intent is not None else self.raise_intent()
        raw = self.payload(
            provider_intent_id=intent.provider_intent_id,
            amount_minor=intent.amount_minor,
            currency=intent.currency,
        )
        return intent, self.deliver(raw, event_id=event_id)

    def line(self, **over: Any) -> dict:
        payload: dict[str, Any] = {
            "kind": "NIGHTLY",
            "description": "2 nights stay",
            "quantity": 2,
            "unit_amount_minor": 500_000,
        }
        payload.update(over)
        return payload

    def issue(self, *, booking_id: str = SEEDED_BOOKING, key: str = "idem_inv_0001", lines=None):
        return self.invoices.issue(
            booking_id=booking_id,
            currency=CURRENCY,
            policy_fingerprint=HASH,
            lines=lines if lines is not None else [self.line()],
            idempotency_key=key,
        )

    # ------------------------------------------------------------ queries --- #
    def text_values(self) -> list[tuple[str, str, str]]:
        """Every stored text value, as ``(table, column, value)``."""
        found: list[tuple[str, str, str]] = []
        for table in MONEY_TABLES:
            for row in self.rows(f"SELECT * FROM {table}"):
                for column, value in row.items():
                    if isinstance(value, str):
                        found.append((table, column, value))
        return found

    def money_columns(self, table: str) -> list[str]:
        return [
            row["name"]
            for row in self.rows(f"PRAGMA table_info({table})")
            if row["name"].endswith("_minor") or row["name"] == "amount_minor"
        ]


# --------------------------------------------------------------------------- #
# 1. A signature that does not verify can never move money
# --------------------------------------------------------------------------- #
class TestSignatureFailClosed(MoneyProbe):
    def test_a_forged_signature_is_recorded_unverified_and_moves_nothing(self) -> None:
        intent = self.raise_intent()
        raw = self.payload(provider_intent_id=intent.provider_intent_id)
        before = self.snapshot()

        with self.assertRaises(PAY.WebhookSignatureInvalid) as raised:
            self.deliver(raw, event_id="evt_forged", signature="0" * 64)
        self.assertEqual(raised.exception.code, PAY.WEBHOOK_SIGNATURE_INVALID)

        row = self.event_row("evt_forged")
        self.assertEqual(row["signature_verified"], 0)
        self.assertEqual(row["applied"], 0)
        self.assertIsNotNone(row["rejection_reason"])
        self.assertGreaterEqual(len(row["rejection_reason"]), 3)

        state = self.snapshot()
        self.assertEqual(state["ledger_entries"], 0, "a forged webhook posted a ledger row")
        self.assertEqual(
            state["payment_events"], before["payment_events"] + 1, "the refusal must be evidence"
        )
        for table in ("payments", "reconciliation_cases", "sla_breaches"):
            self.assertEqual(state[table], before[table], f"a forged webhook wrote {table}")

        invoice = self.invoice_row()
        self.assertEqual(invoice["amount_paid_minor"], 0)
        self.assertEqual(invoice["payment_status"], "UNPAID")
        self.assertEqual(invoice["status"], "ISSUED")
        self.assertEqual(self.payment_row(intent.payment_id)["status"], "PENDING")
        self.assertIsNone(self.payment_row(intent.payment_id)["settled_at"])
        self.assertEqual(self.ledger_rows(), [])

    def test_a_webhook_with_no_signature_at_all_is_recorded_and_never_applied(self) -> None:
        self.raise_intent()
        raw = self.payload()
        before = self.snapshot()

        with self.assertRaises(PAY.WebhookSignatureInvalid):
            self.deliver(raw, event_id="evt_unsigned", signature="")

        row = self.event_row("evt_unsigned")
        self.assertEqual((row["signature_verified"], row["applied"]), (0, 0))
        state = self.snapshot()
        self.assertEqual(state["ledger_entries"], 0)
        self.assertEqual(state["payment_events"], before["payment_events"] + 1)
        self.assertEqual(self.invoice_row()["amount_paid_minor"], 0)

    def test_a_signature_over_a_different_payload_never_applies(self) -> None:
        intent = self.raise_intent()
        raw = self.payload(provider_intent_id=intent.provider_intent_id, amount_minor=SEEDED_TOTAL)
        other = self.payload(provider_intent_id=intent.provider_intent_id, amount_minor=1)
        before = self.snapshot()

        with self.assertRaises(PAY.WebhookSignatureInvalid):
            self.deliver(raw, event_id="evt_swapped", signature=self.sign(other))

        row = self.event_row("evt_swapped")
        self.assertEqual((row["signature_verified"], row["applied"]), (0, 0))
        state = self.snapshot()
        # The refusal itself is the only thing that may be written.
        self.assertEqual(state["payment_events"], before["payment_events"] + 1)
        for table in (
            "payments",
            "ledger_entries",
            "reconciliation_cases",
            "sla_breaches",
            "invoices",
            "invoice_lines",
        ):
            self.assertEqual(state[table], before[table], f"a swapped signature wrote {table}")
        self.assertEqual(state["invoice_paid_minor"], before["invoice_paid_minor"])
        self.assertEqual(self.invoice_row()["payment_status"], "UNPAID")

    def test_a_rejected_event_cannot_be_replayed_into_an_applied_one(self) -> None:
        intent = self.raise_intent()
        raw = self.payload(provider_intent_id=intent.provider_intent_id)

        with self.assertRaises(PAY.WebhookSignatureInvalid):
            self.deliver(raw, event_id="evt_frozen", signature="f" * 64)
        before = self.snapshot()

        # The same event id, now with a signature that *does* verify: the stored
        # refusal is the record of truth, so this replay changes nothing.
        replayed = self.deliver(raw, event_id="evt_frozen")
        self.assertTrue(replayed.duplicate, "a stored refusal must be returned as a duplicate")

        row = self.event_row("evt_frozen")
        self.assertEqual((row["signature_verified"], row["applied"]), (0, 0))
        self.assert_unchanged(before, "a replayed event turned a refusal into an application")
        self.assertEqual(self.invoice_row()["amount_paid_minor"], 0)

    def test_every_refused_webhook_is_kept_as_evidence(self) -> None:
        for index in range(3):
            with self.assertRaises(PAY.WebhookSignatureInvalid):
                self.deliver(self.payload(), event_id=f"evt_refused_{index}", signature="deadbeef")

        rows = self.rows(
            "SELECT * FROM payment_events WHERE signature_verified = 0 AND applied = 0"
        )
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertGreaterEqual(len(row["rejection_reason"]), 3)
        self.assertEqual(self.count("ledger_entries"), 0)

    def test_a_webhook_from_another_provider_writes_nothing(self) -> None:
        before = self.snapshot()
        with self.assertRaises(PAY.WebhookProviderMismatch) as raised:
            self.deliver(self.payload(), event_id="evt_other", provider="someone-else")
        self.assertEqual(raised.exception.code, PAY.PROVIDER_MISMATCH)
        self.assert_unchanged(before, "a foreign provider webhook reached storage")

    def test_the_signature_is_hmac_sha256_over_the_exact_bytes(self) -> None:
        intent = self.raise_intent()
        raw = self.payload(provider_intent_id=intent.provider_intent_id)
        expected = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(
            self.sign(raw), expected, "the probe's own HMAC disagrees with the test rig"
        )

        # A SHA-1 digest of the same payload is not a valid signature.
        with self.assertRaises(PAY.WebhookSignatureInvalid):
            self.deliver(
                raw,
                event_id="evt_sha1",
                signature=hmac.new(SECRET.encode(), raw.encode(), hashlib.sha1).hexdigest(),
            )
        self.assertEqual(self.event_row("evt_sha1")["applied"], 0)

        # ... and the real one applies, so the refusal above was about the digest.
        applied, event = self.settle(intent=intent)
        self.assertEqual(self.event_row(event.provider_event_id)["applied"], 1)
        self.assertEqual(self.invoice_row()["amount_paid_minor"], SEEDED_TOTAL)

    def test_the_stored_payload_hash_is_the_hash_of_exactly_what_was_signed(self) -> None:
        raw = self.payload(provider_intent_id="pi_ghost", marker="keep-out")
        with self.assertRaises(PAY.WebhookSignatureInvalid):
            self.deliver(raw, event_id="evt_hash", signature="0" * 64)
        row = self.event_row("evt_hash")
        expected = P.sha256_hex(EVENT_HASH_DOMAIN, PROVIDER, "evt_hash", raw)
        self.assertEqual(row["payload_sha256"], expected)
        self.assertNotIn(raw, row["payload_redacted"])


# --------------------------------------------------------------------------- #
# 2 + 3. Event and settlement idempotency
# --------------------------------------------------------------------------- #
class TestSettlementIdempotency(MoneyProbe):
    def test_the_same_event_delivered_twice_writes_one_row_and_one_ledger_batch(self) -> None:
        intent, first = self.settle()
        self.assertEqual(first.applied, 1)
        after_first = self.snapshot()
        ledger_batch = self.ledger_rows()
        self.assertTrue(ledger_batch, "a settlement must post a ledger batch")

        second = self.deliver(
            self.payload(provider_intent_id=intent.provider_intent_id, amount_minor=SEEDED_TOTAL),
            event_id=first.provider_event_id,
        )
        self.assertTrue(second.duplicate)
        self.assertEqual(second.applied, 1)

        self.assertEqual(self.count("payment_events"), 1)
        self.assert_unchanged(after_first, "replaying the same event wrote something")
        self.assertEqual(self.ledger_rows(), ledger_batch)
        self.assertEqual(self.invoice_row()["amount_paid_minor"], SEEDED_TOTAL)

    def test_a_cloned_success_event_cannot_settle_the_same_invoice_twice(self) -> None:
        intent, first = self.settle()
        after_first = self.snapshot()

        clone = self.deliver(
            self.payload(provider_intent_id=intent.provider_intent_id, amount_minor=SEEDED_TOTAL),
            event_id="evt_clone_0001",
        )
        self.assertEqual(clone.applied, 0, "a cloned success event must not be applied")
        self.assertEqual(clone.signature_verified, 1)

        case = self.one(
            "SELECT * FROM reconciliation_cases WHERE kind = 'DUPLICATE_EVENT' ORDER BY recon_id"
        )
        self.assertEqual(case["status"], "OPEN")
        self.assertEqual(case["payment_id"], intent.payment_id)
        self.assertEqual(case["due_at"], "2026-09-23T08:00:00+00:00")

        state = self.snapshot()
        self.assertEqual(state["ledger_entries"], after_first["ledger_entries"])
        self.assertEqual(state["invoice_paid_minor"], SEEDED_TOTAL)
        self.assertEqual(self.invoice_row()["amount_paid_minor"], SEEDED_TOTAL)
        self.assertEqual(self.payment_row(intent.payment_id)["status"], "SUCCEEDED")

    def test_recording_the_settlement_twice_does_not_double_the_paid_amount(self) -> None:
        self.settle()
        first = self.invoice_row()
        ledger_batch = self.ledger_rows()

        self.invoices.record_settlement(
            SEEDED_INVOICE, amount_paid_minor=SEEDED_TOTAL, payment_status="PAID"
        )
        second = self.invoice_row()
        self.assertEqual(second["amount_paid_minor"], first["amount_paid_minor"])
        self.assertEqual(second["amount_paid_minor"], SEEDED_TOTAL)
        self.assertEqual(second["payment_status"], "PAID")
        self.assertEqual(self.ledger_rows(), ledger_batch, "a re-recorded settlement posted money")

    def test_a_paid_invoice_can_never_raise_a_second_intent(self) -> None:
        self.settle()
        before = self.snapshot()
        for amount in (1, SEEDED_TOTAL):
            with self.assertRaises(PAY.PaymentAmountMismatch):
                self.raise_intent(amount_minor=amount, key=f"idem_pay_after_{amount}")
        self.assert_unchanged(before, "an intent was raised for an invoice that owes nothing")

    def test_the_intent_key_replays_to_the_same_payment_row(self) -> None:
        first = self.raise_intent(key="idem_pay_replay")
        second = self.raise_intent(key="idem_pay_replay")
        self.assertEqual(first.payment_id, second.payment_id)
        self.assertEqual(self.count("payments"), 1)
        self.assertEqual(self.count("idempotency_keys"), 1)
        self.assertEqual(self.payment_row(first.payment_id)["status"], "PENDING")

    def test_a_reused_intent_key_with_another_request_is_refused(self) -> None:
        self.raise_intent(key="idem_pay_conflict", created_by="agent_0001")
        before = self.snapshot()
        with self.assertRaises(PAY.PaymentIdempotencyConflict) as raised:
            self.raise_intent(key="idem_pay_conflict", created_by="agent_0002")
        self.assertEqual(raised.exception.code, PAY.IDEMPOTENCY_CONFLICT)
        self.assert_unchanged(before, "a conflicting replay wrote another payment row")

    def test_the_invoice_paid_amount_equals_the_ledger_cash_for_that_invoice(self) -> None:
        self.settle()
        cash_in = int(
            self.scalar(
                "SELECT coalesce(sum(amount_minor), 0) FROM ledger_entries"
                " WHERE invoice_id = ? AND account = 'CASH_IN' AND direction = 'DEBIT'",
                (SEEDED_INVOICE,),
            )
        )
        self.assertEqual(self.invoice_row()["amount_paid_minor"], cash_in)
        self.assertEqual(cash_in, SEEDED_TOTAL)

    def test_a_settled_payment_never_creates_a_second_payment_row(self) -> None:
        intent, _ = self.settle()
        self.assertEqual(
            self.rows("SELECT * FROM payments WHERE invoice_id = ?", (SEEDED_INVOICE,)),
            [self.payment_row(intent.payment_id)],
        )


# --------------------------------------------------------------------------- #
# 4. The ledger: balanced, chained, append-only
# --------------------------------------------------------------------------- #
class TestLedgerIntegrity(MoneyProbe):
    def test_a_settlement_posts_a_balanced_chain_that_recomputes(self) -> None:
        self.settle()
        rows = self.assert_ledger_evidence()
        self.assertEqual([row["seq"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[0]["prev_hash"], GENESIS_HASH)
        self.assertTrue(PAY.verify_ledger(self.conn).ok)
        self.assertGreater(self.audit_report().count, 0)

    def test_the_ledger_split_matches_the_invoice_buckets(self) -> None:
        self.booking(booking_id="book_0002", offset=10)
        invoice = self.issue(
            booking_id="book_0002",
            key="idem_inv_split",
            lines=[
                self.line(description="7 nights stay", quantity=7, unit_amount_minor=100_000),
                {
                    "kind": "TAX",
                    "description": "PBJT tax",
                    "quantity": 1,
                    "unit_amount_minor": 100_000,
                },
                {
                    "kind": "SERVICE",
                    "description": "service fee",
                    "quantity": 1,
                    "unit_amount_minor": 200_000,
                },
            ],
        )
        self.assertEqual(
            (invoice.subtotal_minor, invoice.tax_minor, invoice.service_minor, invoice.total_minor),
            (700_000, 100_000, 200_000, 1_000_000),
        )

        intent = self.raise_intent(
            invoice_id=invoice.invoice_id, amount_minor=1_000_000, key="idem_pay_split"
        )
        self.settle(
            event_id="evt_split",
            intent=intent,
        )
        self.assert_ledger_evidence()
        credits = self.ledger_credits()
        self.assertEqual(credits.get("TAX_PAYABLE"), 100_000)
        self.assertEqual(credits.get("SERVICE_FEE"), 200_000)
        self.assertEqual(credits.get("OWNER_PAYABLE"), 700_000)
        self.assertEqual(
            int(
                self.scalar(
                    "SELECT sum(amount_minor) FROM ledger_entries"
                    " WHERE account = 'CASH_IN' AND direction = 'DEBIT'"
                )
            ),
            invoice.total_minor,
        )

    def test_two_settlements_extend_one_continuing_chain(self) -> None:
        self.booking(booking_id="book_0002", offset=10)
        first_intent, _ = self.settle(event_id="evt_first")
        first_rows = self.ledger_rows()
        head = first_rows[-1]["entry_hash"]

        # This invoice carries tax too, so its batch is also three rows.
        second = self.issue(booking_id="book_0002", key="idem_inv_second", lines=[
            self.line(description="3 nights stay", quantity=3, unit_amount_minor=100_000),
            {
                "kind": "TAX",
                "description": "PBJT tax",
                "quantity": 1,
                "unit_amount_minor": 30_000,
            },
        ])
        second_intent = self.raise_intent(
            invoice_id=second.invoice_id, amount_minor=330_000, key="idem_pay_second"
        )
        self.settle(event_id="evt_second", intent=second_intent)

        rows = self.assert_ledger_evidence()
        self.assertEqual(len(rows), len(first_rows) + 3)
        self.assertEqual(rows[len(first_rows)]["prev_hash"], head)
        self.assertEqual(rows[len(first_rows)]["seq"], len(first_rows) + 1)
        self.assertNotEqual(first_intent.payment_id, second_intent.payment_id)
        self.assertIn(first_intent.payment_id, {row["payment_id"] for row in rows})
        self.assertIn(second_intent.payment_id, {row["payment_id"] for row in rows})
        self.assertEqual(self.ledger_credits().get("TAX_PAYABLE"), SEEDED_TAX + 30_000)

    def test_the_ledger_refuses_update_and_delete(self) -> None:
        self.settle()
        before = self.ledger_rows()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE ledger_entries SET amount_minor = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM ledger_entries")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM payment_events")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE payment_events SET applied = 1")
        self.assertEqual(self.ledger_rows(), before)
        self.assert_ledger_evidence()

    def test_an_unbalanced_journal_batch_is_refused_and_writes_nothing(self) -> None:
        before = self.snapshot()
        with self.assertRaises(PAY.LedgerUnbalanced) as raised:
            PAY.post_journal(
                self.conn,
                entries=[
                    {
                        "account": "CASH_IN",
                        "direction": "DEBIT",
                        "amount_minor": 100,
                        "currency": CURRENCY,
                        "memo": "debit without a credit",
                    }
                ],
                occurred_at=START,
                ids=self.ids,
            )
        self.assertEqual(raised.exception.code, PAY.LEDGER_UNBALANCED)
        self.assertEqual(self.ledger_rows(), [])
        self.assert_unchanged(before, "an unbalanced batch wrote a ledger row")

    def test_a_float_or_bool_ledger_amount_is_refused_and_writes_nothing(self) -> None:
        for bad in (100.0, True):
            with self.assertRaises(PAY.LedgerEntryInvalid):
                PAY.post_journal(
                    self.conn,
                    entries=[
                        {
                            "account": "CASH_IN",
                            "direction": "DEBIT",
                            "amount_minor": bad,
                            "currency": CURRENCY,
                            "memo": "not an integer amount",
                        },
                        {
                            "account": "OWNER_PAYABLE",
                            "direction": "CREDIT",
                            "amount_minor": 100,
                            "currency": CURRENCY,
                            "memo": "not an integer amount",
                        },
                    ],
                    occurred_at=START,
                    ids=self.ids,
                )
        self.assertEqual(self.ledger_rows(), [])

    def test_a_forged_ledger_row_is_caught_by_the_independent_oracle(self) -> None:
        self.settle()
        self.assert_ledger_evidence()

        self.conn.execute(
            "INSERT INTO ledger_entries (entry_id, seq, account, direction, amount_minor,"
            " currency, booking_id, invoice_id, payment_id, occurred_at, memo, entry_hash,"
            " prev_hash, created_at) VALUES ('entry_forged', 99, 'CASH_IN', 'DEBIT', 5000,"
            " 'IDR', NULL, NULL, NULL, ?, 'a forged row that never happened', ?, ?, ?)",
            (START, "b" * 64, "c" * 64, START),
        )
        with self.assertRaises(AssertionError):
            self.assert_ledger_evidence()
        report = PAY.verify_ledger(self.conn)
        self.assertFalse(report.ok)
        self.assertEqual(report.first_broken_seq, 99)
        self.assertIsNotNone(report.reason)

    def test_the_ledger_head_moves_only_when_a_batch_is_appended(self) -> None:
        self.settle()
        head = self.assert_ledger_evidence()[-1]["entry_hash"]

        # A refused write (nothing to settle twice) must not move the head.
        with self.assertRaises(PAY.LedgerUnbalanced):
            PAY.post_journal(
                self.conn,
                entries=[
                    {
                        "account": "CASH_IN",
                        "direction": "DEBIT",
                        "amount_minor": 7,
                        "currency": CURRENCY,
                        "memo": "unbalanced on purpose",
                    }
                ],
                occurred_at=START,
                ids=self.ids,
            )
        self.assertEqual(self.ledger_rows()[-1]["entry_hash"], head)

        self.booking(booking_id="book_0002", offset=10)
        invoice = self.issue(booking_id="book_0002", key="idem_inv_head", lines=[
            self.line(description="2 nights stay", quantity=2, unit_amount_minor=250_000),
            {
                "kind": "TAX",
                "description": "PBJT tax",
                "quantity": 1,
                "unit_amount_minor": 50_000,
            },
        ])
        intent = self.raise_intent(
            invoice_id=invoice.invoice_id, amount_minor=550_000, key="idem_pay_head"
        )
        self.settle(event_id="evt_head", intent=intent)
        rows = self.assert_ledger_evidence()
        self.assertNotEqual(rows[-1]["entry_hash"], head)
        self.assertEqual(rows[len(rows) - 3]["prev_hash"], head)


# --------------------------------------------------------------------------- #
# 5. Money that cannot be matched: OPEN, +48h, invoice stays unpaid
# --------------------------------------------------------------------------- #
class TestReconciliationFailClosed(MoneyProbe):
    def test_unmatchable_money_opens_an_open_case_with_a_48h_deadline(self) -> None:
        before = self.snapshot()
        event = self.deliver(
            self.payload(provider_intent_id="pi_ghost", amount_minor=SEEDED_TOTAL),
            event_id="evt_ghost",
        )
        self.assertEqual(event.applied, 0)
        self.assertEqual(event.signature_verified, 1)
        self.assertIsNotNone(event.recon_id)

        case = self.one("SELECT * FROM reconciliation_cases WHERE recon_id = ?", (event.recon_id,))
        self.assertEqual(case["kind"], "UNMATCHED_PAYMENT")
        self.assertEqual(case["status"], "OPEN")
        self.assertIsNone(case["resolution_note"])
        self.assertIsNone(case["resolved_at"])
        self.assertIsNotNone(case["owner_actor"])
        self.assertEqual(case["payment_event_id"], self.event_row("evt_ghost")["event_id"])
        self.assertIsNone(case["payment_id"])
        self.assertEqual(
            P.parse_iso(case["due_at"]) - P.parse_iso(case["detected_at"]),
            timedelta(hours=48),
        )

        breach = self.one("SELECT * FROM sla_breaches WHERE kind = 'UNMATCHED_PAYMENT'")
        self.assertEqual(breach["status"], "OPEN")
        self.assertEqual(breach["subject_type"], "payment")

        state = self.snapshot()
        self.assertEqual(state["ledger_entries"], before["ledger_entries"])
        self.assertEqual(state["invoice_paid_minor"], 0)
        invoice = self.invoice_row()
        self.assertEqual(invoice["amount_paid_minor"], 0)
        self.assertEqual(invoice["payment_status"], "UNPAID")
        self.assertEqual(invoice["status"], "ISSUED")

    def test_the_48_hour_deadline_is_exact_for_every_open_case(self) -> None:
        self.raise_intent()
        self.deliver(self.payload(provider_intent_id="pi_ghost_a"), event_id="evt_case_a")
        self.deliver(self.payload(provider_intent_id="pi_ghost_b"), event_id="evt_case_b")
        cases = self.recon_rows()
        self.assertEqual(len(cases), 2)
        for case in cases:
            self.assertEqual(
                P.parse_iso(case["due_at"]) - P.parse_iso(case["detected_at"]),
                timedelta(hours=48),
                f"{case['recon_id']} does not fall due 48 hours after detection",
            )
            self.assertEqual(case["status"], "OPEN")

    def test_an_amount_mismatch_opens_a_case_and_leaves_the_invoice_unpaid(self) -> None:
        intent = self.raise_intent()
        event = self.deliver(
            self.payload(
                provider_intent_id=intent.provider_intent_id, amount_minor=SEEDED_TOTAL - 1
            ),
            event_id="evt_mismatch",
        )
        self.assertEqual(event.applied, 0)
        case = self.one("SELECT * FROM reconciliation_cases WHERE recon_id = ?", (event.recon_id,))
        self.assertEqual(case["kind"], "AMOUNT_MISMATCH")
        self.assertEqual(case["status"], "OPEN")
        self.assertEqual(case["payment_id"], intent.payment_id)
        payment = self.payment_row(intent.payment_id)
        self.assertEqual(payment["status"], "PENDING")
        self.assertIsNone(payment["settled_at"])
        self.assertEqual(self.invoice_row()["amount_paid_minor"], 0)
        self.assertEqual(self.count("ledger_entries"), 0)

    def test_a_success_event_after_a_failure_event_needs_a_human(self) -> None:
        intent = self.raise_intent()
        failed = self.deliver(
            self.payload(provider_intent_id=intent.provider_intent_id),
            event_id="evt_failed",
            event_type="payment.failed",
        )
        self.assertEqual(failed.applied, 1)
        self.assertEqual(self.payment_row(intent.payment_id)["status"], "FAILED")

        event = self.deliver(
            self.payload(provider_intent_id=intent.provider_intent_id), event_id="evt_late_success"
        )
        self.assertEqual(event.applied, 0)
        case = self.one("SELECT * FROM reconciliation_cases WHERE recon_id = ?", (event.recon_id,))
        self.assertEqual(case["kind"], "UNMATCHED_PAYMENT")
        self.assertEqual(self.payment_row(intent.payment_id)["status"], "FAILED")
        self.assertEqual(self.invoice_row()["amount_paid_minor"], 0)
        self.assertEqual(self.invoice_row()["payment_status"], "UNPAID")
        self.assertEqual(self.count("ledger_entries"), 0)

    def test_a_success_event_for_a_voided_invoice_moves_no_money(self) -> None:
        self.booking(booking_id="book_0002", offset=10)
        invoice = self.issue(booking_id="book_0002", key="idem_inv_voided", lines=[
            self.line(description="5 nights stay", quantity=5, unit_amount_minor=100_000)
        ])
        intent = self.raise_intent(
            invoice_id=invoice.invoice_id, amount_minor=500_000, key="idem_pay_voided"
        )
        self.invoices.void(invoice_id=invoice.invoice_id, reason="salah tarif, terbit ulang")

        event = self.deliver(
            self.payload(provider_intent_id=intent.provider_intent_id, amount_minor=500_000),
            event_id="evt_voided",
        )
        self.assertEqual(event.applied, 0)
        case = self.one("SELECT * FROM reconciliation_cases WHERE recon_id = ?", (event.recon_id,))
        self.assertEqual(case["kind"], "UNMATCHED_PAYMENT")
        voided = self.invoice_row(invoice.invoice_id)
        self.assertEqual(voided["status"], "VOID")
        self.assertEqual(voided["amount_paid_minor"], 0)
        self.assertEqual(voided["payment_status"], "UNPAID")
        self.assertEqual(self.payment_row(intent.payment_id)["status"], "PENDING")
        self.assertEqual(self.count("ledger_entries"), 0)

    def test_a_payload_with_card_data_opens_an_unverified_case_and_stores_no_card(self) -> None:
        intent = self.raise_intent()
        raw = self.payload(provider_intent_id=intent.provider_intent_id, card_number=PAN)  # unsafe-scan: allow proving cardholder data is refused
        with self.assertRaises(PAY.CardDataForbidden) as raised:
            self.deliver(raw, event_id="evt_pan")
        self.assertEqual(raised.exception.code, PAY.CARD_DATA_FORBIDDEN)

        row = self.event_row("evt_pan")
        self.assertEqual(row["applied"], 0)
        self.assertEqual(row["signature_verified"], 1)
        case = self.one("SELECT * FROM reconciliation_cases WHERE kind = 'UNVERIFIED_WEBHOOK'")
        self.assertEqual(case["status"], "OPEN")
        self.assertEqual(
            P.parse_iso(case["due_at"]) - P.parse_iso(case["detected_at"]),
            timedelta(hours=48),
        )
        self.assertEqual(self.invoice_row()["amount_paid_minor"], 0)
        self.assertEqual(self.count("ledger_entries"), 0)
        for table, column, value in self.text_values():
            self.assertFalse(contains_pan(value), f"a PAN reached {table}.{column}")
            self.assertNotIn(PAN, value, f"the test PAN reached {table}.{column}")

    def test_a_case_referencing_a_live_invoice_never_marks_it_paid_by_itself(self) -> None:
        event = self.deliver(
            self.payload(provider_intent_id="pi_ghost_declared", invoice_id=SEEDED_INVOICE),
            event_id="evt_declared",
        )
        case = self.one("SELECT * FROM reconciliation_cases WHERE recon_id = ?", (event.recon_id,))
        self.assertEqual(case["invoice_id"], SEEDED_INVOICE)
        self.assertEqual(case["kind"], "UNMATCHED_PAYMENT")
        invoice = self.invoice_row()
        self.assertEqual(invoice["amount_paid_minor"], 0)
        self.assertEqual(invoice["payment_status"], "UNPAID")
        self.assertEqual(self.count("ledger_entries"), 0)
        self.assertEqual(
            [case.recon_id for case in self.payments.unmatched()], [event.recon_id]
        )

    def test_only_a_matched_resolution_posts_unmatched_money(self) -> None:
        event = self.deliver(
            self.payload(provider_intent_id="pi_ghost_matched", invoice_id=SEEDED_INVOICE),
            event_id="evt_matched",
        )
        before = self.snapshot()
        closed = self.payments.resolve_reconciliation(
            recon_id=event.recon_id,
            resolution="UNMATCHED",
            note="bukan pembayaran kami, dicatat sebagai temuan",
            actor="ops_ana",
        )
        self.assertEqual(closed.status, "RESOLVED")
        after_unmatched = self.snapshot()
        self.assertEqual(after_unmatched["ledger_entries"], before["ledger_entries"])
        self.assertEqual(after_unmatched["invoice_paid_minor"], 0)
        self.assertEqual(closed.resolution_note, "bukan pembayaran kami, dicatat sebagai temuan")

        # A second case, this time resolved MATCHED: that is the one path that
        # may move money, and it must move it through the ledger and the invoice.
        second = self.deliver(
            self.payload(provider_intent_id="pi_ghost_matched_2", invoice_id=SEEDED_INVOICE),
            event_id="evt_matched_2",
        )
        closed = self.payments.resolve_reconciliation(
            recon_id=second.recon_id,
            resolution="MATCHED",
            note="dicocokkan dengan mutasi bank",
            actor="ops_ana",
        )
        self.assertEqual(closed.status, "RESOLVED")
        self.assert_ledger_evidence()
        self.assertEqual(self.invoice_row()["amount_paid_minor"], SEEDED_TOTAL)
        self.assertEqual(self.invoice_row()["payment_status"], "PAID")
        breaches = self.rows(
            "SELECT * FROM sla_breaches WHERE subject_type = 'payment' AND subject_id = ?",
            (self.event_row("evt_matched_2")["event_id"],),
        )
        self.assertEqual(len(breaches), 1, "the matched case must own exactly one breach")
        self.assertEqual(breaches[0]["status"], "RESOLVED")
        self.assertIsNotNone(breaches[0]["resolution_note"])
        self.assertEqual(breaches[0]["kind"], PAY.UNMATCHED_PAYMENT_BREACH)

    def test_a_short_note_or_unknown_resolution_is_refused_and_writes_nothing(self) -> None:
        event = self.deliver(
            self.payload(provider_intent_id="pi_ghost_notes"), event_id="evt_notes"
        )
        before = self.snapshot()
        with self.assertRaises(PAY.ReconciliationNoteRequired):
            self.payments.resolve_reconciliation(
                recon_id=event.recon_id, resolution="MATCHED", note="ok", actor="ops_ana"
            )
        with self.assertRaises(PAY.InvalidResolution):
            self.payments.resolve_reconciliation(
                recon_id=event.recon_id,
                resolution="TIDAK_ADA",
                note="sudah dicocokkan dengan bank",
                actor="ops_ana",
            )
        self.assert_unchanged(before, "a refused resolution wrote something")
        case = self.one("SELECT * FROM reconciliation_cases WHERE recon_id = ?", (event.recon_id,))
        self.assertEqual(case["status"], "OPEN")
        self.assertIsNone(case["resolution_note"])


# --------------------------------------------------------------------------- #
# 6. Invoice money semantics
# --------------------------------------------------------------------------- #
class TestInvoiceMoneySemantics(MoneyProbe):
    def test_every_issued_invoice_totals_to_subtotal_plus_tax_plus_service(self) -> None:
        self.booking(booking_id="book_0002", offset=10)
        self.booking(booking_id="book_0003", offset=20)
        self.issue(
            key="idem_inv_mix",
            lines=[
                self.line(description="2 nights stay", quantity=2, unit_amount_minor=400_000),
                {
                    "kind": "SURCHARGE",
                    "description": "extra guest fee",
                    "quantity": 1,
                    "unit_amount_minor": 250_000,
                },
                {
                    "kind": "DISCOUNT",
                    "description": "long stay discount",
                    "quantity": 1,
                    "unit_amount_minor": -50_000,
                },
                {
                    "kind": "TAX",
                    "description": "PBJT tax",
                    "quantity": 1,
                    "unit_amount_minor": 120_000,
                },
                {
                    "kind": "SERVICE",
                    "description": "service fee",
                    "quantity": 1,
                    "unit_amount_minor": 30_000,
                },
            ],
        )
        self.issue(booking_id="book_0002", key="idem_inv_mix2", lines=[
            self.line(description="3 nights stay", quantity=3, unit_amount_minor=100_000)
        ])
        self.issue(booking_id="book_0003", key="idem_inv_mix3", lines=[
            self.line(description="1 night stay", quantity=1, unit_amount_minor=123_456)
        ])

        invoices = self.rows("SELECT * FROM invoices")
        self.assertEqual(len(invoices), 4, "three issued here plus the seeded invoice")
        for row in self.rows(
            "SELECT * FROM invoices"
            " WHERE total_minor <> subtotal_minor + tax_minor + service_minor"
        ):
            self.fail(f"invoice {row['invoice_id']} does not add up: {row}")

        # The seeded fixture writes an invoice row without lines (it is a *stored
        # state*, not an issued one), so the line-vs-bucket re-derivation applies
        # to the invoices this test issued itself.
        with_lines = self.rows(
            "SELECT * FROM invoices i WHERE EXISTS"
            " (SELECT 1 FROM invoice_lines l WHERE l.invoice_id = i.invoice_id)"
        )
        self.assertEqual(len(with_lines), 3)
        for row in with_lines:
            derived = dict(
                self.one(
                    "SELECT coalesce(sum(CASE WHEN kind = 'TAX' THEN amount_minor ELSE 0 END), 0) AS tax,"
                    " coalesce(sum(CASE WHEN kind = 'SERVICE' THEN amount_minor ELSE 0 END), 0) AS service,"
                    " coalesce(sum(CASE WHEN kind IN ('TAX','SERVICE') THEN 0 ELSE amount_minor END), 0)"
                    " AS subtotal FROM invoice_lines WHERE invoice_id = ?",
                    (row["invoice_id"],),
                )
            )
            self.assertEqual(
                (derived["subtotal"], derived["tax"], derived["service"]),
                (row["subtotal_minor"], row["tax_minor"], row["service_minor"]),
                f"the stored lines of {row['invoice_id']} disagree with its stored buckets",
            )
            self.assertGreater(row["total_minor"], 0)

    def test_the_issue_count_in_that_spread_is_the_count_the_database_agrees_with(self) -> None:
        self.booking(booking_id="book_0002", offset=10)
        issued = [
            self.issue(key="idem_inv_count_1"),
            self.issue(booking_id="book_0002", key="idem_inv_count_2", lines=[
                self.line(description="1 night stay", quantity=1, unit_amount_minor=200_000)
            ]),
        ]
        self.assertNotEqual(issued[0].invoice_id, issued[1].invoice_id)
        # Two issued here plus the seeded invoice.
        self.assertEqual(self.count("invoices"), 3)
        self.assertEqual(
            sorted(row["invoice_id"] for row in self.rows("SELECT invoice_id FROM invoices"))[:1],
            [SEEDED_INVOICE],
        )
        for invoice in issued:
            self.assertEqual(self.invoices.verify(invoice.invoice_id).invoice_id, invoice.invoice_id)

    def test_a_discount_line_reduces_the_subtotal_and_never_the_tax_bucket(self) -> None:
        invoice = self.issue(key="idem_inv_discount", lines=[
            self.line(description="2 nights stay", quantity=2, unit_amount_minor=500_000),
            {
                "kind": "DISCOUNT",
                "description": "long stay discount",
                "quantity": 1,
                "unit_amount_minor": -100_000,
            },
            {
                "kind": "TAX",
                "description": "PBJT tax",
                "quantity": 1,
                "unit_amount_minor": 100_000,
            },
        ])
        row = self.invoice_row(invoice.invoice_id)
        self.assertEqual(row["subtotal_minor"], 900_000)
        self.assertEqual(row["tax_minor"], 100_000)
        self.assertEqual(row["payment_status"], "UNPAID")
        discount = self.one(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? AND kind = 'DISCOUNT'",
            (invoice.invoice_id,),
        )
        self.assertLess(discount["amount_minor"], 0)
        self.assertEqual(discount["amount_minor"], -100_000)
        self.assertEqual(
            self.scalar(
                "SELECT sum(amount_minor) FROM invoice_lines"
                " WHERE invoice_id = ? AND kind NOT IN ('TAX','SERVICE')",
                (invoice.invoice_id,),
            ),
            row["subtotal_minor"],
        )
        self.assertEqual(self.count("ledger_entries"), 0)

    def test_the_tax_bucket_is_taken_from_the_policy_version_not_a_constant(self) -> None:
        """Two policy versions, same stay and rate card, different tax_pbjt_pct."""
        season = QC.Season(
            season_id="season_regular",
            name="Regular",
            start_date="2026-01-01",
            end_date="2026-12-31",
            nightly_rate_minor=1_000_000,
            min_stay_nights=1,
            priority=0,
        )
        rate_card = QC.RateCard(
            rate_card_id="card_1",
            property_id="prop_0001",
            unit_scope="property",
            name="Public 2026",
            version=1,
            effective_from="2026-01-01",
            effective_to="2026-12-31",
            currency=CURRENCY,
            weekly_discount_pct="0",
            monthly_discount_pct="0",
            seasons=(season,),
            source_doc_ref="rate/2026-public.pdf",
            approved_by="owner_anas",
        )
        unit = QC.UnitFacts(
            unit_id="unit_0001", property_id="prop_0001", base_capacity=2, max_guests=4
        )
        request = QC.QuoteRequest(
            quote_date="2026-09-21",
            check_in="2026-10-01",
            check_out="2026-10-03",
            adults=2,
            children=0,
            infants=0,
            extras=(),
            discount_minor=0,
            discount_reason=None,
        )
        rules_payload = {
            "extra_guest_fee_minor": 0,
            "cleaning_fee_minor": 0,
            "security_deposit_minor": 0,
            "min_stay_nights": 1,
            "cancellation_ladder": [{"min_days_before": 0, "refund_pct": "100"}],
        }

        issued: dict[str, dict] = {}
        # ``policy_engine`` bounds ``tax_pbjt_pct`` to 0..10 (Indonesian PBJT), so
        # the two independent values are 5 and 10 — the bound itself is respected
        # here rather than fought: the point is that the *stored* percentage drives
        # the money, not a constant inside the invoice service.
        for policy_id, version, pct, key in (
            ("policy_tax_05", 2, "5", "idem_inv_tax_05"),
            ("policy_tax_10", 3, "10", "idem_inv_tax_10"),
        ):
            rules = PE.PolicyRules(
                currency=CURRENCY,
                deposit_pct="30",
                service_fee_pct="0",
                tax_pbjt_pct=pct,
                max_discount_pct="10",
                quote_valid_hours=48,
                hold_ttl_minutes=60,
                max_hold_extensions=1,
                payload=rules_payload,
            )
            policy = PE.PolicyVersion(
                policy_version_id=policy_id,
                property_id="prop_0001",
                unit_scope="property",
                version=version,
                effective_from="2026-01-01",
                effective_to="2026-12-31",
                rules=rules,
                created_at=CREATED,
                source_doc_ref="policy/2026-public.pdf",
                approved_by="owner_anas",
            )
            row = policy.as_row()
            self.conn.execute(
                f"INSERT INTO policy_versions ({', '.join(row)})"
                f" VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
            quote = QC.calculate_quote(
                request, policy, rate_card, unit, quote_id=f"quote_{pct}", issued_at=START
            )
            invoice = self.invoices.issue_from_quote(
                quote=quote, booking_id=SEEDED_BOOKING, idempotency_key=key
            )
            stored = self.invoice_row(invoice.invoice_id)
            stored_policy = self.one(
                "SELECT * FROM policy_versions WHERE policy_version_id = ?", (policy_id,)
            )
            issued[pct] = {
                "invoice": stored,
                "policy_pct": stored_policy["tax_pbjt_pct"],
                "policy_fingerprint": stored_policy["fingerprint"],
                "subtotal": stored["subtotal_minor"],
                "tax": stored["tax_minor"],
                "total": stored["total_minor"],
            }
            self.assertEqual(stored["policy_fingerprint"], stored_policy["fingerprint"])
            self.assertEqual(stored["total_minor"], stored["subtotal_minor"] + stored["tax_minor"])

        low, high = issued["5"], issued["10"]
        self.assertEqual(low["policy_pct"], "5")
        self.assertEqual(high["policy_pct"], "10")
        self.assertNotEqual(
            low["tax"], high["tax"], "the tax bucket did not follow the policy property"
        )
        self.assertNotEqual(low["policy_fingerprint"], high["policy_fingerprint"])
        for bucket in (low, high):
            percent = int(bucket["policy_pct"])
            self.assertEqual(
                bucket["tax"],
                bucket["subtotal"] * percent // 100,
                "the stored tax is not the policy percentage of the stored subtotal",
            )
        self.assertEqual(high["tax"], low["tax"] * 2)
        self.assertEqual((low["tax"], high["tax"]), (100_000, 200_000))
        self.assertEqual(low["subtotal"], high["subtotal"])

    def test_a_float_or_bool_line_amount_is_refused_and_no_invoice_is_written(self) -> None:
        before = self.snapshot()
        for bad in (500_000.5, True):
            with self.assertRaises(P.DomainError) as raised:
                self.issue(
                    key=f"idem_inv_bad_{bad!r}",
                    lines=[self.line(unit_amount_minor=bad)],
                )
            self.assertEqual(raised.exception.code, INV.INVALID_INVOICE_LINE)
        self.assert_unchanged(before, "a non-integer line amount wrote an invoice")

    def test_issuing_with_one_key_twice_creates_exactly_one_invoice(self) -> None:
        first = self.issue(key="idem_inv_once")
        before = self.snapshot()
        second = self.issue(key="idem_inv_once")
        self.assertEqual(first.invoice_id, second.invoice_id)
        self.assertEqual(self.count("invoices"), 2)  # the seeded one + this one
        self.assertEqual(self.count("idempotency_keys"), 1)
        self.assertEqual(self.invoice_row(first.invoice_id)["invoice_number"], first.invoice_number)
        state = self.snapshot()
        self.assertEqual(state["invoice_lines"], before["invoice_lines"])

    def test_reusing_an_issue_key_for_another_request_is_refused(self) -> None:
        self.issue(key="idem_inv_conflict")
        before = self.snapshot()
        with self.assertRaises(P.DomainError) as raised:
            self.issue(
                key="idem_inv_conflict",
                lines=[self.line(description="9 nights stay", quantity=9, unit_amount_minor=100_000)],
            )
        self.assertEqual(raised.exception.code, INV.IDEMPOTENCY_CONFLICT)
        self.assert_unchanged(before, "a conflicting issue key wrote a second invoice")

    def test_an_invoice_for_a_missing_booking_is_refused_and_writes_nothing(self) -> None:
        before = self.snapshot()
        with self.assertRaises(P.EntityNotFound) as raised:
            self.issue(booking_id="book_tidak_ada", key="idem_inv_missing")
        self.assertEqual(raised.exception.code, INV.BOOKING_NOT_FOUND)
        self.assert_unchanged(before, "an invoice was written for a booking that does not exist")

    def test_a_void_invoice_can_never_hold_paid_money(self) -> None:
        self.settle()
        paid = self.invoice_row()
        self.assertEqual(paid["payment_status"], "PAID")
        before = self.snapshot()

        with self.assertRaises(P.DomainError) as raised:
            self.invoices.void(invoice_id=SEEDED_INVOICE, reason="salah tarif, terbit ulang")
        self.assertEqual(raised.exception.code, INV.INVOICE_HAS_PAYMENTS)
        self.assert_unchanged(before, "voiding a paid invoice changed the database")
        self.assertEqual(self.invoice_row()["status"], "ISSUED")

    def test_the_database_itself_refuses_to_void_a_paid_invoice(self) -> None:
        self.settle()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE invoices SET status = 'VOID', voided_at = ?, void_reason = ?"
                " WHERE invoice_id = ?",
                ("2026-09-21T09:00:00+00:00", "forced void", SEEDED_INVOICE),
            )
        row = self.invoice_row()
        self.assertEqual(row["status"], "ISSUED")
        self.assertEqual(row["amount_paid_minor"], SEEDED_TOTAL)

    def test_a_void_invoice_cannot_be_settled_afterwards(self) -> None:
        self.booking(booking_id="book_0002", offset=10)
        invoice = self.issue(booking_id="book_0002", key="idem_inv_void_then_settle", lines=[
            self.line(description="5 nights stay", quantity=5, unit_amount_minor=100_000)
        ])
        self.invoices.void(invoice_id=invoice.invoice_id, reason="salah tarif, terbit ulang")
        before = self.snapshot()
        with self.assertRaises(P.DomainError) as raised:
            self.invoices.record_settlement(
                invoice.invoice_id, amount_paid_minor=500_000, payment_status="PAID"
            )
        self.assertEqual(raised.exception.code, INV.INVOICE_ALREADY_VOID)
        self.assert_unchanged(before, "a void invoice was settled")
        voided = self.invoice_row(invoice.invoice_id)
        self.assertEqual((voided["status"], voided["amount_paid_minor"]), ("VOID", 0))
        self.assertIsNotNone(voided["voided_at"])
        self.assertGreaterEqual(len(voided["void_reason"]), 3)

    def test_a_void_invoice_cannot_raise_a_payment_intent(self) -> None:
        self.booking(booking_id="book_0002", offset=10)
        invoice = self.issue(booking_id="book_0002", key="idem_inv_void_intent", lines=[
            self.line(description="5 nights stay", quantity=5, unit_amount_minor=100_000)
        ])
        self.invoices.void(invoice_id=invoice.invoice_id, reason="salah tarif, terbit ulang")
        before = self.snapshot()
        with self.assertRaises(PAY.InvoiceNotPayable) as raised:
            self.raise_intent(
                invoice_id=invoice.invoice_id, amount_minor=500_000, key="idem_pay_void_intent"
            )
        self.assertEqual(raised.exception.code, PAY.INVOICE_ALREADY_VOID)
        self.assert_unchanged(before, "an intent was raised for a void invoice")

    def test_an_intent_must_belong_to_the_invoice_currency_and_amount(self) -> None:
        before = self.snapshot()
        with self.assertRaises(PAY.PaymentCurrencyMismatch) as raised:
            self.payments.create_intent(
                invoice_id=SEEDED_INVOICE,
                amount_minor=SEEDED_TOTAL,
                currency="USD",
                idempotency_key="idem_pay_usd",
                created_by="agent_0001",
            )
        self.assertEqual(raised.exception.code, PAY.CURRENCY_MISMATCH)
        with self.assertRaises(PAY.PaymentAmountMismatch):
            self.raise_intent(amount_minor=SEEDED_TOTAL - 1, key="idem_pay_short")
        with self.assertRaises(PAY.PaymentAmountMismatch):
            self.raise_intent(amount_minor=SEEDED_TOTAL + 1, key="idem_pay_long")
        self.assert_unchanged(before, "a mismatched intent was written")
        self.assertEqual(self.count("payments"), 0)


# --------------------------------------------------------------------------- #
# 7. No card data can be stored
# --------------------------------------------------------------------------- #
class TestNoCardData(MoneyProbe):
    def assert_no_card_column(self, table: str, columns: list[str]) -> None:
        for column in columns:
            normalised = re.sub(r"[^a-z0-9]", "", str(column).lower())
            if normalised in CARD_COLUMN_ALLOWLIST:
                continue
            self.assertNotIn(
                normalised,
                PAY.CARD_FIELD_HINTS,
                f"{table}.{column} is named like a card field",
            )
            self.assertIsNone(
                _CARD_TOKENS.search(normalised),
                f"{table}.{column} is named like a card field",
            )

    def test_the_money_tables_have_no_column_that_can_hold_card_data(self) -> None:
        for table in MONEY_TABLES:
            columns = [row["name"] for row in self.rows(f"PRAGMA table_info({table})")]
            self.assertTrue(columns, f"{table} has no columns")
            self.assert_no_card_column(table, columns)

    def test_the_module_insert_statements_name_provider_identifiers_only(self) -> None:
        for module in ("payments.py", "invoicing.py"):
            source = (REPO_ROOT / "src" / module).read_text(encoding="utf-8")
            statements = _INSERT.findall(source)
            self.assertTrue(statements, f"no INSERT statement found in {module}")
            for table, columns_text in statements:
                if "join(" in columns_text:
                    # The column list is built from a module-level tuple.
                    names = re.findall(r"[A-Z][A-Z0-9_]*_COLUMNS", columns_text)
                    self.assertTrue(names, f"cannot resolve the columns of {module}:{table}")
                    for name in names:
                        self.assert_no_card_column(table, list(getattr(PAY, name)))
                else:
                    self.assert_no_card_column(
                        table, [part.strip() for part in columns_text.split(",") if part.strip()]
                    )

        self.assertIn("provider_intent_id", PAY.PAYMENT_COLUMNS)
        provider_ish = {
            column for column in PAY.PAYMENT_COLUMNS if "provider" in column
        }
        self.assertEqual(provider_ish, {"provider", "provider_intent_id", "provider_status"})

    def test_the_only_provider_identifiers_stored_are_opaque_ids(self) -> None:
        self.settle()
        payment = self.payment_row(self.scalar("SELECT payment_id FROM payments"))
        self.assertEqual(payment["provider"], PROVIDER)
        self.assertRegex(payment["provider_intent_id"], r"^[A-Za-z0-9][A-Za-z0-9._:@+-]*$")
        self.assertRegex(
            str(self.event_row(self.scalar("SELECT provider_event_id FROM payment_events"))["provider_event_id"]),
            r"^[A-Za-z0-9][A-Za-z0-9._:@+-]*$",
        )
        for table, column, value in self.text_values():
            self.assertFalse(contains_pan(value), f"a PAN-shaped value reached {table}.{column}")

    def test_a_card_number_in_created_by_is_refused(self) -> None:
        before = self.snapshot()
        with self.assertRaises(PAY.CardDataForbidden):
            self.raise_intent(key="idem_pay_pan", created_by=PAN)
        self.assert_unchanged(before, "an intent was written with card data in created_by")

    def test_the_committed_flag_or_hash_columns_never_carry_a_pan(self) -> None:
        """A PAN in the payload must not survive in a hash, a memo or a reason."""
        raw = self.payload(provider_intent_id="pi_ghost_pan", note=f"card {PAN}")
        with self.assertRaises(PAY.CardDataForbidden):
            self.deliver(raw, event_id="evt_pan_hash")
        for table, column, value in self.text_values():
            self.assertNotIn(PAN, value, f"the PAN reached {table}.{column}")
            self.assertFalse(contains_pan(value), f"a PAN-shaped value reached {table}.{column}")

    def test_the_raw_payload_is_never_stored(self) -> None:
        marker = "GUEST-MARKER-9c1f7a"
        raw = self.payload(provider_intent_id="pi_ghost_marker", guest_contact=marker)
        with self.assertRaises(PAY.WebhookSignatureInvalid):
            self.deliver(raw, event_id="evt_marker", signature="0" * 64)

        row = self.event_row("evt_marker")
        self.assertEqual(row["payload_sha256"], P.sha256_hex(EVENT_HASH_DOMAIN, PROVIDER, "evt_marker", raw))
        for table, column, value in self.text_values():
            self.assertNotIn(marker, value, f"the raw payload reached {table}.{column}")
            self.assertNotIn('"guest_contact"', value, f"raw JSON reached {table}.{column}")

    def test_the_redacted_summary_keeps_only_whitelisted_facts(self) -> None:
        self.raise_intent()
        self.deliver(self.payload(provider_intent_id="pi_ghost_summary"), event_id="evt_summary")
        summary = self.event_row("evt_summary")["payload_redacted"]
        allowed = {
            "provider",
            "provider_event_id",
            "event_type",
            "intent",
            "invoice",
            "amount_minor",
            "currency",
            "status",
        }
        for part in summary.split("; "):
            self.assertIn("=", part)
            self.assertIn(part.split("=", 1)[0], allowed, f"unexpected field in {summary!r}")
        self.assertNotIn("{", summary)
        self.assertNotIn('"', summary)


# --------------------------------------------------------------------------- #
# 8. Money is an exact integer everywhere
# --------------------------------------------------------------------------- #
class TestIntegerMoney(MoneyProbe):
    def test_every_money_column_stores_integers_after_the_whole_flow(self) -> None:
        # A full flow: settle one invoice, and open one reconciliation case.
        self.settle()
        self.deliver(self.payload(provider_intent_id="pi_ghost_types"), event_id="evt_types")

        seen_any = False
        for table in MONEY_TABLES:
            columns = self.money_columns(table)
            rows = self.rows(f"SELECT * FROM {table}")
            for column in columns:
                kinds = {
                    row["kind"]
                    for row in self.rows(f"SELECT DISTINCT typeof({column}) AS kind FROM {table}")
                }
                self.assertTrue(
                    kinds <= {"integer", "null"},
                    f"{table}.{column} holds {kinds}, not integer money",
                )
                if rows and "integer" in kinds:
                    seen_any = True
        self.assertTrue(seen_any, "no money column had a value to check")

    def test_the_money_returned_by_the_services_is_python_int(self) -> None:
        intent, _ = self.settle()
        self.assertIs(type(intent.amount_minor), int)
        self.assertNotIsInstance(intent.amount_minor, bool)
        invoice = self.invoices.get(SEEDED_INVOICE)
        for value in (
            invoice.subtotal_minor,
            invoice.tax_minor,
            invoice.service_minor,
            invoice.total_minor,
            invoice.amount_paid_minor,
        ):
            self.assertIs(type(value), int)
            self.assertNotIsInstance(value, bool)
        for row in self.ledger_rows():
            self.assertIs(type(row["amount_minor"]), int)
            self.assertNotIsInstance(row["amount_minor"], bool)
        self.assertEqual(
            invoice.amount_paid_minor,
            self.scalar(
                "SELECT amount_paid_minor FROM invoices WHERE invoice_id = ?", (SEEDED_INVOICE,)
            ),
        )

    def test_a_float_or_bool_intent_amount_is_refused_and_writes_nothing(self) -> None:
        before = self.snapshot()
        for bad in (SEEDED_TOTAL + 0.5, True):
            with self.assertRaises(P.DomainError) as raised:
                self.raise_intent(amount_minor=bad, key=f"idem_pay_bad_{bad!r}")
            self.assertEqual(raised.exception.code, PAY.INVALID_INTENT_REQUEST)
        self.assert_unchanged(before, "a non-integer amount wrote a payment row")

    def test_settlement_amounts_stay_integral_across_a_partial_flow(self) -> None:
        self.invoices.record_settlement(
            SEEDED_INVOICE, amount_paid_minor=250_000, payment_status="PARTIAL"
        )
        row = self.invoice_row()
        self.assertEqual(row["amount_paid_minor"], 250_000)
        self.assertIs(type(row["amount_paid_minor"]), int)
        self.assertEqual(row["payment_status"], "PARTIAL")
        self.assertEqual(self.count("ledger_entries"), 0)
        self.assert_ledger_evidence()


# --------------------------------------------------------------------------- #
# Regression cover for the three defects this probe first reported red. The
# assertions are the correct contract, not a weakened one: no skip, no
# expectedFailure, and both failures were re-reproduced against a throwaway copy
# of the modules carrying the old logic again.
# --------------------------------------------------------------------------- #
class TestPreviouslyReproducedDefectsStayFixed(MoneyProbe):
    """Regression cover for three defects this probe originally reported red.

    The defect story stays in each docstring: a fixed bug without its original
    evidence is a bug that comes back unnoticed.
    """
    def test_the_booking_mirror_follows_the_settlement_it_just_recorded(self) -> None:
        """``invoicing.record_settlement`` mirrors a *stale* invoice onto ``bookings``.

        It reads ``current`` before the ``UPDATE`` and then calls
        ``_sync_booking(current)``, so the booking is written with the previous
        ``amount_paid_minor``/``payment_status``. A booking whose only invoice was
        paid in full stays ``UNPAID`` with ``amount_paid_minor = 0``.
        """
        self.settle()
        booking = self.one("SELECT * FROM bookings WHERE booking_id = ?", (SEEDED_BOOKING,))
        invoice = self.invoice_row()
        self.assertEqual(invoice["payment_status"], "PAID")
        self.assertEqual(invoice["amount_paid_minor"], SEEDED_TOTAL)
        self.assertEqual(
            booking["amount_paid_minor"],
            min(invoice["amount_paid_minor"], booking["total_minor"]),
            f"DEFECT: bookings.amount_paid_minor = {booking['amount_paid_minor']} after a full"
            f" settlement of {SEEDED_TOTAL}",
        )
        self.assertEqual(
            booking["payment_status"],
            INV.BOOKING_PAYMENT_STATUS["PAID"],
            f"DEFECT: bookings.payment_status = {booking['payment_status']!r} after a full"
            " settlement",
        )

    def test_partial_settlements_credit_each_share_exactly_once(self) -> None:
        """``payments._post_settlement`` recomputes the tax share per settlement.

        ``tax = min(invoice.tax_minor, amount)`` is taken from the *whole* invoice
        tax on every settlement, so two partial settlements credit ``TAX_PAYABLE``
        twice while ``OWNER_PAYABLE`` is short by the same amount. The batch still
        balances, so a balance-only check cannot see it.
        """
        # 400_000 in as unmatched money, matched by a human onto the invoice...
        event = self.deliver(
            self.payload(
                provider_intent_id="pi_ghost_partial",
                amount_minor=400_000,
                invoice_id=SEEDED_INVOICE,
            ),
            event_id="evt_partial_1",
        )
        self.payments.resolve_reconciliation(
            recon_id=event.recon_id,
            resolution="MATCHED",
            note="dicocokkan dengan mutasi bank",
            actor="ops_ana",
        )
        self.assertEqual(self.invoice_row()["payment_status"], "PARTIAL")
        self.assertEqual(self.invoice_row()["amount_paid_minor"], 400_000)

        # ... and the remaining 600_000 through a hosted intent, as the API intends.
        intent = self.raise_intent(amount_minor=600_000, key="idem_pay_partial_2")
        self.settle(event_id="evt_partial_2", intent=intent)

        invoice = self.invoice_row()
        self.assertEqual(invoice["amount_paid_minor"], SEEDED_TOTAL)
        self.assertEqual(invoice["payment_status"], "PAID")
        self.assert_ledger_evidence()

        credits = self.ledger_credits()
        self.assertEqual(
            credits.get("TAX_PAYABLE"),
            invoice["tax_minor"],
            f"DEFECT: TAX_PAYABLE credited {credits.get('TAX_PAYABLE')} for an invoice whose tax"
            f" is {invoice['tax_minor']}",
        )
        self.assertEqual(
            credits.get("OWNER_PAYABLE"),
            invoice["total_minor"] - invoice["tax_minor"] - invoice["service_minor"],
            f"DEFECT: OWNER_PAYABLE credited {credits.get('OWNER_PAYABLE')} instead of the"
            " invoice's own owner share",
        )

    def test_invoicing_writes_an_invoice_without_any_import_side_effect(self) -> None:
        """``invoicing`` must stand on its own, with no import-time patching.

        Original defect: ``Invoice.as_dict`` called ``Money.as_dict``, a name that
        only ``payments`` created by patching ``primitives.Money`` at import time.
        ``invoicing`` on its own therefore raised ``AttributeError`` on every
        issue instead of writing the invoice (or refusing with a code).

        A successful write is the *correct* outcome here, so this test asserts it
        rather than demanding a failure; what it still forbids is the raw
        ``AttributeError`` and any monkey-patch of a core class.
        """

        # Whatever the serialiser is called, the two names must not diverge, and the
        # contract that matters is behavioural: an invoice write must not depend on
        # another module having been imported first.
        reference = P.Money(1_500, "IDR").to_dict()
        as_dict = getattr(P.Money, "as_dict", None)
        if as_dict is not None:
            self.assertEqual(
                P.Money(1_500, "IDR").as_dict(), reference,
                "Money.as_dict must project exactly what Money.to_dict projects",
            )
        try:
            self.issue(key="idem_inv_no_alias")
        except P.DomainError:
            pass  # a coded refusal is an acceptable contract
        except Exception as exc:  # noqa: BLE001 - the point of the test
            self.fail(
                f"DEFECT: issuing raised {type(exc).__name__}: {exc}"
            )
        self.assertEqual(self.count("invoices"), 2, "the seeded invoice plus the new one")
        row = self.one(
            "SELECT payment_status, amount_paid_minor FROM invoices"
            " WHERE invoice_id <> ?", (SEEDED_INVOICE,),
        )
        self.assertEqual(row["amount_paid_minor"], 0)
        self.assertEqual(row["payment_status"], "UNPAID")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
