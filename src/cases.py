"""Case lifecycle: guest requests over ``cases``, ``case_messages`` and ``case_events``.

A *case* is one guest request — a WhatsApp message, a website form, a phone note.
This module is the only write path for case state, and it is deliberately boring:
every command validates its inputs first, moves the status through one
table-driven state machine, and appends **exactly one** ``case_events`` row in the
same transaction as the change itself. One command = one row, enforced twice: by
``CaseService`` (which refuses a ``command_id`` it has already written) and by the
DDL (``CONSTRAINT uq_case_events_command UNIQUE (case_id, command_id)``). A retried
request therefore can never double the history — it is rejected with
``DUPLICATE_COMMAND`` and nothing is written.

Invariants enforced here (each has a failing test in ``tests/test_cases.py``)
----------------------------------------------------------------------------
1. A status change writes exactly one event row with ``seq = max(seq) + 1`` for
   that case, and ``command_id`` is unique per case.
2. A transition outside :data:`CASE_TRANSITIONS` is refused and writes nothing.
3. A ``CLOSED_*`` case refuses ``transition()`` and ``record_inbound()``
   (``CASE_CLOSED``); the only permitted move is the ``CLOSED_DUPLICATE``
   correction path named in :data:`CASE_TRANSITIONS`.
4. ``body_sha256`` is the hash of the **raw** body. ``P3``/``P4`` bodies are never
   stored (``body_redacted IS NULL``), ``P2`` bodies are stored only through
   :func:`primitives.redact`, ``P0``/``P1`` bodies are stored intact because they
   are non-personal by definition.
5. ``first_response_due_at``/``quote_due_at`` are derived from ``created_at`` plus
   the SLA policy windows, and :meth:`CaseService.overdue` is strict
   (``now > due_at``) and read-only.
6. An unknown SLA policy and a too-short idempotency key are refused before any
   row is written.
7. ``guest_ref`` passes :func:`primitives.require_ref` (and the DDL's
   ``length(guest_ref) >= 3``).

Inbound content is never trusted: every stored message carries ``untrusted = 1``.
Conversation text is data, never an instruction.

Decisions recorded on purpose (see the report accompanying this module)
-----------------------------------------------------------------------
* ``mark_quoted`` records the quote time and, when the case is not ``QUOTED`` yet,
  moves it there through :data:`CASE_TRANSITIONS` (one command, one event).
  Re-quoting an already ``QUOTED`` case only updates ``quoted_at``.
* ``mark_first_response`` can be recorded once; a second attempt raises
  ``CASE_ALREADY_RESPONDED`` rather than silently rewriting the first answer.
* ``link_booking`` links the booking row without moving the status: the status
  still moves via ``transition("BOOKED")``, so the state machine stays the only
  authority on status.
* ``overdue`` detects only; writing ``sla_breaches`` rows is a later phase's job.
* ``ESCALATED`` is the only open status that may move anywhere except itself.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterator, Mapping, Optional, Sequence

from audit import append_event
from primitives import (
    UTC,
    DomainError,
    IdFactory,
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
    "CASE_STATUSES",
    "CLOSED_STATUSES",
    "TERMINAL_STATUSES",
    "CASE_CHANNELS",
    "PRIORITIES",
    "CONTENT_CLASSES",
    "UNSTORED_CONTENT_CLASSES",
    "REDACTED_CONTENT_CLASS",
    "DIRECTIONS",
    "EVENT_KINDS",
    "CASE_TRANSITIONS",
    "SLA_POLICIES",
    "SLA_CEILING_MINUTES",
    "DEFAULT_SLA_POLICY_BY_CHANNEL",
    "SlaPolicy",
    "Case",
    "CaseMessage",
    "CaseEvent",
    "CaseService",
    "CaseError",
    "CaseNotFound",
    "InvalidCaseTransition",
    "CaseClosed",
    "DuplicateCommand",
    "DuplicateCase",
    "SlaPolicyUnknown",
    "InvalidChannel",
    "InvalidGuestRef",
    "InvalidContentClass",
]

# --------------------------------------------------------------------------- #
# Vocabulary (mirrors the CHECK enums of src/schema/001_initial.sql)
# --------------------------------------------------------------------------- #
#: ``cases.status`` in declaration order. ``tests/test_cases.py`` parses the DDL
#: text and fails if this tuple drifts from it.
CASE_STATUSES: tuple[str, ...] = (
    "NEW",
    "TRIAGED",
    "MATCHING",
    "AWAITING_EVIDENCE",
    "QUOTED",
    "HOLD_PENDING",
    "BOOKED",
    "ESCALATED",
    "CLOSED_WON",
    "CLOSED_LOST",
    "CLOSED_DUPLICATE",
)

#: Statuses that end a case's working life.
CLOSED_STATUSES: tuple[str, ...] = tuple(s for s in CASE_STATUSES if s.startswith("CLOSED_"))

#: Statuses from which no further work happens. Equal to :data:`CLOSED_STATUSES`:
#: a closed case is terminal, and the single exception (the ``CLOSED_DUPLICATE``
#: correction) is expressed in :data:`CASE_TRANSITIONS`, not by loosening this.
TERMINAL_STATUSES: tuple[str, ...] = CLOSED_STATUSES

#: ``cases.channel`` — lowercase vocabulary column, not a status.
CASE_CHANNELS: tuple[str, ...] = (
    "website",
    "email",
    "whatsapp_business",
    "dashboard",
    "phone",
)

#: ``cases.priority``.
PRIORITIES: tuple[str, ...] = ("LOW", "NORMAL", "HIGH", "URGENT")

#: ``case_messages.content_class``.
CONTENT_CLASSES: tuple[str, ...] = ("P0", "P1", "P2", "P3", "P4")

#: Classes whose body is never stored: only the hash survives.
UNSTORED_CONTENT_CLASSES: tuple[str, ...] = ("P3", "P4")

#: The one class that must be stored redacted (DDL CHECK ``ck_case_messages_p2_redacted``).
REDACTED_CONTENT_CLASS: str = "P2"

#: ``case_messages.direction``. This module only writes inbound traffic; the
#: constant exists so readers of the timeline do not invent the spelling.
DIRECTIONS: tuple[str, ...] = ("INBOUND", "OUTBOUND")
DIRECTION_INBOUND: str = "INBOUND"

#: ``case_events.kind`` — mirrors the DDL enum verbatim.
EVENT_KINDS: tuple[str, ...] = (
    "CREATED",
    "MESSAGE_IN",
    "MESSAGE_OUT",
    "INTAKE_PROPOSED",
    "INTAKE_REVIEWED",
    "MATCH_RUN",
    "QUOTE_LINKED",
    "HOLD_LINKED",
    "BOOKING_LINKED",
    "STATUS_CHANGED",
    "APPROVAL_REQUESTED",
    "APPROVAL_DECIDED",
    "DRAFT_CREATED",
    "SLA_BREACH",
    "ESCALATED",
    "NOTE",
)

# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #
#: Legal status moves. Closed statuses accept only the duplicate correction, so a
#: reopened case is impossible by construction (a new case is opened instead).
CASE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "NEW": ("TRIAGED", "ESCALATED", "CLOSED_LOST", "CLOSED_DUPLICATE"),
    "TRIAGED": ("MATCHING", "AWAITING_EVIDENCE", "ESCALATED", "CLOSED_LOST", "CLOSED_DUPLICATE"),
    "MATCHING": ("AWAITING_EVIDENCE", "QUOTED", "ESCALATED", "CLOSED_LOST", "CLOSED_DUPLICATE"),
    "AWAITING_EVIDENCE": ("MATCHING", "QUOTED", "ESCALATED", "CLOSED_LOST", "CLOSED_DUPLICATE"),
    "QUOTED": ("HOLD_PENDING", "ESCALATED", "CLOSED_LOST", "CLOSED_DUPLICATE"),
    "HOLD_PENDING": ("BOOKED", "ESCALATED", "CLOSED_LOST", "CLOSED_DUPLICATE"),
    "BOOKED": ("CLOSED_WON", "ESCALATED", "CLOSED_LOST", "CLOSED_DUPLICATE"),
    # Escalation is how a human puts a case back on track, so it may reach any
    # other status (a win still needs the BOOKED step to be meaningful, but the
    # transition table does not pretend to police that: link_booking does).
    "ESCALATED": tuple(s for s in CASE_STATUSES if s != "ESCALATED"),
    "CLOSED_WON": ("CLOSED_DUPLICATE",),
    "CLOSED_LOST": ("CLOSED_DUPLICATE",),
    "CLOSED_DUPLICATE": ("CLOSED_DUPLICATE",),
}


def _assert_contract() -> None:
    """Fail closed at import time if the constants are internally inconsistent."""
    if tuple(CASE_TRANSITIONS) != CASE_STATUSES:
        raise DomainError(
            "CASE_TRANSITIONS harus memuat setiap status tepat sekali",
            code="INVALID_CASE_CONTRACT",
        )
    for status, targets in CASE_TRANSITIONS.items():
        for target in targets:
            if target not in CASE_STATUSES:
                raise DomainError(
                    f"target transisi tidak dikenal: {status} -> {target}",
                    code="INVALID_CASE_CONTRACT",
                )
        if status in CLOSED_STATUSES and targets != ("CLOSED_DUPLICATE",):
            raise DomainError(
                f"status {status} hanya boleh dikoreksi ke CLOSED_DUPLICATE",
                code="INVALID_CASE_CONTRACT",
            )
        if status not in CLOSED_STATUSES and status in targets:
            raise DomainError(
                f"transisi ke diri sendiri tidak diizinkan: {status}",
                code="INVALID_CASE_CONTRACT",
            )


_assert_contract()

# --------------------------------------------------------------------------- #
# SLA policies
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SlaPolicy:
    """Service windows for one policy name, in minutes from ``created_at``."""

    name: str
    first_response_minutes: int
    quote_minutes: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or len(self.name) < 3:
            raise DomainError(
                "nama SLA policy minimal 3 karakter",
                code="SLA_POLICY_INVALID",
                name=self.name,
            )
        for field_name, value in (
            ("first_response_minutes", self.first_response_minutes),
            ("quote_minutes", self.quote_minutes),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise DomainError(
                    f"{field_name} harus integer menit",
                    code="SLA_POLICY_INVALID",
                    field=field_name,
                )
            if value < 1:
                raise DomainError(
                    f"{field_name} minimal 1 menit",
                    code="SLA_POLICY_INVALID",
                    field=field_name,
                )
        if self.first_response_minutes > self.quote_minutes:
            # The DDL CHECK ck_cases_sla_window demands
            # created_at < first_response_due_at <= quote_due_at.
            raise DomainError(
                "first_response_minutes tidak boleh melebihi quote_minutes",
                code="SLA_POLICY_INVALID",
                name=self.name,
            )

    def due_at(self, created_at: datetime) -> tuple[datetime, datetime]:
        """``(first_response_due_at, quote_due_at)`` for a case created at ``created_at``."""
        return (
            created_at + timedelta(minutes=self.first_response_minutes),
            created_at + timedelta(minutes=self.quote_minutes),
        )


#: The policy catalogue. Names double as the ``cases.sla_policy`` value (the DDL
#: only demands ``length(sla_policy) >= 3``).
#: SLA per channel. The **ceiling** is the business decision of 2026-10-02 (owner): first
#: response within 15 minutes, a quote within 2 hours (120 minutes). A channel may be
#: *stricter* than the ceiling (whatsapp 10, agent 5) and may never be looser — a guard test
#: pins that, because an SLA that quietly drifts above the approved number is an untested
#: promise to the guest.
SLA_POLICIES: dict[str, SlaPolicy] = {
    "web_v1": SlaPolicy(name="web_v1", first_response_minutes=15, quote_minutes=120),
    "email_v1": SlaPolicy(name="email_v1", first_response_minutes=15, quote_minutes=120),
    "whatsapp_v1": SlaPolicy(name="whatsapp_v1", first_response_minutes=10, quote_minutes=120),
    "agent_v1": SlaPolicy(name="agent_v1", first_response_minutes=5, quote_minutes=120),
}

#: The approved ceiling every policy above must stay under (owner, 2026-10-02).
SLA_CEILING_MINUTES: tuple[int, int] = (15, 120)

#: Channel -> policy applied when ``open_case`` is not given an explicit one.
DEFAULT_SLA_POLICY_BY_CHANNEL: dict[str, str] = {
    "website": "web_v1",
    "email": "email_v1",
    "whatsapp_business": "whatsapp_v1",
    "dashboard": "agent_v1",
    "phone": "agent_v1",
}

# --------------------------------------------------------------------------- #
# Errors (each code is a machine-readable contract; never parse the message)
# --------------------------------------------------------------------------- #
class CaseError(DomainError):
    """Base class for every deliberate case-lifecycle failure."""

    code = "CASE_ERROR"


class CaseNotFound(CaseError):
    code = "CASE_NOT_FOUND"


class InvalidCaseTransition(CaseError):
    code = "INVALID_CASE_TRANSITION"


class CaseClosed(CaseError):
    code = "CASE_CLOSED"


class DuplicateCommand(CaseError):
    """The same ``command_id`` was already used for this case."""

    code = "DUPLICATE_COMMAND"


class DuplicateCase(CaseError):
    """The same idempotency key already opened a case."""

    code = "DUPLICATE_CASE"


class SlaPolicyUnknown(CaseError):
    code = "SLA_POLICY_UNKNOWN"


class InvalidChannel(CaseError):
    code = "INVALID_CHANNEL"


class InvalidGuestRef(CaseError):
    code = "INVALID_GUEST_REF"


class InvalidContentClass(CaseError):
    code = "INVALID_CONTENT_CLASS"


# Supporting codes (kept separate so the ten contract codes stay unambiguous).
class InvalidRef(CaseError):
    code = "INVALID_REF"


class InvalidPriority(CaseError):
    code = "INVALID_PRIORITY"


class InvalidOutcome(CaseError):
    code = "INVALID_OUTCOME"


class InvalidCommandId(CaseError):
    code = "INVALID_COMMAND_ID"


class InvalidIdempotencyKey(CaseError):
    code = "INVALID_IDEMPOTENCY_KEY"


class InvalidAttachmentCount(CaseError):
    code = "INVALID_ATTACHMENT_COUNT"


class InvalidCloseReason(CaseError):
    code = "INVALID_CLOSE_REASON"


class InvalidActor(CaseError):
    code = "INVALID_ACTOR"


class InvalidBody(CaseError):
    code = "INVALID_MESSAGE_BODY"


class InvalidTimestamp(CaseError):
    code = "INVALID_TIMESTAMP"


class BookingNotFound(CaseError):
    code = "BOOKING_NOT_FOUND"


class CaseAlreadyResponded(CaseError):
    code = "CASE_ALREADY_RESPONDED"


# --------------------------------------------------------------------------- #
# Bounds and defaults
# --------------------------------------------------------------------------- #
DEFAULT_PRIORITY: str = "NORMAL"
DEFAULT_ACTOR: str = "system:case_service"
DEFAULT_ACTOR_ROLE: str = "system"
DEFAULT_AGENT_ROLE: str = "agent"
MIN_GUEST_REF_LEN: int = 3
MIN_IDEMPOTENCY_KEY_LEN: int = 8
MIN_COMMAND_ID_LEN: int = 8
MIN_CLOSE_REASON_LEN: int = 3
MAX_BODY_CHARS: int = 20_000
MAX_FIELD_CHARS: int = 120

#: ``close(outcome=...)`` -> target status. The outcome is the business word, the
#: status is the storage word; only this mapping knows both.
CLOSE_OUTCOMES: dict[str, str] = {
    "WON": "CLOSED_WON",
    "LOST": "CLOSED_LOST",
    "DUPLICATE": "CLOSED_DUPLICATE",
}

#: The only status a closed case may still be corrected to.
CORRECTION_STATUS: str = "CLOSED_DUPLICATE"

#: Kinds that mean "the status changed". Non-status events carry NULL from/to so
#: the timeline never pretends a message moved the state.
STATUS_EVENT_KIND: str = "STATUS_CHANGED"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Case:
    """One guest request. Mirrors ``cases`` exactly."""

    case_id: str
    channel: str
    channel_ref: Optional[str]
    guest_ref: str
    status: str
    priority: str
    sla_policy: str
    owner_actor: Optional[str]
    booking_id: Optional[str]
    first_response_due_at: str
    quote_due_at: str
    first_responded_at: Optional[str]
    quoted_at: Optional[str]
    created_at: str
    updated_at: str
    closed_at: Optional[str]
    close_reason: Optional[str]

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | sqlite3.Row) -> "Case":
        return cls(
            case_id=str(row["case_id"]),
            channel=str(row["channel"]),
            channel_ref=row["channel_ref"],
            guest_ref=str(row["guest_ref"]),
            status=str(row["status"]),
            priority=str(row["priority"]),
            sla_policy=str(row["sla_policy"]),
            owner_actor=row["owner_actor"],
            booking_id=row["booking_id"],
            first_response_due_at=str(row["first_response_due_at"]),
            quote_due_at=str(row["quote_due_at"]),
            first_responded_at=row["first_responded_at"],
            quoted_at=row["quoted_at"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            closed_at=row["closed_at"],
            close_reason=row["close_reason"],
        )


@dataclass(frozen=True)
class CaseMessage:
    """One stored message. The body is stored minimised — see the module docstring."""

    case_message_id: str
    case_id: str
    direction: str
    channel: str
    external_ref: Optional[str]
    sender_ref: str
    received_at: str
    content_class: str
    body_redacted: Optional[str]
    body_sha256: str
    attachments: int
    untrusted: bool
    created_at: str

    @property
    def body_stored(self) -> bool:
        """Whether any body text survives in storage (``False`` for P3/P4)."""
        return self.body_redacted is not None

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | sqlite3.Row) -> "CaseMessage":
        return cls(
            case_message_id=str(row["case_message_id"]),
            case_id=str(row["case_id"]),
            direction=str(row["direction"]),
            channel=str(row["channel"]),
            external_ref=row["external_ref"],
            sender_ref=str(row["sender_ref"]),
            received_at=str(row["received_at"]),
            content_class=str(row["content_class"]),
            body_redacted=row["body_redacted"],
            body_sha256=str(row["body_sha256"]),
            attachments=int(row["attachment_count"]),
            untrusted=bool(row["untrusted"]),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True)
class CaseEvent:
    """One append-only timeline row. ``refs`` are opaque ids (never free text)."""

    case_event_id: str
    case_id: str
    seq: int
    kind: str
    from_status: Optional[str]
    to_status: Optional[str]
    actor: str
    actor_role: str
    command_id: str
    reason: Optional[str]
    refs: tuple[str, ...]
    occurred_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | sqlite3.Row) -> "CaseEvent":
        raw = row["refs"]
        parsed = json.loads(raw) if raw else []
        return cls(
            case_event_id=str(row["case_event_id"]),
            case_id=str(row["case_id"]),
            seq=int(row["seq"]),
            kind=str(row["kind"]),
            from_status=row["from_status"],
            to_status=row["to_status"],
            actor=str(row["actor"]),
            actor_role=str(row["actor_role"]),
            command_id=str(row["command_id"]),
            reason=row["reason"],
            refs=tuple(str(item) for item in parsed),
            occurred_at=str(row["occurred_at"]),
        )


# --------------------------------------------------------------------------- #
# Boundary validators (fail closed, Indonesian messages, UPPERCASE codes)
# --------------------------------------------------------------------------- #
def _require_bounded_ref(value: Any, field_name: str, *, minimum: int = 1, code: str = "INVALID_REF") -> str:
    """``primitives.require_ref`` plus the DDL's ``length >= minimum`` demand."""
    if not isinstance(value, str):
        raise InvalidRef(f"{field_name} harus teks", code=code, field=field_name)
    try:
        ref = require_ref(value, field_name)
    except DomainError as exc:
        raise InvalidRef(f"{field_name} tidak valid: {exc.message}", code=code, field=field_name) from exc
    if len(ref) < minimum:
        raise InvalidRef(
            f"{field_name} minimal {minimum} karakter",
            code=code,
            field=field_name,
            length=len(ref),
        )
    return ref


def _require_guest_ref(value: Any) -> str:
    """Validate ``guest_ref`` through :func:`primitives.require_ref` (invariant 7)."""
    return _require_bounded_ref(
        value,
        "guest_ref",
        minimum=MIN_GUEST_REF_LEN,
        code=InvalidGuestRef.code,
    )


def _require_text_field(value: Any, field_name: str, *, max_len: int = MAX_FIELD_CHARS, code: str = "INVALID_REF") -> str:
    try:
        return require_text(value, field_name, max_len=max_len)
    except DomainError as exc:
        raise InvalidRef(f"{field_name} wajib diisi", code=code, field=field_name) from exc


def _require_channel(channel: Any) -> str:
    if not isinstance(channel, str) or channel not in CASE_CHANNELS:
        raise InvalidChannel(
            "channel tidak dikenal: " + str(channel),
            channel=str(channel),
            allowed=CASE_CHANNELS,
        )
    return channel


def _require_priority(priority: Any) -> str:
    if not isinstance(priority, str) or priority not in PRIORITIES:
        raise InvalidPriority(
            "priority tidak dikenal: " + str(priority),
            priority=str(priority),
            allowed=PRIORITIES,
        )
    return priority


def _require_content_class(content_class: Any) -> str:
    if not isinstance(content_class, str) or content_class not in CONTENT_CLASSES:
        raise InvalidContentClass(
            "content_class tidak dikenal: " + str(content_class),
            content_class=str(content_class),
            allowed=CONTENT_CLASSES,
        )
    return content_class


def _require_attachments(attachments: Any) -> int:
    if isinstance(attachments, bool) or not isinstance(attachments, int) or attachments < 0:
        raise InvalidAttachmentCount(
            "attachments harus integer >= 0",
            attachments=str(attachments),
        )
    return attachments


def _require_command_id(command_id: Any) -> str:
    ref = _require_bounded_ref(
        command_id,
        "command_id",
        minimum=MIN_COMMAND_ID_LEN,
        code=InvalidCommandId.code,
    )
    return ref


def _require_idempotency_key(key: Any) -> str:
    ref = _require_bounded_ref(
        key,
        "idempotency_key",
        minimum=MIN_IDEMPOTENCY_KEY_LEN,
        code=InvalidIdempotencyKey.code,
    )
    return ref


def _require_actor(actor: Any, field_name: str = "actor") -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise InvalidActor(f"{field_name} wajib diisi", field=field_name)
    return _require_text_field(actor, field_name, code=InvalidActor.code)


def _require_role(actor_role: Any) -> str:
    return _require_actor(actor_role, "actor_role")


def _require_body(body: Any) -> str:
    if not isinstance(body, str) or not body.strip():
        raise InvalidBody("body wajib diisi", code=InvalidBody.code)
    try:
        require_text(body, "body", max_len=MAX_BODY_CHARS)
    except DomainError as exc:
        raise InvalidBody(f"body tidak valid: {exc.message}", code=InvalidBody.code) from exc
    return body


def _require_refs(refs: Any) -> tuple[str, ...]:
    if refs is None:
        return ()
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
        raise InvalidRef("refs harus urutan id", code=InvalidRef.code)
    out: list[str] = []
    for item in refs:
        out.append(_require_bounded_ref(item, "refs[]", minimum=1, code=InvalidRef.code))
    return tuple(out)


def _body_fields(content_class: str, body: str) -> tuple[str, Optional[str], str]:
    """Return ``(raw_body, body_redacted, body_sha256)`` for a validated body.

    ``body_sha256`` hashes the **raw** text as received (invariant 4); the stored
    copy is the NFC-normalised text, redacted for ``P2`` and absent for ``P3``/``P4``.
    """
    raw = body
    digest = sha256_hex(raw)
    if content_class in UNSTORED_CONTENT_CLASSES:
        return raw, None, digest
    normalised = require_text(raw, "body", max_len=MAX_BODY_CHARS)
    if content_class == REDACTED_CONTENT_CLASS:
        return raw, redact(normalised), digest
    return raw, normalised, digest


def _resolve_sla_policy(channel: str, sla_policy: Optional[str]) -> SlaPolicy:
    """Fail closed on an unknown policy name (invariant 6) — before any write."""
    if sla_policy is None:
        policy_name: Optional[str] = DEFAULT_SLA_POLICY_BY_CHANNEL.get(channel)
        if policy_name is None:  # pragma: no cover - channels are validated first
            raise SlaPolicyUnknown("channel tanpa SLA policy default", channel=channel)
    else:
        if not isinstance(sla_policy, str) or sla_policy not in SLA_POLICIES:
            raise SlaPolicyUnknown(
                "sla_policy tidak dikenal: " + str(sla_policy),
                sla_policy=str(sla_policy),
                allowed=tuple(SLA_POLICIES),
            )
        policy_name = sla_policy
    policy = SLA_POLICIES.get(policy_name)
    if policy is None:  # pragma: no cover - guarded above
        raise SlaPolicyUnknown("sla_policy tidak dikenal: " + str(policy_name), sla_policy=policy_name)
    return policy


def _as_datetime(value: Any, field_name: str) -> datetime:
    """Accept an aware datetime or an ISO-8601 string; refuse everything else."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise InvalidTimestamp(f"{field_name} harus timezone-aware", field=field_name)
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            return parse_iso(value).astimezone(UTC)
        except DomainError as exc:
            raise InvalidTimestamp(f"{field_name} bukan timestamp ISO-8601", field=field_name) from exc
        except ValueError as exc:  # pragma: no cover - parse_iso raises DomainError
            raise InvalidTimestamp(f"{field_name} bukan timestamp ISO-8601", field=field_name) from exc
    raise InvalidTimestamp(f"{field_name} harus timestamp", field=field_name)


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #
class CaseService:
    """Transactional write path for ``cases``/``case_messages``/``case_events``.

    One instance owns one connection. Every write runs inside ``BEGIN IMMEDIATE``
    (the same discipline as :mod:`reservation_kernel`), so the row that records a
    change and the change itself are committed together or not at all.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        *,
        agency_id: str = "agency_default",
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id")
        self._in_tx = False
        self._ensure_agency_scope()

    def _ensure_agency_scope(self) -> None:
        now = self._stamp()
        self.conn.execute(
            """
            INSERT INTO agencies (agency_id, name, status, created_at, updated_at)
            VALUES (?, ?, 'ACTIVE', ?, ?)
            ON CONFLICT(agency_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (self.agency_id, self.agency_id, now, now),
        )

    # -- transactions ------------------------------------------------------- #
    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes the lock before it reads."""
        if self._in_tx:
            raise DomainError(
                "transaksi bersarang tidak didukung",
                code="CASE_NESTED_TRANSACTION",
            )
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            self._in_tx = False
            raise
        self.conn.execute("COMMIT")
        self._in_tx = False

    # -- time / ids --------------------------------------------------------- #
    def _now(self) -> datetime:
        return self.clock.now().astimezone(UTC)

    def _stamp(self) -> str:
        return to_iso(self._now())

    def _normalise_now(self, now: Any) -> datetime:
        """``now`` for read-only queries: default clock, strict validation otherwise."""
        if now is None:
            return self._now()
        return _as_datetime(now, "now")

    # -- reads -------------------------------------------------------------- #
    def _require_case_row(self, case_id: Any) -> sqlite3.Row:
        ref = _require_bounded_ref(case_id, "case_id", minimum=1, code=InvalidRef.code)
        row = self.conn.execute(
            "SELECT * FROM cases WHERE case_id = ? AND agency_id = ?",
            (ref, self.agency_id),
        ).fetchone()
        if row is None:
            raise CaseNotFound(f"case tidak ditemukan: {ref}", case_id=ref)
        return row

    def get(self, case_id: str) -> Case:
        """Return the case, or raise ``CASE_NOT_FOUND``."""
        return Case.from_row(self._require_case_row(case_id))

    def timeline(self, case_id: str) -> list[CaseEvent]:
        """Every event for the case, ordered by ``seq``. Empty case -> ``CASE_NOT_FOUND``."""
        row = self._require_case_row(case_id)
        rows = self.conn.execute(
            "SELECT * FROM case_events WHERE case_id = ? ORDER BY seq ASC",
            (row["case_id"],),
        ).fetchall()
        return [CaseEvent.from_row(r) for r in rows]

    def first_response_overdue(self, now: Any = None) -> list[Case]:
        """Cases whose first-response window closed and which were never answered."""
        return self._overdue_cases("first_responded_at", "first_response_due_at", now)

    def quote_overdue(self, now: Any = None) -> list[Case]:
        """Cases whose quote window closed and which were never quoted."""
        return self._overdue_cases("quoted_at", "quote_due_at", now)

    def overdue(self, now: Any = None) -> list[Case]:
        """Cases late on either window (union of the two, one row per case).

        Strictly ``now > due_at``: at exactly ``due_at`` a case is not yet late.
        Read-only — ``sla_breaches`` rows are a later phase's job.
        """
        moment = to_iso(self._normalise_now(now))
        closed = ",".join("?" for _ in CLOSED_STATUSES)
        rows = self.conn.execute(
            f"""
            SELECT * FROM cases
             WHERE agency_id = ?
               AND status NOT IN ({closed})
               AND (
                     (first_responded_at IS NULL AND first_response_due_at < ?)
                  OR (quoted_at IS NULL AND quote_due_at < ?)
               )
             ORDER BY first_response_due_at ASC, case_id ASC
            """,
            (self.agency_id, *CLOSED_STATUSES, moment, moment),
        ).fetchall()
        return [Case.from_row(r) for r in rows]

    def _overdue_cases(self, answered_column: str, due_column: str, now: Any) -> list[Case]:
        moment = to_iso(self._normalise_now(now))
        closed = ",".join("?" for _ in CLOSED_STATUSES)
        rows = self.conn.execute(
            f"""
            SELECT * FROM cases
             WHERE agency_id = ?
               AND status NOT IN ({closed})
               AND {answered_column} IS NULL
               AND {due_column} < ?
             ORDER BY {due_column} ASC, case_id ASC
            """,
            (self.agency_id, *CLOSED_STATUSES, moment),
        ).fetchall()
        return [Case.from_row(r) for r in rows]

    # -- events ------------------------------------------------------------- #
    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        case_id: str,
        kind: str,
        from_status: Optional[str],
        to_status: Optional[str],
        actor: str,
        actor_role: str,
        command_id: str,
        reason: Optional[str],
        refs: tuple[str, ...],
        occurred_at: str,
    ) -> str:
        """Append exactly one event row (``seq = max(seq) + 1``) — invariant 1.

        The duplicate-command check runs **before** the caller writes anything, so
        a replay leaves the database untouched rather than being rolled back.
        """
        if kind not in EVENT_KINDS:  # pragma: no cover - callers only pass constants
            raise DomainError(f"kind event tidak dikenal: {kind}", code="INVALID_EVENT_KIND")
        existing = conn.execute(
            "SELECT case_event_id FROM case_events WHERE case_id = ? AND command_id = ?",
            (case_id, command_id),
        ).fetchone()
        if existing is not None:
            raise DuplicateCommand(
                f"command_id sudah dipakai untuk case ini: {command_id}",
                case_id=case_id,
                command_id=command_id,
            )
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM case_events WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        seq = int(row["last_seq"]) + 1
        event_id = self.ids.new("cevt")
        conn.execute(
            """
            INSERT INTO case_events (
                case_event_id, case_id, seq, kind, from_status, to_status, actor,
                actor_role, command_id, reason, refs, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                case_id,
                seq,
                kind,
                from_status,
                to_status,
                actor,
                actor_role,
                command_id,
                reason,
                canonical_json(list(refs)),
                occurred_at,
            ),
        )
        return event_id

    def _fetch_case(self, conn: sqlite3.Connection, case_id: str) -> Case:
        return Case.from_row(self._require_case_row(case_id))

    def _touch(self, conn: sqlite3.Connection, case_id: str, moment: str) -> None:
        conn.execute("UPDATE cases SET updated_at = ? WHERE case_id = ?", (moment, case_id))

    # -- writes: open ------------------------------------------------------- #
    def open_case(
        self,
        *,
        channel: str,
        guest_ref: str,
        content_class: str,
        body: str,
        idempotency_key: str,
        priority: str = DEFAULT_PRIORITY,
        sla_policy: Optional[str] = None,
        channel_ref: Optional[str] = None,
        owner_actor: Optional[str] = None,
        sender_ref: Optional[str] = None,
        external_ref: Optional[str] = None,
        received_at: Any = None,
        attachments: int = 0,
    ) -> tuple[Case, CaseMessage]:
        """Open a case plus its first inbound message, in one transaction.

        ``idempotency_key`` is also the ``command_id`` of the ``CREATED`` event: a
        replayed open with the same key raises ``DUPLICATE_CASE`` instead of
        producing a second case (the DDL's ``uq_cases_idempotency`` is the net
        below that check).
        """
        channel_value = _require_channel(channel)
        guest = _require_guest_ref(guest_ref)
        priority_value = _require_priority(priority)
        policy = _resolve_sla_policy(channel_value, sla_policy)
        class_value = _require_content_class(content_class)
        key = _require_idempotency_key(idempotency_key)
        channel_ref_value = None if channel_ref is None else _require_bounded_ref(channel_ref, "channel_ref")
        owner = None if owner_actor is None else _require_bounded_ref(owner_actor, "owner_actor")
        sender = guest if sender_ref is None else _require_bounded_ref(sender_ref, "sender_ref", minimum=MIN_GUEST_REF_LEN)
        external = None if external_ref is None else _require_bounded_ref(external_ref, "external_ref")
        attachment_count = _require_attachments(attachments)
        raw_body = _require_body(body)
        _raw, body_redacted, body_sha256 = _body_fields(class_value, raw_body)

        now = self._now()
        created_at = to_iso(now)
        received_iso = created_at if received_at is None else to_iso(_as_datetime(received_at, "received_at"))
        first_due, quote_due = policy.due_at(now)

        actor = owner or DEFAULT_ACTOR
        actor_role = "owner" if owner else DEFAULT_ACTOR_ROLE
        case_id = self.ids.new("case")
        message_id = self.ids.new("cmsg")

        with self._write() as conn:
            clash = conn.execute(
                "SELECT case_id FROM cases WHERE agency_id = ? AND idempotency_key = ?",
                (self.agency_id, key),
            ).fetchone()
            if clash is not None:
                raise DuplicateCase(
                    f"idempotency_key sudah dipakai: {key}",
                    idempotency_key=key,
                    case_id=str(clash["case_id"]),
                )
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, agency_id, channel, channel_ref, guest_ref, status, priority, owner_actor,
                    booking_id, sla_policy, first_response_due_at, quote_due_at,
                    first_responded_at, quoted_at, idempotency_key, created_at, updated_at,
                    closed_at, close_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, NULL)
                """,
                (
                    case_id,
                    self.agency_id,
                    channel_value,
                    channel_ref_value,
                    guest,
                    "NEW",
                    priority_value,
                    owner,
                    policy.name,
                    to_iso(first_due),
                    to_iso(quote_due),
                    key,
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO case_messages (
                    case_message_id, case_id, direction, channel, external_ref, sender_ref,
                    received_at, content_class, body_redacted, body_sha256, attachment_count,
                    untrusted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    message_id,
                    case_id,
                    DIRECTION_INBOUND,
                    channel_value,
                    external,
                    sender,
                    received_iso,
                    class_value,
                    body_redacted,
                    body_sha256,
                    attachment_count,
                    created_at,
                ),
            )
            append_event(
                conn, clock=self.clock, ids=self.ids,
                action="case.opened", entity_type="case", entity_id=case_id,
                actor=actor, actor_role=actor_role,
                after={"status": "NEW", "channel": channel_value, "priority": priority_value,
                       "content_class": class_value, "sla_policy": policy.name},
            )
            self._append_event(
                conn,
                case_id=case_id,
                kind="CREATED",
                from_status=None,
                to_status="NEW",
                actor=actor,
                actor_role=actor_role,
                command_id=key,
                reason=None,
                refs=(message_id,),
                occurred_at=created_at,
            )
            case_row = conn.execute(
                "SELECT * FROM cases WHERE case_id = ? AND agency_id = ?",
                (case_id, self.agency_id),
            ).fetchone()
            message_row = conn.execute(
                "SELECT * FROM case_messages WHERE case_message_id = ?", (message_id,)
            ).fetchone()
        return Case.from_row(case_row), CaseMessage.from_row(message_row)

    # -- writes: messages --------------------------------------------------- #
    def record_inbound(
        self,
        case_id: str,
        *,
        content_class: str,
        body: str,
        idempotency_key: str,
        sender_ref: Optional[str] = None,
        channel: Optional[str] = None,
        external_ref: Optional[str] = None,
        received_at: Any = None,
        attachments: int = 0,
    ) -> CaseMessage:
        """Store one inbound message and its ``MESSAGE_IN`` timeline row.

        Refused with ``CASE_CLOSED`` once the case is closed: a closed case keeps
        its history, but nothing new is appended to it (invariant 3).
        """
        case = self.get(case_id)
        if case.is_closed:
            raise CaseClosed(f"case {case.case_id} sudah {case.status}", case_id=case.case_id, status=case.status)
        class_value = _require_content_class(content_class)
        channel_value = case.channel if channel is None else _require_channel(channel)
        key = _require_idempotency_key(idempotency_key)
        sender = case.guest_ref if sender_ref is None else _require_bounded_ref(sender_ref, "sender_ref", minimum=MIN_GUEST_REF_LEN)
        external = None if external_ref is None else _require_bounded_ref(external_ref, "external_ref")
        attachment_count = _require_attachments(attachments)
        raw_body = _require_body(body)
        _raw, body_redacted, body_sha256 = _body_fields(class_value, raw_body)

        now = self._now()
        created_at = to_iso(now)
        received_iso = created_at if received_at is None else to_iso(_as_datetime(received_at, "received_at"))
        message_id = self.ids.new("cmsg")

        with self._write() as conn:
            self._append_event(
                conn,
                case_id=case.case_id,
                kind="MESSAGE_IN",
                from_status=None,
                to_status=None,
                actor=case.guest_ref,
                actor_role="guest",
                command_id=key,
                reason=None,
                refs=(message_id,),
                occurred_at=received_iso,
            )
            conn.execute(
                """
                INSERT INTO case_messages (
                    case_message_id, case_id, direction, channel, external_ref, sender_ref,
                    received_at, content_class, body_redacted, body_sha256, attachment_count,
                    untrusted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    message_id,
                    case.case_id,
                    DIRECTION_INBOUND,
                    channel_value,
                    external,
                    sender,
                    received_iso,
                    class_value,
                    body_redacted,
                    body_sha256,
                    attachment_count,
                    created_at,
                ),
            )
            self._touch(conn, case.case_id, created_at)
            message_row = conn.execute(
                "SELECT * FROM case_messages WHERE case_message_id = ?", (message_id,)
            ).fetchone()
        return CaseMessage.from_row(message_row)

    def message_by_command(self, case_id: str, *, command_id: str) -> CaseMessage:
        """The message one command recorded — the answer an idempotent replay returns.

        A replayed webhook must not look like an error to the carrier: the first delivery
        is the fact, and this read hands it back unchanged. The command's timeline row
        names the message it created (``refs``), so the lookup is exact, never "latest".
        """

        require_ref(case_id, "case_id")
        require_ref(command_id, "command_id")
        row = self.conn.execute(
            """
            SELECT m.* FROM case_messages m
            JOIN case_events e ON e.case_id = m.case_id
            WHERE e.case_id = ? AND e.command_id = ? AND e.kind = 'MESSAGE_IN'
              AND e.refs LIKE '%' || m.case_message_id || '%'
            """,
            (case_id, command_id),
        ).fetchone()
        if row is None:
            raise DuplicateCommand(
                f"command_id has not recorded a message for this case: {command_id}",
                case_id=case_id,
                command_id=command_id,
            )
        return CaseMessage.from_row(row)

    # -- writes: status ----------------------------------------------------- #
    def transition(
        self,
        case_id: str,
        to_status: str,
        *,
        actor: str,
        actor_role: str,
        reason: str,
        command_id: str,
    ) -> Case:
        """Move the case along :data:`CASE_TRANSITIONS`, writing one event row."""
        if not isinstance(to_status, str) or to_status not in CASE_STATUSES:
            raise InvalidCaseTransition(
                "status tujuan tidak dikenal: " + str(to_status),
                to_status=str(to_status),
                allowed=CASE_STATUSES,
            )
        actor_value = _require_actor(actor)
        role_value = _require_role(actor_role)
        reason_value = self._require_reason(reason, to_status)
        command = _require_command_id(command_id)

        case = self.get(case_id)
        with self._write() as conn:
            updated = self._apply_status_change(
                conn,
                case,
                to_status,
                actor=actor_value,
                actor_role=role_value,
                reason=reason_value,
                command_id=command,
                occurred_at=self._stamp(),
                refs=(),
            )
        return updated

    def mark_first_response(
        self,
        case_id: str,
        *,
        actor: str,
        actor_role: str = DEFAULT_AGENT_ROLE,
        at: Any = None,
        command_id: str,
    ) -> Case:
        """Record the first answer to the guest (no status change, one event row)."""
        actor_value = _require_actor(actor)
        role_value = _require_role(actor_role)
        command = _require_command_id(command_id)
        case = self.get(case_id)
        if case.is_closed:
            raise CaseClosed(f"case {case.case_id} sudah {case.status}", case_id=case.case_id, status=case.status)
        if case.first_responded_at is not None:
            raise CaseAlreadyResponded(
                "first response sudah tercatat",
                case_id=case.case_id,
                first_responded_at=case.first_responded_at,
            )
        moment = self._moment(at, case, "at")
        moment_iso = to_iso(moment)
        with self._write() as conn:
            self._append_event(
                conn,
                case_id=case.case_id,
                kind="NOTE",
                from_status=None,
                to_status=None,
                actor=actor_value,
                actor_role=role_value,
                command_id=command,
                reason=None,
                refs=("first_response",),
                occurred_at=moment_iso,
            )
            conn.execute(
                "UPDATE cases SET first_responded_at = ?, updated_at = ? WHERE case_id = ?",
                (moment_iso, self._stamp(), case.case_id),
            )
            updated = self._fetch_case(conn, case.case_id)
        return updated

    def mark_quoted(
        self,
        case_id: str,
        *,
        actor: str,
        actor_role: str = DEFAULT_AGENT_ROLE,
        at: Any = None,
        command_id: str,
    ) -> Case:
        """Record the quote time; moves an eligible case to ``QUOTED``.

        One command, one event: if the case is not ``QUOTED`` yet, ``QUOTED`` must
        be a legal move from its current status, and the event is a
        ``STATUS_CHANGED`` row; an already ``QUOTED`` case (a re-quote) only gets
        its ``quoted_at`` refreshed, recorded as a ``NOTE``.
        """
        actor_value = _require_actor(actor)
        role_value = _require_role(actor_role)
        command = _require_command_id(command_id)
        case = self.get(case_id)
        if case.is_closed:
            raise CaseClosed(f"case {case.case_id} sudah {case.status}", case_id=case.case_id, status=case.status)
        moment_iso = to_iso(self._moment(at, case, "at"))

        if case.status == "QUOTED":
            with self._write() as conn:
                self._append_event(
                    conn,
                    case_id=case.case_id,
                    kind="NOTE",
                    from_status=None,
                    to_status=None,
                    actor=actor_value,
                    actor_role=role_value,
                    command_id=command,
                    reason=None,
                    refs=("quoted_at",),
                    occurred_at=moment_iso,
                )
                conn.execute(
                    "UPDATE cases SET quoted_at = ?, updated_at = ? WHERE case_id = ?",
                    (moment_iso, self._stamp(), case.case_id),
                )
                updated = self._fetch_case(conn, case.case_id)
            return updated

        with self._write() as conn:
            updated = self._apply_status_change(
                conn,
                case,
                "QUOTED",
                actor=actor_value,
                actor_role=role_value,
                reason=None,
                command_id=command,
                occurred_at=moment_iso,
                refs=("quoted_at",),
                extra_sets=("quoted_at", moment_iso),
            )
        return updated

    def link_booking(self, case_id: str, booking_id: str, *, actor: str, command_id: str) -> Case:
        """Link the booking that came out of this case (status unchanged).

        The booking must exist: the DDL's foreign key would refuse the row anyway,
        and a clear error beats a raw integrity error.
        """
        actor_value = _require_actor(actor)
        command = _require_command_id(command_id)
        booking = _require_bounded_ref(booking_id, "booking_id")
        case = self.get(case_id)
        if case.is_closed:
            raise CaseClosed(f"case {case.case_id} sudah {case.status}", case_id=case.case_id, status=case.status)
        with self._write() as conn:
            exists = conn.execute("SELECT booking_id FROM bookings WHERE booking_id = ?", (booking,)).fetchone()
            if exists is None:
                raise BookingNotFound(f"booking tidak ditemukan: {booking}", booking_id=booking)
            self._append_event(
                conn,
                case_id=case.case_id,
                kind="BOOKING_LINKED",
                from_status=None,
                to_status=None,
                actor=actor_value,
                actor_role=DEFAULT_AGENT_ROLE,
                command_id=command,
                reason=None,
                refs=(booking,),
                occurred_at=self._stamp(),
            )
            conn.execute(
                "UPDATE cases SET booking_id = ?, updated_at = ? WHERE case_id = ?",
                (booking, self._stamp(), case.case_id),
            )
            updated = self._fetch_case(conn, case.case_id)
        return updated

    def close(
        self,
        case_id: str,
        *,
        outcome: str,
        reason: str,
        actor: str,
        actor_role: str = DEFAULT_AGENT_ROLE,
        command_id: str,
    ) -> Case:
        """Close the case with a business outcome (``WON``/``LOST``/``DUPLICATE``).

        The outcome maps onto a status and then goes through the same guard as
        :meth:`transition`, so a case can only be closed from a status that legally
        reaches that ending (a win needs the ``BOOKED`` step).
        """
        if not isinstance(outcome, str) or outcome not in CLOSE_OUTCOMES:
            raise InvalidOutcome(
                "outcome tidak dikenal: " + str(outcome),
                outcome=str(outcome),
                allowed=tuple(CLOSE_OUTCOMES),
            )
        target = CLOSE_OUTCOMES[outcome]
        actor_value = _require_actor(actor)
        role_value = _require_role(actor_role)
        reason_value = self._require_reason(reason, target)
        command = _require_command_id(command_id)
        case = self.get(case_id)
        with self._write() as conn:
            updated = self._apply_status_change(
                conn,
                case,
                target,
                actor=actor_value,
                actor_role=role_value,
                reason=reason_value,
                command_id=command,
                occurred_at=self._stamp(),
                refs=(),
            )
        return updated

    # -- shared write helpers ----------------------------------------------- #
    def _require_reason(self, reason: Any, to_status: str) -> Optional[str]:
        """Reasons are required text; closing needs the DDL's 3 characters."""
        if to_status in CLOSED_STATUSES:
            if not isinstance(reason, str):
                raise InvalidCloseReason("alasan penutupan wajib diisi", to_status=to_status)
            value = reason.strip()
            if len(value) < MIN_CLOSE_REASON_LEN:
                raise InvalidCloseReason(
                    f"alasan penutupan minimal {MIN_CLOSE_REASON_LEN} karakter",
                    to_status=to_status,
                    length=len(value),
                )
            return _require_text_field(value, "reason", code=InvalidCloseReason.code)
        return _require_text_field(reason, "reason")

    def _moment(self, at: Any, case: Case, field_name: str) -> datetime:
        """Resolve ``at`` (default: the clock) and refuse a time before creation."""
        moment = self._now() if at is None else _as_datetime(at, field_name)
        created = parse_iso(case.created_at)
        if moment < created:
            raise InvalidTimestamp(
                f"{field_name} mendahului created_at case",
                case_id=case.case_id,
                created_at=case.created_at,
            )
        return moment

    def _apply_status_change(
        self,
        conn: sqlite3.Connection,
        case: Case,
        to_status: str,
        *,
        actor: str,
        actor_role: str,
        reason: Optional[str],
        command_id: str,
        occurred_at: str,
        refs: tuple[str, ...],
        extra_sets: tuple[str, Any] | None = None,
    ) -> Case:
        """The single place a status changes: guards, one UPDATE, one event row."""
        if case.is_closed and to_status != CORRECTION_STATUS:
            raise CaseClosed(
                f"case {case.case_id} sudah {case.status}",
                case_id=case.case_id,
                status=case.status,
                to_status=to_status,
            )
        allowed = CASE_TRANSITIONS[case.status]
        if to_status not in allowed:
            raise InvalidCaseTransition(
                f"transisi {case.status} -> {to_status} tidak diizinkan",
                case_id=case.case_id,
                from_status=case.status,
                to_status=to_status,
                allowed=allowed,
            )
        closed_at = case.closed_at
        close_reason = case.close_reason
        if to_status in CLOSED_STATUSES:
            # A correction keeps the first closing time and rewrites the reason.
            closed_at = case.closed_at or occurred_at
            close_reason = reason
        # The event goes in first: a duplicate command then costs nothing but a
        # rollback, and the status is untouched.
        self._append_event(
            conn,
            case_id=case.case_id,
            kind=STATUS_EVENT_KIND,
            from_status=case.status,
            to_status=to_status,
            actor=actor,
            actor_role=actor_role,
            command_id=command_id,
            reason=reason,
            refs=refs,
            occurred_at=occurred_at,
        )
        sets = ["status = ?", "updated_at = ?", "closed_at = ?", "close_reason = ?"]
        values: list[Any] = [to_status, occurred_at, closed_at, close_reason]
        if extra_sets is not None:
            sets.insert(1, f"{extra_sets[0]} = ?")
            values.insert(1, extra_sets[1])
        conn.execute(
            f"UPDATE cases SET {', '.join(sets)} WHERE case_id = ?",
            (*values, case.case_id),
        )
        return self._fetch_case(conn, case.case_id)
