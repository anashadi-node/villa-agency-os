"""Guest billing — the invoice a stay is settled against.

Authority chain: ``src/schema/001_initial.sql`` owns the storage contract for
``invoices``/``invoice_lines``; this module owns the *derivation* rules that turn
a priced quote into a payable invoice and keep ``amount_paid_minor`` honest;
``src/payments.py`` owns the money movement that feeds it back.

Invariants this module enforces, all fail-closed (nothing is written before a
check passes, and every refusal is a :class:`primitives.DomainError` with a stable
``code``):

1. An invoice is only ever issued from lines that sum to its own totals:
   ``subtotal == sum(non-discount lines)``, ``tax == sum(TAX lines)``,
   ``service == sum(SERVICE lines)`` and ``total == subtotal + tax + service``.
   Otherwise ``INVOICE_LINES_MISMATCH``.
2. A line amount is exactly ``quantity * unit_amount_minor`` (the DDL CHECK says
   the same thing); only ``DISCOUNT`` lines may be negative.
3. ``amount_paid_minor``/``payment_status`` are *derived*: only
   :meth:`InvoiceService.record_settlement` may move them, the pair must agree
   with the invoice total, and a mismatch is ``INVALID_PAYMENT_STATUS``. That
   method is what ``payments.PaymentService`` calls inside its own transaction.
4. Invoice numbers are allocated by the service from the issue instant
   (``INV-YYYYMM-NNNNN``), never taken as free text; a caller may pin one only if
   it is at least :data:`MIN_INVOICE_NUMBER_LENGTH` characters and unused.
5. Replaying an idempotency key returns the invoice that was already issued;
   reusing it with a different payload is ``IDEMPOTENCY_CONFLICT``.
6. Voiding keeps the number, requires a reason of at least
   :data:`MIN_VOID_REASON` characters and is refused once anything has been paid
   (``INVOICE_HAS_PAYMENTS``) — the DDL enforces the same pair.
7. Free text (line description, void reason) is validated before storage and
   every amount leaves this module as a :class:`primitives.Money`, never a float.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Optional, Sequence

from audit import AuditLog
from primitives import (
    Clock,
    DomainError,
    EntityNotFound,
    IdFactory,
    Money,
    SYSTEM_ACTOR,
    SystemClock,
    canonical_json,
    parse_iso,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    "INVOICE_STATUSES",
    "PAYMENT_STATUSES",
    "LINE_KINDS",
    "QUOTE_LINE_KINDS",
    "BOOKING_PAYMENT_STATUS",
    "DEFAULT_DUE_DAYS",
    "MIN_INVOICE_NUMBER_LENGTH",
    "MIN_VOID_REASON",
    "ISSUE_OPERATION",
    "SETTLE_OPERATION",
    "Invoice",
    "InvoiceLine",
    "LineSpec",
    "InvoiceService",
    "derive_totals",
    "check_lines_against_totals",
    "booking_payment_status",
    "lines_from_quote",
    "invoice_number_for",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
INVOICE_STATUSES = ("ISSUED", "VOID")

#: ``invoices.payment_status`` — the DDL pins the same five values.
PAYMENT_STATUSES = ("UNPAID", "PARTIAL", "PAID", "OVERPAID", "REFUNDED")

#: ``invoice_lines.kind`` — what the line is for, not who owes it.
LINE_KINDS = (
    "NIGHTLY",
    "SURCHARGE",
    "CLEANING",
    "DISCOUNT",
    "TAX",
    "SERVICE",
    "OTHER",
)

#: A quote line code -> invoice line kind. ``subtotal``/``total`` are aggregates
#: and ``security_deposit`` is refundable, so neither becomes a receivable.
QUOTE_LINE_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "extra_guests": "SURCHARGE",
        "cleaning": "CLEANING",
        "service_fee": "SERVICE",
        "tax_pbjt": "TAX",
    }
)

QUOTE_LINE_PREFIX_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "night:": "NIGHTLY",
        "extra:": "OTHER",
        "discount:": "DISCOUNT",
    }
)

#: Quote line codes that never appear on an invoice, with the reason.
QUOTE_EXCLUDED_LINES: Mapping[str, str] = MappingProxyType(
    {
        "subtotal": "aggregate of the lines below it",
        "total": "aggregate of the lines below it",
        "security_deposit": "refundable, held outside the receivable",
    }
)

#: ``bookings.payment_status`` speaks a different, coarser vocabulary.
BOOKING_PAYMENT_STATUS: Mapping[str, str] = MappingProxyType(
    {
        "UNPAID": "UNPAID",
        "PARTIAL": "DEPOSIT_CLEARED",
        "PAID": "CLEARED_FULL",
        "OVERPAID": "CLEARED_FULL",
        "REFUNDED": "REFUNDED",
    }
)

DEFAULT_DUE_DAYS = 7
MIN_INVOICE_NUMBER_LENGTH = 8
MIN_VOID_REASON = 3
MIN_LINE_DESCRIPTION = 3
MIN_IDEMPOTENCY_KEY = 8
MAX_CURRENCY_MISMATCH = 3

ISSUE_OPERATION = "invoice.issue"
SETTLE_OPERATION = "invoice.settlement"

# Stable failure codes (tests and callers branch on these, never on messages).
INVOICE_NOT_FOUND = "INVOICE_NOT_FOUND"
BOOKING_NOT_FOUND = "BOOKING_NOT_FOUND"
INVALID_ISSUE_REQUEST = "INVALID_INVOICE_ISSUE"
INVALID_INVOICE_LINE = "INVALID_INVOICE_LINE"
INVOICE_LINES_MISMATCH = "INVOICE_LINES_MISMATCH"
INVALID_IDEMPOTENCY_KEY = "INVALID_IDEMPOTENCY_KEY"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
DUPLICATE_INVOICE_NUMBER = "DUPLICATE_INVOICE_NUMBER"
INVOICE_ALREADY_VOID = "INVOICE_ALREADY_VOID"
INVOICE_HAS_PAYMENTS = "INVOICE_HAS_PAYMENTS"
INVALID_VOID_REASON = "INVALID_VOID_REASON"
INVALID_PAYMENT_STATUS = "INVALID_PAYMENT_STATUS"
INVOICE_QUOTE_MISMATCH = "INVOICE_QUOTE_MISMATCH"

AUDIT_INVOICE_ISSUED = "invoice.issued"
AUDIT_INVOICE_VOIDED = "invoice.voided"
AUDIT_INVOICE_SETTLED = "invoice.settlement_recorded"

INVOICE_COLUMNS = (
    "invoice_id",
    "invoice_number",
    "booking_id",
    "policy_fingerprint",
    "currency",
    "subtotal_minor",
    "tax_minor",
    "service_minor",
    "total_minor",
    "amount_paid_minor",
    "status",
    "payment_status",
    "issued_at",
    "due_at",
    "voided_at",
    "void_reason",
    "created_at",
)

LINE_COLUMNS = (
    "line_id",
    "invoice_id",
    "kind",
    "description",
    "quantity",
    "unit_amount_minor",
    "amount_minor",
    "rate_card_id",
    "nights",
    "created_at",
)


# --------------------------------------------------------------------------- #
# Small shared helpers (the repo keeps one private copy per service module)
# --------------------------------------------------------------------------- #
@contextmanager
def _write_transaction(conn: Any) -> Iterator[None]:
    """Run the body in an IMMEDIATE transaction, or join the caller's.

    IMMEDIATE takes the write lock before rows are read, so two concurrent
    issues cannot both read "last number is INV-202609-00004" and both claim
    ``...-00005``. When the caller (``payments.PaymentService``) already opened a
    transaction we must neither nest a BEGIN nor commit their work.
    """
    if getattr(conn, "in_transaction", False):
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _normalise_instant(value: Optional[Any], *, field_name: str) -> str:
    """Accept a datetime or ISO string, store the canonical UTC form."""
    if value is None:
        raise DomainError(f"{field_name} is required", code="MISSING_FIELD", field=field_name)
    if isinstance(value, datetime):
        return to_iso(value)
    return to_iso(parse_iso(require_text(value, field_name, max_len=40)))


def _require_key(value: Any) -> str:
    """Idempotency keys are refs, and long enough not to collide by accident."""
    key = require_ref(value, "idempotency_key", max_len=128)
    if len(key) < MIN_IDEMPOTENCY_KEY:
        raise DomainError(
            f"idempotency_key must be at least {MIN_IDEMPOTENCY_KEY} characters",
            code=INVALID_IDEMPOTENCY_KEY,
            field="idempotency_key",
            length=len(key),
        )
    return key


def _require_currency(value: Any) -> str:
    """Validate through Money so 'IDR'/'idr' and unknown codes behave as elsewhere."""
    try:
        return Money(0, value).currency
    except DomainError as exc:
        raise DomainError(
            f"unsupported currency: {value!r}", code=INVALID_ISSUE_REQUEST, field="currency"
        ) from exc


def _require_fingerprint(value: Any) -> str:
    text = require_ref(value, "policy_fingerprint", max_len=64)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise DomainError(
            "policy_fingerprint must be 64 lowercase hex characters",
            code=INVALID_ISSUE_REQUEST,
            field="policy_fingerprint",
        )
    return text


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError(
            f"{field_name} must be an integer (minor units)",
            code=INVALID_INVOICE_LINE,
            field=field_name,
        )
    return value


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    return _as_int(value, field_name)


# --------------------------------------------------------------------------- #
# Line specification
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LineSpec:
    """One priced line as the caller hands it over — validated, never trusted."""

    kind: str
    description: str
    quantity: int
    unit_amount_minor: int
    rate_card_id: Optional[str] = None
    nights: Optional[int] = None

    @property
    def amount_minor(self) -> int:
        return self.quantity * self.unit_amount_minor

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "quantity": self.quantity,
            "unit_amount_minor": self.unit_amount_minor,
            "amount_minor": self.amount_minor,
            "rate_card_id": self.rate_card_id,
            "nights": self.nights,
        }

    @classmethod
    def from_payload(cls, raw: Any) -> "LineSpec":
        if isinstance(raw, LineSpec):
            return raw
        if not isinstance(raw, Mapping):
            raise DomainError(
                "each invoice line must be a mapping",
                code=INVALID_INVOICE_LINE,
                got=type(raw).__name__,
            )
        allowed = set(cls.__dataclass_fields__) | {"amount_minor"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise DomainError(
                "unknown invoice line field(s)",
                code=INVALID_INVOICE_LINE,
                unknown=unknown,
            )
        kind = require_ref(raw.get("kind"), "kind", max_len=16)
        if kind not in LINE_KINDS:
            raise DomainError(
                "unknown invoice line kind",
                code=INVALID_INVOICE_LINE,
                kind=kind,
                allowed=list(LINE_KINDS),
            )
        description = require_text(raw.get("description"), "description", max_len=200)
        if len(description) < MIN_LINE_DESCRIPTION:
            raise DomainError(
                f"description must be at least {MIN_LINE_DESCRIPTION} characters",
                code=INVALID_INVOICE_LINE,
                field="description",
            )
        quantity = _as_int(raw.get("quantity"), "quantity")
        if quantity < 0:
            raise DomainError(
                "quantity must not be negative", code=INVALID_INVOICE_LINE, field="quantity"
            )
        unit_amount = _as_int(raw.get("unit_amount_minor"), "unit_amount_minor")
        spec = cls(
            kind=kind,
            description=description,
            quantity=quantity,
            unit_amount_minor=unit_amount,
            rate_card_id=(
                require_ref(raw["rate_card_id"], "rate_card_id", max_len=64)
                if raw.get("rate_card_id") is not None
                else None
            ),
            nights=_optional_int(raw.get("nights"), "nights"),
        )
        if spec.nights is not None and spec.nights < 0:
            raise DomainError(
                "nights must not be negative", code=INVALID_INVOICE_LINE, field="nights"
            )
        declared = raw.get("amount_minor")
        if declared is not None and _as_int(declared, "amount_minor") != spec.amount_minor:
            raise DomainError(
                "line amount_minor must equal quantity * unit_amount_minor",
                code=INVALID_INVOICE_LINE,
                field="amount_minor",
                amount_minor=declared,
                expected=spec.amount_minor,
            )
        if spec.kind == "DISCOUNT":
            if spec.amount_minor > 0:
                raise DomainError(
                    "DISCOUNT lines must not be positive",
                    code=INVALID_INVOICE_LINE,
                    amount_minor=spec.amount_minor,
                )
        elif spec.amount_minor < 0:
            raise DomainError(
                "only DISCOUNT lines may be negative",
                code=INVALID_INVOICE_LINE,
                kind=spec.kind,
                amount_minor=spec.amount_minor,
            )
        return spec


# --------------------------------------------------------------------------- #
# Derivation rules (pure, testable without a database)
# --------------------------------------------------------------------------- #
def derive_totals(lines: Sequence[Any]) -> tuple[int, int, int, int]:
    """Return ``(subtotal, tax, service, total)`` for validated lines.

    ``subtotal`` is everything except tax and service, discounts included as the
    negative amounts they are. A negative bucket is a pricing bug, not a payable,
    so it is refused rather than stored.
    """
    subtotal = 0
    tax = 0
    service = 0
    for line in lines:
        kind = getattr(line, "kind", None)
        amount = getattr(line, "amount_minor", None)
        if kind is None or not isinstance(amount, int):
            raise DomainError(
                "lines must expose kind and amount_minor",
                code=INVALID_INVOICE_LINE,
            )
        if kind == "TAX":
            tax += amount
        elif kind == "SERVICE":
            service += amount
        else:
            subtotal += amount
    total = subtotal + tax + service
    if subtotal < 0 or tax < 0 or service < 0 or total < 0:
        raise DomainError(
            "line totals must not be negative",
            code=INVOICE_LINES_MISMATCH,
            subtotal_minor=subtotal,
            tax_minor=tax,
            service_minor=service,
            total_minor=total,
        )
    return subtotal, tax, service, total


def check_lines_against_totals(
    lines: Sequence[Any], *, subtotal_minor: int, tax_minor: int, service_minor: int, total_minor: int
) -> None:
    """Assert stored lines still add up to the stored totals (reads included)."""
    derived = derive_totals(lines)
    expected = (subtotal_minor, tax_minor, service_minor, total_minor)
    if derived != expected:
        raise DomainError(
            "invoice lines do not reconcile to the invoice totals",
            code=INVOICE_LINES_MISMATCH,
            derived={
                "subtotal_minor": derived[0],
                "tax_minor": derived[1],
                "service_minor": derived[2],
                "total_minor": derived[3],
            },
            stored={
                "subtotal_minor": subtotal_minor,
                "tax_minor": tax_minor,
                "service_minor": service_minor,
                "total_minor": total_minor,
            },
        )


def booking_payment_status(payment_status: str, amount_paid_minor: int) -> str:
    """Translate an invoice status into ``bookings.payment_status``.

    A fully refunded invoice that still holds money is a *partial* refund: the
    booking vocabulary has a word for that and the coarser one does not.
    """
    if payment_status not in PAYMENT_STATUSES:
        raise DomainError(
            "unknown payment status", code=INVALID_PAYMENT_STATUS, payment_status=payment_status
        )
    if payment_status == "REFUNDED":
        return "PARTIALLY_REFUNDED" if amount_paid_minor > 0 else "REFUNDED"
    return BOOKING_PAYMENT_STATUS[payment_status]


def invoice_number_for(issued_at_iso: str, sequence: int) -> str:
    """Allocate ``INV-YYYYMM-NNNNN`` from the issue instant (UTC month)."""
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise DomainError(
            "sequence must be a positive integer",
            code=INVALID_ISSUE_REQUEST,
            field="sequence",
        )
    stamp = parse_iso(issued_at_iso)
    number = f"INV-{stamp:%Y%m}-{sequence:05d}"
    if len(number) < MIN_INVOICE_NUMBER_LENGTH:
        raise DomainError(
            f"invoice number must be at least {MIN_INVOICE_NUMBER_LENGTH} characters",
            code=INVALID_ISSUE_REQUEST,
            invoice_number=number,
        )
    return number


def _quote_value(quote: Any, name: str) -> Any:
    if isinstance(quote, Mapping):
        if name not in quote:
            raise DomainError(
                f"quote is missing {name}", code=INVOICE_QUOTE_MISMATCH, field=name
            )
        return quote[name]
    if not hasattr(quote, name):
        raise DomainError(f"quote is missing {name}", code=INVOICE_QUOTE_MISMATCH, field=name)
    return getattr(quote, name)


def _quote_minor(quote: Any, name: str) -> int:
    value = _quote_value(quote, name)
    if isinstance(value, Money):
        return value.amount_minor
    if isinstance(value, Mapping) and "amount_minor" in value:
        return _as_int(value["amount_minor"], name)
    return _as_int(value, name)


def _quote_lines(quote: Any) -> Sequence[Any]:
    lines = _quote_value(quote, "lines")
    if isinstance(lines, (str, bytes)) or not isinstance(lines, Sequence):
        raise DomainError(
            "quote lines must be a sequence", code=INVOICE_QUOTE_MISMATCH, field="lines"
        )
    return lines


def lines_from_quote(quote: Any) -> tuple[LineSpec, ...]:
    """Derive invoice lines from a quote, cross-checked against the quote totals.

    The quote is the pricing authority; the invoice must not silently disagree
    with it. Every non-aggregate, non-refundable quote line becomes exactly one
    invoice line, and the derived totals must equal the quote's own subtotal,
    service fee, PBJT and total — otherwise ``INVOICE_QUOTE_MISMATCH``.
    """
    specs: list[LineSpec] = []
    for raw in _quote_lines(quote):
        if not isinstance(raw, Mapping):
            raise DomainError(
                "quote lines must be mappings", code=INVOICE_QUOTE_MISMATCH, field="lines"
            )
        code = require_ref(raw.get("code"), "code", max_len=64)
        if code in QUOTE_EXCLUDED_LINES:
            continue
        if code in QUOTE_LINE_KINDS:
            kind = QUOTE_LINE_KINDS[code]
        else:
            kind = next(
                (value for prefix, value in QUOTE_LINE_PREFIX_KINDS.items() if code.startswith(prefix)),
                None,
            )
            if kind is None:
                raise DomainError(
                    "quote line code has no invoice line kind",
                    code=INVOICE_QUOTE_MISMATCH,
                    code_value=code,
                )
        amount = _as_int(raw.get("amount_minor"), "amount_minor")
        quantity = raw.get("quantity", 1)
        if quantity in (None,):
            quantity = 1
        quantity = _as_int(quantity, "quantity")
        unit_amount = amount if quantity == 1 else amount // quantity
        if quantity == 0:
            unit_amount = amount
        description = require_text(
            raw.get("description") or code, "description", max_len=200
        )
        if len(description) < MIN_LINE_DESCRIPTION:
            description = f"quote line {code}"
        specs.append(
            LineSpec(
                kind=kind,
                description=description,
                quantity=quantity,
                unit_amount_minor=unit_amount,
                rate_card_id=raw.get("rate_card_id"),
                nights=raw.get("nights"),
            )
        )
    if not specs:
        raise DomainError(
            "quote produced no invoice lines", code=INVOICE_QUOTE_MISMATCH, field="lines"
        )
    derived = derive_totals(specs)
    expected = (
        _quote_minor(quote, "subtotal"),
        _quote_minor(quote, "service_fee"),
        _quote_minor(quote, "tax"),
        _quote_minor(quote, "total"),
    )
    # derive_totals returns (subtotal, tax, service, total); the quote exposes
    # (subtotal, service_fee, tax, total) — compare field by field, not positionally.
    if (derived[0], derived[2], derived[1], derived[3]) != expected:
        raise DomainError(
            "quote totals do not reconcile to the derived invoice lines",
            code=INVOICE_QUOTE_MISMATCH,
            derived={
                "subtotal_minor": derived[0],
                "service_minor": derived[2],
                "tax_minor": derived[1],
                "total_minor": derived[3],
            },
            quote={
                "subtotal_minor": expected[0],
                "service_minor": expected[1],
                "tax_minor": expected[2],
                "total_minor": expected[3],
            },
        )
    return tuple(specs)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class InvoiceLine:
    line_id: str
    invoice_id: str
    kind: str
    description: str
    quantity: int
    unit_amount_minor: int
    amount_minor: int
    rate_card_id: Optional[str]
    nights: Optional[int]
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "InvoiceLine":
        return cls(**{column: row[column] for column in LINE_COLUMNS})

    def money(self, currency: str) -> Money:
        return Money(self.amount_minor, currency)

    def as_row(self) -> dict[str, Any]:
        return {column: getattr(self, column) for column in LINE_COLUMNS}


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    invoice_number: Optional[str]
    booking_id: str
    policy_fingerprint: str
    currency: str
    subtotal_minor: int
    tax_minor: int
    service_minor: int
    total_minor: int
    amount_paid_minor: int
    status: str
    payment_status: str
    issued_at: str
    due_at: str
    voided_at: Optional[str]
    void_reason: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Invoice":
        return cls(**{column: row[column] for column in INVOICE_COLUMNS})

    def as_row(self) -> dict[str, Any]:
        return {column: getattr(self, column) for column in INVOICE_COLUMNS}

    # Money views — callers never subtract raw integers by hand.
    @property
    def subtotal(self) -> Money:
        return Money(self.subtotal_minor, self.currency)

    @property
    def tax(self) -> Money:
        return Money(self.tax_minor, self.currency)

    @property
    def service(self) -> Money:
        return Money(self.service_minor, self.currency)

    @property
    def total(self) -> Money:
        return Money(self.total_minor, self.currency)

    @property
    def amount_paid(self) -> Money:
        return Money(self.amount_paid_minor, self.currency)

    def outstanding(self) -> Money:
        """What is still owed; never negative (overpayment is held, not owed)."""
        return Money(max(0, self.total_minor - self.amount_paid_minor), self.currency)

    @property
    def is_void(self) -> bool:
        return self.status == "VOID"

    @property
    def is_open(self) -> bool:
        return self.status == "ISSUED"

    @property
    def is_settled(self) -> bool:
        return self.status == "ISSUED" and self.amount_paid_minor >= self.total_minor

    def is_overdue(self, when: Any) -> bool:
        """Strictly after the due instant, and still short of the total."""
        if self.status != "ISSUED" or self.amount_paid_minor >= self.total_minor:
            return False
        return self.due_at < _normalise_instant(when, field_name="when")

    def as_dict(self) -> dict[str, Any]:
        payload = self.as_row()
        payload.update(
            {
                "subtotal": self.subtotal.as_dict(),
                "tax": self.tax.as_dict(),
                "service": self.service.as_dict(),
                "total": self.total.as_dict(),
                "amount_paid": self.amount_paid.as_dict(),
                "outstanding": self.outstanding().as_dict(),
            }
        )
        return payload


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class InvoiceService:
    """Issue, read, void and settle invoices for confirmed bookings.

    Every write goes through :func:`_write_transaction` (IMMEDIATE, or joins the
    caller's transaction) and is audited in the same transaction through the
    shared :class:`audit.AuditLog`.
    """

    def __init__(
        self,
        conn: Any,
        clock: Optional[Clock] = None,
        ids: Optional[IdFactory] = None,
        *,
        actor: str = SYSTEM_ACTOR,
        actor_role: str = "system",
        enforce_triggers: bool = True,
    ) -> None:
        self._conn = conn
        self._clock = clock or SystemClock()
        self._ids = ids or IdFactory()
        self._actor = require_ref(actor, "actor")
        self._actor_role = require_ref(actor_role, "actor_role")
        self._audit = AuditLog(
            conn, clock=self._clock, ids=self._ids, enforce_triggers=enforce_triggers
        )

    # -- reads ------------------------------------------------------------- #
    def get(self, invoice_id: Any) -> Invoice:
        invoice_id = require_ref(invoice_id, "invoice_id", max_len=64)
        row = self._conn.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFound(
                f"invoice {invoice_id!r} does not exist",
                code=INVOICE_NOT_FOUND,
                invoice_id=invoice_id,
            )
        return Invoice.from_row(row)

    def lines(self, invoice_id: Any) -> tuple[InvoiceLine, ...]:
        invoice = self.get(invoice_id)
        rows = self._conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_id",
            (invoice.invoice_id,),
        ).fetchall()
        return tuple(InvoiceLine.from_row(row) for row in rows)

    def verify(self, invoice_id: Any) -> Invoice:
        """Re-read the stored lines and re-check them against the stored totals.

        Catches an invoice whose lines were edited out from under the service
        (the DDL cannot express "the parts sum to the whole" as a CHECK).
        """
        invoice = self.get(invoice_id)
        check_lines_against_totals(
            self.lines(invoice.invoice_id),
            subtotal_minor=invoice.subtotal_minor,
            tax_minor=invoice.tax_minor,
            service_minor=invoice.service_minor,
            total_minor=invoice.total_minor,
        )
        return invoice

    def for_booking(self, booking_id: Any) -> tuple[Invoice, ...]:
        booking_id = require_ref(booking_id, "booking_id", max_len=64)
        rows = self._conn.execute(
            "SELECT * FROM invoices WHERE booking_id = ? ORDER BY issued_at, invoice_id",
            (booking_id,),
        ).fetchall()
        return tuple(Invoice.from_row(row) for row in rows)

    def overdue(self, now: Optional[Any] = None) -> tuple[Invoice, ...]:
        """Issued invoices strictly past their due instant and short of the total."""
        cutoff = _normalise_instant(now if now is not None else self._clock.now(), field_name="now")
        rows = self._conn.execute(
            "SELECT * FROM invoices"
            " WHERE status = 'ISSUED' AND amount_paid_minor < total_minor AND due_at < ?"
            " ORDER BY due_at, invoice_id",
            (cutoff,),
        ).fetchall()
        return tuple(Invoice.from_row(row) for row in rows)

    def outstanding(self, invoice_id: Any) -> Money:
        return self.get(invoice_id).outstanding()

    # -- issue ------------------------------------------------------------- #
    def issue(
        self,
        *,
        booking_id: Any,
        currency: Any,
        policy_fingerprint: Any,
        lines: Sequence[Any],
        idempotency_key: Any,
        due_days: int = DEFAULT_DUE_DAYS,
        invoice_number: Optional[Any] = None,
        issued_at: Optional[Any] = None,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> Invoice:
        """Issue one invoice from already-priced lines.

        The caller brings amounts (a quote does the pricing); this method checks
        they are internally consistent, that the booking exists, and that the same
        key cannot issue two invoices, then writes invoice + lines + audit row in
        one transaction.
        """
        key = _require_key(idempotency_key)
        booking_id = require_ref(booking_id, "booking_id", max_len=64)
        currency_code = _require_currency(currency)
        fingerprint = _require_fingerprint(policy_fingerprint)
        issued_iso = _normalise_instant(
            issued_at if issued_at is not None else self._clock.now(), field_name="issued_at"
        )
        if isinstance(due_days, bool) or not isinstance(due_days, int) or due_days < 1:
            raise DomainError(
                "due_days must be a positive integer",
                code=INVALID_ISSUE_REQUEST,
                field="due_days",
            )
        due_iso = to_iso(parse_iso(issued_iso) + timedelta(days=due_days))
        if due_iso <= issued_iso:
            raise DomainError(
                "due_at must be after issued_at", code=INVALID_ISSUE_REQUEST, field="due_days"
            )
        specs = tuple(LineSpec.from_payload(raw) for raw in lines)
        if not specs:
            raise DomainError(
                "an invoice needs at least one line",
                code=INVALID_ISSUE_REQUEST,
                field="lines",
            )
        subtotal, tax, service, total = derive_totals(specs)
        if total <= 0:
            raise DomainError(
                "an invoice total must be positive",
                code=INVALID_ISSUE_REQUEST,
                total_minor=total,
            )
        pinned_number = (
            require_ref(invoice_number, "invoice_number", max_len=32)
            if invoice_number is not None
            else None
        )
        if pinned_number is not None and len(pinned_number) < MIN_INVOICE_NUMBER_LENGTH:
            raise DomainError(
                f"invoice_number must be at least {MIN_INVOICE_NUMBER_LENGTH} characters",
                code=INVALID_ISSUE_REQUEST,
                field="invoice_number",
            )
        request = {
            "booking_id": booking_id,
            "currency": currency_code,
            "policy_fingerprint": fingerprint,
            "due_days": due_days,
            "issued_at": issued_iso,
            "invoice_number": pinned_number,
            "lines": [spec.as_payload() for spec in specs],
        }
        request_hash = sha256_hex(ISSUE_OPERATION, canonical_json(request))

        with _write_transaction(self._conn):
            replay = self._idempotent_lookup(key, ISSUE_OPERATION, request_hash)
            if replay is not None:
                return self.get(replay)
            booking = self._conn.execute(
                "SELECT booking_id FROM bookings WHERE booking_id = ?", (booking_id,)
            ).fetchone()
            if booking is None:
                raise EntityNotFound(
                    f"booking {booking_id!r} does not exist",
                    code=BOOKING_NOT_FOUND,
                    booking_id=booking_id,
                )
            number = pinned_number or invoice_number_for(issued_iso, self._next_sequence(issued_iso))
            if self._number_taken(number):
                raise DomainError(
                    f"invoice_number {number!r} is already used",
                    code=DUPLICATE_INVOICE_NUMBER,
                    invoice_number=number,
                )
            invoice_id = self._ids.new("inv")
            self._conn.execute(
                "INSERT INTO invoices (invoice_id, invoice_number, booking_id, policy_fingerprint,"
                " currency, subtotal_minor, tax_minor, service_minor, total_minor, amount_paid_minor,"
                " status, payment_status, issued_at, due_at, voided_at, void_reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'ISSUED', 'UNPAID', ?, ?, NULL, NULL, ?)",
                (
                    invoice_id,
                    number,
                    booking_id,
                    fingerprint,
                    currency_code,
                    subtotal,
                    tax,
                    service,
                    total,
                    issued_iso,
                    due_iso,
                    issued_iso,
                ),
            )
            for spec in specs:
                self._conn.execute(
                    "INSERT INTO invoice_lines (line_id, invoice_id, kind, description, quantity,"
                    " unit_amount_minor, amount_minor, rate_card_id, nights, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._ids.new("line"),
                        invoice_id,
                        spec.kind,
                        spec.description,
                        spec.quantity,
                        spec.unit_amount_minor,
                        spec.amount_minor,
                        spec.rate_card_id,
                        spec.nights,
                        issued_iso,
                    ),
                )
            invoice = self.get(invoice_id)
            self._audit.append(
                action=AUDIT_INVOICE_ISSUED,
                entity_type="invoice",
                entity_id=invoice_id,
                actor=actor or self._actor,
                actor_role=actor_role or self._actor_role,
                after=invoice.as_dict(),
                reason=f"issued from {len(specs)} line(s) for booking {booking_id}",
            )
            self._idempotent_store(
                key, ISSUE_OPERATION, request_hash, invoice_id, invoice.as_row()
            )
        return self.get(invoice_id)

    def issue_from_quote(
        self,
        *,
        quote: Any,
        booking_id: Any,
        idempotency_key: Any,
        due_days: int = DEFAULT_DUE_DAYS,
        invoice_number: Optional[Any] = None,
        issued_at: Optional[Any] = None,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> Invoice:
        """Issue the invoice a quote describes, after re-checking the quote totals."""
        specs = lines_from_quote(quote)
        return self.issue(
            booking_id=booking_id,
            currency=_quote_value(quote, "currency"),
            policy_fingerprint=_quote_value(quote, "policy_fingerprint"),
            lines=specs,
            idempotency_key=idempotency_key,
            due_days=due_days,
            invoice_number=invoice_number,
            issued_at=issued_at,
            actor=actor,
            actor_role=actor_role,
        )

    # -- void -------------------------------------------------------------- #
    def void(
        self,
        *,
        invoice_id: Any,
        reason: Any,
        at: Optional[Any] = None,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> Invoice:
        """Void an unpaid invoice, keeping its number and recording why."""
        invoice = self.get(invoice_id)
        if invoice.status == "VOID":
            raise DomainError(
                f"invoice {invoice.invoice_id!r} is already void",
                code=INVOICE_ALREADY_VOID,
                invoice_id=invoice.invoice_id,
            )
        if invoice.amount_paid_minor != 0:
            raise DomainError(
                "an invoice with payments cannot be voided; refund or credit it instead",
                code=INVOICE_HAS_PAYMENTS,
                invoice_id=invoice.invoice_id,
                amount_paid_minor=invoice.amount_paid_minor,
            )
        if reason is None:
            raise DomainError(
                "void reason is required", code=INVALID_VOID_REASON, field="reason"
            )
        if not isinstance(reason, str):
            raise DomainError(
                "void reason must be a string", code=INVALID_VOID_REASON, field="reason"
            )
        # Measured on the normalised text so trailing whitespace cannot pad a
        # two-character reason into looking like a real one.
        reason_text = require_text(reason, "reason", max_len=500)
        if len(reason_text) < MIN_VOID_REASON:
            raise DomainError(
                f"void reason must be at least {MIN_VOID_REASON} characters",
                code=INVALID_VOID_REASON,
                field="reason",
            )
        voided_iso = _normalise_instant(
            at if at is not None else self._clock.now(), field_name="at"
        )
        with _write_transaction(self._conn):
            current = self.get(invoice.invoice_id)
            if current.status == "VOID":
                raise DomainError(
                    f"invoice {current.invoice_id!r} is already void",
                    code=INVOICE_ALREADY_VOID,
                    invoice_id=current.invoice_id,
                )
            if current.amount_paid_minor != 0:
                raise DomainError(
                    "an invoice with payments cannot be voided; refund or credit it instead",
                    code=INVOICE_HAS_PAYMENTS,
                    invoice_id=current.invoice_id,
                    amount_paid_minor=current.amount_paid_minor,
                )
            self._conn.execute(
                "UPDATE invoices SET status = 'VOID', voided_at = ?, void_reason = ?"
                " WHERE invoice_id = ?",
                (voided_iso, reason_text, current.invoice_id),
            )
            updated = self.get(current.invoice_id)
            self._audit.append(
                action=AUDIT_INVOICE_VOIDED,
                entity_type="invoice",
                entity_id=current.invoice_id,
                actor=actor or self._actor,
                actor_role=actor_role or self._actor_role,
                before=current.as_dict(),
                after=updated.as_dict(),
                reason=reason_text,
            )
        return self.get(updated.invoice_id)

    # -- settlement (called by payments, inside its transaction) ------------ #
    def record_settlement(
        self,
        invoice_id: Any,
        *,
        amount_paid_minor: int,
        payment_status: str,
        at: Optional[Any] = None,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> Invoice:
        """Set the *derived* settled amount and status of an invoice.

        Only ``payments.PaymentService`` should call this, and it does so inside the
        same transaction as the ledger entries, so an invoice can never claim money
        the ledger does not show. ``payment_status`` must agree with
        ``amount_paid_minor`` and the invoice total — except ``REFUNDED``, which is a
        fact about money moving *back* and is therefore allowed at any amount.
        """
        if isinstance(amount_paid_minor, bool) or not isinstance(amount_paid_minor, int):
            raise DomainError(
                "amount_paid_minor must be an integer (minor units)",
                code=INVALID_PAYMENT_STATUS,
                field="amount_paid_minor",
            )
        if amount_paid_minor < 0:
            raise DomainError(
                "amount_paid_minor must not be negative",
                code=INVALID_PAYMENT_STATUS,
                amount_paid_minor=amount_paid_minor,
            )
        invoice = self.get(invoice_id)
        if invoice.status == "VOID":
            raise DomainError(
                "a void invoice cannot be settled",
                code=INVOICE_ALREADY_VOID,
                invoice_id=invoice.invoice_id,
            )
        expected = _derive_payment_status(amount_paid_minor, invoice.total_minor)
        if payment_status not in PAYMENT_STATUSES:
            raise DomainError(
                "unknown payment status",
                code=INVALID_PAYMENT_STATUS,
                payment_status=payment_status,
            )
        if payment_status == "REFUNDED":
            if amount_paid_minor > invoice.total_minor:
                raise DomainError(
                    "a refund cannot leave the invoice overpaid",
                    code=INVALID_PAYMENT_STATUS,
                    amount_paid_minor=amount_paid_minor,
                    total_minor=invoice.total_minor,
                )
        elif payment_status != expected:
            raise DomainError(
                "payment_status does not match amount_paid_minor",
                code=INVALID_PAYMENT_STATUS,
                payment_status=payment_status,
                expected=expected,
                amount_paid_minor=amount_paid_minor,
                total_minor=invoice.total_minor,
            )
        settled_iso = _normalise_instant(
            at if at is not None else self._clock.now(), field_name="at"
        )
        with _write_transaction(self._conn):
            current = self.get(invoice.invoice_id)
            self._conn.execute(
                "UPDATE invoices SET amount_paid_minor = ?, payment_status = ?"
                " WHERE invoice_id = ?",
                (amount_paid_minor, payment_status, current.invoice_id),
            )
            self._sync_booking(current)
            updated = self.get(current.invoice_id)
            self._audit.append(
                action=AUDIT_INVOICE_SETTLED,
                entity_type="invoice",
                entity_id=current.invoice_id,
                actor=actor or self._actor,
                actor_role=actor_role or self._actor_role,
                before=current.as_dict(),
                after=updated.as_dict(),
                reason=reason or f"settlement recorded at {settled_iso}",
            )
        return self.get(updated.invoice_id)

    # -- internals --------------------------------------------------------- #
    def _sync_booking(self, invoice: Invoice) -> None:
        """Mirror the money onto the booking, capped at the booking's own total."""
        row = self._conn.execute(
            "SELECT total_minor FROM bookings WHERE booking_id = ?", (invoice.booking_id,)
        ).fetchone()
        if row is None:
            # The DDL forbids deleting a booking that has invoices; a missing row
            # here means the caller is driving an unsupported schema.
            return
        booking_total = row["total_minor"]
        paid = min(invoice.amount_paid_minor, booking_total)
        self._conn.execute(
            "UPDATE bookings SET amount_paid_minor = ?, payment_status = ?,"
            " updated_at = ?, version_lock = version_lock + 1 WHERE booking_id = ?",
            (
                paid,
                booking_payment_status(invoice.payment_status, paid),
                to_iso(self._clock.now()),
                invoice.booking_id,
            ),
        )

    def _number_taken(self, number: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM invoices WHERE invoice_number = ?", (number,)
        ).fetchone()
        return row is not None

    def _next_sequence(self, issued_iso: str) -> int:
        """Next per-month sequence, derived from the stored numbers themselves."""
        prefix = f"INV-{parse_iso(issued_iso):%Y%m}-"
        highest = 0
        for row in self._conn.execute(
            "SELECT invoice_number FROM invoices WHERE invoice_number LIKE ?", (prefix + "%",)
        ).fetchall():
            number = row["invoice_number"] or ""
            tail = number[len(prefix) :]
            if tail.isdigit():
                highest = max(highest, int(tail))
        return highest + 1

    def _idempotent_lookup(self, key: str, operation: str, request_hash: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT operation, request_hash, result_ref FROM idempotency_keys"
            " WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise DomainError(
                "idempotency_key was already used with a different request",
                code=IDEMPOTENCY_CONFLICT,
                idempotency_key=key,
                operation=row["operation"],
            )
        return row["result_ref"]

    def _idempotent_store(
        self, key: str, operation: str, request_hash: str, result_ref: str, result: Any
    ) -> None:
        self._conn.execute(
            "INSERT INTO idempotency_keys (idempotency_key, operation, request_hash, result_ref,"
            " result_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                key,
                operation,
                request_hash,
                result_ref,
                sha256_hex(operation, canonical_json(result)),
                to_iso(self._clock.now()),
            ),
        )


def _derive_payment_status(amount_paid_minor: int, total_minor: int) -> str:
    """The one place the paid amount becomes a status."""
    if amount_paid_minor <= 0:
        return "UNPAID"
    if amount_paid_minor < total_minor:
        return "PARTIAL"
    if amount_paid_minor == total_minor:
        return "PAID"
    return "OVERPAID"
