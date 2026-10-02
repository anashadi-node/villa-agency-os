"""The transactional outbox — a typed letter, written with the state it describes.

Why this module exists (ADR-0007, Master Spec §14)
--------------------------------------------------
Two failures it removes, and one it refuses to hide:

* **A letter written after the business row commits can be lost.** The write and the commit
  are two moments; a crash between them loses the notification while the booking says it was
  sent. Here the event is inserted **in the same transaction as the state it describes**, so
  a crash loses both or neither. A later outage delays delivery; it never rewrites truth.
* **A retry can duplicate.** Every event carries an ``idempotency_key`` that is unique *per
  agency*, so a replayed emit returns the original letter instead of writing a second one.
* **A free-form message cannot be replayed, audited, or refused.** §14 forbids agents talking
  to each other in prose: this envelope is ``event_type`` + ``aggregate`` + a JSON payload
  whose digest is stored, which is what makes "who said what to whom, and can it be proven"
  answerable a year later.

What moves, and what never moves
--------------------------------
The **content** is immutable after insert — a trigger refuses an update that touches the
type, the aggregate, the payload, the actor, the correlation, or the idempotency key.
The **delivery state** moves: ``status``, ``attempts``, ``next_attempt_at``, ``published_at``,
``last_error``. A queue whose letters can be edited after the fact is not evidence, and a
queue whose state cannot move is not a queue.

Publishing is a *callback*, not a network call
---------------------------------------------
:meth:`OutboxService.publish_due` takes the publisher as an argument. This module owns the
record and the retry discipline; the transport (email, webhook, chat) lives elsewhere. That
keeps the module free of a client library, and it is what lets the tests prove the retry,
the dead-letter and the "an outage does not touch the business row" rules without a network.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Mapping, Optional

from primitives import (
    Clock,
    DomainError,
    IdFactory,
    SystemClock,
    canonical_json,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    "BACKOFF_MINUTES",
    "DEFAULT_MAX_ATTEMPTS",
    "DELIVERY_OUTCOMES",
    "EVENT_STATUSES",
    "IntegrationConflict",
    "IntegrationInbox",
    "IntegrationReceipt",
    "InvalidEvent",
    "OutboxError",
    "OutboxEvent",
    "OutboxEventNotFound",
    "OutboxIdempotencyConflict",
    "OutboxService",
    "PublishReport",
    "EventNotClaimable",
    "backoff_minutes",
]

EVENT_STATUSES: tuple[str, ...] = ("PENDING", "PUBLISHED", "FAILED", "DEAD_LETTER")
PENDING, PUBLISHED, FAILED, DEAD_LETTER = EVENT_STATUSES
DELIVERY_OUTCOMES: tuple[str, ...] = ("PUBLISHED", "FAILED", "REQUEUED")

DEFAULT_MAX_ATTEMPTS = 5

#: Minutes to wait after attempt *n* has failed. Deliberately increasing, and deliberately
#: local: a table of numbers rather than a policy key, because the *retry schedule* is an
#: engineering property of the queue, not a business decision the owner sets per agency.
BACKOFF_MINUTES: tuple[int, ...] = (1, 5, 15, 60, 240)

MAX_ERROR_LENGTH = 500
MAX_EVENT_TYPE_LENGTH = 64
MAX_PAYLOAD_BYTES = 200_000
MIN_IDEMPOTENCY_KEY_LENGTH = 8
MIN_REASON_LENGTH = 3


class OutboxError(DomainError):
    """Base class for every refusal this module makes."""

    code = "OUTBOX_ERROR"


class InvalidEvent(OutboxError):
    code = "INVALID_EVENT"


class OutboxEventNotFound(OutboxError):
    code = "OUTBOX_EVENT_NOT_FOUND"


class OutboxIdempotencyConflict(OutboxError):
    """The same idempotency key arrived with different content — a replay that is not a replay."""

    code = "IDEMPOTENCY_CONFLICT"


class EventNotClaimable(OutboxError):
    """The event is in a state where the requested transition makes no sense."""

    code = "EVENT_NOT_CLAIMABLE"


class IntegrationConflict(OutboxError):
    """A source re-sent an id it already used, with different content."""

    code = "INTEGRATION_IDENTITY_CONFLICT"


def backoff_minutes(attempts: int) -> int:
    """Minutes to wait after ``attempts`` failures. Beyond the table the last value holds."""
    if attempts < 1:
        raise InvalidEvent("attempts must be at least 1", attempts=attempts)
    index = min(attempts, len(BACKOFF_MINUTES)) - 1
    return BACKOFF_MINUTES[index]


def _is_sqlite(conn: Any) -> bool:
    return type(conn).__module__.split(".", 1)[0] == "sqlite3"


@contextmanager
def _write_transaction(conn: Any) -> Iterator[None]:
    """Run the body in an IMMEDIATE transaction, or join the caller's.

    Joining matters: the whole point of a transactional outbox is that the caller's state
    change and this insert commit together. Nesting a BEGIN inside the caller's transaction
    is an error in SQLite, and committing their work would be worse.
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


@dataclass(frozen=True)
class OutboxEvent:
    """One stored letter, exactly as the table holds it."""

    outbox_event_id: str
    agency_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: str
    payload_sha256: str
    actor: str
    correlation_id: str
    causation_id: Optional[str]
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: str
    published_at: Optional[str]
    last_error: Optional[str]
    idempotency_key: str
    created_at: str

    @property
    def payload_json(self) -> Mapping[str, Any]:
        """The payload as a mapping. Read-only by convention: the digest is the evidence."""
        loaded = json.loads(self.payload)
        if not isinstance(loaded, dict):  # pragma: no cover - the writer only stores objects
            raise OutboxError("a stored payload must be a JSON object")
        return loaded

    @property
    def is_due(self) -> bool:  # pragma: no cover - convenience for callers/tests
        return self.status in (PENDING, FAILED)


@dataclass(frozen=True)
class PublishReport:
    """What one pass of the publisher did — a value, so a caller can log or assert on it."""

    published: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    dead_lettered: tuple[str, ...] = ()

    @property
    def considered(self) -> int:
        return len(self.published) + len(self.failed) + len(self.dead_lettered)


_EVENT_COLUMNS = (
    "outbox_event_id, agency_id, event_type, aggregate_type, aggregate_id, payload,"
    " payload_sha256, actor, correlation_id, causation_id, status, attempts, max_attempts,"
    " next_attempt_at, published_at, last_error, idempotency_key, created_at"
)


def _event(row: Mapping[str, Any]) -> OutboxEvent:
    return OutboxEvent(
        outbox_event_id=row["outbox_event_id"],
        agency_id=row["agency_id"],
        event_type=row["event_type"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        payload=row["payload"],
        payload_sha256=row["payload_sha256"],
        actor=row["actor"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        status=row["status"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        next_attempt_at=row["next_attempt_at"],
        published_at=row["published_at"],
        last_error=row["last_error"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
    )


class OutboxService:
    """The single writer of ``outbox_events`` and its delivery log."""

    def __init__(
        self,
        conn: Any,
        clock: Optional[Clock] = None,
        ids: Optional[IdFactory] = None,
        *,
        agency_id: str = "agency_default",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if not _is_sqlite(conn):
            raise OutboxError(
                "the outbox publishes in the caller's transaction; it needs the sqlite driver "
                "used by this deployment",
                code="UNSUPPORTED_DRIVER",
            )
        self._conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ids: IdFactory = ids if ids is not None else IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id", max_len=64)
        self.max_attempts = self._attempt_limit(max_attempts)

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _attempt_limit(value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise InvalidEvent("max_attempts must be an integer", got=repr(value)) from exc
        if limit < 1:
            raise InvalidEvent("max_attempts must be at least 1", got=limit)
        return limit

    def _now(self, at: Optional[datetime]) -> datetime:
        if at is None:
            return self._clock.now()
        if at.tzinfo is None:
            raise InvalidEvent("at must carry a timezone", at=str(at))
        return at

    # -- writing ---------------------------------------------------------- #
    def emit(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        actor: str,
        idempotency_key: str,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> OutboxEvent:
        """Insert one typed event, in the caller's transaction when there is one.

        A replay of the same ``idempotency_key`` with the same content returns the original
        event; the same key with different content is a conflict, never a silent second letter.
        """
        event_type = require_text(event_type, "event_type", max_len=MAX_EVENT_TYPE_LENGTH)
        aggregate_type = require_text(aggregate_type, "aggregate_type", max_len=MAX_EVENT_TYPE_LENGTH)
        aggregate_id = require_ref(aggregate_id, "aggregate_id", max_len=128)
        actor = require_ref(actor, "actor", max_len=128)
        key = require_text(idempotency_key, "idempotency_key", max_len=128)
        if len(key) < MIN_IDEMPOTENCY_KEY_LENGTH:
            raise InvalidEvent(
                f"idempotency_key must be at least {MIN_IDEMPOTENCY_KEY_LENGTH} characters",
                field="idempotency_key",
            )
        if not isinstance(payload, Mapping):
            raise InvalidEvent("payload must be a mapping", field="payload", got=type(payload).__name__)
        text = canonical_json(dict(payload))
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise InvalidEvent("payload is too large to be a control message", field="payload")

        event_id = self._ids.new("evt")
        correlation = (
            require_ref(correlation_id, "correlation_id", max_len=128)
            if correlation_id is not None
            else event_id
        )
        cause = (
            require_ref(causation_id, "causation_id", max_len=128)
            if causation_id is not None
            else None
        )
        if cause == event_id:
            raise InvalidEvent("an event may not cause itself", field="causation_id")

        digest = sha256_hex(text)
        moment = self._now(at)
        created = to_iso(moment)

        with _write_transaction(self._conn):
            existing = self._by_key(key)
            if existing is not None:
                if existing.payload_sha256 == digest and existing.event_type == event_type:
                    return existing
                raise OutboxIdempotencyConflict(
                    "this idempotency key was already used with different content",
                    idempotency_key=key,
                    outbox_event_id=existing.outbox_event_id,
                )
            self._conn.execute(
                "INSERT INTO outbox_events ("
                + _EVENT_COLUMNS
                + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    self.agency_id,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    text,
                    digest,
                    actor,
                    correlation,
                    cause,
                    PENDING,
                    0,
                    self.max_attempts,
                    created,
                    None,
                    None,
                    key,
                    created,
                ),
            )
        stored = self.get(event_id, at=moment)
        return stored

    def get(self, event_id: str, *, at: Optional[datetime] = None) -> OutboxEvent:
        event_id = require_ref(event_id, "outbox_event_id", max_len=128)
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM outbox_events WHERE outbox_event_id = ? AND agency_id = ?",
            (event_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise OutboxEventNotFound(
                "no such outbox event for this agency",
                outbox_event_id=event_id,
                agency_id=self.agency_id,
            )
        return _event(row)

    def pending(
        self, *, limit: Optional[int] = None, at: Optional[datetime] = None
    ) -> tuple[OutboxEvent, ...]:
        """Events that are due now, oldest first: what a publisher worker should take."""
        moment = self._now(at)
        sql = (
            f"SELECT {_EVENT_COLUMNS} FROM outbox_events"
            " WHERE agency_id = ? AND status IN (?, ?) AND next_attempt_at <= ?"
            " ORDER BY next_attempt_at, created_at, outbox_event_id"
        )
        params: list[Any] = [self.agency_id, PENDING, FAILED, to_iso(moment)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return tuple(_event(row) for row in self._conn.execute(sql, tuple(params)).fetchall())

    def dead_letters(self, *, limit: Optional[int] = None) -> tuple[OutboxEvent, ...]:
        sql = (
            f"SELECT {_EVENT_COLUMNS} FROM outbox_events"
            " WHERE agency_id = ? AND status = ? ORDER BY created_at, outbox_event_id"
        )
        params: list[Any] = [self.agency_id, DEAD_LETTER]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return tuple(_event(row) for row in self._conn.execute(sql, tuple(params)).fetchall())

    def deliveries(self, event_id: str) -> tuple[Mapping[str, Any], ...]:
        """The attempt log: one row per publish attempt, plus one per human re-queue."""
        event = self.get(event_id)
        rows = self._conn.execute(
            "SELECT delivery_id, attempt_no, outcome, error, actor, reason, attempted_at"
            " FROM outbox_deliveries WHERE outbox_event_id = ? ORDER BY attempt_no",
            (event.outbox_event_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def publish_due(
        self,
        publish: Callable[[OutboxEvent], None],
        *,
        limit: int = 100,
        at: Optional[datetime] = None,
    ) -> PublishReport:
        """Hand every due event to ``publish``; record the outcome of each attempt.

        One event's failure never stops the others: each attempt runs in its own transaction,
        and a raising publisher is a *delivery* failure, not an error for the caller. When a
        failure reaches ``max_attempts`` the event becomes a DEAD_LETTER — kept, not dropped,
        so the report survives and a human can re-queue it with a reason.
        """
        if not callable(publish):
            raise InvalidEvent("publish must be callable", got=type(publish).__name__)
        moment = self._now(at)
        published: list[str] = []
        failed: list[str] = []
        dead: list[str] = []
        for event in self.pending(limit=limit, at=moment):
            try:
                publish(event)
            except Exception as exc:  # noqa: BLE001 - a transport failure is data, not a bug here
                outcome = self._record_failure(event, exc, at=moment)
                (dead if outcome == DEAD_LETTER else failed).append(event.outbox_event_id)
            else:
                self._record_success(event, at=moment)
                published.append(event.outbox_event_id)
        return PublishReport(
            published=tuple(published), failed=tuple(failed), dead_lettered=tuple(dead)
        )

    def requeue(
        self, event_id: str, *, actor: str, reason: str, at: Optional[datetime] = None
    ) -> OutboxEvent:
        """Give a dead letter one more chance, and record who decided that and why.

        Only a FAILED or DEAD_LETTER event can be re-queued: a published letter is done, and
        sending it twice is the duplicate the idempotency key exists to prevent.
        """
        try:
            actor = require_ref(actor, "actor", max_len=128)
        except DomainError as exc:
            raise InvalidEvent("a re-queue names the human who decided it", field="actor") from exc
        try:
            reason = require_text(reason, "reason", max_len=MAX_ERROR_LENGTH)
        except DomainError as exc:
            raise InvalidEvent("a re-queue needs a reason", field="reason") from exc
        if len(reason) < MIN_REASON_LENGTH:
            raise InvalidEvent("a re-queue needs a reason", field="reason")
        moment = self._now(at)
        with _write_transaction(self._conn):
            event = self.get(event_id)
            if event.status not in (FAILED, DEAD_LETTER):
                raise EventNotClaimable(
                    "only a failed or dead-lettered event can be re-queued",
                    outbox_event_id=event.outbox_event_id,
                    status=event.status,
                )
            attempts = self._next_attempt_no(event)
            self._conn.execute(
                "UPDATE outbox_events SET status = ?, next_attempt_at = ?, max_attempts = ?,"
                " last_error = ? WHERE outbox_event_id = ?",
                (
                    PENDING,
                    to_iso(moment),
                    max(event.max_attempts, event.attempts) + 1,
                    f"re-queued by {actor}: {reason}",
                    event.outbox_event_id,
                ),
            )
            self._conn.execute(
                "INSERT INTO outbox_deliveries (delivery_id, outbox_event_id, agency_id, attempt_no,"
                " outcome, error, actor, reason, attempted_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    self._ids.new("dlv"),
                    event.outbox_event_id,
                    self.agency_id,
                    attempts,
                    "REQUEUED",
                    actor,
                    reason,
                    to_iso(moment),
                ),
            )
        return self.get(event.outbox_event_id)

    # -- internals -------------------------------------------------------- #
    def _by_key(self, idempotency_key: str) -> Optional[OutboxEvent]:
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM outbox_events WHERE agency_id = ? AND idempotency_key = ?",
            (self.agency_id, idempotency_key),
        ).fetchone()
        return None if row is None else _event(row)

    def _next_attempt_no(self, event: OutboxEvent) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) AS n FROM outbox_deliveries WHERE outbox_event_id = ?",
            (event.outbox_event_id,),
        ).fetchone()
        return int(row["n"]) + 1

    def _record_success(self, event: OutboxEvent, *, at: datetime) -> None:
        moment = to_iso(at)
        with _write_transaction(self._conn):
            attempt = self._next_attempt_no(event)
            self._conn.execute(
                "UPDATE outbox_events SET status = ?, attempts = attempts + 1, published_at = ?,"
                " last_error = NULL WHERE outbox_event_id = ?",
                (PUBLISHED, moment, event.outbox_event_id),
            )
            self._conn.execute(
                "INSERT INTO outbox_deliveries (delivery_id, outbox_event_id, agency_id, attempt_no,"
                " outcome, error, actor, reason, attempted_at) VALUES (?, ?, ?, ?, ?, NULL, NULL,"
                " NULL, ?)",
                (self._ids.new("dlv"), event.outbox_event_id, self.agency_id, attempt, "PUBLISHED", moment),
            )

    def _record_failure(self, event: OutboxEvent, exc: Exception, *, at: datetime) -> str:
        message = require_text(str(exc) or exc.__class__.__name__, "error", max_len=MAX_ERROR_LENGTH)
        attempts = event.attempts + 1
        status = DEAD_LETTER if attempts >= event.max_attempts else FAILED
        retry_at = at + timedelta(minutes=backoff_minutes(attempts))
        with _write_transaction(self._conn):
            attempt = self._next_attempt_no(event)
            self._conn.execute(
                "UPDATE outbox_events SET status = ?, attempts = ?, next_attempt_at = ?,"
                " last_error = ? WHERE outbox_event_id = ?",
                (status, attempts, to_iso(retry_at), message, event.outbox_event_id),
            )
            self._conn.execute(
                "INSERT INTO outbox_deliveries (delivery_id, outbox_event_id, agency_id, attempt_no,"
                " outcome, error, actor, reason, attempted_at) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (
                    self._ids.new("dlv"),
                    event.outbox_event_id,
                    self.agency_id,
                    attempt,
                    "FAILED",
                    message,
                    to_iso(at),
                ),
            )
        return status


@dataclass(frozen=True)
class IntegrationReceipt:
    """What the inbox says about an inbound delivery: the row, and whether it is new."""

    inbox_id: str
    source: str
    external_id: str
    is_new: bool
    processed_at: Optional[str]


class IntegrationInbox:
    """Idempotent consumer for inbound webhooks (ADR-0007).

    A provider retries. The inbox makes the retry harmless: ``(agency, source, external_id)``
    is unique, so the second delivery of the same message returns the first receipt with
    ``is_new=False`` and the caller skips its side effects. The same id with *different*
    content is refused rather than overwritten — that pattern is either a provider bug or an
    attempt to rewrite history, and both deserve a human, not a silent update.
    """

    def __init__(self, conn: Any, clock: Optional[Clock] = None, ids: Optional[IdFactory] = None,
                 *, agency_id: str = "agency_default") -> None:
        self._conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ids: IdFactory = ids if ids is not None else IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id", max_len=64)

    def receive(self, *, source: str, external_id: str, payload: Mapping[str, Any],
                at: Optional[datetime] = None) -> IntegrationReceipt:
        source = require_text(source, "source", max_len=64)
        external_id = require_text(external_id, "external_id", max_len=128)
        if not isinstance(payload, Mapping):
            raise InvalidEvent("payload must be a mapping", field="payload")
        digest = sha256_hex(canonical_json(dict(payload)))
        moment = to_iso(at or self._clock.now())
        existing = self._conn.execute(
            "SELECT inbox_id, source, external_id, payload_sha256, processed_at FROM integration_inbox"
            " WHERE agency_id = ? AND source = ? AND external_id = ?",
            (self.agency_id, source, external_id),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != digest:
                raise IntegrationConflict(
                    "this source re-used an id with different content",
                    source=source,
                    external_id=external_id,
                    inbox_id=existing["inbox_id"],
                )
            return IntegrationReceipt(
                inbox_id=existing["inbox_id"], source=source, external_id=external_id,
                is_new=False, processed_at=existing["processed_at"],
            )
        inbox_id = self._ids.new("inb")
        with _write_transaction(self._conn):
            self._conn.execute(
                "INSERT INTO integration_inbox (inbox_id, agency_id, source, external_id,"
                " payload_sha256, received_at, processed_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (inbox_id, self.agency_id, source, external_id, digest, moment),
            )
        return IntegrationReceipt(
            inbox_id=inbox_id, source=source, external_id=external_id, is_new=True, processed_at=None
        )

    def mark_processed(self, inbox_id: str, *, at: Optional[datetime] = None) -> None:
        inbox_id = require_ref(inbox_id, "inbox_id", max_len=128)
        with _write_transaction(self._conn):
            updated = self._conn.execute(
                "UPDATE integration_inbox SET processed_at = ? WHERE inbox_id = ? AND agency_id = ?"
                " AND processed_at IS NULL",
                (to_iso(at or self._clock.now()), inbox_id, self.agency_id),
            ).rowcount
        if not updated:
            raise OutboxEventNotFound(
                "no unprocessed inbox row with that id for this agency", inbox_id=inbox_id
            )
