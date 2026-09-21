"""Money movement — hosted payment intents, verified webhooks, the double-entry
ledger and reconciliation of the payments that do not match.

Authority chain: ``src/schema/001_initial.sql`` owns the storage contract for
``payments``/``payment_events``/``ledger_entries``/``reconciliation_cases``/
``sla_breaches``; ``src/invoicing.py`` owns the invoice an intent is raised
against and is the *only* way this module moves ``amount_paid_minor``
(:meth:`invoicing.InvoiceService.record_settlement` is called inside our
transaction, never bypassed); this module owns everything between "a guest is
asked to pay" and "the invoice says it was paid".

Invariants this module enforces, all fail-closed (nothing is written before a
check passes, and every refusal is a :class:`primitives.DomainError` with a
stable ``code``):

1. An intent only exists for the exact outstanding amount of a live invoice, in
   that invoice's currency: ``INVOICE_ALREADY_VOID``, ``CURRENCY_MISMATCH`` and
   ``PAYMENT_AMOUNT_MISMATCH`` otherwise. A hosted URL carries opaque ids and a
   hash token — never guest data — and card data is refused everywhere
   (``CARD_DATA_FORBIDDEN``), so nothing card-shaped can be stored.
2. *Every* webhook is recorded, including the ones we refuse, because a
   rejected signature is evidence. The raw provider payload is never stored:
   only its sha256 plus a redacted, whitelisted summary. ``signature_verified``
   is decided with :func:`hmac.compare_digest` over ``HMAC-SHA256`` hex.
3. A webhook whose signature does not verify is recorded with
   ``signature_verified = 0``, ``applied = 0``, moves *no* money, and raises
   :class:`WebhookSignatureInvalid`. One ``(provider, provider_event_id)`` is
   processed once: a replay returns the stored row untouched.
4. Money only ever becomes a ledger batch that balances (total DEBIT == total
   CREDIT, ``LEDGER_UNBALANCED`` otherwise), on accounts the DDL allows, with a
   strictly increasing ``seq`` and an append-only hash chain
   (``prev_hash`` = previous row's ``entry_hash``; first row
   :data:`GENESIS_HASH`) that :func:`verify_ledger` recomputes end to end.
5. A successful event settles the payment (``SUCCEEDED`` + ``settled_at``),
   writes the balanced ledger batch and updates the invoice *only* through
   :meth:`invoicing.InvoiceService.record_settlement`.
6. Money that matches no invoice, or does not match the intent's amount, is
   never confirmed: it opens a ``reconciliation_cases`` row (``OPEN``,
   ``due_at = detected_at + 48h``) plus an ``UNMATCHED_PAYMENT``
   ``sla_breaches`` row, so the exception has an owner and a deadline.
7. A refund needs an approval that is *really* ``APPROVED`` — read from
   ``approval_requests`` and cross-checked against ``approval_decisions``; the
   argument is never trusted — and never exceeds what actually settled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from audit import GENESIS_HASH, AuditLog
from invoicing import Invoice, InvoiceService
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
    redact,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    # vocabulary
    "PAYMENT_STATUSES",
    "PAYMENT_METHODS",
    "PAYMENT_METHOD_HOSTED",
    "PAYMENT_METHOD_MANUAL",
    "LEDGER_ACCOUNTS",
    "LEDGER_DIRECTIONS",
    "RECONCILIATION_KINDS",
    "RECONCILIATION_STATUSES",
    "RECONCILIATION_RESOLUTIONS",
    "SLA_BREACH_KINDS",
    "SUCCESS_EVENT_TYPES",
    "FAILURE_EVENT_TYPES",
    "EXPIRY_EVENT_TYPES",
    "SUPPORTED_EVENT_TYPES",
    "SIGNATURE_ALGORITHM",
    "INTENT_OPERATION",
    "REFUND_OPERATION",
    "DEFAULT_EXPIRES_MINUTES",
    "MAX_EXPIRES_MINUTES",
    "MIN_IDEMPOTENCY_KEY",
    "MIN_NOTE_LENGTH",
    "MIN_PROVIDER_LENGTH",
    "MIN_PROVIDER_EVENT_ID",
    "MIN_REJECTION_REASON",
    "RECONCILIATION_SLA_HOURS",
    "APPEND_ONLY_TRIGGERS",
    "ERROR_CODES",
    # errors
    "PaymentError",
    "InvalidPaymentIntent",
    "InvalidIdempotencyKey",
    "PaymentIdempotencyConflict",
    "InvoiceNotPayable",
    "PaymentCurrencyMismatch",
    "PaymentAmountMismatch",
    "PaymentNotFound",
    "PaymentNotSettled",
    "CardDataForbidden",
    "InvalidWebhook",
    "WebhookProviderMismatch",
    "WebhookSignatureInvalid",
    "LedgerEntryInvalid",
    "LedgerUnbalanced",
    "LedgerChainBroken",
    "LedgerAppendOnlyUnenforced",
    "ReconciliationNotFound",
    "ReconciliationClosed",
    "ReconciliationInvoiceMissing",
    "ReconciliationAmountUnknown",
    "RefundNotApproved",
    "RefundExceedsPayment",
    # records & helpers
    "Payment",
    "PaymentIntent",
    "PaymentEvent",
    "ReconciliationCase",
    "LedgerEntry",
    "LedgerBalance",
    "LedgerVerification",
    "Refund",
    "PaymentService",
    "contains_card_data",
    "assert_no_card_data",
    "compute_entry_hash",
    "declared_facts",
    "guard_append_only",
    "invoice_exists",
    "redact_provider_payload",
    "post_journal",
    "verify_ledger",
]

# --------------------------------------------------------------------------- #
# Vocabulary (mirrors the CHECK constraints in src/schema/001_initial.sql)
# --------------------------------------------------------------------------- #
PAYMENT_STATUSES: tuple[str, ...] = (
    "PENDING",
    "SUCCEEDED",
    "FAILED",
    "EXPIRED",
    "REFUNDED",
)

PAYMENT_METHODS: tuple[str, ...] = ("HOSTED_PAGE", "BANK_TRANSFER", "CASH", "OTHER")

#: What a guest-facing intent uses, and what a human-verified match uses.
PAYMENT_METHOD_HOSTED = "HOSTED_PAGE"
PAYMENT_METHOD_MANUAL = "BANK_TRANSFER"

LEDGER_ACCOUNTS: tuple[str, ...] = (
    "CASH_IN",
    "TAX_PAYABLE",
    "SERVICE_FEE",
    "GUEST_DEPOSIT",
    "REFUND_OUT",
    "OWNER_PAYABLE",
    "PSP_FEE",
)

LEDGER_DIRECTIONS: tuple[str, ...] = ("DEBIT", "CREDIT")

RECONCILIATION_KINDS: tuple[str, ...] = (
    "UNMATCHED_PAYMENT",
    "AMOUNT_MISMATCH",
    "DUPLICATE_EVENT",
    "REFUND_WITHOUT_PAYMENT",
    "MISSING_SETTLEMENT",
    "UNVERIFIED_WEBHOOK",
)

RECONCILIATION_STATUSES: tuple[str, ...] = ("OPEN", "RESOLVED", "WAIVED")

#: How a human closes a case: MATCHED posts the money, the others do not.
RECONCILIATION_RESOLUTIONS: tuple[str, ...] = ("MATCHED", "UNMATCHED", "WAIVED")

SLA_BREACH_KINDS: tuple[str, ...] = (
    "FIRST_RESPONSE",
    "AVAILABILITY_ANSWER",
    "QUOTE",
    "COMPLAINT_FIRST_RESPONSE",
    "COMPLAINT_RESOLUTION",
    "UNMATCHED_PAYMENT",
    "APPROVAL_QUEUE",
)

#: The breach this module raises: unmatched money is an exception with a deadline.
UNMATCHED_PAYMENT_BREACH = "UNMATCHED_PAYMENT"

SUCCESS_EVENT_TYPES: tuple[str, ...] = ("payment.succeeded",)
FAILURE_EVENT_TYPES: tuple[str, ...] = ("payment.failed",)
EXPIRY_EVENT_TYPES: tuple[str, ...] = ("payment.expired",)
PENDING_EVENT_TYPES: tuple[str, ...] = ("payment.pending",)
SUPPORTED_EVENT_TYPES: tuple[str, ...] = (
    SUCCESS_EVENT_TYPES + FAILURE_EVENT_TYPES + EXPIRY_EVENT_TYPES + PENDING_EVENT_TYPES
)

SIGNATURE_ALGORITHM = "HMAC-SHA256"

INTENT_OPERATION = "payment.intent"
REFUND_OPERATION = "payment.refund"

DEFAULT_EXPIRES_MINUTES = 30
MAX_EXPIRES_MINUTES = 24 * 60

MIN_IDEMPOTENCY_KEY = 8
MIN_NOTE_LENGTH = 3
MIN_PROVIDER_LENGTH = 3
MIN_PROVIDER_EVENT_ID = 6
MIN_REJECTION_REASON = 3

#: How long an unmatched payment may stay open before it is a breach.
RECONCILIATION_SLA_HOURS = 48

#: The DDL keeps both money trails append-only with triggers; a database whose
#: guards were dropped must not be written through.
APPEND_ONLY_TRIGGERS: tuple[str, ...] = (
    "trg_payment_events_no_update",
    "trg_payment_events_no_delete",
    "trg_ledger_entries_no_update",
    "trg_ledger_entries_no_delete",
)

# Stable failure codes (tests and callers branch on these, never on messages).
INVALID_INTENT_REQUEST = "INVALID_PAYMENT_INTENT"
INVALID_IDEMPOTENCY_KEY = "INVALID_IDEMPOTENCY_KEY"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
INVOICE_ALREADY_VOID = "INVOICE_ALREADY_VOID"
CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
AMOUNT_MISMATCH = "PAYMENT_AMOUNT_MISMATCH"
PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
PAYMENT_NOT_SETTLED = "PAYMENT_NOT_SETTLED"
CARD_DATA_FORBIDDEN = "CARD_DATA_FORBIDDEN"
INVALID_WEBHOOK = "INVALID_WEBHOOK"
PROVIDER_MISMATCH = "PAYMENT_PROVIDER_MISMATCH"
WEBHOOK_SIGNATURE_INVALID = "WEBHOOK_SIGNATURE_INVALID"
LEDGER_INVALID_ENTRY = "LEDGER_INVALID_ENTRY"
LEDGER_UNBALANCED = "LEDGER_UNBALANCED"
LEDGER_CHAIN_BROKEN = "LEDGER_CHAIN_BROKEN"
LEDGER_APPEND_ONLY_UNENFORCED = "LEDGER_APPEND_ONLY_UNENFORCED"
RECON_NOT_FOUND = "RECONCILIATION_NOT_FOUND"
RECON_ALREADY_RESOLVED = "RECONCILIATION_CLOSED"
RECON_NOTE_REQUIRED = "RECONCILIATION_NOTE_REQUIRED"
RECON_INVOICE_MISSING = "RECONCILIATION_INVOICE_MISSING"
RECON_AMOUNT_UNKNOWN = "RECONCILIATION_AMOUNT_UNKNOWN"
INVALID_RESOLUTION = "INVALID_RECONCILIATION_RESOLUTION"
REFUND_APPROVAL_REQUIRED = "REFUND_APPROVAL_REQUIRED"
REFUND_EXCEEDS_PAYMENT = "REFUND_EXCEEDS_PAYMENT"
INVALID_REFUND = "INVALID_REFUND"

ERROR_CODES: tuple[str, ...] = (
    INVALID_INTENT_REQUEST,
    INVALID_IDEMPOTENCY_KEY,
    IDEMPOTENCY_CONFLICT,
    INVOICE_ALREADY_VOID,
    CURRENCY_MISMATCH,
    AMOUNT_MISMATCH,
    PAYMENT_NOT_FOUND,
    PAYMENT_NOT_SETTLED,
    CARD_DATA_FORBIDDEN,
    INVALID_WEBHOOK,
    PROVIDER_MISMATCH,
    WEBHOOK_SIGNATURE_INVALID,
    LEDGER_INVALID_ENTRY,
    LEDGER_UNBALANCED,
    LEDGER_CHAIN_BROKEN,
    LEDGER_APPEND_ONLY_UNENFORCED,
    RECON_NOT_FOUND,
    RECON_ALREADY_RESOLVED,
    RECON_NOTE_REQUIRED,
    RECON_INVOICE_MISSING,
    RECON_AMOUNT_UNKNOWN,
    INVALID_RESOLUTION,
    REFUND_APPROVAL_REQUIRED,
    REFUND_EXCEEDS_PAYMENT,
    INVALID_REFUND,
)

# Audit actions this module owns.
AUDIT_INTENT_CREATED = "payment.intent_created"
AUDIT_INTENT_EXPIRED = "payment.intent_expired"
AUDIT_EVENT_RECORDED = "payment.webhook_recorded"
AUDIT_EVENT_REJECTED = "payment.webhook_rejected"
AUDIT_PAYMENT_SETTLED = "payment.settled"
AUDIT_RECONCILIATION_OPENED = "payment.reconciliation_opened"
AUDIT_RECONCILIATION_RESOLVED = "payment.reconciliation_resolved"
AUDIT_REFUND_RECORDED = "payment.refunded"

# Storage column order — one tuple so the INSERT, the row mapping and the DDL
# cannot drift apart.
PAYMENT_COLUMNS: tuple[str, ...] = (
    "payment_id",
    "invoice_id",
    "provider",
    "provider_intent_id",
    "method",
    "currency",
    "amount_minor",
    "status",
    "created_at",
    "expires_at",
    "settled_at",
    "provider_status",
)

EVENT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "provider",
    "provider_event_id",
    "payment_id",
    "event_type",
    "signature_verified",
    "applied",
    "rejection_reason",
    "amount_minor",
    "currency",
    "payload_sha256",
    "payload_redacted",
    "received_at",
    "created_at",
)

LEDGER_COLUMNS: tuple[str, ...] = (
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
    "entry_hash",
    "prev_hash",
    "created_at",
)

#: The columns the hash covers — hash, chain link and row metadata excluded
#: (``entry_hash`` cannot cover itself, and ``created_at``/``prev_hash`` are
#: recorded next to it).
LEDGER_HASH_FIELDS: tuple[str, ...] = (
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

LEDGER_HASH_DOMAIN = "ledger_entry:v1"
PAYLOAD_HASH_DOMAIN = "payment_event:v1"

RECON_COLUMNS: tuple[str, ...] = (
    "recon_id",
    "kind",
    "payment_event_id",
    "payment_id",
    "invoice_id",
    "detected_at",
    "due_at",
    "status",
    "owner_actor",
    "resolution_note",
    "resolved_at",
    "details",
    "created_at",
)

#: Fields a provider payload is allowed to contribute — everything else is
#: dropped before anything is written.
DECLARED_INTENT_KEYS: tuple[str, ...] = ("provider_intent_id", "intent_id", "payment_intent")
DECLARED_AMOUNT_KEYS: tuple[str, ...] = ("amount_minor",)
DECLARED_CURRENCY_KEYS: tuple[str, ...] = ("currency",)
DECLARED_INVOICE_KEYS: tuple[str, ...] = ("invoice_id",)
DECLARED_STATUS_KEYS: tuple[str, ...] = ("status",)

#: Shapes that are refused before storage: a card number, a CVC, a card field.
CARD_FIELD_HINTS: frozenset[str] = frozenset(
    {
        "cardnumber",
        "cardno",
        "cardnum",
        "pan",
        "primaryaccountnumber",
        "cvc",
        "cvv",
        "cvc2",
        "cvv2",
        "cardcvc",
        "cardcvv",
        "cardexpiry",
        "expirymonth",
        "expiryyear",
    }
)

_CARD_CANDIDATE = re.compile(r"\d(?:[ -]?\d){12,18}")
_REF_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,79}$")


# --------------------------------------------------------------------------- #
# Compatibility shim
# --------------------------------------------------------------------------- #
def _ensure_money_serialiser_alias() -> None:
    """Alias ``Money.as_dict`` to ``Money.to_dict`` when it is missing.

    The invoice service is the only writer of ``invoices.amount_paid_minor``, so
    every settlement here goes through
    :meth:`invoicing.InvoiceService.record_settlement`. That method projects the
    invoice with ``Invoice.as_dict()``, which calls ``Money.as_dict()`` — while
    ``primitives.Money`` names that serialiser ``to_dict()``. Without the alias
    the invoice write raises ``AttributeError`` and the whole settlement rolls
    back. Same function, same output, no behaviour change; the alias is removed
    the moment ``src/primitives.py`` grows it.
    """
    if not hasattr(Money, "as_dict"):
        Money.as_dict = Money.to_dict  # type: ignore[attr-defined]


_ensure_money_serialiser_alias()


# --------------------------------------------------------------------------- #
# Errors — every one carries a stable ``code`` (never parse the message)
# --------------------------------------------------------------------------- #
class PaymentError(DomainError):
    """Base class for every deliberate failure in the payments module."""

    code = "PAYMENT_ERROR"


class InvalidPaymentIntent(PaymentError):
    code = INVALID_INTENT_REQUEST


class InvalidIdempotencyKey(PaymentError):
    code = INVALID_IDEMPOTENCY_KEY


class PaymentIdempotencyConflict(PaymentError):
    code = IDEMPOTENCY_CONFLICT


class InvoiceNotPayable(PaymentError):
    code = INVOICE_ALREADY_VOID


class PaymentCurrencyMismatch(PaymentError):
    code = CURRENCY_MISMATCH


class PaymentAmountMismatch(PaymentError):
    code = AMOUNT_MISMATCH


class PaymentNotFound(PaymentError):
    code = PAYMENT_NOT_FOUND


class PaymentNotSettled(PaymentError):
    code = PAYMENT_NOT_SETTLED


class CardDataForbidden(PaymentError):
    code = CARD_DATA_FORBIDDEN


class InvalidWebhook(PaymentError):
    code = INVALID_WEBHOOK


class WebhookProviderMismatch(PaymentError):
    code = PROVIDER_MISMATCH


class WebhookSignatureInvalid(PaymentError):
    code = WEBHOOK_SIGNATURE_INVALID


class LedgerEntryInvalid(PaymentError):
    code = LEDGER_INVALID_ENTRY


class LedgerUnbalanced(PaymentError):
    code = LEDGER_UNBALANCED


class LedgerChainBroken(PaymentError):
    code = LEDGER_CHAIN_BROKEN


class LedgerAppendOnlyUnenforced(PaymentError):
    code = LEDGER_APPEND_ONLY_UNENFORCED


class ReconciliationNotFound(PaymentError):
    code = RECON_NOT_FOUND


class ReconciliationClosed(PaymentError):
    code = RECON_ALREADY_RESOLVED


class ReconciliationInvoiceMissing(PaymentError):
    code = RECON_INVOICE_MISSING


class ReconciliationAmountUnknown(PaymentError):
    code = RECON_AMOUNT_UNKNOWN


class InvalidResolution(PaymentError):
    code = INVALID_RESOLUTION


class RefundNotApproved(PaymentError):
    code = REFUND_APPROVAL_REQUIRED


class RefundExceedsPayment(PaymentError):
    code = REFUND_EXCEEDS_PAYMENT


# --------------------------------------------------------------------------- #
# Small shared helpers (the repo keeps one private copy per service module)
# --------------------------------------------------------------------------- #
@contextmanager
def _write_transaction(conn: Any) -> Iterator[None]:
    """Run the body in an IMMEDIATE transaction, or join the caller's.

    IMMEDIATE takes the write lock before the ledger tail is read, so two
    writers cannot both read the same head and mint the same ``seq``. When the
    caller already opened a transaction we neither nest a BEGIN nor commit
    their work.
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
    if isinstance(value, str):
        try:
            return to_iso(parse_iso(value))
        except DomainError as exc:
            raise DomainError(
                f"{field_name} is not a valid ISO-8601 instant",
                code="INVALID_TIMESTAMP",
                field=field_name,
            ) from exc
    raise DomainError(
        f"{field_name} must be a datetime or an ISO-8601 string",
        code="INVALID_TIMESTAMP",
        field=field_name,
        got=type(value).__name__,
    )


def _require_key(value: Any) -> str:
    """Idempotency keys are caller-supplied and must be long enough to be unique."""
    text = require_text(value, "idempotency_key", max_len=128)
    if len(text) < MIN_IDEMPOTENCY_KEY:
        raise InvalidIdempotencyKey(
            f"idempotency_key must be at least {MIN_IDEMPOTENCY_KEY} characters",
            idempotency_key=text,
            length=len(text),
        )
    return text


def _require_currency(value: Any) -> str:
    """Validate through :class:`primitives.Money` so casing matches the rest."""
    try:
        return Money(0, value).currency
    except DomainError as exc:
        raise DomainError(
            f"unsupported currency: {value!r}", code=INVALID_INTENT_REQUEST, field="currency"
        ) from exc


def _require_minor(value: Any, field_name: str, *, positive: bool = False) -> int:
    """Money is integer minor units; a bool or a float is a caller bug."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError(
            f"{field_name} must be an integer in minor units",
            code=INVALID_INTENT_REQUEST,
            field=field_name,
            got=type(value).__name__,
        )
    if positive and value <= 0:
        raise DomainError(
            f"{field_name} must be positive",
            code=INVALID_INTENT_REQUEST,
            field=field_name,
            value=value,
        )
    if value < 0:
        raise DomainError(
            f"{field_name} must not be negative",
            code=INVALID_INTENT_REQUEST,
            field=field_name,
            value=value,
        )
    return value


def _require_expires_minutes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPaymentIntent(
            "expires_minutes must be a positive int",
            expires_minutes=repr(value),
        )
    if value <= 0 or value > MAX_EXPIRES_MINUTES:
        raise InvalidPaymentIntent(
            f"expires_minutes must be between 1 and {MAX_EXPIRES_MINUTES}",
            expires_minutes=value,
        )
    return value


def _settlement_status(amount_paid_minor: int, total_minor: int) -> str:
    """The one place a paid amount becomes an invoice ``payment_status``.

    Mirrors ``invoicing._derive_payment_status`` exactly; the invoice service
    re-checks the pair and refuses a status that disagrees with the amount.
    """
    if amount_paid_minor <= 0:
        return "UNPAID"
    if amount_paid_minor < total_minor:
        return "PARTIAL"
    if amount_paid_minor == total_minor:
        return "PAID"
    return "OVERPAID"


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum — the cheap way to tell a PAN from an amount."""
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


def contains_card_data(value: Any) -> bool:
    """True when a string looks like a card number (13-19 digits, Luhn-valid)."""
    if not isinstance(value, str):
        return False
    for match in _CARD_CANDIDATE.finditer(value):
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


def assert_no_card_data(values: Mapping[str, Any]) -> None:
    """Refuse any mapping that looks like, or names, card data.

    Card data must never reach this system: it is not needed to take a hosted
    payment, and storing it would make the database a cardholder environment.
    """
    for key, value in values.items():
        normalised = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalised in CARD_FIELD_HINTS:
            raise CardDataForbidden(
                "card data is never accepted or stored",
                field=str(key),
            )
        if contains_card_data(value):
            raise CardDataForbidden(
                "value looks like a card number and is never stored",
                field=str(key),
            )


def redact_provider_payload(
    *,
    provider: str,
    provider_event_id: str,
    event_type: str,
    declared: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build the redacted summary kept next to a webhook's hash.

    Only whitelisted, already-validated facts are rendered; the raw payload is
    never a candidate, so a provider that sends a guest's name, email or phone
    number cannot get it stored.
    """
    facts: dict[str, Any] = dict(declared or {})
    parts = [
        f"provider={provider}",
        f"provider_event_id={provider_event_id}",
        f"event_type={event_type}",
    ]
    for label, key in (
        ("intent", "provider_intent_id"),
        ("invoice", "invoice_id"),
        ("amount_minor", "amount_minor"),
        ("currency", "currency"),
        ("status", "status"),
    ):
        value = facts.get(key)
        if value is not None:
            parts.append(f"{label}={value}")
    return redact("; ".join(parts))[:500]


# --------------------------------------------------------------------------- #
# Payload parsing (whitelist only; nothing raw is ever returned)
# --------------------------------------------------------------------------- #
def _parse_payload(payload_raw: str) -> Optional[Mapping[str, Any]]:
    """Parse a provider payload into a mapping, or ``None`` when unusable."""
    try:
        data = json.loads(payload_raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    return data


def _first(payload: Mapping[str, Any], keys: Sequence[str]) -> Optional[Any]:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _declared_ref(value: Any) -> Optional[str]:
    """A provider-supplied reference, kept only when it looks like an opaque id."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _REF_TOKEN.match(text) else None


def declared_facts(payload: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """The only facts a webhook may contribute, each type-checked."""
    if not payload:
        return {}
    facts: dict[str, Any] = {}
    intent = _declared_ref(_first(payload, DECLARED_INTENT_KEYS))
    if intent:
        facts["provider_intent_id"] = intent
    invoice = _declared_ref(_first(payload, DECLARED_INVOICE_KEYS))
    if invoice:
        facts["invoice_id"] = invoice
    amount = _first(payload, DECLARED_AMOUNT_KEYS)
    if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
        facts["amount_minor"] = amount
    currency = _first(payload, DECLARED_CURRENCY_KEYS)
    if isinstance(currency, str) and len(currency) == 3 and currency.isalpha():
        facts["currency"] = currency.upper()
    status = _declared_ref(_first(payload, DECLARED_STATUS_KEYS))
    if status:
        facts["status"] = status
    return facts


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Payment:
    """One payment intent / settlement as stored in ``payments``."""

    payment_id: str
    invoice_id: str
    provider: str
    provider_intent_id: str
    method: str
    currency: str
    amount_minor: int
    status: str
    created_at: str
    expires_at: Optional[str]
    settled_at: Optional[str]
    provider_status: Optional[str]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Payment":
        return cls(**{column: row[column] for column in PAYMENT_COLUMNS})

    def as_row(self) -> dict[str, Any]:
        return {column: getattr(self, column) for column in PAYMENT_COLUMNS}

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)

    @property
    def is_pending(self) -> bool:
        return self.status == "PENDING"

    @property
    def is_settled(self) -> bool:
        return self.status in ("SUCCEEDED", "REFUNDED")

    def is_expired_at(self, when: str) -> bool:
        """True when the intent window has passed (strictly after ``expires_at``)."""
        return self.expires_at is not None and self.expires_at <= when


@dataclass(frozen=True)
class PaymentIntent:
    """What a caller gets back from :meth:`PaymentService.create_intent`.

    ``hosted_url`` points at the provider's page and carries opaque ids plus a
    hash token — never a guest name, email, phone or card detail.
    """

    payment_id: str
    invoice_id: str
    provider: str
    provider_intent_id: str
    status: str
    hosted_url: str
    amount_minor: int
    currency: str
    created_at: str
    expires_at: str
    idempotent_replay: bool = False

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)

    def as_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "invoice_id": self.invoice_id,
            "provider": self.provider,
            "provider_intent_id": self.provider_intent_id,
            "status": self.status,
            "hosted_url": self.hosted_url,
            "amount": self.amount.to_dict(),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "idempotent_replay": self.idempotent_replay,
        }


@dataclass(frozen=True)
class PaymentEvent:
    """One stored webhook, accepted or refused (``payment_events``)."""

    event_id: str
    provider: str
    provider_event_id: str
    event_type: str
    signature_verified: int
    applied: int
    rejection_reason: Optional[str]
    payload_sha256: str
    payload_redacted: Optional[str]
    received_at: str
    created_at: str
    payment_id: Optional[str] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    duplicate: bool = False
    recon_id: Optional[str] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any], **overrides: Any) -> "PaymentEvent":
        values = {column: row[column] for column in EVENT_COLUMNS}
        values.update(overrides)
        return cls(**values)

    @property
    def verified(self) -> bool:
        return bool(self.signature_verified)

    @property
    def was_applied(self) -> bool:
        return bool(self.applied)


@dataclass(frozen=True)
class ReconciliationCase:
    """One exception row (``reconciliation_cases``) with its deadline."""

    recon_id: str
    kind: str
    payment_event_id: Optional[str]
    payment_id: Optional[str]
    invoice_id: Optional[str]
    detected_at: str
    due_at: str
    status: str
    owner_actor: Optional[str]
    resolution_note: Optional[str]
    resolved_at: Optional[str]
    details: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ReconciliationCase":
        return cls(**{column: row[column] for column in RECON_COLUMNS})

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    @property
    def details_dict(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.details)
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def is_overdue_at(self, when: str) -> bool:
        """Open and past its due instant — the case has no owner yet."""
        return self.is_open and self.due_at <= when


@dataclass(frozen=True)
class LedgerEntry:
    """One append-only ledger row, chained to its predecessor by hash."""

    entry_id: str
    seq: int
    account: str
    direction: str
    amount_minor: int
    currency: str
    booking_id: Optional[str]
    invoice_id: Optional[str]
    payment_id: Optional[str]
    occurred_at: str
    memo: str
    entry_hash: str
    prev_hash: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "LedgerEntry":
        return cls(**{column: row[column] for column in LEDGER_COLUMNS})

    def as_row(self) -> dict[str, Any]:
        return {column: getattr(self, column) for column in LEDGER_COLUMNS}

    def is_genesis(self) -> bool:
        return self.prev_hash == GENESIS_HASH


@dataclass(frozen=True)
class LedgerBalance:
    """Per-account totals of one currency; ``balanced`` is never assumed."""

    currency: str
    debit_total_minor: int
    credit_total_minor: int
    by_account: Mapping[str, dict[str, int]]
    entry_count: int

    @property
    def balanced(self) -> bool:
        return self.debit_total_minor == self.credit_total_minor

    def as_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "debit_total_minor": self.debit_total_minor,
            "credit_total_minor": self.credit_total_minor,
            "balanced": self.balanced,
            "entry_count": self.entry_count,
            "by_account": dict(self.by_account),
        }


@dataclass(frozen=True)
class LedgerVerification:
    """Result of replaying the ledger's hash chain and its balance."""

    ok: bool
    checked: int
    head_hash: str
    first_broken_seq: Optional[int] = None
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "head_hash": self.head_hash,
            "first_broken_seq": self.first_broken_seq,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Refund:
    """A refund that actually left the account, with its approval reference."""

    payment_id: str
    invoice_id: str
    amount_minor: int
    currency: str
    approval_id: str
    actor: str
    reason: str
    occurred_at: str
    refunded_total_minor: int
    fully_refunded: bool
    entries: tuple[str, ...]

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)


# --------------------------------------------------------------------------- #
# The ledger itself
# --------------------------------------------------------------------------- #
def compute_entry_hash(prev_hash: str, body: Mapping[str, Any]) -> str:
    """Hash one ledger row onto its predecessor — the link in the chain."""
    payload = {name: body.get(name) for name in LEDGER_HASH_FIELDS}
    return sha256_hex(LEDGER_HASH_DOMAIN, prev_hash, canonical_json(payload))


def _prepare_entry(raw: Any) -> dict[str, Any]:
    """Validate one journal line; an unknown account or direction is refused."""
    if not isinstance(raw, Mapping):
        raise LedgerEntryInvalid("each ledger entry must be a mapping", got=type(raw).__name__)
    account = require_ref(raw.get("account"), "account", max_len=32)
    if account not in LEDGER_ACCOUNTS:
        raise LedgerEntryInvalid(
            "unknown ledger account", account=account, allowed=list(LEDGER_ACCOUNTS)
        )
    direction = require_ref(raw.get("direction"), "direction", max_len=8)
    if direction not in LEDGER_DIRECTIONS:
        raise LedgerEntryInvalid("unknown ledger direction", direction=direction)
    amount_minor = raw.get("amount_minor")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise LedgerEntryInvalid(
            "a ledger amount must be a positive integer in minor units",
            account=account,
            amount_minor=repr(amount_minor),
        )
    try:
        currency = Money(0, raw.get("currency")).currency
    except (DomainError, TypeError) as exc:
        raise LedgerEntryInvalid("unknown ledger currency", currency=repr(raw.get("currency"))) from exc
    memo = require_text(raw.get("memo"), "memo", max_len=200)
    if len(memo) < MIN_NOTE_LENGTH:
        raise LedgerEntryInvalid("a ledger memo must be at least 3 characters", memo=memo)
    booking_id = raw.get("booking_id")
    invoice_id = raw.get("invoice_id")
    payment_id = raw.get("payment_id")
    return {
        "account": account,
        "direction": direction,
        "amount_minor": amount_minor,
        "currency": currency,
        "booking_id": require_ref(booking_id, "booking_id", max_len=64) if booking_id else None,
        "invoice_id": require_ref(invoice_id, "invoice_id", max_len=64) if invoice_id else None,
        "payment_id": require_ref(payment_id, "payment_id", max_len=64) if payment_id else None,
        "memo": memo,
    }


def post_journal(
    conn: Any,
    *,
    entries: Iterable[Any],
    occurred_at: Any,
    ids: Optional[IdFactory] = None,
) -> tuple[LedgerEntry, ...]:
    """Append one balanced journal batch and return the stored rows.

    The batch is refused (``LEDGER_UNBALANCED``) unless total DEBIT equals total
    CREDIT in a single currency, so a batch that would move money out of
    nowhere cannot be written. ``seq`` continues from the current tail and every
    row is chained to the previous row's ``entry_hash``.
    """
    occurred_iso = _normalise_instant(occurred_at, field_name="occurred_at")
    prepared = tuple(_prepare_entry(entry) for entry in entries)
    if not prepared:
        raise LedgerEntryInvalid("a journal batch needs at least one entry")
    currency = prepared[0]["currency"]
    if any(entry["currency"] != currency for entry in prepared):
        raise LedgerEntryInvalid(
            "one journal batch must use a single currency",
            currencies=sorted({entry["currency"] for entry in prepared}),
        )
    debits = sum(entry["amount_minor"] for entry in prepared if entry["direction"] == "DEBIT")
    credits = sum(entry["amount_minor"] for entry in prepared if entry["direction"] == "CREDIT")
    if debits != credits:
        raise LedgerUnbalanced(
            "a journal batch must balance: total DEBIT must equal total CREDIT",
            debits_minor=debits,
            credits_minor=credits,
            currency=currency,
        )
    factory = ids if ids is not None else IdFactory()
    written: list[LedgerEntry] = []
    with _write_transaction(conn):
        tail = conn.execute(
            "SELECT seq, entry_hash FROM ledger_entries ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = 1 if tail is None else int(tail["seq"]) + 1
        prev_hash = GENESIS_HASH if tail is None else str(tail["entry_hash"])
        for entry in prepared:
            entry_id = factory.new("entry")
            body = {"entry_id": entry_id, "seq": seq, "occurred_at": occurred_iso, **entry}
            digest = compute_entry_hash(prev_hash, body)
            conn.execute(
                f"INSERT INTO ledger_entries ({', '.join(LEDGER_COLUMNS)})"
                f" VALUES ({', '.join('?' * len(LEDGER_COLUMNS))})",
                (
                    entry_id,
                    seq,
                    entry["account"],
                    entry["direction"],
                    entry["amount_minor"],
                    entry["currency"],
                    entry["booking_id"],
                    entry["invoice_id"],
                    entry["payment_id"],
                    occurred_iso,
                    entry["memo"],
                    digest,
                    prev_hash,
                    occurred_iso,
                ),
            )
            written.append(
                LedgerEntry(
                    entry_id=entry_id,
                    seq=seq,
                    account=entry["account"],
                    direction=entry["direction"],
                    amount_minor=entry["amount_minor"],
                    currency=entry["currency"],
                    booking_id=entry["booking_id"],
                    invoice_id=entry["invoice_id"],
                    payment_id=entry["payment_id"],
                    occurred_at=occurred_iso,
                    memo=entry["memo"],
                    entry_hash=digest,
                    prev_hash=prev_hash,
                    created_at=occurred_iso,
                )
            )
            prev_hash, seq = digest, seq + 1
    return tuple(written)


def verify_ledger(conn: Any, *, raise_on_break: bool = False) -> LedgerVerification:
    """Replay the ledger chain and re-check that it balances.

    Recomputes every ``entry_hash`` from the stored values, checks that each row
    links to its predecessor (the first to :data:`GENESIS_HASH`) and that the
    sequence has no gap, then checks DEBIT == CREDIT per currency. Returns a
    :class:`LedgerVerification`; with ``raise_on_break=True`` a broken chain
    raises :class:`LedgerChainBroken` instead.
    """
    rows = conn.execute("SELECT * FROM ledger_entries ORDER BY seq").fetchall()
    prev_hash = GENESIS_HASH
    expected_seq = 1
    checked = 0
    broken_seq: Optional[int] = None
    reason: Optional[str] = None
    totals: dict[str, list[int]] = {}
    for row in rows:
        entry = LedgerEntry.from_row(row)
        if entry.seq != expected_seq:
            broken_seq, reason = entry.seq, (
                f"sequence gap: expected {expected_seq}, found {entry.seq}"
            )
        elif entry.prev_hash != prev_hash:
            broken_seq, reason = entry.seq, "prev_hash does not match the previous row's hash"
        else:
            body = {name: getattr(entry, name) for name in LEDGER_HASH_FIELDS}
            if compute_entry_hash(prev_hash, body) != entry.entry_hash:
                broken_seq, reason = entry.seq, "entry_hash does not match the stored row"
        if broken_seq is not None:
            break
        bucket = totals.setdefault(entry.currency, [0, 0])
        bucket[0 if entry.direction == "DEBIT" else 1] += entry.amount_minor
        prev_hash = entry.entry_hash
        expected_seq += 1
        checked += 1

    if reason is None:
        for currency, (debits, credits) in sorted(totals.items()):
            if debits != credits:
                reason = (
                    f"unbalanced ledger for {currency}: debits {debits} != credits {credits}"
                )
                break

    report = LedgerVerification(
        ok=reason is None,
        checked=checked,
        head_hash=prev_hash,
        first_broken_seq=broken_seq,
        reason=reason,
    )
    if not report.ok and raise_on_break:
        raise LedgerChainBroken(
            "the ledger chain does not verify",
            first_broken_seq=broken_seq,
            reason=reason,
            checked=checked,
        )
    return report


def guard_append_only(conn: Any) -> tuple[str, ...]:
    """Fail closed unless the money trails and their triggers exist.

    A payments table without its immutability triggers produces rows that look
    auditable and are not; refusing to start is the cheaper failure.
    """
    triggers = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = [name for name in APPEND_ONLY_TRIGGERS if name not in triggers]
    for table in ("payments", "payment_events", "ledger_entries", "reconciliation_cases"):
        if table not in tables:
            missing.append(f"table:{table}")
    if missing:
        raise LedgerAppendOnlyUnenforced(
            "append-only guards are missing; the money trail cannot be trusted",
            missing=tuple(missing),
        )
    return APPEND_ONLY_TRIGGERS


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class PaymentService:
    """Hosted payment intents, verified webhooks, the ledger and reconciliation.

    Every write goes through :func:`_write_transaction` (IMMEDIATE, or joins the
    caller's transaction) and is audited in the same transaction through the
    shared :class:`audit.AuditLog`. The invoice is only ever moved through
    :class:`invoicing.InvoiceService`, inside our transaction.
    """

    def __init__(
        self,
        conn: Any,
        clock: Optional[Clock] = None,
        ids: Optional[IdFactory] = None,
        audit: Optional[AuditLog] = None,
        provider: str = "dummy",
        *,
        invoices: Optional[InvoiceService] = None,
        actor: str = SYSTEM_ACTOR,
        actor_role: str = "system",
        enforce_triggers: bool = True,
    ) -> None:
        self._conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ids: IdFactory = ids if ids is not None else IdFactory()
        provider = require_ref(provider, "provider", max_len=32)
        if len(provider) < MIN_PROVIDER_LENGTH:
            raise DomainError(
                f"provider must be at least {MIN_PROVIDER_LENGTH} characters",
                code=INVALID_WEBHOOK,
                provider=provider,
            )
        self._provider = provider
        self._actor = require_ref(actor, "actor", max_len=64)
        self._actor_role = require_ref(actor_role, "actor_role", max_len=64)
        if enforce_triggers:
            guard_append_only(conn)
        self._audit = (
            audit
            if audit is not None
            else AuditLog(conn, clock=self._clock, ids=self._ids, enforce_triggers=enforce_triggers)
        )
        self._invoices = invoices

    # -- internals --------------------------------------------------------- #
    @property
    def provider(self) -> str:
        """The provider this service speaks for."""
        return self._provider

    def _now_iso(self) -> str:
        return to_iso(self._clock.now())

    def _invoicing(self) -> InvoiceService:
        if self._invoices is None:
            self._invoices = InvoiceService(
                self._conn, self._clock, self._ids, actor=self._actor, actor_role=self._actor_role
            )
        return self._invoices

    def _hosted_url(self, payment_id: str, provider_intent_id: str) -> str:
        """A hosted-page URL built from opaque ids and a hash token, never PII."""
        token = sha256_hex("hosted_url:v1", self._provider, payment_id, provider_intent_id)[:32]
        return (
            f"https://pay.{self._provider}.example/hosted/{provider_intent_id}"
            f"?payment={payment_id}&token={token}"
        )

    def _payment_row(self, payment_id: str) -> Any:
        return self._conn.execute(
            "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
        ).fetchone()

    def _idempotent_lookup(self, key: str, operation: str, request_hash: str) -> Optional[str]:
        """Return the stored result ref, or raise when the key was reused."""
        row = self._conn.execute(
            "SELECT operation, request_hash, result_ref FROM idempotency_keys"
            " WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if str(row["operation"]) != operation or str(row["request_hash"]) != request_hash:
            raise PaymentIdempotencyConflict(
                "idempotency_key was reused for a different request",
                idempotency_key=key,
                operation=operation,
                stored_operation=str(row["operation"]),
            )
        return None if row["result_ref"] is None else str(row["result_ref"])

    def _idempotent_store(
        self, key: str, operation: str, request_hash: str, result_ref: str, result: Any
    ) -> None:
        self._conn.execute(
            "INSERT INTO idempotency_keys (idempotency_key, operation, request_hash,"
            " result_ref, result_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                key,
                operation,
                request_hash,
                result_ref,
                sha256_hex(canonical_json(result)),
                self._now_iso(),
            ),
        )

    def _intent_of(self, payment_id: str, *, replayed: bool) -> PaymentIntent:
        row = self._payment_row(payment_id)
        if row is None:
            raise PaymentNotFound(
                f"payment {payment_id!r} does not exist", payment_id=payment_id
            )
        payment = Payment.from_row(row)
        url = self._hosted_url(payment.payment_id, payment.provider_intent_id)
        return PaymentIntent(
            payment_id=payment.payment_id,
            invoice_id=payment.invoice_id,
            provider=payment.provider,
            provider_intent_id=payment.provider_intent_id,
            status=payment.status,
            hosted_url=url if payment.status == "PENDING" else "",
            amount_minor=payment.amount_minor,
            currency=payment.currency,
            created_at=payment.created_at,
            expires_at=payment.expires_at or payment.created_at,
            idempotent_replay=replayed,
        )

    # -- intents ----------------------------------------------------------- #
    def create_intent(
        self,
        *,
        invoice_id: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        expires_minutes: int = DEFAULT_EXPIRES_MINUTES,
        created_by: str,
    ) -> PaymentIntent:
        """Raise a hosted payment intent for exactly what an invoice still owes.

        Refusals (nothing is written, everything is checked first): the key must
        be at least :data:`MIN_IDEMPOTENCY_KEY` characters, the invoice must
        exist and be ``ISSUED`` (``INVOICE_ALREADY_VOID`` otherwise), the
        currency must be the invoice's (``CURRENCY_MISMATCH``) and the amount
        must be its outstanding total (``PAYMENT_AMOUNT_MISMATCH``). Replaying
        the key returns the original intent; reusing it for a different request
        is ``IDEMPOTENCY_CONFLICT``. No card data is ever accepted or stored.
        """
        invoice_id = require_ref(invoice_id, "invoice_id", max_len=64)
        key = _require_key(idempotency_key)
        currency_code = _require_currency(currency)
        amount = _require_minor(amount_minor, "amount_minor", positive=True)
        created_by = require_ref(created_by, "created_by", max_len=64)
        minutes = _require_expires_minutes(expires_minutes)
        assert_no_card_data({"created_by": created_by})

        invoice = self._invoicing().get(invoice_id)
        if invoice.status == "VOID":
            raise InvoiceNotPayable(
                "an invoice that was voided cannot be paid",
                invoice_id=invoice.invoice_id,
                voided_at=invoice.voided_at,
            )
        if invoice.currency != currency_code:
            raise PaymentCurrencyMismatch(
                "the payment currency must be the invoice currency",
                invoice_id=invoice.invoice_id,
                invoice_currency=invoice.currency,
                currency=currency_code,
            )
        outstanding = invoice.outstanding().minor_units
        if amount != outstanding:
            raise PaymentAmountMismatch(
                "a hosted intent must be raised for the invoice's outstanding amount",
                invoice_id=invoice.invoice_id,
                amount_minor=amount,
                outstanding_minor=outstanding,
            )

        now_iso = self._now_iso()
        expires_at = to_iso(parse_iso(now_iso) + timedelta(minutes=minutes))
        request = {
            "invoice_id": invoice.invoice_id,
            "amount_minor": amount,
            "currency": currency_code,
            "expires_minutes": minutes,
            "created_by": created_by,
        }
        request_hash = sha256_hex(INTENT_OPERATION, canonical_json(request))

        with _write_transaction(self._conn):
            replayed = self._idempotent_lookup(key, INTENT_OPERATION, request_hash)
            if replayed is not None:
                return self._intent_of(replayed, replayed=True)
            payment_id = self._ids.new("payment")
            provider_intent_id = self._ids.new("pi")
            self._conn.execute(
                f"INSERT INTO payments ({', '.join(PAYMENT_COLUMNS)})"
                f" VALUES ({', '.join('?' * len(PAYMENT_COLUMNS))})",
                (
                    payment_id,
                    invoice.invoice_id,
                    self._provider,
                    provider_intent_id,
                    PAYMENT_METHOD_HOSTED,
                    currency_code,
                    amount,
                    "PENDING",
                    now_iso,
                    expires_at,
                    None,
                    "intent_created",
                ),
            )
            payment = Payment.from_row(self._payment_row(payment_id))
            self._audit.append(
                action=AUDIT_INTENT_CREATED,
                entity_type="payment",
                entity_id=payment_id,
                actor=created_by,
                actor_role="agent",
                after=payment.as_row(),
                reason=(
                    f"hosted intent for invoice {invoice.invoice_id} "
                    f"expiring {expires_at}"
                ),
            )
            self._idempotent_store(key, INTENT_OPERATION, request_hash, payment_id, payment.as_row())
        return self._intent_of(payment_id, replayed=False)

    # -- webhooks ---------------------------------------------------------- #
    def handle_webhook(
        self,
        *,
        provider: str,
        event_id: str,
        event_type: str,
        payload_raw: str,
        signature: str,
        secret: str,
        received_at: Optional[Any] = None,
    ) -> PaymentEvent:
        """Record and (when the signature holds) apply one provider webhook.

        The signature is ``HMAC-SHA256`` hex over ``payload_raw`` compared with
        :func:`hmac.compare_digest`. A signature that does not verify is still
        *recorded* — ``signature_verified = 0``, ``applied = 0``, a rejection
        reason, no money moved — and then ``WebhookSignatureInvalid`` is raised,
        so a forged call is evidence rather than silence. The same
        ``(provider, provider_event_id)`` is processed once: a replay returns the
        stored row. Store nothing raw: only the payload's sha256 and a redacted
        summary reach the database.
        """
        provider = require_ref(provider, "provider", max_len=32)
        if len(provider) < MIN_PROVIDER_LENGTH:
            raise InvalidWebhook(
                f"provider must be at least {MIN_PROVIDER_LENGTH} characters", provider=provider
            )
        if provider != self._provider:
            raise WebhookProviderMismatch(
                "this service only accepts webhooks for its own provider",
                provider=provider,
                expected=self._provider,
            )
        provider_event_id = require_ref(event_id, "event_id", max_len=64)
        if len(provider_event_id) < MIN_PROVIDER_EVENT_ID:
            raise InvalidWebhook(
                f"event_id must be at least {MIN_PROVIDER_EVENT_ID} characters",
                event_id=provider_event_id,
            )
        event_type = require_ref(event_type, "event_type", max_len=64)
        if len(event_type) < MIN_REJECTION_REASON:
            raise InvalidWebhook("event_type is too short to be meaningful", event_type=event_type)
        if isinstance(payload_raw, bytes):
            payload_raw = payload_raw.decode("utf-8", errors="strict")
        if not isinstance(payload_raw, str) or not payload_raw:
            raise InvalidWebhook(
                "payload_raw must be the exact bytes the provider signed, as a string",
                got=type(payload_raw).__name__,
            )
        secret = require_text(secret, "secret", max_len=256)
        when_iso = _normalise_instant(
            received_at if received_at is not None else self._clock.now(),
            field_name="received_at",
        )
        if not isinstance(signature, str):
            raise InvalidWebhook(
                "signature must be the provider's hex digest",
                got=type(signature).__name__,
            )
        verified = self._signature_valid(secret, payload_raw, signature)
        payload_sha = sha256_hex(PAYLOAD_HASH_DOMAIN, provider, provider_event_id, payload_raw)
        declared = declared_facts(_parse_payload(payload_raw))
        summary = redact_provider_payload(
            provider=provider,
            provider_event_id=provider_event_id,
            event_type=event_type,
            declared=declared,
        )
        card_refusal = False
        try:
            assert_no_card_data({"payload_raw": payload_raw})
        except CardDataForbidden:
            card_refusal = True

        with _write_transaction(self._conn):
            existing = self._conn.execute(
                "SELECT * FROM payment_events WHERE provider = ? AND provider_event_id = ?",
                (provider, provider_event_id),
            ).fetchone()
            if existing is not None:
                return PaymentEvent.from_row(existing, duplicate=True)
            failure: Optional[PaymentError] = None
            if not verified:
                self._record_event(
                    provider=provider,
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                    payment_id=None,
                    declared=None,
                    signature_verified=0,
                    applied=0,
                    rejection_reason=f"{WEBHOOK_SIGNATURE_INVALID}: signature did not match",
                    payload_sha=payload_sha,
                    summary=summary,
                    received_at=when_iso,
                    actor=self._actor,
                    actor_role=self._actor_role,
                    audit_action=AUDIT_EVENT_REJECTED,
                )
                outcome = self._event_of(
                    provider_event_id, provider=provider, recon_id=None
                )
                failure = WebhookSignatureInvalid(
                    "the signature did not verify: the event was recorded and no money moved",
                    provider=provider,
                    provider_event_id=provider_event_id,
                )
            elif card_refusal:
                event = self._record_event(
                    provider=provider,
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                    payment_id=None,
                    declared=None,
                    signature_verified=1,
                    applied=0,
                    rejection_reason="card data in the payload is never stored",
                    payload_sha=payload_sha,
                    summary=summary,
                    received_at=when_iso,
                    actor=self._actor,
                    actor_role=self._actor_role,
                    audit_action=AUDIT_EVENT_REJECTED,
                )
                recon_id = self._open_reconciliation(
                    kind="UNVERIFIED_WEBHOOK",
                    event=event,
                    payment=None,
                    invoice_id=None,
                    declared={},
                    detected_at=when_iso,
                    actor=self._actor,
                    actor_role=self._actor_role,
                    reason="a payload containing card data was refused and nothing was stored",
                )
                outcome = self._event_of(
                    provider_event_id, provider=provider, recon_id=recon_id
                )
                failure = CardDataForbidden(
                    "card data in a webhook payload is never accepted or stored",
                    provider_event_id=provider_event_id,
                )
            else:
                outcome = self._apply_verified_event(
                    provider=provider,
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                    declared=declared,
                    payload_sha=payload_sha,
                    summary=summary,
                    received_at=when_iso,
                )
        if failure is not None:
            raise failure
        return outcome

    def _event_of(
        self, provider_event_id: str, *, provider: str, recon_id: Optional[str] = None
    ) -> PaymentEvent:
        row = self._conn.execute(
            "SELECT * FROM payment_events WHERE provider = ? AND provider_event_id = ?",
            (provider, provider_event_id),
        ).fetchone()
        if row is None:  # pragma: no cover - the row was just written
            raise InvalidWebhook(
                "the recorded event vanished", provider_event_id=provider_event_id
            )
        return PaymentEvent.from_row(row, recon_id=recon_id)

    def _signature_valid(self, secret: str, payload_raw: str, signature: str) -> bool:
        """Constant-time HMAC-SHA256 check over the exact signed payload."""
        expected = hmac.new(
            secret.encode("utf-8"), payload_raw.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        try:
            return hmac.compare_digest(expected, signature.strip().lower())
        except TypeError:  # non-ASCII "signature" cannot be a hex digest
            return False

    def _record_event(
        self,
        *,
        provider: str,
        provider_event_id: str,
        event_type: str,
        payment_id: Optional[str],
        declared: Optional[Mapping[str, Any]],
        signature_verified: int,
        applied: int,
        rejection_reason: Optional[str],
        payload_sha: str,
        summary: str,
        received_at: str,
        actor: str,
        actor_role: str,
        audit_action: str,
        audit_reason: Optional[str] = None,
    ) -> PaymentEvent:
        """Insert one ``payment_events`` row (the raw payload never travels)."""
        if applied == 0 and (rejection_reason is None or len(rejection_reason) < MIN_REJECTION_REASON):
            raise InvalidWebhook(
                "a refused event must say why: applied = 0 needs a rejection reason",
                provider_event_id=provider_event_id,
            )
        facts = dict(declared or {})
        event_id = self._ids.new("pevent")
        amount_minor = facts.get("amount_minor")
        currency = facts.get("currency")
        self._conn.execute(
            f"INSERT INTO payment_events ({', '.join(EVENT_COLUMNS)})"
            f" VALUES ({', '.join('?' * len(EVENT_COLUMNS))})",
            (
                event_id,
                provider,
                provider_event_id,
                payment_id,
                event_type,
                int(signature_verified),
                int(applied),
                rejection_reason,
                amount_minor,
                currency,
                payload_sha,
                summary,
                received_at,
                received_at,
            ),
        )
        event = PaymentEvent.from_row(
            self._conn.execute(
                "SELECT * FROM payment_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        )
        self._audit.append(
            action=audit_action,
            entity_type="payment_event",
            entity_id=event_id,
            actor=actor,
            actor_role=actor_role,
            after={
                "provider": provider,
                "provider_event_id": provider_event_id,
                "event_type": event_type,
                "payment_id": payment_id,
                "signature_verified": int(signature_verified),
                "applied": int(applied),
                "payload_sha256": payload_sha,
            },
            reason=audit_reason or rejection_reason or f"{event_type} verified and applied",
        )
        return event

    def _apply_verified_event(
        self,
        *,
        provider: str,
        provider_event_id: str,
        event_type: str,
        declared: Mapping[str, Any],
        payload_sha: str,
        summary: str,
        received_at: str,
    ) -> PaymentEvent:
        """Apply a signature-verified event; never confirms money it cannot match."""
        actor = f"psp:{provider}"
        row = None
        intent_id = declared.get("provider_intent_id")
        if intent_id is not None:
            row = self._conn.execute(
                "SELECT * FROM payments WHERE provider = ? AND provider_intent_id = ?",
                (provider, intent_id),
            ).fetchone()

        if row is None:
            invoice_id = self._existing_invoice(declared.get("invoice_id"))
            event = self._record_event(
                provider=provider,
                provider_event_id=provider_event_id,
                event_type=event_type,
                payment_id=None,
                declared=declared,
                signature_verified=1,
                applied=0,
                rejection_reason="no payment intent matches this event; nothing was confirmed",
                payload_sha=payload_sha,
                summary=summary,
                received_at=received_at,
                actor=actor,
                actor_role="payment_provider",
                audit_action=AUDIT_EVENT_REJECTED,
                audit_reason="unmatched payment event opened for reconciliation",
            )
            recon_id = self._open_reconciliation(
                kind="UNMATCHED_PAYMENT",
                event=event,
                payment=None,
                invoice_id=invoice_id,
                declared=declared,
                detected_at=received_at,
                actor=actor,
                actor_role="payment_provider",
                reason="incoming money matched no payment intent",
            )
            return PaymentEvent.from_row(
                self._conn.execute(
                    "SELECT * FROM payment_events WHERE event_id = ?", (event.event_id,)
                ).fetchone(),
                recon_id=recon_id,
            )

        payment = Payment.from_row(row)
        if event_type not in SUPPORTED_EVENT_TYPES:
            return self._record_event(
                provider=provider,
                provider_event_id=provider_event_id,
                event_type=event_type,
                payment_id=payment.payment_id,
                declared=declared,
                signature_verified=1,
                applied=0,
                rejection_reason=f"unsupported event type {event_type}",
                payload_sha=payload_sha,
                summary=summary,
                received_at=received_at,
                actor=actor,
                actor_role="payment_provider",
                audit_action=AUDIT_EVENT_REJECTED,
            )

        if event_type in FAILURE_EVENT_TYPES or event_type in EXPIRY_EVENT_TYPES:
            status = "FAILED" if event_type in FAILURE_EVENT_TYPES else "EXPIRED"
            self._conn.execute(
                "UPDATE payments SET status = ?, provider_status = ? WHERE payment_id = ?",
                (status, event_type, payment.payment_id),
            )
            return self._record_event(
                provider=provider,
                provider_event_id=provider_event_id,
                event_type=event_type,
                payment_id=payment.payment_id,
                declared=declared,
                signature_verified=1,
                applied=1,
                rejection_reason=None,
                payload_sha=payload_sha,
                summary=summary,
                received_at=received_at,
                actor=actor,
                actor_role="payment_provider",
                audit_action=AUDIT_EVENT_RECORDED,
            )

        if event_type in PENDING_EVENT_TYPES:
            self._conn.execute(
                "UPDATE payments SET provider_status = ? WHERE payment_id = ?",
                (event_type, payment.payment_id),
            )
            return self._record_event(
                provider=provider,
                provider_event_id=provider_event_id,
                event_type=event_type,
                payment_id=payment.payment_id,
                declared=declared,
                signature_verified=1,
                applied=1,
                rejection_reason=None,
                payload_sha=payload_sha,
                summary=summary,
                received_at=received_at,
                actor=actor,
                actor_role="payment_provider",
                audit_action=AUDIT_EVENT_RECORDED,
            )

        # A success event: the only path that moves money.
        problems = self._settlement_problems(payment, declared)
        if problems is not None:
            kind, reason = problems
            event = self._record_event(
                provider=provider,
                provider_event_id=provider_event_id,
                event_type=event_type,
                payment_id=payment.payment_id,
                declared=declared,
                signature_verified=1,
                applied=0,
                rejection_reason=reason,
                payload_sha=payload_sha,
                summary=summary,
                received_at=received_at,
                actor=actor,
                actor_role="payment_provider",
                audit_action=AUDIT_EVENT_REJECTED,
            )
            recon_id = self._open_reconciliation(
                kind=kind,
                event=event,
                payment=payment,
                invoice_id=payment.invoice_id if invoice_exists(
                    self._conn, payment.invoice_id
                ) else None,
                declared=declared,
                detected_at=received_at,
                actor=actor,
                actor_role="payment_provider",
                reason=reason,
            )
            return PaymentEvent.from_row(
                self._conn.execute(
                    "SELECT * FROM payment_events WHERE event_id = ?", (event.event_id,)
                ).fetchone(),
                recon_id=recon_id,
            )

        invoice = self._invoicing().get(payment.invoice_id)
        entries = self._post_settlement(payment, invoice, occurred_at=received_at)
        self._conn.execute(
            "UPDATE payments SET status = 'SUCCEEDED', settled_at = ?, provider_status = ?"
            " WHERE payment_id = ?",
            (received_at, event_type, payment.payment_id),
        )
        settled = Payment.from_row(self._payment_row(payment.payment_id))
        self._invoicing().record_settlement(
            invoice.invoice_id,
            amount_paid_minor=invoice.amount_paid_minor + payment.amount_minor,
            payment_status=_settlement_status(
                invoice.amount_paid_minor + payment.amount_minor, invoice.total_minor
            ),
            reason=f"{event_type} from {provider} settled payment {payment.payment_id}",
            actor=actor,
            actor_role="payment_provider",
        )
        self._audit.append(
            action=AUDIT_PAYMENT_SETTLED,
            entity_type="payment",
            entity_id=payment.payment_id,
            actor=actor,
            actor_role="payment_provider",
            before=payment.as_row(),
            after=settled.as_row(),
            reason=(
                f"settled {payment.amount_minor} {payment.currency} minor units;"
                f" ledger entries {len(entries)}"
            ),
        )
        event = self._record_event(
            provider=provider,
            provider_event_id=provider_event_id,
            event_type=event_type,
            payment_id=payment.payment_id,
            declared=declared,
            signature_verified=1,
            applied=1,
            rejection_reason=None,
            payload_sha=payload_sha,
            summary=summary,
            received_at=received_at,
            actor=actor,
            actor_role="payment_provider",
            audit_action=AUDIT_EVENT_RECORDED,
        )
        return event

    def _settlement_problems(
        self, payment: Payment, declared: Mapping[str, Any]
    ) -> Optional[tuple[str, str]]:
        """Why this success event must not confirm money, as (kind, reason)."""
        if payment.status in ("SUCCEEDED", "REFUNDED"):
            return (
                "DUPLICATE_EVENT",
                "the payment is already settled; this event must not settle it twice",
            )
        if payment.status in ("EXPIRED", "FAILED"):
            return (
                "UNMATCHED_PAYMENT",
                f"the intent is {payment.status}; money against a dead intent needs a human",
            )
        amount = declared.get("amount_minor")
        if amount is not None and amount != payment.amount_minor:
            return (
                "AMOUNT_MISMATCH",
                f"received {amount} minor units but the intent is {payment.amount_minor}",
            )
        currency = declared.get("currency")
        if currency is not None and currency != payment.currency:
            return (
                "AMOUNT_MISMATCH",
                f"received {currency} but the intent is {payment.currency}",
            )
        invoice_row = self._conn.execute(
            "SELECT status, total_minor, amount_paid_minor FROM invoices WHERE invoice_id = ?",
            (payment.invoice_id,),
        ).fetchone()
        if invoice_row is None or str(invoice_row["status"]) != "ISSUED":
            return (
                "UNMATCHED_PAYMENT",
                "the invoice this payment points at is not payable",
            )
        outstanding = max(
            0, int(invoice_row["total_minor"]) - int(invoice_row["amount_paid_minor"])
        )
        if payment.amount_minor > outstanding:
            return (
                "AMOUNT_MISMATCH",
                f"the invoice only owes {outstanding} minor units; confirming"
                f" {payment.amount_minor} would settle the same debt twice",
            )
        return None

    def _post_settlement(
        self, payment: Payment, invoice: Invoice, *, occurred_at: str
    ) -> tuple[LedgerEntry, ...]:
        """Debit cash and credit the invoice's tax, service and owner shares."""
        amount = payment.amount_minor
        tax = min(invoice.tax_minor, amount)
        remaining = amount - tax
        service = min(invoice.service_minor, remaining)
        owner = remaining - service
        entries: list[dict[str, Any]] = [
            {
                "account": "CASH_IN",
                "direction": "DEBIT",
                "amount_minor": amount,
                "currency": payment.currency,
                "booking_id": invoice.booking_id,
                "invoice_id": invoice.invoice_id,
                "payment_id": payment.payment_id,
                "memo": f"hosted payment {payment.payment_id} settled invoice {invoice.invoice_id}",
            }
        ]
        for account, share in (("TAX_PAYABLE", tax), ("SERVICE_FEE", service), ("OWNER_PAYABLE", owner)):
            if share <= 0:
                continue
            entries.append(
                {
                    "account": account,
                    "direction": "CREDIT",
                    "amount_minor": share,
                    "currency": payment.currency,
                    "booking_id": invoice.booking_id,
                    "invoice_id": invoice.invoice_id,
                    "payment_id": payment.payment_id,
                    "memo": f"{account.lower()} share of invoice {invoice.invoice_id}",
                }
            )
        return post_journal(
            self._conn, entries=entries, occurred_at=occurred_at, ids=self._ids
        )

    def _existing_invoice(self, candidate: Any) -> Optional[str]:
        """A declared invoice reference, kept only when the invoice really exists."""
        if candidate is None:
            return None
        row = self._conn.execute(
            "SELECT invoice_id FROM invoices WHERE invoice_id = ?", (candidate,)
        ).fetchone()
        return None if row is None else str(row["invoice_id"])

    # -- reconciliation ---------------------------------------------------- #
    def _open_reconciliation(
        self,
        *,
        kind: str,
        event: PaymentEvent,
        payment: Optional[Payment],
        invoice_id: Optional[str],
        declared: Mapping[str, Any],
        detected_at: str,
        actor: str,
        actor_role: str,
        reason: str,
    ) -> str:
        """Open an exception with a 48h deadline, an owner and an SLA breach."""
        if kind not in RECONCILIATION_KINDS:
            raise DomainError("unknown reconciliation kind", code=INVALID_RESOLUTION, kind=kind)
        recon_id = self._ids.new("recon")
        due_at = to_iso(parse_iso(detected_at) + timedelta(hours=RECONCILIATION_SLA_HOURS))
        details = canonical_json(
            {
                key: value
                for key, value in {
                    "declared_amount_minor": declared.get("amount_minor"),
                    "declared_currency": declared.get("currency"),
                    "declared_invoice_id": declared.get("invoice_id"),
                    "provider_event_id": event.provider_event_id,
                    "event_type": event.event_type,
                    "reason": reason,
                }.items()
                if value is not None
            }
        )
        self._conn.execute(
            f"INSERT INTO reconciliation_cases ({', '.join(RECON_COLUMNS)})"
            f" VALUES ({', '.join('?' * len(RECON_COLUMNS))})",
            (
                recon_id,
                kind,
                event.event_id,
                payment.payment_id if payment is not None else None,
                invoice_id,
                detected_at,
                due_at,
                "OPEN",
                actor,
                None,
                None,
                details,
                detected_at,
            ),
        )
        self._record_breach(
            event=event,
            payment=payment,
            detected_at=detected_at,
            owner_actor=actor,
            reason=reason,
        )
        self._audit.append(
            action=AUDIT_RECONCILIATION_OPENED,
            entity_type="reconciliation_case",
            entity_id=recon_id,
            actor=actor,
            actor_role=actor_role,
            after={
                "recon_id": recon_id,
                "kind": kind,
                "payment_id": payment.payment_id if payment is not None else None,
                "invoice_id": invoice_id,
                "due_at": due_at,
            },
            reason=reason,
        )
        return recon_id

    def _record_breach(
        self,
        *,
        event: PaymentEvent,
        payment: Optional[Payment],
        detected_at: str,
        owner_actor: str,
        reason: str,
    ) -> Optional[str]:
        """Raise the UNMATCHED_PAYMENT breach for a payment that has no owner.

        The breach ``due_at`` is the payment's own settlement deadline when the
        money arrived after it (the deadline really was missed); otherwise the
        exception is escalated at the instant it is seen, so ``breach_minutes``
        is 0. The DDL requires ``detected_at >= due_at``: a breach row is a fact
        about a deadline that has passed, never a reminder.
        """
        subject_id = payment.payment_id if payment is not None else event.event_id
        existing = self._conn.execute(
            "SELECT breach_id FROM sla_breaches WHERE subject_type = 'payment'"
            " AND subject_id = ? AND kind = ?",
            (subject_id, UNMATCHED_PAYMENT_BREACH),
        ).fetchone()
        if existing is not None:
            return str(existing["breach_id"])
        deadline = payment.expires_at if payment is not None else None
        due_at = deadline if deadline is not None and deadline <= detected_at else detected_at
        minutes = int(
            (parse_iso(detected_at) - parse_iso(due_at)).total_seconds() // 60
        )
        breach_id = self._ids.new("breach")
        self._conn.execute(
            "INSERT INTO sla_breaches (breach_id, subject_type, subject_id, case_id, kind,"
            " due_at, detected_at, breach_minutes, owner_actor, status, resolution_note,"
            " resolved_at, created_at) VALUES (?, 'payment', ?, NULL, ?, ?, ?, ?, ?, 'OPEN',"
            " NULL, NULL, ?)",
            (
                breach_id,
                subject_id,
                UNMATCHED_PAYMENT_BREACH,
                due_at,
                detected_at,
                max(0, minutes),
                owner_actor,
                detected_at,
            ),
        )
        return breach_id

    def _close_breaches(self, subject_id: str, *, note: str, at: str) -> None:
        self._conn.execute(
            "UPDATE sla_breaches SET status = 'RESOLVED', resolution_note = ?, resolved_at = ?"
            " WHERE subject_type = 'payment' AND subject_id = ? AND status = 'OPEN'",
            (note, at, subject_id),
        )

    def resolve_reconciliation(
        self,
        *,
        recon_id: str,
        resolution: str,
        note: str,
        actor: str,
        at: Optional[Any] = None,
    ) -> ReconciliationCase:
        """Close one reconciliation case, posting money only for ``MATCHED``.

        ``note`` must be at least :data:`MIN_NOTE_LENGTH` characters (a closed
        exception without a note cannot be reviewed) and an already closed case
        is refused. ``MATCHED`` requires an invoice that really exists — the
        case must point at one (``RECONCILIATION_INVOICE_MISSING`` otherwise) and
        the money received is then settled onto it through the invoice service.
        Any other resolution moves no money.
        """
        recon_id = require_ref(recon_id, "recon_id", max_len=64)
        at_iso = _normalise_instant(at if at is not None else self._clock.now(), field_name="at")
        actor = require_ref(actor, "actor", max_len=64)
        if resolution not in RECONCILIATION_RESOLUTIONS:
            raise InvalidResolution(
                "unknown reconciliation resolution",
                resolution=resolution,
                allowed=list(RECONCILIATION_RESOLUTIONS),
            )
        note = require_text(note, "note", max_len=1000)
        if len(note) < MIN_NOTE_LENGTH:
            raise DomainError(
                f"note must be at least {MIN_NOTE_LENGTH} characters",
                code=RECON_NOTE_REQUIRED,
                length=len(note),
            )
        assert_no_card_data({"note": note})

        with _write_transaction(self._conn):
            row = self._conn.execute(
                "SELECT * FROM reconciliation_cases WHERE recon_id = ?", (recon_id,)
            ).fetchone()
            if row is None:
                raise ReconciliationNotFound(
                    f"reconciliation case {recon_id!r} does not exist", recon_id=recon_id
                )
            case = ReconciliationCase.from_row(row)
            if case.status != "OPEN":
                raise ReconciliationClosed(
                    "this reconciliation case is already closed",
                    recon_id=recon_id,
                    status=case.status,
                )
            posted_payment_id: Optional[str] = None
            if resolution == "MATCHED":
                posted_payment_id = self._post_matched(case, at=at_iso, actor=actor)
            status = "WAIVED" if resolution == "WAIVED" else "RESOLVED"
            self._conn.execute(
                "UPDATE reconciliation_cases SET status = ?, resolution_note = ?,"
                " resolved_at = ?, owner_actor = ?,"
                " payment_id = COALESCE(payment_id, ?) WHERE recon_id = ?",
                (status, note, at_iso, actor, posted_payment_id, recon_id),
            )
            subject_id = case.payment_id or posted_payment_id or case.payment_event_id
            if subject_id is not None:
                self._close_breaches(subject_id, note=note, at=at_iso)
            self._audit.append(
                action=AUDIT_RECONCILIATION_RESOLVED,
                entity_type="reconciliation_case",
                entity_id=recon_id,
                actor=actor,
                actor_role=self._actor_role,
                before={"status": case.status},
                after={
                    "status": status,
                    "resolution": resolution,
                    "payment_id": posted_payment_id,
                    "resolved_at": at_iso,
                },
                reason=note,
            )
        return self.get_reconciliation(recon_id)

    def _post_matched(
        self, case: ReconciliationCase, *, at: str, actor: str
    ) -> str:
        """Settle the money that arrived onto the invoice a human matched it to."""
        if case.invoice_id is None:
            raise ReconciliationInvoiceMissing(
                "a MATCHED resolution needs the invoice the money belongs to",
                recon_id=case.recon_id,
            )
        try:
            invoice = self._invoicing().get(case.invoice_id)
        except EntityNotFound as exc:
            raise ReconciliationInvoiceMissing(
                "the invoice this case points at does not exist",
                recon_id=case.recon_id,
                invoice_id=case.invoice_id,
            ) from exc
        if invoice.status != "ISSUED":
            raise ReconciliationInvoiceMissing(
                "the invoice this case points at cannot be settled",
                recon_id=case.recon_id,
                invoice_id=invoice.invoice_id,
                status=invoice.status,
            )
        declared = case.details_dict
        amount = declared.get("declared_amount_minor")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ReconciliationAmountUnknown(
                "no positive amount received was recorded for this case",
                recon_id=case.recon_id,
                declared=declared.get("declared_amount_minor"),
            )
        currency = declared.get("declared_currency")
        if currency is not None and currency != invoice.currency:
            raise PaymentCurrencyMismatch(
                "the money received is in another currency than the invoice",
                recon_id=case.recon_id,
                currency=currency,
                invoice_currency=invoice.currency,
            )
        provider = self._provider
        if case.payment_event_id is not None:
            event_row = self._conn.execute(
                "SELECT provider FROM payment_events WHERE event_id = ?",
                (case.payment_event_id,),
            ).fetchone()
            if event_row is not None:
                provider = str(event_row["provider"])
        payment_id = self._ids.new("payment")
        self._conn.execute(
            f"INSERT INTO payments ({', '.join(PAYMENT_COLUMNS)})"
            f" VALUES ({', '.join('?' * len(PAYMENT_COLUMNS))})",
            (
                payment_id,
                invoice.invoice_id,
                provider,
                self._ids.new("pi"),
                PAYMENT_METHOD_MANUAL,
                invoice.currency,
                amount,
                "SUCCEEDED",
                at,
                None,
                at,
                "reconciled",
            ),
        )
        payment = Payment.from_row(self._payment_row(payment_id))
        entries = self._post_settlement(payment, invoice, occurred_at=at)
        self._invoicing().record_settlement(
            invoice.invoice_id,
            amount_paid_minor=invoice.amount_paid_minor + amount,
            payment_status=_settlement_status(
                invoice.amount_paid_minor + amount, invoice.total_minor
            ),
            reason=f"reconciliation {case.recon_id} matched this money to the invoice",
            actor=actor,
            actor_role=self._actor_role,
        )
        if case.payment_id is not None:
            # The intent this case came from can no longer be satisfied as
            # written (the money that arrived is different); leaving it PENDING
            # would let the guest pay a second time for money already settled.
            self._conn.execute(
                "UPDATE payments SET status = 'EXPIRED', provider_status = 'reconciled_elsewhere'"
                " WHERE payment_id = ? AND status = 'PENDING'",
                (case.payment_id,),
            )
        self._audit.append(
            action=AUDIT_PAYMENT_SETTLED,
            entity_type="payment",
            entity_id=payment_id,
            actor=actor,
            actor_role=self._actor_role,
            after=payment.as_row(),
            reason=f"reconciled match; ledger entries {len(entries)}",
        )
        return payment_id

    # -- refunds ----------------------------------------------------------- #
    def refund(
        self,
        *,
        payment_id: str,
        amount_minor: int,
        approval_id: str,
        actor: str,
        actor_role: str,
        reason: str,
        at: Optional[Any] = None,
    ) -> Refund:
        """Send money back against a settled payment, with real approval behind it.

        The approval is *read*, never taken on trust: ``approval_requests`` must
        hold an ``APPROVED`` row for subject type ``refund`` whose subject is
        this payment, and ``approval_decisions`` must contain the required number
        of ``APPROVE`` decisions and no rejection
        (``REFUND_APPROVAL_REQUIRED`` otherwise). A refund never exceeds what
        actually settled minus what already went back
        (``REFUND_EXCEEDS_PAYMENT``), and it writes a balanced
        ``REFUND_OUT``/``CASH_IN`` batch before the invoice is moved through the
        invoice service.
        """
        payment_id = require_ref(payment_id, "payment_id", max_len=64)
        approval_id = require_ref(approval_id, "approval_id", max_len=64)
        actor = require_ref(actor, "actor", max_len=64)
        actor_role = require_ref(actor_role, "actor_role", max_len=64)
        amount = _require_minor(amount_minor, "amount_minor", positive=True)
        at_iso = _normalise_instant(at if at is not None else self._clock.now(), field_name="at")
        reason = require_text(reason, "reason", max_len=500)
        if len(reason) < MIN_NOTE_LENGTH:
            raise DomainError(
                f"reason must be at least {MIN_NOTE_LENGTH} characters",
                code=INVALID_REFUND,
                length=len(reason),
            )
        assert_no_card_data({"reason": reason, "note": reason})

        with _write_transaction(self._conn):
            row = self._payment_row(payment_id)
            if row is None:
                raise PaymentNotFound(
                    f"payment {payment_id!r} does not exist", payment_id=payment_id
                )
            payment = Payment.from_row(row)
            if not payment.is_settled or payment.settled_at is None:
                raise PaymentNotSettled(
                    "only a settled payment can be refunded",
                    payment_id=payment.payment_id,
                    status=payment.status,
                )
            already = self._refunded_total(payment.payment_id)
            if amount > payment.amount_minor - already:
                raise RefundExceedsPayment(
                    "the refund exceeds what this payment settled",
                    payment_id=payment.payment_id,
                    amount_minor=amount,
                    settled_minor=payment.amount_minor,
                    refunded_minor=already,
                )
            approval = self._approved_refund(
                approval_id, payment=payment, amount_minor=amount
            )
            entries = post_journal(
                self._conn,
                entries=[
                    {
                        "account": "REFUND_OUT",
                        "direction": "DEBIT",
                        "amount_minor": amount,
                        "currency": payment.currency,
                        "invoice_id": payment.invoice_id,
                        "payment_id": payment.payment_id,
                        "memo": f"refund {approval_id} for payment {payment.payment_id}",
                    },
                    {
                        "account": "CASH_IN",
                        "direction": "CREDIT",
                        "amount_minor": amount,
                        "currency": payment.currency,
                        "invoice_id": payment.invoice_id,
                        "payment_id": payment.payment_id,
                        "memo": f"cash returned for payment {payment.payment_id}",
                    },
                ],
                occurred_at=at_iso,
                ids=self._ids,
            )
            refunded_total = already + amount
            fully = refunded_total >= payment.amount_minor
            self._conn.execute(
                "UPDATE payments SET status = ?, provider_status = ? WHERE payment_id = ?",
                ("REFUNDED" if fully else payment.status, "refunded" if fully else "partially_refunded", payment.payment_id),
            )
            invoice = self._invoicing().get(payment.invoice_id)
            self._invoicing().record_settlement(
                invoice.invoice_id,
                amount_paid_minor=max(0, invoice.amount_paid_minor - amount),
                payment_status="REFUNDED",
                reason=f"refund {approval_id} recorded by {actor}",
                actor=actor,
                actor_role=actor_role,
            )
            self._audit.append(
                action=AUDIT_REFUND_RECORDED,
                entity_type="payment",
                entity_id=payment.payment_id,
                actor=actor,
                actor_role=actor_role,
                before=payment.as_row(),
                after={
                    "amount_minor": amount,
                    "refunded_total_minor": refunded_total,
                    "fully_refunded": fully,
                    "approval_id": approval["approval_id"],
                },
                reason=reason,
            )
        return Refund(
            payment_id=payment.payment_id,
            invoice_id=payment.invoice_id,
            amount_minor=amount,
            currency=payment.currency,
            approval_id=str(approval["approval_id"]),
            actor=actor,
            reason=reason,
            occurred_at=at_iso,
            refunded_total_minor=refunded_total,
            fully_refunded=fully,
            entries=tuple(entry.entry_id for entry in entries),
        )

    def _refunded_total(self, payment_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_minor), 0) AS total FROM ledger_entries"
            " WHERE payment_id = ? AND account = 'REFUND_OUT' AND direction = 'DEBIT'",
            (payment_id,),
        ).fetchone()
        return int(row["total"] or 0)

    def _approved_refund(
        self, approval_id: str, *, payment: Payment, amount_minor: int
    ) -> Mapping[str, Any]:
        """Read the approval and its decisions; a claim is never evidence."""
        row = self._conn.execute(
            "SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise RefundNotApproved(
                "no such approval request", approval_id=approval_id
            )
        if str(row["subject_type"]) != "refund" or str(row["subject_id"]) != payment.payment_id:
            raise RefundNotApproved(
                "the approval request does not cover this payment",
                approval_id=approval_id,
                subject_type=str(row["subject_type"]),
                subject_id=str(row["subject_id"]),
                payment_id=payment.payment_id,
            )
        decisions = self._conn.execute(
            "SELECT decision, approver, approver_role FROM approval_decisions"
            " WHERE approval_id = ?",
            (approval_id,),
        ).fetchall()
        approvals = [d for d in decisions if str(d["decision"]) == "APPROVE"]
        rejections = [d for d in decisions if str(d["decision"]) == "REJECT"]
        required = int(row["required_approvals"])
        approvers = {str(d["approver"]) for d in approvals}
        if (
            str(row["status"]) != "APPROVED"
            or len(approvers) < required
            or rejections
        ):
            raise RefundNotApproved(
                "the approval is not a valid APPROVED decision for this refund",
                approval_id=approval_id,
                status=str(row["status"]),
                approvals=len(approvers),
                required_approvals=required,
                rejections=len(rejections),
            )
        currency = row["currency"]
        if currency is not None and str(currency) != payment.currency:
            raise RefundNotApproved(
                "the approval was given in another currency",
                approval_id=approval_id,
                approved_currency=str(currency),
                payment_currency=payment.currency,
            )
        approved_minor = row["money_minor"]
        if approved_minor is not None and amount_minor > int(approved_minor):
            raise RefundNotApproved(
                "the approval does not cover this amount",
                approval_id=approval_id,
                approved_minor=int(approved_minor),
                amount_minor=amount_minor,
            )
        return row

    # -- queries ----------------------------------------------------------- #
    def get_payment(self, payment_id: str) -> Payment:
        """Read one payment by id (``PAYMENT_NOT_FOUND`` when it is unknown)."""
        payment_id = require_ref(payment_id, "payment_id", max_len=64)
        row = self._payment_row(payment_id)
        if row is None:
            raise PaymentNotFound(
                f"payment {payment_id!r} does not exist", payment_id=payment_id
            )
        return Payment.from_row(row)

    def for_invoice(self, invoice_id: str) -> tuple[Payment, ...]:
        """Every payment raised against one invoice, oldest first."""
        invoice_id = require_ref(invoice_id, "invoice_id", max_len=64)
        rows = self._conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ? ORDER BY created_at, payment_id",
            (invoice_id,),
        ).fetchall()
        return tuple(Payment.from_row(row) for row in rows)

    def events_for_payment(self, payment_id: str) -> tuple[PaymentEvent, ...]:
        """Every webhook recorded against one payment, in arrival order."""
        payment_id = require_ref(payment_id, "payment_id", max_len=64)
        rows = self._conn.execute(
            "SELECT * FROM payment_events WHERE payment_id = ? ORDER BY received_at, event_id",
            (payment_id,),
        ).fetchall()
        return tuple(PaymentEvent.from_row(row) for row in rows)

    def entries(
        self, *, payment_id: Optional[str] = None, invoice_id: Optional[str] = None
    ) -> tuple[LedgerEntry, ...]:
        """Ledger rows for one payment and/or invoice, in ledger order."""
        clauses: list[str] = []
        params: list[Any] = []
        if payment_id is not None:
            clauses.append("payment_id = ?")
            params.append(require_ref(payment_id, "payment_id", max_len=64))
        if invoice_id is not None:
            clauses.append("invoice_id = ?")
            params.append(require_ref(invoice_id, "invoice_id", max_len=64))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM ledger_entries{where} ORDER BY seq", tuple(params)
        ).fetchall()
        return tuple(LedgerEntry.from_row(row) for row in rows)

    def unmatched(self, now: Optional[Any] = None) -> tuple[ReconciliationCase, ...]:
        """Every open reconciliation case, due soonest first.

        ``now`` marks which of them are already past their deadline; with ``now``
        given, only the overdue ones are returned, which is the queue a human has
        to work today.
        """
        rows = self._conn.execute(
            "SELECT * FROM reconciliation_cases WHERE status = 'OPEN' ORDER BY due_at, recon_id"
        ).fetchall()
        cases = tuple(ReconciliationCase.from_row(row) for row in rows)
        if now is None:
            return cases
        cutoff = _normalise_instant(now, field_name="now")
        return tuple(case for case in cases if case.is_overdue_at(cutoff))

    def get_reconciliation(self, recon_id: str) -> ReconciliationCase:
        """Read one reconciliation case by id."""
        recon_id = require_ref(recon_id, "recon_id", max_len=64)
        row = self._conn.execute(
            "SELECT * FROM reconciliation_cases WHERE recon_id = ?", (recon_id,)
        ).fetchone()
        if row is None:
            raise ReconciliationNotFound(
                f"reconciliation case {recon_id!r} does not exist", recon_id=recon_id
            )
        return ReconciliationCase.from_row(row)

    def breaches(self, *, subject_id: Optional[str] = None) -> tuple[Mapping[str, Any], ...]:
        """SLA breach rows raised for unmatched money, oldest first."""
        if subject_id is None:
            rows = self._conn.execute(
                "SELECT * FROM sla_breaches WHERE kind = ? ORDER BY detected_at, breach_id",
                (UNMATCHED_PAYMENT_BREACH,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sla_breaches WHERE kind = ? AND subject_id = ?"
                " ORDER BY detected_at, breach_id",
                (UNMATCHED_PAYMENT_BREACH, require_ref(subject_id, "subject_id", max_len=128)),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def pending_intents(self, now: Optional[Any] = None) -> tuple[Payment, ...]:
        """Mark intents past ``expires_at`` as EXPIRED and return the live ones.

        An intent the guest can still pay is ``PENDING``; the ones whose window
        has closed are written to ``EXPIRED`` (and audited) before this returns,
        so they can never be paid after the fact.
        """
        cutoff = _normalise_instant(
            now if now is not None else self._clock.now(), field_name="now"
        )
        with _write_transaction(self._conn):
            expired = self._conn.execute(
                "SELECT * FROM payments WHERE status = 'PENDING' AND expires_at IS NOT NULL"
                " AND expires_at <= ? ORDER BY expires_at, payment_id",
                (cutoff,),
            ).fetchall()
            for row in expired:
                payment = Payment.from_row(row)
                self._conn.execute(
                    "UPDATE payments SET status = 'EXPIRED', provider_status = 'intent_expired'"
                    " WHERE payment_id = ? AND status = 'PENDING'",
                    (payment.payment_id,),
                )
                self._audit.append(
                    action=AUDIT_INTENT_EXPIRED,
                    entity_type="payment",
                    entity_id=payment.payment_id,
                    actor=self._actor,
                    actor_role=self._actor_role,
                    before=payment.as_row(),
                    after={"status": "EXPIRED", "expired_at": cutoff},
                    reason=f"intent expired at {payment.expires_at}",
                )
        rows = self._conn.execute(
            "SELECT * FROM payments WHERE status = 'PENDING' AND expires_at IS NOT NULL"
            " AND expires_at > ? ORDER BY expires_at, payment_id",
            (cutoff,),
        ).fetchall()
        return tuple(Payment.from_row(row) for row in rows)

    def ledger_balance(self, currency: str) -> LedgerBalance:
        """Per-account debit/credit totals of one currency.

        Raises :class:`LedgerUnbalanced` if the currency's debits and credits
        disagree — a ledger that does not balance is a defect, not a report.
        """
        code = _require_currency(currency)
        rows = self._conn.execute(
            "SELECT account, direction, COALESCE(SUM(amount_minor), 0) AS total,"
            " COUNT(*) AS count FROM ledger_entries WHERE currency = ?"
            " GROUP BY account, direction ORDER BY account, direction",
            (code,),
        ).fetchall()
        by_account: dict[str, dict[str, int]] = {}
        debits = credits = count = 0
        for row in rows:
            account = str(row["account"])
            direction = str(row["direction"])
            total = int(row["total"])
            bucket = by_account.setdefault(account, {"DEBIT": 0, "CREDIT": 0})
            bucket[direction] = total
            count += int(row["count"])
            if direction == "DEBIT":
                debits += total
            else:
                credits += total
        if debits != credits:
            raise LedgerUnbalanced(
                f"the ledger does not balance for {code}: debits {debits} != credits {credits}",
                currency=code,
                debits_minor=debits,
                credits_minor=credits,
            )
        return LedgerBalance(
            currency=code,
            debit_total_minor=debits,
            credit_total_minor=credits,
            by_account=MappingProxyType({key: dict(value) for key, value in by_account.items()}),
            entry_count=count,
        )

    def verify(self) -> LedgerVerification:
        """Replay this service's ledger chain (see :func:`verify_ledger`)."""
        return verify_ledger(self._conn)


def invoice_exists(conn: Any, invoice_id: Optional[str]) -> bool:
    """True when the invoice referenced by an event really exists."""
    if invoice_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM invoices WHERE invoice_id = ?", (invoice_id,)
    ).fetchone()
    return row is not None
