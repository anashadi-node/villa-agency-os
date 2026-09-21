"""Operational task queue — ``ops_tasks`` (Phase 3: jadwal & tugas).

Every guest cycle produces work: menyiapkan unit sebelum check-in, memeriksa unit
sesudah check-out, membersihkan, memperbaiki, menindaklanjuti tamu dan menagih
tagihan. This module is the single writer for the ``ops_tasks`` table: it turns
those jobs into bounded rows that always have an owner and a deadline, and it
refuses anything else.

Invariants enforced here and tested in ``tests/test_ops_tasks.py`` (the DDL
constraints and the module rules are two nets over the same facts):

1. **Setiap tugas punya pemilik dan tenggat.** ``assignee`` and ``due_at`` are
   mandatory at creation and can never be blanked afterwards: a task without an
   owner is an unassigned promise, and a task without a deadline is invisible to
   ``overdue()``. The DDL allows a NULL ``assignee``; this module does not, so
   the stricter rule is the module's, not the database's.
2. **A task always has a subject.** ``TASK_SUBJECT_RULES`` fixes which reference
   each kind must carry (a cleaning task belongs to a unit or a booking, a guest
   follow-up to a booking or a case). The referenced row must exist — checked
   before the INSERT so the caller gets ``OPS_TASK_REF_NOT_FOUND`` instead of a
   raw foreign-key ``IntegrityError``.
3. **Scheduled jobs do not duplicate tasks.** ``idempotency_key`` is unique
   (``uq_ops_tasks_idempotency``, at least 8 characters). ``create_task()``
   refuses a replay, ``ensure_task()`` returns the row already stored for the
   same work slot, and neither writes a second row.
4. **Status moves only along ``TASK_TRANSITIONS``.** ``DONE`` and ``CANCELLED``
   are terminal and carry ``completed_at``; nothing leaves them. ``BLOCKED``
   requires a reason of at least 3 characters, and the reason is cleared as soon
   as the task leaves ``BLOCKED`` so a non-blocked row never lies about being
   blocked. ``BLOCKED`` cannot jump to ``DONE``: work that was stopped has to be
   explicitly resumed first.
5. **``DONE`` means the runbook ran.** The ``checklist`` (JSON, see
   ``TASK_CHECKLIST_SHAPE``) is the structured runbook for the task; completion
   is refused while any step is still open. Operators who abandon the work
   ``cancel()`` it instead — that is the only honest exit.
6. **Refusals write nothing.** Every command validates fully before touching a
   row; a rejected command leaves ``status``, ``checklist`` and ``updated_at``
   exactly as they were, so a retry is safe.
7. **Reads are deterministic.** Queue views are ordered by ``(due_at, priority
   desc, task_id)``; ``overdue()`` is strict (``due_at`` strictly before *now*)
   and never counts closed tasks.

Cancellation differs from ``cases.py`` on purpose: ``ops_tasks`` has no reason
column beyond ``blocked_reason``, so a cancellation carries no narrative — the
audit trail of *why* an operational task was dropped belongs to the case that
explains the booking, not to a column invented here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from primitives import (
    UTC,
    DomainError,
    IdFactory,
    SystemClock,
    canonical_json,
    parse_iso,
    require_ref,
    require_text,
    to_iso,
)

__all__ = [
    "ACTIVE_STATUSES",
    "CLOSED_STATUSES",
    "DEFAULT_PRIORITY",
    "KIND_CLEANING",
    "KIND_CHECKIN_PREP",
    "KIND_CHECKOUT_INSPECTION",
    "KIND_DOCUMENT_CHASE",
    "KIND_GUEST_FOLLOWUP",
    "KIND_MAINTENANCE",
    "KIND_PAYMENT_CHASE",
    "KIND_VERIFICATION",
    "ChecklistIncomplete",
    "ChecklistItem",
    "DuplicateIdempotencyKey",
    "InvalidBlockedReason",
    "InvalidChecklist",
    "InvalidTaskField",
    "InvalidTaskKind",
    "InvalidTaskPriority",
    "InvalidTaskTransition",
    "MAX_CHECKLIST_ITEMS",
    "MAX_LABEL_LEN",
    "MAX_NOTES_LEN",
    "MIN_BLOCKED_REASON_LEN",
    "MIN_IDEMPOTENCY_KEY_LEN",
    "OpsTaskError",
    "OpsTaskService",
    "PRIORITY_RANK",
    "SUBJECT_FIELDS",
    "TASK_CHECKLIST_SHAPE",
    "TASK_ID_PREFIX",
    "TASK_KINDS",
    "TASK_PRIORITIES",
    "TASK_STATUSES",
    "TASK_SUBJECT_RULES",
    "TASK_TRANSITIONS",
    "Task",
    "TaskAlreadyLinked",
    "TaskClosed",
    "TaskFieldUnchanged",
    "TaskNotBlocked",
    "TaskNotFound",
    "TaskRefNotFound",
]

# --------------------------------------------------------------------------- #
# Vocabulary (mirrors the CHECK constraints in src/schema/001_initial.sql)
# --------------------------------------------------------------------------- #
KIND_CHECKIN_PREP = "CHECKIN_PREP"
KIND_CHECKOUT_INSPECTION = "CHECKOUT_INSPECTION"
KIND_CLEANING = "CLEANING"
KIND_MAINTENANCE = "MAINTENANCE"
KIND_GUEST_FOLLOWUP = "GUEST_FOLLOWUP"
KIND_PAYMENT_CHASE = "PAYMENT_CHASE"
KIND_DOCUMENT_CHASE = "DOCUMENT_CHASE"
KIND_VERIFICATION = "VERIFICATION"

TASK_KINDS: tuple[str, ...] = (
    KIND_CHECKIN_PREP,
    KIND_CHECKOUT_INSPECTION,
    KIND_CLEANING,
    KIND_MAINTENANCE,
    KIND_GUEST_FOLLOWUP,
    KIND_PAYMENT_CHASE,
    KIND_DOCUMENT_CHASE,
    KIND_VERIFICATION,
)

STATUS_OPEN = "OPEN"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_BLOCKED = "BLOCKED"
STATUS_DONE = "DONE"
STATUS_CANCELLED = "CANCELLED"

TASK_STATUSES: tuple[str, ...] = (
    STATUS_OPEN,
    STATUS_IN_PROGRESS,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_CANCELLED,
)

#: Tasked work that still needs attention — the queue, the SLA and the sleep of
#: nothing else. ``overdue()`` only ever looks at these three.
ACTIVE_STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_BLOCKED)

#: Terminal statuses. A row in one of these is history: it is never re-opened,
#: never re-assigned and never re-dated. A wrong closure is corrected by filing
#: the follow-up work as a new task, not by editing the record.
CLOSED_STATUSES: tuple[str, ...] = (STATUS_DONE, STATUS_CANCELLED)

PRIORITY_LOW = "LOW"
PRIORITY_NORMAL = "NORMAL"
PRIORITY_HIGH = "HIGH"
PRIORITY_URGENT = "URGENT"

TASK_PRIORITIES: tuple[str, ...] = (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH, PRIORITY_URGENT)

DEFAULT_PRIORITY = PRIORITY_NORMAL

#: Ranking used for ordering (a same-due-day URGENT task outranks a LOW one).
PRIORITY_RANK: Mapping[str, int] = {
    PRIORITY_LOW: 0,
    PRIORITY_NORMAL: 1,
    PRIORITY_HIGH: 2,
    PRIORITY_URGENT: 3,
}

#: Legal status moves. Deliberately absent: ``BLOCKED -> DONE`` (resume first),
#: anything out of ``DONE``/``CANCELLED`` (terminal), and any ``X -> X`` no-op.
TASK_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    STATUS_OPEN: (STATUS_IN_PROGRESS, STATUS_BLOCKED, STATUS_DONE, STATUS_CANCELLED),
    STATUS_IN_PROGRESS: (STATUS_BLOCKED, STATUS_DONE, STATUS_CANCELLED),
    STATUS_BLOCKED: (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_CANCELLED),
    STATUS_DONE: (),
    STATUS_CANCELLED: (),
}

#: Any-of reference requirements per kind: where the physical work happens. A
#: kind may satisfy its rule with whichever of the listed columns is available.
TASK_SUBJECT_RULES: Mapping[str, tuple[str, ...]] = {
    KIND_CHECKIN_PREP: ("booking_id",),
    KIND_CHECKOUT_INSPECTION: ("booking_id",),
    KIND_CLEANING: ("unit_id", "booking_id"),
    KIND_MAINTENANCE: ("unit_id", "booking_id"),
    KIND_GUEST_FOLLOWUP: ("booking_id", "case_id"),
    KIND_PAYMENT_CHASE: ("booking_id", "case_id"),
    KIND_DOCUMENT_CHASE: ("booking_id", "case_id"),
    KIND_VERIFICATION: ("unit_id", "booking_id", "case_id"),
}

SUBJECT_FIELDS: tuple[str, ...] = ("booking_id", "unit_id", "case_id")

#: Reference column -> (table, key column) used for the existence pre-check.
_REF_TABLES: Mapping[str, tuple[str, str]] = {
    "booking_id": ("bookings", "booking_id"),
    "unit_id": ("units", "unit_id"),
    "case_id": ("cases", "case_id"),
}

#: Columns a command may rewrite after creation. Kept here so the update helper
#: can never be handed an arbitrary column name.
_UPDATABLE_COLUMNS: tuple[str, ...] = ("assignee", "due_at", "priority", "case_id", "checklist")

TASK_ID_PREFIX = "task"

#: Shape of the structured checklist stored in ``ops_tasks.checklist``: a JSON
#: array of ``{"label": str, "done": bool, "done_at": str|null}`` in runbook
#: order. Labels are free text (spaces allowed) and unique within a task.
TASK_CHECKLIST_SHAPE = '{"label": str, "done": bool, "done_at": str|null}'

MIN_IDEMPOTENCY_KEY_LEN = 8
MIN_BLOCKED_REASON_LEN = 3
MIN_ASSIGNEE_LEN = 3
MAX_ASSIGNEE_LEN = 64
MAX_LABEL_LEN = 200
MAX_CHECKLIST_ITEMS = 50
MAX_BLOCKED_REASON_LEN = 1000
MAX_IDEMPOTENCY_KEY_LEN = 128
MAX_NOTES_LEN = 1000

_CHECKLIST_KEYS = frozenset({"label", "done", "done_at"})
_EMPTY_CHECKLIST_JSON = "[]"

# Refusal → the caller is told exactly which move was legal instead.
_TRANSITION_CACHE: Mapping[str, tuple[str, ...]] = TASK_TRANSITIONS


def _validate_vocabulary() -> None:
    """Fail closed at import when the tables above stop being self-consistent."""
    if set(_TRANSITION_CACHE) != set(TASK_STATUSES):
        raise DomainError(
            "TASK_TRANSITIONS must define every status exactly once",
            code="OPS_TASK_TRANSITIONS_INVALID",
            statuses=list(TASK_STATUSES),
            declared=sorted(_TRANSITION_CACHE),
        )
    for source, targets in _TRANSITION_CACHE.items():
        unknown = [target for target in targets if target not in TASK_STATUSES]
        if unknown:
            raise DomainError(
                f"{source} transitions to unknown status(es): {', '.join(unknown)}",
                code="OPS_TASK_TRANSITIONS_INVALID",
                source=source,
                targets=list(targets),
            )
        if source in targets:
            raise DomainError(
                f"{source} must not transition to itself",
                code="OPS_TASK_TRANSITIONS_INVALID",
                source=source,
            )
    if set(TASK_SUBJECT_RULES) != set(TASK_KINDS):
        raise DomainError(
            "TASK_SUBJECT_RULES must declare a rule for every kind",
            code="OPS_TASK_SUBJECT_RULES_INVALID",
            kinds=list(TASK_KINDS),
            declared=sorted(TASK_SUBJECT_RULES),
        )
    for kind, fields in TASK_SUBJECT_RULES.items():
        unknown = [field for field in fields if field not in SUBJECT_FIELDS]
        if unknown or not fields:
            raise DomainError(
                f"{kind} declares an unusable subject rule",
                code="OPS_TASK_SUBJECT_RULES_INVALID",
                kind=kind,
                fields=list(fields),
            )
    if set(PRIORITY_RANK) != set(TASK_PRIORITIES):
        raise DomainError(
            "PRIORITY_RANK must rank every priority",
            code="OPS_TASK_PRIORITIES_INVALID",
            priorities=list(TASK_PRIORITIES),
        )
    missing = [status for status in CLOSED_STATUSES if _TRANSITION_CACHE.get(status)]
    if missing:
        raise DomainError(
            "terminal statuses must have no outgoing transition",
            code="OPS_TASK_TRANSITIONS_INVALID",
            statuses=missing,
        )
    if set(ACTIVE_STATUSES) | set(CLOSED_STATUSES) != set(TASK_STATUSES):
        raise DomainError(
            "ACTIVE_STATUSES and CLOSED_STATUSES must partition TASK_STATUSES",
            code="OPS_TASK_STATUSES_INVALID",
            active=list(ACTIVE_STATUSES),
            closed=list(CLOSED_STATUSES),
        )


_validate_vocabulary()


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class OpsTaskError(DomainError):
    """Base class for every refusal raised by this module."""


class TaskNotFound(OpsTaskError):
    pass


class InvalidTaskKind(OpsTaskError):
    pass


class InvalidTaskPriority(OpsTaskError):
    pass


class InvalidTaskField(OpsTaskError):
    pass


class InvalidBlockedReason(OpsTaskError):
    pass


class InvalidChecklist(OpsTaskError):
    pass


class TaskRefNotFound(OpsTaskError):
    pass


class DuplicateIdempotencyKey(OpsTaskError):
    pass


class InvalidTaskTransition(OpsTaskError):
    pass


class ChecklistIncomplete(OpsTaskError):
    pass


class TaskClosed(OpsTaskError):
    pass


class TaskNotBlocked(OpsTaskError):
    pass


class TaskFieldUnchanged(OpsTaskError):
    pass


class TaskAlreadyLinked(OpsTaskError):
    pass


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChecklistItem:
    """One runbook step. ``done_at`` is stamped by the service clock, not by hand."""

    label: str
    done: bool = False
    done_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "done": self.done, "done_at": self.done_at}


@dataclass(frozen=True)
class Task:
    """A row of ``ops_tasks`` (never mutated in place; commands return a fresh copy)."""

    task_id: str
    kind: str
    status: str
    priority: str
    assignee: str
    due_at: str
    checklist: tuple[ChecklistItem, ...]
    idempotency_key: str
    created_at: str
    updated_at: str
    completed_at: Optional[str]
    blocked_reason: Optional[str]
    booking_id: Optional[str] = None
    unit_id: Optional[str] = None
    case_id: Optional[str] = None

    # -- state --------------------------------------------------------------
    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_blocked(self) -> bool:
        return self.status == STATUS_BLOCKED

    @property
    def allowed_transitions(self) -> tuple[str, ...]:
        return tuple(TASK_TRANSITIONS[self.status])

    # -- checklist ----------------------------------------------------------
    @property
    def progress(self) -> tuple[int, int]:
        """``(steps done, steps total)`` — ``(0, 0)`` for a task with no runbook."""
        done = sum(1 for item in self.checklist if item.done)
        return done, len(self.checklist)

    @property
    def open_steps(self) -> tuple[str, ...]:
        return tuple(item.label for item in self.checklist if not item.done)

    @property
    def checklist_complete(self) -> bool:
        """True when every step is done (vacuously true for an empty runbook)."""
        return not self.open_steps

    # -- references ---------------------------------------------------------
    @property
    def subject_refs(self) -> tuple[tuple[str, str], ...]:
        """The non-null subject references, in ``SUBJECT_FIELDS`` order."""
        pairs = (
            ("booking_id", self.booking_id),
            ("unit_id", self.unit_id),
            ("case_id", self.case_id),
        )
        return tuple((name, value) for name, value in pairs if value is not None)

    def is_overdue(self, now: datetime) -> bool:
        """Strict lateness: active *and* ``due_at`` strictly before ``now``."""
        return self.is_active and parse_iso(self.due_at) < _as_datetime(now, "now")

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(
            task_id=str(row["task_id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            priority=str(row["priority"]),
            assignee=_assignee_from_row(row),
            due_at=str(row["due_at"]),
            checklist=_decode_checklist(row["checklist"], task_id=str(row["task_id"])),
            idempotency_key=str(row["idempotency_key"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=None if row["completed_at"] is None else str(row["completed_at"]),
            blocked_reason=None if row["blocked_reason"] is None else str(row["blocked_reason"]),
            booking_id=None if row["booking_id"] is None else str(row["booking_id"]),
            unit_id=None if row["unit_id"] is None else str(row["unit_id"]),
            case_id=None if row["case_id"] is None else str(row["case_id"]),
        )


def _assignee_from_row(row: sqlite3.Row) -> str:
    """A stored NULL assignee is a row this module never wrote: fail closed."""
    value = row["assignee"]
    if value is None or not str(value).strip():
        raise DomainError(
            "ops_tasks row has no assignee",
            code="OPS_TASK_ASSIGNEE_MISSING",
            task_id=str(row["task_id"]),
        )
    return str(value)


# --------------------------------------------------------------------------- #
# Field validation (pure: no connection is touched)
# --------------------------------------------------------------------------- #
def _as_datetime(value: Any, field_name: str) -> datetime:
    """Accept an ISO string or an aware datetime; refuse naive stamps."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        moment = parse_iso(value)
    else:
        raise DomainError(
            f"{field_name} must be an ISO-8601 string or datetime",
            code="INVALID_FIELD",
            field=field_name,
            got=type(value).__name__,
        )
    if moment.tzinfo is None:
        raise DomainError(
            f"{field_name} must carry a timezone",
            code="INVALID_FIELD",
            field=field_name,
        )
    return moment.astimezone(UTC)


def _require_task_id(value: Any) -> str:
    try:
        return require_ref(value, "task_id", 128)
    except DomainError as exc:
        raise TaskNotFound(str(exc), code="OPS_TASK_ID_INVALID", task_id=value) from exc


def _require_kind(value: Any) -> str:
    if not isinstance(value, str) or value not in TASK_KINDS:
        raise InvalidTaskKind(
            "kind must be one of the declared task kinds",
            code="OPS_TASK_KIND_INVALID",
            kind=value,
            allowed=list(TASK_KINDS),
        )
    return value


def _require_status(value: Any) -> str:
    if not isinstance(value, str) or value not in TASK_STATUSES:
        raise DomainError(
            "status must be one of the declared task statuses",
            code="OPS_TASK_STATUS_INVALID",
            status=value,
            allowed=list(TASK_STATUSES),
        )
    return value


def _require_priority(value: Any) -> str:
    if not isinstance(value, str) or value not in TASK_PRIORITIES:
        raise InvalidTaskPriority(
            "priority must be one of the declared priorities",
            code="OPS_TASK_PRIORITY_INVALID",
            priority=value,
            allowed=list(TASK_PRIORITIES),
        )
    return value


def _require_assignee(value: Any) -> str:
    """Owner handle: an opaque token (``staff_014``, ``vendor_bright_clean``).

    Tighter than the DDL on purpose — invariant 1 says a task always has an
    owner, so a blank or whitespace-laden assignee is refused before any write.
    """
    if not isinstance(value, str):
        raise InvalidTaskField(
            "assignee must be a string",
            code="OPS_TASK_ASSIGNEE_INVALID",
            field="assignee",
            got=type(value).__name__,
        )
    try:
        handle = require_ref(value, "assignee", MAX_ASSIGNEE_LEN)
    except DomainError as exc:
        raise InvalidTaskField(
            str(exc),
            code="OPS_TASK_ASSIGNEE_INVALID",
            field="assignee",
        ) from exc
    if len(handle) < MIN_ASSIGNEE_LEN:
        raise InvalidTaskField(
            f"assignee must be at least {MIN_ASSIGNEE_LEN} characters",
            code="OPS_TASK_ASSIGNEE_INVALID",
            field="assignee",
            length=len(handle),
        )
    return handle


def _require_due_at(value: Any) -> str:
    try:
        return to_iso(_as_datetime(value, "due_at"))
    except DomainError as exc:
        if exc.code.startswith("OPS_TASK"):
            raise
        raise InvalidTaskField(
            f"due_at must be an ISO-8601 timestamp with a timezone: {exc}",
            code="OPS_TASK_DUE_AT_INVALID",
            field="due_at",
        ) from exc


def _require_reason(value: Any) -> str:
    text = require_text(value, "blocked_reason", MAX_BLOCKED_REASON_LEN)
    if len(text) < MIN_BLOCKED_REASON_LEN:
        raise InvalidBlockedReason(
            f"blocked_reason must be at least {MIN_BLOCKED_REASON_LEN} characters",
            code="OPS_TASK_BLOCKED_REASON_INVALID",
            length=len(text),
        )
    return text


def _require_idempotency_key(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidTaskField(
            "idempotency_key must be a string",
            code="OPS_TASK_IDEMPOTENCY_KEY_INVALID",
            field="idempotency_key",
            got=type(value).__name__,
        )
    try:
        key = require_ref(value, "idempotency_key", MAX_IDEMPOTENCY_KEY_LEN)
    except DomainError as exc:
        raise InvalidTaskField(
            str(exc),
            code="OPS_TASK_IDEMPOTENCY_KEY_INVALID",
            field="idempotency_key",
        ) from exc
    if len(key) < MIN_IDEMPOTENCY_KEY_LEN:
        raise InvalidTaskField(
            f"idempotency_key must be at least {MIN_IDEMPOTENCY_KEY_LEN} characters",
            code="OPS_TASK_IDEMPOTENCY_KEY_INVALID",
            field="idempotency_key",
            length=len(key),
        )
    return key


def _require_label(value: Any) -> str:
    """Checklist labels are free text: a runbook step reads like a sentence."""
    try:
        return require_text(value, "checklist label", MAX_LABEL_LEN)
    except DomainError as exc:
        raise InvalidChecklist(
            str(exc),
            code="OPS_TASK_CHECKLIST_LABEL_INVALID",
        ) from exc


def _require_subject_refs(kind: str, **refs: Any) -> dict[str, str]:
    """Validate the reference columns present, then apply the kind's rule."""
    provided: dict[str, str] = {}
    for field_name in SUBJECT_FIELDS:
        value = refs.get(field_name)
        if value is None:
            continue
        try:
            provided[field_name] = require_ref(value, field_name, 128)
        except DomainError as exc:
            raise InvalidTaskField(
                str(exc),
                code="OPS_TASK_REF_INVALID",
                field=field_name,
            ) from exc
    required = TASK_SUBJECT_RULES[kind]
    if not any(field_name in provided for field_name in required):
        raise InvalidTaskField(
            f"{kind} task must reference one of: {', '.join(required)}",
            code="OPS_TASK_SUBJECT_MISSING",
            kind=kind,
            required=list(required),
            provided=sorted(provided),
        )
    return provided


def _require_checklist(value: Any) -> tuple[ChecklistItem, ...]:
    """Normalise a runbook: labels only, or ``ChecklistItem``s with state."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise InvalidChecklist(
            "checklist must be a sequence of steps",
            code="OPS_TASK_CHECKLIST_INVALID",
            got=type(value).__name__,
        )
    try:
        entries = list(value)
    except TypeError as exc:
        raise InvalidChecklist(
            "checklist must be a sequence of steps",
            code="OPS_TASK_CHECKLIST_INVALID",
            got=type(value).__name__,
        ) from exc
    if len(entries) > MAX_CHECKLIST_ITEMS:
        raise InvalidChecklist(
            f"a checklist may hold at most {MAX_CHECKLIST_ITEMS} steps",
            code="OPS_TASK_CHECKLIST_TOO_LONG",
            length=len(entries),
        )
    items: list[ChecklistItem] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, ChecklistItem):
            label = _require_label(entry.label)
            if entry.done and entry.done_at is None:
                raise InvalidChecklist(
                    "a step that is already done needs its done_at stamp",
                    code="OPS_TASK_CHECKLIST_INVALID",
                    label=label,
                )
            if not entry.done and entry.done_at is not None:
                raise InvalidChecklist(
                    "an open step must not carry a done_at stamp",
                    code="OPS_TASK_CHECKLIST_INVALID",
                    label=label,
                )
            done_at = entry.done_at
        elif isinstance(entry, str):
            label = _require_label(entry)
            done_at = None
            entry = ChecklistItem(label=label)
        else:
            raise InvalidChecklist(
                "each checklist step is a label or a ChecklistItem",
                code="OPS_TASK_CHECKLIST_INVALID",
                got=type(entry).__name__,
            )
        if label in seen:
            raise InvalidChecklist(
                f"duplicate checklist step: {label}",
                code="OPS_TASK_CHECKLIST_DUPLICATE",
                label=label,
            )
        seen.add(label)
        items.append(ChecklistItem(label=label, done=bool(entry.done), done_at=done_at))
    return tuple(items)


def _encode_checklist(items: Sequence[ChecklistItem]) -> str:
    if not items:
        return _EMPTY_CHECKLIST_JSON
    return canonical_json([item.to_dict() for item in items])


def _decode_checklist(text: Any, *, task_id: str) -> tuple[ChecklistItem, ...]:
    """Read the stored runbook strictly: unknown shapes fail closed."""
    if text is None or not str(text).strip():
        raise DomainError(
            "ops_tasks.checklist is empty; the runbook was lost",
            code="OPS_TASK_CHECKLIST_CORRUPT",
            task_id=task_id,
        )
    try:
        payload = json.loads(str(text))
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "ops_tasks.checklist is not valid JSON",
            code="OPS_TASK_CHECKLIST_CORRUPT",
            task_id=task_id,
        ) from exc
    if not isinstance(payload, list):
        raise DomainError(
            "ops_tasks.checklist must be a JSON array",
            code="OPS_TASK_CHECKLIST_CORRUPT",
            task_id=task_id,
        )
    items: list[ChecklistItem] = []
    seen: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) != _CHECKLIST_KEYS:
            raise DomainError(
                f"ops_tasks.checklist entry must match {TASK_CHECKLIST_SHAPE}",
                code="OPS_TASK_CHECKLIST_CORRUPT",
                task_id=task_id,
            )
        label = entry["label"]
        done = entry["done"]
        done_at = entry["done_at"]
        if not isinstance(label, str) or not label.strip():
            raise DomainError(
                "ops_tasks.checklist entry has no label",
                code="OPS_TASK_CHECKLIST_CORRUPT",
                task_id=task_id,
            )
        if not isinstance(done, bool):
            raise DomainError(
                "ops_tasks.checklist entry 'done' must be a boolean",
                code="OPS_TASK_CHECKLIST_CORRUPT",
                task_id=task_id,
            )
        if done_at is not None and not isinstance(done_at, str):
            raise DomainError(
                "ops_tasks.checklist entry 'done_at' must be a timestamp or null",
                code="OPS_TASK_CHECKLIST_CORRUPT",
                task_id=task_id,
            )
        if done != (done_at is not None):
            raise DomainError(
                "ops_tasks.checklist entry disagrees with itself about being done",
                code="OPS_TASK_CHECKLIST_CORRUPT",
                task_id=task_id,
                label=label,
            )
        if label in seen:
            raise DomainError(
                f"ops_tasks.checklist repeats the step: {label}",
                code="OPS_TASK_CHECKLIST_CORRUPT",
                task_id=task_id,
            )
        seen.add(label)
        items.append(ChecklistItem(label=label, done=done, done_at=done_at))
    return tuple(items)


def _priority_rank_sql() -> str:
    """``ORDER BY`` fragment ranking priorities without binding parameters."""
    cases = " ".join(
        f"WHEN '{name}' THEN {rank}"
        for name, rank in sorted(PRIORITY_RANK.items(), key=lambda pair: pair[1], reverse=True)
    )
    return f"CASE priority {cases} ELSE -1 END DESC"


_QUEUE_ORDER_SQL = f"ORDER BY due_at ASC, {_priority_rank_sql()}, task_id ASC"


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class OpsTaskService:
    """The single writer of ``ops_tasks``.

    ``conn`` is a ``schema_loader.connect_sqlite`` connection (SQLite in tests,
    PostgreSQL in production, same SQL surface). ``clock`` and ``ids`` are
    injected so a test can pin both time and identifiers.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Optional[Any] = None,
        ids: Optional[Any] = None,
    ) -> None:
        self.conn = conn
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory(seed="task")

    # -- internals ----------------------------------------------------------
    def _now(self) -> datetime:
        return self.clock.now()

    def _stamp(self) -> str:
        return to_iso(self._now())

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One transaction per command: BEGIN IMMEDIATE, commit, or roll back."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def _row(self, task_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM ops_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(
                f"no such ops task: {task_id}",
                code="OPS_TASK_NOT_FOUND",
                task_id=task_id,
            )
        return row

    def _row_by_key(self, conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM ops_tasks WHERE idempotency_key = ?", (key,)
        ).fetchone()

    def _require_refs_exist(self, conn: sqlite3.Connection, refs: Mapping[str, str]) -> None:
        for field_name, value in refs.items():
            table, column = _REF_TABLES[field_name]
            found = conn.execute(
                f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (value,)
            ).fetchone()
            if found is None:
                raise TaskRefNotFound(
                    f"{field_name} does not exist: {value}",
                    code="OPS_TASK_REF_NOT_FOUND",
                    field=field_name,
                    ref=value,
                )

    def _require_active_row(self, conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM ops_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(
                f"no such ops task: {task_id}",
                code="OPS_TASK_NOT_FOUND",
                task_id=task_id,
            )
        if str(row["status"]) in CLOSED_STATUSES:
            raise TaskClosed(
                f"task {task_id} is {row['status']} and closed tasks are history",
                code="OPS_TASK_CLOSED",
                task_id=task_id,
                status=str(row["status"]),
            )
        return row

    # -- creation -----------------------------------------------------------
    def create_task(
        self,
        *,
        kind: Any,
        assignee: Any,
        due_at: Any,
        idempotency_key: Any,
        priority: Any = DEFAULT_PRIORITY,
        booking_id: Optional[Any] = None,
        unit_id: Optional[Any] = None,
        case_id: Optional[Any] = None,
        checklist: Any = (),
        created_at: Optional[Any] = None,
    ) -> Task:
        """File one operational task. A replayed ``idempotency_key`` is refused.

        Validation order (every step pure, nothing is written until all pass):
        kind → priority → due_at → assignee → subject rule → checklist →
        idempotency key; then, inside the transaction, key replay and reference
        existence. ``created_at`` may be supplied by an importer (the row is
        then back-dated and ``updated_at`` matches it) and must be tz-aware.
        """
        kind_value = _require_kind(kind)
        priority_value = _require_priority(priority)
        due_value = _require_due_at(due_at)
        assignee_value = _require_assignee(assignee)
        refs = _require_subject_refs(kind_value, booking_id=booking_id, unit_id=unit_id, case_id=case_id)
        items = _require_checklist(checklist)
        key = _require_idempotency_key(idempotency_key)
        moment = self._stamp() if created_at is None else to_iso(_as_datetime(created_at, "created_at"))
        task_id = self.ids.new(TASK_ID_PREFIX)

        with self._write() as conn:
            existing = self._row_by_key(conn, key)
            if existing is not None:
                raise DuplicateIdempotencyKey(
                    "idempotency_key already filed a task",
                    code="OPS_TASK_DUPLICATE_IDEMPOTENCY_KEY",
                    idempotency_key=key,
                    task_id=str(existing["task_id"]),
                )
            self._require_refs_exist(conn, refs)
            conn.execute(
                "INSERT INTO ops_tasks ("
                "task_id, kind, booking_id, unit_id, case_id, due_at, priority, assignee, "
                "status, checklist, idempotency_key, created_at, updated_at, completed_at, blocked_reason"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    task_id,
                    kind_value,
                    refs.get("booking_id"),
                    refs.get("unit_id"),
                    refs.get("case_id"),
                    due_value,
                    priority_value,
                    assignee_value,
                    STATUS_OPEN,
                    _encode_checklist(items),
                    key,
                    moment,
                    moment,
                ),
            )
        return self.get(task_id)

    def ensure_task(
        self,
        *,
        kind: Any,
        assignee: Any,
        due_at: Any,
        idempotency_key: Any,
        priority: Any = DEFAULT_PRIORITY,
        booking_id: Optional[Any] = None,
        unit_id: Optional[Any] = None,
        case_id: Optional[Any] = None,
        checklist: Any = (),
        created_at: Optional[Any] = None,
    ) -> tuple[Task, bool]:
        """Idempotent create for scheduled jobs. Returns ``(task, created)``.

        The key identifies one piece of work: same key and same slot
        (kind + references + ``due_at``) returns the stored task with
        ``created=False`` and writes nothing. Same key on a *different* slot is a
        programming error in the caller and raises
        ``OPS_TASK_DUPLICATE_IDEMPOTENCY_KEY`` rather than silently mixing two
        jobs. Operator state that drifts afterwards (assignee, priority,
        checklist progress) does not affect the match — a job re-running after a
        supervisor reassigned the task must not fail.
        """
        key = _require_idempotency_key(idempotency_key)
        kind_value = _require_kind(kind)
        due_value = _require_due_at(due_at)
        refs = _require_subject_refs(kind_value, booking_id=booking_id, unit_id=unit_id, case_id=case_id)
        _require_priority(priority)
        _require_assignee(assignee)
        _require_checklist(checklist)

        existing = self.find_by_idempotency_key(key)
        if existing is None:
            return (
                self.create_task(
                    kind=kind_value,
                    assignee=assignee,
                    due_at=due_value,
                    idempotency_key=key,
                    priority=priority,
                    booking_id=booking_id,
                    unit_id=unit_id,
                    case_id=case_id,
                    checklist=checklist,
                    created_at=created_at,
                ),
                True,
            )
        stored_slot = (existing.kind, existing.booking_id, existing.unit_id, existing.case_id, existing.due_at)
        wanted_slot = (
            kind_value,
            refs.get("booking_id"),
            refs.get("unit_id"),
            refs.get("case_id"),
            due_value,
        )
        if stored_slot != wanted_slot:
            raise DuplicateIdempotencyKey(
                "idempotency_key is bound to a different work slot",
                code="OPS_TASK_DUPLICATE_IDEMPOTENCY_KEY",
                idempotency_key=key,
                task_id=existing.task_id,
                stored=stored_slot,
                wanted=wanted_slot,
            )
        return existing, False

    # -- reads --------------------------------------------------------------
    def get(self, task_id: Any) -> Task:
        return Task.from_row(self._row(_require_task_id(task_id)))

    def find_by_idempotency_key(self, idempotency_key: Any) -> Optional[Task]:
        key = _require_idempotency_key(idempotency_key)
        row = self.conn.execute(
            "SELECT * FROM ops_tasks WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return None if row is None else Task.from_row(row)

    def list_tasks(
        self,
        *,
        statuses: Optional[Sequence[str]] = None,
        kinds: Optional[Sequence[str]] = None,
        priorities: Optional[Sequence[str]] = None,
        assignees: Optional[Sequence[str]] = None,
        unit_id: Optional[str] = None,
        booking_id: Optional[str] = None,
        case_id: Optional[str] = None,
        due_before: Optional[Any] = None,
        due_after: Optional[Any] = None,
    ) -> tuple[Task, ...]:
        """Filtered queue, always ordered by ``(due_at, priority desc, task_id)``.

        ``due_before`` is exclusive and ``due_after`` inclusive, both compared on
        the canonical UTC stamps this module writes, so the ordering and the
        comparison agree with each other.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if statuses is not None:
            wanted = [_require_status(status) for status in statuses]
            if not wanted:
                return ()
            clauses.append(f"status IN ({', '.join('?' * len(wanted))})")
            params.extend(wanted)
        if kinds is not None:
            wanted_kinds = [_require_kind(kind) for kind in kinds]
            if not wanted_kinds:
                return ()
            clauses.append(f"kind IN ({', '.join('?' * len(wanted_kinds))})")
            params.extend(wanted_kinds)
        if priorities is not None:
            wanted_priorities = [_require_priority(priority) for priority in priorities]
            if not wanted_priorities:
                return ()
            clauses.append(f"priority IN ({', '.join('?' * len(wanted_priorities))})")
            params.extend(wanted_priorities)
        if assignees is not None:
            wanted_assignees = [_require_assignee(assignee) for assignee in assignees]
            if not wanted_assignees:
                return ()
            clauses.append(f"assignee IN ({', '.join('?' * len(wanted_assignees))})")
            params.extend(wanted_assignees)
        for field_name, value in (
            ("unit_id", unit_id),
            ("booking_id", booking_id),
            ("case_id", case_id),
        ):
            if value is None:
                continue
            try:
                ref = require_ref(value, field_name, 128)
            except DomainError as exc:
                raise InvalidTaskField(
                    str(exc), code="OPS_TASK_REF_INVALID", field=field_name
                ) from exc
            clauses.append(f"{field_name} = ?")
            params.append(ref)
        if due_before is not None:
            clauses.append("due_at < ?")
            params.append(to_iso(_as_datetime(due_before, "due_before")))
        if due_after is not None:
            clauses.append("due_at >= ?")
            params.append(to_iso(_as_datetime(due_after, "due_after")))

        sql = "SELECT * FROM ops_tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " " + _QUEUE_ORDER_SQL
        rows = self.conn.execute(sql, params).fetchall()
        return tuple(Task.from_row(row) for row in rows)

    def open_tasks(self, **filters: Any) -> tuple[Task, ...]:
        """Everything still active (``OPEN``/``IN_PROGRESS``/``BLOCKED``)."""
        filters.pop("statuses", None)
        return self.list_tasks(statuses=ACTIVE_STATUSES, **filters)

    def overdue(self, now: Optional[Any] = None, **filters: Any) -> tuple[Task, ...]:
        """Active tasks whose deadline passed strictly before *now*."""
        moment = self._stamp() if now is None else to_iso(_as_datetime(now, "now"))
        filters.pop("statuses", None)
        filters.pop("due_before", None)
        return self.list_tasks(statuses=ACTIVE_STATUSES, due_before=moment, **filters)

    def due_within(self, hours: float, now: Optional[Any] = None, **filters: Any) -> tuple[Task, ...]:
        """Active tasks due in ``[now, now + hours]`` — the shift hand-over view."""
        if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours < 0:
            raise InvalidTaskField(
                "hours must be a non-negative number",
                code="OPS_TASK_HORIZON_INVALID",
                hours=hours,
            )
        base = _as_datetime(self._stamp() if now is None else now, "now")
        horizon = base + timedelta(hours=float(hours))
        filters.pop("statuses", None)
        return self.list_tasks(
            statuses=ACTIVE_STATUSES,
            due_after=base,
            due_before=horizon,
            **filters,
        )

    def by_assignee(self, assignee: Any, *, include_closed: bool = False, **filters: Any) -> tuple[Task, ...]:
        """One person's workload (active first; history only when asked)."""
        who = _require_assignee(assignee)
        if include_closed:
            return self.list_tasks(assignees=(who,), **filters)
        return self.list_tasks(statuses=ACTIVE_STATUSES, assignees=(who,), **filters)

    def by_unit(self, unit_id: Any, *, include_closed: bool = True, **filters: Any) -> tuple[Task, ...]:
        """A unit's task history: what was cleaned, inspected, repaired."""
        statuses = None if include_closed else ACTIVE_STATUSES
        return self.list_tasks(statuses=statuses, unit_id=unit_id, **filters)

    def by_booking(self, booking_id: Any, *, include_closed: bool = True, **filters: Any) -> tuple[Task, ...]:
        """A booking's turnaround: check-in prep, checkout inspection, follow-ups."""
        statuses = None if include_closed else ACTIVE_STATUSES
        return self.list_tasks(statuses=statuses, booking_id=booking_id, **filters)

    def by_case(self, case_id: Any, *, include_closed: bool = True, **filters: Any) -> tuple[Task, ...]:
        """The operational work filed against a case."""
        statuses = None if include_closed else ACTIVE_STATUSES
        return self.list_tasks(statuses=statuses, case_id=case_id, **filters)

    def next_due(self, *, assignee: Optional[Any] = None, **filters: Any) -> Optional[Task]:
        """The single next thing on the queue, or ``None`` when it is empty."""
        if assignee is not None:
            filters["assignees"] = (_require_assignee(assignee),)
        queue = self.list_tasks(statuses=ACTIVE_STATUSES, **filters)
        return queue[0] if queue else None

    def counts_by_status(self) -> dict[str, int]:
        """Every status, zero-filled, in declaration order."""
        return self._counts("status", TASK_STATUSES)

    def counts_by_kind(self) -> dict[str, int]:
        """Every kind, zero-filled, in declaration order."""
        return self._counts("kind", TASK_KINDS)

    def _counts(self, column: str, vocabulary: Sequence[str]) -> dict[str, int]:
        rows = self.conn.execute(
            f"SELECT {column} AS value, COUNT(*) AS total FROM ops_tasks GROUP BY {column}"
        ).fetchall()
        found = {str(row["value"]): int(row["total"]) for row in rows}
        return {name: found.get(name, 0) for name in vocabulary}

    # -- transitions --------------------------------------------------------
    def transition(self, task_id: Any, status: Any, *, reason: Optional[Any] = None) -> Task:
        """Move a task along ``TASK_TRANSITIONS`` (the only way ``status`` changes).

        ``BLOCKED`` requires ``reason`` (>= 3 chars) and stamps it; every move to
        a non-``BLOCKED`` status clears it. ``DONE`` requires a finished
        checklist and stamps ``completed_at``; ``CANCELLED`` stamps it too.
        ``DONE``/``CANCELLED`` are terminal.
        """
        ref = _require_task_id(task_id)
        target = _require_status(status)
        row = self._row(ref)
        current = str(row["status"])
        if current in CLOSED_STATUSES:
            raise TaskClosed(
                f"task {ref} is {current}; {target} is not reachable from a closed task",
                code="OPS_TASK_CLOSED",
                task_id=ref,
                status=current,
            )
        if target == current:
            raise InvalidTaskTransition(
                f"task {ref} is already {current}",
                code="OPS_TASK_STATUS_UNCHANGED",
                task_id=ref,
                status=current,
                target=target,
            )
        allowed = tuple(_TRANSITION_CACHE[current])
        if target not in allowed:
            raise InvalidTaskTransition(
                f"{current} cannot move to {target}",
                code="OPS_TASK_TRANSITION_INVALID",
                task_id=ref,
                status=current,
                target=target,
                allowed=list(allowed),
            )

        completed_at: Optional[str] = None
        blocked_reason: Optional[str] = None
        if target == STATUS_BLOCKED:
            blocked_reason = _require_reason(reason)
        elif target == STATUS_DONE:
            items = _decode_checklist(row["checklist"], task_id=ref)
            open_steps = tuple(item.label for item in items if not item.done)
            if open_steps:
                raise ChecklistIncomplete(
                    f"task {ref} still has {len(open_steps)} open step(s)",
                    code="OPS_TASK_CHECKLIST_INCOMPLETE",
                    task_id=ref,
                    open_steps=list(open_steps),
                )
            completed_at = self._stamp()
        elif target == STATUS_CANCELLED:
            completed_at = self._stamp()

        moment = self._stamp()
        with self._write() as conn:
            fresh = conn.execute(
                "SELECT status FROM ops_tasks WHERE task_id = ?", (ref,)
            ).fetchone()
            if fresh is None:
                raise TaskNotFound(
                    f"no such ops task: {ref}", code="OPS_TASK_NOT_FOUND", task_id=ref
                )
            if str(fresh["status"]) != current:
                raise InvalidTaskTransition(
                    "task status changed while the command was being prepared",
                    code="OPS_TASK_TRANSITION_INVALID",
                    task_id=ref,
                    status=str(fresh["status"]),
                    target=target,
                    allowed=list(_TRANSITION_CACHE.get(str(fresh["status"]), ())),
                )
            conn.execute(
                "UPDATE ops_tasks SET status = ?, completed_at = ?, blocked_reason = ?, "
                "updated_at = ? WHERE task_id = ?",
                (target, completed_at, blocked_reason, moment, ref),
            )
        return self.get(ref)

    def start(self, task_id: Any) -> Task:
        """Begin work: ``OPEN``/``BLOCKED`` → ``IN_PROGRESS`` (clears the block)."""
        ref = _require_task_id(task_id)
        row = self._row(ref)
        current = str(row["status"])
        if current not in (STATUS_OPEN, STATUS_BLOCKED):
            raise InvalidTaskTransition(
                f"{current} cannot be started",
                code="OPS_TASK_TRANSITION_INVALID",
                task_id=ref,
                status=current,
                target=STATUS_IN_PROGRESS,
                allowed=list(_TRANSITION_CACHE[current]),
            )
        return self.transition(ref, STATUS_IN_PROGRESS)

    def block(self, task_id: Any, reason: Any) -> Task:
        """Stop work with a stated reason (>= 3 characters)."""
        return self.transition(task_id, STATUS_BLOCKED, reason=reason)

    def unblock(self, task_id: Any, *, resume: bool = True) -> Task:
        """Lift a block: back to ``IN_PROGRESS`` (default) or to the ``OPEN`` queue."""
        ref = _require_task_id(task_id)
        row = self._row(ref)
        current = str(row["status"])
        if current != STATUS_BLOCKED:
            raise TaskNotBlocked(
                f"task {ref} is {current}, not {STATUS_BLOCKED}",
                code="OPS_TASK_NOT_BLOCKED",
                task_id=ref,
                status=current,
            )
        return self.transition(ref, STATUS_IN_PROGRESS if resume else STATUS_OPEN)

    def complete(self, task_id: Any) -> Task:
        """Finish the work: refused while any checklist step is open."""
        return self.transition(task_id, STATUS_DONE)

    def cancel(self, task_id: Any) -> Task:
        """Drop the work. Terminal, and deliberately without a narrative reason."""
        return self.transition(task_id, STATUS_CANCELLED)

    # -- field commands -----------------------------------------------------
    def _update_field(self, task_id: Any, column: str, value: Any) -> Task:
        if column not in _UPDATABLE_COLUMNS:
            raise DomainError(
                f"column is not updatable by this module: {column}",
                code="OPS_TASK_COLUMN_NOT_UPDATABLE",
                column=column,
            )
        ref = _require_task_id(task_id)
        moment = self._stamp()
        with self._write() as conn:
            self._require_active_row(conn, ref)
            conn.execute(
                f"UPDATE ops_tasks SET {column} = ?, updated_at = ? WHERE task_id = ?",
                (value, moment, ref),
            )
        return self.get(ref)

    def assign(self, task_id: Any, assignee: Any) -> Task:
        """Hand the task to another owner. A blank handle is never accepted."""
        ref = _require_task_id(task_id)
        who = _require_assignee(assignee)
        row = self._row(ref)
        if str(row["assignee"]) == who:
            raise TaskFieldUnchanged(
                f"task {ref} is already assigned to {who}",
                code="OPS_TASK_ASSIGNEE_UNCHANGED",
                task_id=ref,
                field="assignee",
                value=who,
            )
        return self._update_field(ref, "assignee", who)

    def reschedule(self, task_id: Any, due_at: Any) -> Task:
        """Move the deadline (same instant is refused: a command is a change)."""
        ref = _require_task_id(task_id)
        due_value = _require_due_at(due_at)
        row = self._row(ref)
        if str(row["due_at"]) == due_value:
            raise TaskFieldUnchanged(
                f"task {ref} is already due at {due_value}",
                code="OPS_TASK_DUE_AT_UNCHANGED",
                task_id=ref,
                field="due_at",
                value=due_value,
            )
        return self._update_field(ref, "due_at", due_value)

    def set_priority(self, task_id: Any, priority: Any) -> Task:
        """Re-rank the task inside the same due window."""
        ref = _require_task_id(task_id)
        priority_value = _require_priority(priority)
        row = self._row(ref)
        if str(row["priority"]) == priority_value:
            raise TaskFieldUnchanged(
                f"task {ref} is already {priority_value}",
                code="OPS_TASK_PRIORITY_UNCHANGED",
                task_id=ref,
                field="priority",
                value=priority_value,
            )
        return self._update_field(ref, "priority", priority_value)

    def link_case(self, task_id: Any, case_id: Any) -> Task:
        """Attach the task to the case that explains it. One case, once."""
        ref = _require_task_id(task_id)
        try:
            case_ref = require_ref(case_id, "case_id", 128)
        except DomainError as exc:
            raise InvalidTaskField(
                str(exc), code="OPS_TASK_REF_INVALID", field="case_id"
            ) from exc
        row = self._row(ref)
        current_case = None if row["case_id"] is None else str(row["case_id"])
        if current_case == case_ref:
            raise TaskFieldUnchanged(
                f"task {ref} is already linked to case {case_ref}",
                code="OPS_TASK_CASE_UNCHANGED",
                task_id=ref,
                field="case_id",
                value=case_ref,
            )
        if current_case is not None:
            raise TaskAlreadyLinked(
                f"task {ref} is linked to case {current_case}; unlink is not supported",
                code="OPS_TASK_CASE_ALREADY_LINKED",
                task_id=ref,
                case_id=current_case,
            )
        with self._write() as conn:
            self._require_active_row(conn, ref)
            self._require_refs_exist(conn, {"case_id": case_ref})
        return self._update_field(ref, "case_id", case_ref)

    # -- checklist commands -------------------------------------------------
    def _mutate_checklist(
        self,
        task_id: Any,
        mutate: Callable[[tuple[ChecklistItem, ...], str], tuple[ChecklistItem, ...]],
    ) -> Task:
        ref = _require_task_id(task_id)
        with self._write() as conn:
            row = self._require_active_row(conn, ref)
            moment = self._stamp()
            items = _decode_checklist(row["checklist"], task_id=ref)
            updated = mutate(items, moment)
            conn.execute(
                "UPDATE ops_tasks SET checklist = ?, updated_at = ? WHERE task_id = ?",
                (_encode_checklist(updated), moment, ref),
            )
        return self.get(ref)

    def set_checklist(self, task_id: Any, labels: Any) -> Task:
        """Replace the whole runbook; every step starts open."""
        ref = _require_task_id(task_id)
        items = _require_checklist(labels)
        fresh = tuple(ChecklistItem(label=item.label) for item in items)
        self._row(ref)
        return self._mutate_checklist(ref, lambda _existing, _moment: fresh)

    def add_checklist_item(self, task_id: Any, label: Any) -> Task:
        """Append one runbook step (refused when the label already exists)."""
        ref = _require_task_id(task_id)
        wanted = _require_label(label)

        def mutate(items: tuple[ChecklistItem, ...], _moment: str) -> tuple[ChecklistItem, ...]:
            if any(item.label == wanted for item in items):
                raise InvalidChecklist(
                    f"checklist already has the step: {wanted}",
                    code="OPS_TASK_CHECKLIST_DUPLICATE",
                    task_id=ref,
                    label=wanted,
                )
            if len(items) >= MAX_CHECKLIST_ITEMS:
                raise InvalidChecklist(
                    f"a checklist may hold at most {MAX_CHECKLIST_ITEMS} steps",
                    code="OPS_TASK_CHECKLIST_TOO_LONG",
                    task_id=ref,
                    length=len(items),
                )
            return items + (ChecklistItem(label=wanted),)

        self._row(ref)
        return self._mutate_checklist(ref, mutate)

    def mark_checklist_item(self, task_id: Any, label: Any, *, done: bool = True) -> Task:
        """Tick (or re-open) one step, stamping ``done_at`` from the service clock."""
        ref = _require_task_id(task_id)
        wanted = _require_label(label)

        def mutate(items: tuple[ChecklistItem, ...], moment: str) -> tuple[ChecklistItem, ...]:
            updated: list[ChecklistItem] = []
            found = False
            for item in items:
                if item.label != wanted:
                    updated.append(item)
                    continue
                found = True
                if item.done == done:
                    raise TaskFieldUnchanged(
                        f"step '{wanted}' is already {'done' if done else 'open'}",
                        code="OPS_TASK_CHECKLIST_ITEM_UNCHANGED",
                        task_id=ref,
                        label=wanted,
                        done=done,
                    )
                updated.append(
                    ChecklistItem(label=item.label, done=done, done_at=moment if done else None)
                )
            if not found:
                raise InvalidChecklist(
                    f"checklist has no step: {wanted}",
                    code="OPS_TASK_CHECKLIST_ITEM_NOT_FOUND",
                    task_id=ref,
                    label=wanted,
                )
            return tuple(updated)

        self._row(ref)
        return self._mutate_checklist(ref, mutate)
