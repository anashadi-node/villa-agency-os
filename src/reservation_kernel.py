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
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

import matching as MATCHING
import policy_engine as PE
import quote_calculator as QC

from ai_service import require_tool_authority
from audit import AuditLog, append_event
from availability import (
    ANSWERED_STATE,
    CHECK_STATES,
    RESTRICTED_RESULT,
    RESTRICTION_KEYS,
    SOURCE_RESULTS,
    check_transition_allowed,
)
from gates import (
    CODE_EVIDENCE_STALE,
    AvailabilityEvidence,
    PURPOSE_CONFIRM,
    gate_availability,
)
from hold_states import (
    CLOSED_HOLD_STATES,
    HOLD_CONFIRMED,
    HOLD_CONVERTED,
    HOLD_EXPIRED_PENDING_VERIFICATION,
    HOLD_EXTENSION_REQUESTED,
    HOLD_RELEASE_CONFIRMED,
    LIVE_HOLD_STATES,
    OCCUPYING_HOLD_STATES,
    can_transition,
    resolve_timezone,
)
from primitives import (
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
    restriction_payload,
    sha256_hex,
    to_iso,
)
from schema_loader import applied_version, connect_sqlite

__all__ = [
    "ReservationKernel",
    "HoldOutcome",
    "BookingOutcome",
    "CheckOutcome",
    "AVAILABILITY_REQUEST_TOOL",
    "HOLD_CONFIRMED",
    "HOLD_RELEASE_CONFIRMED",
    "HOLD_CONVERTED",
    "HOLD_EXPIRED_PENDING_VERIFICATION",
    "BOOKING_STATES",
    "OCCUPYING_BOOKING_STATES",
    "DEFAULT_ROLE",
]

# The hold vocabulary lives in one place (``hold_states``, imported above): the
# schema CHECK, the partial unique index and the PG exclusion constraint each
# restate it, and ``tests/test_wave4b_hold_lifecycle.py`` pins them together.

#: Statuses that make a booking occupy its slot. Must match the partial unique
#: index ``uq_bookings_active_slot`` in the schema — the two are a pair.
OCCUPYING_BOOKING_STATES: tuple[str, ...] = (
    "CONFIRMED",
    "BALANCE_DUE",
    "BALANCE_OVERDUE",
    "PRE_ARRIVAL",
    "IN_STAY",
    # ADR-0002 (Opsi A): the slot stays bound while a cancellation is reviewed or
    # a dispute is open — see the matching partial index in 001_initial.sql.
    "CANCELLATION_REQUESTED",
    "CANCELLATION_REVIEW",
    "DISPUTED",
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
# The canonical vocabulary has exactly one home (§11, Appendix A).
EVIDENCE_RESULTS: tuple[str, ...] = SOURCE_RESULTS
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

#: The availability-check vocabulary (§7), mirroring the schema CHECK verbatim —
#: the same pairing ``EVIDENCE_RESULTS``/``SOURCE_RESULTS`` already has.
CHECK_REQUEST_STATES: tuple[str, ...] = CHECK_STATES

#: The §56 tool this flow has to pass. It is seeded in ``ai_tools`` as a MEDIUM
#: policy-controlled tool, which is why a machine actor is refused until the owner
#: publishes ``security.ai.medium_tool_allowance``: §56 names "availability requests"
#: among the actions an AI may take only under policy control.
AVAILABILITY_REQUEST_TOOL = "availability.request"
#: §56 lists "tentative-hold requests" beside availability requests, so the hold
#: path is gated by the same policy value family.
HOLD_REQUEST_TOOL = "hold.request_tentative"
#: The chain's starting value. 64 zeros rather than a word, because the schema
#: requires every ``prev_hash`` to look like a SHA-256 hex digest.


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #
def _validated_restrictions(result: str, restrictions: Any) -> Optional[str]:
    """The restriction payload as stored JSON, or a refusal saying why not.

    §11 requires a restricted state to state what is restricted, and the two keys
    are the two the spec names: an unknown key is a typo that would otherwise be
    stored and silently never enforced.
    """
    payload = restriction_payload(restrictions)
    if result == RESTRICTED_RESULT and not payload:
        raise DomainError(
            "a restricted availability result must state its restriction",
            code="EVIDENCE_RESTRICTION_REQUIRED",
            restrictions=restrictions,
        )
    if payload and result != RESTRICTED_RESULT:
        raise DomainError(
            "only a restricted availability result may carry a restriction",
            code="EVIDENCE_RESTRICTION_NOT_APPLICABLE",
            result=result,
        )
    unknown = sorted(set(payload) - set(RESTRICTION_KEYS))
    if unknown:
        raise DomainError(
            f"unknown restriction keys {unknown}: expected {list(RESTRICTION_KEYS)}",
            code="EVIDENCE_INVALID_RESTRICTIONS",
            restrictions=payload,
        )
    for key, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise DomainError(
                f"restriction {key} must be a positive integer",
                code="EVIDENCE_INVALID_RESTRICTIONS",
                restrictions=payload,
            )
    return canonical_json(payload) if payload else None


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


@dataclass(frozen=True)
class CheckOutcome:
    """Result of a check-availability request.

    ``state`` is the request's own state (``CHECK_REQUESTED``, ``CHECKING`` or
    ``ANSWERED``); once it is ``ANSWERED``, ``evidence_id`` names the answer. The
    canonical availability state of the unit is derived separately, by
    ``availability.AvailabilityService``.
    """

    check_id: str
    unit_id: str
    check_in: str
    check_out: str
    state: str
    requested_at: str
    evidence_id: Optional[str] = None
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
        agency_id: str = "agency_default",
        transition_gate: Optional[Callable[[str, str, str], bool]] = None,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.actor = actor
        self.actor_role = actor_role
        self.agency_id = require_ref(agency_id, "agency_id")
        self._transition_gate = transition_gate or _default_transition_gate
        self._in_tx = False
        self._ensure_agency_scope()

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
        agency_id: str = "agency_default",
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
            agency_id=agency_id,
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

    def _ensure_agency_scope(self) -> None:
        """Make the kernel agency explicit before any scoped write/read."""
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO agencies (agency_id, name, status, created_at, updated_at)
            VALUES (?, ?, 'ACTIVE', ?, ?)
            ON CONFLICT(agency_id) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            (self.agency_id, self.agency_id, now, now),
        )

    # -- transactions ------------------------------------------------------- #
    @contextmanager
    def transaction(self, *, join: bool = False) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes the lock before it reads.

        Nesting is refused by default: a silent nested ``BEGIN`` would either be a no-op
        (and lose the guarantee) or raise mid-write.

        ``join=True`` is the explicit way for a caller that already holds this kernel's
        transaction to have an inner call run inside it. A bulk inventory import needs exactly
        that: the whole file is one transaction, so a refused row half-way cannot leave half a
        villa list behind. It is opt-in, so it is never a silent no-op.
        """
        if join and self._in_tx:
            yield self.conn
            return
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
        join: bool = False,
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
        with self.transaction(join=join) as conn:
            existing = conn.execute(
                "SELECT agency_id FROM properties WHERE property_id = ?", (property_id,)
            ).fetchone()
            if existing is not None and existing["agency_id"] != self.agency_id:
                raise DomainError(
                    f"unknown property: {property_id}",
                    code="PROPERTY_NOT_FOUND",
                    property_id=property_id,
                )
            conn.execute(
                """
                INSERT INTO properties (
                    property_id, agency_id, name, property_type, region, province, timezone,
                    status, contract_pack_status, contract_pack_evaluated_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    self.agency_id,
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
        join: bool = False,
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
        with self.transaction(join=join) as conn:
            prop = conn.execute(
                "SELECT property_id FROM properties WHERE property_id = ? AND agency_id = ?",
                (property_id, self.agency_id),
            ).fetchone()
            if prop is None:
                raise DomainError(
                    f"unknown property: {property_id}",
                    code="PROPERTY_NOT_FOUND",
                    property_id=property_id,
                )
            existing = conn.execute(
                "SELECT agency_id FROM units WHERE unit_id = ?", (unit_id,)
            ).fetchone()
            if existing is not None and existing["agency_id"] != self.agency_id:
                raise DomainError(f"unknown unit: {unit_id}", code="UNIT_NOT_FOUND", unit_id=unit_id)
            conn.execute(
                """
                INSERT INTO units (
                    unit_id, agency_id, property_id, name, unit_type, bedrooms, bathrooms,
                    base_capacity, max_guests, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    self.agency_id,
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
        row = dict(policy)
        row.setdefault("agency_id", self.agency_id)
        with self.transaction() as conn:
            columns = sorted(row)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO policy_versions ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
        return str(row["policy_version_id"])

    def insert_rate_card(self, card: Mapping[str, Any], seasons: Sequence[Mapping[str, Any]] = ()) -> str:
        row = dict(card)
        row.setdefault("agency_id", self.agency_id)
        with self.transaction() as conn:
            columns = sorted(row)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO rate_cards ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
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
        row.setdefault("agency_id", self.agency_id)
        with self.transaction() as conn:
            columns = sorted(row)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO quotes ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
        return str(row["quote_id"])

    def quote_stay(
        self,
        *,
        unit_id: Any,
        check_in: Any,
        check_out: Any,
        adults: Any,
        children: Any = 0,
        infants: Any = 0,
        extras: Optional[Sequence[Mapping[str, Any]]] = None,
        discount_minor: int = 0,
        discount_reason: Optional[str] = None,
    ) -> Any:
        """Derive the price of a stay from the versions in force — §9's pricing step.

        This method **assembles**, it does not price: the policy version covering the stay comes
        from :func:`matching.policy_for` (the same version the ranking applied), the rate card
        from :func:`matching.rate_card_for`, and the arithmetic from
        :func:`quote_calculator.calculate_quote`. Re-deriving "which card wins" here would be a
        second price for the same stay, so no query in this method chooses a version — every
        choice is delegated to the rule that already makes it.

        It also **records nothing**. §56 tiers the AI's acts toward the outside, and this one has
        no outside: it reads the agency's own rows and returns a figure. The priced stay becomes
        a row through :meth:`persist_quote`, which the offer path takes after its door — so a
        kill switch that leaves the AI able to answer a price still leaves it unable to start an
        offer.

        Nothing is guessed. An unknown unit, no policy in force, or no rate card in force refuses
        with a machine-readable code instead of producing a number from an absent basis; the
        card's own refusals (a season outside the card window, say) propagate unchanged. Guest
        counts and money are handed to the request object, which validates them — this method
        does not coerce a caller's value into a valid one.
        """
        unit_id = require_ref(unit_id, "unit_id")
        window_start = parse_iso_date(check_in)
        window_end = parse_iso_date(check_out)
        # Refuses a window that does not move forward, with the same code the availability
        # and hold paths raise: one mistake keeps one name across the flows.
        nights_between(window_start, window_end)

        issued_at = self._now()
        with self.transaction() as conn:
            unit = conn.execute(
                "SELECT * FROM units WHERE unit_id = ? AND agency_id = ?",
                (unit_id, self.agency_id),
            ).fetchone()
            if unit is None:
                raise DomainError(f"unknown unit: {unit_id}", code="UNIT_NOT_FOUND", unit_id=unit_id)

            policy_row = MATCHING.policy_for(
                conn, unit, window_start.isoformat(), window_end.isoformat()
            )
            if policy_row is None:
                raise DomainError(
                    "no policy version covers this stay",
                    code="QUOTE_NO_POLICY",
                    unit_id=unit_id,
                    property_id=str(unit["property_id"]),
                    check_in=window_start.isoformat(),
                    check_out=window_end.isoformat(),
                )
            policy = PE.PolicyVersion.from_row(policy_row)

            card, card_note = MATCHING.rate_card_for(
                conn, unit, window_start.isoformat(), window_end.isoformat()
            )
            if card is None:
                raise DomainError(
                    "no rate card covers this stay",
                    code="QUOTE_NO_RATE_CARD",
                    unit_id=unit_id,
                    property_id=str(unit["property_id"]),
                    check_in=window_start.isoformat(),
                    check_out=window_end.isoformat(),
                    note=card_note,
                )

            request = QC.QuoteRequest(
                quote_date=parse_iso(issued_at).date().isoformat(),
                check_in=window_start.isoformat(),
                check_out=window_end.isoformat(),
                adults=adults,
                children=children,
                infants=infants,
                extras=tuple(extras or ()),
                discount_minor=discount_minor,
                discount_reason=discount_reason,
            )
            facts = QC.UnitFacts(
                unit_id=unit_id,
                property_id=str(unit["property_id"]),
                base_capacity=int(unit["base_capacity"]),
                max_guests=int(unit["max_guests"]),
            )
        return QC.calculate_quote(
            request, policy, card, facts, quote_id=self.ids.new("quote"), issued_at=issued_at
        )

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
        restrictions: Optional[Mapping[str, Any]] = None,
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
        restrictions_json = _validated_restrictions(normalised_result, restrictions)
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
                    evidence_id, agency_id, unit_id, evidence_level, source_type, source_ref,
                    captured_at, valid_until, result, occupancy_ranges,
                    can_quote, can_hold, can_confirm, notes, created_at, restrictions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    self.agency_id,
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
                    restrictions_json,
                ),
            )
        return evidence_id

    def get_evidence(self, evidence_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM availability_evidence WHERE evidence_id = ? AND agency_id = ?",
            (evidence_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                f"unknown availability evidence: {evidence_id}",
                code="EVIDENCE_NOT_FOUND",
                evidence_id=evidence_id,
            )
        return row

    # -- availability checks (§7) ------------------------------------------- #
    def request_availability_check(
        self,
        *,
        unit_id: str,
        check_in: str,
        check_out: str,
        actor: str,
        actor_role: str,
        idempotency_key: str,
        principal_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> CheckOutcome:
        """Ask a source whether a unit is free for a window — §7's CHECK AVAILABILITY.

        This is the platform's *outbound* question, which is why §56 applies to it: a
        machine actor has to clear ``availability.request`` first. A human actor is
        admitted directly. The gate runs before the idempotency lookup, so a replay is
        gated too — the permission is about asking, not about creating a row.

        No evidence is required to ask. Asking is how you find out.
        """
        unit_id = require_ref(unit_id, "unit_id")
        idempotency_key = require_ref(idempotency_key, "idempotency_key")
        _require_min_length(idempotency_key, "idempotency_key", 8)
        window_start = parse_iso_date(check_in)
        window_end = parse_iso_date(check_out)
        # Refuses a window that does not move forward, with the same code the hold path
        # raises: one mistake keeps one name across the two flows.
        nights_between(window_start, window_end)
        actor = require_ref(actor, "actor", max_len=64)
        actor_role = require_ref(actor_role, "actor_role", max_len=64)
        if notes is not None:
            notes = require_text(notes, "notes", max_len=500)
        self._require_tool_authority(
            actor, actor_role, principal_id, tool=AVAILABILITY_REQUEST_TOOL
        )

        request_hash = sha256_hex(
            "availability_check",
            unit_id,
            window_start.isoformat(),
            window_end.isoformat(),
            actor,
            actor_role,
        )
        with self.transaction() as conn:
            replay = self._idempotent_lookup(
                conn, "request_availability_check", idempotency_key, request_hash
            )
            if replay is not None:
                row = conn.execute(
                    "SELECT * FROM availability_checks WHERE check_id = ? AND agency_id = ?",
                    (replay, self.agency_id),
                ).fetchone()
                if row is None:  # pragma: no cover - defensive
                    raise DomainError(
                        "idempotency ledger points at a missing availability check",
                        code="IDEMPOTENCY_DANGLING",
                        check_id=replay,
                    )
                return self._check_outcome(conn, row, replayed=True)

            unit = conn.execute(
                "SELECT * FROM units WHERE unit_id = ? AND agency_id = ?",
                (unit_id, self.agency_id),
            ).fetchone()
            if unit is None:
                raise DomainError(f"unknown unit: {unit_id}", code="UNIT_NOT_FOUND", unit_id=unit_id)

            check_id = self.ids.new("chk")
            requested_at = self._now()
            conn.execute(
                """
                INSERT INTO availability_checks (
                    check_id, agency_id, unit_id, check_in, check_out, requested_by,
                    requested_role, requested_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    check_id,
                    self.agency_id,
                    unit_id,
                    window_start.isoformat(),
                    window_end.isoformat(),
                    actor,
                    actor_role,
                    requested_at,
                    notes,
                ),
            )
            self._transition_check(
                conn,
                check_id=check_id,
                to_state="CHECK_REQUESTED",
                actor=actor,
                actor_role=actor_role,
            )
            self._record_idempotency(
                conn, "request_availability_check", idempotency_key, request_hash, check_id
            )
            self._audit(
                conn,
                action="availability.check_requested",
                entity_type="availability_check",
                entity_id=check_id,
                after={
                    "unit_id": unit_id,
                    "check_in": window_start.isoformat(),
                    "check_out": window_end.isoformat(),
                    "state": "CHECK_REQUESTED",
                },
                reason=None,
                policy_version_id=None,
                evidence_ref=None,
                actor=actor,
            )
            row = conn.execute(
                "SELECT * FROM availability_checks WHERE check_id = ?", (check_id,)
            ).fetchone()
            return self._check_outcome(conn, row, replayed=False)

    def begin_check(
        self,
        check_id: str,
        *,
        actor: str,
        actor_role: str,
        note: Optional[str] = None,
    ) -> CheckOutcome:
        """Record that the question is with the source and an answer is outstanding."""
        return self._move_check(
            check_id,
            to_state="CHECKING",
            action="availability.check_started",
            actor=actor,
            actor_role=actor_role,
            note=note,
        )

    def answer_check(
        self,
        check_id: str,
        *,
        evidence_id: str,
        actor: str,
        actor_role: str,
        note: Optional[str] = None,
    ) -> CheckOutcome:
        """Close a request with the answer it received.

        The evidence is checked here for existence, unit and tenant — and *not* for
        freshness. Freshness is a property of reading (ADR-0019): refusing to record
        what a source said because the answer has since aged would lose the fact that
        it was ever said. An aged answer reports RECONFIRMATION_REQUIRED instead.

        A machine actor is not gated here. Recording what a source reported is not the
        outbound act §56 governs; what authorises a quote or a hold is the gate at the
        point of use, and that gate reads the evidence, not this request.
        """
        check_id = require_ref(check_id, "check_id")
        evidence_id = require_ref(evidence_id, "evidence_id")
        actor = require_ref(actor, "actor", max_len=64)
        actor_role = require_ref(actor_role, "actor_role", max_len=64)
        if note is not None:
            note = require_text(note, "note", max_len=500)
        with self.transaction() as conn:
            row = self._check_row(conn, check_id)
            evidence = conn.execute(
                "SELECT * FROM availability_evidence WHERE evidence_id = ? AND agency_id = ?",
                (evidence_id, self.agency_id),
            ).fetchone()
            if evidence is None:
                raise DomainError(
                    f"unknown availability evidence: {evidence_id}",
                    code="EVIDENCE_NOT_FOUND",
                    evidence_id=evidence_id,
                )
            if evidence["unit_id"] != row["unit_id"]:
                raise DomainError(
                    "evidence belongs to a different unit",
                    code="EVIDENCE_UNIT_MISMATCH",
                    evidence_unit=evidence["unit_id"],
                    unit_id=row["unit_id"],
                )
            self._transition_check(
                conn,
                check_id=check_id,
                to_state=ANSWERED_STATE,
                actor=actor,
                actor_role=actor_role,
                evidence_id=evidence_id,
                note=note,
            )
            self._audit(
                conn,
                action="availability.check_answered",
                entity_type="availability_check",
                entity_id=check_id,
                after={
                    "state": ANSWERED_STATE,
                    "evidence_id": evidence_id,
                    "result": evidence["result"],
                },
                reason=None,
                policy_version_id=None,
                evidence_ref=evidence_id,
                actor=actor,
            )
            return self._check_outcome(conn, row, replayed=False)

    def get_check(self, check_id: str) -> sqlite3.Row:
        reference = require_ref(check_id, "check_id")
        row = self.conn.execute(
            "SELECT * FROM availability_checks WHERE check_id = ? AND agency_id = ?",
            (reference, self.agency_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                f"unknown availability check: {reference}",
                code="CHECK_NOT_FOUND",
                check_id=reference,
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
        actor: str,
        actor_role: str,
        principal_id: Optional[str] = None,
        quote_ref: Optional[str] = None,
        evidence_id: Optional[str] = None,
        ttl_minutes: Optional[int] = None,
    ) -> HoldOutcome:
        """Lock inventory for a short window, or fail with a reason.

        Taking a slot out of sale is an action the platform has to attribute, so the
        caller's identity is a required argument rather than an optional one: a gate
        that a caller can skip by omitting an argument is not a gate.

        The order matters: identity first (§56 — a machine actor needs the owner to
        publish ``hold.request_tentative``, a human actor is admitted directly), then
        idempotency, then evidence, then overlap, all inside one write transaction so
        nothing can change underneath.
        """
        unit_id = require_ref(unit_id, "unit_id")
        guest_ref = require_ref(guest_ref, "guest_ref")
        policy_version_id = require_ref(policy_version_id, "policy_version_id")
        idempotency_key = require_ref(idempotency_key, "idempotency_key")
        _require_min_length(idempotency_key, "idempotency_key", 8)
        actor = require_ref(actor, "actor", max_len=64)
        actor_role = require_ref(actor_role, "actor_role", max_len=64)
        nights = nights_between(parse_iso_date(check_in), parse_iso_date(check_out))
        # Outside the transaction, and before the idempotency lookup, for the reason
        # ADR-0021 gives: the permission is about taking the slot, so a replay of a
        # refused request is refused too. AiService writes its own audit row.
        self._require_tool_authority(
            actor, actor_role, principal_id, tool=HOLD_REQUEST_TOOL
        )

        request_hash = sha256_hex("hold", unit_id, check_in, check_out, guest_ref, policy_version_id)
        now = self._now()

        with self.transaction() as conn:
            replay = self._idempotent_lookup(conn, "request_hold", idempotency_key, request_hash)
            if replay is not None:
                row = conn.execute(
                    "SELECT * FROM holds WHERE hold_id = ? AND agency_id = ?", (replay, self.agency_id)
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

            unit = conn.execute(
                "SELECT * FROM units WHERE unit_id = ? AND agency_id = ?",
                (unit_id, self.agency_id),
            ).fetchone()
            if unit is None:
                raise DomainError(f"unknown unit: {unit_id}", code="UNIT_NOT_FOUND", unit_id=unit_id)
            property_id = unit["property_id"]
            policy = conn.execute(
                "SELECT * FROM policy_versions WHERE policy_version_id = ? AND agency_id = ?",
                (policy_version_id, self.agency_id),
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
                "SELECT * FROM availability_evidence WHERE evidence_id = ? AND agency_id = ?",
                (evidence_id, self.agency_id),
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
                    "SELECT * FROM quotes WHERE quote_id = ? AND agency_id = ?",
                    (quote_ref, self.agency_id),
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
            # §7: an expiry without the zone it was agreed in is half a fact.
            property_row = conn.execute(
                "SELECT timezone FROM properties WHERE property_id = ?", (property_id,)
            ).fetchone()
            if property_row is None:  # pragma: no cover - FK guarantees this
                raise DomainError(
                    f"unknown property: {property_id}", code="PROPERTY_NOT_FOUND"
                )
            expires_timezone = resolve_timezone(property_row["timezone"])
            conn.execute(
                """
                INSERT INTO holds (
                    hold_id, agency_id, unit_id, property_id, check_in, check_out, nights, status,
                    guest_ref, quote_ref, policy_version_id, evidence_id, idempotency_key,
                    created_at, expires_at, expires_timezone, closed_at, close_reason,
                    extended_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)
                """,
                (
                    hold_id, self.agency_id, unit_id, property_id, check_in, check_out, nights,
                    HOLD_CONFIRMED,
                    guest_ref, quote_ref, policy_version_id, evidence_id, idempotency_key,
                    now, expires_at, expires_timezone,
                ),
            )
            self._record_idempotency(conn, "request_hold", idempotency_key, request_hash, hold_id)
            self._audit(
                conn,
                action="hold.requested",
                entity_type="hold",
                entity_id=hold_id,
                after={
                    "status": HOLD_CONFIRMED,
                    "unit_id": unit_id,
                    "expires_at": expires_at,
                    "expires_timezone": expires_timezone,
                },
                reason=None,
                policy_version_id=policy_version_id,
                evidence_ref=evidence_id,
                actor=actor,
            )
            return HoldOutcome(
                hold_id=hold_id,
                unit_id=unit_id,
                status=HOLD_CONFIRMED,
                check_in=check_in,
                check_out=check_out,
                nights=nights,
                evidence_id=evidence_id,
                expires_at=expires_at,
            )

    def release_hold(self, hold_id: str, *, reason: str, actor: Optional[str] = None) -> str:
        """Release a hold so the slot can be sold again."""
        hold_id = require_ref(hold_id, "hold_id")
        reason = require_text(reason, "reason", max_len=200)
        with self.transaction() as conn:
            return self._transition_hold(
                conn,
                hold_id,
                HOLD_RELEASE_CONFIRMED,
                reason=reason,
                actor=actor,
                action="hold.released",
            )

    def expire_hold(self, hold_id: str, *, actor: Optional[str] = None) -> str:
        """Record a TTL expiry as pending verification (§7), never as a release.

        Normally called by :meth:`sweep_expired_holds`. A hold whose TTL ran out is
        only *believed* to be over until an authoritative source says so.
        """
        hold_id = require_ref(hold_id, "hold_id")
        with self.transaction() as conn:
            return self._transition_hold(
                conn,
                hold_id,
                HOLD_EXPIRED_PENDING_VERIFICATION,
                reason="ttl_expired",
                actor=actor,
                action="hold.expired",
            )

    def request_extension(
        self,
        hold_id: str,
        *,
        minutes: Optional[int] = None,
        timezone: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> str:
        """Ask for more time on a hold, without moving its expiry (§7).

        "EXTENSION_REQUESTED tidak memperpanjang expiry sampai supplier
        mengonfirmasi": the request is a row of its own, and the expiry moves only
        in :meth:`confirm_extension`. Returns the extension request id.
        """
        hold_id = require_ref(hold_id, "hold_id")
        with self.transaction() as conn:
            hold = self._require_hold(conn, hold_id)
            if not can_transition(hold["status"], HOLD_EXTENSION_REQUESTED):
                raise DomainError(
                    f"a hold in {hold['status']} cannot be extended",
                    code="HOLD_ILLEGAL_TRANSITION",
                    hold_id=hold_id,
                    status=hold["status"],
                )
            if hold["expires_at"] <= self._now():
                # The TTL is already gone; asking for more time on a lapsed hold is a
                # request to revive it, and §7 has no edge for that.
                raise DomainError(
                    "hold already expired; release and re-request instead",
                    code="HOLD_ALREADY_EXPIRED",
                    hold_id=hold_id,
                    expires_at=hold["expires_at"],
                )
            policy = self._require_policy(conn, hold["policy_version_id"])
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
            agreed_zone = resolve_timezone(timezone or hold["expires_timezone"])
            extension_id = self.ids.new("hext")
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM hold_extension_requests"
                " WHERE hold_id = ?",
                (hold_id,),
            ).fetchone()["next_seq"]
            conn.execute(
                """
                INSERT INTO hold_extension_requests (
                    extension_id, agency_id, hold_id, seq, requested_minutes,
                    requested_timezone, requested_at, requested_by, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REQUESTED')
                """,
                (
                    extension_id,
                    self.agency_id,
                    hold_id,
                    next_seq,
                    step,
                    agreed_zone,
                    self._now(),
                    actor,
                ),
            )
            self._transition_hold(
                conn,
                hold_id,
                HOLD_EXTENSION_REQUESTED,
                reason="extension_requested",
                actor=actor,
                action="hold.extension_requested",
            )
        return extension_id

    def confirm_extension(self, extension_id: str, *, actor: Optional[str] = None) -> str:
        """Confirm a requested extension: only now does the expiry move (§7)."""
        extension_id = require_ref(extension_id, "extension_id")
        with self.transaction() as conn:
            request = self._require_extension(conn, extension_id)
            if request["status"] != "REQUESTED":
                raise DomainError(
                    "only a requested extension can be confirmed",
                    code="EXTENSION_ALREADY_DECIDED",
                    extension_id=extension_id,
                    status=request["status"],
                )
            hold = self._require_hold(conn, request["hold_id"])
            if hold["status"] != HOLD_EXTENSION_REQUESTED:
                raise DomainError(
                    f"a hold in {hold['status']} has no extension to confirm",
                    code="HOLD_ILLEGAL_TRANSITION",
                    hold_id=hold["hold_id"],
                    status=hold["status"],
                )
            if hold["expires_at"] <= self._now():
                raise DomainError(
                    "hold expired before the extension was confirmed",
                    code="HOLD_ALREADY_EXPIRED",
                    hold_id=hold["hold_id"],
                    expires_at=hold["expires_at"],
                )
            policy = self._require_policy(conn, hold["policy_version_id"])
            limit = int(policy["max_hold_extensions"])
            if hold["extended_count"] >= limit:
                raise DomainError(
                    "hold extension limit reached",
                    code="HOLD_EXTENSION_LIMIT",
                    hold_id=hold["hold_id"],
                    max_hold_extensions=limit,
                )
            step = int(request["requested_minutes"])
            new_expiry = to_iso(parse_iso(hold["expires_at"]) + timedelta(minutes=step))
            now = self._now()
            conn.execute(
                "UPDATE holds SET expires_at = ?, expires_timezone = ?,"
                " extended_count = extended_count + 1 WHERE hold_id = ?",
                (new_expiry, request["requested_timezone"], hold["hold_id"]),
            )
            conn.execute(
                "UPDATE hold_extension_requests SET status = 'CONFIRMED', decided_at = ?,"
                " decided_by = ?, decided_timezone = ? WHERE extension_id = ?",
                (now, actor, request["requested_timezone"], extension_id),
            )
            self._audit(
                conn,
                action="hold.extension_confirmed",
                entity_type="hold",
                entity_id=hold["hold_id"],
                before={"expires_at": hold["expires_at"], "status": HOLD_EXTENSION_REQUESTED},
                after={
                    "expires_at": new_expiry,
                    "expires_timezone": request["requested_timezone"],
                    "extended_count": hold["extended_count"] + 1,
                    "extension_id": extension_id,
                },
                reason="extension_confirmed",
                policy_version_id=hold["policy_version_id"],
                evidence_ref=hold["evidence_id"],
                actor=actor,
            )
            self._transition_hold(
                conn,
                hold["hold_id"],
                HOLD_CONFIRMED,
                reason="extension_confirmed",
                actor=actor,
                action="hold.extension_answered",
            )
        return new_expiry

    def reject_extension(
        self, extension_id: str, *, reason: str, actor: Optional[str] = None
    ) -> str:
        """Refuse a requested extension: the expiry stays exactly where it was (§7)."""
        extension_id = require_ref(extension_id, "extension_id")
        reason = require_text(reason, "reason", max_len=200)
        with self.transaction() as conn:
            request = self._require_extension(conn, extension_id)
            if request["status"] != "REQUESTED":
                raise DomainError(
                    "only a requested extension can be rejected",
                    code="EXTENSION_ALREADY_DECIDED",
                    extension_id=extension_id,
                    status=request["status"],
                )
            hold = self._require_hold(conn, request["hold_id"])
            conn.execute(
                "UPDATE hold_extension_requests SET status = 'REJECTED', decided_at = ?,"
                " decided_by = ?, reason = ? WHERE extension_id = ?",
                (self._now(), actor, reason, extension_id),
            )
            self._audit(
                conn,
                action="hold.extension_rejected",
                entity_type="hold",
                entity_id=hold["hold_id"],
                before={"expires_at": hold["expires_at"], "status": hold["status"]},
                after={"expires_at": hold["expires_at"], "extension_id": extension_id},
                reason=reason,
                policy_version_id=hold["policy_version_id"],
                evidence_ref=hold["evidence_id"],
                actor=actor,
            )
            if hold["status"] == HOLD_EXTENSION_REQUESTED:
                self._transition_hold(
                    conn,
                    hold["hold_id"],
                    HOLD_CONFIRMED,
                    reason="extension_rejected",
                    actor=actor,
                    action="hold.extension_answered",
                )
        return hold["expires_at"]

    def verify_expired_hold(
        self, hold_id: str, *, evidence_id: str, actor: Optional[str] = None
    ) -> str:
        """Close an unverified expiry once an authoritative source confirms it (§7)."""
        hold_id = require_ref(hold_id, "hold_id")
        evidence_id = require_ref(evidence_id, "evidence_id")
        with self.transaction() as conn:
            hold = self._require_hold(conn, hold_id)
            if hold["status"] != HOLD_EXPIRED_PENDING_VERIFICATION:
                raise DomainError(
                    "hold is not pending verification",
                    code="HOLD_NOT_PENDING_VERIFICATION",
                    hold_id=hold_id,
                    status=hold["status"],
                )
            return self._transition_hold(
                conn,
                hold_id,
                HOLD_RELEASE_CONFIRMED,
                reason="expiry_verified",
                actor=actor,
                evidence_ref=evidence_id,
                action="hold.release_confirmed",
            )

    def sweep_expired_holds(self, *, now: Optional[str] = None) -> tuple[str, ...]:
        """Move every hold whose TTL has passed to EXPIRED_PENDING_VERIFICATION (§7).

        The target state is deliberately not a release: only an authoritative source
        may say the supplier's hold is really gone, so the slot stays blocked until
        :meth:`verify_expired_hold` is satisfied. A hold that is already pending
        verification is left alone, which is what makes the sweep idempotent.
        """
        moment = now or self._now()
        placeholders = ", ".join("?" for _ in LIVE_HOLD_STATES)
        with self.transaction() as conn:
            rows = conn.execute(
                f"SELECT hold_id FROM holds WHERE status IN ({placeholders})"
                " AND expires_at <= ? ORDER BY hold_id",
                (*LIVE_HOLD_STATES, moment),
            ).fetchall()
            for row in rows:
                self._transition_hold(
                    conn,
                    row["hold_id"],
                    HOLD_EXPIRED_PENDING_VERIFICATION,
                    reason="ttl_expired",
                    actor=None,
                    action="hold.expired",
                    at=moment,
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
                row = conn.execute(
                    "SELECT * FROM bookings WHERE booking_id = ? AND agency_id = ?",
                    (replay, self.agency_id),
                ).fetchone()
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
                    "SELECT * FROM bookings WHERE hold_ref = ? AND agency_id = ? ORDER BY created_at LIMIT 1",
                    (hold_id, self.agency_id),
                ).fetchone()
                if existing is not None:
                    return self._booking_outcome(conn, existing, replayed=True)
            if hold["status"] != HOLD_CONFIRMED:
                raise DomainError(
                    "only a confirmed hold can be confirmed into a booking",
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

            quote = conn.execute(
                "SELECT * FROM quotes WHERE quote_id = ? AND agency_id = ?",
                (quote_id, self.agency_id),
            ).fetchone()
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

            # §9's commercial gate: a booking rests on a price the guest *said yes to* — the
            # accepted version of this quote, for this stay. Without it a confirmation turns an
            # unanswered offer into a contract. The check sits before the booking row exists, and
            # names its two refusals apart: nobody answered, or somebody answered other dates.
            accepted = conn.execute(
                "SELECT unit_id, check_in, check_out FROM quotation_versions"
                " WHERE quote_id = ? AND agency_id = ? AND status = 'ACCEPTED'"
                " ORDER BY version DESC",
                (quote_id, self.agency_id),
            ).fetchall()
            if not accepted:
                raise DomainError(
                    f"quote {quote_id} carries no accepted version: a booking needs the version "
                    "the guest accepted, not an offer that was only sent",
                    code="CONFIRM_NOT_ACCEPTED",
                    quote_id=quote_id,
                )
            if not any(
                row["unit_id"] == hold["unit_id"]
                and row["check_in"] == hold["check_in"]
                and row["check_out"] == hold["check_out"]
                for row in accepted
            ):
                raise DomainError(
                    "the accepted version of this quote covers another stay: a yes for other "
                    "dates is not a yes for these",
                    code="CONFIRM_ACCEPTED_OTHER_STAY",
                    quote_id=quote_id,
                    unit_id=hold["unit_id"],
                    check_in=hold["check_in"],
                    check_out=hold["check_out"],
                )

            unit = conn.execute(
                "SELECT * FROM units WHERE unit_id = ? AND agency_id = ?",
                (hold["unit_id"], self.agency_id),
            ).fetchone()
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
                    booking_id, agency_id, property_id, unit_id, check_in, check_out, nights, status,
                    guest_ref, adults, children, infants, currency, policy_version_id,
                    policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,
                    security_deposit_minor, payment_status, idempotency_key, version_lock,
                    created_at, updated_at, confirmed_at, cancelled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL)
                """,
                (
                    booking_id,
                    self.agency_id,
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
            row = conn.execute(
                "SELECT * FROM bookings WHERE booking_id = ? AND agency_id = ?",
                (booking_id, self.agency_id),
            ).fetchone()
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
            WHERE agency_id = ? AND unit_id = ? AND status IN ({}) AND check_in < ? AND ? < check_out
            UNION ALL
            SELECT 'booking' AS kind, booking_id AS id, status, check_in, check_out
            FROM bookings
            WHERE agency_id = ? AND unit_id = ? AND status IN ({})
              AND check_in < ? AND ? < check_out
            ORDER BY check_in, id
            """.format(
                ", ".join("?" for _ in OCCUPYING_HOLD_STATES),
                ", ".join("?" for _ in OCCUPYING_BOOKING_STATES),
            ),
            (
                self.agency_id,
                unit_id,
                *OCCUPYING_HOLD_STATES,
                end,
                start,
                self.agency_id,
                unit_id,
                *OCCUPYING_BOOKING_STATES,
                end,
                start,
            ),
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
        """Recompute the audit chain using the chain's own verifier.

        Delegating (rather than re-hashing here) is what keeps this answer equal to
        the one :mod:`audit` gives. Two verifiers with two recipes disagree
        eventually, and the disagreement looks exactly like tampering.
        """

        try:
            AuditLog(self.conn, clock=self.clock, ids=self.ids).verify()
        except DomainError:
            return False
        return True

    # -- internals ---------------------------------------------------------- #
    def _now(self) -> str:
        return to_iso(self.clock.now())

    def _require_hold(self, conn: sqlite3.Connection, hold_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM holds WHERE hold_id = ? AND agency_id = ?",
            (hold_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise DomainError(f"unknown hold: {hold_id}", code="HOLD_NOT_FOUND", hold_id=hold_id)
        return row

    def _require_policy(self, conn: sqlite3.Connection, policy_version_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM policy_versions WHERE policy_version_id = ?", (policy_version_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - the FK guarantees this
            raise DomainError("hold references an unknown policy", code="POLICY_NOT_FOUND")
        return row

    def _require_extension(self, conn: sqlite3.Connection, extension_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM hold_extension_requests WHERE extension_id = ? AND agency_id = ?",
            (extension_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                f"unknown extension request: {extension_id}",
                code="EXTENSION_NOT_FOUND",
                extension_id=extension_id,
            )
        return row

    def _authoritative_evidence(
        self, conn: sqlite3.Connection, hold: sqlite3.Row, evidence_id: str
    ) -> sqlite3.Row:
        """The evidence §7 demands before an expiry may be treated as a release.

        "Authoritative integration" is level A (``property_api``/``booking_engine``);
        a written note or an iCal feed is not. The row must also be about the same
        unit, carry the confirm capability and still be fresh for its level — all of
        which the availability gate already decides, so this asks the gate rather
        than restating its rules in a second place.
        """
        row = conn.execute(
            "SELECT * FROM availability_evidence WHERE evidence_id = ? AND agency_id = ?",
            (evidence_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                f"unknown availability evidence: {evidence_id}",
                code="EVIDENCE_NOT_FOUND",
                evidence_id=evidence_id,
            )
        if row["unit_id"] != hold["unit_id"]:
            raise DomainError(
                "the verification evidence is about a different unit",
                code="HOLD_VERIFICATION_UNIT_MISMATCH",
                evidence_unit=row["unit_id"],
                unit_id=hold["unit_id"],
            )
        if row["evidence_level"] != "A":
            raise DomainError(
                "an expiry may only be verified by an authoritative source",
                code="HOLD_VERIFICATION_REQUIRES_AUTHORITATIVE_EVIDENCE",
                evidence_level=row["evidence_level"],
            )
        decision = gate_availability(
            # ``from_row`` takes a mapping; a sqlite3.Row is not one.
            AvailabilityEvidence.from_row(dict(row)),
            purpose=PURPOSE_CONFIRM,
            now=self._now(),
        )
        if decision.denied:
            raise DomainError(
                "the verification evidence is not usable",
                code=(
                    "HOLD_VERIFICATION_EVIDENCE_STALE"
                    if decision.code == CODE_EVIDENCE_STALE
                    else "HOLD_VERIFICATION_EVIDENCE_UNUSABLE"
                ),
                gate_code=decision.code,
                gate_message=decision.message,
            )
        return row

    def _transition_hold(
        self,
        conn: sqlite3.Connection,
        hold_id: str,
        to_state: str,
        *,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
        evidence_ref: Optional[str] = None,
        action: Optional[str] = None,
        at: Optional[str] = None,
    ) -> str:
        """Move a hold to ``to_state``, refusing any edge §7 does not allow.

        The single writer for hold state changes: it validates the edge against the
        canonical vocabulary, sets ``closed_at`` exactly for the closed states, and
        records the before/after pair in the same transaction. Leaving an unverified
        expiry is the one edge that additionally needs its evidence (§7).
        """
        hold = self._require_hold(conn, hold_id)
        current = hold["status"]
        if current == to_state:
            return hold_id  # already in the requested state: idempotent
        if not can_transition(current, to_state):
            raise DomainError(
                f"a hold cannot go from {current} to {to_state}",
                code="HOLD_ILLEGAL_TRANSITION",
                hold_id=hold_id,
                status=current,
                target=to_state,
            )
        if current == HOLD_EXPIRED_PENDING_VERIFICATION:
            if not evidence_ref:
                raise DomainError(
                    "closing an unverified expiry needs the evidence that verifies it",
                    code="HOLD_VERIFICATION_REQUIRES_AUTHORITATIVE_EVIDENCE",
                    hold_id=hold_id,
                    status=current,
                )
            self._authoritative_evidence(conn, hold, evidence_ref)
        moment = at or self._now()
        closed = to_state in CLOSED_HOLD_STATES
        closed_at = moment if closed else None
        close_reason = reason if closed else None
        conn.execute(
            "UPDATE holds SET status = ?, closed_at = ?, close_reason = ? WHERE hold_id = ?",
            (to_state, closed_at, close_reason, hold_id),
        )
        self._audit(
            conn,
            action=action or f"hold.{to_state.lower()}",
            entity_type="hold",
            entity_id=hold_id,
            before={"status": current, "expires_at": hold["expires_at"]},
            after={"status": to_state, "closed_at": closed_at, "close_reason": close_reason},
            reason=reason,
            policy_version_id=hold["policy_version_id"],
            evidence_ref=evidence_ref or hold["evidence_id"],
            actor=actor,
        )
        return hold_id

    def _require_booking(self, conn: sqlite3.Connection, booking_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM bookings WHERE booking_id = ? AND agency_id = ?",
            (booking_id, self.agency_id),
        ).fetchone()
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
                evidence_id, agency_id, unit_id, evidence_level, source_type, source_ref,
                captured_at, valid_until, result, occupancy_ranges,
                can_quote, can_hold, can_confirm, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                self.agency_id,
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
        hold_placeholders = ", ".join("?" for _ in OCCUPYING_HOLD_STATES)
        params: list[Any] = [self.agency_id, unit_id, *OCCUPYING_HOLD_STATES, check_out, check_in]
        sql = f"""
            SELECT hold_id, check_in, check_out FROM holds
            WHERE agency_id = ? AND unit_id = ? AND status IN ({hold_placeholders})
              AND check_in < ? AND ? < check_out
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
        params: list[Any] = [self.agency_id, unit_id, *OCCUPYING_BOOKING_STATES, check_out, check_in]
        sql = f"""
            SELECT booking_id, check_in, check_out FROM bookings
            WHERE agency_id = ? AND unit_id = ? AND status IN ({placeholders}) AND check_in < ? AND ? < check_out
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

    # -- availability checks: internals ------------------------------------- #
    def _require_tool_authority(
        self, actor: str, actor_role: str, principal_id: Optional[str], *, tool: str
    ) -> None:
        """§56: a machine actor may act on a medium-risk tool only under policy control.

        The rule itself lives in ``ai_service.require_tool_authority`` — one
        implementation for every gated path, sitting beside the authority it enforces,
        so a second path cannot inherit a weaker copy of it or forget the principal
        requirement.
        """
        require_tool_authority(
            self.conn,
            actor,
            actor_role,
            principal_id,
            tool=tool,
            clock=self.clock,
            ids=self.ids,
            agency_id=self.agency_id,
        )

    def _move_check(
        self,
        check_id: str,
        *,
        to_state: str,
        action: str,
        actor: str,
        actor_role: str,
        note: Optional[str] = None,
    ) -> CheckOutcome:
        """The forward move that needs no evidence; the rules live in ``_transition_check``."""
        check_id = require_ref(check_id, "check_id")
        actor = require_ref(actor, "actor", max_len=64)
        actor_role = require_ref(actor_role, "actor_role", max_len=64)
        if note is not None:
            note = require_text(note, "note", max_len=500)
        with self.transaction() as conn:
            row = self._check_row(conn, check_id)
            before = self._current_check_state(conn, check_id)
            self._transition_check(
                conn,
                check_id=check_id,
                to_state=to_state,
                actor=actor,
                actor_role=actor_role,
                note=note,
            )
            self._audit(
                conn,
                action=action,
                entity_type="availability_check",
                entity_id=check_id,
                before={"state": before},
                after={"state": to_state},
                reason=None,
                policy_version_id=None,
                evidence_ref=None,
                actor=actor,
            )
            return self._check_outcome(conn, row, replayed=False)

    def _transition_check(
        self,
        conn: sqlite3.Connection,
        *,
        check_id: str,
        to_state: str,
        actor: str,
        actor_role: str,
        evidence_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> str:
        """The one place a check changes state.

        Every caller goes through here, so the transition map cannot be bypassed: a
        second writer would be a second set of rules, and the two eventually disagree.
        """
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM availability_check_events"
            " WHERE check_id = ?",
            (check_id,),
        ).fetchone()
        seq = int(row["last_seq"]) + 1
        if seq == 1:
            if to_state != "CHECK_REQUESTED":
                raise DomainError(
                    "a check's first event is its request",
                    code="CHECK_INVALID_TRANSITION",
                    from_state=None,
                    to_state=to_state,
                    check_id=check_id,
                )
        else:
            current = self._current_check_state(conn, check_id)
            if not check_transition_allowed(current, to_state):
                raise DomainError(
                    f"a check in {current} cannot become {to_state}",
                    code=(
                        "CHECK_ALREADY_ANSWERED"
                        if current == ANSWERED_STATE and to_state == ANSWERED_STATE
                        else "CHECK_INVALID_TRANSITION"
                    ),
                    from_state=current,
                    to_state=to_state,
                    check_id=check_id,
                )
        event_id = self.ids.new("cke")
        conn.execute(
            """
            INSERT INTO availability_check_events (
                check_event_id, check_id, seq, state, actor, actor_role, evidence_id,
                note, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                check_id,
                seq,
                to_state,
                actor,
                actor_role,
                evidence_id,
                note,
                self._now(),
            ),
        )
        return event_id

    def _current_check_state(self, conn: sqlite3.Connection, check_id: str) -> str:
        """The newest event's state: the last line of a history is what is true now."""
        row = conn.execute(
            "SELECT state FROM availability_check_events WHERE check_id = ?"
            " ORDER BY seq DESC LIMIT 1",
            (check_id,),
        ).fetchone()
        return row["state"] if row is not None else ""

    def _check_row(self, conn: sqlite3.Connection, check_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM availability_checks WHERE check_id = ? AND agency_id = ?",
            (check_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                f"unknown availability check: {check_id}",
                code="CHECK_NOT_FOUND",
                check_id=check_id,
            )
        return row

    def _check_outcome(
        self, conn: sqlite3.Connection, row: sqlite3.Row, *, replayed: bool
    ) -> CheckOutcome:
        answer = conn.execute(
            "SELECT evidence_id FROM availability_check_events WHERE check_id = ?"
            " AND state = ? ORDER BY seq DESC LIMIT 1",
            (row["check_id"], ANSWERED_STATE),
        ).fetchone()
        return CheckOutcome(
            check_id=row["check_id"],
            unit_id=row["unit_id"],
            check_in=row["check_in"],
            check_out=row["check_out"],
            state=self._current_check_state(conn, row["check_id"]),
            requested_at=row["requested_at"],
            evidence_id=answer["evidence_id"] if answer is not None else None,
            replayed=replayed,
        )

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
            "SELECT * FROM idempotency_keys WHERE idempotency_key = ? AND agency_id = ?",
            (idempotency_key, self.agency_id),
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
                idempotency_key, agency_id, operation, request_hash, result_ref,
                result_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                self.agency_id,
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
        """Append one audit row through the chain's single implementation.

        The kernel writes the state change and its audit row in one transaction,
        so it calls :func:`audit.append_event` directly instead of opening a
        nested transaction. It must not hash the row itself: a second hash recipe
        produces rows that the audit module then reports as tampered.
        """

        event = append_event(
            conn,
            clock=self.clock,
            ids=self.ids,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor or self.actor,
            actor_role=self.actor_role,
            before=dict(before) if before is not None else None,
            after=dict(after) if after is not None else None,
            reason=reason,
            policy_version_id=policy_version_id,
            evidence_ref=evidence_ref,
        )
        return event.audit_event_id

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
