"""The reservation kernel: holds, bookings and the no-double-booking guarantee.

This module is the only place that writes inventory state. It exists because the
North Star for this division is *zero double bookings*, and that cannot be
enforced by careful callers — it has to be enforced by the storage layer.

How double booking is prevented
-------------------------------
1. Every write that can occupy a slot runs inside ``BEGIN IMMEDIATE``, which
   takes SQLite's write lock up front. Two concurrent requests therefore
   serialise instead of racing between a check and an insert.
2. Inside that transaction the kernel runs an overlap query over the *range*
   ``check_in <= candidate < check_out`` against active holds and occupying
   bookings. Range overlap is the real question; a unique index on
   ``(unit_id, check_in)`` alone cannot answer it.
3. The partial unique indexes (``uq_holds_active_slot``, ``uq_bookings_active_slot``)
   remain as a second line of defence for the exact-same-slot case, so even a
   kernel bug cannot produce two identical active slots.
4. In PostgreSQL the equivalent range guarantee is an ``EXCLUDE USING gist``
   constraint with ``daterange``; see ``002_pg_range_exclusions.pg.sql``.

What a hold needs
-----------------
A hold can only exist against *evidence*, never against an assumption: the row
must cite an ``availability_evidence`` record whose ``result`` is ``available``
and whose ``can_hold`` flag is set. Missing, stale or contradicting evidence
fails closed.

Every state change is journalled (``booking_state_events``) and audited
(``audit_events``, hash-chained) in the same transaction as the change itself, so
there is no window where the state moved but the history did not.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from primitives import (
    UTC,
    DomainError,
    IdFactory,
    Money,
    SystemClock,
    canonical_json,
    nights_between,
    parse_iso,
    parse_iso_date,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)
from schema_loader import applied_version, connect_sqlite

__all__ = [
    "ReservationKernel",
    "HoldOutcome",
    "BookingOutcome",
    "HOLD_ACTIVE",
    "HOLD_RELEASED",
    "HOLD_CONVERTED",
    "HOLD_EXPIRED",
    "BOOKING_STATES",
    "OCCUPYING_BOOKING_STATES",
    "DEFAULT_ROLE",
]

HOLD_ACTIVE = "ACTIVE"
HOLD_RELEASED = "RELEASED"
HOLD_CONVERTED = "CONVERTED"
HOLD_EXPIRED = "EXPIRED"

#: Statuses that make a booking occupy its slot. Must match the partial unique
#: index ``uq_bookings_active_slot`` in the schema — the two are a pair.
OCCUPYING_BOOKING_STATES: tuple[str, ...] = (
    "CONFIRMED",
    "BALANCE_DUE",
    "BALANCE_OVERDUE",
    "PRE_ARRIVAL",
    "IN_STAY",
)

#: Every legal booking status, mirroring the CHECK constraint verbatim
#: (``tests/test_reservation_kernel.py`` asserts this set against the schema).
BOOKING_STATES: tuple[str, ...] = (
    "INQUIRY",
    "QUALIFIED",
    "AVAILABILITY_PENDING",
    "AVAILABILITY_CONFIRMED",
    "AVAILABILITY_UNAVAILABLE",
    "QUOTE_DRAFTED",
    "QUOTE_SENT",
    "QUOTE_EXPIRED",
    "HOLD_REQUESTED",
    "HOLD_ACTIVE",
    "HOLD_EXPIRED",
    "PAYMENT_PENDING",
    "PAYMENT_PARTIAL",
    "PAYMENT_UNMATCHED",
    "PAYMENT_FAILED",
    "DEPOSIT_PAID",
    "PAID_IN_FULL",
    "CONFIRMED",
    "BALANCE_DUE",
    "BALANCE_OVERDUE",
    "PRE_ARRIVAL",
    "IN_STAY",
    "COMPLETED",
    "CANCELLATION_REQUESTED",
    "CANCELLATION_REVIEW",
    "CANCELLED",
    "REFUND_PENDING",
    "REFUNDED",
    "NO_SHOW",
    "RELOCATED",
    "DISPUTED",
    "CLOSED",
)

#: Evidence results and levels, exactly as the CHECK constraints spell them.
EVIDENCE_RESULTS: tuple[str, ...] = ("AVAILABLE", "OCCUPIED", "UNKNOWN")
EVIDENCE_LEVELS: tuple[str, ...] = ("A", "B", "C", "D")
EVIDENCE_SOURCES: tuple[str, ...] = (
    "property_api",
    "booking_engine",
    "manager_written",
    "ical",
    "memory",
    "manual_ops",
)

#: How long a piece of evidence stays usable when the caller does not say.
DEFAULT_EVIDENCE_TTL_HOURS = 24

DEFAULT_ROLE = "kernel"

#: The chain's starting value. 64 zeros rather than a word, because the schema
#: requires every ``prev_hash`` to look like a SHA-256 hex digest.
_GENESIS_HASH = "0" * 64


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HoldOutcome:
    """Result of a hold request — ``replayed`` means idempotency returned the old row."""

    hold_id: str
    unit_id: str
    status: str
    check_in: str
    check_out: str
    nights: int
    evidence_id: str
    expires_at: str
    replayed: bool = False


@dataclass(frozen=True)
class BookingOutcome:
    """Result of a confirmation."""

    booking_id: str
    unit_id: str
    status: str
    check_in: str
    check_out: str
    nights: int
    currency: str
    total_minor: int
    deposit_due_minor: int
    balance_due_minor: int
    replayed: bool = False


# --------------------------------------------------------------------------- #
# The kernel
# --------------------------------------------------------------------------- #
class ReservationKernel:
    """Transactional write path for inventory state.

    One instance owns one connection. Do not share an instance across threads or
    processes: SQLite serialises writers, and the kernel's overlap guarantee
    depends on ``BEGIN IMMEDIATE`` actually holding the write lock.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        actor: str = "system:kernel",
        actor_role: str = DEFAULT_ROLE,
        transition_gate: Optional[Callable[[str, str, str], bool]] = None,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.actor = actor
        self.actor_role = actor_role
        self._transition_gate = transition_gate or _default_transition_gate
        self._in_tx = False

    # -- construction ------------------------------------------------------- #
    @classmethod
    def open(
        cls,
        db_path: str | Path,
        *,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        actor: str = "system:kernel",
        actor_role: str = DEFAULT_ROLE,
        transition_gate: Optional[Callable[[str, str, str], bool]] = None,
    ) -> "ReservationKernel":
        """Open a SQLite-backed kernel with the schema applied.

        ``schema_loader.connect_sqlite`` owns schema application (and enforces
        ``foreign_keys``/WAL/busy-timeout), so there is exactly one place that
        knows how to prepare a database.
        """
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = connect_sqlite(str(db_path))
        return cls(
            conn,
            clock=clock,
            ids=ids,
            actor=actor,
            actor_role=actor_role,
            transition_gate=transition_gate,
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ReservationKernel":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def schema_version(self) -> Optional[str]:
        """The applied schema version (``None`` when the database is uninitialised)."""
        return applied_version(self.conn)

    # -- transactions ------------------------------------------------------- #
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes the lock before it reads.

        Nesting is refused: a silent nested ``BEGIN`` would either be a no-op
        (and lose the guarantee) or raise mid-write.
        """
        if self._in_tx:
            raise DomainError(
                "nested transactions are not supported by the kernel",
                code="KERNEL_NESTED_TRANSACTION",
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

    # -- inventory fixtures (ops path) -------------------------------------- #
    def upsert_property(
        self,
        *,
        property_id: str,
        name: str,
        property_type: str = "villa",
        region: str = "canggu",
        province: str = "Bali",
        timezone: str = "Asia/Makassar",
        status: str = "SELLABLE",
        contract_pack_status: str = "VERIFIED",
    ) -> str:
        """Insert or refresh a property row (contract pack status is explicit)."""
        property_id = require_ref(property_id, "property_id")
        now = self._now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO properties (
                    property_id, name, property_type, region, province, timezone,
                    status, contract_pack_status, contract_pack_evaluated_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(property_id) DO UPDATE SET
                    name = excluded.name,
                    property_type = excluded.property_type,
                    region = excluded.region,
                    province = excluded.province,
                    timezone = excluded.timezone,
                    status = excluded.status,
                    contract_pack_status = excluded.contract_pack_status,
                    contract_pack_evaluated_at = excluded.contract_pack_evaluated_at,
                    updated_at = excluded.updated_at
                """,
                (
                    property_id,
                    require_text(name, "name", max_len=120),
                    property_type,
                    region,
                    province,
                    timezone,
                    status,
                    contract_pack_status,
                    now,
                    now,
                    now,
                ),
            )
        return property_id

    def upsert_unit(
        self,
        *,
        unit_id: str,
        property_id: str,
        name: str,
        unit_type: str = "entire_villa",
        bedrooms: int = 1,
        bathrooms: int = 1,
        base_capacity: int = 2,
        max_guests: int = 4,
        status: str = "ACTIVE",
    ) -> str:
        unit_id = require_ref(unit_id, "unit_id")
        if max_guests < base_capacity:
            raise DomainError(
                "max_guests must be >= base_capacity",
                code="CAPACITY_INVALID",
                base_capacity=base_capacity,
                max_guests=max_guests,
            )
        now = self._now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO units (
                    unit_id, property_id, name, unit_type, bedrooms, bathrooms,
                    base_capacity, max_guests, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unit_id) DO UPDATE SET
                    property_id = excluded.property_id,
                    name = excluded.name,
                    unit_type = excluded.unit_type,
                    bedrooms = excluded.bedrooms,
                    bathrooms = excluded.bathrooms,
                    base_capacity = excluded.base_capacity,
                    max_guests = excluded.max_guests,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    unit_id,
                    property_id,
                    require_text(name, "name", max_len=120),
                    unit_type,
                    bedrooms,
                    bathrooms,
                    base_capacity,
                    max_guests,
                    status,
                    now,
                    now,
                ),
            )
        return unit_id

    def insert_policy_version(self, policy: Mapping[str, Any]) -> str:
        """Store a :class:`policy_engine.PolicyVersion` row (``as_row()`` output)."""
        with self.transaction() as conn:
            columns = sorted(policy)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO policy_versions ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(policy[column] for column in columns),
            )
        return str(policy["policy_version_id"])

    def insert_rate_card(self, card: Mapping[str, Any], seasons: Sequence[Mapping[str, Any]] = ()) -> str:
        with self.transaction() as conn:
            columns = sorted(card)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO rate_cards ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(card[column] for column in columns),
            )
            for season in seasons:
                season_columns = sorted(season)
                season_placeholders = ", ".join("?" for _ in season_columns)
                conn.execute(
                    f"INSERT INTO rate_seasons ({', '.join(season_columns)}) VALUES ({season_placeholders})",
                    tuple(season[column] for column in season_columns),
                )
        return str(card["rate_card_id"])

    def persist_quote(self, quote: Any) -> str:
        """Persist a :class:`quote_calculator.Quote` (``as_row()`` output)."""
        row = quote.as_row()
        with self.transaction() as conn:
            columns = sorted(row)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO quotes ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
        return str(row["quote_id"])

    # -- evidence ----------------------------------------------------------- #
    def record_evidence(
        self,
        *,
        unit_id: str,
        source_type: str,
        evidence_level: str,
        result: str,
        captured_at: Optional[str] = None,
        valid_until: Optional[str] = None,
        ttl_hours: int = DEFAULT_EVIDENCE_TTL_HOURS,
        occupancy_ranges: Sequence[Mapping[str, str]] = (),
        can_quote: bool = True,
        can_hold: bool = True,
        can_confirm: bool = False,
        source_ref: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> str:
        """Record availability evidence — the only thing that can authorise a hold.

        ``evidence_level`` A/B are the levels that may authorise a confirmation
        (``ck_evidence_confirm_requires_ab``); the kernel refuses to record a
        ``can_confirm`` claim at level C/D rather than letting the database
        reject it later.

        ``valid_until`` is mandatory in the schema, so it is always derived: either
        the caller states it, or it is ``captured_at + ttl_hours``. Evidence that
        never expires would silently authorise a hold forever.
        """
        require_ref(unit_id, "unit_id")
        level = str(evidence_level).strip().upper()
        if level not in EVIDENCE_LEVELS:
            raise DomainError(
                f"evidence_level must be one of {list(EVIDENCE_LEVELS)}",
                code="EVIDENCE_INVALID_LEVEL",
                evidence_level=evidence_level,
            )
        if source_type not in EVIDENCE_SOURCES:
            raise DomainError(
                f"source_type must be one of {list(EVIDENCE_SOURCES)}",
                code="EVIDENCE_INVALID_SOURCE",
                source_type=source_type,
            )
        normalised_result = str(result).strip().upper()
        if normalised_result not in EVIDENCE_RESULTS:
            raise DomainError(
                f"evidence result must be one of {list(EVIDENCE_RESULTS)}",
                code="EVIDENCE_INVALID_RESULT",
                result=result,
            )
        captured = captured_at or self._now()
        expires = valid_until or to_iso(parse_iso(captured) + timedelta(hours=ttl_hours))
        if expires <= captured:
            # Mirrors ck_evidence_window: evidence must describe a forward window.
            raise DomainError(
                "valid_until must be after captured_at",
                code="EVIDENCE_INVALID_WINDOW",
                captured_at=captured,
                valid_until=expires,
            )
        if can_confirm and level not in ("A", "B"):
            raise DomainError(
                "only level A or B evidence may authorise a confirmation",
                code="EVIDENCE_LEVEL_INSUFFICIENT",
                evidence_level=level,
            )
        reference = source_ref or f"{source_type}:{unit_id}"
        evidence_id = self.ids.new("evid")
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO availability_evidence (
                    evidence_id, unit_id, evidence_level, source_type, source_ref,
                    captured_at, valid_until, result, occupancy_ranges,
                    can_quote, can_hold, can_confirm, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    unit_id,
                    level,
                    source_type,
                    require_text(reference, "source_ref", max_len=200),
                    captured,
                    expires,
                    normalised_result,
                    canonical_json([dict(r) for r in occupancy_ranges]),
                    1 if can_quote else 0,
                    1 if can_hold else 0,
                    1 if can_confirm else 0,
                    notes,
                    self._now(),
                ),
            )
        return evidence_id

    def get_evidence(self, evidence_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM availability_evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise DomainError(
                f"unknown availability evidence: {evidence_id}",
                code="EVIDENCE_NOT_FOUND",
                evidence_id=evidence_id,
            )
        return row

    # -- holds -------------------------------------------------------------- #
    def request_hold(
        self,
        *,
        unit_id: str,
        check_in: str,
        check_out: str,
        guest_ref: str,
        policy_version_id: str,
        idempotency_key: str,
        quote_ref: Optional[str] = None,
        evidence_id: Optional[str] = None,
        ttl_minutes: Optional[int] = None,
    ) -> HoldOutcome:
        """Lock inventory for a short window, or fail with a reason.

        The order matters: check idempotency, then evidence, then overlap, all
        inside one write transaction so nothing can change underneath.
        """
        unit_id = require_ref(unit_id, "unit_id")
        guest_ref = require_ref(guest_ref, "guest_ref")
        policy_version_id = require_ref(policy_version_id, "policy_version_id")
        idempotency_key = require_ref(idempotency_key, "idempotency_key")
        _require_min_length(idempotency_key, "idempotency_key", 8)
        nights = nights_between(parse_iso_date(check_in), parse_iso_date(check_out))

        request_hash = sha256_hex("hold", unit_id, check_in, check_out, guest_ref, policy_version_id)
        now = self._now()

        with self.transaction() as conn:
            replay = self._idempotent_lookup(conn, "request_hold", idempotency_key, request_hash)
            if replay is not None:
                row = conn.execute(
                    "SELECT * FROM holds WHERE hold_id = ?", (replay,)
                ).fetchone()
                if row is None:  # pragma: no cover - defensive
                    raise DomainError(
                        "idempotency ledger points at a missing hold",
                        code="IDEMPOTENCY_DANGLING",
                        hold_id=replay,
                    )
                return HoldOutcome(
                    hold_id=row["hold_id"],
                    unit_id=row["unit_id"],
                    status=row["status"],
                    check_in=row["check_in"],
                    check_out=row["check_out"],
                    nights=row["nights"],
                    evidence_id=row["evidence_id"],
                    expires_at=row["expires_at"],
                    replayed=True,
                )

            unit = conn.execute("SELECT * FROM units WHERE unit_id = ?", (unit_id,)).fetchone()
            if unit is None:
                raise DomainError(f"unknown unit: {unit_id}", code="UNIT_NOT_FOUND", unit_id=unit_id)
            property_id = unit["property_id"]
            policy = conn.execute(
                "SELECT * FROM policy_versions WHERE policy_version_id = ?",
                (policy_version_id,),
            ).fetchone()
            if policy is None:
                raise DomainError(
                    f"unknown policy version: {policy_version_id}",
                    code="POLICY_NOT_FOUND",
                    policy_version_id=policy_version_id,
                )
            if policy["property_id"] != property_id:
                raise DomainError(
                    "policy version belongs to a different property",
                    code="POLICY_PROPERTY_MISMATCH",
                    policy_property=policy["property_id"],
                    unit_property=property_id,
                )

            if evidence_id is None:
                evidence_id = self._record_evidence_within(
                    conn,
                    unit_id=unit_id,
                    source_type="manual_ops",
                    evidence_level="B",
                    result="AVAILABLE",
                    can_quote=True,
                    can_hold=True,
                    can_confirm=False,
                    notes="auto-recorded by request_hold",
                )
            evidence = conn.execute(
                "SELECT * FROM availability_evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
            if evidence is None:
                raise DomainError(
                    f"unknown availability evidence: {evidence_id}",
                    code="EVIDENCE_NOT_FOUND",
                    evidence_id=evidence_id,
                )
            if evidence["unit_id"] != unit_id:
                raise DomainError(
                    "evidence belongs to a different unit",
                    code="EVIDENCE_UNIT_MISMATCH",
                    evidence_unit=evidence["unit_id"],
                    unit_id=unit_id,
                )
            if evidence["result"] != "AVAILABLE":
                raise DomainError(
                    "evidence does not say the unit is available",
                    code="EVIDENCE_NOT_AVAILABLE",
                    result=evidence["result"],
                )
            if not evidence["can_hold"]:
                raise DomainError(
                    "evidence does not authorise a hold",
                    code="EVIDENCE_DOES_NOT_AUTHORISE_HOLD",
                    evidence_id=evidence_id,
                    evidence_level=evidence["evidence_level"],
                )
            if evidence["valid_until"] is not None and evidence["valid_until"] < now:
                raise DomainError(
                    "evidence expired before the hold was requested",
                    code="EVIDENCE_STALE",
                    valid_until=evidence["valid_until"],
                    now=now,
                )

            self._assert_no_overlap(conn, unit_id=unit_id, check_in=check_in, check_out=check_out)

            if quote_ref is not None:
                quote = conn.execute(
                    "SELECT * FROM quotes WHERE quote_id = ?", (quote_ref,)
                ).fetchone()
                if quote is None:
                    raise DomainError(
                        f"unknown quote: {quote_ref}", code="QUOTE_NOT_FOUND", quote_id=quote_ref
                    )
                if quote["unit_id"] != unit_id:
                    raise DomainError(
                        "quote belongs to a different unit",
                        code="QUOTE_UNIT_MISMATCH",
                        quote_unit=quote["unit_id"],
                        unit_id=unit_id,
                    )
                if quote["policy_version_id"] != policy_version_id:
                    raise DomainError(
                        "quote was priced with a different policy version",
                        code="QUOTE_POLICY_MISMATCH",
                        quote_policy=quote["policy_version_id"],
                        policy_version_id=policy_version_id,
                    )

            ttl = ttl_minutes if ttl_minutes is not None else int(policy["hold_ttl_minutes"])
            if ttl <= 0:
                raise DomainError("ttl_minutes must be positive", code="HOLD_INVALID_TTL")
            hold_id = self.ids.new("hold")
            expires_at = to_iso(parse_iso(now) + timedelta(minutes=ttl))
            conn.execute(
                """
                INSERT INTO holds (
                    hold_id, unit_id, property_id, check_in, check_out, nights, status,
                    guest_ref, quote_ref, policy_version_id, evidence_id, idempotency_key,
                    created_at, expires_at, closed_at, close_reason, extended_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)
                """,
                (
                    hold_id, unit_id, property_id, check_in, check_out, nights, HOLD_ACTIVE,
                    guest_ref, quote_ref, policy_version_id, evidence_id, idempotency_key,
                    now, expires_at,
                ),
            )
            self._record_idempotency(conn, "request_hold", idempotency_key, request_hash, hold_id)
            self._audit(
                conn,
                action="hold.requested",
                entity_type="hold",
                entity_id=hold_id,
                after={"status": HOLD_ACTIVE, "unit_id": unit_id, "expires_at": expires_at},
                reason=None,
                policy_version_id=policy_version_id,
                evidence_ref=evidence_id,
            )
            return HoldOutcome(
                hold_id=hold_id,
                unit_id=unit_id,
                status=HOLD_ACTIVE,
                check_in=check_in,
                check_out=check_out,
                nights=nights,
                evidence_id=evidence_id,
                expires_at=expires_at,
            )

    def release_hold(self, hold_id: str, *, reason: str, actor: Optional[str] = None) -> str:
        """Release an active hold so the slot can be sold again."""
        return self._close_hold(hold_id, status=HOLD_RELEASED, reason=reason, actor=actor)

    def expire_hold(self, hold_id: str, *, actor: Optional[str] = None) -> str:
        """Mark a hold expired (normally called by :meth:`sweep_expired_holds`)."""
        return self._close_hold(hold_id, status=HOLD_EXPIRED, reason="ttl_expired", actor=actor)

    def extend_hold(self, hold_id: str, *, minutes: Optional[int] = None, actor: Optional[str] = None) -> str:
        """Extend an active hold, bounded by the policy's extension limit."""
        hold_id = require_ref(hold_id, "hold_id")
        now = self._now()
        with self.transaction() as conn:
            hold = self._require_hold(conn, hold_id)
            if hold["status"] != HOLD_ACTIVE:
                raise DomainError(
                    "only an active hold can be extended",
                    code="HOLD_NOT_ACTIVE",
                    hold_id=hold_id,
                    status=hold["status"],
                )
            if hold["expires_at"] <= now:
                raise DomainError(
                    "hold already expired; release and re-request instead",
                    code="HOLD_ALREADY_EXPIRED",
                    hold_id=hold_id,
                    expires_at=hold["expires_at"],
                )
            policy = conn.execute(
                "SELECT * FROM policy_versions WHERE policy_version_id = ?",
                (hold["policy_version_id"],),
            ).fetchone()
            if policy is None:  # pragma: no cover - FK guarantees this
                raise DomainError("hold references an unknown policy", code="POLICY_NOT_FOUND")
            limit = int(policy["max_hold_extensions"])
            if hold["extended_count"] >= limit:
                raise DomainError(
                    "hold extension limit reached",
                    code="HOLD_EXTENSION_LIMIT",
                    hold_id=hold_id,
                    max_hold_extensions=limit,
                )
            step = minutes if minutes is not None else int(policy["hold_ttl_minutes"])
            if step <= 0:
                raise DomainError("minutes must be positive", code="HOLD_INVALID_TTL")
            new_expiry = to_iso(parse_iso(hold["expires_at"]) + timedelta(minutes=step))
            conn.execute(
                "UPDATE holds SET expires_at = ?, extended_count = extended_count + 1 WHERE hold_id = ?",
                (new_expiry, hold_id),
            )
            self._audit(
                conn,
                action="hold.extended",
                entity_type="hold",
                entity_id=hold_id,
                before={"expires_at": hold["expires_at"]},
                after={"expires_at": new_expiry, "extended_count": hold["extended_count"] + 1},
                reason=None,
                policy_version_id=hold["policy_version_id"],
                evidence_ref=hold["evidence_id"],
                actor=actor,
            )
        return new_expiry

    def sweep_expired_holds(self, *, now: Optional[str] = None) -> tuple[str, ...]:
        """Expire every hold whose TTL has passed; returns the affected ids."""
        moment = now or self._now()
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT hold_id FROM holds WHERE status = ? AND expires_at <= ? ORDER BY hold_id",
                (HOLD_ACTIVE, moment),
            ).fetchall()
            for row in rows:
                hold = self._require_hold(conn, row["hold_id"])
                conn.execute(
                    """
                    UPDATE holds SET status = ?, closed_at = ?, close_reason = ?
                    WHERE hold_id = ?
                    """,
                    (HOLD_EXPIRED, moment, "ttl_expired", row["hold_id"]),
                )
                self._audit(
                    conn,
                    action="hold.expired",
                    entity_type="hold",
                    entity_id=row["hold_id"],
                    before={"status": HOLD_ACTIVE},
                    after={"status": HOLD_EXPIRED, "closed_at": moment},
                    reason="ttl_expired",
                    policy_version_id=hold["policy_version_id"],
                    evidence_ref=hold["evidence_id"],
                )
        return tuple(row["hold_id"] for row in rows)

    # -- bookings ----------------------------------------------------------- #
    def confirm_from_hold(
        self,
        *,
        hold_id: str,
        quote_id: str,
        idempotency_key: str,
        command_id: str,
        adults: int,
        children: int = 0,
        infants: int = 0,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
        amount_paid_minor: int = 0,
    ) -> BookingOutcome:
        """Convert an active hold into a confirmed booking.

        The booking's money comes from the *quote*, never from the request: the
        guest sees a price, the system stores that price.
        """
        hold_id = require_ref(hold_id, "hold_id")
        quote_id = require_ref(quote_id, "quote_id")
        idempotency_key = require_ref(idempotency_key, "idempotency_key")
        _require_min_length(idempotency_key, "idempotency_key", 8)
        command_id = require_ref(command_id, "command_id")
        _require_min_length(command_id, "command_id", 8)
        for name, value in (("adults", adults), ("children", children), ("infants", infants)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DomainError(f"{name} must be a non-negative integer", code="GUESTS_INVALID")
        if adults < 1:
            raise DomainError("at least one adult is required", code="GUESTS_INVALID")

        request_hash = sha256_hex("confirm_from_hold", hold_id, quote_id, command_id)
        now = self._now()

        with self.transaction() as conn:
            replay = self._idempotent_lookup(conn, "confirm_from_hold", idempotency_key, request_hash)
            if replay is not None:
                row = conn.execute("SELECT * FROM bookings WHERE booking_id = ?", (replay,)).fetchone()
                if row is None:  # pragma: no cover - defensive
                    raise DomainError(
                        "idempotency ledger points at a missing booking",
                        code="IDEMPOTENCY_DANGLING",
                        booking_id=replay,
                    )
                return self._booking_outcome(conn, row, replayed=True)

            hold = self._require_hold(conn, hold_id)
            if hold["status"] == HOLD_CONVERTED:
                existing = conn.execute(
                    "SELECT * FROM bookings WHERE hold_ref = ? ORDER BY created_at LIMIT 1",
                    (hold_id,),
                ).fetchone()
                if existing is not None:
                    return self._booking_outcome(conn, existing, replayed=True)
            if hold["status"] != HOLD_ACTIVE:
                raise DomainError(
                    "only an active hold can be confirmed",
                    code="HOLD_NOT_ACTIVE",
                    hold_id=hold_id,
                    status=hold["status"],
                )
            if hold["expires_at"] <= now:
                raise DomainError(
                    "hold expired before confirmation",
                    code="HOLD_ALREADY_EXPIRED",
                    hold_id=hold_id,
                    expires_at=hold["expires_at"],
                )

            quote = conn.execute("SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)).fetchone()
            if quote is None:
                raise DomainError(f"unknown quote: {quote_id}", code="QUOTE_NOT_FOUND", quote_id=quote_id)
            if quote["unit_id"] != hold["unit_id"]:
                raise DomainError(
                    "quote and hold refer to different units",
                    code="QUOTE_UNIT_MISMATCH",
                    quote_unit=quote["unit_id"],
                    hold_unit=hold["unit_id"],
                )
            if quote["policy_version_id"] != hold["policy_version_id"]:
                raise DomainError(
                    "quote and hold were priced with different policy versions",
                    code="QUOTE_POLICY_MISMATCH",
                    quote_policy=quote["policy_version_id"],
                    hold_policy=hold["policy_version_id"],
                )
            if quote["status"] == "VOID":
                raise DomainError("quote was voided", code="QUOTE_VOID")
            if quote["expires_at"] <= now:
                raise DomainError(
                    "quote expired before confirmation",
                    code="QUOTE_EXPIRED",
                    expires_at=quote["expires_at"],
                )

            unit = conn.execute("SELECT * FROM units WHERE unit_id = ?", (hold["unit_id"],)).fetchone()
            if unit is None:  # pragma: no cover - FK guarantees this
                raise DomainError("hold references an unknown unit", code="UNIT_NOT_FOUND")
            guests = adults + children
            if guests > unit["max_guests"]:
                raise DomainError(
                    "party exceeds the unit's maximum occupancy",
                    code="CAPACITY_EXCEEDED",
                    guests=guests,
                    max_guests=unit["max_guests"],
                )

            total = int(quote["total_minor"])
            if amount_paid_minor < 0 or amount_paid_minor > total:
                raise DomainError(
                    "amount_paid_minor must be between 0 and the quote total",
                    code="PAYMENT_INVALID_AMOUNT",
                    amount_paid_minor=amount_paid_minor,
                    total_minor=total,
                )

            # The slot is already ours via the hold, but re-check the range: another
            # booking may have been created directly (import, ops tool) since.
            # Our own hold is excluded — it is being converted right here.
            self._assert_no_overlap(
                conn,
                unit_id=hold["unit_id"],
                check_in=hold["check_in"],
                check_out=hold["check_out"],
                exclude_hold_id=hold_id,
            )

            booking_id = self.ids.new("book")
            conn.execute(
                """
                INSERT INTO bookings (
                    booking_id, property_id, unit_id, check_in, check_out, nights, status,
                    guest_ref, adults, children, infants, currency, policy_version_id,
                    policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,
                    security_deposit_minor, payment_status, idempotency_key, version_lock,
                    created_at, updated_at, confirmed_at, cancelled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL)
                """,
                (
                    booking_id,
                    hold["property_id"],
                    hold["unit_id"],
                    hold["check_in"],
                    hold["check_out"],
                    hold["nights"],
                    "CONFIRMED",
                    hold["guest_ref"],
                    adults,
                    children,
                    infants,
                    quote["currency"],
                    hold["policy_version_id"],
                    quote["policy_fingerprint"],
                    quote_id,
                    hold_id,
                    total,
                    amount_paid_minor,
                    int(quote["security_deposit_minor"]),
                    _payment_status(amount_paid_minor, total),
                    idempotency_key,
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE holds SET status = ?, closed_at = ?, close_reason = ?
                WHERE hold_id = ?
                """,
                (HOLD_CONVERTED, now, "converted_to_booking", hold_id),
            )
            self._append_state_event(
                conn,
                booking_id=booking_id,
                from_state="HOLD_ACTIVE",
                to_state="CONFIRMED",
                command="confirm_booking",
                command_id=command_id,
                actor=actor or self.actor,
                actor_role=actor_role or self.actor_role,
                reason=None,
                evidence_ref=hold["evidence_id"],
            )
            self._record_idempotency(
                conn, "confirm_from_hold", idempotency_key, request_hash, booking_id
            )
            self._audit(
                conn,
                action="booking.confirmed",
                entity_type="booking",
                entity_id=booking_id,
                after={
                    "status": "CONFIRMED",
                    "unit_id": hold["unit_id"],
                    "total_minor": total,
                    "quote_id": quote_id,
                    "hold_id": hold_id,
                },
                reason=None,
                policy_version_id=hold["policy_version_id"],
                evidence_ref=hold["evidence_id"],
                actor=actor,
            )
            row = conn.execute("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,)).fetchone()
            return self._booking_outcome(conn, row, replayed=False)

    def cancel_booking(
        self,
        *,
        booking_id: str,
        reason: str,
        command_id: str,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> str:
        """Cancel a booking. Cancelling twice is a no-op, not an error."""
        booking_id = require_ref(booking_id, "booking_id")
        command_id = require_ref(command_id, "command_id")
        _require_min_length(command_id, "command_id", 8)
        reason = require_text(reason, "reason", max_len=200)
        now = self._now()
        with self.transaction() as conn:
            booking = self._require_booking(conn, booking_id)
            if booking["status"] == "CANCELLED":
                return booking_id
            if booking["status"] in ("COMPLETED", "CLOSED"):
                raise DomainError(
                    "a completed or closed booking cannot be cancelled",
                    code="BOOKING_NOT_CANCELLABLE",
                    status=booking["status"],
                )
            if not self._transition_gate(booking["status"], "CANCELLED", "cancel_booking"):
                raise DomainError(
                    "state machine refuses this transition",
                    code="BOOKING_TRANSITION_REFUSED",
                    from_state=booking["status"],
                    to_state="CANCELLED",
                )
            conn.execute(
                """
                UPDATE bookings SET status = ?, cancelled_at = ?, updated_at = ?,
                    version_lock = version_lock + 1
                WHERE booking_id = ?
                """,
                ("CANCELLED", now, now, booking_id),
            )
            self._append_state_event(
                conn,
                booking_id=booking_id,
                from_state=booking["status"],
                to_state="CANCELLED",
                command="cancel_booking",
                command_id=command_id,
                actor=actor or self.actor,
                actor_role=actor_role or self.actor_role,
                reason=reason,
                evidence_ref=None,
            )
            self._audit(
                conn,
                action="booking.cancelled",
                entity_type="booking",
                entity_id=booking_id,
                before={"status": booking["status"]},
                after={"status": "CANCELLED", "cancelled_at": now},
                reason=reason,
                policy_version_id=booking["policy_version_id"],
                evidence_ref=None,
                actor=actor,
            )
        return booking_id

    # -- reads --------------------------------------------------------------- #
    def get_hold(self, hold_id: str) -> sqlite3.Row:
        return self._require_hold(self.conn, require_ref(hold_id, "hold_id"))

    def get_booking(self, booking_id: str) -> sqlite3.Row:
        return self._require_booking(self.conn, require_ref(booking_id, "booking_id"))

    def occupancy(self, unit_id: str, *, start: str, end: str) -> tuple[Mapping[str, Any], ...]:
        """Everything occupying ``unit_id`` in ``[start, end)`` — the overlap proof."""
        require_ref(unit_id, "unit_id")
        parse_iso_date(start)
        parse_iso_date(end)
        rows = self.conn.execute(
            """
            SELECT 'hold' AS kind, hold_id AS id, status, check_in, check_out
            FROM holds
            WHERE unit_id = ? AND status = ? AND check_in < ? AND ? < check_out
            UNION ALL
            SELECT 'booking' AS kind, booking_id AS id, status, check_in, check_out
            FROM bookings
            WHERE unit_id = ? AND status IN ({})
              AND check_in < ? AND ? < check_out
            ORDER BY check_in, id
            """.format(", ".join("?" for _ in OCCUPYING_BOOKING_STATES)),
            (unit_id, HOLD_ACTIVE, end, start, unit_id, *OCCUPYING_BOOKING_STATES, end, start),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def state_events(self, booking_id: str) -> tuple[Mapping[str, Any], ...]:
        rows = self.conn.execute(
            "SELECT * FROM booking_state_events WHERE booking_id = ? ORDER BY seq", (booking_id,)
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def audit_trail(self, *, entity_id: Optional[str] = None) -> tuple[Mapping[str, Any], ...]:
        if entity_id is None:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_events WHERE entity_id = ? ORDER BY seq", (entity_id,)
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def verify_audit_chain(self) -> bool:
        """Recompute the audit hash chain; False means the log was tampered with."""
        previous = _GENESIS_HASH
        for row in self.conn.execute("SELECT * FROM audit_events ORDER BY seq"):
            expected = _audit_hash(dict(row), previous)
            if row["hash"] != expected or row["prev_hash"] != previous:
                return False
            previous = row["hash"]
        return True

    # -- internals ---------------------------------------------------------- #
    def _now(self) -> str:
        return to_iso(self.clock.now())

    def _require_hold(self, conn: sqlite3.Connection, hold_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()
        if row is None:
            raise DomainError(f"unknown hold: {hold_id}", code="HOLD_NOT_FOUND", hold_id=hold_id)
        return row

    def _require_booking(self, conn: sqlite3.Connection, booking_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,)).fetchone()
        if row is None:
            raise DomainError(
                f"unknown booking: {booking_id}", code="BOOKING_NOT_FOUND", booking_id=booking_id
            )
        return row

    def _record_evidence_within(
        self,
        conn: sqlite3.Connection,
        **kwargs: Any,
    ) -> str:
        """Insert evidence inside an existing transaction (no nested BEGIN).

        Mirrors :meth:`record_evidence` for the auto-recorded case: a forward
        validity window and a non-empty source reference are mandatory in the
        schema, so they are always supplied.
        """
        evidence_id = self.ids.new("evid")
        captured = self._now()
        expires = to_iso(parse_iso(captured) + timedelta(hours=DEFAULT_EVIDENCE_TTL_HOURS))
        source_type = kwargs.get("source_type", "manual_ops")
        conn.execute(
            """
            INSERT INTO availability_evidence (
                evidence_id, unit_id, evidence_level, source_type, source_ref,
                captured_at, valid_until, result, occupancy_ranges,
                can_quote, can_hold, can_confirm, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                kwargs["unit_id"],
                kwargs.get("evidence_level", "B"),
                source_type,
                kwargs.get("source_ref") or f"{source_type}:{kwargs['unit_id']}",
                captured,
                expires,
                kwargs.get("result", "AVAILABLE"),
                1 if kwargs.get("can_quote", True) else 0,
                1 if kwargs.get("can_hold", True) else 0,
                1 if kwargs.get("can_confirm", False) else 0,
                kwargs.get("notes"),
                captured,
            ),
        )
        return evidence_id

    def _assert_no_overlap(
        self,
        conn: sqlite3.Connection,
        *,
        unit_id: str,
        check_in: str,
        check_out: str,
        exclude_hold_id: Optional[str] = None,
        exclude_booking_id: Optional[str] = None,
    ) -> None:
        """Refuse if any active hold or occupying booking intersects the window.

        Range logic: two half-open ranges overlap when ``a.start < b.end`` and
        ``b.start < a.end``. Anything else (touching endpoints) is a legal
        back-to-back stay.

        ``exclude_hold_id`` exists because converting a hold into a booking must
        not count *that same hold* as a conflict — it is being retired in the same
        transaction.
        """
        params: list[Any] = [unit_id, HOLD_ACTIVE, check_out, check_in]
        sql = """
            SELECT hold_id, check_in, check_out FROM holds
            WHERE unit_id = ? AND status = ? AND check_in < ? AND ? < check_out
        """
        if exclude_hold_id is not None:
            sql += " AND hold_id <> ?"
            params.append(exclude_hold_id)
        hold = conn.execute(sql + " LIMIT 1", tuple(params)).fetchone()
        if hold is not None:
            raise DomainError(
                "the unit is already held for an overlapping period",
                code="SLOT_ALREADY_HELD",
                unit_id=unit_id,
                conflicting_hold=hold["hold_id"],
                conflicting_window=(hold["check_in"], hold["check_out"]),
                requested_window=(check_in, check_out),
            )
        placeholders = ", ".join("?" for _ in OCCUPYING_BOOKING_STATES)
        params: list[Any] = [unit_id, *OCCUPYING_BOOKING_STATES, check_out, check_in]
        sql = f"""
            SELECT booking_id, check_in, check_out FROM bookings
            WHERE unit_id = ? AND status IN ({placeholders}) AND check_in < ? AND ? < check_out
        """
        if exclude_booking_id is not None:
            sql += " AND booking_id <> ?"
            params.append(exclude_booking_id)
        booking = conn.execute(sql + " LIMIT 1", tuple(params)).fetchone()
        if booking is not None:
            raise DomainError(
                "the unit is already booked for an overlapping period",
                code="SLOT_ALREADY_BOOKED",
                unit_id=unit_id,
                conflicting_booking=booking["booking_id"],
                conflicting_window=(booking["check_in"], booking["check_out"]),
                requested_window=(check_in, check_out),
            )

    def _close_hold(
        self, hold_id: str, *, status: str, reason: str, actor: Optional[str] = None
    ) -> str:
        hold_id = require_ref(hold_id, "hold_id")
        reason = require_text(reason, "reason", max_len=200)
        now = self._now()
        with self.transaction() as conn:
            hold = self._require_hold(conn, hold_id)
            if hold["status"] != HOLD_ACTIVE:
                if hold["status"] == status:
                    return hold_id  # already in the requested end state: idempotent
                raise DomainError(
                    "hold is already closed",
                    code="HOLD_NOT_ACTIVE",
                    hold_id=hold_id,
                    status=hold["status"],
                )
            conn.execute(
                "UPDATE holds SET status = ?, closed_at = ?, close_reason = ? WHERE hold_id = ?",
                (status, now, reason, hold_id),
            )
            self._audit(
                conn,
                action=f"hold.{status.lower()}",
                entity_type="hold",
                entity_id=hold_id,
                before={"status": HOLD_ACTIVE},
                after={"status": status, "closed_at": now, "close_reason": reason},
                reason=reason,
                policy_version_id=hold["policy_version_id"],
                evidence_ref=hold["evidence_id"],
                actor=actor,
            )
        return hold_id

    def _append_state_event(
        self,
        conn: sqlite3.Connection,
        *,
        booking_id: str,
        from_state: str,
        to_state: str,
        command: str,
        command_id: str,
        actor: str,
        actor_role: str,
        reason: Optional[str],
        evidence_ref: Optional[str],
    ) -> str:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM booking_state_events WHERE booking_id = ?",
            (booking_id,),
        ).fetchone()
        seq = int(row["last_seq"]) + 1
        event_id = self.ids.new("bse")
        conn.execute(
            """
            INSERT INTO booking_state_events (
                state_event_id, booking_id, seq, from_state, to_state, command, actor,
                actor_role, command_id, evidence_ref, approval_ref, reason, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                event_id, booking_id, seq, from_state, to_state, command, actor,
                actor_role, command_id, evidence_ref, reason, self._now(),
            ),
        )
        return event_id

    def _idempotent_lookup(
        self,
        conn: sqlite3.Connection,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> Optional[str]:
        row = conn.execute(
            "SELECT * FROM idempotency_keys WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise DomainError(
                "idempotency key reused with different operation or payload",
                code="IDEMPOTENCY_CONFLICT",
                idempotency_key=idempotency_key,
                stored_operation=row["operation"],
                incoming_operation=operation,
            )
        return row["result_ref"]

    def _record_idempotency(
        self,
        conn: sqlite3.Connection,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        result_ref: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO idempotency_keys (
                idempotency_key, operation, request_hash, result_ref, result_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                operation,
                request_hash,
                result_ref,
                sha256_hex(operation, result_ref),
                self._now(),
            ),
        )

    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        before: Optional[Mapping[str, Any]] = None,
        after: Optional[Mapping[str, Any]] = None,
        reason: Optional[str],
        policy_version_id: Optional[str],
        evidence_ref: Optional[str],
        actor: Optional[str] = None,
    ) -> str:
        """Append one audit row, chained to the previous hash inside this transaction."""
        last = conn.execute(
            "SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = (int(last["seq"]) + 1) if last is not None else 1
        prev_hash = last["hash"] if last is not None else _GENESIS_HASH
        event_id = self.ids.new("audit")
        record = {
            "audit_event_id": event_id,
            "seq": seq,
            "occurred_at": self._now(),
            "actor": actor or self.actor,
            "actor_role": self.actor_role,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "before_state": canonical_json(dict(before)) if before is not None else None,
            "after_state": canonical_json(dict(after)) if after is not None else None,
            "reason": reason,
            "policy_version_id": policy_version_id,
            "evidence_ref": evidence_ref,
        }
        digest = _audit_hash(record, prev_hash)
        conn.execute(
            """
            INSERT INTO audit_events (
                audit_event_id, seq, occurred_at, actor, actor_role, action, entity_type,
                entity_id, before_state, after_state, reason, policy_version_id, evidence_ref,
                tool, trace_id, prev_hash, hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                record["audit_event_id"], record["seq"], record["occurred_at"], record["actor"],
                record["actor_role"], record["action"], record["entity_type"], record["entity_id"],
                record["before_state"], record["after_state"], record["reason"],
                record["policy_version_id"], record["evidence_ref"], prev_hash, digest,
            ),
        )
        return event_id

    def _booking_outcome(
        self, conn: sqlite3.Connection, row: sqlite3.Row, *, replayed: bool
    ) -> BookingOutcome:
        total = int(row["total_minor"])
        deposit_pct = _policy_deposit_pct(conn, row["policy_version_id"])
        deposit_due = Money.from_minor(total, row["currency"]).pct(deposit_pct)
        return BookingOutcome(
            booking_id=row["booking_id"],
            unit_id=row["unit_id"],
            status=row["status"],
            check_in=row["check_in"],
            check_out=row["check_out"],
            nights=int(row["nights"]),
            currency=row["currency"],
            total_minor=total,
            deposit_due_minor=deposit_due.minor_units,
            balance_due_minor=total - deposit_due.minor_units,
            replayed=replayed,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _require_min_length(value: str, field_name: str, minimum: int) -> str:
    """Enforce a schema ``CHECK (length(col) >= n)`` in code, with a clear error.

    The database would reject the row anyway (``IntegrityError``); failing here
    turns an opaque storage error into a named domain error.
    """
    if len(value) < minimum:
        raise DomainError(
            f"{field_name} must be at least {minimum} characters",
            code="INVALID_FIELD_LENGTH",
            field=field_name,
            length=len(value),
            minimum=minimum,
        )
    return value


def _policy_deposit_pct(conn: sqlite3.Connection, policy_version_id: str) -> str:
    row = conn.execute(
        "SELECT deposit_pct FROM policy_versions WHERE policy_version_id = ?", (policy_version_id,)
    ).fetchone()
    if row is None:
        raise DomainError(
            f"unknown policy version: {policy_version_id}", code="POLICY_NOT_FOUND"
        )
    return str(row["deposit_pct"])


def _payment_status(paid_minor: int, total_minor: int) -> str:
    if paid_minor <= 0:
        return "UNPAID"
    if paid_minor < total_minor:
        return "DEPOSIT_CLEARED"
    return "CLEARED_FULL"


#: The exact fields covered by the audit hash. Kept as an explicit list so the
#: hash computed at write time and the hash recomputed during verification are
#: built from the same fields — a row-level diff (tool, trace_id) must not change
#: the answer.
_AUDIT_HASH_FIELDS: tuple[str, ...] = (
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
)


def _audit_hash(record: Mapping[str, Any], prev_hash: str) -> str:
    """Hash of the audit row over :data:`_AUDIT_HASH_FIELDS`, chained to the previous."""
    payload = {field: record.get(field) for field in _AUDIT_HASH_FIELDS}
    return sha256_hex("audit", prev_hash, canonical_json(payload))


#: Transitions the kernel performs itself, used when no richer gate is injected.
#: ``booking_states`` supplies the full machine; this keeps the kernel fail-closed
#: on its own rather than silently allowing anything.
_KERNEL_TRANSITIONS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("INQUIRY", "CANCELLED", "cancel_booking"),
        ("QUALIFIED", "CANCELLED", "cancel_booking"),
        ("AVAILABILITY_PENDING", "CANCELLED", "cancel_booking"),
        ("AVAILABILITY_CONFIRMED", "CANCELLED", "cancel_booking"),
        ("QUOTE_DRAFTED", "CANCELLED", "cancel_booking"),
        ("QUOTE_SENT", "CANCELLED", "cancel_booking"),
        ("QUOTE_EXPIRED", "CANCELLED", "cancel_booking"),
        ("HOLD_REQUESTED", "CANCELLED", "cancel_booking"),
        ("HOLD_ACTIVE", "CANCELLED", "cancel_booking"),
        ("HOLD_ACTIVE", "CONFIRMED", "confirm_booking"),
        ("PAYMENT_PENDING", "CANCELLED", "cancel_booking"),
        ("PAYMENT_PARTIAL", "CANCELLED", "cancel_booking"),
        ("DEPOSIT_PAID", "CANCELLED", "cancel_booking"),
        ("BALANCE_DUE", "CANCELLED", "cancel_booking"),
        ("BALANCE_OVERDUE", "CANCELLED", "cancel_booking"),
        ("CONFIRMED", "CANCELLED", "cancel_booking"),
        ("PRE_ARRIVAL", "CANCELLED", "cancel_booking"),
        ("IN_STAY", "CANCELLED", "cancel_booking"),
    }
)


def _default_transition_gate(from_state: str, to_state: str, command: str) -> bool:
    return (from_state, to_state, command) in _KERNEL_TRANSITIONS
