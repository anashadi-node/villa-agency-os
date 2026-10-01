"""Grounded outbound message drafts for Villa Agency OS.

Scope, stated before anything else: **this module never sends anything**. There is
no SMTP, WhatsApp, HTTP or socket call anywhere in it. A draft is text the agency
*intends* to send; :meth:`MessageService.mark_sent` only records that a **human**
sent it out of band, together with the ``external_ref`` they can point at later.
The module's job is to make that record trustworthy:

* **Nothing enters a body that is not in the database.** Every template is a named
  placeholder skeleton (:data:`TEMPLATES`). A placeholder with no binding raises
  :class:`UnboundPlaceholder` and *no row is written*; every binding must cite a
  real record (``intake_field`` / ``invoice`` / ``quote`` / ``booking`` /
  ``policy_version`` / ``hold`` / ``case`` / ``property`` / ``unit``) and every
  number, date, time, percentage or rupiah amount in the rendered body must be
  backed by one of those bindings *exactly as written* — otherwise the draft is
  ``UNGROUNDED`` and cannot be approved
  (:data:`CONSTRAINT_GROUNDED_BEFORE_APPROVAL`, mirrored by a DB CHECK).
* **The classification gate runs on every draft.** The rendered body is classified
  (default ``P1``) and pushed through :func:`gates.gate_outbound` for the channel's
  destination; the outcome is stored in ``message_drafts.gate_decision``. A denied
  body — direct identifier in a P0/P1 field, medical ``P3``, card ``P4``, an
  unredacted ``P2`` — can never be approved and :meth:`mark_sent` raises
  :class:`GateRefused`.
* **A draft is APPROVED only because a human approved a request.** Approval goes
  through :class:`approvals.ApprovalService` with ``subject_type='message_send'``;
  the status column is *derived from* that decision, never asserted directly. The
  draft's author can never be its approver.
* **The body cannot change after it is written.** There is no update path in this
  module; the body is hashed at creation and the hash is re-verified before
  approval and before sending. Any out-of-band edit (a direct ``UPDATE`` in the
  database) makes :meth:`approve` / :meth:`mark_sent` raise :class:`BodyTampered`,
  and :meth:`verify` reports it.

Deliberately *not* here: real delivery, template authoring, retries, scheduling.
"""

from __future__ import annotations

import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable, Iterator, Mapping, Optional

import roles
from approvals import ROLE_RANK, Approval, ApprovalService
from audit import append_event, contains_direct_identifiers, guard_append_only
from gates import (
    DESTINATION_CHAT,
    DESTINATION_EMAIL,
    DESTINATION_STORE,
    ClassifiedField,
    gate_outbound,
)
from primitives import (
    DomainError,
    IdFactory,
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
    "TEMPLATES",
    "TEMPLATE_NAMES",
    "CHANNELS",
    "CHANNEL_DESTINATIONS",
    "DRAFT_STATUSES",
    "GROUNDING_STATUSES",
    "CLAIM_KINDS",
    "SOURCE_TABLES",
    "DEFAULT_CONTENT_CLASS",
    "CONSTRAINT_GROUNDED_BEFORE_APPROVAL",
    "MACHINE_ACTOR_MARKERS",
    "SENDER_ROLES",
    "CLAIM_PATTERNS",
    "Binding",
    "Claim",
    "GroundingResult",
    "MessageDraft",
    "VerificationReport",
    "MessageService",
    "MessageError",
    "UnknownTemplate",
    "InvalidChannel",
    "InvalidRecipient",
    "InvalidLocale",
    "InvalidActor",
    "InvalidSenderRole",
    "MissingExternalRef",
    "BodyTooLong",
    "UnboundPlaceholder",
    "InvalidPlaceholder",
    "UnknownPlaceholder",
    "UnknownSourceType",
    "UnsourcedBinding",
    "UnknownDraft",
    "CaseNotFound",
    "BookingNotFound",
    "UngroundedDraft",
    "GateRefused",
    "BodyTampered",
    "DraftNotPending",
    "DraftNotApproved",
    "DraftClosed",
    "SelfApprovalRefused",
    "InvalidRange",
    "extract_claims",
    "ground",
    "render_template",
    "template_placeholders",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Named-placeholder skeletons. They are the *only* way text becomes a body, and
#: (by design) they carry no literal digits: every number a guest may read must be
#: filled from a binding, so it can be traced back to a row.
TEMPLATES: dict[str, str] = {
    "availability_offer": (
        "Halo {guest_name}, unit {unit_code} masih tersedia untuk {check_in} sampai "
        "{check_out} ({nights} malam). Tarif {nightly_rate} per malam, total "
        "{total_amount}. Balas pesan ini bila unit ingin kami tahan untuk Anda."
    ),
    "quote_offer": (
        "Halo {guest_name}, berikut penawaran menginap di {unit_code} untuk {check_in} "
        "sampai {check_out} ({nights} malam). Akomodasi {accommodation_amount}, biaya "
        "kebersihan {cleaning_amount}, pajak {tax_amount}, total {total_amount}. "
        "Penawaran berlaku sampai {valid_until}. Balas pesan ini untuk melanjutkan."
    ),
    "hold_confirmation": (
        "Halo {guest_name}, unit {unit_code} kami tahan atas nama Anda sampai "
        "{hold_expires_at}. Mohon konfirmasi sebelum waktu tersebut agar unit tetap "
        "tersedia untuk {check_in} sampai {check_out}."
    ),
    "payment_reminder": (
        "Halo {guest_name}, pengingat pembayaran invoice {invoice_ref} sebesar "
        "{amount_due} yang jatuh tempo {invoice_due}. Mohon bayar melalui "
        "{payment_channel} atas nama {account_name}. Balas pesan ini bila sudah bayar."
    ),
    "arrival_instructions": (
        "Halo {guest_name}, petunjuk kedatangan {unit_code}, {property_name}: check-in "
        "mulai {check_in_time} oleh {host_name}. Kode akses dikirim oleh {host_name} "
        "melalui {contact_channel}. Bila tiba di luar jam tersebut, hubungi {host_name} "
        "pada nomor reservasi {booking_ref}."
    ),
    "complaint_ack": (
        "Halo {guest_name}, laporan Anda mengenai {issue_summary} sudah kami terima dan "
        "kami mohon maaf atas ketidaknyamanannya. Laporan tercatat sebagai {ticket_ref} "
        "dan ditangani oleh {owner_name} paling lambat {resolution_due}. Kami akan "
        "mengabari Anda melalui {contact_channel}."
    ),
}

TEMPLATE_NAMES: tuple[str, ...] = tuple(sorted(TEMPLATES))

#: ``message_drafts.channel`` CHECK values. No new channel may be invented here:
#: the DB rejects it, and so does :class:`InvalidChannel`.
CHANNELS: tuple[str, ...] = ("website", "email", "whatsapp_business", "dashboard", "phone")

#: Which :mod:`gates` destination each channel actually is. ``chat`` denies P2,
#: ``email`` demands a role for P2, ``store`` (internal dashboard) allows P2.
CHANNEL_DESTINATIONS: dict[str, str] = {
    "website": DESTINATION_CHAT,
    "whatsapp_business": DESTINATION_CHAT,
    "phone": DESTINATION_CHAT,
    "email": DESTINATION_EMAIL,
    "dashboard": DESTINATION_STORE,
}

DRAFT_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "PENDING_APPROVAL",
    "APPROVED",
    "SENT",
    "REJECTED",
    "EXPIRED",
)

#: ``message_drafts.grounding`` CHECK values.
GROUNDING_STATUSES: tuple[str, ...] = ("GROUNDED", "UNGROUNDED", "REVIEW_REQUIRED")

GROUNDED = "GROUNDED"
UNGROUNDED = "UNGROUNDED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"

DRAFT = "DRAFT"
PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVED = "APPROVED"
SENT = "SENT"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"

#: Kinds of claim :func:`extract_claims` recognises, and the kind a binding must
#: declare for the claim to be considered backed.
CLAIM_KINDS: tuple[str, ...] = ("money", "date", "time", "percent", "number", "text")

#: Every binding must cite a row in one of these tables. ``policy`` is accepted as
#: an alias for ``policy_version`` because that is how people say it out loud.
SOURCE_TABLES: dict[str, tuple[str, str]] = {
    "intake_field": ("intake_fields", "intake_field_id"),
    "invoice": ("invoices", "invoice_id"),
    "quote": ("quotes", "quote_id"),
    "booking": ("bookings", "booking_id"),
    "hold": ("holds", "hold_id"),
    "policy_version": ("policy_versions", "policy_version_id"),
    "policy": ("policy_versions", "policy_version_id"),
    "case": ("cases", "case_id"),
    "property": ("properties", "property_id"),
    "unit": ("units", "unit_id"),
}

#: Public operational content: allowed at every destination in :data:`POLICY_TABLE`.
DEFAULT_CONTENT_CLASS = "P1"

#: Roles allowed to record that a human sent a draft. Note this is *narrower* than
#: the approval roles: a manager may approve, only front-line staff may send.
SENDER_ROLES: tuple[str, ...] = ("agent", "owner")

#: Actor ids that name a machine, not a person. A draft may be *prepared* by the
#: kernel, but approval and sending are human acts.
MACHINE_ACTOR_MARKERS: tuple[str, ...] = ("system", "machine", "svc", "bot", "scheduler", "cron")

CONSTRAINT_GROUNDED_BEFORE_APPROVAL = (
    "grounding = 'GROUNDED' OR status NOT IN ('APPROVED','SENT')"
)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ANY_BRACE_RE = re.compile(r"[{}]")

_NUMERIC_PLACEHOLDER_KINDS: Mapping[str, str] = {
    "check_in": "date",
    "check_out": "date",
    "nights": "number",
    "nightly_rate": "money",
    "total_amount": "money",
    "accommodation_amount": "money",
    "cleaning_amount": "money",
    "tax_amount": "money",
    "valid_until": "date",
    "hold_expires_at": "date",
    "amount_due": "money",
    "invoice_due": "date",
    "check_in_time": "time",
    "resolution_due": "date",
}

# Ordered: money and dates are consumed before the generic number pattern, so
# "Rp 1.500.000" is one money claim rather than three numeric fragments.
CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("money", re.compile(r"\b(?:Rp|IDR|rp)\s?\.?\s?\d[\d.,]*")),
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("date", re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")),
    (
        "date",
        re.compile(
            r"\b\d{1,2}\s+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|"
            r"September|Oktober|November|Desember|January|February|March|April|May|"
            r"June|July|August|September|October|November|December)\s+\d{4}\b"
        ),
    ),
    ("time", re.compile(r"\b\d{1,2}:\d{2}\b")),
    ("percent", re.compile(r"\b\d+(?:[.,]\d+)?\s?%")),
    ("number", re.compile(r"\b\d+(?:[.,]\d+)?\b")),
)

_TS_FIELDS = ("created_at", "approved_at", "sent_at")

_COLUMNS = (
    "draft_id",
    "case_id",
    "booking_id",
    "channel",
    "template",
    "locale",
    "recipient_ref",
    "body_text",
    "body_sha256",
    "claim_bindings",
    "grounding",
    "gate_decision",
    "status",
    "approval_id",
    "created_by",
    "created_at",
    "approved_by",
    "approved_at",
    "sent_by",
    "sent_at",
    "external_ref",
)


# ---------------------------------------------------------------------------
# Errors — one class per refusal, so a caller can branch on the reason
# ---------------------------------------------------------------------------


class MessageError(DomainError):
    """Base class for every refusal in this module."""


class UnknownTemplate(MessageError):
    code = "UNKNOWN_TEMPLATE"


class InvalidChannel(MessageError):
    code = "INVALID_CHANNEL"


class InvalidRecipient(MessageError):
    code = "INVALID_RECIPIENT"


class InvalidLocale(MessageError):
    code = "INVALID_LOCALE"


class InvalidActor(MessageError):
    code = "INVALID_ACTOR"


class InvalidSenderRole(MessageError):
    code = "INVALID_SENDER_ROLE"


class MissingExternalRef(MessageError):
    code = "MISSING_EXTERNAL_REF"


class BodyTooLong(MessageError):
    code = "BODY_TOO_LONG"


class UnboundPlaceholder(MessageError):
    """A template placeholder had no binding. Nothing is written to the DB."""

    code = "UNBOUND_PLACEHOLDER"


class InvalidPlaceholder(MessageError):
    code = "INVALID_PLACEHOLDER"


class UnknownPlaceholder(MessageError):
    code = "UNKNOWN_PLACEHOLDER"


class UnknownSourceType(MessageError):
    code = "UNKNOWN_SOURCE_TYPE"


class UnsourcedBinding(MessageError):
    code = "UNSOURCED_BINDING"


class UnknownDraft(MessageError):
    code = "DRAFT_NOT_FOUND"


class CaseNotFound(MessageError):
    code = "CASE_NOT_FOUND"


class BookingNotFound(MessageError):
    code = "BOOKING_NOT_FOUND"


class UngroundedDraft(MessageError):
    code = "UNGROUNDED_DRAFT"


class GateRefused(MessageError):
    """The classification gate denied this body for this destination."""

    code = "GATE_REFUSED"


class BodyTampered(MessageError):
    """The stored body no longer hashes to its recorded digest."""

    code = "BODY_TAMPERED"


class BodyImmutable(MessageError):
    """A body may not be rewritten after creation: create a new draft instead."""

    code = "BODY_IMMUTABLE"


class DraftNotPending(MessageError):
    code = "DRAFT_NOT_PENDING"


class DraftNotApproved(MessageError):
    code = "DRAFT_NOT_APPROVED"


class DraftClosed(MessageError):
    code = "DRAFT_CLOSED"


class SelfApprovalRefused(MessageError):
    code = "SELF_APPROVAL_FORBIDDEN"


class InvalidRange(MessageError):
    code = "INVALID_RANGE"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """NFC + collapsed whitespace. The canonical form both sides compare in."""
    return " ".join(unicodedata.normalize("NFC", str(text)).replace("\u00a0", " ").split())


def _digits(text: str) -> str:
    return re.sub(r"\D", "", str(text))


@dataclass(frozen=True)
class Binding:
    """One fact inserted into a body, plus the row it came from.

    ``value`` is the literal text placed in the body — grounding compares it
    exactly (NFC + whitespace-collapsed), so "Rp 1.500.000" and "Rp 1500000" are
    different facts and each needs its own binding.
    """

    value: str
    kind: str = "text"
    source_type: str = ""
    source_id: str = ""
    placeholder: str = ""
    field_name: str = ""

    def __post_init__(self) -> None:
        value = require_text(self.value, "binding.value", max_len=10000)
        object.__setattr__(self, "value", value)
        kind = str(self.kind or "").strip().lower()
        if kind not in CLAIM_KINDS:
            raise UnsourcedBinding(
                f"binding kind {self.kind!r} is not one of {CLAIM_KINDS}",
                kind=self.kind,
                value=value,
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source_type", str(self.source_type or "").strip().lower())
        object.__setattr__(self, "source_id", "" if self.source_id is None else str(self.source_id))
        object.__setattr__(self, "placeholder", str(self.placeholder or ""))
        object.__setattr__(self, "field_name", str(self.field_name or ""))

    @property
    def key(self) -> str:
        """The canonical text this binding can back."""
        return _norm(self.value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "kind": self.kind,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "placeholder": self.placeholder,
            "field_name": self.field_name,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Binding":
        return cls(
            value=data["value"],
            kind=data.get("kind", "text"),
            source_type=data.get("source_type", ""),
            source_id=data.get("source_id", ""),
            placeholder=data.get("placeholder", ""),
            field_name=data.get("field_name", ""),
        )


@dataclass(frozen=True)
class Claim:
    """A number/date/amount found in a rendered body."""

    text: str
    kind: str
    start: int
    end: int

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "kind": self.kind, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class GroundingResult:
    """Outcome of :func:`ground`."""

    status: str
    claims: tuple[Claim, ...]
    matched: tuple[str, ...]
    unmatched: tuple[Claim, ...]
    missing_sources: tuple[str, ...]
    unverified_sources: tuple[str, ...]
    bindings: tuple[Binding, ...]

    @property
    def is_grounded(self) -> bool:
        return self.status == GROUNDED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "claims": [c.as_dict() for c in self.claims],
            "matched": list(self.matched),
            "unmatched": [c.as_dict() for c in self.unmatched],
            "missing_sources": list(self.missing_sources),
            "unverified_sources": list(self.unverified_sources),
            "bindings": [b.as_dict() for b in self.bindings],
        }

    def to_json(self) -> str:
        """Serialised form stored in ``message_drafts.claim_bindings``."""
        return canonical_json(
            {
                "bindings": [b.as_dict() for b in self.bindings],
                "claims": [c.as_dict() for c in self.claims],
                "unmatched": [c.as_dict() for c in self.unmatched],
                "missing_sources": list(self.missing_sources),
                "unverified_sources": list(self.unverified_sources),
            }
        )


@dataclass(frozen=True)
class MessageDraft:
    """A row of ``message_drafts``, verbatim."""

    draft_id: str
    case_id: str
    booking_id: Optional[str]
    channel: str
    template: str
    locale: str
    recipient_ref: str
    body_text: str
    body_sha256: str
    claim_bindings: str
    grounding: str
    gate_decision: str
    status: str
    approval_id: Optional[str]
    created_by: str
    created_at: str
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    sent_by: Optional[str] = None
    sent_at: Optional[str] = None
    external_ref: Optional[str] = None

    @property
    def bindings(self) -> tuple[Binding, ...]:
        """The bindings exactly as stored, replayed for verification."""
        return tuple(
            Binding.from_dict(item) for item in _json(self.claim_bindings).get("bindings", [])
        )

    @property
    def claims(self) -> tuple[Claim, ...]:
        """The claims the body made when it was grounded."""
        return tuple(
            Claim(**item) for item in _json(self.claim_bindings).get("claims", [])  # noqa: S
        )

    @property
    def gate(self) -> Mapping[str, Any]:
        import json

        return json.loads(self.gate_decision)

    @property
    def gate_allowed(self) -> bool:
        return bool(self.gate.get("decision", {}).get("allowed"))

    @property
    def is_sendable(self) -> bool:
        return self.status == "APPROVED" and self.grounding == GROUNDED and self.gate_allowed

    def as_dict(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in _COLUMNS}
        data["gate"] = self.gate
        return data


@dataclass(frozen=True)
class VerificationReport:
    """Result of :meth:`MessageService.verify` — a re-derivation, not a lookup."""

    draft_id: str
    mismatches: tuple[str, ...]
    details: Mapping[str, Any]
    stored_body_sha256: str
    recomputed_body_sha256: str
    stored_grounding: str
    recomputed_grounding: str
    stored_gate_allowed: bool
    recomputed_gate_allowed: bool
    approval_status: Optional[str]

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "ok": self.ok,
            "mismatches": list(self.mismatches),
            "stored_body_sha256": self.stored_body_sha256,
            "recomputed_body_sha256": self.recomputed_body_sha256,
            "stored_grounding": self.stored_grounding,
            "recomputed_grounding": self.recomputed_grounding,
            "stored_gate_allowed": self.stored_gate_allowed,
            "recomputed_gate_allowed": self.recomputed_gate_allowed,
            "approval_status": self.approval_status,
            "details": dict(self.details),
        }


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def template_placeholders(name: str) -> tuple[str, ...]:
    """Named placeholders of a template, in order of first appearance."""
    text = TEMPLATES.get(name)
    if text is None:
        raise UnknownTemplate(
            f"unknown template {name!r}: known templates are {TEMPLATE_NAMES}",
            template=name,
            known=list(TEMPLATE_NAMES),
        )
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(text):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return tuple(seen)


def _coerce_bindings(bindings: Any) -> dict[str, Binding]:
    """Normalise ``{placeholder: Binding}`` or ``[Binding, ...]`` to a mapping."""
    if bindings is None:
        return {}
    result: dict[str, Binding] = {}
    if isinstance(bindings, Mapping):
        items: Iterable[tuple[Any, Any]] = bindings.items()
        for placeholder, binding in items:
            name = str(placeholder)
            if not isinstance(binding, Binding):
                raise UnsourcedBinding(
                    "bindings must be Binding values; pass raw text through a "
                    "Binding so it can cite a record",
                    placeholder=name,
                    got=type(binding).__name__,
                )
            # The mapping key *is* the placeholder: keep it on the binding so the
            # stored ``claim_bindings`` can re-render the same body later.
            result[name] = binding if binding.placeholder == name else replace(
                binding, placeholder=name
            )
        return result
    for index, binding in enumerate(bindings):
        if not isinstance(binding, Binding):
            raise UnsourcedBinding(
                "bindings must be Binding values",
                got=type(binding).__name__,
            )
        # A sequence is also how :meth:`MessageService.verify` replays the bindings
        # stored on a draft, where the placeholder that produced each value is not
        # needed (the body is already rendered): the value is what grounding matches.
        result[binding.placeholder or f"#{index}"] = binding
    return result


def render_template(template: str, bindings: Any) -> tuple[str, tuple[Binding, ...]]:
    """Fill a named-placeholder template. Raises if any placeholder is unbound."""
    text = TEMPLATES.get(template)
    if text is None:
        raise UnknownTemplate(
            f"unknown template {template!r}: known templates are {TEMPLATE_NAMES}",
            template=template,
            known=list(TEMPLATE_NAMES),
        )
    supplied = _coerce_bindings(bindings)
    names = template_placeholders(template)
    missing = [name for name in names if name not in supplied]
    if missing:
        raise UnboundPlaceholder(
            f"template {template!r} has unbound placeholders: {', '.join(missing)}",
            template=template,
            missing=missing,
            supplied=sorted(supplied),
        )
    # Reject any brace the placeholder grammar does not own: a template must not
    # be able to smuggle unfilled text past the check above.
    stripped = _PLACEHOLDER_RE.sub("", text)
    if _ANY_BRACE_RE.search(stripped):
        raise InvalidPlaceholder(
            f"template {template!r} contains a brace that is not a named placeholder",
            template=template,
        )
    used = {name: supplied[name] for name in names}
    for name, expected in _NUMERIC_PLACEHOLDER_KINDS.items():
        if name in used and used[name].kind != expected:
            raise UnsourcedBinding(
                f"placeholder {name!r} must be bound as {expected!r}, not {used[name].kind!r}",
                placeholder=name,
                expected=expected,
                got=used[name].kind,
            )
    body = _PLACEHOLDER_RE.sub(lambda m: used[m.group(1)].value, text)
    return _norm(body), tuple(used[name] for name in names)


# ---------------------------------------------------------------------------
# Claim extraction and grounding
# ---------------------------------------------------------------------------


def extract_claims(body_text: str) -> tuple[Claim, ...]:
    """Every amount, date, time, percentage or number in ``body_text``.

    Patterns are applied in :data:`CLAIM_PATTERNS` order and the spans they consume
    are masked, so "Rp 1.500.000" yields one money claim instead of a money claim
    plus three numbers.
    """
    text = _norm(require_text(body_text, "body_text", max_len=8000))
    taken = [False] * (len(text) + 1)
    claims: list[Claim] = []
    for kind, pattern in CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(taken[start:end]):
                continue
            raw = _norm(match.group(0))
            if kind in ("money", "number", "percent"):
                # Sentence punctuation is not part of the amount: "total Rp 6.000.000."
                # must claim exactly "Rp 6.000.000" or no binding could ever state it.
                trimmed = raw.rstrip(".,;:")
                if trimmed:
                    end -= len(raw) - len(trimmed)
                    raw = trimmed
            if not raw:
                continue
            for i in range(start, end):
                taken[i] = True
            claims.append(Claim(text=raw, kind=kind, start=start, end=end))
    claims.sort(key=lambda c: (c.start, c.end))
    return tuple(claims)


def _claim_is_backed(claim: Claim, binding: Binding) -> bool:
    """A claim is backed only by a binding that states it *exactly*.

    Both the kind and the text must match. A ``text`` binding whose value happens
    to contain ``Rp 5.000.000`` is not a source for that amount: otherwise any
    number could be laundered by stuffing it into a free-text field and the
    "every number has a source" guarantee would be worthless.
    """
    return binding.kind == claim.kind and binding.key == claim.text


def _source_exists(conn: Any, binding: Binding) -> Optional[bool]:
    """``True``/``False`` when the cited row can be looked up, ``None`` if untestable."""
    if not binding.source_type or not binding.source_id:
        return False
    table_key = SOURCE_TABLES.get(binding.source_type)
    if table_key is None:
        raise UnknownSourceType(
            f"unknown binding source_type {binding.source_type!r}",
            source_type=binding.source_type,
            known=sorted(SOURCE_TABLES),
        )
    table, column = table_key
    if conn is None:
        return None
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",  # noqa: S608 - table from SOURCE_TABLES
        (binding.source_id,),
    ).fetchone()
    return row is not None


def _intake_value_conflicts(conn: Any, binding: Binding) -> bool:
    """True when an ``intake_field`` binding names a field whose value differs.

    Only intake fields are cross-checked by value: they are the accepted-fact
    ledger, so a binding that points at a real field id but quotes a different
    number is the exact failure this module exists to prevent. Comparison is
    digit-normalised for money/number/percent and text-normalised otherwise.
    """
    if conn is None or binding.source_type != "intake_field" or not binding.field_name:
        return False
    row = conn.execute(
        "SELECT field_value FROM intake_fields WHERE intake_field_id = ? LIMIT 1",
        (binding.source_id,),
    ).fetchone()
    if row is None or row["field_value"] is None:
        return False
    stored = str(row["field_value"])
    if binding.kind in ("money", "number", "percent"):
        return _digits(stored) != _digits(binding.value)
    return _norm(stored) != binding.key


def ground(
    body_text: str,
    bindings: Any = (),
    conn: Any = None,
) -> GroundingResult:
    """Decide whether ``body_text`` is backed by ``bindings`` **in the database**.

    Fail-closed on every axis:

    * a claim with no matching binding → ``UNGROUNDED`` (``unmatched``)
    * a binding with no source, or a source id that does not exist, or an intake
      field whose stored value differs from the quoted text → ``UNGROUNDED``
      (``missing_sources``)
    * ``conn is None``, so sources cannot be checked at all → ``REVIEW_REQUIRED``,
      never ``GROUNDED``. A body can only be grounded against a database.
    """
    text = _norm(require_text(body_text, "body_text", max_len=8000))
    supplied = _coerce_bindings(bindings)
    ordered = tuple(supplied[name] for name in supplied)
    claims = extract_claims(text)

    unmatched: list[Claim] = []
    matched: list[str] = []
    for claim in claims:
        backing = [b for b in ordered if _claim_is_backed(claim, b)]
        if not backing:
            unmatched.append(claim)
        else:
            matched.append(claim.text)

    missing_sources: list[str] = []
    unverified_sources: list[str] = []
    for binding in ordered:
        exists = _source_exists(conn, binding)
        if exists is None:
            unverified_sources.append(binding.source_id or binding.value)
            continue
        if not exists:
            missing_sources.append(binding.source_id or f"{binding.source_type}:<none>")
            continue
        if _intake_value_conflicts(conn, binding):
            missing_sources.append(f"{binding.source_id}:{binding.field_name}")

    if unmatched or missing_sources:
        status = UNGROUNDED
    elif unverified_sources:
        status = REVIEW_REQUIRED
    else:
        status = GROUNDED

    return GroundingResult(
        status=status,
        claims=claims,
        matched=tuple(matched),
        unmatched=tuple(unmatched),
        missing_sources=tuple(dict.fromkeys(missing_sources)),
        unverified_sources=tuple(dict.fromkeys(unverified_sources)),
        bindings=ordered,
    )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@contextmanager
def _write(conn: Any) -> Iterator[None]:
    """One explicit transaction; the repo runs SQLite with ``isolation_level=None``."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MessageService:
    """Grounded drafts: create, route through human approval, record the sending."""

    def __init__(
        self,
        conn: Any,
        *,
        clock: Any = None,
        ids: Any = None,
        approvals: Optional[ApprovalService] = None,
        audit_enabled: bool = True,
    ) -> None:
        self._conn = conn
        self._clock = clock if clock is not None else SystemClock()
        self._ids = ids if ids is not None else IdFactory()
        self._approvals = (
            approvals if approvals is not None else ApprovalService(conn, self._clock, self._ids)
        )
        self._audit_enabled = bool(audit_enabled)
        if self._audit_enabled:
            # Fail closed if the append-only triggers are missing: an audit trail
            # that can be edited is not one.
            guard_append_only(conn)

    # -- introspection ----------------------------------------------------
    @property
    def conn(self) -> Any:
        return self._conn

    @property
    def approvals(self) -> ApprovalService:
        return self._approvals

    # -- helpers ----------------------------------------------------------
    def _now(self, at: Optional[datetime] = None) -> datetime:
        if at is not None:
            return at if isinstance(at, datetime) else parse_iso(str(at))
        return self._clock.now()

    def _audit(
        self,
        *,
        action: str,
        draft_id: str,
        actor: str,
        actor_role: str,
        after: Mapping[str, Any],
        reason: Optional[str] = None,
    ) -> None:
        if not self._audit_enabled:
            return
        append_event(
            self._conn,
            clock=self._clock,
            ids=self._ids,
            action=action,
            entity_type="message_draft",
            entity_id=draft_id,
            actor=actor,
            actor_role=actor_role,
            reason=reason,
            after=dict(after),
        )

    def _require_human(self, actor: str, role: str, *, field_name: str = "actor") -> tuple[str, str]:
        ref = require_ref(actor, field_name)
        role_key = str(role or "").strip().lower()
        if roles.role_rank(role_key) is None:
            raise InvalidActor(
                f"{field_name}_role {role!r} is not one of {sorted(ROLE_RANK)}",
                role=role,
                known=sorted(ROLE_RANK),
            )
        lowered = ref.lower()
        if lowered == SYSTEM_ACTOR or any(
            lowered == marker or lowered.startswith(marker + ":") or lowered.startswith(marker + "-")
            for marker in MACHINE_ACTOR_MARKERS
        ):
            raise InvalidActor(
                f"{field_name} {ref!r} names a machine: approval and sending are human acts",
                actor=ref,
            )
        return ref, role_key

    def _row(self, draft_id: str) -> Any:
        ref = require_ref(draft_id, "draft_id")
        row = self._conn.execute(
            "SELECT * FROM message_drafts WHERE draft_id = ?", (ref,)
        ).fetchone()
        if row is None:
            raise UnknownDraft(f"no message draft {ref!r}", draft_id=ref)
        return row

    def _draft(self, row: Any) -> MessageDraft:
        return MessageDraft(**{name: row[name] for name in _COLUMNS})

    def _check_exists(self, table: str, column: str, value: str, error: type, label: str) -> str:
        ref = require_ref(value, label if isinstance(label, str) else "ref")
        row = self._conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",  # noqa: S608 - fixed table names
            (ref,),
        ).fetchone()
        if row is None:
            raise error(f"no {table} row {ref!r}", **{label: ref})
        return ref

    def _assert_intact(self, row: Any) -> MessageDraft:
        """Re-verify the body digest before any state change. Fail closed."""
        draft = self._draft(row)
        recomputed = sha256_hex(draft.body_text)
        if recomputed != draft.body_sha256:
            raise BodyTampered(
                f"draft {draft.draft_id} body no longer matches its recorded digest",
                draft_id=draft.draft_id,
                stored=draft.body_sha256,
                recomputed=recomputed,
            )
        return draft

    def _assert_sendable(self, draft: MessageDraft) -> None:
        decision = draft.gate["decision"]
        if not decision.get("allowed"):
            raise GateRefused(
                f"classification gate denied draft {draft.draft_id}: "
                f"{decision.get('code')}",
                draft_id=draft.draft_id,
                gate_code=decision.get("code"),
                decision=decision,
            )
        if draft.grounding != GROUNDED:
            raise UngroundedDraft(
                f"draft {draft.draft_id} is {draft.grounding}: every number must be backed "
                "by a record before an outbound message is approved",
                draft_id=draft.draft_id,
                grounding=draft.grounding,
            )

    def _message_approval(self, draft: MessageDraft) -> Optional[Approval]:
        """Return the latest real approval row for a draft, if it still exists."""
        if draft.approval_id:
            try:
                return self._approvals.get(draft.approval_id)
            except DomainError:
                return None
        approvals = self._approvals.for_subject("message_send", draft.draft_id)
        return approvals[-1] if approvals else None

    # -- create -----------------------------------------------------------
    def update_body(
        self,
        draft_id: str,
        body_text: str,
        *,
        actor: str,
        actor_role: str = "",
    ) -> None:
        """Tripwire: there is deliberately no way to rewrite a draft's body.

        A message body is the artefact a human approves and a guest reads, so the
        only honest lifecycle is create → approve → send. Allowing an edit after
        creation would invalidate the digest, the grounding verdict and the gate
        decision taken on the approved text, and would let a body change *after*
        a human said yes. This method exists so that any future attempt to grow
        such a path fails loudly here instead of silently in production; it always
        raises :class:`BodyImmutable`. To change a body, create a new draft.
        """
        raise BodyImmutable(
            "a draft body is write-once; create a new draft instead of editing one",
            draft_id=draft_id,
            actor=actor,
            actor_role=actor_role,
        )

    def draft(
        self,
        *,
        case_id: str,
        template: str,
        channel: str,
        recipient_ref: str,
        bindings: Any,
        created_by: str,
        created_by_role: str,
        reason: str,
        booking_id: Optional[str] = None,
        locale: str = "id-ID",
        content_class: str = DEFAULT_CONTENT_CLASS,
        redacted: bool = False,
        at: Optional[datetime] = None,
    ) -> MessageDraft:
        """Render, ground, gate and persist one draft.

        Order matters: everything that can refuse the draft happens *before* the
        INSERT, so an
        :class:`UnboundPlaceholder` leaves the table untouched.
        """
        if template not in TEMPLATES:
            raise UnknownTemplate(
                f"unknown template {template!r}: known templates are {TEMPLATE_NAMES}",
                template=template,
                known=list(TEMPLATE_NAMES),
            )
        if channel not in CHANNELS:
            raise InvalidChannel(
                f"unknown channel {channel!r}: the schema allows {CHANNELS}",
                channel=channel,
                known=list(CHANNELS),
            )
        if not isinstance(locale, str) or len(locale) < 5 or locale.count("-") != 1:
            raise InvalidLocale(
                f"locale {locale!r} must look like 'id-ID' (the schema wants >= 5 chars)",
                locale=locale,
            )
        try:
            recipient = require_ref(recipient_ref, "recipient_ref")
        except DomainError as exc:
            raise InvalidRecipient(
                "recipient_ref must be a non-empty reference, not raw contact detail",
                recipient_ref="[DIRECT_IDENTIFIER_REDACTED]",
            ) from exc
        if len(recipient) < 3:
            raise InvalidRecipient(
                "recipient_ref must be at least 3 characters, per the schema",
                recipient_ref=recipient,
            )
        if contains_direct_identifiers(recipient):
            raise InvalidRecipient(
                "recipient_ref carries a direct identifier: store a reference, never the "
                "address itself",
                recipient_ref=redact(recipient),
            )
        actor, role = self._require_human(created_by, created_by_role, field_name="created_by")
        case_ref = self._check_exists("cases", "case_id", case_id, CaseNotFound, "case_id")
        booking_ref: Optional[str] = None
        if booking_id is not None:
            booking_ref = self._check_exists(
                "bookings", "booking_id", booking_id, BookingNotFound, "booking_id"
            )

        body, used = render_template(template, bindings)
        if not body or len(body) > 8000:
            raise BodyTooLong(
                f"rendered body must be 1..8000 characters, got {len(body)}",
                length=len(body),
            )

        grounding = ground(body, used, self._conn)

        fields = [
            ClassifiedField(name="body_text", classification=content_class, value=body, redacted=redacted),
            ClassifiedField(name="recipient_ref", classification="P1", value=recipient),
        ]
        decision = gate_outbound(
            fields,
            destination=CHANNEL_DESTINATIONS[channel],
            actor_role=role,
        )
        gate_payload = {
            "classification": content_class,
            "redacted": bool(redacted),
            "destination": CHANNEL_DESTINATIONS[channel],
            "actor_role": role,
            "decision": decision.as_dict(),
        }

        now = self._now(at)
        draft_id = self._ids.new("msg")
        digest = sha256_hex(body)
        values = (
            draft_id,
            case_ref,
            booking_ref,
            channel,
            template,
            locale,
            recipient,
            body,
            digest,
            grounding.to_json(),
            grounding.status,
            canonical_json(gate_payload),
            "DRAFT",
            None,
            actor,
            to_iso(now),
        )
        with _write(self._conn):
            placeholders = ", ".join(["?"] * len(_COLUMNS[:16]))
            self._conn.execute(
                f"INSERT INTO message_drafts ({', '.join(_COLUMNS[:16])}) VALUES ({placeholders})",  # noqa: S608
                values,
            )
            self._audit(
                action="message.draft",
                draft_id=draft_id,
                actor=actor,
                actor_role=role,
                reason=reason,
                after={
                    "case_id": case_ref,
                    "template": template,
                    "channel": channel,
                    "body_sha256": digest,
                    "grounding": grounding.status,
                    "gate_allowed": bool(decision.allowed),
                    "gate_code": decision.code,
                },
            )
        return self._draft(self._row(draft_id))

    # -- human approval ---------------------------------------------------
    def submit_for_approval(
        self,
        draft_id: str,
        *,
        actor: str,
        actor_role: str,
        reason: str,
        idempotency_key: str,
        required_role: str = "manager",
        at: Optional[datetime] = None,
    ) -> Approval:
        """Open a real ``message_send`` approval request. Never fakes a status."""
        actor_ref, role = self._require_human(actor, actor_role, field_name="actor")
        row = self._row(draft_id)
        draft = self._assert_intact(row)
        if draft.status in ("APPROVED", "SENT", "REJECTED", "EXPIRED"):
            raise DraftClosed(
                f"draft {draft.draft_id} is {draft.status}; a new draft is needed",
                draft_id=draft.draft_id,
                status=draft.status,
            )
        self._assert_sendable(draft)
        if draft.status == "PENDING_APPROVAL":
            approval = self._message_approval(draft)
            if approval is not None:
                return approval

        now = self._now(at)
        approval = self._approvals.request(
            subject_type="message_send",
            subject_id=draft.draft_id,
            action="send_outbound_message",
            requested_by=actor_ref,
            requested_role=role,
            reason=reason,
            payload={
                "draft_id": draft.draft_id,
                "case_id": draft.case_id,
                "template": draft.template,
                "channel": draft.channel,
                "recipient_ref": draft.recipient_ref,
                "body_sha256": draft.body_sha256,
                "grounding": draft.grounding,
                "gate_code": draft.gate["decision"].get("code"),
            },
            required_role=required_role,
            risk_level="MEDIUM",
            idempotency_key=idempotency_key,
            at=now,
        )
        with _write(self._conn):
            self._conn.execute(
                "UPDATE message_drafts SET status = ? WHERE draft_id = ?",
                ("PENDING_APPROVAL", draft.draft_id),
            )
            self._audit(
                action="message.submit",
                draft_id=draft.draft_id,
                actor=actor_ref,
                actor_role=role,
                reason=reason,
                after={"status": "PENDING_APPROVAL", "approval_subject": draft.draft_id},
            )
        return approval

    def approve(
        self,
        draft_id: str,
        *,
        approver: str,
        approver_role: str,
        note: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> MessageDraft:
        """Decide the draft's approval request. The author can never approve it."""
        approver_ref, role = self._require_human(approver, approver_role, field_name="approver")
        row = self._row(draft_id)
        draft = self._assert_intact(row)
        self._assert_sendable(draft)
        if draft.status != "PENDING_APPROVAL":
            raise DraftNotPending(
                f"draft {draft.draft_id} is {draft.status}: there is no approval request to decide",
                draft_id=draft.draft_id,
                status=draft.status,
            )
        approval = self._message_approval(draft)
        if approval is None:
            raise DraftNotPending(
                f"draft {draft.draft_id} is PENDING_APPROVAL but no approval request exists",
                draft_id=draft.draft_id,
                status=draft.status,
            )
        if approver_ref == draft.created_by:
            raise SelfApprovalRefused(
                f"{approver_ref!r} wrote draft {draft.draft_id} and cannot approve it",
                draft_id=draft.draft_id,
                actor=approver_ref,
            )

        approval = self._approvals.decide(
            approval.approval_id,
            approver=approver_ref,
            approver_role=role,
            decision="APPROVE",
            note=note,
            at=self._now(at),
        )
        if approval.status != "APPROVED":
            raise DraftNotApproved(
                f"approval {approval.approval_id} is {approval.status}, not APPROVED",
                draft_id=draft.draft_id,
                approval_id=approval.approval_id,
                status=approval.status,
            )
        now = self._now(at)
        with _write(self._conn):
            self._conn.execute(
                "UPDATE message_drafts SET status = ?, approval_id = ?, approved_by = ?, approved_at = ? "
                "WHERE draft_id = ?",
                ("APPROVED", approval.approval_id, approver_ref, to_iso(now), draft.draft_id),
            )
            self._audit(
                action="message.approve",
                draft_id=draft.draft_id,
                actor=approver_ref,
                actor_role=role,
                reason=note,
                after={
                    "status": "APPROVED",
                    "approval_id": approval.approval_id,
                    "body_sha256": draft.body_sha256,
                },
            )
        return self._draft(self._row(draft.draft_id))

    def reject(
        self,
        draft_id: str,
        *,
        approver: str,
        approver_role: str,
        note: str,
        at: Optional[datetime] = None,
    ) -> MessageDraft:
        approver_ref, role = self._require_human(approver, approver_role, field_name="approver")
        row = self._row(draft_id)
        draft = self._assert_intact(row)
        if draft.status != "PENDING_APPROVAL":
            raise DraftNotPending(
                f"draft {draft.draft_id} is {draft.status}: there is no approval request to decide",
                draft_id=draft.draft_id,
                status=draft.status,
            )
        approval = self._message_approval(draft)
        if approval is None:
            raise DraftNotPending(
                f"draft {draft.draft_id} is PENDING_APPROVAL but no approval request exists",
                draft_id=draft.draft_id,
                status=draft.status,
            )
        self._approvals.decide(
            approval.approval_id,
            approver=approver_ref,
            approver_role=role,
            decision="REJECT",
            note=note,
            at=self._now(at),
        )
        now = self._now(at)
        with _write(self._conn):
            self._conn.execute(
                "UPDATE message_drafts SET status = ?, approved_by = ?, approved_at = ? "
                "WHERE draft_id = ?",
                ("REJECTED", approver_ref, to_iso(now), draft.draft_id),
            )
            self._audit(
                action="message.reject",
                draft_id=draft.draft_id,
                actor=approver_ref,
                actor_role=role,
                reason=note,
                after={"status": "REJECTED"},
            )
        return self._draft(self._row(draft.draft_id))

    # -- sending (recording that a human sent it) -------------------------
    def mark_sent(
        self,
        draft_id: str,
        *,
        actor: str,
        actor_role: str,
        external_ref: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> MessageDraft:
        """Record that a human sent the approved body, and where to find proof.

        No network call, no SMTP, no WhatsApp API — ``external_ref`` is the human's
        own receipt reference (a message id, an email thread, a call log entry).
        """
        try:
            actor_ref = require_ref(actor, "actor")
        except DomainError as exc:
            raise InvalidActor("actor must be a human reference", actor=redact(str(actor))) from exc
        lowered = actor_ref.lower()
        if lowered == SYSTEM_ACTOR or any(
            lowered == marker or lowered.startswith(marker + ":") or lowered.startswith(marker + "-")
            for marker in MACHINE_ACTOR_MARKERS
        ):
            raise InvalidActor(
                f"actor {actor_ref!r} names a machine: sending is a human act",
                actor=actor_ref,
            )
        role = str(actor_role or "").strip().lower()
        if role not in SENDER_ROLES:
            raise InvalidSenderRole(
                f"role {role!r} may not record a send; allowed: {SENDER_ROLES}",
                role=role,
                allowed=list(SENDER_ROLES),
            )
        if external_ref is None or not str(external_ref).strip():
            raise MissingExternalRef(
                "external_ref is required: recording a send without proof turns the trail "
                "into an assertion",
                draft_id=draft_id,
            )
        try:
            proof = require_ref(external_ref, "external_ref")
        except DomainError as exc:
            raise MissingExternalRef(
                "external_ref must be a single reference token",
                external_ref=redact(str(external_ref)),
            ) from exc

        row = self._row(draft_id)
        draft = self._assert_intact(row)
        self._assert_sendable(draft)
        if draft.status != "APPROVED":
            raise DraftNotApproved(
                f"draft {draft.draft_id} is {draft.status}: only an APPROVED draft may be "
                "recorded as sent",
                draft_id=draft.draft_id,
                status=draft.status,
            )
        approval = self._message_approval(draft)
        if approval is None or approval.status != "APPROVED":
            raise DraftNotApproved(
                f"draft {draft.draft_id} carries no APPROVED human approval",
                draft_id=draft.draft_id,
                approval_id=draft.approval_id,
                approval_status=None if approval is None else approval.status,
            )
        now = self._now(at)
        with _write(self._conn):
            self._conn.execute(
                "UPDATE message_drafts SET status = ?, sent_by = ?, sent_at = ?, external_ref = ? "
                "WHERE draft_id = ?",
                ("SENT", actor_ref, to_iso(now), proof, draft.draft_id),
            )
            self._audit(
                action="message.sent",
                draft_id=draft.draft_id,
                actor=actor_ref,
                actor_role=role,
                reason=f"recorded send, external_ref {proof}",
                after={
                    "status": "SENT",
                    "external_ref": proof,
                    "body_sha256": draft.body_sha256,
                },
            )
        return self._draft(self._row(draft.draft_id))

    # -- reads ------------------------------------------------------------
    def get(self, draft_id: str) -> MessageDraft:
        return self._draft(self._row(draft_id))

    def drafts_for_case(self, case_id: str) -> tuple[MessageDraft, ...]:
        ref = require_ref(case_id, "case_id")
        rows = self._conn.execute(
            "SELECT * FROM message_drafts WHERE case_id = ? ORDER BY created_at, draft_id",
            (ref,),
        ).fetchall()
        return tuple(self._draft(r) for r in rows)

    def pending(self) -> tuple[MessageDraft, ...]:
        rows = self._conn.execute(
            "SELECT * FROM message_drafts WHERE status = 'PENDING_APPROVAL' "
            "ORDER BY created_at, draft_id"
        ).fetchall()
        return tuple(self._draft(r) for r in rows)

    def sent_between(self, start: Any, end: Any) -> tuple[MessageDraft, ...]:
        start_iso = to_iso(start) if isinstance(start, datetime) else to_iso(parse_iso(str(start)))
        end_iso = to_iso(end) if isinstance(end, datetime) else to_iso(parse_iso(str(end)))
        if start_iso > end_iso:
            raise InvalidRange(
                f"sent_between start {start_iso} is after end {end_iso}",
                start=start_iso,
                end=end_iso,
            )
        rows = self._conn.execute(
            "SELECT * FROM message_drafts WHERE status = 'SENT' AND sent_at >= ? AND sent_at <= ? "
            "ORDER BY sent_at, draft_id",
            (start_iso, end_iso),
        ).fetchall()
        return tuple(self._draft(r) for r in rows)

    def verify(self, draft_id: str) -> VerificationReport:
        """Recompute digest, grounding and gate from the stored row.

        This is the module's own audit: it trusts nothing it stored except the
        inputs (body, bindings, classification, destination) and reports every
        disagreement instead of raising on the first one.
        """
        draft = self._draft(self._row(draft_id))
        mismatches: list[str] = []
        details: dict[str, Any] = {}

        recomputed_sha = sha256_hex(draft.body_text)
        if recomputed_sha != draft.body_sha256:
            mismatches.append("BODY_TAMPERED")

        stored = _json(draft.claim_bindings)
        bindings = [Binding.from_dict(b) for b in stored.get("bindings", [])]
        recomputed = ground(draft.body_text, bindings, self._conn)
        if recomputed.status != draft.grounding:
            mismatches.append("GROUNDING_MISMATCH")
        if recomputed.unmatched:
            mismatches.append("UNMATCHED_CLAIMS")
        if recomputed.missing_sources:
            mismatches.append("MISSING_SOURCES")
        details["unmatched"] = [c.text for c in recomputed.unmatched]
        details["missing_sources"] = list(recomputed.missing_sources)
        details["claims"] = [c.as_dict() for c in recomputed.claims]

        gate = draft.gate
        fields = [
            ClassifiedField(
                name="body_text",
                classification=gate.get("classification", DEFAULT_CONTENT_CLASS),
                value=draft.body_text,
                redacted=bool(gate.get("redacted", False)),
            ),
            ClassifiedField(name="recipient_ref", classification="P1", value=draft.recipient_ref),
        ]
        recomputed_decision = gate_outbound(
            fields,
            destination=gate.get("destination", CHANNEL_DESTINATIONS.get(draft.channel, DESTINATION_CHAT)),
            actor_role=gate.get("actor_role"),
        )
        stored_allowed = bool(gate.get("decision", {}).get("allowed"))
        if bool(recomputed_decision.allowed) != stored_allowed:
            mismatches.append("GATE_MISMATCH")
        if recomputed_decision.code != gate.get("decision", {}).get("code"):
            mismatches.append("GATE_CODE_MISMATCH")
        details["gate_recomputed"] = recomputed_decision.as_dict()

        approval = self._message_approval(draft)
        approval_status = None if approval is None else approval.status

        if draft.status in ("APPROVED", "SENT"):
            if not draft.approved_by or not draft.approved_at:
                mismatches.append("APPROVAL_FIELDS_MISSING")
            if approval is None:
                mismatches.append("APPROVAL_MISSING")
            elif approval_status != "APPROVED":
                mismatches.append("APPROVAL_NOT_APPROVED")
            if draft.approved_by and draft.approved_by == draft.created_by:
                mismatches.append("SELF_APPROVED")
            if draft.grounding != GROUNDED:
                mismatches.append("UNGROUNDED_SENT")
        if draft.status == "PENDING_APPROVAL" and approval is None:
            mismatches.append("APPROVAL_REQUEST_MISSING")
        if draft.status == "SENT":
            if not draft.sent_by or not draft.sent_at:
                mismatches.append("SENT_FIELDS_MISSING")
            if not draft.external_ref:
                mismatches.append("SENT_WITHOUT_EXTERNAL_REF")

        return VerificationReport(
            draft_id=draft.draft_id,
            mismatches=tuple(mismatches),
            details=details,
            stored_body_sha256=draft.body_sha256,
            recomputed_body_sha256=recomputed_sha,
            stored_grounding=draft.grounding,
            recomputed_grounding=recomputed.status,
            stored_gate_allowed=stored_allowed,
            recomputed_gate_allowed=bool(recomputed_decision.allowed),
            approval_status=approval_status,
        )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _json(text: str) -> Mapping[str, Any]:
    import json

    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:  # pragma: no cover - corrupt row
        raise MessageError("stored claim_bindings is not valid JSON", raw=text[:120]) from exc
    if not isinstance(data, Mapping):
        raise MessageError("stored claim_bindings must be a JSON object", got=type(data).__name__)
    return data
