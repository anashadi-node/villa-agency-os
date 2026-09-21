"""Append-only, tamper-evident, PII-safe audit trail (``audit_events``).

Three guarantees, each enforced where it can actually be enforced:

1. **Append-only** — this module only ever INSERTs and SELECTs (no update/delete
   code path exists to be called by accident), and the schema's
   ``trg_audit_events_no_update`` / ``trg_audit_events_no_delete`` triggers make
   the database itself refuse a rewrite. :func:`guard_append_only` fails closed at
   construction time if those triggers are missing, so a dropped guard cannot go
   unnoticed.
2. **Tamper-evident** — every row carries ``prev_hash`` and ``hash``, where
   ``hash = SHA-256(domain ‖ prev_hash ‖ canonical_json(body))`` over the *stored
   bytes* of that row. Editing, reordering, removing or inserting a row breaks the
   chain at that point and :meth:`AuditLog.verify` names the first broken link.
   Removing the tail is invisible in the data alone, so :meth:`AuditLog.anchor`
   publishes ``(count, head_hash)`` for storage outside the database and
   :meth:`AuditLog.verify(anchor=...)` re-checks it.
3. **PII-safe** — states and free text are sanitised *before* they are hashed:
   keys that name a credential or a direct identifier are rejected outright,
   identifier-shaped values are pseudonymised (stable per salt, so correlation
   across rows still works) and all other strings are redacted. A guard re-checks
   the sanitised result and refuses the write if an identifier survived, so a leak
   fails the append instead of reaching the log.

The hash is taken over the *sanitised* body on purpose: verification rebuilds the
body from the stored columns, and re-serialising a parsed payload could change the
bytes and produce a false "chain broken".
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Iterator, Mapping, Optional

from primitives import (
    Clock,
    DomainError,
    IdFactory,
    Money,
    canonical_json,
    decimal_to_str,
    pseudonymize,
    redact,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
    to_iso_date,
)

__all__ = [
    "GENESIS_HASH",
    "CHAIN_DOMAIN",
    "PII_SALT",
    "AUDIT_TABLE",
    "APPEND_ONLY_TRIGGERS",
    "MAX_PAYLOAD_DEPTH",
    "COLUMNS",
    "CHAIN_FIELDS",
    "AuditEvent",
    "ChainAnchor",
    "ChainReport",
    "AuditLog",
    "AuditEventInvalid",
    "AuditChainBroken",
    "AuditSequenceGap",
    "AuditAnchorMismatch",
    "AuditPIIForbiddenField",
    "AuditPIILeak",
    "AuditAppendOnlyUnenforced",
    "compute_event_hash",
    "sanitize_payload",
    "serialize_state",
    "contains_direct_identifiers",
    "assert_pii_free",
    "verify_chain",
    "guard_append_only",
    "safe_projection",
    "to_log_line",
]

#: ``prev_hash`` of the first event: the chain starts from a known constant, so an
#: empty log and a log whose head was replaced with nothing are distinguishable.
GENESIS_HASH = "0" * 64

#: Domain separator — a hash from this chain must never be confusable with the
#: fingerprint of a policy snapshot or a quote.
CHAIN_DOMAIN = "villa-agency-os:audit:v1"

#: Separate from the primitives' default salt: pseudonyms in the audit trail must
#: not be joinable against pseudonyms produced elsewhere by accident.
PII_SALT = "villa-agency-os:audit"

AUDIT_TABLE = "audit_events"
APPEND_ONLY_TRIGGERS: tuple[str, ...] = (
    "trg_audit_events_no_update",
    "trg_audit_events_no_delete",
)

#: Guard against a self-referential payload turning sanitisation or hashing into a
#: stack overflow; a real booking snapshot is nowhere near this deep.
MAX_PAYLOAD_DEPTH = 12

#: Column order of ``audit_events`` — one tuple so the INSERT, the row mapping and
#: the chain field list can never drift apart.
COLUMNS: tuple[str, ...] = (
    "audit_event_id",
    "seq",
    "occurred_at",
    "actor",
    "actor_role",
    "action",
    "entity_type",
    "entity_id",
    "before_state",
    "after_state",
    "reason",
    "policy_version_id",
    "evidence_ref",
    "tool",
    "trace_id",
    "prev_hash",
    "hash",
)

#: Fields covered by the chain hash: every column except ``hash`` itself and
#: ``prev_hash`` (which is fed to the digest as its own part, not as body data).
CHAIN_FIELDS: tuple[str, ...] = COLUMNS[:-2]

_INSERT_SQL = (
    f"INSERT INTO {AUDIT_TABLE} ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(COLUMNS))})"
)


# --------------------------------------------------------------------------- #
# Errors — every deliberate failure carries a machine-readable code
# --------------------------------------------------------------------------- #
class AuditEventInvalid(DomainError):
    """The event is malformed (bad field, unserialisable or over-deep payload)."""

    code = "AUDIT_EVENT_INVALID"


class AuditChainBroken(DomainError):
    """A stored row's hash or its ``prev_hash`` link does not match."""

    code = "AUDIT_CHAIN_BROKEN"


class AuditSequenceGap(DomainError):
    """Sequence numbers are not contiguous from 1 — a row was removed or reordered."""

    code = "AUDIT_SEQUENCE_GAP"


class AuditAnchorMismatch(DomainError):
    """The chain does not match the previously published anchor (tail removed)."""

    code = "AUDIT_ANCHOR_MISMATCH"


class AuditPIIForbiddenField(DomainError):
    """A payload key names a credential or a direct identifier — never storable."""

    code = "AUDIT_PII_FORBIDDEN_FIELD"


class AuditPIILeak(DomainError):
    """A direct identifier survived sanitisation; the write is refused."""

    code = "AUDIT_PII_LEAK"


class AuditAppendOnlyUnenforced(DomainError):
    """The table or its immutability triggers are missing — refusing to write."""

    code = "AUDIT_APPEND_ONLY_UNENFORCED"


# --------------------------------------------------------------------------- #
# PII sanitisation
# --------------------------------------------------------------------------- #
#: A key matching this is refused outright with ``AUDIT_PII_FORBIDDEN_FIELD``.
#: These fields must live in their own access-controlled store behind a
#: ``*_ref``: no pseudonym of a card number is acceptable either.
_FORBIDDEN_KEY_RE = re.compile(
    r"(^|_)(card|card_number|card_no|pan|cc|cvv|cvc|cvn|security_code|"
    r"password|passwd|secret|api_key|apikey|access_token|refresh_token|token|"
    r"private_key|passport|passport_number|passport_no|nik|ktp|ktp_number|"
    r"bank_account|account_number|iban|swift|pin|otp)($|_)"
)

#: A key matching this has its scalar value replaced by a stable pseudonym: an
#: audit reader can still tell "same guest" from "different guest" without ever
#: seeing the identifier.
_PSEUDONYM_KEY_RE = re.compile(
    r"(^|_)(email|e_mail|guest_email|owner_email|phone|phone_number|mobile|"
    r"wa_number|whatsapp|guest_phone|guest_name|owner_name|full_name|"
    r"contact_name|id_number|address|full_address|dob|date_of_birth)($|_)"
)

#: Digest-shaped columns, excluded from the PII scan: a SHA-256 hex digest can
#: contain a nine-digit run by chance (the genesis hash is 64 zeros), and a digest
#: is never personally identifying. Scanning them would only create false alarms.
_DIGEST_FIELDS = ("prev_hash", "hash")


def _normalise_key(key: Any) -> str:
    """Lower-case, punctuation-collapsed form of a payload key.

    ``guest.email``, ``Guest-Email`` and ``guest_email`` must classify identically;
    a key that slips through classification is a silent PII leak.
    """
    if not isinstance(key, str):
        raise AuditEventInvalid(
            f"audit payload keys must be strings, got {type(key).__name__}",
            key=repr(key),
        )
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def contains_direct_identifiers(text: str) -> bool:
    """True when ``redact`` would change ``text``.

    Defined in terms of :func:`primitives.redact` rather than a private copy of its
    patterns: the guard can then never drift from the repo's definition of a direct
    identifier (email, Indonesian mobile, NIK, long digit runs).
    """
    return redact(text) != text


def sanitize_payload(
    value: Any,
    *,
    salt: str = PII_SALT,
    _depth: int = 0,
    _path: str = "",
) -> Any:
    """Return a PII-free copy of a before/after state, ready to be hashed.

    Ordering matters: forbidden keys are rejected before any value is inspected, so
    a credential can never be *stored* just because its value happened to look
    harmless. Non-scalar leaves that cannot be represented deterministically (set,
    float, opaque object) are rejected rather than guessed at — a snapshot that
    cannot be reproduced exactly has no business in an audit trail.
    """
    where = _path or "<root>"
    if isinstance(value, float):
        raise AuditEventInvalid(
            f"float is not allowed in an audit payload at {where}; "
            "use integer minor units or a decimal string",
            path=where,
            value=repr(value),
        )
    if isinstance(value, Money):
        return value.to_dict()
    if isinstance(value, Decimal):
        return decimal_to_str(value)
    if isinstance(value, datetime):
        return to_iso(value)  # rejects naive datetimes (NAIVE_TIMESTAMP)
    if isinstance(value, date):
        return to_iso_date(value)
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            return redact(value)
        return value
    if isinstance(value, Mapping):
        if _depth >= MAX_PAYLOAD_DEPTH:
            raise AuditEventInvalid(
                f"audit payload nested deeper than {MAX_PAYLOAD_DEPTH} levels at {where}",
                path=where,
                depth=_depth,
            )
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = _normalise_key(key)
            child = f"{_path}.{key}" if _path else str(key)
            if _FORBIDDEN_KEY_RE.search(name):
                raise AuditPIIForbiddenField(
                    f"{key!r} is forbidden in an audit payload at {child}; "
                    "store a reference in the owning table instead",
                    path=child,
                    field=key,
                )
            if item is None:
                out[key] = None
                continue
            if _PSEUDONYM_KEY_RE.search(name) and isinstance(item, (bool, int, str)):
                out[key] = pseudonymize(item, salt=salt)
                continue
            out[key] = sanitize_payload(
                item, salt=salt, _depth=_depth + 1, _path=child
            )
        return out
    if isinstance(value, (list, tuple)):
        if _depth >= MAX_PAYLOAD_DEPTH:
            raise AuditEventInvalid(
                f"audit payload nested deeper than {MAX_PAYLOAD_DEPTH} levels at {where}",
                path=where,
                depth=_depth,
            )
        return [
            sanitize_payload(item, salt=salt, _depth=_depth + 1, _path=f"{where}[{i}]")
            for i, item in enumerate(value)
        ]
    raise AuditEventInvalid(
        f"unsupported audit payload value at {where}: {type(value).__name__}",
        path=where,
        got=type(value).__name__,
    )


def assert_pii_free(
    value: Any, *, context: str = "audit payload", _path: str = ""
) -> Any:
    """Fail closed if a *string leaf* still contains a direct identifier.

    Only strings are examined: a nine-digit IDR amount is money, not PII, and
    scanning the serialised JSON would reject every large booking. Digest-shaped
    fields are excluded by their callers (:data:`_DIGEST_FIELDS`) because a hash can
    contain a long digit run by chance and a digest is never PII. The error's own
    message and details are redacted, so reporting a leak cannot leak.
    """
    if isinstance(value, str):
        if contains_direct_identifiers(value):
            raise AuditPIILeak(
                f"direct identifier survived sanitisation in {context}",
                context=context,
                path=_path or None,
                sample=redact(value)[:120],
            )
        return value
    if isinstance(value, Mapping):
        for key, item in value.items():
            assert_pii_free(item, context=context, _path=f"{_path}.{key}".lstrip("."))
        return value
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_pii_free(item, context=context, _path=f"{_path}[{index}]")
        return value
    return value


def serialize_state(
    value: Any, *, salt: str = PII_SALT, field_name: str = "state"
) -> Optional[str]:
    """Sanitise a state mapping and render it as canonical JSON text (or ``None``)."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AuditEventInvalid(
            f"{field_name} must be a mapping of field names to values, "
            f"got {type(value).__name__}",
            field=field_name,
            got=type(value).__name__,
        )
    if not value:
        # An empty snapshot is indistinguishable from "nothing happened"; keep it
        # as NULL so "unchanged" is one value, not two.
        return None
    sanitized = sanitize_payload(value, salt=salt)
    assert_pii_free(sanitized, context=field_name)
    return canonical_json(sanitized)


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def compute_event_hash(prev_hash: str, body: Mapping[str, Any]) -> str:
    """``SHA-256(domain ‖ prev_hash ‖ canonical_json(body))``.

    ``prev_hash`` is a separate digest part (``primitives.sha256_hex`` writes a
    unit separator after every part), so no body value can be shifted into the
    previous hash by concatenation.
    """
    if not isinstance(prev_hash, str) or len(prev_hash) != 64:
        raise AuditEventInvalid(
            "prev_hash must be a 64-character hex digest",
            prev_hash=repr(prev_hash),
        )
    return sha256_hex(CHAIN_DOMAIN, prev_hash, canonical_json(dict(body)))


@dataclass(frozen=True)
class AuditEvent:
    """One immutable audit row.

    ``before_state`` / ``after_state`` hold the canonical JSON *text* that was
    hashed, not parsed dicts: re-serialising a parsed payload can change the bytes
    and break verification for a legitimate row. :attr:`before` / :attr:`after`
    are read-only decoded views for callers.
    """

    audit_event_id: str
    seq: int
    occurred_at: str
    actor: str
    actor_role: str
    action: str
    entity_type: str
    entity_id: str
    prev_hash: str
    hash: str
    before_state: Optional[str] = None
    after_state: Optional[str] = None
    reason: Optional[str] = None
    policy_version_id: Optional[str] = None
    evidence_ref: Optional[str] = None
    tool: Optional[str] = None
    trace_id: Optional[str] = None

    # -- construction ------------------------------------------------------ #
    @classmethod
    def from_row(cls, row: Mapping[str, Any] | sqlite3.Row) -> "AuditEvent":
        """Build from a database row; a missing column is a schema drift, not a default."""
        data = dict(row)
        missing = [name for name in COLUMNS if name not in data]
        if missing:
            raise AuditEventInvalid(
                "audit_events row is missing expected columns",
                missing=missing,
            )
        return cls(**{name: data[name] for name in COLUMNS})

    # -- canonical body ---------------------------------------------------- #
    def body(self) -> dict[str, Any]:
        """The exact field set that is hashed (everything but hash/prev_hash)."""
        return {name: getattr(self, name) for name in CHAIN_FIELDS}

    def computed_hash(self) -> str:
        return compute_event_hash(self.prev_hash, self.body())

    def verify_hash(self) -> str:
        """Return the recomputed hash, or raise if this row was modified.

        Local by design: it proves nothing about the rows before it — use
        :func:`verify_chain` (or :meth:`AuditLog.verify`) for that.
        """
        recomputed = self.computed_hash()
        if recomputed != self.hash:
            raise AuditChainBroken(
                "audit event hash mismatch: the row was modified after it was written",
                seq=self.seq,
                expected=recomputed,
                stored=self.hash,
            )
        return recomputed

    # -- decoded views ----------------------------------------------------- #
    @property
    def before(self) -> Optional[dict[str, Any]]:
        return None if self.before_state is None else json.loads(self.before_state)

    @property
    def after(self) -> Optional[dict[str, Any]]:
        return None if self.after_state is None else json.loads(self.after_state)

    @property
    def is_genesis(self) -> bool:
        """True when this is the first link of a chain."""
        return self.prev_hash == GENESIS_HASH

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in COLUMNS}


@dataclass(frozen=True)
class ChainAnchor:
    """``(count, head_hash)`` published outside the database.

    The chain alone cannot distinguish "120 events" from "120 events whose last 9
    were deleted"; an anchor recorded in a second system can.
    """

    count: int
    head_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {"count": self.count, "head_hash": self.head_hash}


@dataclass(frozen=True)
class ChainReport:
    """Result of a successful verification."""

    count: int
    head_hash: str
    verified_against_anchor: bool = False


def verify_chain(
    events: Iterable[AuditEvent], *, anchor: Optional[ChainAnchor] = None
) -> ChainReport:
    """Walk an ordered event stream and raise on the first broken link.

    Three distinct failures, each with its own code, because the response differs:
    a gap means rows were removed or reordered, a hash/link mismatch means a row's
    content or its pointer was rewritten, and an anchor mismatch means history was
    truncated (or wholly recomputed) in a way the data itself cannot reveal.
    """
    expected_seq = 1
    prev_hash = GENESIS_HASH
    count = 0
    previous: Optional[AuditEvent] = None
    for event in events:
        if event.seq != expected_seq:
            raise AuditSequenceGap(
                "audit sequence is not contiguous: a row was removed or reordered",
                expected_seq=expected_seq,
                found_seq=event.seq,
                after_seq=None if previous is None else previous.seq,
            )
        if event.prev_hash != prev_hash:
            raise AuditChainBroken(
                "audit chain linkage broken: prev_hash does not match the previous event",
                seq=event.seq,
                expected_prev=prev_hash,
                stored_prev=event.prev_hash,
            )
        event.verify_hash()
        prev_hash = event.hash
        previous = event
        count += 1
        expected_seq += 1

    report = ChainReport(count=count, head_hash=prev_hash)
    if anchor is not None:
        if anchor.count > count:
            raise AuditAnchorMismatch(
                "audit chain is shorter than the published anchor: history was removed",
                observed_count=count,
                anchor_count=anchor.count,
                observed_head=prev_hash,
                anchor_head=anchor.head_hash,
            )
        if count == anchor.count and prev_hash != anchor.head_hash:
            raise AuditAnchorMismatch(
                "audit chain head differs from the published anchor at the same length: "
                "history was rewritten",
                observed_count=count,
                anchor_count=anchor.count,
                observed_head=prev_hash,
                anchor_head=anchor.head_hash,
            )
        report = ChainReport(
            count=count, head_hash=prev_hash, verified_against_anchor=True
        )
    return report


def guard_append_only(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Fail closed unless the table and both immutability triggers exist.

    Writing an audit trail into a database whose guards were dropped produces rows
    that *look* fine and are worthless; refusing to start is the cheaper failure.
    """
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if AUDIT_TABLE not in tables:
        raise AuditAppendOnlyUnenforced(
            f"{AUDIT_TABLE} does not exist: apply the schema before writing audit rows",
            missing=(f"table:{AUDIT_TABLE}",),
        )
    triggers = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    missing = [name for name in APPEND_ONLY_TRIGGERS if name not in triggers]
    if missing:
        raise AuditAppendOnlyUnenforced(
            "append-only triggers are missing; the audit trail cannot be trusted",
            missing=tuple(missing),
        )
    return APPEND_ONLY_TRIGGERS


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the body in an IMMEDIATE transaction, or join the caller's.

    IMMEDIATE takes the write lock before the tail is read, so two writers cannot
    both read the same head and mint the same ``seq``. When the caller already
    opened a transaction we must not nest a BEGIN (SQLite would error) and must not
    commit their work either — their rollback is what makes our INSERT disappear.
    """
    if conn.in_transaction:
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
# Writer / reader / verifier
# --------------------------------------------------------------------------- #
class AuditLog:
    """Append-only writer and verifier for the ``audit_events`` chain.

    Deliberately dumb about *who* may append: authorisation lives in the domain
    layer. What this class guarantees is that a row, once written, is chained to its
    predecessor before the transaction commits.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Clock,
        ids: IdFactory,
        pii_salt: str = PII_SALT,
        verify_on_append: bool = True,
        enforce_triggers: bool = True,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._ids = ids
        self._salt = require_text(pii_salt, "pii_salt")
        self._verify_on_append = bool(verify_on_append)
        if enforce_triggers:
            guard_append_only(conn)

    # -- append ------------------------------------------------------------ #
    def append(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        actor: str,
        actor_role: str,
        before: Optional[Mapping[str, Any]] = None,
        after: Optional[Mapping[str, Any]] = None,
        reason: Optional[str] = None,
        policy_version_id: Optional[str] = None,
        evidence_ref: Optional[str] = None,
        tool: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> AuditEvent:
        """Append one event and return the stored (frozen) row.

        All keyword-only and ``actor``/``actor_role`` mandatory: an audit row that
        cannot name who did the thing cannot be used in an incident review, and a
        defaulted actor would hide exactly that defect.
        """
        action = require_ref(action, "action")
        entity_type = require_ref(entity_type, "entity_type")
        entity_id = require_ref(entity_id, "entity_id")
        actor = require_ref(actor, "actor")
        actor_role = require_ref(actor_role, "actor_role")
        policy_version_id = _optional_ref(policy_version_id, "policy_version_id")
        evidence_ref = _optional_ref(evidence_ref, "evidence_ref")
        tool = _optional_ref(tool, "tool")
        trace_id = _optional_ref(trace_id, "trace_id")

        before_state = serialize_state(
            before, salt=self._salt, field_name="before_state"
        )
        after_state = serialize_state(after, salt=self._salt, field_name="after_state")
        reason_text = self._clean_reason(reason)
        occurred_at = to_iso(self._clock.now())
        audit_event_id = self._ids.new("audit")

        body: dict[str, Any] = {
            "audit_event_id": audit_event_id,
            "seq": 0,
            "occurred_at": occurred_at,
            "actor": actor,
            "actor_role": actor_role,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "before_state": before_state,
            "after_state": after_state,
            "reason": reason_text,
            "policy_version_id": policy_version_id,
            "evidence_ref": evidence_ref,
            "tool": tool,
            "trace_id": trace_id,
        }

        with _write_transaction(self._conn):
            tail = self._tail_event()
            if tail is None:
                seq, prev_hash = 1, GENESIS_HASH
            else:
                if self._verify_on_append:
                    # Cheap fail-closed check: never extend a chain whose head no
                    # longer hashes to what it claims.
                    tail.verify_hash()
                seq, prev_hash = tail.seq + 1, tail.hash
            body["seq"] = seq
            event = AuditEvent(
                prev_hash=prev_hash,
                hash=compute_event_hash(prev_hash, body),
                **body,
            )
            self._conn.execute(_INSERT_SQL, tuple(getattr(event, c) for c in COLUMNS))
        return event

    def _clean_reason(self, reason: Optional[str]) -> Optional[str]:
        """Redact free text; a blank reason is stored as NULL, not as ``''``."""
        if reason is None:
            return None
        text = redact(
            require_text(reason, "reason", max_len=2000, allow_empty=True)
        )
        if not text:
            return None
        assert_pii_free(text, context="audit reason")
        return text

    # -- read -------------------------------------------------------------- #
    def count(self) -> int:
        return int(self._conn.execute(f"SELECT COUNT(*) FROM {AUDIT_TABLE}").fetchone()[0])

    def head(self) -> Optional[AuditEvent]:
        """The newest event, or ``None`` for an empty log."""
        return self._tail_event()

    def _tail_event(self) -> Optional[AuditEvent]:
        row = self._conn.execute(
            f"SELECT * FROM {AUDIT_TABLE} ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return None if row is None else AuditEvent.from_row(row)

    def iter_events(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        since_seq: int = 0,
    ) -> Iterator[AuditEvent]:
        """Stream events in ``seq`` order (the chain order), optionally filtered.

        ``seq`` order and not ``occurred_at``: a clock can move backwards, the chain
        cannot.
        """
        if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
            raise AuditEventInvalid(
                "since_seq must be a non-negative integer",
                since_seq=repr(since_seq),
            )
        sql = f"SELECT * FROM {AUDIT_TABLE} WHERE seq > ?"
        params: list[Any] = [since_seq]
        if entity_type is not None:
            sql += " AND entity_type = ?"
            params.append(require_ref(entity_type, "entity_type"))
        if entity_id is not None:
            sql += " AND entity_id = ?"
            params.append(require_ref(entity_id, "entity_id"))
        sql += " ORDER BY seq ASC"
        for row in self._conn.execute(sql, params):
            yield AuditEvent.from_row(row)

    def events(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        since_seq: int = 0,
    ) -> list[AuditEvent]:
        return list(
            self.iter_events(
                entity_type=entity_type, entity_id=entity_id, since_seq=since_seq
            )
        )

    def for_entity(self, entity_type: str, entity_id: str) -> list[AuditEvent]:
        """Every event recorded against one entity, oldest first."""
        return self.events(entity_type=entity_type, entity_id=entity_id)

    # -- integrity --------------------------------------------------------- #
    def anchor(self) -> ChainAnchor:
        """Publishable ``(count, head_hash)`` — store it outside this database."""
        tail = self._tail_event()
        return ChainAnchor(
            count=self.count(),
            head_hash=GENESIS_HASH if tail is None else tail.hash,
        )

    def verify(self, *, anchor: Optional[ChainAnchor] = None) -> ChainReport:
        """Recompute the whole chain; raise on the first broken link."""
        return verify_chain(self.events(), anchor=anchor)


def _optional_ref(value: Optional[str], field_name: str) -> Optional[str]:
    """Validate an optional reference; ``None``/omitted means "not supplied".

    An empty string is *not* silently accepted as absent: ``require_ref`` rejects
    it, because a blank reference is almost always a caller bug that would leave the
    audit trail unable to point at its evidence.
    """
    if value is None:
        return None
    return require_ref(value, field_name)


# --------------------------------------------------------------------------- #
# Safe views: logs, exports and model prompts
# --------------------------------------------------------------------------- #
def safe_projection(event: AuditEvent) -> dict[str, Any]:
    """A PII-free view of one event for a log line, an export or a model prompt.

    Guarded instead of trusted: if a row (written by an older version or a
    hand-edited database) still carries an identifier, this raises rather than
    returning it to the caller.
    """
    out: dict[str, Any] = {
        "audit_event_id": event.audit_event_id,
        "seq": event.seq,
        "occurred_at": event.occurred_at,
        "actor": event.actor,
        "actor_role": event.actor_role,
        "action": event.action,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "policy_version_id": event.policy_version_id,
        "evidence_ref": event.evidence_ref,
        "tool": event.tool,
        "trace_id": event.trace_id,
        "reason": event.reason,
        "before": event.before,
        "after": event.after,
        "prev_hash": event.prev_hash,
        "hash": event.hash,
    }
    assert_pii_free(
        {k: v for k, v in out.items() if k not in _DIGEST_FIELDS},
        context="audit projection",
    )
    return out


def to_log_line(event: AuditEvent) -> str:
    """One-line, PII-free summary — the only form that should reach stderr.

    The line is assembled from fields that were validated (and, for content,
    sanitised) at write time, and is guarded before it is returned: a value that
    somehow carries an identifier raises instead of being printed.
    """
    assert_pii_free(
        {
            "actor": event.actor,
            "actor_role": event.actor_role,
            "action": event.action,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
        },
        context="audit log line",
    )
    return (
        f"[audit] seq={event.seq} action={event.action} "
        f"entity={event.entity_type}/{event.entity_id} actor={event.actor} "
        f"role={event.actor_role} at={event.occurred_at} hash={event.hash[:12]}"
    )
