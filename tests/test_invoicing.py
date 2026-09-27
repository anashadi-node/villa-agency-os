"""Independent verification of ``src/invoicing.py`` (the invoice service).

Authority for storage is ``src/schema/001_initial.sql`` (``invoices``,
``invoice_lines``, ``payments``, ``idempotency_keys``, ``audit_events``); nothing
here mocks storage. Every test drives the real ``InvoiceService`` against an
in-memory SQLite database built by ``schema_loader.connect_sqlite``, with time
from ``primitives.FrozenClock`` and identifiers from a deterministic
``IdFactory``. No test calls ``datetime.now()`` and no test edits ``src/``.

The ten properties this file pins, and where they live:

1. ``total_minor == subtotal_minor + tax_minor + service_minor`` and every
   money column is an integer in minor units -> ``TestIssueTotals``.
2. ``issue()`` refuses bad requests (unknown booking, short key, bad line, bad
   currency, non-positive total) *and writes nothing* -> ``TestIssueRejections``.
3. idempotency: same key + same payload replays, same key + different payload
   conflicts, and neither path adds a row -> ``TestIdempotency``.
4. invoice numbers are unique, ordered per month and consistently formatted ->
   ``TestInvoiceNumbering``.
5. ``void()`` needs a reason, refuses settled and already-void invoices ->
   ``TestVoid``.
6. ``record_settlement()`` derives UNPAID/PARTIAL/PAID/OVERPAID, refuses
   inconsistent settlements, ``outstanding()`` is ``total - paid`` ->
   ``TestRecordSettlement``.
7. ``is_overdue()`` is strictly after ``due_at`` and deaf to settled invoices ->
   ``TestOverdue``.
8. ``verify()`` re-reconciles stored lines against stored totals ->
   ``TestVerify``.
9. every accepted write leaves exactly one audit row with a documented action ->
   ``TestAuditTrail``.
10. no money column anywhere stores a float -> ``TestMoneyIsNeverFloat``.

Three behaviours of the module contradict the brief or its own documentation. They
are pinned as ``unittest.expectedFailure`` in ``TestKnownDefects`` (so the suite
stays green and the defect is still visible in the report) rather than silently
weakened:

* ``invoice_lines.quantity = 0`` is accepted when another line carries the total
  (``LineSpec`` only rejects ``quantity < 0`` and the DDL only checks
  ``quantity >= 0``).
* ``record_settlement()`` mirrors a *stale* invoice snapshot onto ``bookings``
  (``_sync_booking(current)`` is called with the pre-update row), so the booking
  mirror always lags one settlement behind.
* ``invoicing`` alone cannot write at all: ``Invoice.as_dict()`` calls
  ``Money.as_dict()``, which ``primitives.Money`` does not define (it is
  ``to_dict()``). ``payments.py`` papers over this with a module-level alias
  (``_ensure_money_serialiser_alias``), so the alias below mirrors that fix
  instead of editing ``src/``.
* a non-string ``currency`` escapes as a bare ``AttributeError`` instead of a
  coded ``INVALID_INVOICE_ISSUE``.
"""

from __future__ import annotations

import re
import unittest
from datetime import timedelta

import audit as A
import invoicing as INV
import primitives as P
from schema_loader import connect_sqlite

# --------------------------------------------------------------------------- #
# Compatibility alias the module needs to write at all.
#
# ``src/payments.py`` defines the same alias and documents why
# (``_ensure_money_serialiser_alias``, payments.py): ``Invoice.as_dict()`` calls

START = "2026-09-21T08:00:00Z"
NOW_ISO = P.to_iso(P.parse_iso(START))
FINGERPRINT = "a" * 64
CURRENCY = "IDR"
NUMERIC_FIELDS = ("subtotal_minor", "tax_minor", "service_minor", "total_minor", "amount_paid_minor")
LINE_NUMERIC_FIELDS = ("quantity", "unit_amount_minor", "amount_minor")

# The booking each test invoices. Seeded once, exactly as the kernel stores it.
BOOKING_WINDOWS = {
    "book_1": ("2026-10-01", "2026-10-03"),
    "book_2": ("2026-11-01", "2026-11-03"),
}


def seed_booking(conn, booking_id: str, *, total_minor: int = 1_500_000) -> str:
    """Insert the parent chain a ``bookings`` row needs (property -> unit -> policy)."""
    check_in, check_out = BOOKING_WINDOWS[booking_id]
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
    conn.execute(
        "INSERT OR IGNORE INTO bookings (booking_id, property_id, unit_id, check_in, check_out,"
        " nights, status, guest_ref, adults, children, infants, currency, policy_version_id,"
        " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
        " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
        " updated_at, confirmed_at, cancelled_at)"
        " VALUES (?, 'prop_1', 'unit_1', ?, ?, 2, 'CONFIRMED', ?, 2, 0, 0, 'IDR', 'policy_v1', ?,"
        " 'quote_1', 'hold_1', ?, 0, 0, 'UNPAID', ?, 1, ?, ?, ?, NULL)",
        (
            booking_id,
            check_in,
            check_out,
            "guest_" + booking_id,
            "0" * 64,
            total_minor,
            "idem_" + booking_id,
            NOW_ISO,
            NOW_ISO,
            NOW_ISO,
        ),
    )
    return booking_id


# --------------------------------------------------------------------------- #
# Independent oracle for the money buckets. Computed here from the inputs the
# test itself wrote, never read back from the module under test.
# --------------------------------------------------------------------------- #
def line_amounts(lines) -> list:
    """``amount_minor`` = ``quantity * unit_amount_minor`` for every line."""
    return [line["quantity"] * line["unit_amount_minor"] for line in lines]


def expected_buckets(lines) -> tuple:
    """(subtotal, tax, service, total) implied by the requested lines."""
    amounts = line_amounts(lines)
    tax = sum(a for line, a in zip(lines, amounts) if line["kind"] == "TAX")
    service = sum(a for line, a in zip(lines, amounts) if line["kind"] == "SERVICE")
    subtotal = sum(a for line, a in zip(lines, amounts) if line["kind"] not in ("TAX", "SERVICE"))
    return subtotal, tax, service, subtotal + tax + service


class InvoiceTestBase(unittest.TestCase):
    """Shared rig: in-memory SQLite + frozen clock + deterministic ids.

    ``issue()`` gets a fresh idempotency key per call (``self.key()``) so a test
    that wants a replay has to reuse a key on purpose.
    """

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.clock = P.FrozenClock(P.parse_iso(START))
        self.ids = P.IdFactory(deterministic=True, seed="invoicing")
        self.svc = INV.InvoiceService(self.conn, clock=self.clock, ids=self.ids)
        for booking_id in BOOKING_WINDOWS:
            seed_booking(self.conn, booking_id)
        self._keys = 0

    # -- rig --------------------------------------------------------------- #
    def key(self) -> str:
        self._keys += 1
        return "idem_key_%04d" % self._keys

    def line(self, **overrides) -> dict:
        payload = {
            "kind": "NIGHTLY",
            "description": "Villa night",
            "quantity": 2,
            "unit_amount_minor": 750_000,
        }
        payload.update(overrides)
        return payload

    def issue(self, **overrides) -> INV.Invoice:
        request = {
            "booking_id": "book_1",
            "currency": CURRENCY,
            "policy_fingerprint": FINGERPRINT,
            "lines": [self.line()],
            "idempotency_key": self.key(),
        }
        request.update(overrides)
        return self.svc.issue(**request)

    def insert_foreign_invoice(self, number: str, *, booking_id: str = "book_1") -> str:
        """Write an invoice row behind the service's back (a number-squat fixture).

        The CHECK constraints still apply, so the row has to be internally
        consistent: total = subtotal + tax + service, and due_at > issued_at.
        """
        invoice_id = "inv_foreign_" + number.replace("-", "_")
        self.conn.execute(
            "INSERT INTO invoices (invoice_id, invoice_number, booking_id, policy_fingerprint,"
            " currency, subtotal_minor, tax_minor, service_minor, total_minor, amount_paid_minor,"
            " status, payment_status, issued_at, due_at, voided_at, void_reason, created_at)"
            " VALUES (?, ?, ?, ?, 'IDR', 100, 0, 0, 100, 0, 'ISSUED', 'UNPAID', ?, ?, NULL, NULL, ?)",
            (invoice_id, number, booking_id, FINGERPRINT, NOW_ISO, "2026-09-28T08:00:00+00:00", NOW_ISO),
        )
        return invoice_id

    # -- observation ------------------------------------------------------- #
    def count(self, table: str) -> int:
        return int(self.conn.execute("SELECT count(*) AS c FROM %s" % table).fetchone()["c"])

    def state(self) -> dict:
        """Row counts plus money sums — the witness for 'nothing was written'."""
        row = self.conn.execute(
            "SELECT coalesce(sum(total_minor), 0) AS t, coalesce(sum(amount_paid_minor), 0) AS p"
            " FROM invoices"
        ).fetchone()
        booking = self.conn.execute(
            "SELECT coalesce(sum(amount_paid_minor), 0) AS p, coalesce(sum(total_minor), 0) AS t"
            " FROM bookings"
        ).fetchone()
        return {
            "invoices": self.count("invoices"),
            "lines": self.count("invoice_lines"),
            "audit": self.count("audit_events"),
            "keys": self.count("idempotency_keys"),
            "payments": self.count("payments"),
            "bookings": self.count("bookings"),
            "invoice_total_minor": int(row["t"]),
            "invoice_paid_minor": int(row["p"]),
            "booking_paid_minor": int(booking["p"]),
            "booking_total_minor": int(booking["t"]),
        }

    def row(self, table: str, **where) -> dict:
        clause = " AND ".join("%s = ?" % column for column in where) if where else "1"
        found = self.conn.execute(
            "SELECT * FROM %s WHERE %s" % (table, clause), tuple(where.values())
        ).fetchone()
        self.assertIsNotNone(found, "no %s row matching %r" % (table, where))
        return dict(found)

    def audit_actions(self) -> list:
        return [
            row["action"]
            for row in self.conn.execute("SELECT action FROM audit_events ORDER BY seq").fetchall()
        ]

    def audit_count(self, action: str) -> int:
        return int(
            self.conn.execute(
                "SELECT count(*) AS c FROM audit_events WHERE action = ?", (action,)
            ).fetchone()["c"]
        )

    def money_typeof(self, table: str, columns) -> dict:
        selection = ", ".join("typeof(%s) AS %s" % (c, c) for c in columns)
        return dict(self.conn.execute("SELECT %s FROM %s LIMIT 1" % (selection, table)).fetchone())

    # -- assertions -------------------------------------------------------- #
    def assert_refused(self, code, call, *, message: str = "") -> None:
        """The call must fail with ``code`` **and** leave the database untouched.

        Every rejection test in this file goes through here: an exception type on
        its own proves nothing about what a half-finished write left behind.
        """
        before = self.state()
        with self.assertRaises(P.DomainError) as raised:
            call()
        if isinstance(code, (tuple, list, set, frozenset)):
            self.assertIn(raised.exception.code, code, message or str(raised.exception))
        else:
            self.assertEqual(raised.exception.code, code, message or str(raised.exception))
        self.assertEqual(self.state(), before, "a refused call wrote rows")

    def assert_unchanged(self, before: dict, message: str = "") -> None:
        self.assertEqual(self.state(), before, message or "state changed unexpectedly")

    # -- settled/oracle helpers -------------------------------------------- #
    def assert_totals_reconcile(self, invoice: INV.Invoice, lines) -> None:
        subtotal, tax, service, total = expected_buckets(lines)
        self.assertEqual(invoice.subtotal_minor, subtotal)
        self.assertEqual(invoice.tax_minor, tax)
        self.assertEqual(invoice.service_minor, service)
        self.assertEqual(invoice.total_minor, total)
        self.assertEqual(invoice.total_minor, invoice.subtotal_minor + invoice.tax_minor + invoice.service_minor)

    def line_states(self, invoice_id: str) -> list:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_id", (invoice_id,)
            ).fetchall()
        ]


class TestIssueTotals(InvoiceTestBase):
    """(1) the four money buckets, and that every one of them is integer minor."""

    def test_single_line_invoice_total_is_subtotal_plus_tax_plus_service(self) -> None:
        lines = [self.line(quantity=3, unit_amount_minor=500_000)]
        invoice = self.issue(lines=lines)
        self.assertEqual(
            invoice.total_minor, invoice.subtotal_minor + invoice.tax_minor + invoice.service_minor
        )
        self.assertEqual(invoice.total_minor, 1_500_000)

    def test_mixed_kind_invoice_total_is_subtotal_plus_tax_plus_service(self) -> None:
        lines = [
            self.line(quantity=2, unit_amount_minor=750_000),
            self.line(kind="DISCOUNT", quantity=1, unit_amount_minor=-100_000),
            self.line(kind="TAX", quantity=1, unit_amount_minor=75_000),
            self.line(kind="SERVICE", quantity=1, unit_amount_minor=150_000),
        ]
        invoice = self.issue(lines=lines)
        self.assertEqual(
            invoice.total_minor, invoice.subtotal_minor + invoice.tax_minor + invoice.service_minor
        )

    def test_tax_lines_land_in_tax_and_service_lines_in_service(self) -> None:
        """Buckets follow the line kind, not the order the caller wrote them."""
        lines = [
            self.line(kind="SERVICE", quantity=2, unit_amount_minor=150_000),
            self.line(kind="TAX", quantity=1, unit_amount_minor=225_000),
            self.line(quantity=2, unit_amount_minor=1_000_000),
        ]
        invoice = self.issue(lines=lines)
        self.assert_totals_reconcile(invoice, lines)
        self.assertEqual(invoice.tax_minor, 225_000)
        self.assertEqual(invoice.service_minor, 300_000)

    def test_discount_line_reduces_subtotal_by_its_full_amount(self) -> None:
        plain = self.issue(lines=[self.line(quantity=2, unit_amount_minor=750_000)])
        discounted = self.issue(
            lines=[
                self.line(quantity=2, unit_amount_minor=750_000),
                self.line(kind="DISCOUNT", quantity=1, unit_amount_minor=-250_000),
            ]
        )
        self.assertEqual(discounted.total_minor, plain.total_minor - 250_000)
        self.assertEqual(discounted.subtotal_minor, plain.subtotal_minor - 250_000)
        self.assertEqual(discounted.tax_minor, plain.tax_minor)

    def test_discount_does_not_enter_the_tax_or_service_bucket(self) -> None:
        lines = [
            self.line(kind="DISCOUNT", quantity=2, unit_amount_minor=-100_000),
            self.line(quantity=3, unit_amount_minor=500_000),
        ]
        invoice = self.issue(lines=lines)
        self.assertEqual(invoice.tax_minor, 0)
        self.assertEqual(invoice.service_minor, 0)
        self.assertEqual(invoice.subtotal_minor, 1_500_000 - 200_000)

    def test_every_line_amount_is_quantity_times_unit_amount(self) -> None:
        lines = [
            self.line(quantity=2, unit_amount_minor=750_000),
            self.line(kind="CLEANING", quantity=3, unit_amount_minor=125_000),
        ]
        invoice = self.issue(lines=lines)
        stored = self.line_states(invoice.invoice_id)
        for row, expected in zip(stored, line_amounts(lines)):
            self.assertEqual(row["amount_minor"], expected)
            self.assertEqual(row["amount_minor"], row["quantity"] * row["unit_amount_minor"])

    def test_invoice_money_columns_are_python_ints(self) -> None:
        invoice = self.issue()
        values = invoice.as_row()
        for field in NUMERIC_FIELDS:
            value = values[field]
            self.assertIsInstance(value, int, "%s is not an int" % field)
            self.assertNotIsInstance(value, bool)
            self.assertNotIsInstance(value, float)

    def test_money_properties_are_integer_minor_money(self) -> None:
        invoice = self.issue()
        for money in (invoice.subtotal, invoice.tax, invoice.service, invoice.total, invoice.amount_paid):
            self.assertIsInstance(money, P.Money)
            self.assertIsInstance(money.amount_minor, int)
            self.assertEqual(money.currency, CURRENCY)
        self.assertEqual(invoice.total.amount_minor, invoice.total_minor)

    def test_totals_reconcile_for_a_spread_of_request_shapes(self) -> None:
        shapes = [
            [self.line(quantity=1, unit_amount_minor=1)],
            [self.line(quantity=7, unit_amount_minor=99_999)],
            [self.line(kind="OTHER", quantity=5, unit_amount_minor=12_345)],
            [
                self.line(kind="NIGHTLY", quantity=4, unit_amount_minor=1_000_000),
                self.line(kind="SURCHARGE", quantity=1, unit_amount_minor=250_000),
                self.line(kind="TAX", quantity=1, unit_amount_minor=525_000),
                self.line(kind="SERVICE", quantity=1, unit_amount_minor=1_050_000),
                self.line(kind="DISCOUNT", quantity=1, unit_amount_minor=-500_000),
            ],
        ]
        for lines in shapes:
            with self.subTest(lines=lines):
                invoice = self.issue(lines=lines)
                self.assert_totals_reconcile(invoice, lines)
                self.assertGreater(invoice.total_minor, 0)


class TestIssueRejections(InvoiceTestBase):
    """(2) every bad request is refused with a stable code and zero writes."""

    def test_issue_refuses_an_unknown_booking(self) -> None:
        self.assert_refused(INV.BOOKING_NOT_FOUND, lambda: self.issue(booking_id="book_404"))

    def test_issue_refuses_a_blank_booking_reference(self) -> None:
        self.assert_refused(("MISSING_FIELD", "INVALID_FIELD"), lambda: self.issue(booking_id="   "))

    def test_issue_refuses_an_idempotency_key_shorter_than_the_minimum(self) -> None:
        self.assert_refused(
            INV.INVALID_IDEMPOTENCY_KEY, lambda: self.issue(idempotency_key="1234567")
        )

    def test_issue_accepts_an_idempotency_key_of_exactly_the_minimum_length(self) -> None:
        invoice = self.issue(idempotency_key="i" * INV.MIN_IDEMPOTENCY_KEY)
        self.assertEqual(self.count("invoices"), 1)
        self.assertTrue(invoice.invoice_id)

    def test_issue_refuses_a_non_string_idempotency_key(self) -> None:
        self.assert_refused(("INVALID_FIELD", "MISSING_FIELD"), lambda: self.issue(idempotency_key=12345678))

    def test_issue_refuses_an_idempotency_key_containing_whitespace(self) -> None:
        self.assert_refused(("INVALID_FIELD", "MISSING_FIELD"), lambda: self.issue(idempotency_key="idem key 0001"))

    def test_issue_refuses_a_request_without_lines(self) -> None:
        self.assert_refused(INV.INVALID_ISSUE_REQUEST, lambda: self.issue(lines=[]))

    def test_issue_refuses_a_line_without_a_description(self) -> None:
        line = {"kind": "NIGHTLY", "quantity": 1, "unit_amount_minor": 500_000}
        self.assert_refused(("MISSING_FIELD", "INVALID_INVOICE_LINE"), lambda: self.issue(lines=[line]))

    def test_issue_refuses_a_line_with_a_blank_description(self) -> None:
        self.assert_refused(
            ("MISSING_FIELD", "INVALID_INVOICE_LINE"),
            lambda: self.issue(lines=[self.line(description="   ")]),
        )

    def test_issue_refuses_a_line_description_below_the_minimum_length(self) -> None:
        self.assert_refused(
            ("INVALID_INVOICE_LINE", "MISSING_FIELD"),
            lambda: self.issue(lines=[self.line(description="ab")]),
        )

    def test_issue_refuses_a_line_with_negative_quantity(self) -> None:
        self.assert_refused(
            INV.INVALID_INVOICE_LINE, lambda: self.issue(lines=[self.line(quantity=-1), self.line()])
        )

    def test_issue_refuses_an_invoice_whose_total_is_not_positive(self) -> None:
        self.assert_refused(
            ("INVALID_ISSUE_REQUEST", "INVALID_INVOICE_ISSUE"),
            lambda: self.issue(lines=[self.line(quantity=1, unit_amount_minor=0)]),
        )

    def test_issue_refuses_an_invoice_made_only_of_negative_lines(self) -> None:
        """A discount with nothing to discount leaves a negative total."""
        self.assert_refused(
            ("INVOICE_LINES_MISMATCH", "INVALID_ISSUE_REQUEST"),
            lambda: self.issue(lines=[self.line(kind="DISCOUNT", quantity=1, unit_amount_minor=-50_000)]),
        )

    def test_issue_refuses_a_discount_line_with_a_positive_amount(self) -> None:
        self.assert_refused(
            INV.INVALID_INVOICE_LINE,
            lambda: self.issue(lines=[self.line(kind="DISCOUNT", quantity=1, unit_amount_minor=50_000)]),
        )

    def test_issue_refuses_a_line_with_an_unknown_kind(self) -> None:
        self.assert_refused(
            INV.INVALID_INVOICE_LINE, lambda: self.issue(lines=[self.line(kind="PIZZA")])
        )

    def test_issue_refuses_a_line_with_an_unknown_field(self) -> None:
        self.assert_refused(
            INV.INVALID_INVOICE_LINE, lambda: self.issue(lines=[self.line(colour="red")])
        )

    def test_issue_refuses_a_float_unit_amount(self) -> None:
        self.assert_refused(
            INV.INVALID_INVOICE_LINE, lambda: self.issue(lines=[self.line(unit_amount_minor=1.5)])
        )

    def test_issue_refuses_a_boolean_unit_amount(self) -> None:
        self.assert_refused(
            INV.INVALID_INVOICE_LINE, lambda: self.issue(lines=[self.line(unit_amount_minor=True)])
        )

    def test_issue_refuses_a_currency_that_is_not_three_letters(self) -> None:
        for currency in ("IDRX", "ID", "RUPIAH"):
            with self.subTest(currency=currency):
                self.assert_refused(
                    INV.INVALID_ISSUE_REQUEST, lambda currency=currency: self.issue(currency=currency)
                )

    def test_issue_refuses_an_unknown_three_letter_currency(self) -> None:
        self.assert_refused(INV.INVALID_ISSUE_REQUEST, lambda: self.issue(currency="ZZZ"))

    def test_issue_refuses_an_empty_currency(self) -> None:
        self.assert_refused(INV.INVALID_ISSUE_REQUEST, lambda: self.issue(currency=""))

    def test_issue_refuses_a_policy_fingerprint_that_is_not_lowercase_hex(self) -> None:
        self.assert_refused(
            INV.INVALID_ISSUE_REQUEST, lambda: self.issue(policy_fingerprint="A" * 64)
        )

    def test_issue_refuses_a_policy_fingerprint_of_the_wrong_length(self) -> None:
        self.assert_refused(INV.INVALID_ISSUE_REQUEST, lambda: self.issue(policy_fingerprint="ab"))

    def test_issue_refuses_a_non_positive_due_days(self) -> None:
        for due_days in (0, -1):
            with self.subTest(due_days=due_days):
                self.assert_refused(
                    INV.INVALID_ISSUE_REQUEST, lambda due_days=due_days: self.issue(due_days=due_days)
                )

    def test_issue_refuses_a_pinned_invoice_number_below_the_minimum_length(self) -> None:
        self.assert_refused(
            INV.INVALID_ISSUE_REQUEST, lambda: self.issue(invoice_number="INV-1")
        )

    def test_a_refused_issue_leaves_no_audit_row(self) -> None:
        self.assert_refused(INV.BOOKING_NOT_FOUND, lambda: self.issue(booking_id="book_404"))
        self.assertEqual(self.audit_actions(), [])

    def test_a_refused_issue_leaves_no_idempotency_row(self) -> None:
        self.assert_refused(INV.INVALID_IDEMPOTENCY_KEY, lambda: self.issue(idempotency_key="123"))
        self.assertEqual(self.count("idempotency_keys"), 0)

    def test_a_failed_issue_does_not_consume_the_month_sequence(self) -> None:
        self.assert_refused(INV.BOOKING_NOT_FOUND, lambda: self.issue(booking_id="book_404"))
        invoice = self.issue()
        self.assertTrue(invoice.invoice_number.endswith("-00001"), invoice.invoice_number)


class TestIdempotency(InvoiceTestBase):
    """(3) one key + one payload = one invoice; the same key with another payload is a conflict."""

    def replay(self, key: str, **overrides):
        request = {
            "booking_id": "book_1",
            "currency": CURRENCY,
            "policy_fingerprint": FINGERPRINT,
            "lines": [self.line()],
            "idempotency_key": key,
        }
        request.update(overrides)
        return self.svc.issue(**request)

    def test_replaying_the_same_request_returns_the_same_invoice(self) -> None:
        key = "idem_replay_0000"
        first = self.replay(key)
        second = self.replay(key)
        self.assertEqual(second.invoice_id, first.invoice_id)
        self.assertEqual(second.total_minor, first.total_minor)
        self.assertEqual(second.booking_id, first.booking_id)
        self.assertEqual(second.issued_at, first.issued_at)

    def test_replay_returns_the_same_invoice_id_and_number(self) -> None:
        key = "idem_replay_0001"
        first = self.replay(key)
        second = self.replay(key)
        self.assertEqual(second.invoice_id, first.invoice_id)
        self.assertEqual(second.invoice_number, first.invoice_number)

    def test_replay_does_not_add_a_second_invoice_row(self) -> None:
        key = "idem_replay_0002"
        self.replay(key)
        after_first = self.count("invoices")
        self.replay(key)
        self.assertEqual(self.count("invoices"), after_first)
        self.assertEqual(after_first, 1)

    def test_replay_does_not_add_a_second_line_row(self) -> None:
        key = "idem_replay_0003"
        self.replay(key)
        after_first = self.count("invoice_lines")
        self.replay(key)
        self.assertEqual(self.count("invoice_lines"), after_first)

    def test_replay_does_not_add_a_second_idempotency_row(self) -> None:
        key = "idem_replay_0004"
        self.replay(key)
        self.replay(key)
        self.assertEqual(self.count("idempotency_keys"), 1)

    def test_replay_does_not_add_a_second_audit_row(self) -> None:
        key = "idem_replay_0005"
        self.replay(key)
        after_first = self.audit_count(INV.AUDIT_INVOICE_ISSUED)
        self.replay(key)
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_ISSUED), after_first)

    def test_replay_reflects_the_invoice_as_it_is_stored_now(self) -> None:
        """The replay path re-reads the row, so later settlements are visible."""
        key = "idem_replay_0006"
        first = self.replay(key)
        self.svc.record_settlement(
            first.invoice_id, amount_paid_minor=250_000, payment_status="PARTIAL"
        )
        replayed = self.replay(key)
        self.assertEqual(replayed.amount_paid_minor, 250_000)
        self.assertEqual(replayed.payment_status, "PARTIAL")

    def test_same_key_with_a_different_payload_is_refused(self) -> None:
        key = "idem_replay_0007"
        self.replay(key)
        self.assert_refused(
            INV.IDEMPOTENCY_CONFLICT,
            lambda: self.replay(key, lines=[self.line(unit_amount_minor=999_000)]),
        )

    def test_same_key_for_a_different_booking_is_refused(self) -> None:
        key = "idem_replay_0008"
        self.replay(key)
        self.assert_refused(INV.IDEMPOTENCY_CONFLICT, lambda: self.replay(key, booking_id="book_2"))

    def test_a_conflicting_replay_leaves_the_original_invoice_untouched(self) -> None:
        key = "idem_replay_0009"
        first = self.replay(key)
        before = self.state()
        with self.assertRaises(P.DomainError) as raised:
            self.replay(key, lines=[self.line(unit_amount_minor=123_000)])
        self.assertEqual(raised.exception.code, INV.IDEMPOTENCY_CONFLICT)
        self.assert_unchanged(before)
        stored = self.row("invoices", invoice_id=first.invoice_id)
        self.assertEqual(stored["total_minor"], first.total_minor)
        self.assertEqual(stored["status"], "ISSUED")

    def test_a_conflicting_replay_keeps_the_original_idempotency_record(self) -> None:
        key = "idem_replay_0010"
        first = self.replay(key)
        with self.assertRaises(P.DomainError) as raised:
            self.replay(key, due_days=3)
        self.assertEqual(raised.exception.code, INV.IDEMPOTENCY_CONFLICT)
        record = self.row("idempotency_keys", idempotency_key=key)
        self.assertEqual(record["operation"], INV.ISSUE_OPERATION)
        self.assertEqual(record["result_ref"], first.invoice_id)

    def test_different_keys_make_different_invoices_from_the_same_payload(self) -> None:
        first = self.replay("idem_replay_0011")
        second = self.replay("idem_replay_0012")
        self.assertNotEqual(first.invoice_id, second.invoice_id)
        self.assertNotEqual(first.invoice_number, second.invoice_number)
        self.assertEqual(self.count("invoices"), 2)

    def test_each_issued_invoice_gets_exactly_one_idempotency_record(self) -> None:
        for index in range(3):
            self.replay("idem_replay_%04d" % (100 + index))
        self.assertEqual(self.count("idempotency_keys"), self.count("invoices"))
        self.assertEqual(self.count("invoices"), 3)


class TestInvoiceNumbering(InvoiceTestBase):
    """(4) numbers are unique, per-month sequential and consistently formatted."""

    PATTERN = re.compile(r"^INV-\d{6}-\d{5}$")

    def test_generated_number_matches_the_documented_format(self) -> None:
        invoice = self.issue()
        self.assertRegex(invoice.invoice_number, self.PATTERN)

    def test_generated_number_carries_the_month_of_its_issue_instant(self) -> None:
        invoice = self.issue(issued_at="2026-10-15T09:30:00Z")
        self.assertTrue(invoice.invoice_number.startswith("INV-202610-"), invoice.invoice_number)
        self.assertEqual(invoice.issued_at, P.to_iso(P.parse_iso("2026-10-15T09:30:00Z")))

    def test_sequence_increments_within_the_same_month(self) -> None:
        first = self.issue()
        second = self.issue()
        self.assertEqual(first.invoice_number, "INV-202609-00001")
        self.assertEqual(second.invoice_number, "INV-202609-00002")

    def test_sequence_restarts_in_a_new_month(self) -> None:
        self.issue(issued_at="2026-09-30T23:00:00Z")
        october = self.issue(issued_at="2026-10-01T00:30:00Z")
        self.assertEqual(october.invoice_number, "INV-202610-00001")

    def test_sequence_ignores_stored_numbers_with_a_non_numeric_tail(self) -> None:
        self.insert_foreign_invoice("INV-202609-ABCDE")
        invoice = self.issue()
        self.assertEqual(invoice.invoice_number, "INV-202609-00001")

    def test_number_is_at_least_the_documented_minimum_length(self) -> None:
        invoice = self.issue()
        self.assertGreaterEqual(len(invoice.invoice_number), INV.MIN_INVOICE_NUMBER_LENGTH)

    def test_pinned_number_is_stored_verbatim(self) -> None:
        invoice = self.issue(invoice_number="INV-MANUAL-0001")
        self.assertEqual(invoice.invoice_number, "INV-MANUAL-0001")
        self.assertEqual(
            self.row("invoices", invoice_id=invoice.invoice_id)["invoice_number"], "INV-MANUAL-0001"
        )

    def test_duplicate_pinned_number_is_refused(self) -> None:
        self.issue(invoice_number="INV-MANUAL-0002")  # pinned number, for its effect
        self.assert_refused(
            INV.DUPLICATE_INVOICE_NUMBER,
            lambda: self.issue(invoice_number="INV-MANUAL-0002"),
        )
        self.assertEqual(self.count("invoices"), 1)
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_ISSUED), 1)

    def test_generated_number_skips_past_a_number_already_stored(self) -> None:
        """The sequence comes from the stored numbers, so a squatter is skipped, not taken."""
        self.insert_foreign_invoice("INV-202609-00001")
        invoice = self.issue()
        self.assertEqual(invoice.invoice_number, "INV-202609-00002")
        self.assertEqual(self.count("invoices"), 2)

    def test_stored_numbers_stay_unique_across_a_run_of_issues(self) -> None:
        numbers = [self.issue().invoice_number for _ in range(5)]
        self.assertEqual(len(set(numbers)), len(numbers))
        stored = [
            row["invoice_number"]
            for row in self.conn.execute("SELECT invoice_number FROM invoices").fetchall()
        ]
        self.assertEqual(sorted(stored), sorted(numbers))

    def test_a_voided_invoice_keeps_its_number_and_the_number_stays_taken(self) -> None:
        invoice = self.issue(invoice_number="INV-MANUAL-0003")
        self.svc.void(invoice_id=invoice.invoice_id, reason="issued in error")
        self.assertEqual(
            self.row("invoices", invoice_id=invoice.invoice_id)["invoice_number"], "INV-MANUAL-0003"
        )
        self.assert_refused(
            INV.DUPLICATE_INVOICE_NUMBER,
            lambda: self.issue(invoice_number="INV-MANUAL-0003"),
        )


class TestVoid(InvoiceTestBase):
    """(5) voiding needs a reason, refuses settled/already-void invoices, and sticks."""

    def test_void_refuses_a_reason_shorter_than_three_characters(self) -> None:
        invoice = self.issue()
        for reason in ("no", "  no  "):
            with self.subTest(reason=reason):
                self.assert_refused(
                    INV.INVALID_VOID_REASON,
                    lambda reason=reason: self.svc.void(invoice_id=invoice.invoice_id, reason=reason),
                )

    def test_void_refuses_a_missing_reason(self) -> None:
        invoice = self.issue()
        self.assert_refused(
            INV.INVALID_VOID_REASON,
            lambda: self.svc.void(invoice_id=invoice.invoice_id, reason=None),
        )

    def test_void_refuses_a_non_string_reason(self) -> None:
        invoice = self.issue()
        self.assert_refused(
            INV.INVALID_VOID_REASON,
            lambda: self.svc.void(invoice_id=invoice.invoice_id, reason=404),
        )

    def test_void_refuses_a_whitespace_only_reason(self) -> None:
        invoice = self.issue()
        self.assert_refused(
            ("MISSING_FIELD", "INVALID_VOID_REASON"),
            lambda: self.svc.void(invoice_id=invoice.invoice_id, reason="     "),
        )

    def test_void_marks_the_invoice_as_void(self) -> None:
        invoice = self.issue()
        voided = self.svc.void(invoice_id=invoice.invoice_id, reason="guest cancelled")
        self.assertTrue(voided.is_void)
        self.assertFalse(voided.is_open)
        self.assertEqual(voided.status, "VOID")
        self.assertEqual(voided.voided_at, NOW_ISO)
        self.assertEqual(voided.void_reason, "guest cancelled")
        self.assertTrue(self.svc.get(invoice.invoice_id).is_void)

    def test_void_stores_the_reason_with_surrounding_whitespace_stripped(self) -> None:
        invoice = self.issue()
        voided = self.svc.void(invoice_id=invoice.invoice_id, reason="   issued in error   ")
        self.assertEqual(voided.void_reason, "issued in error")
        self.assertEqual(
            self.row("invoices", invoice_id=invoice.invoice_id)["void_reason"], "issued in error"
        )

    def test_a_voided_invoice_cannot_be_voided_again(self) -> None:
        invoice = self.issue()
        self.svc.void(invoice_id=invoice.invoice_id, reason="duplicate issue")
        before = self.state()
        with self.assertRaises(P.DomainError) as raised:
            self.svc.void(invoice_id=invoice.invoice_id, reason="second attempt")
        self.assertEqual(raised.exception.code, INV.INVOICE_ALREADY_VOID)
        self.assert_unchanged(before)
        self.assertEqual(
            self.row("invoices", invoice_id=invoice.invoice_id)["void_reason"], "duplicate issue"
        )

    def test_voiding_twice_writes_exactly_one_audit_row(self) -> None:
        invoice = self.issue()
        self.svc.void(invoice_id=invoice.invoice_id, reason="duplicate issue")
        with self.assertRaises(P.DomainError):
            self.svc.void(invoice_id=invoice.invoice_id, reason="second attempt")
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_VOIDED), 1)

    def test_void_is_refused_when_money_has_been_recorded(self) -> None:
        invoice = self.issue()
        self.svc.record_settlement(
            invoice.invoice_id, amount_paid_minor=100_000, payment_status="PARTIAL"
        )
        before = self.state()
        with self.assertRaises(P.DomainError) as raised:
            self.svc.void(invoice_id=invoice.invoice_id, reason="changed my mind")
        self.assertEqual(raised.exception.code, INV.INVOICE_HAS_PAYMENTS)
        self.assert_unchanged(before)
        stored = self.row("invoices", invoice_id=invoice.invoice_id)
        self.assertEqual(stored["status"], "ISSUED")
        self.assertIsNone(stored["voided_at"])
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_VOIDED), 0)

    def test_void_is_refused_for_an_unknown_invoice(self) -> None:
        self.assert_refused(
            INV.INVOICE_NOT_FOUND, lambda: self.svc.void(invoice_id="inv_nope", reason="no such")
        )

    def test_a_voided_invoice_cannot_be_settled(self) -> None:
        invoice = self.issue()
        self.svc.void(invoice_id=invoice.invoice_id, reason="issued in error")
        before = self.state()
        with self.assertRaises(P.DomainError) as raised:
            self.svc.record_settlement(
                invoice.invoice_id, amount_paid_minor=1, payment_status="PARTIAL"
            )
        self.assertEqual(raised.exception.code, INV.INVOICE_ALREADY_VOID)
        self.assert_unchanged(before)

    def test_void_keeps_the_number_the_lines_and_the_total(self) -> None:
        invoice = self.issue()
        lines_before = self.line_states(invoice.invoice_id)
        voided = self.svc.void(invoice_id=invoice.invoice_id, reason="issued in error")
        self.assertEqual(voided.invoice_number, invoice.invoice_number)
        self.assertEqual(voided.total_minor, invoice.total_minor)
        self.assertEqual(self.line_states(invoice.invoice_id), lines_before)

    def test_a_zero_settlement_does_not_block_voiding(self) -> None:
        """The rule keys off *recorded money*, not off the payments ledger."""
        invoice = self.issue()
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=0, payment_status="UNPAID")
        voided = self.svc.void(invoice_id=invoice.invoice_id, reason="no money moved")
        self.assertTrue(voided.is_void)
        self.assertEqual(voided.amount_paid_minor, 0)

    def test_void_reports_the_new_state_on_the_audit_row(self) -> None:
        invoice = self.issue()
        self.svc.void(invoice_id=invoice.invoice_id, reason="issued in error")
        event = self.conn.execute(
            "SELECT * FROM audit_events WHERE action = ? ORDER BY seq DESC LIMIT 1",
            (INV.AUDIT_INVOICE_VOIDED,),
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event["entity_id"], invoice.invoice_id)
        self.assertIn("VOID", event["after_state"])
        self.assertIn("ISSUED", event["before_state"])


class TestRecordSettlement(InvoiceTestBase):
    """(6) settlement writes the amount, derives the status and never exceeds the total."""

    def settle(self, invoice, amount, status: str):
        """``amount`` is deliberately untyped: rejected payloads are part of the contract."""
        return self.svc.record_settlement(
            invoice.invoice_id, amount_paid_minor=amount, payment_status=status
        )

    def test_a_partial_settlement_moves_the_invoice_to_partial(self) -> None:
        invoice = self.issue()
        settled = self.settle(invoice, invoice.total_minor // 2, "PARTIAL")
        self.assertEqual(settled.amount_paid_minor, invoice.total_minor // 2)
        self.assertEqual(settled.payment_status, "PARTIAL")

    def test_a_full_settlement_moves_the_invoice_to_paid(self) -> None:
        invoice = self.issue()
        settled = self.settle(invoice, invoice.total_minor, "PAID")
        self.assertEqual(settled.payment_status, "PAID")
        self.assertTrue(settled.is_settled)

    def test_an_overpayment_moves_the_invoice_to_overpaid(self) -> None:
        invoice = self.issue()
        settled = self.settle(invoice, invoice.total_minor + 1, "OVERPAID")
        self.assertEqual(settled.payment_status, "OVERPAID")
        self.assertEqual(settled.amount_paid_minor, invoice.total_minor + 1)

    def test_a_zero_settlement_leaves_the_invoice_unpaid(self) -> None:
        invoice = self.issue()
        settled = self.settle(invoice, 0, "UNPAID")
        self.assertEqual(settled.payment_status, "UNPAID")
        self.assertEqual(settled.amount_paid_minor, 0)

    def test_the_status_chain_unpaid_partial_paid_follows_the_amount(self) -> None:
        invoice = self.issue()
        chain = [(0, "UNPAID"), (invoice.total_minor - 1, "PARTIAL"), (invoice.total_minor, "PAID")]
        for amount, expected in chain:
            with self.subTest(amount=amount):
                settled = self.settle(invoice, amount, expected)
                self.assertEqual(settled.amount_paid_minor, amount)
                self.assertEqual(settled.payment_status, expected)

    def test_outstanding_is_total_minus_paid(self) -> None:
        invoice = self.issue()
        paid = 400_000
        self.settle(invoice, paid, "PARTIAL")
        self.assertEqual(self.svc.outstanding(invoice.invoice_id).amount_minor, invoice.total_minor - paid)

    def test_outstanding_is_zero_once_the_invoice_is_paid_in_full(self) -> None:
        invoice = self.issue()
        self.settle(invoice, invoice.total_minor, "PAID")
        self.assertEqual(self.svc.outstanding(invoice.invoice_id).amount_minor, 0)

    def test_outstanding_never_goes_negative_after_an_overpayment(self) -> None:
        invoice = self.issue()
        self.settle(invoice, invoice.total_minor + 250_000, "OVERPAID")
        self.assertEqual(self.svc.outstanding(invoice.invoice_id).amount_minor, 0)

    def test_settlement_is_refused_when_it_claims_more_than_the_total_as_paid(self) -> None:
        invoice = self.issue()
        before = self.state()
        self.assert_refused(
            INV.INVALID_PAYMENT_STATUS,
            lambda: self.settle(invoice, invoice.total_minor + 1, "PAID"),
        )
        self.assert_unchanged(before)

    def test_settlement_status_must_match_the_amount(self) -> None:
        invoice = self.issue()
        cases = [
            (invoice.total_minor // 2, "PAID"),
            (invoice.total_minor, "PARTIAL"),
            (invoice.total_minor, "UNPAID"),
            (0, "PARTIAL"),
        ]
        for amount, status in cases:
            with self.subTest(amount=amount, status=status):
                self.assert_refused(
                    INV.INVALID_PAYMENT_STATUS, lambda a=amount, s=status: self.settle(invoice, a, s)
                )

    def test_settlement_refuses_an_unknown_payment_status(self) -> None:
        invoice = self.issue()
        self.assert_refused(
            INV.INVALID_PAYMENT_STATUS, lambda: self.settle(invoice, 10_000, "MOSTLY_PAID")
        )

    def test_settlement_refuses_a_negative_amount(self) -> None:
        invoice = self.issue()
        self.assert_refused(
            INV.INVALID_PAYMENT_STATUS, lambda: self.settle(invoice, -1, "REFUNDED")
        )

    def test_settlement_refuses_a_float_amount(self) -> None:
        invoice = self.issue()
        self.assert_refused(INV.INVALID_PAYMENT_STATUS, lambda: self.settle(invoice, 1.5, "PARTIAL"))

    def test_settlement_refuses_a_boolean_amount(self) -> None:
        invoice = self.issue()
        self.assert_refused(INV.INVALID_PAYMENT_STATUS, lambda: self.settle(invoice, True, "PARTIAL"))

    def test_settlement_refuses_an_unknown_invoice(self) -> None:
        self.assert_refused(
            INV.INVOICE_NOT_FOUND,
            lambda: self.svc.record_settlement("inv_nope", amount_paid_minor=1, payment_status="PARTIAL"),
        )

    def test_settlement_refuses_a_void_invoice(self) -> None:
        invoice = self.issue()
        self.svc.void(invoice_id=invoice.invoice_id, reason="issued in error")
        self.assert_refused(
            INV.INVOICE_ALREADY_VOID,
            lambda: self.svc.record_settlement(
                invoice.invoice_id, amount_paid_minor=10_000, payment_status="PARTIAL"
            ),
        )

    def test_a_refund_larger_than_the_total_is_refused(self) -> None:
        invoice = self.issue()
        self.settle(invoice, invoice.total_minor, "PAID")
        self.assert_refused(
            INV.INVALID_PAYMENT_STATUS,
            lambda: self.settle(invoice, invoice.total_minor + 1, "REFUNDED"),
        )

    def test_a_refund_below_the_total_is_accepted(self) -> None:
        """REFUNDED is a fact about money moving back, so it is allowed at any amount."""
        invoice = self.issue()
        self.settle(invoice, invoice.total_minor, "PAID")
        refunded = self.settle(invoice, 0, "REFUNDED")
        self.assertEqual(refunded.payment_status, "REFUNDED")
        self.assertEqual(refunded.amount_paid_minor, 0)

    def test_settlement_writes_the_amount_into_the_invoice_row(self) -> None:
        invoice = self.issue()
        self.settle(invoice, 300_000, "PARTIAL")
        stored = self.row("invoices", invoice_id=invoice.invoice_id)
        self.assertEqual(stored["amount_paid_minor"], 300_000)
        self.assertEqual(stored["payment_status"], "PARTIAL")
        self.assertEqual(stored["status"], "ISSUED")

    def test_settlement_does_not_write_to_the_payments_ledger(self) -> None:
        """``payments`` owns the ledger; this service only mirrors the invoice total."""
        invoice = self.issue()
        self.settle(invoice, invoice.total_minor, "PAID")
        self.assertEqual(self.count("payments"), 0)

    def test_settlement_advances_the_booking_version_lock(self) -> None:
        invoice = self.issue()
        before = self.row("bookings", booking_id="book_1")["version_lock"]
        self.settle(invoice, 100_000, "PARTIAL")
        after = self.row("bookings", booking_id="book_1")["version_lock"]
        self.assertGreater(after, before)


class TestOverdue(InvoiceTestBase):
    """(7) ``is_overdue`` is strictly-after the due instant, and only for unpaid issued invoices."""

    def test_due_at_is_issued_at_plus_the_requested_days(self) -> None:
        invoice = self.issue(due_days=3)
        issued = P.parse_iso(invoice.issued_at)
        due = P.parse_iso(invoice.due_at)
        self.assertEqual(due - issued, timedelta(days=3))

    def test_due_at_defaults_to_seven_days_after_issue(self) -> None:
        invoice = self.issue()
        issued = P.parse_iso(invoice.issued_at)
        due = P.parse_iso(invoice.due_at)
        self.assertEqual(due - issued, timedelta(days=INV.DEFAULT_DUE_DAYS))

    def test_an_invoice_is_not_overdue_exactly_at_its_due_instant(self) -> None:
        invoice = self.issue()
        stored = self.svc.get(invoice.invoice_id)
        self.assertFalse(stored.is_overdue(P.parse_iso(invoice.due_at)))

    def test_an_invoice_is_overdue_one_second_past_its_due_instant(self) -> None:
        invoice = self.issue()
        stored = self.svc.get(invoice.invoice_id)
        self.assertTrue(stored.is_overdue(P.parse_iso(invoice.due_at) + timedelta(seconds=1)))

    def test_an_invoice_is_not_overdue_more_than_a_day_early(self) -> None:
        invoice = self.issue(due_days=7)
        stored = self.svc.get(invoice.invoice_id)
        self.assertFalse(stored.is_overdue(P.parse_iso(invoice.due_at) - timedelta(days=1)))

    def test_a_paid_invoice_is_never_overdue(self) -> None:
        invoice = self.issue()
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=invoice.total_minor, payment_status="PAID")
        stored = self.svc.get(invoice.invoice_id)
        self.assertFalse(stored.is_overdue(P.parse_iso(invoice.due_at) + timedelta(days=30)))

    def test_a_voided_invoice_is_never_overdue(self) -> None:
        invoice = self.issue()
        self.svc.void(invoice_id=invoice.invoice_id, reason="issued in error")
        stored = self.svc.get(invoice.invoice_id)
        self.assertFalse(stored.is_overdue(P.parse_iso(invoice.due_at) + timedelta(days=30)))

    def test_an_invoice_due_tomorrow_is_not_overdue_today(self) -> None:
        invoice = self.issue(issued_at=NOW_ISO, due_days=1)
        stored = self.svc.get(invoice.invoice_id)
        self.assertFalse(stored.is_overdue(P.parse_iso(NOW_ISO)))

    def test_the_overdue_query_lists_only_unpaid_past_due_invoices(self) -> None:
        overdue = self.issue()
        paid = self.issue()
        self.svc.record_settlement(paid.invoice_id, amount_paid_minor=paid.total_minor, payment_status="PAID")
        later = P.parse_iso(overdue.due_at) + timedelta(seconds=1)
        listed = [found.invoice_id for found in self.svc.overdue(later)]
        self.assertEqual(listed, [overdue.invoice_id])

    def test_the_overdue_query_is_empty_before_the_due_instant(self) -> None:
        invoice = self.issue()
        self.assertEqual(self.svc.overdue(P.parse_iso(invoice.due_at)), ())

    def test_the_overdue_query_excludes_void_invoices(self) -> None:
        voided = self.issue()
        self.svc.void(invoice_id=voided.invoice_id, reason="issued in error")
        later = P.parse_iso(voided.due_at) + timedelta(days=1)
        self.assertEqual(self.svc.overdue(later), ())


class TestVerify(InvoiceTestBase):
    """(8) ``verify()`` reconciles stored rows with the totals they were written with."""

    def test_verify_returns_the_invoice_it_stored(self) -> None:
        invoice = self.issue()
        checked = self.svc.verify(invoice.invoice_id)
        self.assertEqual(checked.invoice_id, invoice.invoice_id)
        self.assertEqual(checked.total_minor, invoice.total_minor)
        self.assertEqual(checked.as_row(), self.row("invoices", invoice_id=invoice.invoice_id))

    def test_verify_reports_the_number_of_stored_lines(self) -> None:
        invoice = self.issue(
            lines=[self.line(), self.line(kind="SERVICE", quantity=1, unit_amount_minor=150_000)]
        )
        self.assertEqual(len(self.svc.lines(invoice.invoice_id)), 2)
        self.svc.verify(invoice.invoice_id)
        self.assertEqual(self.count("invoice_lines"), 2)

    def test_verify_detects_line_money_tampered_underneath_the_service(self) -> None:
        invoice = self.issue()
        # Plain digits on purpose: SQLite only accepts underscores in numeric literals from
        # 3.46, and the interpreter this runs under decides which SQLite is linked in (3.45.1
        # on the system Python here, 3.53 in the project venv). A tamper value must not be the
        # thing that makes the suite interpreter-dependent.
        self.conn.execute(
            "UPDATE invoice_lines SET quantity = 3, amount_minor = 2250000 WHERE invoice_id = ?",
            (invoice.invoice_id,),
        )
        with self.assertRaises(P.DomainError) as raised:
            self.svc.verify(invoice.invoice_id)
        self.assertEqual(raised.exception.code, INV.INVOICE_LINES_MISMATCH)

    def test_verify_detects_a_tampered_invoice_total(self) -> None:
        invoice = self.issue()
        self.conn.execute(
            "UPDATE invoices SET subtotal_minor = subtotal_minor + 5, total_minor = total_minor + 5"
            " WHERE invoice_id = ?",
            (invoice.invoice_id,),
        )
        with self.assertRaises(P.DomainError) as raised:
            self.svc.verify(invoice.invoice_id)
        self.assertEqual(raised.exception.code, INV.INVOICE_LINES_MISMATCH)

    def test_verify_detects_a_deleted_line(self) -> None:
        invoice = self.issue()
        self.conn.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice.invoice_id,))
        with self.assertRaises(P.DomainError) as raised:
            self.svc.verify(invoice.invoice_id)
        self.assertEqual(raised.exception.code, INV.INVOICE_LINES_MISMATCH)

    def test_verify_writes_nothing(self) -> None:
        invoice = self.issue()
        before = self.state()
        self.svc.verify(invoice.invoice_id)
        self.assert_unchanged(before)

    def test_verify_still_holds_after_a_settlement(self) -> None:
        invoice = self.issue()
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=200_000, payment_status="PARTIAL")
        checked = self.svc.verify(invoice.invoice_id)
        self.assertEqual(checked.amount_paid_minor, 200_000)
        self.assertEqual(
            self.svc.outstanding(invoice.invoice_id).amount_minor,
            checked.total_minor - checked.amount_paid_minor,
        )

    def test_verify_holds_for_a_voided_invoice(self) -> None:
        invoice = self.issue()
        self.svc.void(invoice_id=invoice.invoice_id, reason="issued in error")
        self.assertTrue(self.svc.verify(invoice.invoice_id).is_void)

    def test_verify_rejects_an_unknown_invoice(self) -> None:
        self.assert_refused(INV.INVOICE_NOT_FOUND, lambda: self.svc.verify("inv_nope"))

    def test_verify_reconciles_every_line_amount_with_quantity_times_unit(self) -> None:
        invoice = self.issue(
            lines=[
                self.line(quantity=3, unit_amount_minor=100_000),
                self.line(kind="TAX", quantity=1, unit_amount_minor=30_000),
            ]
        )
        for row in self.line_states(invoice.invoice_id):
            self.assertEqual(row["amount_minor"], row["quantity"] * row["unit_amount_minor"])


class TestAuditTrail(InvoiceTestBase):
    """(9) every write leaves exactly one audit event — and no write goes unrecorded."""

    def test_issue_writes_one_invoice_issued_event(self) -> None:
        invoice = self.issue()
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_ISSUED), 1)
        event = self.conn.execute(
            "SELECT * FROM audit_events WHERE action = ?", (INV.AUDIT_INVOICE_ISSUED,)
        ).fetchone()
        self.assertEqual(event["entity_type"], "invoice")
        self.assertEqual(event["entity_id"], invoice.invoice_id)

    def test_void_writes_one_invoice_voided_event(self) -> None:
        invoice = self.issue()
        self.svc.void(invoice_id=invoice.invoice_id, reason="issued in error")
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_VOIDED), 1)
        event = self.conn.execute(
            "SELECT * FROM audit_events WHERE action = ?", (INV.AUDIT_INVOICE_VOIDED,)
        ).fetchone()
        self.assertEqual(event["entity_id"], invoice.invoice_id)

    def test_settlement_writes_one_invoice_settlement_recorded_event(self) -> None:
        invoice = self.issue()
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=50_000, payment_status="PARTIAL")
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_SETTLED), 1)

    def test_every_invoice_row_has_a_matching_issued_event(self) -> None:
        for index in range(4):
            self.issue(booking_id="book_1" if index % 2 else "book_2")
        self.assertEqual(self.count("invoices"), 4)
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_ISSUED), self.count("invoices"))

    def test_the_audit_total_equals_the_number_of_successful_writes(self) -> None:
        first = self.issue()
        second = self.issue()
        self.svc.void(invoice_id=second.invoice_id, reason="issued in error")
        self.svc.record_settlement(first.invoice_id, amount_paid_minor=1, payment_status="PARTIAL")
        self.assertEqual(self.count("audit_events"), 4)
        self.assertEqual(
            self.count("audit_events"),
            self.audit_count(INV.AUDIT_INVOICE_ISSUED)
            + self.audit_count(INV.AUDIT_INVOICE_VOIDED)
            + self.audit_count(INV.AUDIT_INVOICE_SETTLED),
        )

    def test_a_refused_issue_writes_no_audit_row(self) -> None:
        self.assert_refused(
            INV.BOOKING_NOT_FOUND,
            lambda: self.issue(booking_id="book_missing"),
        )
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_ISSUED), 0)

    def test_a_refused_void_writes_no_audit_row(self) -> None:
        invoice = self.issue()
        self.assert_refused(
            INV.INVALID_VOID_REASON, lambda: self.svc.void(invoice_id=invoice.invoice_id, reason="x")
        )
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_VOIDED), 0)

    def test_a_refused_settlement_writes_no_audit_row(self) -> None:
        invoice = self.issue()
        self.assert_refused(
            INV.INVALID_PAYMENT_STATUS,
            lambda: self.svc.record_settlement(
                invoice.invoice_id, amount_paid_minor=1, payment_status="PAID"
            ),
        )
        self.assertEqual(self.audit_count(INV.AUDIT_INVOICE_SETTLED), 0)

    def test_only_the_three_documented_actions_are_written(self) -> None:
        first = self.issue()
        second = self.issue()
        self.svc.record_settlement(first.invoice_id, amount_paid_minor=1, payment_status="PARTIAL")
        self.svc.void(invoice_id=second.invoice_id, reason="issued in error")
        self.assertEqual(
            sorted(set(self.audit_actions())),
            sorted({INV.AUDIT_INVOICE_ISSUED, INV.AUDIT_INVOICE_VOIDED, INV.AUDIT_INVOICE_SETTLED}),
        )

    def test_the_audit_chain_verifies_and_covers_every_event(self) -> None:
        settled = self.issue()
        voided = self.issue()
        self.svc.record_settlement(settled.invoice_id, amount_paid_minor=1, payment_status="PARTIAL")
        self.svc.void(invoice_id=voided.invoice_id, reason="never mind")
        report = A.AuditLog(self.conn, clock=self.clock, ids=self.ids).verify()
        self.assertEqual(report.count, self.count("audit_events"))
        head = self.conn.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual(report.head_hash, head["hash"])

    def test_events_for_one_invoice_are_found_by_entity_id(self) -> None:
        invoice = self.issue()
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=1, payment_status="PARTIAL")
        log = A.AuditLog(self.conn, clock=self.clock, ids=self.ids)
        actions = [event.action for event in log.for_entity("invoice", invoice.invoice_id)]
        self.assertEqual(actions, [INV.AUDIT_INVOICE_ISSUED, INV.AUDIT_INVOICE_SETTLED])

    def test_an_issue_event_carries_the_caller_actor(self) -> None:
        invoice = self.issue(actor="agent_7", actor_role="reservations")
        event = self.conn.execute(
            "SELECT * FROM audit_events WHERE action = ?", (INV.AUDIT_INVOICE_ISSUED,)
        ).fetchone()
        self.assertEqual(event["entity_id"], invoice.invoice_id)
        self.assertEqual(event["actor"], "agent_7")
        self.assertEqual(event["actor_role"], "reservations")


class TestMoneyIsNeverFloat(InvoiceTestBase):
    """(10) every money column is an integer in SQLite and an int in Python."""

    INVOICE_MONEY = ("subtotal_minor", "tax_minor", "service_minor", "total_minor", "amount_paid_minor")
    LINE_MONEY = ("quantity", "unit_amount_minor", "amount_minor")

    def test_invoice_money_columns_are_stored_as_sqlite_integers(self) -> None:
        invoice = self.issue()
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=10, payment_status="PARTIAL")
        types = self.money_typeof("invoices", self.INVOICE_MONEY)
        self.assertEqual(types, {column: "integer" for column in self.INVOICE_MONEY})

    def test_invoice_line_money_columns_are_stored_as_sqlite_integers(self) -> None:
        self.issue(lines=[self.line(), self.line(kind="TAX", quantity=1, unit_amount_minor=75_000)])
        types = self.money_typeof("invoice_lines", self.LINE_MONEY)
        self.assertEqual(types, {column: "integer" for column in self.LINE_MONEY})

    def test_invoice_money_is_exposed_as_python_ints(self) -> None:
        invoice = self.issue()
        for column in self.INVOICE_MONEY:
            with self.subTest(column=column):
                value = invoice.as_row()[column]
                self.assertIsInstance(value, int)
                self.assertNotIsInstance(value, bool)

    def test_invoice_line_money_is_exposed_as_python_ints(self) -> None:
        invoice = self.issue()
        for row in self.line_states(invoice.invoice_id):
            for column in self.LINE_MONEY:
                with self.subTest(column=column):
                    self.assertIsInstance(row[column], int)
                    self.assertNotIsInstance(row[column], bool)

    def test_no_money_column_holds_a_float_after_a_settlement(self) -> None:
        invoice = self.issue()
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=123_456, payment_status="PARTIAL")
        for table, columns in (("invoices", self.INVOICE_MONEY), ("invoice_lines", self.LINE_MONEY)):
            with self.subTest(table=table):
                types = self.money_typeof(table, columns)
                self.assertNotIn("real", set(types.values()))

    def test_the_booking_mirror_money_is_stored_as_an_integer(self) -> None:
        invoice = self.issue()
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=invoice.total_minor, payment_status="PAID")
        types = self.money_typeof("bookings", ("total_minor", "amount_paid_minor"))
        self.assertEqual(types, {"total_minor": "integer", "amount_paid_minor": "integer"})

    def test_money_properties_expose_integer_minor_units(self) -> None:
        invoice = self.issue()
        for money in (invoice.subtotal, invoice.tax, invoice.service, invoice.total, invoice.amount_paid):
            with self.subTest(currency=money.currency):
                self.assertIsInstance(money.amount_minor, int)
                self.assertEqual(money.currency, CURRENCY)


class TestDefectsFixedAndPinned(InvoiceTestBase):
    """Regressions for five defects an independent review found in this module.

    They were originally pinned with ``expectedFailure``; each one started passing
    once the module was fixed, which is exactly the signal that was designed into
    them. The marker is gone and the defect story stays in every docstring: a fixed
    bug without its original evidence comes back unnoticed.
    """

    def test_issue_refuses_a_line_with_zero_quantity_beside_a_real_line(self) -> None:
        """DEFECT: a ``quantity=0`` line is accepted when other lines carry the total.

        ``LineSpec`` only rejects ``quantity < 0`` (invoicing.py:~310) and
        ``ck_invoice_lines_quantity`` allows 0, so ``issue()`` happily stores a
        line for nothing. A zero-quantity line is a pricing mistake, not an
        invoice line: it should be refused the way a negative one is.
        """
        lines = [self.line(), self.line(kind="SERVICE", quantity=0, unit_amount_minor=150_000)]
        self.assert_refused(INV.INVALID_INVOICE_LINE, lambda: self.issue(lines=lines))

    def test_issue_reports_a_non_string_currency_as_a_domain_error(self) -> None:
        """DEFECT: a non-string currency crashes with ``AttributeError``.

        ``_require_currency`` funnels the value through ``Money(0, value)`` but
        only catches ``DomainError``; ``Money`` starts by calling ``.strip()`` on
        the code, so ``currency=840`` escapes as ``AttributeError: 'int' object
        has no attribute 'strip'`` instead of a coded ``INVALID_INVOICE_ISSUE``.
        """
        self.assert_refused(INV.INVALID_ISSUE_REQUEST, lambda: self.issue(currency=840))

    def test_record_settlement_mirrors_the_new_amount_onto_the_booking(self) -> None:
        """DEFECT: the booking mirror lags one settlement behind.

        ``record_settlement`` reads ``current`` *before* the UPDATE and then calls
        ``self._sync_booking(current)`` (invoicing.py:~1130), so the booking is
        written with the invoice's previous ``amount_paid_minor``/``payment_status``.
        A booking whose only invoice was paid in full ends up ``UNPAID``.
        """
        invoice = self.issue()
        self.svc.record_settlement(
            invoice.invoice_id, amount_paid_minor=invoice.total_minor, payment_status="PAID"
        )
        booking = self.row("bookings", booking_id="book_1")
        self.assertEqual(booking["amount_paid_minor"], min(invoice.total_minor, booking["total_minor"]))
        self.assertEqual(booking["payment_status"], INV.BOOKING_PAYMENT_STATUS["PAID"])

    def test_issue_works_without_the_payments_module_alias(self) -> None:
        """DEFECT: importing ``invoicing`` alone leaves every write raising ``AttributeError``.

        ``Invoice.as_dict()`` projects money with ``Money.as_dict()``, but
        ``primitives.Money`` names that serialiser ``to_dict()``. The alias that
        bridges them lives in ``payments._ensure_money_serialiser_alias`` and is
        applied as an import side effect, so an invoice write depends on another
        module having been imported first. The alias belongs in ``primitives``.
        """
        had_alias = hasattr(P.Money, "as_dict")
        original = P.Money.__dict__.get("as_dict")
        try:
            if had_alias:
                del P.Money.as_dict
            invoice = self.issue()
            self.assertTrue(invoice.is_open)
        finally:
            # Restore the previous state exactly: a test that re-installs an alias
            # nobody had leaks it into every later test in the same process.
            if had_alias:
                P.Money.as_dict = original
            # else: nothing was installed by this test, so nothing is restored —
            # __dict__ of a class is a mappingproxy and cannot be mutated anyway.

    def test_settlement_mirrors_the_amount_after_a_second_settlement(self) -> None:
        """DEFECT (same root cause): only the *second* settlement reaches the booking intact."""
        invoice = self.issue()
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=100_000, payment_status="PARTIAL")
        self.svc.record_settlement(invoice.invoice_id, amount_paid_minor=200_000, payment_status="PARTIAL")
        booking = self.row("bookings", booking_id="book_1")
        self.assertEqual(booking["amount_paid_minor"], 200_000)
