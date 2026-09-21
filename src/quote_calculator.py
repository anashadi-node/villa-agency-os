"""Deterministic quote arithmetic.

A quote is a promise with a number on it, so this module is written to be
*boring*: every branch is explicit, nothing is guessed, and the same inputs
always produce the same totals and the same fingerprint.

Money rules
-----------
* Amounts are integer minor units end to end; percentages are exact decimals
  (``Decimal``) taken from the policy and rate card, never floats.
* Rounding is ``ROUND_HALF_UP`` and happens exactly once per derived amount, on
  the minor-unit value. ``deposit + balance`` always equals ``total`` exactly.
* The refundable security deposit is reported but never folded into the total.

Fail-closed rules
-----------------
* A night with no season rate stops the quote (``RATE_NOT_FOUND``) — a missing
  season is a contract gap, not a free night.
* Two seasons with the same winning priority for one night stops the quote
  (``RATE_AMBIGUOUS``): someone must fix the rate card.
* A discount above the policy ceiling stops the quote
  (``DISCOUNT_EXCEEDS_POLICY``) instead of being silently clamped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Optional, Sequence

from policy_engine import PolicyRules, PolicyVersion, parse_pct
from primitives import (
    DomainError,
    Money,
    canonical_json,
    nights_between,
    parse_iso_date,
    require_ref,
    require_text,
    sha256_hex,
    to_iso_date,
)

__all__ = [
    "Season",
    "RateCard",
    "UnitFacts",
    "QuoteRequest",
    "Quote",
    "calculate_quote",
    "LOS_MONTHLY_NIGHTS",
    "LOS_WEEKLY_NIGHTS",
]

#: Length-of-stay thresholds that unlock the rate card discounts.
LOS_WEEKLY_NIGHTS = 7
LOS_MONTHLY_NIGHTS = 28


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Season:
    """A dated nightly rate inside a rate card."""

    season_id: str
    name: str
    start_date: str
    end_date: str
    nightly_rate_minor: int
    min_stay_nights: int = 1
    priority: int = 0

    def __post_init__(self) -> None:
        require_ref(self.season_id, "season_id")
        require_text(self.name, "name", max_len=80)
        start, end = parse_iso_date(self.start_date), parse_iso_date(self.end_date)
        if start > end:
            raise DomainError(
                "season start_date must not be after end_date",
                code="SEASON_WINDOW_INVALID",
                season_id=self.season_id,
            )
        if isinstance(self.nightly_rate_minor, bool) or not isinstance(self.nightly_rate_minor, int):
            raise DomainError(
                "nightly_rate_minor must be an integer in minor units",
                code="SEASON_INVALID",
                season_id=self.season_id,
            )
        if self.nightly_rate_minor < 0:
            raise DomainError(
                "nightly_rate_minor must not be negative",
                code="SEASON_INVALID",
                season_id=self.season_id,
            )
        if isinstance(self.min_stay_nights, bool) or not isinstance(self.min_stay_nights, int) or self.min_stay_nights < 1:
            raise DomainError(
                "min_stay_nights must be an integer >= 1",
                code="SEASON_INVALID",
                season_id=self.season_id,
            )
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise DomainError(
                "priority must be an integer (higher wins)",
                code="SEASON_INVALID",
                season_id=self.season_id,
            )

    def covers_night(self, night: str) -> bool:
        """A night is covered inclusively: a season for 1-3 Oct prices 1, 2 and 3 Oct."""
        target = parse_iso_date(night)
        return parse_iso_date(self.start_date) <= target <= parse_iso_date(self.end_date)


@dataclass(frozen=True)
class RateCard:
    """A versioned rate card holding seasons (mirrors the ``rate_cards`` table)."""

    rate_card_id: str
    property_id: str
    unit_scope: str
    name: str
    version: int
    effective_from: str
    effective_to: str
    currency: str
    weekly_discount_pct: str = "0"
    monthly_discount_pct: str = "0"
    seasons: Sequence[Season] = field(default_factory=tuple)
    source_doc_ref: str = ""
    approved_by: str = ""

    def __post_init__(self) -> None:
        require_ref(self.rate_card_id, "rate_card_id")
        require_ref(self.property_id, "property_id")
        require_text(self.name, "name", max_len=80)
        if self.unit_scope not in ("property", "unit"):
            raise DomainError("unit_scope must be 'property' or 'unit'", code="RATE_CARD_INVALID")
        Money.zero(self.currency)
        object.__setattr__(
            self,
            "weekly_discount_pct",
            format(parse_pct(self.weekly_discount_pct, field_name="weekly_discount_pct", bounds=("0", "50")), "f"),
        )
        object.__setattr__(
            self,
            "monthly_discount_pct",
            format(parse_pct(self.monthly_discount_pct, field_name="monthly_discount_pct", bounds=("0", "60")), "f"),
        )
        start, end = parse_iso_date(self.effective_from), parse_iso_date(self.effective_to)
        if start > end:
            raise DomainError("rate card window is inverted", code="RATE_CARD_INVALID")
        # Provenance is mandatory (NOT NULL in the schema): a rate nobody approved
        # and whose source document is unknown cannot be sold from.
        require_text(self.source_doc_ref, "source_doc_ref", max_len=200)
        require_ref(self.approved_by, "approved_by")
        seasons = tuple(sorted(self.seasons, key=lambda s: (s.start_date, s.end_date, s.priority, s.season_id)))
        for season in seasons:
            if not (self.effective_from <= season.start_date and season.end_date <= self.effective_to):
                raise DomainError(
                    "season falls outside the rate card validity window",
                    code="RATE_CARD_INVALID",
                    season_id=season.season_id,
                    card_window=(self.effective_from, self.effective_to),
                )
        object.__setattr__(self, "seasons", seasons)

    def winning_season(self, night: str) -> Season:
        """The season that prices ``night`` — highest priority, ties are an error."""
        covering = [s for s in self.seasons if s.covers_night(night)]
        if not covering:
            raise DomainError(
                "no season rate covers this night",
                code="RATE_NOT_FOUND",
                night=night,
                rate_card_id=self.rate_card_id,
            )
        best_priority = max(s.priority for s in covering)
        winners = [s for s in covering if s.priority == best_priority]
        if len(winners) > 1:
            raise DomainError(
                "two seasons claim the same priority for one night — refusing to guess the rate",
                code="RATE_AMBIGUOUS",
                night=night,
                rate_card_id=self.rate_card_id,
                season_ids=sorted(s.season_id for s in winners),
            )
        return winners[0]

    def min_stay_over(self, nights: Sequence[str]) -> int:
        """Strictest minimum stay across every season that prices the stay."""
        return max([1] + [self.winning_season(night).min_stay_nights for night in nights])

    def fingerprint(self) -> str:
        payload = {
            "rate_card_id": self.rate_card_id,
            "property_id": self.property_id,
            "unit_scope": self.unit_scope,
            "version": self.version,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "currency": self.currency,
            "weekly_discount_pct": self.weekly_discount_pct,
            "monthly_discount_pct": self.monthly_discount_pct,
            "seasons": [
                {
                    "season_id": s.season_id,
                    "name": s.name,
                    "start_date": s.start_date,
                    "end_date": s.end_date,
                    "nightly_rate_minor": s.nightly_rate_minor,
                    "min_stay_nights": s.min_stay_nights,
                    "priority": s.priority,
                }
                for s in self.seasons
            ],
        }
        return sha256_hex("rate-card", canonical_json(payload))


@dataclass(frozen=True)
class UnitFacts:
    """The capacity facts a quote needs from the inventory side."""

    unit_id: str
    property_id: str
    base_capacity: int
    max_guests: int

    def __post_init__(self) -> None:
        require_ref(self.unit_id, "unit_id")
        require_ref(self.property_id, "property_id")
        for name in ("base_capacity", "max_guests"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise DomainError(
                    f"{name} must be an integer >= 1", code="CAPACITY_INVALID", field=name
                )
        if self.max_guests < self.base_capacity:
            raise DomainError(
                "max_guests must be >= base_capacity",
                code="CAPACITY_INVALID",
                base_capacity=self.base_capacity,
                max_guests=self.max_guests,
            )


@dataclass(frozen=True)
class QuoteRequest:
    """Everything the caller asks for — the only inputs that may influence price."""

    quote_date: str
    check_in: str
    check_out: str
    adults: int
    children: int = 0
    infants: int = 0
    extras: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    discount_minor: int = 0
    discount_reason: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("check_in", "check_out", "quote_date"):
            parse_iso_date(getattr(self, name))
        # Fail at construction, not deep inside pricing: a non-positive stay is a
        # caller error and must never reach the rate loop.
        nights_between(parse_iso_date(self.check_in), parse_iso_date(self.check_out))
        for name in ("adults", "children", "infants"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DomainError(
                    f"{name} must be a non-negative integer", code="GUESTS_INVALID", field=name
                )
        if self.adults < 1:
            raise DomainError("at least one adult is required", code="GUESTS_INVALID", field="adults")
        if isinstance(self.discount_minor, bool) or not isinstance(self.discount_minor, int) or self.discount_minor < 0:
            raise DomainError(
                "discount_minor must be a non-negative integer in minor units",
                code="DISCOUNT_INVALID",
            )
        extras = [self._validate_extra(extra) for extra in self.extras]
        codes = [extra["code"] for extra in extras]
        duplicates = sorted({code for code in codes if codes.count(code) > 1})
        if duplicates:
            raise DomainError(
                "duplicate extra codes", code="EXTRAS_DUPLICATE", codes=duplicates
            )
        object.__setattr__(self, "extras", tuple(sorted(extras, key=lambda e: e["code"])))
        if self.discount_reason is not None:
            require_text(self.discount_reason, "discount_reason", max_len=200)

    @staticmethod
    def _validate_extra(extra: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(extra, Mapping) or set(extra) != {"code", "amount_minor"}:
            raise DomainError(
                "each extra needs exactly {code, amount_minor}",
                code="EXTRAS_INVALID",
                extra=dict(extra) if isinstance(extra, Mapping) else repr(extra),
            )
        code = require_ref(extra["code"], "extra.code", max_len=40)
        amount = extra["amount_minor"]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise DomainError(
                "extra amount_minor must be a non-negative integer",
                code="EXTRAS_INVALID",
                code_name=code,
            )
        return {"code": code, "amount_minor": amount}

    @property
    def nights(self) -> int:
        return nights_between(parse_iso_date(self.check_in), parse_iso_date(self.check_out))

    def night_dates(self) -> tuple[str, ...]:
        """Every night sold: check-in inclusive, check-out exclusive."""
        return tuple(_add_days(self.check_in, offset) for offset in range(self.nights))

    @property
    def paying_guests(self) -> int:
        """Infants stay free and are excluded from extra-guest charges."""
        return self.adults + self.children


def _add_days(iso_date: str, offset: int) -> str:
    """Add whole days to an ISO date (calendar-safe, no month-length arithmetic)."""
    return to_iso_date(parse_iso_date(iso_date) + timedelta(days=offset))


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Quote:
    """A priced, reproducible offer."""

    quote_id: str
    property_id: str
    unit_id: str
    check_in: str
    check_out: str
    nights: int
    adults: int
    children: int
    infants: int
    currency: str
    policy_version_id: str
    policy_fingerprint: str
    rate_card_id: str
    rate_card_fingerprint: str
    accommodation: Money
    extra_guest: Money
    cleaning: Money
    extras: Money
    discount: Money
    subtotal: Money
    service_fee: Money
    tax: Money
    total: Money
    security_deposit: Money
    deposit_due: Money
    balance_due: Money
    lines: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]
    fingerprint: str
    created_at: str
    expires_at: str

    # -- database-ready shape ---------------------------------------------- #
    def as_row(self) -> dict[str, Any]:
        """Row for the ``quotes`` table: minor units + canonical JSON."""
        return {
            "quote_id": self.quote_id,
            "property_id": self.property_id,
            "unit_id": self.unit_id,
            "check_in": self.check_in,
            "check_out": self.check_out,
            "nights": self.nights,
            "adults": self.adults,
            "children": self.children,
            "infants": self.infants,
            "currency": self.currency,
            "policy_version_id": self.policy_version_id,
            "policy_fingerprint": self.policy_fingerprint,
            "rate_card_id": self.rate_card_id,
            "accommodation_minor": self.accommodation.minor_units,
            "extra_guest_minor": self.extra_guest.minor_units,
            "cleaning_minor": self.cleaning.minor_units,
            "extras_minor": self.extras.minor_units,
            "discount_minor": self.discount.minor_units,
            "subtotal_minor": self.subtotal.minor_units,
            "service_fee_minor": self.service_fee.minor_units,
            "tax_minor": self.tax.minor_units,
            "total_minor": self.total.minor_units,
            "security_deposit_minor": self.security_deposit.minor_units,
            "lines": canonical_json(list(self.lines)),
            "fingerprint": self.fingerprint,
            "status": "DRAFT",
            "warnings": canonical_json(list(self.warnings)),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.as_row(),
            "deposit_due_minor": self.deposit_due.minor_units,
            "balance_due_minor": self.balance_due.minor_units,
            "rate_card_fingerprint": self.rate_card_fingerprint,
        }


# --------------------------------------------------------------------------- #
# Calculation
# --------------------------------------------------------------------------- #
def calculate_quote(
    request: QuoteRequest,
    policy: PolicyVersion,
    rate_card: RateCard,
    unit: UnitFacts,
    *,
    quote_id: str,
    issued_at: str,
) -> Quote:
    """Price the stay, or fail with a machine-readable reason.

    Order of operations (documented because changing it changes prices):

    1. validate capacity and minimum stay,
    2. price every night from the winning season,
    3. add extra-guest, cleaning and extra line fees,
    4. apply the length-of-stay discount to room revenue, then the requested discount,
    5. service fee on the discounted subtotal, tax (PBJT) on subtotal + service fee,
    6. split the deposit; ``deposit_due + balance_due == total`` exactly.
    """
    require_ref(quote_id, "quote_id")
    rules: PolicyRules = policy.rules
    warnings: list[str] = []

    if rate_card.property_id != unit.property_id:
        raise DomainError(
            "rate card belongs to a different property",
            code="RATE_CARD_MISMATCH",
            rate_card_property=rate_card.property_id,
            unit_property=unit.property_id,
        )
    if policy.currency != rate_card.currency:
        raise DomainError(
            "policy and rate card currencies differ",
            code="QUOTE_CURRENCY_MISMATCH",
            policy_currency=policy.currency,
            rate_card_currency=rate_card.currency,
        )
    currency = policy.currency

    # 1. capacity and minimum stay
    if request.paying_guests > unit.max_guests:
        raise DomainError(
            "party exceeds the unit's maximum occupancy",
            code="CAPACITY_EXCEEDED",
            guests=request.paying_guests,
            max_guests=unit.max_guests,
        )
    nights = request.nights
    night_dates = request.night_dates()
    required_min = max(rules.min_stay_nights, rate_card.min_stay_over(night_dates))
    if nights < required_min:
        raise DomainError(
            "stay is shorter than the minimum stay",
            code="STAY_TOO_SHORT",
            nights=nights,
            min_stay_nights=required_min,
        )

    # 2. price every night
    seasonal = [(night, rate_card.winning_season(night)) for night in night_dates]
    accommodation = Money.zero(currency)
    for _night, season in seasonal:
        accommodation = accommodation + Money.from_minor(season.nightly_rate_minor, currency)
    distinct_seasons = {season.season_id for _night, season in seasonal}
    if len(distinct_seasons) > 1:
        warnings.append("STAY_SPANS_SEASONS:" + ",".join(sorted(distinct_seasons)))

    # 3. per-night guest fee, cleaning, extras
    extra_guests = max(0, request.paying_guests - unit.base_capacity)
    extra_guest = rules.extra_guest_fee * extra_guests * nights
    if extra_guests:
        warnings.append(f"EXTRA_GUESTS:{extra_guests}")
    cleaning = rules.cleaning_fee
    extras = Money.zero(currency)
    for extra in request.extras:
        extras = extras + Money.from_minor(extra["amount_minor"], currency)

    pre_discount = accommodation + extra_guest + cleaning + extras

    # 4. discounts: length-of-stay first (room revenue only), then the explicit request
    los_pct = "0"
    if nights >= LOS_MONTHLY_NIGHTS:
        los_pct = rate_card.monthly_discount_pct
    elif nights >= LOS_WEEKLY_NIGHTS:
        los_pct = rate_card.weekly_discount_pct
    # A length-of-stay discount is a reward for a long booking, so it applies to
    # room revenue (nights + extra guests) — never to cleaning or extras, which are
    # pass-through costs that would otherwise be discounted out of existence.
    los_discount = (accommodation + extra_guest).pct(los_pct)
    if los_discount.is_positive:
        warnings.append(f"LOS_DISCOUNT_PCT:{los_pct}")

    after_los = pre_discount - los_discount
    ceiling = after_los.pct(rules.max_discount_pct)
    requested = Money.from_minor(request.discount_minor, currency)
    if requested.amount_minor > ceiling.amount_minor:
        raise DomainError(
            "requested discount exceeds the policy ceiling",
            code="DISCOUNT_EXCEEDS_POLICY",
            requested_minor=requested.minor_units,
            ceiling_minor=ceiling.minor_units,
            max_discount_pct=rules.max_discount_pct,
        )
    discount = los_discount + requested
    if requested.is_positive and not request.discount_reason:
        warnings.append("DISCOUNT_WITHOUT_REASON")

    # 5. subtotal, service fee, tax
    subtotal = pre_discount - discount
    if subtotal.is_negative:
        raise DomainError(
            "discounts exceed the subtotal",
            code="DISCOUNT_EXCEEDS_SUBTOTAL",
            subtotal_minor=subtotal.minor_units,
        )
    service_fee = subtotal.pct(rules.service_fee_pct)
    tax = (subtotal + service_fee).pct(rules.tax_pbjt_pct)
    total = subtotal + service_fee + tax

    # 6. deposit split — exact by construction
    deposit_due = total.pct(rules.deposit_pct)
    balance_due = total - deposit_due

    lines = _build_lines(
        currency=currency,
        seasonal=seasonal,
        extras=request.extras,
        extra_guests=extra_guests,
        extra_guest_fee=rules.extra_guest_fee,
        cleaning=cleaning,
        los_discount=los_discount,
        requested_discount=requested,
        subtotal=subtotal,
        service_fee=service_fee,
        tax=tax,
        total=total,
        security_deposit=rules.security_deposit,
        service_fee_pct=rules.service_fee_pct,
        tax_pct=rules.tax_pbjt_pct,
    )

    fingerprint = _fingerprint(
        quote_id=quote_id,
        request=request,
        unit=unit,
        policy=policy,
        rate_card=rate_card,
        total_minor=total.minor_units,
    )

    return Quote(
        quote_id=quote_id,
        property_id=unit.property_id,
        unit_id=unit.unit_id,
        check_in=request.check_in,
        check_out=request.check_out,
        nights=nights,
        adults=request.adults,
        children=request.children,
        infants=request.infants,
        currency=currency,
        policy_version_id=policy.policy_version_id,
        policy_fingerprint=policy.fingerprint,
        rate_card_id=rate_card.rate_card_id,
        rate_card_fingerprint=rate_card.fingerprint(),
        accommodation=accommodation,
        extra_guest=extra_guest,
        cleaning=cleaning,
        extras=extras,
        discount=discount,
        subtotal=subtotal,
        service_fee=service_fee,
        tax=tax,
        total=total,
        security_deposit=rules.security_deposit,
        deposit_due=deposit_due,
        balance_due=balance_due,
        lines=tuple(lines),
        warnings=tuple(sorted(set(warnings))),
        fingerprint=fingerprint,
        created_at=issued_at,
        expires_at=policy.quote_expiry(issued_at),
    )


def _line(currency: str, code: str, description: str, amount: Money, **extra: Any) -> dict[str, Any]:
    return {
        "code": code,
        "description": description,
        "amount_minor": amount.minor_units,
        "currency": currency,
        **extra,
    }


def _build_lines(
    *,
    currency: str,
    seasonal: Sequence[tuple[str, Season]],
    extras: Sequence[Mapping[str, Any]],
    extra_guests: int,
    extra_guest_fee: Money,
    cleaning: Money,
    los_discount: Money,
    requested_discount: Money,
    subtotal: Money,
    service_fee: Money,
    tax: Money,
    total: Money,
    security_deposit: Money,
    service_fee_pct: str,
    tax_pct: str,
) -> list[dict[str, Any]]:
    """Ordered line items: nights first, then fees, discounts, then totals."""
    lines: list[dict[str, Any]] = []
    for night, season in seasonal:
        lines.append(
            _line(
                currency,
                f"night:{night}",
                f"{season.name} night",
                Money.from_minor(season.nightly_rate_minor, currency),
                season_id=season.season_id,
            )
        )
    if extra_guests:
        lines.append(
            _line(
                currency,
                "extra_guests",
                f"{extra_guests} guest(s) above base capacity",
                extra_guest_fee * extra_guests * len(seasonal),
                guests=extra_guests,
            )
        )
    if cleaning.is_positive:
        lines.append(_line(currency, "cleaning", "Cleaning fee", cleaning))
    for extra in extras:
        lines.append(
            _line(currency, f"extra:{extra['code']}", f"Extra: {extra['code']}",
                  Money.from_minor(extra["amount_minor"], currency))
        )
    if los_discount.is_positive:
        lines.append(_line(currency, "discount:los", "Length-of-stay discount", -los_discount))
    if requested_discount.is_positive:
        lines.append(_line(currency, "discount:requested", "Requested discount", -requested_discount))
    lines.append(_line(currency, "subtotal", "Subtotal", subtotal))
    lines.append(
        _line(currency, "service_fee", f"Service fee {service_fee_pct}%", service_fee)
    )
    lines.append(_line(currency, "tax_pbjt", f"PBJT {tax_pct}%", tax))
    lines.append(_line(currency, "total", "Total", total))
    if security_deposit.is_positive:
        lines.append(
            _line(currency, "security_deposit", "Refundable security deposit", security_deposit,
                  refundable=True)
        )
    return lines


def _fingerprint(
    *,
    quote_id: str,
    request: QuoteRequest,
    unit: UnitFacts,
    policy: PolicyVersion,
    rate_card: RateCard,
    total_minor: int,
) -> str:
    """Hash of everything that influenced the price — enables bit-exact replay."""
    return sha256_hex(
        "quote",
        canonical_json(
            {
                "quote_id": quote_id,
                "quote_date": request.quote_date,
                "check_in": request.check_in,
                "check_out": request.check_out,
                "adults": request.adults,
                "children": request.children,
                "infants": request.infants,
                "extras": [dict(extra) for extra in request.extras],
                "discount_minor": request.discount_minor,
                "unit": {
                    "unit_id": unit.unit_id,
                    "base_capacity": unit.base_capacity,
                    "max_guests": unit.max_guests,
                },
                "policy_version_id": policy.policy_version_id,
                "policy_fingerprint": policy.fingerprint,
                "rate_card_fingerprint": rate_card.fingerprint(),
                "total_minor": total_minor,
            }
        ),
    )
