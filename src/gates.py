"""Fail-closed domain gates for Villa Agency OS.

Four gates decide whether a unit may be *shown*, *quoted* or *held*, and whether
a payload may *leave the system*:

``gate_contract_pack``    Gate #0 — property contract pack
                          (``docs/PROPERTY_CONTRACT_PACK.md``).
``gate_availability``     Availability evidence freshness + level A–D
                          (``docs/POLICY_MATRIX.md`` §6).
``gate_outbound``         Data classification P0–P4 for every destination
                          (``docs/DATA_CLASSIFICATION_AND_ACCESS.md``).
``gate_unit``             Composite: unit status → contract pack → availability.

Design rules
------------
* **Fail closed.** Missing, stale, ambiguous, unknown or misdeclared input is a
  denial carrying a machine-readable ``code`` — never a default allow.
* **Pure.** No database, no network, no implicit time source: ``now`` is always
  an argument and every function returns a :class:`GateDecision`.
* **Explicit staleness.** Freshness windows are caller-visible parameters
  (``max_age_hours``); the per-level guardrails of POLICY_MATRIX §6 cap them, and
  a window *looser* than the guardrail is itself a denial.
* **Boundary convention.** An evidence freshness window is *inclusive* at its
  limit (``age == window`` is still fresh, ``age == window + 1s`` is stale);
  ``valid_until`` is *exclusive* (``now == valid_until`` is expired). A review
  date given as a plain date means "end of that UTC day".
* Callers that need an exception instead of a decision use :func:`require_allow`,
  which raises :class:`GateDenied` (a :class:`primitives.DomainError`) carrying
  the denial code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as _field, replace
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from primitives import (
    UTC,
    DomainError,
    canonical_json,
    decimal_of,
    parse_iso,
    parse_iso_date,
    redact,
    to_iso,
)

__all__ = [
    # decisions
    "GateDecision",
    "GateDenied",
    "require_allow",
    # reasons
    "CODE_OK",
    "CODE_REVIEW_REQUIRED",
    "CODE_APPROVAL_REQUIRED",
    "CODE_INVALID_PURPOSE",
    "CODE_INVALID_NOW",
    "CODE_INVALID_FRESHNESS_WINDOW",
    "CODE_FRESHNESS_WINDOW_TOO_LOOSE",
    "CODE_CONTRACT_PACK_MISSING",
    "CODE_CONTRACT_PACK_EMPTY",
    "CODE_CONTRACT_PACK_STATUS_UNKNOWN",
    "CODE_CONTRACT_PACK_DRAFT",
    "CODE_CONTRACT_PACK_NOT_ACTIVATED",
    "CODE_CONTRACT_PACK_EXPIRED",
    "CODE_CONTRACT_PACK_EVALUATION_STALE",
    "CODE_CONTRACT_PACK_ITEM_MISSING",
    "CODE_CONTRACT_PACK_ITEM_EXPIRED",
    "CODE_CONTRACT_PACK_ITEM_INVALID",
    "CODE_PROPERTY_SUSPENDED",
    "CODE_PROPERTY_NOT_SELLABLE",
    "CODE_PROPERTY_STATUS_UNKNOWN",
    "CODE_UNIT_SUSPENDED",
    "CODE_UNIT_IN_MAINTENANCE",
    "CODE_UNIT_STATUS_UNKNOWN",
    "CODE_EVIDENCE_MISSING",
    "CODE_EVIDENCE_TIMESTAMP_INVALID",
    "CODE_EVIDENCE_WINDOW_INVALID",
    "CODE_EVIDENCE_FROM_FUTURE",
    "CODE_EVIDENCE_STALE",
    "CODE_EVIDENCE_EXPIRED",
    "CODE_EVIDENCE_LEVEL_UNKNOWN",
    "CODE_EVIDENCE_LEVEL_TOO_WEAK",
    "CODE_EVIDENCE_SOURCE_UNKNOWN",
    "CODE_EVIDENCE_SOURCE_LEVEL_MISMATCH",
    "CODE_EVIDENCE_CANNOT_QUOTE",
    "CODE_EVIDENCE_CANNOT_HOLD",
    "CODE_EVIDENCE_CANNOT_CONFIRM",
    "CODE_AVAILABILITY_UNKNOWN",
    "CODE_UNIT_OCCUPIED",
    "CODE_EMPTY_PAYLOAD",
    "CODE_UNKNOWN_DESTINATION",
    "CODE_UNKNOWN_CLASSIFICATION",
    "CODE_P4_PROHIBITED",
    "CODE_P3_FORBIDDEN",
    "CODE_PII_REDACTION_REQUIRED",
    "CODE_REDACTION_NOT_APPLIED",
    "CODE_CONTENT_LOGGING_FORBIDDEN",
    "CODE_CHANNEL_FORBIDDEN",
    "CODE_MISCLASSIFIED_CONTENT",
    "CODE_EXPORT_REQUIRES_ROLE",
    "CODE_EXPORT_REQUIRES_JUSTIFICATION",
    # contract pack
    "GATE_CONTRACT_PACK",
    "GATE_AVAILABILITY",
    "GATE_CLASSIFICATION",
    "GATE_UNIT",
    "GATES",
    "PURPOSES",
    "PURPOSE_SHOW",
    "PURPOSE_QUOTE",
    "PURPOSE_HOLD",
    "PURPOSE_CONFIRM",
    "PROPERTY_STATUSES",
    "CONTRACT_PACK_STATUSES",
    "ITEM_STATUSES",
    "UNIT_STATUSES",
    "ContractPackItem",
    "ContractPack",
    "gate_contract_pack",
    # availability
    "LEVEL_FRESHNESS_SECONDS",
    "LEVEL_MAX_FRESHNESS_SECONDS",
    "PURPOSE_LEVELS",
    "LEVEL_SOURCE_TYPES",
    "SOURCE_TYPES",
    "AvailabilityEvidence",
    "gate_availability",
    # classification
    "CLASSIFICATIONS",
    "DESTINATIONS",
    "POLICY_TABLE",
    "P2_AUTH_ROLES",
    "P3_AUTH_ROLES",
    "IDENTIFIER_PATTERNS",
    "ClassifiedField",
    "apply_redaction",
    "gate_outbound",
    "gate_classification",
    # composite
    "gate_unit",
]


# --------------------------------------------------------------------------- #
# Reason codes (the machine-readable contract — never parse messages)
# --------------------------------------------------------------------------- #
CODE_OK = "OK"
CODE_REVIEW_REQUIRED = "REVIEW_REQUIRED"
CODE_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"

CODE_INVALID_PURPOSE = "INVALID_PURPOSE"
CODE_INVALID_NOW = "INVALID_NOW"
CODE_INVALID_FRESHNESS_WINDOW = "INVALID_FRESHNESS_WINDOW"
CODE_FRESHNESS_WINDOW_TOO_LOOSE = "FRESHNESS_WINDOW_TOO_LOOSE"

#: Gate #0 — contract pack.
CODE_CONTRACT_PACK_MISSING = "CONTRACT_PACK_MISSING"
CODE_CONTRACT_PACK_EMPTY = "CONTRACT_PACK_EMPTY"
CODE_CONTRACT_PACK_STATUS_UNKNOWN = "CONTRACT_PACK_STATUS_UNKNOWN"
CODE_CONTRACT_PACK_DRAFT = "CONTRACT_PACK_DRAFT"
CODE_CONTRACT_PACK_NOT_ACTIVATED = "CONTRACT_PACK_NOT_ACTIVATED"
CODE_CONTRACT_PACK_EXPIRED = "CONTRACT_PACK_EXPIRED"
CODE_CONTRACT_PACK_EVALUATION_STALE = "CONTRACT_PACK_EVALUATION_STALE"
CODE_CONTRACT_PACK_ITEM_MISSING = "CONTRACT_PACK_ITEM_MISSING"
CODE_CONTRACT_PACK_ITEM_EXPIRED = "CONTRACT_PACK_ITEM_EXPIRED"
CODE_CONTRACT_PACK_ITEM_INVALID = "CONTRACT_PACK_ITEM_INVALID"
CODE_PROPERTY_SUSPENDED = "PROPERTY_SUSPENDED"
CODE_PROPERTY_NOT_SELLABLE = "PROPERTY_NOT_SELLABLE"
CODE_PROPERTY_STATUS_UNKNOWN = "PROPERTY_STATUS_UNKNOWN"
CODE_UNIT_SUSPENDED = "UNIT_SUSPENDED"
CODE_UNIT_IN_MAINTENANCE = "UNIT_IN_MAINTENANCE"
CODE_UNIT_STATUS_UNKNOWN = "UNIT_STATUS_UNKNOWN"

#: Availability evidence freshness / level.
CODE_EVIDENCE_MISSING = "EVIDENCE_MISSING"
CODE_EVIDENCE_TIMESTAMP_INVALID = "EVIDENCE_TIMESTAMP_INVALID"
CODE_EVIDENCE_WINDOW_INVALID = "EVIDENCE_WINDOW_INVALID"
CODE_EVIDENCE_FROM_FUTURE = "EVIDENCE_FROM_FUTURE"
CODE_EVIDENCE_STALE = "EVIDENCE_STALE"
CODE_EVIDENCE_EXPIRED = "EVIDENCE_EXPIRED"
CODE_EVIDENCE_LEVEL_UNKNOWN = "EVIDENCE_LEVEL_UNKNOWN"
CODE_EVIDENCE_LEVEL_TOO_WEAK = "EVIDENCE_LEVEL_TOO_WEAK"
CODE_EVIDENCE_SOURCE_UNKNOWN = "EVIDENCE_SOURCE_UNKNOWN"
CODE_EVIDENCE_SOURCE_LEVEL_MISMATCH = "EVIDENCE_SOURCE_LEVEL_MISMATCH"
CODE_EVIDENCE_CANNOT_QUOTE = "EVIDENCE_CANNOT_QUOTE"
CODE_EVIDENCE_CANNOT_HOLD = "EVIDENCE_CANNOT_HOLD"
CODE_EVIDENCE_CANNOT_CONFIRM = "EVIDENCE_CANNOT_CONFIRM"
CODE_AVAILABILITY_UNKNOWN = "AVAILABILITY_UNKNOWN"
CODE_UNIT_OCCUPIED = "UNIT_OCCUPIED"

#: Data classification (P0–P4) for anything leaving the system.
CODE_EMPTY_PAYLOAD = "EMPTY_PAYLOAD"
CODE_UNKNOWN_DESTINATION = "UNKNOWN_DESTINATION"
CODE_UNKNOWN_CLASSIFICATION = "UNKNOWN_CLASSIFICATION"
CODE_P4_PROHIBITED = "P4_PROHIBITED"
CODE_P3_FORBIDDEN = "P3_FORBIDDEN"
CODE_PII_REDACTION_REQUIRED = "PII_REDACTION_REQUIRED"
CODE_REDACTION_NOT_APPLIED = "REDACTION_NOT_APPLIED"
CODE_CONTENT_LOGGING_FORBIDDEN = "CONTENT_LOGGING_FORBIDDEN"
CODE_CHANNEL_FORBIDDEN = "CHANNEL_FORBIDDEN"
CODE_MISCLASSIFIED_CONTENT = "MISCLASSIFIED_CONTENT"
CODE_EXPORT_REQUIRES_ROLE = "EXPORT_REQUIRES_ROLE"
CODE_EXPORT_REQUIRES_JUSTIFICATION = "EXPORT_REQUIRES_JUSTIFICATION"

#: Gate names used in :attr:`GateDecision.gate` and :data:`GATES`.
GATE_CONTRACT_PACK = "contract_pack"
GATE_AVAILABILITY = "availability"
GATE_CLASSIFICATION = "classification"
GATE_UNIT = "unit"

#: What a unit may be used for. ``confirm`` is included because the availability
#: level rules (POLICY_MATRIX §6) only make sense next to it.
PURPOSE_SHOW = "show"
PURPOSE_QUOTE = "quote"
PURPOSE_HOLD = "hold"
PURPOSE_CONFIRM = "confirm"
PURPOSES = (PURPOSE_SHOW, PURPOSE_QUOTE, PURPOSE_HOLD, PURPOSE_CONFIRM)

#: Enumerations taken from ``src/schema/001_initial.sql``.
PROPERTY_STATUSES = ("SELLABLE", "SELLABLE_WITH_REVIEW", "NOT_SELLABLE", "SUSPENDED")
CONTRACT_PACK_STATUSES = ("DRAFT", "VERIFIED", "ACTIVATED", "EXPIRED")
ITEM_STATUSES = ("VALID", "MISSING", "EXPIRED", "INVALID", "PENDING")
UNIT_STATUSES = ("ACTIVE", "MAINTENANCE", "SUSPENDED")


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GateDecision:
    """The immutable outcome of one gate evaluation.

    ``allowed`` is the only thing a caller may act on; ``code`` is the
    machine-readable reason and never empty.
    """

    allowed: bool
    code: str
    message: str = ""
    gate: str = ""
    review_required: bool = False
    details: Mapping[str, Any] = _field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise DomainError(
                "allowed must be a bool",
                code="INVALID_FIELD",
                field="allowed",
                got=type(self.allowed).__name__,
            )
        if not isinstance(self.code, str) or not self.code:
            raise DomainError(
                "a gate decision must carry a non-empty reason code",
                code="INVALID_FIELD",
                field="code",
            )
        if not isinstance(self.details, Mapping):
            raise DomainError(
                "details must be a mapping",
                code="INVALID_FIELD",
                field="details",
            )

    @property
    def denied(self) -> bool:
        return not self.allowed

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "message": self.message,
            "gate": self.gate,
            "review_required": self.review_required,
            "details": dict(self.details),
        }

    def __bool__(self) -> bool:
        return self.allowed

    def __str__(self) -> str:  # pragma: no cover - formatting only
        verdict = "ALLOW" if self.allowed else "DENY"
        return f"{verdict} [{self.code}] {self.gate}: {self.message}"


class GateDenied(DomainError):
    """Raised by :func:`require_allow`; carries the denial code and details."""

    code = "GATE_DENIED"

    def __init__(self, decision: GateDecision) -> None:
        details: dict[str, Any] = {"gate": decision.gate}
        details.update({str(k): v for k, v in decision.details.items()})
        super().__init__(
            decision.message or f"gate {decision.gate or 'unknown'} denied",
            code=decision.code,
            **details,
        )
        self.decision = decision


def require_allow(decision: GateDecision) -> GateDecision:
    """Return ``decision`` when allowed, else raise :class:`GateDenied`."""
    if not isinstance(decision, GateDecision):
        raise DomainError(
            "require_allow expects a GateDecision",
            code="INVALID_FIELD",
            field="decision",
            got=type(decision).__name__,
        )
    if not decision.allowed:
        raise GateDenied(decision)
    return decision


def _allow(
    gate: str,
    code: str = CODE_OK,
    message: str = "",
    review_required: bool = False,
    details: Optional[Mapping[str, Any]] = None,
) -> GateDecision:
    return GateDecision(
        allowed=True,
        code=code,
        message=message,
        gate=gate,
        review_required=review_required,
        details=dict(details or {}),
    )


def _deny(
    gate: str,
    code: str,
    message: str,
    details: Optional[Mapping[str, Any]] = None,
) -> GateDecision:
    return GateDecision(
        allowed=False,
        code=code,
        message=message,
        gate=gate,
        details=dict(details or {}),
    )


# --------------------------------------------------------------------------- #
# Small internal helpers (pure)
# --------------------------------------------------------------------------- #
def _norm(value: Any) -> str:
    """Upper-cased enum form (``"sellable-with review"`` → ``SELLABLE_WITH_REVIEW``).

    Anything that is not a string normalises to ``""`` — which every caller
    treats as unknown, i.e. a denial.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip().upper().replace("-", " ").replace("_", " ")
    return "_".join(text.split())


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Aware-UTC datetime, or ``None`` when the value cannot be trusted.

    Date-only strings are rejected on purpose: a bare date cannot express how old
    a piece of evidence is (fail closed).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(UTC)
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    try:
        return parse_iso(value)
    except DomainError:
        return None


def _parse_deadline(value: Any) -> Optional[datetime]:
    """Deadline from a timestamp or a plain date (end of that UTC day)."""
    if isinstance(value, datetime):
        return _parse_timestamp(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return parse_iso(text)
    except DomainError:
        pass
    try:
        day = parse_iso_date(text)
    except DomainError:
        return None
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


class _InvalidWindow(Exception):
    """Internal: a freshness window parameter that cannot be trusted."""


def _hours_to_seconds(value: Any) -> int:
    """Hours → whole seconds, truncating (a shorter window is the safe side)."""
    if value is None or isinstance(value, bool):
        raise _InvalidWindow(f"freshness window must be a number of hours, got {value!r}")
    try:
        hours = decimal_of(value)
    except DomainError as exc:
        raise _InvalidWindow(f"freshness window is not a number: {value!r}") from exc
    if hours < 0:
        raise _InvalidWindow(f"freshness window must not be negative: {value!r}")
    return int(hours * 3600)


def _flag(value: Any) -> bool:
    """SQLite ``0/1`` or python truthy → bool; anything else is ``False``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# --------------------------------------------------------------------------- #
# Gate #0 — property contract pack
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ContractPackItem:
    """One contract-pack line item (C01–C20).

    ``blocking`` follows the only place the docs allow that decision to live: the
    property's ``policy_matrix`` (``blocking: true|false`` per item).
    """

    item_id: str
    status: str = "MISSING"
    blocking: bool = True
    next_review_at: Optional[str] = None

    def is_valid(self) -> bool:
        return _norm(self.status) == "VALID"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "status": self.status,
            "blocking": self.blocking,
            "next_review_at": self.next_review_at,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ContractPackItem":
        return cls(
            item_id=str(row.get("item_id") or row.get("id") or ""),
            status=row.get("status") or "MISSING",
            blocking=_flag(row.get("blocking", True)),
            next_review_at=row.get("next_review_at"),
        )


@dataclass(frozen=True)
class ContractPack:
    """The evaluated contract pack of one property.

    Defaults deny: an unset status is *unknown*, not sellable.
    """

    property_id: str = ""
    status: str = ""
    property_status: str = "NOT_SELLABLE"
    items: tuple[ContractPackItem, ...] = ()
    evaluated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "property_id": self.property_id,
            "contract_pack_status": self.status,
            "property_status": self.property_status,
            "evaluated_at": self.evaluated_at,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ContractPack":
        """Build from a ``properties`` row (plus optional ``items``)."""
        raw_items = row.get("items") or ()
        items = tuple(
            entry if isinstance(entry, ContractPackItem) else ContractPackItem.from_row(entry)
            for entry in raw_items
            if isinstance(entry, (ContractPackItem, Mapping))
        )
        return cls(
            property_id=str(row.get("property_id") or ""),
            status=row.get("contract_pack_status") or row.get("status") or "",
            property_status=row.get("property_status") or row.get("status") or "",
            items=items,
            evaluated_at=row.get("contract_pack_evaluated_at") or row.get("evaluated_at"),
        )


def _coerce_pack(pack: Any) -> Optional[ContractPack]:
    if isinstance(pack, ContractPack):
        return pack
    if isinstance(pack, Mapping):
        return ContractPack.from_row(pack)
    return None


def gate_contract_pack(
    pack: Any,
    *,
    now: Any,
    purpose: str,
    max_pack_age_hours: Any = None,
    approval_ref: Optional[str] = None,
) -> GateDecision:
    """Gate #0: may this unit be shown / quoted / held at all?

    Ordered, fail-closed evaluation:

    1. a manually ``SUSPENDED`` property stops everything;
    2. the pack must exist and be ``ACTIVATED`` (``DRAFT`` → not verified,
       ``VERIFIED`` → not yet activated, ``EXPIRED`` → re-verification due);
    3. the property must not be ``NOT_SELLABLE``;
    4. optional ``max_pack_age_hours``: a stale evaluation is not trusted;
    5. every *blocking* item must be ``VALID`` and not past its
       ``next_review_at``;
    6. non-blocking gaps and ``SELLABLE_WITH_REVIEW`` set ``review_required``;
       ``confirm`` additionally needs ``approval_ref``.
    """
    gate = GATE_CONTRACT_PACK
    purpose_key = _norm(purpose).lower()
    if purpose_key not in PURPOSES:
        return _deny(
            gate,
            CODE_INVALID_PURPOSE,
            f"unknown purpose {purpose!r}: expected one of {list(PURPOSES)}",
            {"purpose": purpose},
        )
    moment = _parse_timestamp(now)
    if moment is None:
        return _deny(
            gate,
            CODE_INVALID_NOW,
            "now must be a timezone-aware datetime (UTC)",
            {"now": repr(now)},
        )

    resolved = _coerce_pack(pack)
    if resolved is None:
        return _deny(
            gate,
            CODE_CONTRACT_PACK_MISSING,
            "no contract pack: a unit without a pack may never be shown, quoted or held",
        )

    property_status = _norm(resolved.property_status)
    if property_status == "SUSPENDED":
        return _deny(
            gate,
            CODE_PROPERTY_SUSPENDED,
            "property is suspended (dispute, damage or serious complaint)",
            {"property_id": resolved.property_id},
        )
    if property_status not in PROPERTY_STATUSES:
        return _deny(
            gate,
            CODE_PROPERTY_STATUS_UNKNOWN,
            f"property status {resolved.property_status!r} is not a known status",
            {"property_id": resolved.property_id, "property_status": resolved.property_status},
        )

    pack_status = _norm(resolved.status)
    if pack_status not in CONTRACT_PACK_STATUSES:
        return _deny(
            gate,
            CODE_CONTRACT_PACK_STATUS_UNKNOWN,
            f"contract pack status {resolved.status!r} is not a known status",
            {"property_id": resolved.property_id, "contract_pack_status": resolved.status},
        )
    if pack_status == "DRAFT":
        return _deny(
            gate,
            CODE_CONTRACT_PACK_DRAFT,
            "contract pack is still a draft: verification has not happened",
            {"property_id": resolved.property_id, "contract_pack_status": pack_status},
        )
    if pack_status == "VERIFIED":
        return _deny(
            gate,
            CODE_CONTRACT_PACK_NOT_ACTIVATED,
            "contract pack is verified but not activated: property status was never re-evaluated",
            {"property_id": resolved.property_id, "contract_pack_status": pack_status},
        )
    if pack_status == "EXPIRED":
        return _deny(
            gate,
            CODE_CONTRACT_PACK_EXPIRED,
            "contract pack has expired: re-verification is due",
            {"property_id": resolved.property_id, "contract_pack_status": pack_status},
        )

    if property_status == "NOT_SELLABLE":
        return _deny(
            gate,
            CODE_PROPERTY_NOT_SELLABLE,
            "property is NOT_SELLABLE: a blocking item is missing or expired",
            {"property_id": resolved.property_id, "property_status": property_status},
        )

    if max_pack_age_hours is not None:
        try:
            window_seconds = _hours_to_seconds(max_pack_age_hours)
        except _InvalidWindow as exc:
            return _deny(
                gate,
                CODE_INVALID_FRESHNESS_WINDOW,
                str(exc),
                {"max_pack_age_hours": repr(max_pack_age_hours)},
            )
        evaluated_at = _parse_timestamp(resolved.evaluated_at)
        if evaluated_at is None:
            return _deny(
                gate,
                CODE_CONTRACT_PACK_EVALUATION_STALE,
                "pack evaluation time is missing or unreadable, so its age cannot be bounded",
                {"evaluated_at": repr(resolved.evaluated_at)},
            )
        age = moment - evaluated_at
        if age < timedelta(0):
            return _deny(
                gate,
                CODE_CONTRACT_PACK_EVALUATION_STALE,
                "pack evaluation is stamped in the future",
                {"evaluated_at": to_iso(evaluated_at), "age_seconds": int(age.total_seconds())},
            )
        if age > timedelta(seconds=window_seconds):
            return _deny(
                gate,
                CODE_CONTRACT_PACK_EVALUATION_STALE,
                "pack evaluation is older than the allowed window",
                {
                    "evaluated_at": to_iso(evaluated_at),
                    "age_seconds": int(age.total_seconds()),
                    "window_seconds": window_seconds,
                },
            )

    if not resolved.items:
        return _deny(
            gate,
            CODE_CONTRACT_PACK_EMPTY,
            "pack carries no items: an unevaluated pack cannot be trusted",
            {"property_id": resolved.property_id},
        )

    blocking_missing: list[str] = []
    blocking_expired: list[str] = []
    blocking_invalid: list[str] = []
    nonblocking_gaps: list[str] = []
    for item in sorted(resolved.items, key=lambda entry: entry.item_id):
        status = _norm(item.status)
        problem: Optional[str] = None
        if status != "VALID":
            if status == "MISSING":
                problem = CODE_CONTRACT_PACK_ITEM_MISSING
            elif status == "EXPIRED":
                problem = CODE_CONTRACT_PACK_ITEM_EXPIRED
            else:
                problem = CODE_CONTRACT_PACK_ITEM_INVALID
        else:
            deadline = _parse_deadline(item.next_review_at)
            if item.next_review_at is not None:
                if deadline is None:
                    return _deny(
                        gate,
                        CODE_CONTRACT_PACK_ITEM_INVALID,
                        f"item {item.item_id} has an unreadable next_review_at",
                        {"item_ids": [item.item_id], "next_review_at": repr(item.next_review_at)},
                    )
                if moment > deadline:
                    problem = CODE_CONTRACT_PACK_ITEM_EXPIRED
        if problem is None:
            continue
        if item.blocking:
            if problem == CODE_CONTRACT_PACK_ITEM_MISSING:
                blocking_missing.append(item.item_id)
            elif problem == CODE_CONTRACT_PACK_ITEM_EXPIRED:
                blocking_expired.append(item.item_id)
            else:
                blocking_invalid.append(item.item_id)
        else:
            nonblocking_gaps.append(item.item_id)

    for codes, names, label in (
        (blocking_missing, blocking_missing, "missing"),
        (blocking_expired, blocking_expired, "expired"),
        (blocking_invalid, blocking_invalid, "invalid"),
    ):
        if not codes:
            continue
        code = {
            "missing": CODE_CONTRACT_PACK_ITEM_MISSING,
            "expired": CODE_CONTRACT_PACK_ITEM_EXPIRED,
            "invalid": CODE_CONTRACT_PACK_ITEM_INVALID,
        }[label]
        return _deny(
            gate,
            code,
            f"blocking contract-pack item(s) {label}: {', '.join(sorted(set(names)))}",
            {
                "item_ids": sorted(set(names)),
                "property_id": resolved.property_id,
            },
        )

    review_required = property_status == "SELLABLE_WITH_REVIEW" or bool(nonblocking_gaps)
    if purpose_key == PURPOSE_CONFIRM and review_required and not _text(approval_ref):
        return _deny(
            gate,
            CODE_APPROVAL_REQUIRED,
            "confirming a SELLABLE_WITH_REVIEW property needs an explicit manager approval",
            {"property_id": resolved.property_id, "review_items": sorted(set(nonblocking_gaps))},
        )

    details: dict[str, Any] = {
        "property_id": resolved.property_id,
        "contract_pack_status": pack_status,
        "property_status": property_status,
    }
    if nonblocking_gaps:
        details["review_items"] = sorted(set(nonblocking_gaps))
    if review_required:
        return _allow(
            gate,
            CODE_REVIEW_REQUIRED,
            "allowed with review: non-blocking contract-pack items still need completion",
            review_required=True,
            details=details,
        )
    return _allow(gate, details=details)


# --------------------------------------------------------------------------- #
# Availability evidence — freshness + level A–D
# --------------------------------------------------------------------------- #
#: Default freshness window per level (POLICY_MATRIX §6).
LEVEL_FRESHNESS_SECONDS = {"A": 900, "B": 86400, "C": 172800, "D": 0}

#: Hard cap per level: a caller may never request a looser window (§6 guardrails).
LEVEL_MAX_FRESHNESS_SECONDS = {"A": 3600, "B": 172800, "C": 604800, "D": 0}

#: Which levels may be used for which purpose (level table, POLICY_MATRIX §6).
#: D never appears: "memori lama / tanpa sumber" sells nothing.
PURPOSE_LEVELS = {
    PURPOSE_SHOW: ("A", "B", "C"),
    PURPOSE_QUOTE: ("A", "B", "C"),
    PURPOSE_HOLD: ("A", "B", "C"),
    PURPOSE_CONFIRM: ("A", "B"),
}

SOURCE_TYPES = ("property_api", "booking_engine", "manager_written", "ical", "memory", "manual_ops")

#: An evidence level must be justified by its source type.
LEVEL_SOURCE_TYPES = {
    "A": ("property_api", "booking_engine"),
    "B": ("manager_written",),
    "C": ("ical",),
    "D": ("memory", "manual_ops"),
}


@dataclass(frozen=True)
class AvailabilityEvidence:
    """One ``availability_evidence`` row.

    Defaults deny: level unknown, result unknown, no capability flags.
    """

    evidence_id: str = ""
    unit_id: str = ""
    evidence_level: str = ""
    source_type: str = ""
    captured_at: str = ""
    result: str = ""
    valid_until: Optional[str] = None
    can_quote: bool = False
    can_hold: bool = False
    can_confirm: bool = False
    source_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "unit_id": self.unit_id,
            "evidence_level": self.evidence_level,
            "source_type": self.source_type,
            "captured_at": self.captured_at,
            "result": self.result,
            "valid_until": self.valid_until,
            "can_quote": self.can_quote,
            "can_hold": self.can_hold,
            "can_confirm": self.can_confirm,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AvailabilityEvidence":
        """Build from a SQLite row (``can_*`` arrive as ``0``/``1``)."""
        return cls(
            evidence_id=str(row.get("evidence_id") or ""),
            unit_id=str(row.get("unit_id") or ""),
            evidence_level=row.get("evidence_level") or "",
            source_type=row.get("source_type") or "",
            captured_at=row.get("captured_at") or "",
            result=row.get("result") or "",
            valid_until=row.get("valid_until"),
            can_quote=_flag(row.get("can_quote")),
            can_hold=_flag(row.get("can_hold")),
            can_confirm=_flag(row.get("can_confirm")),
            source_ref=str(row.get("source_ref") or ""),
        )


def _coerce_evidence(evidence: Any) -> Optional[AvailabilityEvidence]:
    if isinstance(evidence, AvailabilityEvidence):
        return evidence
    if isinstance(evidence, Mapping):
        return AvailabilityEvidence.from_row(evidence)
    return None


def gate_availability(
    evidence: Any,
    *,
    now: Any,
    purpose: str,
    max_age_hours: Any = None,
) -> GateDecision:
    """May this unit be shown / quoted / held / confirmed on this evidence?

    ``max_age_hours`` is the explicit staleness parameter: it replaces the level
    default window and may only *tighten* it (a value above the level guardrail is
    rejected — a too-loose window is a configuration error, not a permission).
    The window is inclusive at its limit; ``valid_until`` is exclusive.
    """
    gate = GATE_AVAILABILITY
    purpose_key = _norm(purpose).lower()
    if purpose_key not in PURPOSES:
        return _deny(
            gate,
            CODE_INVALID_PURPOSE,
            f"unknown purpose {purpose!r}: expected one of {list(PURPOSES)}",
            {"purpose": purpose},
        )
    moment = _parse_timestamp(now)
    if moment is None:
        return _deny(
            gate,
            CODE_INVALID_NOW,
            "now must be a timezone-aware datetime (UTC)",
            {"now": repr(now)},
        )

    resolved = _coerce_evidence(evidence)
    if resolved is None:
        return _deny(
            gate,
            CODE_EVIDENCE_MISSING,
            "no availability evidence: nothing may be shown, quoted or held",
        )

    level = _norm(resolved.evidence_level)
    if level not in LEVEL_FRESHNESS_SECONDS:
        return _deny(
            gate,
            CODE_EVIDENCE_LEVEL_UNKNOWN,
            f"evidence level {resolved.evidence_level!r} is not one of A–D",
            {"evidence_level": resolved.evidence_level},
        )
    if level not in PURPOSE_LEVELS[purpose_key]:
        return _deny(
            gate,
            CODE_EVIDENCE_LEVEL_TOO_WEAK,
            f"level {level} evidence may not be used to {purpose_key}",
            {
                "evidence_level": level,
                "purpose": purpose_key,
                "allowed_levels": list(PURPOSE_LEVELS[purpose_key]),
            },
        )

    source = _norm(resolved.source_type).lower()
    if source not in SOURCE_TYPES:
        return _deny(
            gate,
            CODE_EVIDENCE_SOURCE_UNKNOWN,
            f"source type {resolved.source_type!r} is not a known source",
            {"source_type": resolved.source_type},
        )
    if source not in LEVEL_SOURCE_TYPES[level]:
        return _deny(
            gate,
            CODE_EVIDENCE_SOURCE_LEVEL_MISMATCH,
            f"source {source!r} cannot justify evidence level {level}",
            {
                "source_type": source,
                "evidence_level": level,
                "allowed_sources": list(LEVEL_SOURCE_TYPES[level]),
            },
        )

    result = _norm(resolved.result)
    if result == "OCCUPIED":
        return _deny(
            gate,
            CODE_UNIT_OCCUPIED,
            "unit is occupied according to the cited evidence",
            {"evidence_id": resolved.evidence_id, "evidence_level": level},
        )
    if result != "AVAILABLE":
        return _deny(
            gate,
            CODE_AVAILABILITY_UNKNOWN,
            "availability is unknown: unknown is not available",
            {"evidence_id": resolved.evidence_id, "result": resolved.result},
        )

    captured = _parse_timestamp(resolved.captured_at)
    if captured is None:
        return _deny(
            gate,
            CODE_EVIDENCE_TIMESTAMP_INVALID,
            "captured_at must be a full ISO-8601 timestamp with an offset",
            {"captured_at": repr(resolved.captured_at)},
        )
    valid_until: Optional[datetime] = None
    if resolved.valid_until is not None:
        valid_until = _parse_timestamp(resolved.valid_until)
        if valid_until is None:
            return _deny(
                gate,
                CODE_EVIDENCE_TIMESTAMP_INVALID,
                "valid_until must be a full ISO-8601 timestamp with an offset",
                {"valid_until": repr(resolved.valid_until)},
            )
        if captured >= valid_until:
            return _deny(
                gate,
                CODE_EVIDENCE_WINDOW_INVALID,
                "captured_at must be strictly before valid_until",
                {"captured_at": to_iso(captured), "valid_until": to_iso(valid_until)},
            )

    window_seconds = LEVEL_FRESHNESS_SECONDS[level]
    if max_age_hours is not None:
        try:
            requested = _hours_to_seconds(max_age_hours)
        except _InvalidWindow as exc:
            return _deny(
                gate,
                CODE_INVALID_FRESHNESS_WINDOW,
                str(exc),
                {"max_age_hours": repr(max_age_hours)},
            )
        guardrail = LEVEL_MAX_FRESHNESS_SECONDS[level]
        if requested > guardrail:
            return _deny(
                gate,
                CODE_FRESHNESS_WINDOW_TOO_LOOSE,
                f"max_age_hours {max_age_hours!r} exceeds the level {level} guardrail ({guardrail}s)",
                {
                    "max_age_hours": repr(max_age_hours),
                    "guardrail_seconds": guardrail,
                    "evidence_level": level,
                },
            )
        window_seconds = requested

    age = moment - captured
    if age < timedelta(0):
        return _deny(
            gate,
            CODE_EVIDENCE_FROM_FUTURE,
            "evidence is stamped after the evaluation time: it cannot be trusted",
            {
                "captured_at": to_iso(captured),
                "now": to_iso(moment),
                "skew_seconds": int(age.total_seconds()),
            },
        )
    if valid_until is not None and moment >= valid_until:
        return _deny(
            gate,
            CODE_EVIDENCE_EXPIRED,
            "evidence is past its valid_until window",
            {"valid_until": to_iso(valid_until), "now": to_iso(moment)},
        )
    if age > timedelta(seconds=window_seconds):
        return _deny(
            gate,
            CODE_EVIDENCE_STALE,
            "evidence is older than the allowed freshness window",
            {
                "captured_at": to_iso(captured),
                "age_seconds": int(age.total_seconds()),
                "window_seconds": window_seconds,
                "expires_at": to_iso(captured + timedelta(seconds=window_seconds)),
            },
        )

    capability_code = None
    if purpose_key == PURPOSE_QUOTE and not resolved.can_quote:
        capability_code = (CODE_EVIDENCE_CANNOT_QUOTE, "evidence row is not marked can_quote")
    elif purpose_key == PURPOSE_HOLD and not resolved.can_hold:
        capability_code = (CODE_EVIDENCE_CANNOT_HOLD, "evidence row is not marked can_hold")
    elif purpose_key == PURPOSE_CONFIRM and not resolved.can_confirm:
        capability_code = (
            CODE_EVIDENCE_CANNOT_CONFIRM,
            "evidence row is not marked can_confirm (level A/B only)",
        )
    if capability_code is not None:
        return _deny(
            gate,
            capability_code[0],
            capability_code[1],
            {"evidence_id": resolved.evidence_id, "evidence_level": level, "purpose": purpose_key},
        )

    details: dict[str, Any] = {
        "evidence_id": resolved.evidence_id,
        "evidence_level": level,
        "source_type": source,
        "captured_at": to_iso(captured),
        "age_seconds": int(age.total_seconds()),
        "window_seconds": window_seconds,
    }
    if level == "C":
        # POLICY_MATRIX §6: C is indicative only and may never confirm.
        details["indicative"] = True
    return _allow(gate, details=details)


# --------------------------------------------------------------------------- #
# Data classification gate — anything leaving the system
# --------------------------------------------------------------------------- #
CLASSIFICATIONS = ("P0", "P1", "P2", "P3", "P4")

DESTINATION_STORE = "store"
DESTINATION_LLM = "llm"
DESTINATION_LOG = "log"
DESTINATION_AUDIT = "audit"
DESTINATION_EMAIL = "email"
DESTINATION_EXPORT = "export"
DESTINATION_CHAT = "chat"

#: Sinks where "id only, never content" applies (DATA_CLASSIFICATION §2).
LOGGING_DESTINATIONS = (DESTINATION_LOG, DESTINATION_AUDIT)

DESTINATIONS = (
    DESTINATION_STORE,
    DESTINATION_LLM,
    DESTINATION_LOG,
    DESTINATION_AUDIT,
    DESTINATION_EMAIL,
    DESTINATION_EXPORT,
    DESTINATION_CHAT,
)

#: Verdict per (destination, class):
#:   ``allow``  — may leave as-is
#:   ``redact`` — only pseudonymised/redacted values may leave
#:   ``auth``   — needs a recorded role (and, for P3 exports, a justification)
#:   ``deny``   — never leaves
POLICY_TABLE: Mapping[str, Mapping[str, str]] = {
    DESTINATION_STORE: {"P0": "allow", "P1": "allow", "P2": "allow", "P3": "allow", "P4": "deny"},
    DESTINATION_LLM: {"P0": "allow", "P1": "allow", "P2": "redact", "P3": "deny", "P4": "deny"},
    DESTINATION_LOG: {"P0": "allow", "P1": "allow", "P2": "deny", "P3": "deny", "P4": "deny"},
    DESTINATION_AUDIT: {"P0": "allow", "P1": "allow", "P2": "deny", "P3": "deny", "P4": "deny"},
    DESTINATION_EMAIL: {"P0": "allow", "P1": "allow", "P2": "auth", "P3": "deny", "P4": "deny"},
    DESTINATION_EXPORT: {"P0": "allow", "P1": "allow", "P2": "auth", "P3": "auth", "P4": "deny"},
    DESTINATION_CHAT: {"P0": "allow", "P1": "allow", "P2": "deny", "P3": "deny", "P4": "deny"},
}

#: Who may export/email which class (DATA_CLASSIFICATION §2).
P2_AUTH_ROLES = ("manager", "admin", "finance")
P3_AUTH_ROLES = ("admin", "finance")

#: Detectors for direct identifiers. Deliberately over-matching (a long digit run
#: is treated as an identifier) — the fail-closed side of a false positive is a
#: refused message, the other side is a leak.
IDENTIFIER_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?:\+?62|0)8[1-9][0-9]{6,11}")),
    ("nik", re.compile(r"\b\d{16}\b")),
    ("card", re.compile(r"(?:\d[ \-]?){13,19}")),
)


@dataclass(frozen=True)
class ClassifiedField:
    """One value on its way out, with the classification it was declared under."""

    name: str
    classification: str = ""
    value: Any = None
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "classification": self.classification,
            "redacted": self.redacted,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ClassifiedField":
        return cls(
            name=str(row.get("name") or row.get("field") or ""),
            classification=row.get("classification") or row.get("class") or "",
            value=row.get("value"),
            redacted=_flag(row.get("redacted")),
        )


def _identifier_kinds(value: Any) -> frozenset[str]:
    """Which direct-identifier patterns ``value`` carries (recursively for JSON)."""
    if value is None:
        return frozenset()
    if isinstance(value, bool):
        return frozenset()
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        try:
            text = canonical_json(value)
        except Exception:  # pragma: no cover - defensive: unknown object
            text = str(value)
    found = {name for name, pattern in IDENTIFIER_PATTERNS if pattern.search(text)}
    return frozenset(found)


def _unclassified(name: str, value: Any) -> ClassifiedField:
    return ClassifiedField(name=name, classification="", value=value)


def _prepare_fields(fields: Any) -> tuple[ClassifiedField, ...]:
    """Normalise the payload into classified fields; unclassified input stays
    unclassified (and therefore gets denied)."""
    if isinstance(fields, ClassifiedField):
        return (fields,)
    if isinstance(fields, Mapping):
        return tuple(
            fields[name]
            if isinstance(fields[name], ClassifiedField)
            else _unclassified(str(name), fields[name])
            for name in sorted(fields)
        )
    if isinstance(fields, Sequence) and not isinstance(fields, (str, bytes)):
        prepared: list[ClassifiedField] = []
        for index, entry in enumerate(fields):
            if isinstance(entry, ClassifiedField):
                prepared.append(entry)
            elif isinstance(entry, Mapping) and ("classification" in entry or "class" in entry):
                prepared.append(ClassifiedField.from_row(entry))
            else:
                prepared.append(_unclassified(f"field[{index}]", entry))
        return tuple(prepared)
    return (_unclassified("payload", fields),)


def _evaluate_field(
    field_item: ClassifiedField,
    destination: str,
    *,
    actor_role: Any,
    justification: Any,
) -> Optional[tuple[int, str, str]]:
    """``(severity, code, message)`` for the first violation of ``field_item``."""
    cls = _norm(field_item.classification)
    role = _text(actor_role).lower()
    if cls not in POLICY_TABLE[destination]:
        return (
            1,
            CODE_UNKNOWN_CLASSIFICATION,
            f"field {field_item.name!r} has no usable classification "
            f"({field_item.classification!r}): unclassified data never leaves",
        )
    verdict = POLICY_TABLE[destination][cls]
    identifiers = _identifier_kinds(field_item.value)

    if cls == "P4":
        return (
            0,
            CODE_P4_PROHIBITED,
            f"field {field_item.name!r} is P4 (card/OTP/password/key) and may never be stored or sent",
        )

    if cls == "P3":
        if verdict == "deny":
            return (
                1,
                CODE_P3_FORBIDDEN,
                f"P3 field {field_item.name!r} may not be sent to {destination}",
            )
        if verdict == "auth":
            if role not in P3_AUTH_ROLES:
                return (
                    2,
                    CODE_EXPORT_REQUIRES_ROLE,
                    f"P3 export of {field_item.name!r} needs one of {list(P3_AUTH_ROLES)}",
                )
            if not _text(justification):
                return (
                    2,
                    CODE_EXPORT_REQUIRES_JUSTIFICATION,
                    f"P3 export of {field_item.name!r} needs a recorded justification",
                )
        return None

    if cls == "P2":
        if verdict == "deny":
            if destination in LOGGING_DESTINATIONS:
                return (
                    3,
                    CODE_CONTENT_LOGGING_FORBIDDEN,
                    f"P2 field {field_item.name!r} may not be written to {destination}: logs carry ids only",
                )
            return (
                3,
                CODE_CHANNEL_FORBIDDEN,
                f"P2 field {field_item.name!r} may not leave through {destination}",
            )
        if verdict == "redact":
            if not field_item.redacted:
                return (
                    4,
                    CODE_PII_REDACTION_REQUIRED,
                    f"P2 field {field_item.name!r} must be pseudonymised/redacted before {destination}",
                )
            if identifiers:
                return (
                    3,
                    CODE_REDACTION_NOT_APPLIED,
                    f"P2 field {field_item.name!r} claims to be redacted but still carries {sorted(identifiers)}",
                )
        if verdict == "auth" and role not in P2_AUTH_ROLES:
            return (
                2,
                CODE_EXPORT_REQUIRES_ROLE,
                f"export of P2 field {field_item.name!r} needs one of {list(P2_AUTH_ROLES)}",
            )
        return None

    # P0 / P1: content that carries direct identifiers was misclassified.
    if identifiers and destination != DESTINATION_STORE:
        return (
            1,
            CODE_MISCLASSIFIED_CONTENT,
            f"field {field_item.name!r} is declared {cls} but carries {sorted(identifiers)}",
        )
    return None


def gate_outbound(
    fields: Any,
    *,
    destination: str,
    actor_role: Optional[str] = None,
    justification: Optional[str] = None,
) -> GateDecision:
    """Classification gate for anything leaving the system.

    ``fields`` are :class:`ClassifiedField` values (a mapping name → field also
    works). A value without a classification is denied — "no classification" is
    never a lower tier. Direct identifiers found in a P0/P1 field are denied as
    :data:`CODE_MISCLASSIFIED_CONTENT`, and a P2 field that claims to be redacted
    while still carrying an identifier is denied as
    :data:`CODE_REDACTION_NOT_APPLIED`.
    """
    gate = GATE_CLASSIFICATION
    destination_key = _norm(destination).lower()
    if destination_key not in POLICY_TABLE:
        return _deny(
            gate,
            CODE_UNKNOWN_DESTINATION,
            f"unknown destination {destination!r}: add it to POLICY_TABLE before sending",
            {"destination": destination, "known": list(DESTINATIONS)},
        )
    if fields is None or (isinstance(fields, (Sequence, Mapping)) and not fields):
        return _deny(
            gate,
            CODE_EMPTY_PAYLOAD,
            "nothing to classify: an outbound payload must carry classified fields",
            {"destination": destination_key},
        )

    prepared = _prepare_fields(fields)
    violations: list[tuple[int, str, str, str]] = []
    for entry in prepared:
        verdict = _evaluate_field(
            entry, destination_key, actor_role=actor_role, justification=justification
        )
        if verdict is not None:
            violations.append((verdict[0], entry.name, verdict[1], verdict[2]))
    if violations:
        violations.sort(key=lambda row: (row[0], row[1]))
        _severity, name, code, message = violations[0]
        return _deny(
            gate,
            code,
            message,
            {
                "destination": destination_key,
                "field": name,
                "violations": sorted({row[2] for row in violations}),
            },
        )
    return _allow(
        gate,
        details={"destination": destination_key, "fields": [entry.name for entry in prepared]},
    )


#: Alias: the docs call this "the PII/classification gate".
gate_classification = gate_outbound


def apply_redaction(fields: Any) -> tuple[ClassifiedField, ...]:
    """Redact P2 string values with :func:`primitives.redact` and mark them.

    Only string values are touched: a structured P2 value is left alone and the
    gate will deny it, which is safer than pretending a nested object was
    pseudonymised.
    """
    prepared = _prepare_fields(fields)
    out: list[ClassifiedField] = []
    for entry in prepared:
        if _norm(entry.classification) == "P2" and isinstance(entry.value, str):
            out.append(replace(entry, value=redact(entry.value), redacted=True))
        else:
            out.append(entry)
    return tuple(out)


# --------------------------------------------------------------------------- #
# Composite gate
# --------------------------------------------------------------------------- #
def gate_unit(
    *,
    now: Any,
    purpose: str,
    unit_status: str,
    pack: Any = None,
    evidence: Any = None,
    max_age_hours: Any = None,
    max_pack_age_hours: Any = None,
    approval_ref: Optional[str] = None,
) -> GateDecision:
    """May this unit be shown / quoted / held right now?

    Evaluates unit status → contract pack (Gate #0) → availability evidence and
    returns the first denial unchanged (``decision.gate`` tells you which gate
    refused). Fails closed on ``pack=None`` / ``evidence=None``.
    """
    gate = GATE_UNIT
    purpose_key = _norm(purpose).lower()
    if purpose_key not in PURPOSES:
        return _deny(
            gate,
            CODE_INVALID_PURPOSE,
            f"unknown purpose {purpose!r}: expected one of {list(PURPOSES)}",
            {"purpose": purpose},
        )
    moment = _parse_timestamp(now)
    if moment is None:
        return _deny(
            gate,
            CODE_INVALID_NOW,
            "now must be a timezone-aware datetime (UTC)",
            {"now": repr(now)},
        )

    status = _norm(unit_status)
    if status == "SUSPENDED":
        return _deny(
            gate,
            CODE_UNIT_SUSPENDED,
            "unit is suspended: it may not be shown, quoted or held",
            {"unit_status": unit_status},
        )
    if status == "MAINTENANCE":
        return _deny(
            gate,
            CODE_UNIT_IN_MAINTENANCE,
            "unit is in maintenance: it may not be shown, quoted or held",
            {"unit_status": unit_status},
        )
    if status not in UNIT_STATUSES:
        return _deny(
            gate,
            CODE_UNIT_STATUS_UNKNOWN,
            f"unit status {unit_status!r} is not a known status",
            {"unit_status": unit_status},
        )

    pack_decision = gate_contract_pack(
        pack,
        now=moment,
        purpose=purpose_key,
        max_pack_age_hours=max_pack_age_hours,
        approval_ref=approval_ref,
    )
    if not pack_decision.allowed:
        return pack_decision

    evidence_decision = gate_availability(
        evidence, now=moment, purpose=purpose_key, max_age_hours=max_age_hours
    )
    if not evidence_decision.allowed:
        return evidence_decision

    return _allow(
        gate,
        code=pack_decision.code,
        message=pack_decision.message,
        review_required=pack_decision.review_required,
        details={
            "contract_pack_code": pack_decision.code,
            "evidence_code": evidence_decision.code,
            "evidence_id": evidence_decision.details.get("evidence_id"),
            "review_required": pack_decision.review_required,
        },
    )


#: Name → gate, so callers can address gates by name without importing symbols.
GATES: Mapping[str, Any] = {
    GATE_CONTRACT_PACK: gate_contract_pack,
    GATE_AVAILABILITY: gate_availability,
    GATE_CLASSIFICATION: gate_outbound,
    GATE_UNIT: gate_unit,
}
