"""Explainable unit matching (Phase 2).

Read-only ranking of ``units`` for one case, written down as an append-only
ledger (one ``match_runs`` row + one ``match_candidates`` row per unit).

Why this module is shaped the way it is
---------------------------------------
* **Eligibility first, score second.** Every unit is first put through the hard
  gates (property sellability, unit status, area, capacity, minimum stay, date
  availability). A candidate that fails a gate is never scored and never ranked
  above an eligible one; it carries at least one coded ``deny_reason`` — the
  schema's ``ck_match_candidate_deny`` CHECK enforces the same rule.
* **Availability is the kernel's call, never this module's.** Date availability
  comes from :meth:`reservation_kernel.ReservationKernel.occupancy`; matching
  never queries ``holds``/``bookings`` itself (asserted by a test on this
  module's source), and the blocking ids it saw are copied into
  ``evidence_refs`` so a human can replay the decision.
* **No score without a reason.** ``score_centi`` is the integer sum of the
  integer weights of the positive reasons. There is no float anywhere on the
  scoring path. When the *basis* for a dimension is missing (no budget fact, no
  rate card covering the stay, no bedroom count requested, …) the dimension is
  reported as ``UNKNOWN`` and simply not scored — nothing is assumed.
* **Deterministic.** Same facts + same database + same injected clock/ids ⇒
  byte-identical decision payload (:meth:`MatchResult.to_dict`). Ties in score
  break on ascending ``unit_id``. ``now``/``clock``/``ids`` are injected: this
  module never reads the wall clock or a random source on its own.

Fail-closed
-----------
``MissingFacts`` (a required fact is absent — the keys intake marks required),
``InvalidCriteria`` (``check_out <= check_in``, a non-integer guest/budget
value, a float), ``NoInventory`` (no unit exists to consider). None of them
write a single row: a failed match is not a match run.

Scope note
----------
Matching is advisory: it ranks and explains. It does not authorise a hold — the
write path (``reservation_kernel`` + ``gates``) re-checks unit status, contract
pack and availability evidence before anything is locked, so a stale match run
can never become a booking on its own.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Optional

from intake import IntakeService, validate_facts
from primitives import (
    DomainError,
    IdFactory,
    canonical_json,
    fingerprint,
    nights_between,
    parse_iso,
    parse_iso_date,
    require_ref,
    to_iso,
    to_iso_date,
)
from quote_calculator import RateCard, Season

__all__ = [
    "ENGINE_VERSION",
    "UNKNOWN",
    "REQUIRED_FACT_KEYS",
    "POSITIVE_REASON_CODES",
    "DENY_REASON_CODES",
    "REASON_WEIGHTS",
    "CODE_MISSING_FACTS",
    "CODE_INVALID_CRITERIA",
    "CODE_NO_INVENTORY",
    "CODE_MATCH_RUN_NOT_FOUND",
    "CODE_KERNEL_REQUIRED",
    "CODE_DANGLING_EVIDENCE",
    "MatchingError",
    "MissingFacts",
    "InvalidCriteria",
    "NoInventory",
    "MatchRunNotFound",
    "MatchCandidate",
    "MatchResult",
    "MatchExplanation",
    "match",
    "explain",
]

#: Bumped whenever the gate set, the weights or the criteria shape change.
ENGINE_VERSION = "matching-1"

#: Marker written down instead of a guess (budget basis, min stay basis, …).
UNKNOWN = "UNKNOWN"

#: Who the ledger row says produced it. Callers stay out of the writer's name.
DEFAULT_ACTOR = "system:matching"

#: Facts that must be present and non-empty, mirroring ``intake.REQUIRED_FIELDS``.
REQUIRED_FACT_KEYS: tuple[str, ...] = ("check_in", "check_out", "adults")

#: Positive reasons, in canonical order. Weights live in :data:`REASON_WEIGHTS`.
POSITIVE_REASON_CODES: tuple[str, ...] = (
    "CAPACITY_FIT",
    "BEDROOM_FIT",
    "AREA_MATCH",
    "BUDGET_WITHIN",
    "AMENITY_MATCH",
    "AVAILABLE",
    "MIN_STAY_FIT",
)

#: Refusals, in canonical order. Every ineligible candidate carries >= 1 of them.
DENY_REASON_CODES: tuple[str, ...] = (
    "DATES_UNAVAILABLE",
    "CAPACITY_EXCEEDED",
    "MIN_STAY_NOT_MET",
    "OUT_OF_SERVICE",
    "PROPERTY_NOT_SELLABLE",
    "AREA_MISMATCH",
    "MAINTENANCE_BLOCK",
)

#: Integer weights, in centi. Equal scores are expected; the tie-break is
#: ``unit_id`` (see :func:`match`), never the weight table.
REASON_WEIGHTS: Mapping[str, int] = {
    "CAPACITY_FIT": 30,
    "BEDROOM_FIT": 20,
    "AREA_MATCH": 25,
    "BUDGET_WITHIN": 25,
    "AMENITY_MATCH": 15,
    "AVAILABLE": 10,
    "MIN_STAY_FIT": 10,
}

CODE_MISSING_FACTS = "MISSING_FACTS"
CODE_INVALID_CRITERIA = "INVALID_CRITERIA"
CODE_NO_INVENTORY = "NO_INVENTORY"
CODE_MATCH_RUN_NOT_FOUND = "MATCH_RUN_NOT_FOUND"
CODE_KERNEL_REQUIRED = "MATCH_KERNEL_REQUIRED"
CODE_DANGLING_EVIDENCE = "MATCH_EVIDENCE_MISSING"
CODE_SCORE_INVARIANT = "MATCH_SCORE_INVARIANT"

#: Where each input came from — recorded in ``criteria`` so a run is auditable.
PROVENANCE: Mapping[str, str] = {
    "availability": "reservation_kernel.occupancy",
    "facts": "intake.accepted_facts",
    "inventory": "units",
    "policy": "policy_versions",
    "rate": "rate_cards+rate_seasons",
}

_INTAKE_DOMAIN = "villa-agency-os:matching:intake"


# --------------------------------------------------------------------------- #
# Errors — one base class, explicit UPPERCASE codes (never parse the message)
# --------------------------------------------------------------------------- #
class MatchingError(DomainError):
    """Base class for every deliberate matching failure."""


class MissingFacts(MatchingError):
    """A required fact is absent. Nothing is assumed, nothing is written."""

    code = CODE_MISSING_FACTS


class InvalidCriteria(MatchingError):
    """The criteria cannot be turned into a stay (inverted dates, bad number)."""

    code = CODE_INVALID_CRITERIA


class NoInventory(MatchingError):
    """There is no unit to consider at all."""

    code = CODE_NO_INVENTORY


class MatchRunNotFound(MatchingError):
    """``explain`` was asked about a run that does not exist."""

    code = CODE_MATCH_RUN_NOT_FOUND


# --------------------------------------------------------------------------- #
# Result objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MatchCandidate:
    """One considered unit, its verdict, its score and its evidence.

    Read-only and mapping-friendly (``c.unit_id`` and ``c["unit_id"]`` both
    work) so agents can consume it without importing dataclasses.
    """

    unit_id: str
    property_id: str
    rank: int
    eligible: bool
    score_centi: int
    reasons: tuple[Mapping[str, Any], ...] = ()
    deny_reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    notes: tuple[Mapping[str, Any], ...] = ()
    review_required: bool = False
    basis: Mapping[str, Any] = field(default_factory=dict)
    unit_name: str = ""
    property_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "unit_id": self.unit_id,
                "property_id": self.property_id,
                "rank": self.rank,
                "eligible": self.eligible,
                "score_centi": self.score_centi,
                "reasons": [dict(r) for r in self.reasons],
                "deny_reasons": list(self.deny_reasons),
                "evidence_refs": list(self.evidence_refs),
                "unknowns": list(self.unknowns),
                "notes": [dict(n) for n in self.notes],
                "review_required": self.review_required,
                "basis": dict(self.basis),
                "unit_name": self.unit_name,
                "property_name": self.property_name,
            }
        )

    # -- mapping sugar ------------------------------------------------------ #
    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> tuple[str, ...]:
        return tuple(self.to_dict())

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def __len__(self) -> int:
        return len(self.to_dict())

    def reason_codes(self) -> tuple[str, ...]:
        return tuple(str(r["code"]) for r in self.reasons)

    def short(self) -> str:
        verdict = "ELIGIBLE" if self.eligible else "DENIED"
        tail = f" score={self.score_centi}" if self.eligible else " score=0"
        return f"#{self.rank} [{verdict}{tail}] {self.unit_id}"


@dataclass(frozen=True)
class MatchResult:
    """The outcome of one match run (also the shape persisted in the ledger)."""

    match_run_id: str
    case_id: str
    engine_version: str
    intake_hash: str
    criteria: Mapping[str, Any]
    considered_count: int
    eligible_count: int
    candidates: tuple[MatchCandidate, ...]
    created_at: str
    intake_snapshot: str = ""

    # -- serialisation ------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        """The **decision payload** — volatile ledger identity excluded.

        ``match_run_id``/``created_at`` are necessarily different for every run
        (append-only ledger), so they are not part of the decision. Two runs over
        identical facts and an identical database therefore serialize
        byte-for-byte identically, which is what determinism means here. Use
        :meth:`to_record` (or ``result["match_run_id"]``) for the ledger row.
        """
        return _json_safe(
            {
                "case_id": self.case_id,
                "engine_version": self.engine_version,
                "intake_hash": self.intake_hash,
                "criteria": dict(self.criteria),
                "considered_count": self.considered_count,
                "eligible_count": self.eligible_count,
                "candidates": [c.to_dict() for c in self.candidates],
            }
        )

    #: Alias kept for callers that prefer the repo's ``as_dict`` naming.
    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def to_record(self) -> dict[str, Any]:
        """Everything, including the ledger identity."""
        record = {
            "match_run_id": self.match_run_id,
            "created_at": self.created_at,
            "intake_snapshot": self.intake_snapshot,
        }
        record.update(self.to_dict())
        return record

    def canonical(self) -> str:
        """Canonical JSON of the decision payload (byte-comparable)."""
        return canonical_json(self.to_dict())

    def fingerprint(self) -> str:
        return fingerprint(self.to_dict(), domain="villa-agency-os:matching:run")

    def __getitem__(self, key: str) -> Any:
        return self.to_record()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_record().get(key, default)

    def keys(self) -> tuple[str, ...]:
        return tuple(self.to_record())

    # -- accessors ---------------------------------------------------------- #
    @property
    def eligible_candidates(self) -> tuple[MatchCandidate, ...]:
        return tuple(c for c in self.candidates if c.eligible)

    @property
    def denied_candidates(self) -> tuple[MatchCandidate, ...]:
        return tuple(c for c in self.candidates if not c.eligible)

    def candidate_for(self, unit_id: str) -> MatchCandidate:
        for candidate in self.candidates:
            if candidate.unit_id == unit_id:
                return candidate
        raise MatchingError(
            f"unit {unit_id!r} is not a candidate of {self.match_run_id}",
            code="MATCH_CANDIDATE_NOT_FOUND",
            unit_id=unit_id,
            match_run_id=self.match_run_id,
        )

    def denied_codes(self, unit_id: str) -> tuple[str, ...]:
        return self.candidate_for(unit_id).deny_reasons


class MatchExplanation(str):
    """A human-readable explanation that is *also* a ``str``.

    ``str`` so ``print(explain(conn, run_id))`` and ``"CAPACITY_FIT" in
    explain(...)`` work; carry the structured parts as attributes so an agent
    can still read ``.candidates`` / ``.evidence_refs``.
    """

    match_run_id: str
    case_id: str
    engine_version: str
    criteria: Mapping[str, Any]
    candidates: tuple[Mapping[str, Any], ...]
    evidence_refs: tuple[Mapping[str, Any], ...]
    considered_count: int
    eligible_count: int

    def __new__(cls, text: str, **parts: Any) -> "MatchExplanation":
        obj = super().__new__(cls, text)
        obj.match_run_id = parts.get("match_run_id", "")
        obj.case_id = parts.get("case_id", "")
        obj.engine_version = parts.get("engine_version", "")
        obj.criteria = parts.get("criteria", {})
        obj.candidates = parts.get("candidates", ())
        obj.evidence_refs = parts.get("evidence_refs", ())
        obj.considered_count = parts.get("considered_count", 0)
        obj.eligible_count = parts.get("eligible_count", 0)
        return obj

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_run_id": self.match_run_id,
            "case_id": self.case_id,
            "engine_version": self.engine_version,
            "criteria": dict(self.criteria),
            "considered_count": self.considered_count,
            "eligible_count": self.eligible_count,
            "candidates": [dict(c) for c in self.candidates],
            "evidence_refs": [dict(e) for e in self.evidence_refs],
            "text": str(self),
        }

    def __getitem__(self, key: Any) -> Any:  # type: ignore[override]
        if isinstance(key, str):
            return self.as_dict()[key]
        return super().__getitem__(key)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction, taking the write lock before it reads.

    Joins the caller's transaction instead of nesting one (SQLite has no nested
    transactions): a caller that already opened ``BEGIN IMMEDIATE`` stays in
    charge of commit/rollback.
    """
    if getattr(conn, "in_transaction", False):
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _resolve_moment(now: Any, clock: Any) -> datetime:
    """The one moment a run is dated with. Injected, never implicit."""
    if now is None:
        if clock is None:
            raise InvalidCriteria(
                "now or clock is required: matching never reads the wall clock",
                code=CODE_INVALID_CRITERIA,
                field="now",
            )
        moment = clock.now()
    elif isinstance(now, datetime):
        moment = now
    else:
        try:
            moment = parse_iso(str(now))
        except Exception as exc:  # noqa: BLE001 - re-raised as a domain error
            raise InvalidCriteria(
                "now must be an ISO-8601 timestamp with a timezone",
                code=CODE_INVALID_CRITERIA,
                field="now",
                value=repr(now),
            ) from exc
    if moment.tzinfo is None:
        raise InvalidCriteria(
            "now must be timezone-aware",
            code=CODE_INVALID_CRITERIA,
            field="now",
            value=repr(now),
        )
    return moment


def _jsonable(value: Any, *, field_name: str) -> Any:
    """Facts are JSON-able primitives only — floats are refused, not rounded."""
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, str):
        return value
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return to_iso_date(value)
    if isinstance(value, Mapping):
        return {
            str(k): _jsonable(v, field_name=f"{field_name}.{k}")
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [_jsonable(v, field_name=field_name) for v in items]
    raise InvalidCriteria(
        f"fakta {field_name!r} bertipe {type(value).__name__}: hanya nilai bulat/teks/tanggal",
        code=CODE_INVALID_CRITERIA,
        field=field_name,
        got=type(value).__name__,
    )


def _coerce_facts(conn: sqlite3.Connection, case_id: str, facts: Any, *, clock: Any, ids: Any) -> dict[str, Any]:
    """Normalise the caller's facts into a plain dict.

    Accepts a mapping, an iterable of ``{field_name, value}`` rows/objects, or
    ``None`` — in which case the ACCEPTED intake facts of the case are loaded
    (``intake.IntakeService.accepted_facts``), which is the production path.
    """
    if facts is None:
        return dict(IntakeService(conn, clock=clock, ids=ids).accepted_facts(case_id))
    if isinstance(facts, Mapping):
        return {str(k): v for k, v in facts.items()}
    if hasattr(facts, "facts") and isinstance(getattr(facts, "facts"), Mapping):
        return {str(k): v for k, v in getattr(facts, "facts").items()}
    if isinstance(facts, Iterable):
        collected: dict[str, Any] = {}
        for item in facts:
            if isinstance(item, Mapping):
                name, value = item.get("field_name"), item.get("value")
            else:
                name, value = getattr(item, "field_name", None), getattr(item, "value", None)
            if name is None:
                raise InvalidCriteria(
                    "fakta harus Mapping atau baris ber-field_name",
                    code=CODE_INVALID_CRITERIA,
                    field="facts",
                    got=type(item).__name__,
                )
            collected[str(name)] = value
        return collected
    raise InvalidCriteria(
        "facts harus Mapping (atau None untuk membaca fakta intake)",
        code=CODE_INVALID_CRITERIA,
        field="facts",
        got=type(facts).__name__,
    )


def _present(facts: Mapping[str, Any], key: str) -> bool:
    return key in facts and facts[key] is not None


def _require_facts(facts: Mapping[str, Any]) -> None:
    missing = tuple(name for name in REQUIRED_FACT_KEYS if not _present(facts, name))
    if missing:
        raise MissingFacts(
            "fakta wajib belum lengkap: " + ", ".join(missing),
            code=CODE_MISSING_FACTS,
            missing=missing,
            case_required=REQUIRED_FACT_KEYS,
        )


def _normalised_facts(
    facts: Mapping[str, Any],
    *,
    check_in: date,
    check_out: date,
    adults: int,
) -> dict[str, Any]:
    """Facts in the Python types ``intake.validate_facts`` expects.

    The validator speaks typed values (dates, ints), while callers may hand us
    ISO strings — normalise first, then let the repo's own cross-field rules
    (inverted stay, non-positive guests/budget, bad currency) have their say.
    """
    normalised: dict[str, Any] = dict(facts)
    normalised["check_in"] = check_in
    normalised["check_out"] = check_out
    normalised["adults"] = adults
    for key in ("children", "infants", "budget_minor", "bedrooms"):
        if _present(normalised, key):
            normalised[key] = _as_int(normalised[key], field_name=key)
    return normalised


def _as_date(value: Any, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return parse_iso_date(value.strip())
        except Exception as exc:  # noqa: BLE001 - re-raised as a domain error
            raise InvalidCriteria(
                f"fakta {field_name!r} bukan tanggal ISO-8601",
                code=CODE_INVALID_CRITERIA,
                field=field_name,
                value=value,
            ) from exc
    raise InvalidCriteria(
        f"fakta {field_name!r} harus tanggal ISO-8601",
        code=CODE_INVALID_CRITERIA,
        field=field_name,
        got=type(value).__name__,
    )


def _as_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise InvalidCriteria(
            f"fakta {field_name!r} harus bilangan bulat, bukan boolean",
            code=CODE_INVALID_CRITERIA,
            field=field_name,
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise InvalidCriteria(
        f"fakta {field_name!r} harus bilangan bulat (float dilarang)",
        code=CODE_INVALID_CRITERIA,
        field=field_name,
        got=type(value).__name__,
    )


def _optional_int(facts: Mapping[str, Any], key: str) -> Optional[int]:
    if not _present(facts, key):
        return None
    return _as_int(facts[key], field_name=key)


def _optional_text(facts: Mapping[str, Any], key: str) -> Optional[str]:
    if not _present(facts, key):
        return None
    value = facts[key]
    if not isinstance(value, str):
        raise InvalidCriteria(
            f"fakta {key!r} harus teks",
            code=CODE_INVALID_CRITERIA,
            field=key,
            got=type(value).__name__,
        )
    text = value.strip()
    return text or None


def _amenities(facts: Mapping[str, Any]) -> tuple[str, ...]:
    """Requested amenities as a sorted, casefolded tuple (empty = not asked)."""
    if not _present(facts, "amenities"):
        return ()
    raw = facts["amenities"]
    if isinstance(raw, str):
        tokens = [part.strip() for part in raw.replace(";", ",").split(",")]
    elif isinstance(raw, Iterable):
        tokens = [str(part).strip() for part in raw]
    else:
        raise InvalidCriteria(
            "fakta 'amenities' harus teks atau daftar",
            code=CODE_INVALID_CRITERIA,
            field="amenities",
            got=type(raw).__name__,
        )
    return tuple(sorted({token.casefold() for token in tokens if token}))


def _json_safe(obj: Any) -> Any:
    """Round-trip through the canonical encoder so callers get plain JSON data.

    ``canonical_json`` already knows how to render ``date``/``Decimal``/objects
    with ``to_dict``; decoding its output gives a structure that any
    ``json.dumps`` accepts — no caller has to know about our Python types.
    """
    return json.loads(canonical_json(obj))


def _norm_text(value: Any) -> str:
    return " ".join(str(value).casefold().split())


def _reason_order(code: str) -> int:
    """Canonical ordinal of a positive reason code (unknown codes sort last)."""
    try:
        return POSITIVE_REASON_CODES.index(code)
    except ValueError:
        return len(POSITIVE_REASON_CODES)


def _area_matches(preference: str, region: str) -> bool:
    """Area preference vs ``properties.region``: normalised, forgiving call.

    A preference ("ubud") matches the region ("Ubud") or a region that contains
    it ("Ubud, Gianyar"). Anything else is a mismatch — never a guess.
    """
    wanted, have = _norm_text(preference), _norm_text(region)
    if not wanted or not have:
        return False
    return wanted == have or wanted in have or have in wanted


# --------------------------------------------------------------------------- #
# Inventory / sources (read-only)
# --------------------------------------------------------------------------- #
_INVENTORY_SQL = """
    SELECT u.unit_id            AS unit_id,
           u.property_id        AS property_id,
           u.name               AS unit_name,
           u.unit_type          AS unit_type,
           u.bedrooms           AS bedrooms,
           u.base_capacity      AS base_capacity,
           u.max_guests         AS max_guests,
           u.status             AS unit_status,
           p.name               AS property_name,
           p.region             AS region,
           p.province           AS province,
           p.property_type      AS property_type,
           p.status             AS property_status,
           p.contract_pack_status AS contract_pack_status
    FROM units u
    JOIN properties p ON p.property_id = u.property_id
    ORDER BY u.unit_id
"""


def _load_inventory(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    """Every unit that exists, in ascending ``unit_id`` (deterministic order)."""
    rows = conn.execute(_INVENTORY_SQL).fetchall()
    return tuple(dict(row) for row in rows)


def _policy_for(conn: sqlite3.Connection, unit: Mapping[str, Any], check_in: str, check_out: str) -> Optional[dict[str, Any]]:
    """The effective policy version covering the whole stay, if one exists.

    The ``policy_versions`` table carries no unit column (``unit_scope`` is
    ``property``/``unit``), so a unit-scoped version is treated as a refinement
    of the property-wide one and wins; then the newest version wins.
    """
    row = conn.execute(
        """
        SELECT policy_version_id, unit_scope, version, effective_from, effective_to,
               currency, payload
        FROM policy_versions
        WHERE property_id = ?
          AND unit_scope IN ('property', 'unit')
          AND effective_from <= ?
          AND ? <= effective_to
        ORDER BY (CASE WHEN unit_scope = 'unit' THEN 1 ELSE 0 END) DESC,
                 version DESC,
                 policy_version_id DESC
        LIMIT 1
        """,
        (unit["property_id"], check_in, check_out),
    ).fetchone()
    return None if row is None else dict(row)


def _rate_card_for(conn: sqlite3.Connection, unit: Mapping[str, Any], check_in: str, check_out: str) -> tuple[Optional[RateCard], Optional[str]]:
    """The rate card covering the whole stay, rebuilt from its dated rows.

    Returns ``(card, note_code)``: a card the rule set refused to build (e.g. a
    season outside the card window) yields ``(None, <code>)`` — the missing basis
    is written down instead of being guessed at.
    """
    header = conn.execute(
        """
        SELECT rate_card_id, property_id, unit_scope, name, version, effective_from,
               effective_to, currency, weekly_discount_pct, monthly_discount_pct,
               source_doc_ref, approved_by
        FROM rate_cards
        WHERE property_id = ?
          AND unit_scope IN ('property', 'unit')
          AND effective_from <= ?
          AND ? <= effective_to
        ORDER BY (CASE WHEN unit_scope = 'unit' THEN 1 ELSE 0 END) DESC,
                 version DESC,
                 rate_card_id DESC
        LIMIT 1
        """,
        (unit["property_id"], check_in, check_out),
    ).fetchone()
    if header is None:
        return None, None
    seasons = tuple(
        Season(
            season_id=str(row["season_id"]),
            name=str(row["name"]),
            start_date=str(row["start_date"]),
            end_date=str(row["end_date"]),
            nightly_rate_minor=int(row["nightly_rate_minor"]),
            min_stay_nights=int(row["min_stay_nights"]),
            priority=int(row["priority"]),
        )
        for row in conn.execute(
            """
            SELECT season_id, name, start_date, end_date, nightly_rate_minor,
                   min_stay_nights, priority
            FROM rate_seasons
            WHERE rate_card_id = ?
            ORDER BY start_date, end_date, priority, season_id
            """,
            (header["rate_card_id"],),
        ).fetchall()
    )
    try:
        card = RateCard(
            rate_card_id=str(header["rate_card_id"]),
            property_id=str(header["property_id"]),
            unit_scope=str(header["unit_scope"]),
            name=str(header["name"]),
            version=int(header["version"]),
            effective_from=str(header["effective_from"]),
            effective_to=str(header["effective_to"]),
            currency=str(header["currency"]),
            weekly_discount_pct=str(header["weekly_discount_pct"]),
            monthly_discount_pct=str(header["monthly_discount_pct"]),
            seasons=seasons,
            source_doc_ref=str(header["source_doc_ref"]),
            approved_by=str(header["approved_by"]),
        )
    except DomainError as exc:
        return None, str(exc.code)
    return card, None


def _stay_nights(check_in: date, check_out: date) -> tuple[str, ...]:
    return tuple(to_iso_date(check_in + timedelta(days=offset)) for offset in range(nights_between(check_in, check_out)))


def _policy_min_stay(policy: Mapping[str, Any]) -> tuple[Optional[int], Optional[str]]:
    """``min_stay_nights`` out of the policy payload, if it is usable."""
    try:
        payload = json.loads(policy["payload"])
    except (TypeError, ValueError):
        return None, "POLICY_PAYLOAD_UNREADABLE"
    if not isinstance(payload, Mapping):
        return None, "POLICY_PAYLOAD_UNREADABLE"
    value = payload.get("min_stay_nights")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None, "POLICY_MIN_STAY_INVALID"
    return int(value), None


def _policy_amenities(policy: Mapping[str, Any]) -> tuple[Optional[tuple[str, ...]], Optional[str]]:
    """Declared amenities from the policy payload, when a version declares them.

    The schema carries no amenity column anywhere, so the *declared* list can
    only come from the versioned business document. Absent → ``None``: matching
    then reports ``UNKNOWN`` rather than inventing a match.
    """
    try:
        payload = json.loads(policy["payload"])
    except (TypeError, ValueError):
        return None, "POLICY_PAYLOAD_UNREADABLE"
    if not isinstance(payload, Mapping) or "amenities" not in payload:
        return None, None
    raw = payload["amenities"]
    if isinstance(raw, str):
        tokens = [part.strip() for part in raw.replace(";", ",").split(",")]
    elif isinstance(raw, Iterable):
        tokens = [str(part).strip() for part in raw]
    else:
        return None, "POLICY_AMENITIES_INVALID"
    return tuple(sorted({token.casefold() for token in tokens if token})), None


def _accommodation_basis(card: RateCard, nights: Sequence[str]) -> int:
    """Accommodation subtotal: the undiscounted sum of the winning nightly rates.

    Deliberately the *gross* nightly sum (no length-of-stay discount): a budget
    test against it is conservative, and both sides stay integer minor units.
    """
    return sum(int(card.winning_season(night).nightly_rate_minor) for night in nights)


# --------------------------------------------------------------------------- #
# The candidate evaluation
# --------------------------------------------------------------------------- #
def _evaluate_unit(
    *,
    conn: sqlite3.Connection,
    kernel: Any,
    unit: Mapping[str, Any],
    criteria: Mapping[str, Any],
) -> MatchCandidate:
    check_in: date = parse_iso_date(str(criteria["check_in"]))
    check_out: date = parse_iso_date(str(criteria["check_out"]))
    nights: tuple[str, ...] = criteria["nights_list"]
    denies: list[str] = []
    notes: list[dict[str, Any]] = []
    unknowns: list[str] = []
    review_required = False

    # -- gate 1: is the property sellable at all? --------------------------- #
    property_status = str(unit["property_status"])
    if property_status in ("NOT_SELLABLE", "SUSPENDED"):
        denies.append("PROPERTY_NOT_SELLABLE")
    elif property_status == "SELLABLE_WITH_REVIEW":
        notes.append({"code": "PROPERTY_SELLABLE_WITH_REVIEW", "property_status": property_status})
        review_required = True
    if str(unit["contract_pack_status"]) != "ACTIVATED":
        notes.append(
            {
                "code": "CONTRACT_PACK_NOT_ACTIVATED",
                "contract_pack_status": str(unit["contract_pack_status"]),
            }
        )
        review_required = True

    # -- gate 2: may this unit be sold right now? --------------------------- #
    unit_status = str(unit["unit_status"])
    if unit_status == "SUSPENDED":
        denies.append("OUT_OF_SERVICE")
    elif unit_status == "MAINTENANCE":
        denies.append("MAINTENANCE_BLOCK")

    # -- gate 3: area ------------------------------------------------------- #
    area_preference = criteria["area_preference"]
    area_state = UNKNOWN
    if area_preference is None:
        unknowns.append("UNKNOWN_AREA_BASIS")
    elif _area_matches(area_preference, str(unit["region"])):
        area_state = "MATCH"
    else:
        area_state = "MISMATCH"
        denies.append("AREA_MISMATCH")

    # -- gate 4: capacity --------------------------------------------------- #
    capacity_ok = criteria["guests"] <= int(unit["max_guests"])
    if not capacity_ok:
        denies.append("CAPACITY_EXCEEDED")

    # -- gate 5: minimum stay ----------------------------------------------- #
    policy = _policy_for(conn, unit, to_iso_date(check_in), to_iso_date(check_out))
    min_stays: list[int] = []
    min_stay_sources: list[str] = []
    if policy is not None:
        policy_min, policy_note = _policy_min_stay(policy)
        if policy_min is None and policy_note:
            notes.append({"code": policy_note})
        if policy_min is not None:
            min_stays.append(policy_min)
            min_stay_sources.append(f"policy:{policy['policy_version_id']}")

    card, card_note = _rate_card_for(conn, unit, to_iso_date(check_in), to_iso_date(check_out))
    if card is None and card_note:
        notes.append({"code": card_note})
    accommodation: Optional[int] = None
    if card is not None:
        try:
            min_stays.append(int(card.min_stay_over(nights)))
            min_stay_sources.append(f"rate_card:{card.rate_card_id}")
            accommodation = _accommodation_basis(card, nights)
        except DomainError as exc:
            notes.append({"code": str(exc.code), "detail": dict(exc.details)})

    min_stay_state = UNKNOWN
    if not min_stays:
        unknowns.append("UNKNOWN_MIN_STAY_BASIS")
    else:
        required_min_stay = max(min_stays)
        if criteria["nights"] < required_min_stay:
            min_stay_state = "NOT_MET"
            denies.append("MIN_STAY_NOT_MET")
        else:
            min_stay_state = "FIT"

    # -- gate 6: date availability — the kernel's call, never ours ---------- #
    occupancy = tuple(kernel.occupancy(unit["unit_id"], start=to_iso_date(check_in), end=to_iso_date(check_out)))
    evidence_refs = tuple(dict.fromkeys(str(entry["id"]) for entry in occupancy))
    if occupancy:
        denies.append("DATES_UNAVAILABLE")

    # -- budget / bedroom / amenity bases (never assumed) ------------------- #
    budget_minor = criteria["budget_minor"]
    currency = criteria["currency"]
    budget_state = UNKNOWN
    if budget_minor is None:
        unknowns.append("UNKNOWN_BUDGET_CRITERIA")
    elif accommodation is None:
        unknowns.append("UNKNOWN_BUDGET_BASIS")
    elif currency is not None and card is not None and currency != card.currency:
        unknowns.append("UNKNOWN_BUDGET_CURRENCY")
        notes.append({"code": "BUDGET_CURRENCY_UNCOMPARABLE", "currency": currency, "rate_currency": card.currency})
    elif budget_minor >= accommodation:
        budget_state = "WITHIN"
    else:
        budget_state = "EXCEEDED"
        notes.append(
            {
                "code": "BUDGET_EXCEEDED",
                "budget_minor": budget_minor,
                "basis_minor": accommodation,
                "rate_card_id": card.rate_card_id if card is not None else None,
            }
        )

    bedrooms_requested = criteria["bedrooms"]
    bedroom_state = UNKNOWN
    if bedrooms_requested is None:
        unknowns.append("UNKNOWN_BEDROOM_BASIS")
    elif int(unit["bedrooms"]) >= bedrooms_requested:
        bedroom_state = "FIT"
    else:
        bedroom_state = "SHORTFALL"
        notes.append({"code": "BEDROOM_SHORTFALL", "requested": bedrooms_requested, "bedrooms": int(unit["bedrooms"])})

    requested_amenities = criteria["amenities"]
    amenity_state = "NOT_REQUESTED"
    if requested_amenities:
        declared, amenity_note = _policy_amenities(policy) if policy is not None else (None, None)
        if declared is None:
            amenity_state = UNKNOWN
            unknowns.append("UNKNOWN_AMENITY_BASIS")
            if amenity_note:
                notes.append({"code": amenity_note})
        else:
            missing = tuple(a for a in requested_amenities if a not in declared)
            if missing:
                amenity_state = "PARTIAL"
                notes.append({"code": "AMENITY_MISSING", "missing": list(missing)})
            else:
                amenity_state = "MATCH"

    basis = {
        "area": area_state,
        "bedrooms": bedroom_state,
        "budget": budget_state,
        "min_stay": min_stay_state,
        "amenities": amenity_state,
        "policy_version_id": policy["policy_version_id"] if policy is not None else None,
        "rate_card_id": card.rate_card_id if card is not None else None,
        "accommodation_minor": accommodation,
        "min_stay_required": max(min_stays) if min_stays else None,
        "min_stay_sources": list(min_stay_sources),
    }

    eligible = not denies
    reasons: list[dict[str, Any]] = []
    if eligible:
        # Eligibility already proved capacity, area and the stay length; every
        # reason below is one of the closed set in POSITIVE_REASON_CODES.
        reasons.append(
            {
                "code": "CAPACITY_FIT",
                "weight": REASON_WEIGHTS["CAPACITY_FIT"],
                "detail": {
                    "guests": criteria["guests"],
                    "max_guests": int(unit["max_guests"]),
                    "base_capacity": int(unit["base_capacity"]),
                },
            }
        )
        if bedroom_state == "FIT":
            reasons.append(
                {
                    "code": "BEDROOM_FIT",
                    "weight": REASON_WEIGHTS["BEDROOM_FIT"],
                    "detail": {"requested": bedrooms_requested, "bedrooms": int(unit["bedrooms"])},
                }
            )
        if area_state == "MATCH":
            reasons.append(
                {
                    "code": "AREA_MATCH",
                    "weight": REASON_WEIGHTS["AREA_MATCH"],
                    "detail": {"area_preference": area_preference, "region": str(unit["region"])},
                }
            )
        if budget_state == "WITHIN":
            reasons.append(
                {
                    "code": "BUDGET_WITHIN",
                    "weight": REASON_WEIGHTS["BUDGET_WITHIN"],
                    "detail": {
                        "budget_minor": budget_minor,
                        "basis_minor": accommodation,
                        "rate_card_id": card.rate_card_id if card is not None else None,
                    },
                }
            )
        if amenity_state == "MATCH":
            reasons.append(
                {
                    "code": "AMENITY_MATCH",
                    "weight": REASON_WEIGHTS["AMENITY_MATCH"],
                    "detail": {"amenities": list(requested_amenities)},
                }
            )
        reasons.append(
            {
                "code": "AVAILABLE",
                "weight": REASON_WEIGHTS["AVAILABLE"],
                "detail": {
                    "source": PROVENANCE["availability"],
                    "start": to_iso_date(check_in),
                    "end": to_iso_date(check_out),
                    "occupying": 0,
                },
            }
        )
        if min_stay_state == "FIT":
            reasons.append(
                {
                    "code": "MIN_STAY_FIT",
                    "weight": REASON_WEIGHTS["MIN_STAY_FIT"],
                    "detail": {"nights": criteria["nights"], "required": max(min_stays)},
                }
            )
        # The kernel's verdict *is* the AVAILABLE reason; the ids it handed back
        # (if any) ride along on the candidate as evidence_refs.

    reasons.sort(key=lambda item: (_reason_order(str(item["code"])), -int(item["weight"])))

    score_centi = 0
    if eligible:
        score_centi = int(sum(int(item["weight"]) for item in reasons))
    if isinstance(score_centi, bool) or not isinstance(score_centi, int):
        raise DomainError(
            "score_centi must be an integer (float forbidden on the scoring path)",
            code=CODE_SCORE_INVARIANT,
            score_centi=repr(score_centi),
        )
    if eligible and score_centi != sum(int(item["weight"]) for item in reasons):
        raise DomainError(
            "score_centi must equal the sum of the positive reason weights",
            code=CODE_SCORE_INVARIANT,
            score_centi=score_centi,
        )

    deny_reasons = tuple(sorted(set(denies), key=DENY_REASON_CODES.index))
    for code in deny_reasons:
        if code not in DENY_REASON_CODES:
            raise DomainError(
                f"unknown deny reason {code!r}",
                code=CODE_SCORE_INVARIANT,
                deny_reason=code,
            )

    return MatchCandidate(
        unit_id=str(unit["unit_id"]),
        property_id=str(unit["property_id"]),
        rank=0,  # assigned after ordering
        eligible=eligible,
        score_centi=score_centi,
        reasons=tuple(reasons),
        deny_reasons=deny_reasons,
        evidence_refs=evidence_refs,
        unknowns=tuple(sorted(set(unknowns))),
        notes=tuple(notes),
        review_required=review_required,
        basis=basis,
        unit_name=str(unit["unit_name"]),
        property_name=str(unit["property_name"]),
    )


def _order_candidates(candidates: Sequence[MatchCandidate]) -> tuple[MatchCandidate, ...]:
    """Eligible first (score desc, then ``unit_id`` asc); refusals last.

    An ineligible candidate can never outrank an eligible one — that is the
    "eligibility first, score second" rule, and the tie-break is the ascending
    ``unit_id`` so two identical scores always resolve the same way.
    """
    eligible = sorted(
        (c for c in candidates if c.eligible),
        key=lambda c: (-c.score_centi, c.unit_id),
    )
    denied = sorted(
        (c for c in candidates if not c.eligible),
        key=lambda c: (
            DENY_REASON_CODES.index(c.deny_reasons[0]) if c.deny_reasons else len(DENY_REASON_CODES),
            c.unit_id,
        ),
    )
    ordered: list[MatchCandidate] = []
    for position, candidate in enumerate(eligible + denied, start=1):
        ordered.append(replace(candidate, rank=position))
    return tuple(ordered)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def match(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    facts: Any,
    kernel: Any,
    now: Any,
    clock: Any = None,
    ids: Optional[IdFactory] = None,
) -> MatchResult:
    """Rank the units for ``case_id`` and write the run to the ledger.

    ``facts`` are the ACCEPTED intake facts (``intake.accepted_facts``); pass
    ``None`` to have them loaded from ``intake_fields``. ``kernel`` is the
    :class:`reservation_kernel.ReservationKernel` that owns availability — the
    only thing allowed to answer "are these dates free?". ``now`` dates the run
    (``clock`` is the fallback); ``ids`` is the injected
    :class:`primitives.IdFactory`.

    Returns a :class:`MatchResult` and appends exactly one ``match_runs`` row
    plus one ``match_candidates`` row per considered unit — append-only, in one
    transaction. Fail-closed: a missing required fact, an inverted stay or an
    empty inventory raises ``MissingFacts`` / ``InvalidCriteria`` /
    ``NoInventory`` and writes nothing.
    """
    case_id = require_ref(case_id, "case_id")
    if kernel is None:
        raise DomainError(
            "kernel is required: availability may only be decided by reservation_kernel",
            code=CODE_KERNEL_REQUIRED,
        )
    if not hasattr(kernel, "occupancy"):
        raise DomainError(
            "kernel must expose occupancy(unit_id, start=..., end=...)",
            code=CODE_KERNEL_REQUIRED,
            got=type(kernel).__name__,
        )
    id_factory = ids if ids is not None else IdFactory()
    moment = _resolve_moment(now, clock)

    fact_map = _coerce_facts(conn, case_id, facts, clock=clock, ids=id_factory)
    _require_facts(fact_map)

    check_in = _as_date(fact_map["check_in"], field_name="check_in")
    check_out = _as_date(fact_map["check_out"], field_name="check_out")
    adults = _as_int(fact_map["adults"], field_name="adults")
    if check_out <= check_in:
        raise InvalidCriteria(
            "check_out harus setelah check_in",
            code=CODE_INVALID_CRITERIA,
            check_in=to_iso_date(check_in),
            check_out=to_iso_date(check_out),
        )
    if adults < 1:
        raise InvalidCriteria(
            "adults minimal 1",
            code=CODE_INVALID_CRITERIA,
            adults=adults,
        )
    problems = validate_facts(_normalised_facts(fact_map, check_in=check_in, check_out=check_out, adults=adults))
    if problems:
        raise InvalidCriteria(
            "fakta tidak konsisten: " + ", ".join(problems),
            code=CODE_INVALID_CRITERIA,
            problems=problems,
        )

    children = _optional_int(fact_map, "children") or 0
    infants = _optional_int(fact_map, "infants") or 0
    guests = adults + children
    if children < 0 or infants < 0:
        raise InvalidCriteria(
            "children/infants tidak boleh negatif",
            code=CODE_INVALID_CRITERIA,
            children=children,
            infants=infants,
        )

    inventory = _load_inventory(conn)
    if not inventory:
        raise NoInventory(
            "tidak ada unit untuk dicocokkan",
            code=CODE_NO_INVENTORY,
            case_id=case_id,
        )

    criteria: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "check_in": to_iso_date(check_in),
        "check_out": to_iso_date(check_out),
        "nights": nights_between(check_in, check_out),
        "nights_list": _stay_nights(check_in, check_out),
        "adults": adults,
        "children": children,
        "infants": infants,
        "guests": guests,
        "area_preference": _optional_text(fact_map, "area_preference"),
        "budget_minor": _optional_int(fact_map, "budget_minor"),
        "currency": _optional_text(fact_map, "currency"),
        "bedrooms": _optional_int(fact_map, "bedrooms"),
        "amenities": _amenities(fact_map),
        "purpose": _optional_text(fact_map, "purpose"),
        "provenance": dict(PROVENANCE),
    }
    if criteria["budget_minor"] is not None and criteria["budget_minor"] < 0:
        raise InvalidCriteria(
            "budget_minor tidak boleh negatif",
            code=CODE_INVALID_CRITERIA,
            budget_minor=criteria["budget_minor"],
        )

    snapshot: dict[str, Any] = {}
    for key, value in sorted(fact_map.items()):
        if value is None:
            continue  # UNKNOWN is an absence, not a fact
        snapshot[str(key)] = _jsonable(value, field_name=str(key))
    snapshot["check_in"] = to_iso_date(check_in)
    snapshot["check_out"] = to_iso_date(check_out)
    intake_snapshot = canonical_json(snapshot)
    intake_hash = fingerprint(snapshot, domain=_INTAKE_DOMAIN)

    evaluated = tuple(
        _evaluate_unit(conn=conn, kernel=kernel, unit=unit, criteria=criteria) for unit in inventory
    )
    candidates = _order_candidates(evaluated)
    if len({c.unit_id for c in candidates}) != len(candidates):
        raise DomainError(
            "dua kandidat menunjuk unit yang sama",
            code=CODE_SCORE_INVARIANT,
            considered=len(candidates),
        )
    if [c.rank for c in candidates] != list(range(1, len(candidates) + 1)):
        raise DomainError(
            "rank harus unik dan berurutan",
            code=CODE_SCORE_INVARIANT,
            ranks=[c.rank for c in candidates],
        )

    considered_count = len(candidates)
    eligible_count = sum(1 for c in candidates if c.eligible)
    match_run_id = id_factory.new("match_run")
    created_at = to_iso(moment)
    criteria_json = canonical_json(
        {key: value for key, value in criteria.items() if key not in ("nights_list", "check_in_date", "check_out_date")}
    )

    with _write_transaction(conn):
        conn.execute(
            """
            INSERT INTO match_runs (
                match_run_id, case_id, engine_version, intake_snapshot, intake_hash,
                criteria, considered_count, eligible_count, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_run_id,
                case_id,
                ENGINE_VERSION,
                intake_snapshot,
                intake_hash,
                criteria_json,
                considered_count,
                eligible_count,
                DEFAULT_ACTOR,
                created_at,
            ),
        )
        for candidate in candidates:
            conn.execute(
                """
                INSERT INTO match_candidates (
                    candidate_id, match_run_id, unit_id, rank, eligible, score_centi,
                    reasons, deny_reasons, evidence_refs, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id_factory.new("match_candid"),
                    match_run_id,
                    candidate.unit_id,
                    candidate.rank,
                    1 if candidate.eligible else 0,
                    candidate.score_centi,
                    canonical_json([dict(item) for item in candidate.reasons]),
                    canonical_json(list(candidate.deny_reasons)),
                    canonical_json(list(candidate.evidence_refs)),
                    created_at,
                ),
            )

    return MatchResult(
        match_run_id=match_run_id,
        case_id=case_id,
        engine_version=ENGINE_VERSION,
        intake_hash=intake_hash,
        criteria=criteria,
        considered_count=considered_count,
        eligible_count=eligible_count,
        candidates=candidates,
        created_at=created_at,
        intake_snapshot=intake_snapshot,
    )


def _resolve_evidence_ref(conn: sqlite3.Connection, ref: str) -> Optional[dict[str, Any]]:
    """Look an evidence id up in the tables that can own it. ``None`` = dangling."""
    row = conn.execute(
        "SELECT booking_id AS id, status, check_in, check_out FROM bookings WHERE booking_id = ?",
        (ref,),
    ).fetchone()
    if row is not None:
        return {"ref": ref, "kind": "booking", "status": str(row["status"]), "check_in": str(row["check_in"]), "check_out": str(row["check_out"])}
    row = conn.execute(
        "SELECT hold_id AS id, status, check_in, check_out FROM holds WHERE hold_id = ?",
        (ref,),
    ).fetchone()
    if row is not None:
        return {"ref": ref, "kind": "hold", "status": str(row["status"]), "check_in": str(row["check_in"]), "check_out": str(row["check_out"])}
    row = conn.execute(
        "SELECT evidence_id AS id, result, valid_until, evidence_level FROM availability_evidence WHERE evidence_id = ?",
        (ref,),
    ).fetchone()
    if row is not None:
        return {
            "ref": ref,
            "kind": "availability_evidence",
            "status": str(row["result"]),
            "check_in": "",
            "check_out": str(row["valid_until"]),
            "evidence_level": str(row["evidence_level"]),
        }
    return None


def explain(conn: sqlite3.Connection, match_run_id: Any = None, *, now: Any = None) -> MatchExplanation:
    """Ordered, human-readable reasons for one run — evidence checked against the DB.

    Every ``evidence_ref`` the run recorded must still resolve to a real
    ``bookings`` / ``holds`` / ``availability_evidence`` row; a dangling
    reference raises ``DomainError[MATCH_EVIDENCE_MISSING]`` instead of being
    printed as if it were proof. Callable as ``explain(conn, run_id)`` (or with
    the two arguments swapped, which some callers find natural). ``now`` is
    accepted for signature symmetry; explain only ever reads the ledger.
    """
    if isinstance(conn, str) and match_run_id is not None and not isinstance(match_run_id, (str, bytes)):
        conn, match_run_id = match_run_id, conn
    run_id = require_ref(match_run_id, "match_run_id")
    row = conn.execute("SELECT * FROM match_runs WHERE match_run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise MatchRunNotFound(
            f"match run {run_id!r} tidak ditemukan",
            code=CODE_MATCH_RUN_NOT_FOUND,
            match_run_id=run_id,
        )
    run = dict(row)
    rows = conn.execute(
        """
        SELECT c.*, u.property_id AS property_id, u.name AS unit_name,
               p.name AS property_name, p.region AS region
        FROM match_candidates c
        JOIN units u ON u.unit_id = c.unit_id
        LEFT JOIN properties p ON p.property_id = u.property_id
        WHERE c.match_run_id = ?
        ORDER BY c.rank
        """,
        (run_id,),
    ).fetchall()

    lines: list[str] = [
        f"match run {run_id} · case {run['case_id']} · {run['engine_version']}",
        f"created_at={run['created_at']} by {run['created_by']}",
        (
            f"considered={run['considered_count']} eligible={run['eligible_count']} "
            f"intake_hash={run['intake_hash'][:12]}…"
        ),
        f"criteria: {run['criteria']}",
    ]
    candidates: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for candidate_row in rows:
        candidate = dict(candidate_row)
        reasons = json.loads(candidate["reasons"] or "[]")
        deny_reasons = json.loads(candidate["deny_reasons"] or "[]")
        refs = json.loads(candidate["evidence_refs"] or "[]")
        verdict = "ELIGIBLE" if int(candidate["eligible"]) == 1 else "DENIED"
        lines.append(
            f"#{candidate['rank']} [{verdict} score={candidate['score_centi']} centi] "
            f"unit {candidate['unit_id']} ({candidate['property_id']}"
            f"{' / ' + str(candidate['region']) if candidate.get('region') else ''})"
            f" — {candidate['unit_name']}"
        )
        for reason in reasons:
            weight = int(reason.get("weight", 0))
            lines.append(f"    + {reason.get('code')} ({weight} centi): {canonical_json(reason.get('detail', {}))}")
        if not reasons and int(candidate["eligible"]) == 1:
            raise DomainError(
                "an eligible candidate must carry at least one positive reason",
                code=CODE_SCORE_INVARIANT,
                unit_id=candidate["unit_id"],
            )
        for code in deny_reasons:
            lines.append(f"    - {code}")
        if int(candidate["eligible"]) == 0 and not deny_reasons:
            raise DomainError(
                "an ineligible candidate must carry at least one coded deny reason",
                code=CODE_SCORE_INVARIANT,
                unit_id=candidate["unit_id"],
            )
        resolved: list[dict[str, Any]] = []
        for ref in refs:
            found = _resolve_evidence_ref(conn, str(ref))
            if found is None:
                raise DomainError(
                    f"evidence_ref {ref!r} tidak ada di DB",
                    code=CODE_DANGLING_EVIDENCE,
                    match_run_id=run_id,
                    unit_id=candidate["unit_id"],
                    evidence_ref=str(ref),
                )
            resolved.append(found)
            evidence.append(found)
            lines.append(
                f"    evidence {found['ref']} ({found['kind']} {found['status']} "
                f"{found['check_in']}→{found['check_out']})"
            )
        candidates.append(
            {
                "rank": int(candidate["rank"]),
                "unit_id": str(candidate["unit_id"]),
                "property_id": str(candidate["property_id"]),
                "unit_name": str(candidate["unit_name"]),
                "eligible": int(candidate["eligible"]) == 1,
                "score_centi": int(candidate["score_centi"]),
                "reasons": reasons,
                "deny_reasons": deny_reasons,
                "evidence_refs": [str(ref) for ref in refs],
                "evidence": resolved,
            }
        )
    text = "\n".join(lines)
    return MatchExplanation(
        text,
        match_run_id=run_id,
        case_id=str(run["case_id"]),
        engine_version=str(run["engine_version"]),
        criteria=json.loads(run["criteria"] or "{}"),
        candidates=tuple(candidates),
        evidence_refs=tuple(evidence),
        considered_count=int(run["considered_count"]),
        eligible_count=int(run["eligible_count"]),
    )
