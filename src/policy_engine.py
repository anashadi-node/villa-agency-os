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

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, NoReturn, Optional, Sequence

from primitives import (
    DEFAULT_AGENCY_ID,
    DomainError,
    IdFactory,
    Money,
    SystemClock,
    canonical_json,
    decimal_of,
    parse_iso,
    parse_iso_date,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    "PolicyRules",
    "PolicyVersion",
    "PolicyRegistry",
    "POLICY_SCOPES",
    "PAYLOAD_MONEY_KEYS",
    "PAYLOAD_REQUIRED_KEYS",
    "parse_pct",
    "cancellation_step",
    # §16 governance half
    "PolicyDef",
    "PolicyValue",
    "PolicyResolver",
    "POLICY_DOMAINS",
    "POLICY_VALUE_TYPES",
    "POLICY_RISK_LEVELS",
    "POLICY_DEF_TABLE",
    "POLICY_REVISION_TABLE",
    "PolicyError",
    "UnknownPolicyKey",
    "PolicyValueNotPublished",
    "InvalidPolicyValue",
    "InvalidPolicyWindow",
    "PolicySourceRequired",
    "PolicyApproverRequired",
    "PolicyBackdatedRevision",
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
    def tax_applies_to(self) -> str:
        """``subtotal`` or ``subtotal_plus_service`` (POLICY_MATRIX; default the latter)."""
        return str(self.payload.get("tax_applies_to", DEFAULT_TAX_APPLIES_TO))

    @property
    def tax_included_in_rate(self) -> bool:
        """True when published rates already contain PBJT (it is then backed out)."""
        return bool(self.payload.get("tax_included_in_rate", False))

    @property
    def blackout_dates(self) -> tuple[str, ...]:
        return tuple(self.payload.get("blackout_dates", ()))

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
    _validate_optional_tax_keys(payload)
    return _freeze({key: payload[key] for key in sorted(payload)})



def cancellation_step(ladder: Any, *, notice_days: int) -> Optional[Mapping[str, Any]]:
    """The ladder step that governs ``notice_days`` before check-in, or ``None``.

    A ladder is the validated ``cancellation_ladder`` payload: a non-empty list of steps, each
    exactly ``{"min_days_before": int >= 0, "refund_pct": pct}``. The governing step is the one
    with the **largest** ``min_days_before`` the notice still meets, so a ladder whose thresholds
    are published out of order still reads correctly — the payload is validated for duplicates,
    never for order.

    ``None`` means "no step applies" (the notice sits inside every threshold) and it is **not** a
    zero refund: a refund is money, money is the finance module's fact and a person's decision,
    and turning a missing rule into a 0 % answer would invent a commercial term no tenant
    published.
    """

    if not isinstance(ladder, Sequence) or isinstance(ladder, (str, bytes)) or not ladder:
        return None
    days = int(notice_days)
    best: Optional[Mapping[str, Any]] = None
    best_days = -1
    for step in ladder:
        if not isinstance(step, Mapping):
            continue
        threshold = step.get("min_days_before")
        if isinstance(threshold, bool) or not isinstance(threshold, int):
            continue
        if threshold <= days and threshold > best_days:
            best, best_days = step, threshold
    return best


_ALLOWED_PAYLOAD_KEYS: tuple[str, ...] = (
    "extra_guest_fee_minor",
    "cleaning_fee_minor",
    "security_deposit_minor",
    "min_stay_nights",
    "cancellation_ladder",
    "checkin_time",
    "checkout_time",
    "notes",
    "tax_applies_to",
    "tax_included_in_rate",
    "blackout_dates",
)

#: Optional tax rules (POLICY_MATRIX §Pajak & fee; audit 2026-11-13, T1). Absent keys
#: keep the behaviour every existing policy was priced with, so no fingerprint and
#: no published price moves until a tenant publishes a new version that names them.
TAX_APPLIES_TO_VALUES: tuple[str, ...] = ("subtotal", "subtotal_plus_service")
DEFAULT_TAX_APPLIES_TO = "subtotal_plus_service"
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _validate_optional_tax_keys(payload: Mapping[str, Any]) -> None:
    applies_to = payload.get("tax_applies_to", DEFAULT_TAX_APPLIES_TO)
    if applies_to not in TAX_APPLIES_TO_VALUES:
        raise DomainError(
            f"payload.tax_applies_to must be one of {list(TAX_APPLIES_TO_VALUES)}",
            code="POLICY_PAYLOAD_INVALID",
            field="tax_applies_to",
        )
    included = payload.get("tax_included_in_rate", False)
    if not isinstance(included, bool):
        raise DomainError(
            "payload.tax_included_in_rate must be true or false",
            code="POLICY_PAYLOAD_INVALID",
            field="tax_included_in_rate",
        )
    blackout = payload.get("blackout_dates", [])
    if isinstance(blackout, (str, bytes)) or not isinstance(blackout, Sequence):
        raise DomainError(
            "payload.blackout_dates must be a list of YYYY-MM-DD dates",
            code="POLICY_PAYLOAD_INVALID",
            field="blackout_dates",
        )
    for day in blackout:
        if not isinstance(day, str) or not _ISO_DATE_RE.match(day):
            raise DomainError(
                "payload.blackout_dates must be a list of YYYY-MM-DD dates",
                code="POLICY_PAYLOAD_INVALID",
                field="blackout_dates",
            )
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise DomainError(
                f"payload.blackout_dates holds an impossible date: {day}",
                code="POLICY_PAYLOAD_INVALID",
                field="blackout_dates",
            ) from exc


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

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PolicyVersion":
        """The inverse of :meth:`as_row`: a stored row back into the version it came from.

        Every field is passed through the same ``__post_init__`` validation the writer
        went through, so a row that drifted cannot become an object that prices a stay.

        ``fingerprint`` is deliberately **not** read: it is a derived value, and a
        stored column that disagrees with the rules it claims to describe is exactly
        what a caller would want to notice. Re-derive it with :attr:`fingerprint` and
        compare against the column to check a row rather than trust it.
        """
        payload = row["payload"]
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    "policy payload is not readable JSON",
                    code="POLICY_PAYLOAD_UNREADABLE",
                    policy_version_id=str(row["policy_version_id"]),
                ) from exc
        if not isinstance(payload, Mapping):
            raise DomainError(
                "policy payload must be a JSON object",
                code="POLICY_PAYLOAD_INVALID",
                policy_version_id=str(row["policy_version_id"]),
            )
        return cls(
            policy_version_id=str(row["policy_version_id"]),
            property_id=str(row["property_id"]),
            unit_scope=str(row["unit_scope"]),
            version=int(row["version"]),
            effective_from=str(row["effective_from"]),
            effective_to=str(row["effective_to"]),
            rules=PolicyRules(
                currency=str(row["currency"]),
                deposit_pct=str(row["deposit_pct"]),
                service_fee_pct=str(row["service_fee_pct"]),
                tax_pbjt_pct=str(row["tax_pbjt_pct"]),
                max_discount_pct=str(row["max_discount_pct"]),
                quote_valid_hours=int(row["quote_valid_hours"]),
                hold_ttl_minutes=int(row["hold_ttl_minutes"]),
                max_hold_extensions=int(row["max_hold_extensions"]),
                payload=dict(payload),
            ),
            created_at=str(row["created_at"]),
            source_doc_ref=str(row["source_doc_ref"]),
            approved_by=str(row["approved_by"]),
        )


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


# --------------------------------------------------------------------------- #
# §16 Policy Engine — the governance half
# --------------------------------------------------------------------------- #
#
# ``PolicyRegistry`` above answers "what does this villa cost on this date". The
# governance half answers "what is the platform's rule on this date" for the
# numbers that are not per-villa: security windows, approval thresholds, evidence
# freshness, retention. §16 requires those to be first-class and versioned rather
# than constants scattered across the source, and §5 classes policy as VERSIONED:
# a change is a new version and the old one is preserved.
#
# Supersession differs from ``PolicyRegistry.active`` deliberately. That kernel
# refuses an overlapping window, because two prices for one night make a quote
# unreproducible. A governance value is different in kind: it *is* superseded by
# a later revision that starts while the old one is still nominally open, and the
# table is append-only, so an old row can never be closed after the fact. So here
# the covering revision with the highest version wins, publishing may never move
# a window backwards in time (which would silently rewrite what a past decision
# was allowed to do), and every decision can cite the revision it obeyed.

#: Domains a policy key may belong to (mirrors ``ck_policy_defs_domain``).
POLICY_DOMAINS: tuple[str, ...] = (
    "SECURITY",
    "FINANCE",
    "COMMERCIAL",
    "AVAILABILITY",
    "OPERATIONS",
    "RETENTION",
)

#: Declared value types (mirrors ``ck_policy_defs_value_type``).
POLICY_VALUE_TYPES: tuple[str, ...] = (
    "MINUTES",
    "HOURS",
    "DAYS",
    "COUNT",
    "PCT",
    "BOOL",
    "JSON",
    "ROLE_SET",
    "TEXT",
)

#: Risk levels, the same vocabulary §18/§56 use for roles and actions.
POLICY_RISK_LEVELS: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")

POLICY_DEF_TABLE = "policy_defs"
POLICY_REVISION_TABLE = "policy_revisions"

#: Types whose value is a non-negative integer.
_INT_TYPES: tuple[str, ...] = ("MINUTES", "HOURS", "DAYS", "COUNT")

#: The open end of a policy window: a value that never lapses by itself.
POLICY_OPEN_END = "9999-12-31"


class PolicyError(DomainError):
    """A deliberate refusal from the policy engine."""

    code = "POLICY_ERROR"


class UnknownPolicyKey(PolicyError):
    """The key is not in the catalogue; policy cannot be invented at call time."""

    code = "POLICY_UNKNOWN_KEY"


class PolicyValueNotPublished(PolicyError):
    """No revision covers this instant — a refusal, never a silent default."""

    code = "POLICY_VALUE_NOT_PUBLISHED"


class InvalidPolicyValue(PolicyError):
    """The value does not fit the type the catalogue declares for this key."""

    code = "POLICY_VALUE_INVALID"


class InvalidPolicyWindow(PolicyError):
    """A window that ends before it starts is not a window."""

    code = "POLICY_WINDOW_INVALID"


class PolicySourceRequired(PolicyError):
    """A value that cannot cite its document is an assumption, not a policy."""

    code = "POLICY_SOURCE_REQUIRED"


class PolicyApproverRequired(PolicyError):
    """Somebody has to own the number; an anonymous policy is not admissible."""

    code = "POLICY_APPROVER_REQUIRED"


class PolicyBackdatedRevision(PolicyError):
    """Versions only move forward, so history cannot be rewritten."""

    code = "POLICY_BACKDATED_REVISION"


@dataclass(frozen=True)
class PolicyDef:
    """One entry of the policy catalogue: the vocabulary, not the value."""

    policy_key: str
    domain: str
    value_type: str
    risk_level: str
    owner_role: str
    spec_ref: str
    description: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PolicyDef":
        return cls(
            policy_key=str(row["policy_key"]),
            domain=str(row["domain"]),
            value_type=str(row["value_type"]),
            risk_level=str(row["risk_level"]),
            owner_role=str(row["owner_role"]),
            spec_ref=str(row["spec_ref"]),
            description=str(row["description"]),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True)
class PolicyValue:
    """One published revision, addressable and reproducible.

    ``source_doc_ref`` and ``approved_by`` are required for the same reason
    ``PolicyVersion`` requires them: a number that cannot cite its document and
    the human who approved it is not a policy.
    """

    policy_revision_id: str
    agency_id: str
    policy_key: str
    version: int
    value: str
    value_type: str
    effective_from: str
    effective_to: str
    source_doc_ref: str
    approved_by: str
    fingerprint: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any], value_type: str) -> "PolicyValue":
        return cls(
            policy_revision_id=str(row["policy_revision_id"]),
            agency_id=str(row["agency_id"]),
            policy_key=str(row["policy_key"]),
            version=int(row["version"]),
            value=str(row["value"]),
            value_type=value_type,
            effective_from=str(row["effective_from"]),
            effective_to=str(row["effective_to"]),
            source_doc_ref=str(row["source_doc_ref"]),
            approved_by=str(row["approved_by"]),
            fingerprint=str(row["fingerprint"]),
            created_at=str(row["created_at"]),
        )

    def covers(self, on_date: str) -> bool:
        """True when ``on_date`` (ISO date) falls inside the inclusive window."""
        return self.effective_from <= on_date <= self.effective_to

    # -- typed reads ------------------------------------------------------- #
    def _require_type(self, *kinds: str) -> None:
        if self.value_type not in kinds:
            raise InvalidPolicyValue(
                f"{self.policy_key} is {self.value_type}, not {'/'.join(kinds)}",
                code="POLICY_VALUE_INVALID",
                policy_key=self.policy_key,
                value_type=self.value_type,
            )

    def as_int(self) -> int:
        self._require_type(*_INT_TYPES)
        return int(self.value)

    def as_decimal(self) -> Decimal:
        self._require_type("PCT")
        return Decimal(self.value)

    def as_bool(self) -> bool:
        self._require_type("BOOL")
        return self.value == "true"

    def as_text(self) -> str:
        self._require_type("TEXT")
        return self.value

    def as_json(self) -> Any:
        self._require_type("JSON", "ROLE_SET")
        return json.loads(self.value)

    def as_roles(self) -> tuple[str, ...]:
        self._require_type("ROLE_SET")
        return tuple(str(role) for role in json.loads(self.value))


class PolicyResolver:
    """Reads and publishes governance policy over one SQLite/PostgreSQL connection.

    Every read is agency-scoped with the platform baseline as the fallback: an
    agency that has published nothing inherits the baseline rather than silence.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Any = None,
        agency_id: str = DEFAULT_AGENCY_ID,
        ids: Optional[IdFactory] = None,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.agency_id = require_ref(agency_id, "agency_id")
        self.ids = ids or IdFactory()

    # -- plumbing ---------------------------------------------------------- #
    def _on_date(self, at: Any = None) -> str:
        """Resolve ``at`` to the calendar date a policy window is compared against."""
        if at is None:
            return self.clock.now().astimezone(UTC).date().isoformat()
        if isinstance(at, datetime):
            if at.tzinfo is None:
                raise InvalidPolicyWindow(
                    "at must carry a timezone", code="POLICY_WINDOW_INVALID"
                )
            return at.astimezone(UTC).date().isoformat()
        text = str(at)
        if "T" in text:
            return parse_iso(text).astimezone(UTC).date().isoformat()
        return parse_iso_date(text).isoformat()

    def _revision_rows(self, policy_key: str, *, agency_id: str) -> list[Mapping[str, Any]]:
        return list(
            self.conn.execute(
                f"SELECT * FROM {POLICY_REVISION_TABLE} WHERE policy_key = ? AND agency_id = ?"
                " ORDER BY version",
                (policy_key, agency_id),
            ).fetchall()
        )

    # -- reads ------------------------------------------------------------- #
    def catalogue(self) -> tuple[PolicyDef, ...]:
        rows = self.conn.execute(
            f"SELECT * FROM {POLICY_DEF_TABLE} ORDER BY domain, policy_key"
        ).fetchall()
        return tuple(PolicyDef.from_row(row) for row in rows)

    def definition(self, policy_key: str) -> PolicyDef:
        row = self.conn.execute(
            f"SELECT * FROM {POLICY_DEF_TABLE} WHERE policy_key = ?", (policy_key,)
        ).fetchone()
        if row is None:
            raise UnknownPolicyKey(
                f"unknown policy key: {policy_key}",
                code="POLICY_UNKNOWN_KEY",
                policy_key=policy_key,
            )
        return PolicyDef.from_row(row)

    def history(self, policy_key: str, *, agency_id: Optional[str] = None) -> tuple[PolicyValue, ...]:
        """Every revision this scope published for the key, oldest version first."""
        definition = self.definition(policy_key)
        scope = self.agency_id if agency_id is None else agency_id
        return tuple(
            PolicyValue.from_row(row, definition.value_type)
            for row in self._revision_rows(policy_key, agency_id=scope)
        )

    def resolve(self, policy_key: str, *, at: Any = None) -> PolicyValue:
        """The revision that governs ``at`` — the highest version that covers it."""
        definition = self.definition(policy_key)
        on_date = self._on_date(at)
        for scope in self._scopes():
            covering = [
                row
                for row in self._revision_rows(policy_key, agency_id=scope)
                if str(row["effective_from"]) <= on_date <= str(row["effective_to"])
            ]
            if covering:
                chosen = max(covering, key=lambda row: int(row["version"]))
                return PolicyValue.from_row(chosen, definition.value_type)
        raise PolicyValueNotPublished(
            f"no revision of {policy_key} covers {on_date}",
            code="POLICY_VALUE_NOT_PUBLISHED",
            policy_key=policy_key,
            agency_id=self.agency_id,
            on_date=on_date,
        )

    def _scopes(self) -> tuple[str, ...]:
        """Agency policy first, then the platform baseline."""
        if self.agency_id == DEFAULT_AGENCY_ID:
            return (DEFAULT_AGENCY_ID,)
        return (self.agency_id, DEFAULT_AGENCY_ID)

    # -- writes ------------------------------------------------------------ #
    def publish(
        self,
        policy_key: str,
        value: Any,
        *,
        effective_from: str,
        effective_to: str = POLICY_OPEN_END,
        source_doc_ref: str,
        approved_by: str,
        at: Any = None,
    ) -> PolicyValue:
        """Append a new revision. The previous one stays readable forever."""
        definition = self.definition(policy_key)
        text = self._validate_value(definition, value)
        if not str(source_doc_ref).strip():
            raise PolicySourceRequired(
                f"{policy_key} must cite the document it came from",
                code="POLICY_SOURCE_REQUIRED",
                policy_key=policy_key,
            )
        if not str(approved_by).strip():
            raise PolicyApproverRequired(
                f"{policy_key} must name the human who approved it",
                code="POLICY_APPROVER_REQUIRED",
                policy_key=policy_key,
            )
        source = require_text(source_doc_ref, "source_doc_ref", max_len=200)
        approver = require_ref(approved_by, "approved_by")
        start, end = parse_iso_date(effective_from), parse_iso_date(effective_to)
        if start > end:
            raise InvalidPolicyWindow(
                "effective_from must not be after effective_to",
                code="POLICY_WINDOW_INVALID",
                effective_from=effective_from,
                effective_to=effective_to,
            )
        existing = self.history(policy_key)
        if existing:
            newest_start = max(parse_iso_date(item.effective_from) for item in existing)
            if start < newest_start:
                raise PolicyBackdatedRevision(
                    f"{policy_key} already has a revision starting {newest_start.isoformat()}",
                    code="POLICY_BACKDATED_REVISION",
                    policy_key=policy_key,
                    effective_from=start.isoformat(),
                    newest_effective_from=newest_start.isoformat(),
                )
        version = max((item.version for item in existing), default=0) + 1
        revision_id = self.ids.new("polrev")
        window = {
            "agency_id": self.agency_id,
            "policy_key": policy_key,
            "version": version,
            "value": text,
            "effective_from": start.isoformat(),
            "effective_to": end.isoformat(),
            "source_doc_ref": source,
            "approved_by": approver,
        }
        created_at = to_iso(self.clock.now() if at is None else parse_iso(str(at)))
        self.conn.execute(
            f"INSERT INTO {POLICY_REVISION_TABLE} (policy_revision_id, agency_id, policy_key,"
            " version, value, effective_from, effective_to, source_doc_ref, approved_by,"
            " fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                self.agency_id,
                policy_key,
                version,
                text,
                start.isoformat(),
                end.isoformat(),
                source,
                approver,
                sha256_hex(canonical_json(window)),
                created_at,
            ),
        )
        return PolicyValue(
            policy_revision_id=revision_id,
            agency_id=self.agency_id,
            policy_key=policy_key,
            version=version,
            value=text,
            value_type=definition.value_type,
            effective_from=start.isoformat(),
            effective_to=end.isoformat(),
            source_doc_ref=source,
            approved_by=approver,
            fingerprint=sha256_hex(canonical_json(window)),
            created_at=created_at,
        )

    def _validate_value(self, definition: PolicyDef, value: Any) -> str:
        """Canonical text for ``value``, or a refusal if it does not fit the type."""
        kind = definition.value_type

        def refuse(reason: str) -> NoReturn:
            raise InvalidPolicyValue(
                f"{definition.policy_key}: {reason}",
                code="POLICY_VALUE_INVALID",
                policy_key=definition.policy_key,
                value_type=kind,
            )

        if kind in _INT_TYPES:
            try:
                number = int(str(value).strip())
            except (TypeError, ValueError):
                refuse(f"{value!r} is not an integer")
            if number < 0:
                refuse("a policy duration or count cannot be negative")
            return str(number)
        if kind == "PCT":
            text = str(value).strip()
            try:
                pct = parse_pct(text, field_name=definition.policy_key)
            except DomainError:
                refuse(f"{value!r} is not a percentage")
            if not (Decimal("0") <= pct <= Decimal("100")):
                refuse("a percentage must be between 0 and 100")
            return text
        if kind == "BOOL":
            text = str(value).strip().lower()
            if text not in ("true", "false"):
                refuse(f"{value!r} is not a boolean")
            return text
        if kind in ("JSON", "ROLE_SET"):
            payload = value
            if isinstance(value, str):
                try:
                    payload = json.loads(value)
                except json.JSONDecodeError:
                    refuse(f"{value!r} is not JSON")
            if kind == "ROLE_SET":
                if not isinstance(payload, list) or not payload:
                    refuse("a role set must be a non-empty list of roles")
                for role in payload:
                    if not isinstance(role, str) or not role.strip():
                        refuse("a role set may only contain role names")
            elif not isinstance(payload, (list, dict)):
                refuse("a JSON policy value must be a list or an object")
            return canonical_json(payload)
        if kind == "TEXT":
            text = str(value)
            if not text.strip():
                refuse("a text policy value cannot be empty")
            return require_text(text, "value", max_len=500)
        refuse(f"unsupported value type {kind!r}")
