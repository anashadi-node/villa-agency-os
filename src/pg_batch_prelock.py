"""Canonical batch prelocking for PostgreSQL occupancy writes — application-path only.

The database already refuses a second occupant of one unit-night: 002's EXCLUDE constraints
guard each table and 021's trigger pair plus ``villa_lock_occupied_nights()`` guards the
cross-table case. What it cannot do is order the locks of a *multi-row* writer: the trigger
fires per row, in the executor's row order, so a statement that writes unit A then unit B can
ask for a later night before an earlier one, invert against a statement writing B then A, and
be chosen as the deadlock victim. PACK-004A reproduced that on PostgreSQL 16 with two
connections (40P01, 5/5 attempts); PACK-004B measured the correction (0 × 40P01 in 60 racing
transactions, refusals unchanged) and its limit (a writer that skips this module still
deadlocks 3/3).

This module is that correction: before the caller mutates anything, it expands the declared
stays into every ``(unit_id, night)`` identity, deduplicates them, and acquires them in one
global order through the **approved** function — never a second key format:

    SELECT public.villa_lock_occupied_nights(unit, night, night + 1 day)

Why one call per contiguous run instead of one per night: the approved function locks the
nights of its range in ascending order, so calling it for the maximal runs of one unit, units
in sorted order, reproduces exactly the sorted ``(unit_id, night)`` order the full batch would
have taken. The lock identities, their count, and their order are identical; only the number
of round trips changes. A night is expressed as a one-night range so the half-open convention
``[check_in, check_out)`` stays in the database function, not in a second copy of the key
derivation here.

Coverage — what this module does and does not buy:

* It is ``APPLICATION_PATH_MITIGATION``. A writer that does not call it (psql, an import
  script, another service, a trigger-only UPDATE) keeps the deadlock exposure of PACK-004A.
* It is not a replacement for the database guards. It takes the same locks the guards take;
  the guards still decide whether a row is allowed. Removing 021 would make this module a
  mutual-exclusion device with no invariant behind it. Installing this module on a database
  without 021 therefore fails loudly (the helper and ``villa_occupying_states`` do not exist).
* It does not retry, and it must not be described as making "all PostgreSQL multi-row
  operations" safe. RISK-M1 stays open for non-participating writers.

Failure contract (no retries in this module, by design):

    SQLSTATE  classification                  initial behaviour
    40P01     DEADLOCK                        rollback; retry candidate
    55P03     LOCK_UNAVAILABLE               rollback; conditional retry candidate
    57014     QUERY_CANCELED                 rollback; fail closed
    53200     SHARED_MEMORY_EXHAUSTED        rollback; fail closed
    23P01     EXCLUSION_VIOLATION            rollback; domain conflict
    23505     UNIQUE_VIOLATION               rollback; inspect idempotency/conflict
    40001     SERIALIZATION_FAILURE          rollback; report separately until policy approved

A future retry must repeat the entire eligible transaction with the same idempotency identity.
Every classification is data (``RetryHint``), never an instruction to loop here.

No DSN, credential, guest identifier or payment detail is read, logged or embedded by this
module; it receives an open connection and text arguments.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

#: Half-open ``[check_in, check_out)``, ISO-8601 calendar dates only. A locale-dependent
#: rendering (``01/06/2027``) is not accepted: 021 renders lock keys with ``to_char`` for the
#: same reason — two sessions must never mint two keys for one night.
_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_MAX_UNIT_LEN = 128
_ONE_DAY = _dt.timedelta(days=1)


@dataclasses.dataclass(frozen=True)
class Limits:
    """Bounded validation, checked independently. PROPOSED prototype values, not certified.

    ``max_rows_per_batch`` alone cannot bound the lock count (20 stays × 365 nights = 7 300
    locks), so the three limits are enforced separately and a batch must satisfy all of them.
    A production ceiling needs Owner approval and environment-specific measurement.
    """

    max_distinct_locks: int = 5000
    max_nights_per_stay: int = 365
    max_rows_per_batch: int = 20


DEFAULT_LIMITS = Limits()


@dataclasses.dataclass(frozen=True)
class Stay:
    """One declared occupancy slot: unit, check-in, check-out (ISO dates)."""

    unit_id: str
    check_in: str
    check_out: str

    def nights(self) -> tuple[_dt.date, ...]:
        first, last = _dt.date.fromisoformat(self.check_in), _dt.date.fromisoformat(self.check_out)
        return tuple(first + i * _ONE_DAY for i in range((last - first).days))


@dataclasses.dataclass(frozen=True)
class LockPlan:
    """The canonical lock inventory: what will be locked, in which order, before any write."""

    stays: tuple[Stay, ...]
    nights: tuple[tuple[str, _dt.date], ...]  # sorted (unit_id, night), deduplicated
    ranges: tuple[tuple[str, str, str], ...]  # (unit_id, first_night, day_after_last) calls

    @property
    def lock_count(self) -> int:
        return len(self.nights)


class PrelockError(Exception):
    """Base class for every deliberate refusal from this module."""

    code = "PRELOCK_ERROR"
    retry_hint = "no_retry"

    def __init__(self, message: str, *, sqlstate: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.sqlstate = sqlstate
        self.details = details


class SlotRejected(PrelockError):
    """A stay descriptor is unusable (empty unit, bad date, inverted or zero-night range)."""

    code = "SLOT_REJECTED"


class BatchCapacityRefused(PrelockError):
    """A limit would be exceeded; refused before any lock or row is touched."""

    code = "BATCH_CAPACITY_REFUSED"


class UnsafeTransactionState(PrelockError):
    """The connection cannot be used safely: autocommit on, or a transaction already open."""

    code = "UNSAFE_TRANSACTION_STATE"


class UnsupportedOperation(PrelockError):
    """The operation cannot be represented through the bounded contract of this module."""

    code = "UNSUPPORTED_OPERATION"


class IntegrityViolation(PrelockError):
    """Occupancy invariant broken inside the transaction: the caller must roll back."""

    code = "INTEGRITY_VIOLATION"


@dataclasses.dataclass(frozen=True)
class Classification:
    name: str
    retry_hint: str


#: SQLSTATE → (classification, retry hint). Unknown codes are classified ``UNMAPPED``: they are
#: surfaced as-is rather than guessed into a retryable bucket.
SQLSTATE_CLASSES: dict[str, Classification] = {
    "40P01": Classification("DEADLOCK", "retry_candidate"),
    "55P03": Classification("LOCK_UNAVAILABLE", "conditional_retry_candidate"),
    "57014": Classification("QUERY_CANCELED", "fail_closed"),
    "53200": Classification("SHARED_MEMORY_EXHAUSTED", "fail_closed"),
    "23P01": Classification("EXCLUSION_VIOLATION", "domain_conflict"),
    "23505": Classification("UNIQUE_VIOLATION", "inspect_idempotency_or_conflict"),
    "40001": Classification("SERIALIZATION_FAILURE", "report_separately"),
}
UNMAPPED = Classification("UNMAPPED", "no_retry")


class ClassifiedDatabaseError(PrelockError):
    """A driver error carrying a stable classification — never the raw message as contract."""

    code = "DATABASE_ERROR"

    def __init__(self, error: BaseException) -> None:
        sqlstate = str(getattr(error, "sqlstate", "") or "") or None
        classification = SQLSTATE_CLASSES.get(sqlstate or "", UNMAPPED)
        super().__init__(
            f"{classification.name}: {type(error).__name__}",
            sqlstate=sqlstate,
            classification=classification.name,
            retry_hint=classification.retry_hint,
            origin=type(error).__name__,
        )
        self.retry_hint = classification.retry_hint
        self.raw = error


def classify(error: BaseException) -> Classification:
    """Classification of a driver error without raising (tests and callers use this)."""
    return SQLSTATE_CLASSES.get(str(getattr(error, "sqlstate", "") or ""), UNMAPPED)


def _unit(value: Any) -> str:
    if not isinstance(value, str):
        raise SlotRejected("unit_id must be a string", field="unit_id", value_type=type(value).__name__)
    unit = value
    if not unit or unit != unit.strip():
        raise SlotRejected("unit_id must be non-empty and unpadded", field="unit_id")
    if "\x00" in unit or len(unit) > _MAX_UNIT_LEN:
        raise SlotRejected("unit_id is not a usable identifier", field="unit_id", length=len(unit))
    return unit


def _date(value: Any, field: str) -> _dt.date:
    if not isinstance(value, str) or not _DATE.match(value):
        raise SlotRejected(f"{field} must be an ISO date (YYYY-MM-DD)", field=field)
    try:
        return _dt.date.fromisoformat(value)
    except ValueError as error:  # 2027-02-30 and friends
        raise SlotRejected(f"{field} is not a calendar date", field=field) from error


def parse_stay(slot: "Stay | dict[str, Any] | Sequence[Any]") -> Stay:
    """Validate one declared slot. Accepts ``Stay``, a mapping, or a 3-sequence."""
    if isinstance(slot, Stay):
        raw = slot
    elif isinstance(slot, dict):
        missing = {"unit_id", "check_in", "check_out"} - set(slot)
        if missing:
            raise SlotRejected("slot is missing required keys", field="slot", missing=sorted(missing))
        raw = Stay(slot["unit_id"], slot["check_in"], slot["check_out"])
    elif isinstance(slot, (tuple, list)) and len(slot) == 3:
        raw = Stay(slot[0], slot[1], slot[2])
    else:
        raise UnsupportedOperation(
            "a slot must be a Stay, a mapping or a 3-item sequence of (unit, check_in, check_out)"
        )
    unit = _unit(raw.unit_id)
    first = _date(raw.check_in, "check_in")
    last = _date(raw.check_out, "check_out")
    if last <= first:
        raise SlotRejected("check_out must be after check_in (half-open [in, out))", unit_id=unit)
    return Stay(unit, first.isoformat(), last.isoformat())


def canonical_plan(
    slots: Iterable["Stay | dict[str, Any] | Sequence[Any]"],
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> LockPlan:
    """Validate, expand, deduplicate and sort the complete lock set — before any SQL runs."""
    if limits.max_rows_per_batch < 1 or limits.max_distinct_locks < 1 or limits.max_nights_per_stay < 1:
        raise UnsupportedOperation("limits must be positive integers")
    if isinstance(slots, (str, bytes)) or not isinstance(slots, Iterable):
        raise UnsupportedOperation("slots must be an iterable of stay descriptors")
    raw = list(slots)
    if not raw:
        raise SlotRejected("an empty batch has no lock set to prove; refusing", field="slots")
    if len(raw) > limits.max_rows_per_batch:
        raise BatchCapacityRefused(
            f"batch of {len(raw)} rows exceeds the {limits.max_rows_per_batch}-row limit",
            rows=len(raw),
            limit=limits.max_rows_per_batch,
        )

    stays = tuple(parse_stay(slot) for slot in raw)
    nights: set[tuple[str, _dt.date]] = set()
    for stay in stays:
        span = stay.nights()
        if len(span) > limits.max_nights_per_stay:
            raise BatchCapacityRefused(
                f"stay of {len(span)} nights exceeds the {limits.max_nights_per_stay}-night limit",
                unit_id=stay.unit_id,
                nights=len(span),
                limit=limits.max_nights_per_stay,
            )
        nights.update((stay.unit_id, night) for night in span)
    if len(nights) > limits.max_distinct_locks:
        raise BatchCapacityRefused(
            f"batch needs {len(nights)} distinct locks, above the {limits.max_distinct_locks} limit",
            distinct_locks=len(nights),
            limit=limits.max_distinct_locks,
        )

    ordered = tuple(sorted(nights, key=lambda item: (item[0], item[1])))
    ranges: list[tuple[str, str, str]] = []
    index = 0
    while index < len(ordered):
        unit, first = ordered[index]
        end = first
        index += 1
        while index < len(ordered) and ordered[index][0] == unit and ordered[index][1] == end + _ONE_DAY:
            end = ordered[index][1]
            index += 1
        ranges.append((unit, first.isoformat(), (end + _ONE_DAY).isoformat()))
    return LockPlan(stays=stays, nights=ordered, ranges=tuple(ranges))


def _transaction(conn: Any) -> Any:
    """Use the driver's transaction context when it has one (psycopg 3), else the connection."""
    factory = getattr(conn, "transaction", None)
    return factory() if callable(factory) else conn


def _status(conn: Any) -> int:
    info = getattr(conn, "info", None)
    status = getattr(info, "transaction_status", None)
    if status is None:
        return -1
    try:
        return int(status)
    except (TypeError, ValueError):
        return -1


def _require_own_transaction(conn: Any) -> None:
    """Refuse to join a transaction that may already hold conflicting occupancy locks."""
    if getattr(conn, "autocommit", False):
        raise UnsafeTransactionState("autocommit is on; batch prelocking needs a transaction")
    if _status(conn) != 0:
        raise UnsafeTransactionState(
            "connection is already inside a transaction; the complete lock set cannot be "
            "computed before the first occupancy write",
        )


def _run(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    try:
        cursor = conn.execute(sql, params) if params else conn.execute(sql)
    except Exception as error:  # driver errors carry sqlstate; anything else is re-raised
        if getattr(error, "sqlstate", None):
            raise ClassifiedDatabaseError(error) from error
        raise
    return cursor


def _require_canonical(plan: LockPlan) -> None:
    """A hand-built LockPlan must still be sorted: order is the whole correction."""
    if not plan.ranges or not plan.nights:
        raise UnsupportedOperation("a lock plan needs at least one unit-night")
    if list(plan.nights) != sorted(plan.nights, key=lambda item: (item[0], item[1])):
        raise UnsupportedOperation("lock nights are not in canonical (unit, night) order")
    if len({(unit, night) for unit, night in plan.nights}) != len(plan.nights):
        raise UnsupportedOperation("lock plan contains duplicate unit-night identities")
    keys = [(unit, start) for unit, start, _end in plan.ranges]
    if keys != sorted(keys):
        raise UnsupportedOperation("lock ranges are not in canonical (unit, first night) order")
    for _unit, start, end in plan.ranges:
        if _dt.date.fromisoformat(end) <= _dt.date.fromisoformat(start):
            raise UnsupportedOperation("a lock range must end after it starts")
    covered: set[tuple[str, _dt.date]] = set()
    for unit, start, end in plan.ranges:
        first, last = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
        covered.update((unit, first + i * _ONE_DAY) for i in range((last - first).days))
    if covered != set(plan.nights):
        raise UnsupportedOperation(
            "lock ranges do not cover exactly the declared nights; a plan that locks less than "
            "it declares is a subset pretending to be complete"
        )


def _acquire(conn: Any, plan: LockPlan) -> LockPlan:
    _require_canonical(plan)
    for unit, start, end in plan.ranges:
        _run(conn, "SELECT public.villa_lock_occupied_nights(%s, %s, %s)", (unit, start, end))
    return plan


def lock_batch(conn: Any, plan: LockPlan) -> LockPlan:
    """Take every lock in the plan, in canonical order. Caller owns the transaction.

    Must be the first statement of that transaction: joining a transaction that already wrote
    an occupancy row means the complete lock set is no longer knowable, and the plan would be
    a subset pretending to be complete.
    """
    _require_own_transaction(conn)
    return _acquire(conn, plan)


def prelock(
    conn: Any,
    slots: Iterable["Stay | dict[str, Any] | Sequence[Any]"],
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> LockPlan:
    """Validate and lock, without running a write. For callers that own the transaction."""
    return lock_batch(conn, canonical_plan(slots, limits=limits))


def occupancy_conflicts(conn: Any, plan: LockPlan) -> int:
    """Cross-table overlaps among occupying rows on the plan's units, read inside the txn.

    This is the one invariant a batch can break that 002's per-table EXCLUDE cannot see, so it
    is the one worth re-reading after the write. It reports, it does not repair.
    """
    units = sorted({unit for unit, _ in plan.nights})
    row = _run(
        conn,
        "SELECT count(*) FROM public.holds h JOIN public.bookings b"
        " ON b.unit_id = h.unit_id"
        " AND daterange(public.villa_iso_date(h.check_in), public.villa_iso_date(h.check_out), '[)')"
        "  && daterange(public.villa_iso_date(b.check_in), public.villa_iso_date(b.check_out), '[)')"
        " WHERE h.unit_id = ANY(%s)"
        " AND h.status = ANY(public.villa_occupying_states('hold'))"
        " AND b.status = ANY(public.villa_occupying_states('booking'))",
        (units,),
    ).fetchone()
    return int(row[0]) if row else 0


def prelock_and_run(
    conn: Any,
    slots: Iterable["Stay | dict[str, Any] | Sequence[Any]"],
    operation: Callable[[Any], Any],
    *,
    limits: Limits = DEFAULT_LIMITS,
    verify: bool = True,
) -> Any:
    """Own the transaction: validate → prelock everything → run ``operation`` → verify → commit.

    ``operation`` receives the connection and must restrict itself to the declared slots. It
    must not call the network, a supplier, mail, or a payment: those belong outside a
    transaction that can still roll back. Any exception rolls the whole transaction back; a
    driver error is re-raised as :class:`ClassifiedDatabaseError` with a stable classification.
    """
    _require_own_transaction(conn)
    plan = canonical_plan(slots, limits=limits)
    try:
        with _transaction(conn):
            _acquire(conn, plan)
            result = operation(conn)
            if verify and occupancy_conflicts(conn, plan):
                raise IntegrityViolation(
                    "cross-table occupancy overlap inside the transaction; rolled back",
                    units=sorted({unit for unit, _ in plan.nights}),
                )
        return result
    except PrelockError:
        raise
    except Exception as error:
        if getattr(error, "sqlstate", None):
            raise ClassifiedDatabaseError(error) from error
        raise
