"""Human approval queue with dual control for money- and legal-impacting actions.

Two tables carry the queue: ``approval_requests`` (what somebody wants to do, and
whose sign-off it needs) and ``approval_decisions`` (who said yes or no, when, and
over which payload). The DDL is the contract; this module is the policy that keeps
the contract honest.

Rules worth knowing before calling this:

* **A status is derived from the decisions, never trusted from the column.** While
  no decision rows exist a request reads PENDING whatever
  ``approval_requests.status`` claims, so ``UPDATE ... SET status = 'APPROVED'``
  buys an attacker nothing: one REJECT makes it REJECTED, ``required_approvals``
  distinct APPROVE rows make it APPROVED, and only EXPIRED / WITHDRAWN are taken
  from the column (no decision can derive them). An approval with no decision row
  behind it is void by construction.
* **Dual control is refused, not negotiated.** ``refund``/``payout``/``bank_change``
  — and any request carrying a positive ``money`` amount — must ask for at least
  two approvers. A caller asking for one gets INVALID_DUAL_CONTROL instead of a
  silently upgraded count: quietly changing ``required_approvals`` would change
  what the caller asked for, and "fail closed when in doubt" is this module's
  stance. (The DDL's ``ck_approval_dual_for_money`` is the backstop, not the rule.)
  The rule is also re-applied on the way *out* by
  :func:`policy_required_approvals`: a row edited to ``required_approvals = 1``
  still needs two signatures, so dual control does not depend on a column staying
  honest.
* **The queue's status lookup is a hint, never evidence.** ``outstanding`` and
  ``expire_due`` scan rows whose stored ``status`` is ``PENDING`` (that is what
  the ``ix_approval_status`` index is for) and then re-derive the status from the
  decision log before acting, so the column can neither approve nor expire
  anything. A row whose column was forged to a decided status stays readable as
  PENDING through :meth:`ApprovalService.get`.
* **Nobody signs their own paperwork.** The requester may not decide their own
  request (SELF_APPROVAL_FORBIDDEN), owners included, and a machine principal
  (``system:``, ``bot:``, ...) is not an approver (INVALID_APPROVER) — an approval
  is a human act by construction.
* **One person, one vote.** A second decision from the same approver is refused
  (DUPLICATE_APPROVER) before ``UNIQUE (approval_id, approver)`` has to say so.
* **A decision is bound to the payload it was given.** Each row records
  ``request_hash`` (the payload digest at decision time) and ``signature_hash``
  over ``(approval_id, approver, decision, decided_at, payload_sha256)``, and a
  payload that no longer hashes to ``payload_sha256`` is detected on read
  (APPROVAL_TAMPERED) rather than silently reusing somebody's signature.
* **A rejection is final.** One REJECT closes the request immediately; every later
  decision is APPROVAL_CLOSED and nothing more is written.
* **Time comes from the clock** — never a hidden ``now()``. ``expires_at`` defaults
  to LOW 1440 / MEDIUM 720 / HIGH 240 minutes (:data:`EXPIRY_MINUTES`). Deciding an
  overdue request first records it as EXPIRED and then refuses with
  APPROVAL_EXPIRED, and :meth:`ApprovalService.expire_due` performs the same
  transition for the whole queue (idempotently).
* **Reads fail closed.** A tampered row raises wherever it is read; a queue that
  hid the tampering would be worse than one that stops.

Error codes are the contract (``DomainError.code``), see :data:`ERROR_CODES`.
Malformed *shapes* (a wrong field type, a too-short opaque key, a bad timestamp)
surface the kernel's INVALID_FIELD / MISSING_FIELD / INVALID_TIMESTAMP codes from
:mod:`primitives`; every *semantic* refusal uses an approvals code.

SQL uses ``?`` placeholders and reads rows by column name (SQLite, as tested here).
A PostgreSQL deployment needs a dict/Row factory and ``%s`` placeholders — the same
adaptation the rest of ``src/`` requires.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from primitives import (
    SYSTEM_ACTOR,
    UTC,
    Clock,
    DomainError,
    IdFactory,
    InvalidMoney,
    Money,
    SystemClock,
    canonical_json,
    parse_iso,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    "SUBJECT_TYPES",
    "RISK_LEVELS",
    "DUAL_CONTROL_SUBJECTS",
    "ROLE_RANK",
    "APPROVAL_STATUSES",
    "TERMINAL_STATUSES",
    "DECISION_VALUES",
    "EXPIRY_MINUTES",
    "ERROR_CODES",
    "REQUEST_TABLE",
    "DECISION_TABLE",
    "APPEND_ONLY_TRIGGERS",
    "COLUMNS",
    "DECISION_COLUMNS",
    "MIN_REQUIRED_APPROVALS",
    "MAX_REQUIRED_APPROVALS",
    "DUAL_CONTROL_MIN_APPROVALS",
    "MIN_REASON_LENGTH",
    "MIN_NOTE_LENGTH",
    "MIN_IDEMPOTENCY_KEY_LENGTH",
    "Approval",
    "Decision",
    "ApprovalQueueItem",
    "ApprovalService",
    "ApprovalError",
    "ApprovalNotFound",
    "InvalidSubjectType",
    "InvalidRiskLevel",
    "InvalidDecision",
    "SelfApprovalForbidden",
    "DuplicateApprover",
    "ApprovalClosed",
    "ApprovalExpired",
    "RoleTooLow",
    "InvalidDualControl",
    "IdempotencyConflict",
    "ApprovalTampered",
    "InvalidApprover",
    "MissingReason",
    "InvalidExpiry",
    "ApprovalAppendOnlyUnenforced",
    "guard_append_only",
    "request_fingerprint",
    "decision_signature",
    "derive_status",
]

# --------------------------------------------------------------------------- #
# Vocabulary — mirrored by the DDL, asserted against it in tests
# --------------------------------------------------------------------------- #
#: Subject types an approval may be raised for (``approval_requests.subject_type``).
SUBJECT_TYPES: tuple[str, ...] = (
    "booking",
    "case",
    "quote",
    "hold",
    "refund",
    "payout",
    "bank_change",
    "discount",
    "relocation",
    "policy_exception",
    "message_send",
    "cancellation",
)

#: Risk levels; also the key of the default expiry window.
RISK_LEVELS: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")

#: Subjects where money leaves or legal exposure changes hands: never one approver.
DUAL_CONTROL_SUBJECTS: tuple[str, ...] = ("refund", "payout", "bank_change")

#: Who outranks whom. An approver at or above the request's ``required_role`` rank
#: may decide; a lower rank gets ROLE_TOO_LOW.
ROLE_RANK: Mapping[str, int] = {
    "agent": 1,
    "supervisor": 2,
    "accountant": 3,
    "owner": 4,
}

APPROVAL_STATUSES: tuple[str, ...] = ("PENDING", "APPROVED", "REJECTED", "EXPIRED", "WITHDRAWN")

#: Statuses no further decision is possible on.
TERMINAL_STATUSES: tuple[str, ...] = ("APPROVED", "REJECTED", "EXPIRED", "WITHDRAWN")

DECISION_VALUES: tuple[str, ...] = ("APPROVE", "REJECT")

#: Default time-to-live per risk level, in minutes.
EXPIRY_MINUTES: Mapping[str, int] = {"LOW": 1440, "MEDIUM": 720, "HIGH": 240}

MIN_REQUIRED_APPROVALS = 1
MAX_REQUIRED_APPROVALS = 3
DUAL_CONTROL_MIN_APPROVALS = 2

#: ``approval_requests.reason`` has ``CHECK (length(reason) >= 10)``; a request
#: without a real justification is exactly the kind of thing dual control exists
#: to stop, so the minimum is enforced here too.
MIN_REASON_LENGTH = 10

#: ``approval_decisions.note`` has ``CHECK (length(note) >= 3)``: a decision must
#: carry the human's own words, so a missing/blank note fails closed rather than
#: being synthesised into the audit trail.
MIN_NOTE_LENGTH = 3

MIN_ACTION_LENGTH = 3
MIN_IDEMPOTENCY_KEY_LENGTH = 8
MAX_TEXT_LENGTH = 2000

REQUEST_TABLE = "approval_requests"
DECISION_TABLE = "approval_decisions"

#: The decision log is append-only, enforced by triggers in the schema. Checked at
#: construction so a schema whose guards were dropped cannot be written through.
APPEND_ONLY_TRIGGERS: tuple[str, ...] = (
    "trg_approval_decisions_no_update",
    "trg_approval_decisions_no_delete",
)

#: ``approval_requests`` column order — one tuple so the INSERT, the row mapping and
#: the DDL cannot drift apart.
COLUMNS: tuple[str, ...] = (
    "approval_id",
    "subject_type",
    "subject_id",
    "action",
    "risk_level",
    "requested_by",
    "requested_role",
    "requested_at",
    "reason",
    "evidence_refs",
    "payload",
    "payload_sha256",
    "money_minor",
    "currency",
    "required_role",
    "required_approvals",
    "status",
    "expires_at",
    "idempotency_key",
    "created_at",
    "closed_at",
)

#: ``approval_decisions`` column order. ``note``/``decided_at`` are the human
#: record; ``request_hash``/``signature_hash`` are the integrity record.
DECISION_COLUMNS: tuple[str, ...] = (
    "decision_id",
    "approval_id",
    "approver",
    "approver_role",
    "decision",
    "note",
    "decided_at",
    "request_hash",
    "signature_hash",
)

_INSERT_REQUEST_SQL = (
    f"INSERT INTO {REQUEST_TABLE} ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(COLUMNS))})"
)
_INSERT_DECISION_SQL = (
    f"INSERT INTO {DECISION_TABLE} ({', '.join(DECISION_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(DECISION_COLUMNS))})"
)

_STATUS_DERIVED_FROM_DECISIONS = ("APPROVED", "REJECTED")
_STATUS_FROM_COLUMN = ("EXPIRED", "WITHDRAWN")

#: Fields that identify *what* was asked for, used for idempotent replay. Time
#: fields are deliberately absent: replaying the same logical request at a later
#: clock reading must return the original row, not conflict with it.
_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "subject_type",
    "subject_id",
    "action",
    "risk_level",
    "requested_by",
    "requested_role",
    "reason",
    "evidence_refs",
    "payload",
    "money_minor",
    "currency",
    "required_role",
    "required_approvals",
)

#: An approval is a human act: a machine principal may not stand in for a person.
_MACHINE_PREFIXES: tuple[str, ...] = (
    "system:",
    "service:",
    "cron:",
    "scheduler:",
    "kernel:",
    "job:",
    "bot:",
    "daemon:",
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ApprovalError(DomainError):
    """Base class for every deliberate refusal made by this module."""

    code = "APPROVAL_ERROR"


class ApprovalNotFound(ApprovalError):
    code = "APPROVAL_NOT_FOUND"


class InvalidSubjectType(ApprovalError):
    code = "INVALID_SUBJECT_TYPE"


class InvalidRiskLevel(ApprovalError):
    code = "INVALID_RISK_LEVEL"


class InvalidDecision(ApprovalError):
    code = "INVALID_DECISION"


class SelfApprovalForbidden(ApprovalError):
    code = "SELF_APPROVAL_FORBIDDEN"


class DuplicateApprover(ApprovalError):
    code = "DUPLICATE_APPROVER"


class ApprovalClosed(ApprovalError):
    code = "APPROVAL_CLOSED"


class ApprovalExpired(ApprovalError):
    code = "APPROVAL_EXPIRED"


class RoleTooLow(ApprovalError):
    code = "ROLE_TOO_LOW"


class InvalidDualControl(ApprovalError):
    code = "INVALID_DUAL_CONTROL"


class IdempotencyConflict(ApprovalError):
    code = "IDEMPOTENCY_CONFLICT"


class ApprovalTampered(ApprovalError):
    code = "APPROVAL_TAMPERED"


class InvalidApprover(ApprovalError):
    code = "INVALID_APPROVER"


class MissingReason(ApprovalError):
    code = "MISSING_REASON"


class InvalidExpiry(ApprovalError):
    code = "INVALID_EXPIRY"


class ApprovalAppendOnlyUnenforced(ApprovalError):
    """The decision log's append-only triggers are missing (construction guard)."""

    code = "APPROVAL_APPEND_ONLY_UNENFORCED"


#: Every code a caller may have to branch on, including the construction guard.
ERROR_CODES: tuple[str, ...] = (
    "APPROVAL_NOT_FOUND",
    "INVALID_SUBJECT_TYPE",
    "INVALID_RISK_LEVEL",
    "INVALID_DECISION",
    "SELF_APPROVAL_FORBIDDEN",
    "DUPLICATE_APPROVER",
    "APPROVAL_CLOSED",
    "APPROVAL_EXPIRED",
    "ROLE_TOO_LOW",
    "INVALID_DUAL_CONTROL",
    "IDEMPOTENCY_CONFLICT",
    "APPROVAL_TAMPERED",
    "INVALID_APPROVER",
    "MISSING_REASON",
    "INVALID_EXPIRY",
    "APPROVAL_APPEND_ONLY_UNENFORCED",
)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Approval:
    """One stored request, with its status/counters derived from the decisions.

    ``payload`` is excluded from equality/hashing on purpose: two rows are the same
    request when ``payload_sha256`` matches, and the digest is a compared field.
    """

    approval_id: str
    subject_type: str
    subject_id: str
    action: str
    risk_level: str
    requested_by: str
    requested_role: str
    requested_at: str
    reason: str
    evidence_refs: tuple[str, ...]
    payload: Mapping[str, Any] = field(compare=False, hash=False, repr=False)
    payload_sha256: str = ""
    money_minor: Optional[int] = None
    currency: Optional[str] = None
    required_role: str = "owner"
    required_approvals: int = 1
    status: str = "PENDING"
    expires_at: str = ""
    idempotency_key: str = ""
    created_at: str = ""
    closed_at: Optional[str] = None
    approvals_count: int = 0
    rejections_count: int = 0

    @property
    def is_pending(self) -> bool:
        return self.status == "PENDING"

    @property
    def is_dual_control(self) -> bool:
        """True when one signature is not enough to move this request."""
        return self.required_approvals >= DUAL_CONTROL_MIN_APPROVALS

    @property
    def money(self) -> Optional[Money]:
        """The requested amount as :class:`~primitives.Money` (``None`` if none)."""
        if self.money_minor is None or self.currency is None:
            return None
        return Money(self.money_minor, self.currency)


@dataclass(frozen=True)
class Decision:
    """One recorded signature. ``signature_hash`` binds it to the payload."""

    decision_id: str
    approval_id: str
    approver: str
    approver_role: str
    decision: str
    note: str
    decided_at: str
    request_hash: str
    signature_hash: str


@dataclass(frozen=True)
class ApprovalQueueItem:
    """A read-only row of the outstanding queue (dashboard / SLA use)."""

    approval_id: str
    subject_type: str
    subject_id: str
    action: str
    risk_level: str
    money_minor: Optional[int]
    currency: Optional[str]
    required_role: str
    required_approvals: int
    approvals_count: int
    requested_at: str
    expires_at: str
    age_minutes: int
    reason: str


# --------------------------------------------------------------------------- #
# Field validation (fail closed, cheap checks first)
# --------------------------------------------------------------------------- #
def _subject_type(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in SUBJECT_TYPES:
        raise InvalidSubjectType(
            f"subject_type must be one of {list(SUBJECT_TYPES)}",
            subject_type=repr(value),
        )
    return value.strip()


def _risk_level(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in RISK_LEVELS:
        raise InvalidRiskLevel(
            f"risk_level must be one of {list(RISK_LEVELS)}",
            risk_level=repr(value),
        )
    return value.strip()


def _decision_value(value: Any) -> str:
    if not isinstance(value, str) or value.strip().upper() not in DECISION_VALUES:
        raise InvalidDecision(
            f"decision must be one of {list(DECISION_VALUES)}",
            decision=repr(value),
        )
    return value.strip().upper()


def _role(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() not in ROLE_RANK:
        raise InvalidApprover(
            f"{field_name} must be one of {sorted(ROLE_RANK)}",
            field=field_name,
            got=repr(value),
        )
    return value.strip()


def _rank(role: str, field_name: str) -> int:
    """Rank of a role *read from a row* — an unknown role must not KeyError."""
    rank = ROLE_RANK.get(role)
    if rank is None:
        raise InvalidApprover(
            f"unknown role {role!r} in {field_name}",
            field=field_name,
            known=sorted(ROLE_RANK),
        )
    return rank


def _approver(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidApprover("an approver must be named", approver=repr(value))
    try:
        ref = require_ref(value, "approver", max_len=128)
    except DomainError as exc:
        raise InvalidApprover(
            "approver must be an opaque human reference, not free text",
            approver=repr(value),
        ) from exc
    if ref == SYSTEM_ACTOR or any(ref.startswith(prefix) for prefix in _MACHINE_PREFIXES):
        raise InvalidApprover(
            "an approval is a human act: a machine principal cannot approve",
            approver=ref,
        )
    return ref


def _reason(reason: Any) -> str:
    try:
        text = require_text(reason, "reason", max_len=MAX_TEXT_LENGTH)
    except DomainError as exc:
        raise MissingReason(
            "an approval request must state why it is needed",
            field="reason",
        ) from exc
    if len(text) < MIN_REASON_LENGTH:
        raise MissingReason(
            f"reason must be at least {MIN_REASON_LENGTH} characters",
            field="reason",
            length=len(text),
        )
    return text


def _note(note: Any) -> str:
    try:
        text = require_text(note, "note", max_len=MAX_TEXT_LENGTH)
    except DomainError as exc:
        raise MissingReason(
            "a decision must carry the approver's own note",
            field="note",
        ) from exc
    if len(text) < MIN_NOTE_LENGTH:
        raise MissingReason(
            f"note must be at least {MIN_NOTE_LENGTH} characters",
            field="note",
            length=len(text),
        )
    return text


def _required_approvals(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not (MIN_REQUIRED_APPROVALS <= value <= MAX_REQUIRED_APPROVALS)
    ):
        raise InvalidDualControl(
            f"required_approvals must be an int between {MIN_REQUIRED_APPROVALS} "
            f"and {MAX_REQUIRED_APPROVALS}",
            required_approvals=repr(value),
        )
    return value


def _money_pair(money: Any) -> tuple[Optional[int], Optional[str]]:
    """``money`` → ``(money_minor, currency)``; both present, or both ``NULL``."""
    if money is None:
        return None, None
    if not isinstance(money, Money):
        raise InvalidMoney(
            f"money must be a primitives.Money, got {type(money).__name__}",
            money=repr(money),
        )
    if money.amount_minor < 0:
        raise InvalidMoney(
            "an approval request cannot carry a negative amount",
            amount_minor=money.amount_minor,
        )
    return money.amount_minor, money.currency


def _check_dual_control(
    subject_type: str, required_approvals: int, money: Optional[Money]
) -> None:
    """Refuse a single-approver request that moves money (the fail-closed answer)."""
    if subject_type in DUAL_CONTROL_SUBJECTS and required_approvals < DUAL_CONTROL_MIN_APPROVALS:
        raise InvalidDualControl(
            f"{subject_type} moves money: dual control requires at least "
            f"{DUAL_CONTROL_MIN_APPROVALS} approvers",
            subject_type=subject_type,
            required_approvals=required_approvals,
        )
    if money is not None and money.amount_minor > 0 and required_approvals < DUAL_CONTROL_MIN_APPROVALS:
        raise InvalidDualControl(
            "a request that moves money needs at least "
            f"{DUAL_CONTROL_MIN_APPROVALS} approvers",
            subject_type=subject_type,
            required_approvals=required_approvals,
            money_minor=money.amount_minor,
        )


def _expires_minutes(expires_minutes: Any, risk_level: str) -> int:
    if expires_minutes is None:
        return EXPIRY_MINUTES[risk_level]
    if isinstance(expires_minutes, bool) or not isinstance(expires_minutes, int):
        raise InvalidExpiry(
            "expires_minutes must be a positive int",
            expires_minutes=repr(expires_minutes),
        )
    if expires_minutes <= 0:
        raise InvalidExpiry(
            "expires_minutes must be positive: an approval that expires on arrival "
            "can never be decided",
            expires_minutes=expires_minutes,
        )
    return expires_minutes


def _payload(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise DomainError(
            f"payload must be a mapping, got {type(payload).__name__}",
            code="INVALID_FIELD",
            field="payload",
        )
    body = dict(payload)
    try:
        canonical_json(body)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "payload must be JSON-serialisable: it is the thing the digest covers",
            code="INVALID_PAYLOAD",
            field="payload",
        ) from exc
    return body


def _evidence_refs(refs: Any) -> tuple[str, ...]:
    if refs is None:
        return ()
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Iterable):
        raise DomainError(
            "evidence_refs must be a sequence of references, not a single value",
            code="INVALID_FIELD",
            field="evidence_refs",
        )
    return tuple(require_ref(ref, "evidence_refs") for ref in refs)


def _idempotency_key(value: Any) -> str:
    try:
        text = require_text(value, "idempotency_key", max_len=128)
    except DomainError as exc:
        raise DomainError(
            "idempotency_key is required: without it a retry becomes a duplicate "
            "approval request",
            code="MISSING_FIELD",
            field="idempotency_key",
        ) from exc
    if len(text) < MIN_IDEMPOTENCY_KEY_LENGTH:
        raise DomainError(
            f"idempotency_key must be at least {MIN_IDEMPOTENCY_KEY_LENGTH} characters",
            code="INVALID_FIELD",
            field="idempotency_key",
            length=len(text),
        )
    return text


def _approval_ref(value: Any) -> str:
    """An unusable id is reported as "not found", never as a parsing accident."""
    try:
        return require_ref(value, "approval_id", max_len=128)
    except DomainError as exc:
        raise ApprovalNotFound(
            f"no approval request with id {value!r}",
            approval_id=repr(value),
        ) from exc


def _aware(when: Any, field_name: str = "at") -> datetime:
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise DomainError(
            f"{field_name} must be a timezone-aware datetime (clock.now() or a "
            "FrozenClock reading)",
            code="INVALID_TIMESTAMP",
            field=field_name,
        )
    return when.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #
def request_fingerprint(
    *,
    subject_type: str,
    subject_id: str,
    action: str,
    risk_level: str,
    requested_by: str,
    requested_role: str,
    reason: str,
    evidence_refs: Iterable[str],
    payload: Mapping[str, Any],
    money_minor: Optional[int],
    currency: Optional[str],
    required_role: str,
    required_approvals: int,
) -> str:
    """Canonical digest of *what was asked for* (everything but the clock)."""
    return canonical_json(
        {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "action": action,
            "risk_level": risk_level,
            "requested_by": requested_by,
            "requested_role": requested_role,
            "reason": reason,
            "evidence_refs": list(evidence_refs),
            "payload": dict(payload),
            "money_minor": money_minor,
            "currency": currency,
            "required_role": required_role,
            "required_approvals": required_approvals,
        }
    )


def decision_signature(
    approval_id: str, approver: str, decision: str, decided_at: str, payload_sha256: str
) -> str:
    """Integrity digest for one decision — what exactly was signed, and over what."""
    return sha256_hex(
        canonical_json(
            {
                "approval_id": approval_id,
                "approver": approver,
                "decision": decision,
                "decided_at": decided_at,
                "payload_sha256": payload_sha256,
            }
        )
    )


def _payload_digest(payload: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json(payload))


# --------------------------------------------------------------------------- #
# Status derivation — the decisions are the truth
# --------------------------------------------------------------------------- #
def policy_required_approvals(
    subject_type: str, money_minor: Optional[int], declared: int
) -> int:
    """Signatures required by *policy*, not by the row.

    ``required_approvals`` is a column, and a column can be edited. Dual control
    is the module's rule, so a money-moving request can never need fewer than
    ``DUAL_CONTROL_MIN_APPROVALS`` however the row was written; everything that
    derives a status, reports a record or closes a request goes through here.
    """
    required = int(declared)
    if subject_type in DUAL_CONTROL_SUBJECTS or (
        money_minor is not None and int(money_minor) > 0
    ):
        return max(required, DUAL_CONTROL_MIN_APPROVALS)
    return required


def derive_status(row: Mapping[str, Any], approvals_count: int, rejections_count: int) -> str:
    """Status implied by the decision log for ``row``.

    A REJECT wins, then enough APPROVE rows; EXPIRED/WITHDRAWN can only come from
    the column (no decision derives them), and a stored APPROVED/REJECTED with no
    decision rows behind it is *not* believed — it reads PENDING.
    """
    if rejections_count > 0:
        return "REJECTED"
    if approvals_count >= policy_required_approvals(
        row["subject_type"], row["money_minor"], row["required_approvals"]
    ):
        return "APPROVED"
    stored = row["status"]
    if stored in _STATUS_FROM_COLUMN:
        return stored
    return "PENDING"


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
def _is_sqlite(conn: Any) -> bool:
    """True when ``conn`` speaks SQLite (the dialect this module's SQL targets)."""
    return type(conn).__module__.split(".", 1)[0] == "sqlite3"


def guard_append_only(conn: Any) -> tuple[str, ...]:
    """Fail closed unless both tables exist and the decision log is append-only.

    A decision log that can be rewritten is not evidence, so a schema whose
    triggers were dropped must stop the service from starting rather than accept
    writes that look fine.
    """
    for table in (REQUEST_TABLE, DECISION_TABLE):
        try:
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        except Exception as exc:  # absent table, in either dialect
            raise ApprovalAppendOnlyUnenforced(
                f"{table} does not exist: apply the schema before queueing approvals",
                missing=(f"table:{table}",),
            ) from exc
    if _is_sqlite(conn):
        triggers = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        missing = [name for name in APPEND_ONLY_TRIGGERS if name not in triggers]
        if missing:
            raise ApprovalAppendOnlyUnenforced(
                "the append-only triggers on the decision log are missing; decisions "
                "made through this service could not be trusted",
                missing=tuple(missing),
            )
    # A non-SQLite driver enforces the same guard in its own DDL (see the
    # PostgreSQL schema); the table check above is all this module can assert here.
    return APPEND_ONLY_TRIGGERS


@contextmanager
def _write_transaction(conn: Any) -> Iterator[None]:
    """Run the body in an IMMEDIATE transaction, or join the caller's.

    IMMEDIATE takes the write lock before the row is read, so two approvers cannot
    both read "1 of 2 approvals" and both write the second one. When the caller
    already opened a transaction we must not nest a BEGIN (SQLite errors) and must
    not commit their work either.
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


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class ApprovalService:
    """The approval queue: raise requests, record decisions, watch the SLA.

    Every refusal is a :class:`DomainError` with a stable ``code``; nothing is
    written before a check passes, so a refused call leaves the database exactly
    as it found it.
    """

    def __init__(
        self,
        conn: Any,
        clock: Optional[Clock] = None,
        ids: Optional[IdFactory] = None,
        *,
        enforce_append_only: bool = True,
    ) -> None:
        self._conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ids: IdFactory = ids if ids is not None else IdFactory()
        if enforce_append_only:
            guard_append_only(conn)

    # -- requests ---------------------------------------------------------- #
    def request(
        self,
        *,
        subject_type: str,
        subject_id: str,
        action: str,
        requested_by: str,
        requested_role: str,
        reason: str,
        payload: Optional[Mapping[str, Any]] = None,
        required_role: str = "owner",
        required_approvals: int = 1,
        risk_level: str = "MEDIUM",
        evidence_refs: Iterable[str] = (),
        money: Optional[Money] = None,
        expires_minutes: Optional[int] = None,
        idempotency_key: str,
        at: Optional[datetime] = None,
    ) -> Approval:
        """Queue one request for human sign-off and return the stored row.

        Checks run in this order, and none of them writes anything: subject type,
        subject id, action, risk level, requester and roles, reason, approval
        count, dual control, money, evidence, payload, clock, expiry, idempotency
        key. A replay of the same key with the same logical request returns the
        original approval; the same key with a different request is
        IDEMPOTENCY_CONFLICT.
        """
        subject_type = _subject_type(subject_type)
        subject_id = require_ref(subject_id, "subject_id")
        action = require_text(action, "action", max_len=64)
        if len(action) < MIN_ACTION_LENGTH:
            raise DomainError(
                f"action must be at least {MIN_ACTION_LENGTH} characters: an "
                "approval names what it authorises",
                code="INVALID_FIELD",
                field="action",
                length=len(action),
            )
        risk_level = _risk_level(risk_level)
        requested_by = require_ref(requested_by, "requested_by")
        requested_role = _role(requested_role, "requested_role")
        reason = _reason(reason)
        required_role = _role(required_role, "required_role")
        required_approvals = _required_approvals(required_approvals)
        money_minor, currency = _money_pair(money)
        _check_dual_control(subject_type, required_approvals, money)
        refs = _evidence_refs(evidence_refs)
        body = _payload(payload)
        when = _aware(at if at is not None else self._clock.now())
        minutes = _expires_minutes(expires_minutes, risk_level)
        expires_at = to_iso(when + timedelta(minutes=minutes))
        key = _idempotency_key(idempotency_key)

        fingerprint = request_fingerprint(
            subject_type=subject_type,
            subject_id=subject_id,
            action=action,
            risk_level=risk_level,
            requested_by=requested_by,
            requested_role=requested_role,
            reason=reason,
            evidence_refs=refs,
            payload=body,
            money_minor=money_minor,
            currency=currency,
            required_role=required_role,
            required_approvals=required_approvals,
        )

        replayed = self._row_by_key(key)
        if replayed is not None:
            if self._row_fingerprint(replayed) != fingerprint:
                raise IdempotencyConflict(
                    "idempotency_key was reused for a different request",
                    idempotency_key=key,
                    approval_id=replayed["approval_id"],
                )
            return self._approval_of(replayed)

        approval_id = self._ids.new("approval")
        values = (
            approval_id,
            subject_type,
            subject_id,
            action,
            risk_level,
            requested_by,
            requested_role,
            to_iso(when),
            reason,
            canonical_json(list(refs)),
            canonical_json(body),
            _payload_digest(body),
            money_minor,
            currency,
            required_role,
            required_approvals,
            "PENDING",
            expires_at,
            key,
            to_iso(when),
            None,
        )
        with _write_transaction(self._conn):
            self._conn.execute(_INSERT_REQUEST_SQL, values)
        return self.get(approval_id)

    # -- decisions --------------------------------------------------------- #
    def decide(
        self,
        approval_id: str,
        *,
        approver: str,
        approver_role: str,
        decision: str,
        note: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> Approval:
        """Record one APPROVE/REJECT and return the request as it now stands.

        Refusals, in the order they are checked: APPROVAL_NOT_FOUND,
        INVALID_APPROVER / INVALID_DECISION / MISSING_REASON (input shape),
        APPROVAL_TAMPERED (payload no longer matches its digest),
        SELF_APPROVAL_FORBIDDEN, APPROVAL_CLOSED (already decided/withdrawn, or
        already expired) — an overdue PENDING request is first written to EXPIRED
        and then refused with APPROVAL_EXPIRED — ROLE_TOO_LOW and
        DUPLICATE_APPROVER. Only then is the decision row inserted, and the
        request's status/closed_at are recomputed from the full decision log in
        the same transaction.

        The refusal path is read-only: it never opens a write transaction, so a
        refused decision leaves no trace. The one write a refusal may make is the
        forced EXPIRED mark, and it is committed *before* APPROVAL_EXPIRED is
        raised.
        """
        approval_id = _approval_ref(approval_id)
        decision = _decision_value(decision)
        approver = _approver(approver)
        approver_role = _role(approver_role, "approver_role")
        note = _note(note)
        when = _aware(at if at is not None else self._clock.now())
        decided_at = to_iso(when)

        row = self._row_or_raise(approval_id)
        self._verify_integrity(row)
        approvals_count, rejections_count, _ = self._counts(approval_id)
        status = derive_status(row, approvals_count, rejections_count)

        if approver == row["requested_by"]:
            raise SelfApprovalForbidden(
                "the requester cannot decide their own approval request",
                approval_id=approval_id,
                approver=approver,
            )
        if status != "PENDING":
            raise ApprovalClosed(
                "this approval request is already closed",
                approval_id=approval_id,
                status=status,
                closed_at=row["closed_at"],
            )
        if when >= parse_iso(row["expires_at"]):
            # Expiry is a fact, not a decision: record it first, then refuse.
            with _write_transaction(self._conn):
                self._mark_expired(row)
            raise ApprovalExpired(
                "this approval request expired before the decision was made",
                approval_id=approval_id,
                expires_at=row["expires_at"],
                decided_at=decided_at,
            )
        if _rank(approver_role, "approver_role") < _rank(row["required_role"], "required_role"):
            raise RoleTooLow(
                f"{approver_role} may not decide a request that requires "
                f"{row['required_role']}",
                approval_id=approval_id,
                approver_role=approver_role,
                required_role=row["required_role"],
            )
        if self._has_decision(approval_id, approver):
            raise DuplicateApprover(
                "this approver already recorded a decision on this request",
                approval_id=approval_id,
                approver=approver,
            )

        payload_sha256 = row["payload_sha256"]
        with _write_transaction(self._conn):
            self._conn.execute(
                _INSERT_DECISION_SQL,
                (
                    self._ids.new("decision"),
                    approval_id,
                    approver,
                    approver_role,
                    decision,
                    note,
                    decided_at,
                    payload_sha256,
                    decision_signature(
                        approval_id, approver, decision, decided_at, payload_sha256
                    ),
                ),
            )
            self._apply_outcome(approval_id, row, decision, decided_at)

        return self.get(approval_id)

    def _apply_outcome(
        self, approval_id: str, row: Mapping[str, Any], decision: str, decided_at: str
    ) -> None:
        """Recompute status/closed_at from the decision log just written."""
        approvals_count, rejections_count, last_decided_at = self._counts(approval_id)
        required = policy_required_approvals(
            row["subject_type"], row["money_minor"], row["required_approvals"]
        )
        if rejections_count > 0:
            status, closed_at = "REJECTED", last_decided_at or decided_at
        elif approvals_count >= required:
            status, closed_at = "APPROVED", last_decided_at or decided_at
        else:
            status, closed_at = "PENDING", None
        if status == "PENDING":
            return
        self._conn.execute(
            f"UPDATE {REQUEST_TABLE} SET status = ?, closed_at = ? WHERE approval_id = ?",
            (status, closed_at, approval_id),
        )

    # -- reads ------------------------------------------------------------- #
    def get(self, approval_id: str) -> Approval:
        """The request, with status and counters derived from its decisions."""
        return self._approval_of(self._row_or_raise(_approval_ref(approval_id)))

    def decisions(self, approval_id: str) -> list[Decision]:
        """Every recorded signature, oldest first."""
        approval_id = _approval_ref(approval_id)
        self._verify_integrity(self._row_or_raise(approval_id))
        rows = self._conn.execute(
            f"SELECT * FROM {DECISION_TABLE} WHERE approval_id = ? "
            "ORDER BY decided_at, decision_id",
            (approval_id,),
        ).fetchall()
        return [self._decision_of(row) for row in rows]

    def for_subject(self, subject_type: str, subject_id: str) -> list[Approval]:
        """Every request ever raised for one subject, oldest first."""
        subject_type = _subject_type(subject_type)
        subject_id = require_ref(subject_id, "subject_id")
        rows = self._conn.execute(
            f"SELECT * FROM {REQUEST_TABLE} WHERE subject_type = ? AND subject_id = ? "
            "ORDER BY requested_at, approval_id",
            (subject_type, subject_id),
        ).fetchall()
        return [self._approval_of(row) for row in rows]

    def outstanding(
        self, now: Optional[datetime] = None, *, required_role: Optional[str] = None
    ) -> list[ApprovalQueueItem]:
        """The actionable queue: still PENDING, not yet overdue, oldest first.

        An overdue request is *not* actionable — :meth:`expire_due` owns that
        transition, and this method stays read-only. ``required_role`` filters on
        the item's own ``required_role`` (the rank needed to decide it).
        """
        when = _aware(now if now is not None else self._clock.now())
        role = None if required_role is None else _role(required_role, "required_role")
        rows = self._conn.execute(
            f"SELECT * FROM {REQUEST_TABLE} WHERE status = 'PENDING' "
            "ORDER BY requested_at, approval_id"
        ).fetchall()
        counts = self._aggregate([row["approval_id"] for row in rows])
        items: list[ApprovalQueueItem] = []
        for row in rows:
            self._verify_integrity(row)
            approvals_count, rejections_count, _ = counts.get(row["approval_id"], (0, 0, None))
            if derive_status(row, approvals_count, rejections_count) != "PENDING":
                continue
            if parse_iso(row["expires_at"]) <= when:
                continue
            if role is not None and row["required_role"] != role:
                continue
            age = int((when - parse_iso(row["requested_at"])).total_seconds() // 60)
            items.append(
                ApprovalQueueItem(
                    approval_id=row["approval_id"],
                    subject_type=row["subject_type"],
                    subject_id=row["subject_id"],
                    action=row["action"],
                    risk_level=row["risk_level"],
                    money_minor=row["money_minor"],
                    currency=row["currency"],
                    required_role=row["required_role"],
                    required_approvals=policy_required_approvals(
                        row["subject_type"], row["money_minor"], row["required_approvals"]
                    ),
                    approvals_count=approvals_count,
                    requested_at=row["requested_at"],
                    expires_at=row["expires_at"],
                    age_minutes=max(0, age),
                    reason=row["reason"],
                )
            )
        return items

    def expire_due(self, now: Optional[datetime] = None) -> tuple[str, ...]:
        """Mark every overdue PENDING request EXPIRED; return their ids.

        Idempotent: a second call finds nothing due, writes nothing and returns an
        empty tuple. No decision row is ever written by an expiry — the log only
        records human acts. ``closed_at`` is the moment the request expired, so the
        sweep and :meth:`decide` produce the same row for the same request.
        """
        when = _aware(now if now is not None else self._clock.now())
        expired: list[str] = []
        with _write_transaction(self._conn):
            rows = self._conn.execute(
                f"SELECT * FROM {REQUEST_TABLE} WHERE status = 'PENDING' "
                "ORDER BY requested_at, approval_id"
            ).fetchall()
            counts = self._aggregate([row["approval_id"] for row in rows])
            for row in rows:
                self._verify_integrity(row)
                approvals_count, rejections_count, _ = counts.get(
                    row["approval_id"], (0, 0, None)
                )
                if derive_status(row, approvals_count, rejections_count) != "PENDING":
                    continue
                if parse_iso(row["expires_at"]) > when:
                    continue
                self._mark_expired(row)
                expired.append(row["approval_id"])
        return tuple(expired)

    # -- internals --------------------------------------------------------- #
    def _row_or_raise(self, approval_id: str) -> Mapping[str, Any]:
        row = self._conn.execute(
            f"SELECT * FROM {REQUEST_TABLE} WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ApprovalNotFound(
                f"no approval request with id {approval_id}", approval_id=approval_id
            )
        return row

    def _row_by_key(self, idempotency_key: str) -> Optional[Mapping[str, Any]]:
        return self._conn.execute(
            f"SELECT * FROM {REQUEST_TABLE} WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()

    def _row_fingerprint(self, row: Mapping[str, Any]) -> str:
        return request_fingerprint(
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            action=row["action"],
            risk_level=row["risk_level"],
            requested_by=row["requested_by"],
            requested_role=row["requested_role"],
            reason=row["reason"],
            evidence_refs=json.loads(row["evidence_refs"] or "[]"),
            payload=json.loads(row["payload"]),
            money_minor=row["money_minor"],
            currency=row["currency"],
            required_role=row["required_role"],
            required_approvals=int(row["required_approvals"]),
        )

    def _counts(self, approval_id: str) -> tuple[int, int, Optional[str]]:
        """``(approvals, rejections, last_decided_at)`` straight from the log."""
        row = self._conn.execute(
            f"SELECT "
            f"SUM(CASE WHEN decision = 'APPROVE' THEN 1 ELSE 0 END) AS approvals, "
            f"SUM(CASE WHEN decision = 'REJECT' THEN 1 ELSE 0 END) AS rejections, "
            f"MAX(decided_at) AS last_decided_at "
            f"FROM {DECISION_TABLE} WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is None or row["approvals"] is None:
            return 0, 0, None
        return int(row["approvals"]), int(row["rejections"]), row["last_decided_at"]

    def _aggregate(
        self, approval_ids: list[str]
    ) -> dict[str, tuple[int, int, Optional[str]]]:
        if not approval_ids:
            return {}
        marks = ", ".join("?" * len(approval_ids))
        rows = self._conn.execute(
            f"SELECT approval_id, "
            f"SUM(CASE WHEN decision = 'APPROVE' THEN 1 ELSE 0 END) AS approvals, "
            f"SUM(CASE WHEN decision = 'REJECT' THEN 1 ELSE 0 END) AS rejections, "
            f"MAX(decided_at) AS last_decided_at "
            f"FROM {DECISION_TABLE} WHERE approval_id IN ({marks}) "
            f"GROUP BY approval_id",
            tuple(approval_ids),
        ).fetchall()
        return {
            row["approval_id"]: (
                int(row["approvals"] or 0),
                int(row["rejections"] or 0),
                row["last_decided_at"],
            )
            for row in rows
        }

    def _has_decision(self, approval_id: str, approver: str) -> bool:
        row = self._conn.execute(
            f"SELECT 1 FROM {DECISION_TABLE} WHERE approval_id = ? AND approver = ? LIMIT 1",
            (approval_id, approver),
        ).fetchone()
        return row is not None

    def _verify_integrity(self, row: Mapping[str, Any]) -> None:
        """Fail closed when the stored bytes no longer match their digests."""
        approval_id = row["approval_id"]
        try:
            parsed = json.loads(row["payload"])
        except (TypeError, ValueError) as exc:
            raise ApprovalTampered(
                "the stored payload is not valid JSON",
                approval_id=approval_id,
            ) from exc
        recomputed = _payload_digest(parsed)
        if recomputed != row["payload_sha256"]:
            raise ApprovalTampered(
                "the payload no longer matches payload_sha256: this request was edited",
                approval_id=approval_id,
                stored=row["payload_sha256"],
                recomputed=recomputed,
            )
        stale = self._conn.execute(
            f"SELECT decision_id FROM {DECISION_TABLE} "
            f"WHERE approval_id = ? AND request_hash <> ? LIMIT 1",
            (approval_id, row["payload_sha256"]),
        ).fetchone()
        if stale is not None:
            raise ApprovalTampered(
                "a decision was signed over a different payload hash: the request "
                "was edited after it was approved",
                approval_id=approval_id,
                decision_id=stale["decision_id"],
            )

    def _mark_expired(self, row: Mapping[str, Any]) -> None:
        self._conn.execute(
            f"UPDATE {REQUEST_TABLE} SET status = 'EXPIRED', closed_at = ? "
            f"WHERE approval_id = ? AND status = 'PENDING'",
            (row["expires_at"], row["approval_id"]),
        )

    def _approval_of(self, row: Mapping[str, Any]) -> Approval:
        self._verify_integrity(row)
        approvals_count, rejections_count, last_decided_at = self._counts(row["approval_id"])
        status = derive_status(row, approvals_count, rejections_count)
        closed_at = row["closed_at"]
        if status == "PENDING":
            closed_at = None
        elif status in _STATUS_DERIVED_FROM_DECISIONS:
            closed_at = last_decided_at or row["closed_at"]
        return Approval(
            approval_id=row["approval_id"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            action=row["action"],
            risk_level=row["risk_level"],
            requested_by=row["requested_by"],
            requested_role=row["requested_role"],
            requested_at=row["requested_at"],
            reason=row["reason"],
            evidence_refs=tuple(json.loads(row["evidence_refs"] or "[]")),
            payload=json.loads(row["payload"]),
            payload_sha256=row["payload_sha256"],
            money_minor=row["money_minor"],
            currency=row["currency"],
            required_role=row["required_role"],
            required_approvals=policy_required_approvals(
                row["subject_type"], row["money_minor"], row["required_approvals"]
            ),
            status=status,
            expires_at=row["expires_at"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            closed_at=closed_at,
            approvals_count=approvals_count,
            rejections_count=rejections_count,
        )

    def _decision_of(self, row: Mapping[str, Any]) -> Decision:
        return Decision(
            decision_id=row["decision_id"],
            approval_id=row["approval_id"],
            approver=row["approver"],
            approver_role=row["approver_role"],
            decision=row["decision"],
            note=row["note"],
            decided_at=row["decided_at"],
            request_hash=row["request_hash"],
            signature_hash=row["signature_hash"],
        )
