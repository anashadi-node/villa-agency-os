"""Versioned policy objects — the single home for every business number.

A quote is only reproducible if the rules that produced it are immutable and
identifiable. So prices, deposit percentages, taxes, cancellation ladders and
fee amounts never live in code and never live in a per-booking column: they live
in one versioned, fingerprinted object (see ``docs/POLICY_MATRIX.md``).

Two invariants this module enforces:

* **Percentages are exact decimals, never floats.** They arrive as strings
  (``"30"``, ``"7.5"``) and are stored as decimal text, matching the ``TEXT``
  columns and the database CHECK bounds.
* **A policy window is unambiguous.** For one property/scope and one calendar
  date, exactly one version may apply. Two overlapping windows are a data error,
  not a "pick the newest" situation, because silently picking one changes prices.

Money amounts inside the free-form ``payload`` are integer minor units, so the
kernel can never introduce a rounding difference the database did not see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from primitives import (
    UTC,
    DomainError,
    Money,
    canonical_json,
    decimal_of,
    parse_iso,
    parse_iso_date,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
    to_iso_date,
)

__all__ = [
    "PolicyRules",
    "PolicyVersion",
    "PolicyRegistry",
    "POLICY_SCOPES",
    "PAYLOAD_MONEY_KEYS",
    "PAYLOAD_REQUIRED_KEYS",
    "parse_pct",
]

#: The only two scopes the schema allows (``ck_policy_scope``).
POLICY_SCOPES: tuple[str, ...] = ("property", "unit")

#: Percentage bounds, mirroring the CHECK constraints in ``001_initial.sql``.
#: The database is the second line of defence; this is the first, with a better
#: error message and a machine-readable code.
_PCT_BOUNDS: Mapping[str, tuple[str, str]] = {
    "deposit_pct": ("0", "100"),
    "service_fee_pct": ("0", "25"),
    "tax_pbjt_pct": ("0", "10"),
    "max_discount_pct": ("0", "50"),
}

#: Integer limits and their allowed ranges, mirroring the CHECK constraints on
#: ``policy_versions`` in ``001_initial.sql`` (asserted against the schema in
#: ``tests/test_policy_engine.py``). ``max_hold_extensions = 0`` is legal: a
#: policy may forbid extensions outright.
_LIMIT_BOUNDS: Mapping[str, tuple[int, int]] = {
    "quote_valid_hours": (1, 168),
    "hold_ttl_minutes": (15, 1440),
    "max_hold_extensions": (0, 3),
}

#: ``payload`` keys whose value must be an integer amount in minor units.
PAYLOAD_MONEY_KEYS: tuple[str, ...] = (
    "extra_guest_fee_minor",
    "cleaning_fee_minor",
    "security_deposit_minor",
)

#: ``payload`` keys that must be present for a policy to be usable.
PAYLOAD_REQUIRED_KEYS: tuple[str, ...] = (
    "extra_guest_fee_minor",
    "cleaning_fee_minor",
    "security_deposit_minor",
    "min_stay_nights",
    "cancellation_ladder",
)

_PCT_RE = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,4})?$")


def parse_pct(value: Any, *, field_name: str, bounds: Optional[tuple[str, str]] = None) -> Decimal:
    """Parse an exact percentage from ``str``/``int``/``Decimal`` (never float).

    Rejects negatives, exponential notation and stray characters: a percentage
    that cannot be read exactly must stop the quote, not be guessed at.
    """
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, bool):
        raise DomainError(
            f"{field_name} must be a percentage, not a bool",
            code="POLICY_INVALID_PERCENT",
            field=field_name,
        )
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise DomainError(
            f"{field_name} must be a str/int/Decimal percentage (float forbidden)",
            code="POLICY_INVALID_PERCENT",
            field=field_name,
        )
    if not _PCT_RE.match(text):
        raise DomainError(
            f"{field_name} is not a plain non-negative percentage: {value!r}",
            code="POLICY_INVALID_PERCENT",
            field=field_name,
        )
    dec = decimal_of(text)
    if bounds is not None:
        low, high = bounds
        if dec < decimal_of(low) or dec > decimal_of(high):
            raise DomainError(
                f"{field_name} must be between {low} and {high} (got {text})",
                code="POLICY_PERCENT_OUT_OF_RANGE",
                field=field_name,
                value=text,
            )
    return dec


# --------------------------------------------------------------------------- #
# The rule set (everything that shapes a price, and nothing else)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolicyRules:
    """Rule-bearing fields of a policy version.

    Deliberately excludes ``policy_version_id``, ``created_at``, ``approved_by``
    and ``source_doc_ref``: those are provenance, not rules, and must not move
    the fingerprint (re-approving the same rules yields the same fingerprint).
    """

    currency: str
    deposit_pct: str
    service_fee_pct: str
    tax_pbjt_pct: str
    max_discount_pct: str
    quote_valid_hours: int
    hold_ttl_minutes: int
    max_hold_extensions: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate currency by constructing a zero amount: reuses the ISO-4217 table.
        Money.zero(self.currency)
        for name, bounds in _PCT_BOUNDS.items():
            object.__setattr__(
                self, name, format(parse_pct(getattr(self, name), field_name=name, bounds=bounds), "f")
            )
        for name, (low, high) in _LIMIT_BOUNDS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise DomainError(
                    f"{name} must be an integer (got {value!r})",
                    code="POLICY_INVALID_LIMIT",
                    field=name,
                )
            if not low <= value <= high:
                raise DomainError(
                    f"{name} must be between {low} and {high} (got {value})",
                    code="POLICY_LIMIT_OUT_OF_RANGE",
                    field=name,
                    value=value,
                    bounds=(low, high),
                )
        object.__setattr__(self, "payload", _validate_payload(self.payload))

    # -- typed accessors ---------------------------------------------------- #
    @property
    def deposit_ratio(self) -> Decimal:
        return decimal_of(self.deposit_pct) / Decimal(100)

    @property
    def service_fee_ratio(self) -> Decimal:
        return decimal_of(self.service_fee_pct) / Decimal(100)

    @property
    def tax_ratio(self) -> Decimal:
        return decimal_of(self.tax_pbjt_pct) / Decimal(100)

    @property
    def max_discount_ratio(self) -> Decimal:
        return decimal_of(self.max_discount_pct) / Decimal(100)

    @property
    def min_stay_nights(self) -> int:
        return int(self.payload["min_stay_nights"])

    @property
    def extra_guest_fee(self) -> Money:
        return Money.from_minor(self.payload["extra_guest_fee_minor"], self.currency)

    @property
    def cleaning_fee(self) -> Money:
        return Money.from_minor(self.payload["cleaning_fee_minor"], self.currency)

    @property
    def security_deposit(self) -> Money:
        return Money.from_minor(self.payload["security_deposit_minor"], self.currency)

    def as_dict(self) -> dict[str, Any]:
        """A plain, deep-copied snapshot (safe for callers to mutate freely)."""
        return {
            "currency": self.currency,
            "deposit_pct": self.deposit_pct,
            "service_fee_pct": self.service_fee_pct,
            "tax_pbjt_pct": self.tax_pbjt_pct,
            "max_discount_pct": self.max_discount_pct,
            "quote_valid_hours": self.quote_valid_hours,
            "hold_ttl_minutes": self.hold_ttl_minutes,
            "max_hold_extensions": self.max_hold_extensions,
            "payload": _thaw(self.payload),
        }

    def fingerprint(self) -> str:
        """Content hash of the rule set (domain-separated, key-order stable)."""
        return sha256_hex("policy-rules", canonical_json(self.as_dict()))


class _FrozenDict(dict):
    """A ``dict`` that refuses mutation.

    Needed because the payload participates in the fingerprint: if a caller could
    mutate it after construction, the stored fingerprint would describe rules that
    no longer exist. Subclasses ``dict`` so ``json.dumps``/``canonical_json`` keep
    working unchanged.
    """

    def _immutable(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("policy payload is immutable; build a new PolicyRules instead")

    # Deliberate signature mismatches: the point is to make these operations raise.
    __setitem__ = _immutable  # type: ignore[assignment]
    __delitem__ = _immutable  # type: ignore[assignment]
    pop = _immutable  # type: ignore[assignment]
    popitem = _immutable  # type: ignore[assignment]
    clear = _immutable  # type: ignore[assignment]
    update = _immutable  # type: ignore[assignment]
    setdefault = _immutable  # type: ignore[assignment]


def _freeze(value: Any) -> Any:
    """Recursively freeze mappings into :class:`_FrozenDict` and lists into tuples."""
    if isinstance(value, Mapping):
        return _FrozenDict({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Inverse of :func:`_freeze`: hand callers plain, copyable structures."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


_REQUIRED_LADDER_KEYS = ("min_days_before", "refund_pct")


def _validate_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the free-form policy payload, failing closed on anything unknown.

    An unknown key is almost always a typo (``extra_guest_fee`` instead of
    ``extra_guest_fee_minor``) that would silently price a stay at zero. Rejecting
    it is the only safe behaviour.
    """
    if not isinstance(payload, Mapping):
        raise DomainError("payload must be a mapping", code="POLICY_PAYLOAD_INVALID")
    missing = [key for key in PAYLOAD_REQUIRED_KEYS if key not in payload]
    if missing:
        raise DomainError(
            f"payload is missing required keys: {missing}",
            code="POLICY_PAYLOAD_INCOMPLETE",
            missing=missing,
        )
    unknown = [key for key in payload if key not in _ALLOWED_PAYLOAD_KEYS]
    if unknown:
        raise DomainError(
            f"payload has unknown keys: {unknown}",
            code="POLICY_PAYLOAD_UNKNOWN_KEY",
            unknown=sorted(unknown),
        )
    for key in PAYLOAD_MONEY_KEYS:
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DomainError(
                f"payload.{key} must be a non-negative integer in minor units (got {value!r})",
                code="POLICY_PAYLOAD_INVALID",
                field=key,
            )
    nights = payload["min_stay_nights"]
    if isinstance(nights, bool) or not isinstance(nights, int) or nights < 1:
        raise DomainError(
            "payload.min_stay_nights must be an integer >= 1",
            code="POLICY_PAYLOAD_INVALID",
            field="min_stay_nights",
        )
    ladder = payload["cancellation_ladder"]
    if not isinstance(ladder, Sequence) or isinstance(ladder, (str, bytes)) or not ladder:
        raise DomainError(
            "payload.cancellation_ladder must be a non-empty list",
            code="POLICY_PAYLOAD_INVALID",
            field="cancellation_ladder",
        )
    seen_days: set[int] = set()
    for step in ladder:
        if not isinstance(step, Mapping) or set(step) != set(_REQUIRED_LADDER_KEYS):
            raise DomainError(
                f"each cancellation step needs exactly {list(_REQUIRED_LADDER_KEYS)}",
                code="POLICY_PAYLOAD_INVALID",
                field="cancellation_ladder",
                step=dict(step) if isinstance(step, Mapping) else repr(step),
            )
        days = step["min_days_before"]
        if isinstance(days, bool) or not isinstance(days, int) or days < 0:
            raise DomainError(
                "cancellation step min_days_before must be an integer >= 0",
                code="POLICY_PAYLOAD_INVALID",
                field="cancellation_ladder",
            )
        if days in seen_days:
            raise DomainError(
                f"duplicate cancellation step for {days} days before check-in",
                code="POLICY_PAYLOAD_INVALID",
                field="cancellation_ladder",
            )
        seen_days.add(days)
        parse_pct(
            step["refund_pct"],
            field_name="cancellation.refund_pct",
            bounds=("0", "100"),
        )
    return _freeze({key: payload[key] for key in sorted(payload)})


_ALLOWED_PAYLOAD_KEYS: tuple[str, ...] = (
    "extra_guest_fee_minor",
    "cleaning_fee_minor",
    "security_deposit_minor",
    "min_stay_nights",
    "cancellation_ladder",
    "checkin_time",
    "checkout_time",
    "notes",
)


# --------------------------------------------------------------------------- #
# A stored version
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolicyVersion:
    """One immutable policy version, addressable and reproducible.

    ``source_doc_ref`` and ``approved_by`` are **required**: a price rule that
    cannot cite the document it came from and the human who approved it is not a
    policy, it is an assumption. The schema makes both columns NOT NULL for the
    same reason.
    """

    policy_version_id: str
    property_id: str
    unit_scope: str
    version: int
    effective_from: str
    effective_to: str
    rules: PolicyRules
    created_at: str
    source_doc_ref: str
    approved_by: str

    def __post_init__(self) -> None:
        require_ref(self.policy_version_id, "policy_version_id")
        require_ref(self.property_id, "property_id")
        if self.unit_scope not in POLICY_SCOPES:
            raise DomainError(
                f"unit_scope must be one of {list(POLICY_SCOPES)} (got {self.unit_scope!r})",
                code="POLICY_INVALID_SCOPE",
                unit_scope=self.unit_scope,
            )
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise DomainError("version must be an integer >= 1", code="POLICY_INVALID_VERSION")
        if not isinstance(self.rules, PolicyRules):
            raise DomainError("rules must be a PolicyRules instance", code="POLICY_PAYLOAD_INVALID")
        start, end = parse_iso_date(self.effective_from), parse_iso_date(self.effective_to)
        if start > end:
            raise DomainError(
                "effective_from must not be after effective_to",
                code="POLICY_WINDOW_INVALID",
                effective_from=self.effective_from,
                effective_to=self.effective_to,
            )
        to_iso(parse_iso(self.created_at))
        require_text(self.source_doc_ref, "source_doc_ref", max_len=200)
        require_ref(self.approved_by, "approved_by")

    # -- helpers ------------------------------------------------------------ #
    @property
    def fingerprint(self) -> str:
        """Fingerprint of the rules only — the value a quote pins itself to."""
        return self.rules.fingerprint()

    @property
    def currency(self) -> str:
        return self.rules.currency

    def covers(self, on_date: str) -> bool:
        """True when ``on_date`` (ISO date) falls inside the inclusive window."""
        target = parse_iso_date(on_date)
        return parse_iso_date(self.effective_from) <= target <= parse_iso_date(self.effective_to)

    def quote_expiry(self, issued_at: str) -> str:
        """When a quote issued at ``issued_at`` stops being valid."""
        return to_iso(parse_iso(issued_at) + timedelta(hours=self.rules.quote_valid_hours))

    def as_row(self) -> dict[str, Any]:
        """Database-ready row: percentages as text, payload as canonical JSON."""
        return {
            "policy_version_id": self.policy_version_id,
            "property_id": self.property_id,
            "unit_scope": self.unit_scope,
            "version": self.version,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "currency": self.rules.currency,
            "deposit_pct": self.rules.deposit_pct,
            "service_fee_pct": self.rules.service_fee_pct,
            "tax_pbjt_pct": self.rules.tax_pbjt_pct,
            "max_discount_pct": self.rules.max_discount_pct,
            "quote_valid_hours": self.rules.quote_valid_hours,
            "hold_ttl_minutes": self.rules.hold_ttl_minutes,
            "max_hold_extensions": self.rules.max_hold_extensions,
            "payload": canonical_json(dict(self.rules.payload)),
            "fingerprint": self.fingerprint,
            "source_doc_ref": self.source_doc_ref,
            "approved_by": self.approved_by,
            "created_at": self.created_at,
        }


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #
class PolicyRegistry:
    """In-memory policy versions with fail-closed resolution.

    Used by tests and by the kernel's request path; the database remains the
    durable store (see ``src/reservation_kernel.py``).
    """

    def __init__(self, versions: Iterable[PolicyVersion] = ()) -> None:
        self._versions: list[PolicyVersion] = []
        for version in versions:
            self.add(version)

    def add(self, version: PolicyVersion) -> None:
        for existing in self._versions:
            if (
                existing.property_id == version.property_id
                and existing.unit_scope == version.unit_scope
                and existing.version == version.version
            ):
                raise DomainError(
                    "duplicate policy version",
                    code="POLICY_VERSION_DUPLICATE",
                    property_id=version.property_id,
                    version=version.version,
                )
        self._versions.append(version)

    def versions(self) -> tuple[PolicyVersion, ...]:
        return tuple(
            sorted(self._versions, key=lambda v: (v.property_id, v.unit_scope, v.version))
        )

    def active(self, property_id: str, unit_scope: str, on_date: str) -> PolicyVersion:
        """The single policy version covering ``on_date``.

        Ambiguity is an error: two versions covering the same date would make a
        quote unreproducible, so the kernel refuses to choose.
        """
        candidates = [
            version
            for version in self._versions
            if version.property_id == property_id
            and version.unit_scope == unit_scope
            and version.covers(on_date)
        ]
        if not candidates:
            raise DomainError(
                "no policy version covers this date",
                code="POLICY_NOT_FOUND",
                property_id=property_id,
                unit_scope=unit_scope,
                on_date=on_date,
            )
        if len(candidates) > 1:
            raise DomainError(
                "overlapping policy versions — refusing to guess which price applies",
                code="POLICY_WINDOW_OVERLAP",
                property_id=property_id,
                unit_scope=unit_scope,
                on_date=on_date,
                versions=[v.policy_version_id for v in candidates],
            )
        return candidates[0]

    def get(self, policy_version_id: str) -> PolicyVersion:
        for version in self._versions:
            if version.policy_version_id == policy_version_id:
                return version
        raise DomainError(
            f"unknown policy version: {policy_version_id}",
            code="POLICY_NOT_FOUND",
            policy_version_id=policy_version_id,
        )

    def resolve_for_unit(
        self, property_id: str, unit_id: str, on_date: str
    ) -> PolicyVersion:
        """Unit-level policy wins over property-level when both exist."""
        try:
            return self.active(property_id, "unit", on_date)
        except DomainError as exc:
            if exc.code != "POLICY_NOT_FOUND":
                raise
        return self.active(property_id, "property", on_date)
