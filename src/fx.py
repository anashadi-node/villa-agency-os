"""Currency conversion — turning an amount in one currency into another, honestly.

**Why this module exists.** Every amount in this platform is an integer of minor units *of a
named currency* (``primitives.Money``), and one number on its own carries no currency at all.
§11's discount guard publishes its absolute half as a single number —
``commercial.margin_floor_minor`` — so that number only means something together with the
currency it was declared in (``commercial.margin_floor_currency``). A booking priced in another
currency has to convert the floor before comparing it, and a conversion needs a rate that
someone published. Bali sells in USD, Yogyakarta may contract in IDR, so both cases are real.

**What this module refuses to do.**

* **It never assumes a rate.** No published pair → :class:`FxRateNotPublished`. A default of
  1:1 would read IDR rupiah as if they were dollars — exactly the failure this module exists to
  prevent, and one that *passes* every absolute floor instead of failing it.
* **It never uses floats.** ``primitives.decimal_of`` rejects them, so a rate written ``0.1``
  cannot arrive as the binary approximation that breaks money arithmetic.
* **It never rounds a minimum down.** The default rounding is ``ROUND_CEILING``: the callers
  here convert *minimums*, and a minimum rounded down would hand out margin the owner never
  allowed. A caller converting an amount merely to display it may pass ``ROUND_NEAREST``.
* **It never invents a currency.** ``Money`` validates both codes against the supported
  ISO-4217 table, so an unknown code refuses here instead of travelling on into a quote.

**Inverted pairs are derived, not published.** ``FxTable.rate`` answers a pair it was not given
by inverting the one it was, and :class:`FxRate` reports that through :attr:`FxRate.derived`.
Publish the direction you rely on when the exact number matters; the derivation exists so the
other direction keeps working without a second row that can drift out of step.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from typing import Any, Mapping

from primitives import DomainError, Money, decimal_of, decimal_to_str

#: §16 policy key holding the published rates. JSON: ``{"USD/IDR": "16250"}`` — the value is
#: read, never written, here.
FX_RATES_KEY = "commercial.fx.rates"

#: Separator inside a pair key. ``"USD/IDR"`` reads "one USD is worth this many IDR".
PAIR_SEPARATOR = "/"

#: Rounding for a *minimum*: a floor must never be rounded down.
ROUND_UP = ROUND_CEILING
#: Rounding for an amount that is being shown rather than guarded.
ROUND_NEAREST = ROUND_HALF_EVEN

#: The roundings a caller may ask for. Anything else refuses; there is no silent default.
ROUNDINGS: tuple[str, ...] = (ROUND_UP, ROUND_NEAREST)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
class FxError(DomainError):
    """Base for every conversion refusal."""

    code = "FX_ERROR"


class FxRateInvalid(FxError):
    """A published rate is not a positive exact decimal, or a pair key is malformed."""

    code = "FX_RATE_INVALID"


class FxRateNotPublished(FxError):
    """No rate for this pair exists — refuse, do not assume parity."""

    code = "FX_RATE_NOT_PUBLISHED"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalise_currency(currency: Any) -> str:
    """Validate and upper-case an ISO-4217 code through the money primitive.

    ``Money`` owns the currency table, so this module reads it rather than keeping a second
    copy that could drift: an unsupported code raises ``InvalidMoney`` here, at the boundary.
    """
    return Money.zero(currency).currency


def pair_key(base: Any, quote: Any) -> str:
    """``("USD", "idr")`` → ``"USD/IDR"``. Both codes are validated and normalised."""
    return f"{normalise_currency(base)}{PAIR_SEPARATOR}{normalise_currency(quote)}"


def _flip(key: str) -> str:
    """The opposite direction of an already-normalised pair key."""
    base, quote = key.split(PAIR_SEPARATOR)
    return f"{quote}{PAIR_SEPARATOR}{base}"


def parse_rate(value: Any, *, pair: str) -> Decimal:
    """Read one published rate: a positive exact decimal, never a float, never zero.

    ``decimal_of`` owns the float refusal; it is re-raised as :class:`FxRateInvalid` so a caller
    catching ``FxError`` cannot miss a rate that arrived as a binary approximation.
    """
    try:
        rate = decimal_of(value)
    except DomainError as not_a_number:
        raise FxRateInvalid(
            f"a rate must be an exact decimal: {pair} was published as {value!r}",
            pair=pair,
            value=repr(value),
            reason=not_a_number.code,
        ) from not_a_number
    if rate <= 0:
        raise FxRateInvalid(
            f"a rate must be positive: {pair} was published as {value!r}",
            pair=pair,
            rate=str(rate),
        )
    return rate


def parse_rates(payload: Any) -> dict[str, Decimal]:
    """Read the published JSON payload into ``{"USD/IDR": Decimal("16250")}``.

    Refuses anything that is not a non-empty object of ``"BASE/QUOTE"`` keys: a payload this
    module cannot read exactly is not something it will guess at.
    """
    if payload is None:
        raise FxRateNotPublished(
            "no FX rates published", policy_key=FX_RATES_KEY
        )
    if not isinstance(payload, Mapping) or not payload:
        raise FxRateInvalid(
            "published FX rates must be a non-empty object of 'BASE/QUOTE' keys",
            got=type(payload).__name__,
            policy_key=FX_RATES_KEY,
        )
    rates: dict[str, Decimal] = {}
    for raw_pair, raw_rate in payload.items():
        if not isinstance(raw_pair, str) or raw_pair.count(PAIR_SEPARATOR) != 1:
            raise FxRateInvalid(
                "a rate key must read BASE/QUOTE", pair=repr(raw_pair), policy_key=FX_RATES_KEY
            )
        base, quote = raw_pair.split(PAIR_SEPARATOR)
        try:
            key = pair_key(base, quote)
        except DomainError as bad_currency:
            raise FxRateInvalid(
                f"a rate key names an unsupported currency: {raw_pair!r}",
                pair=repr(raw_pair),
                reason=bad_currency.code,
            ) from bad_currency
        if key in rates:
            raise FxRateInvalid("two published keys name the same pair", pair=key)
        rates[key] = parse_rate(raw_rate, pair=key)
    return rates


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FxRate:
    """One direction: one unit of :attr:`base` is worth :attr:`rate` units of :attr:`quote`."""

    base: str
    quote: str
    rate: Decimal
    #: True when this direction was inverted from a published one rather than published itself.
    derived: bool = False

    def __post_init__(self) -> None:
        try:
            base = normalise_currency(self.base)
            quote = normalise_currency(self.quote)
        except DomainError as bad_currency:
            raise FxRateInvalid(
                "a rate names an unsupported currency",
                base=repr(self.base),
                quote=repr(self.quote),
                reason=bad_currency.code,
            ) from bad_currency
        if base == quote:
            raise FxRateInvalid("a rate needs two different currencies", pair=base)
        object.__setattr__(self, "base", base)
        object.__setattr__(self, "quote", quote)
        object.__setattr__(self, "rate", parse_rate(self.rate, pair=f"{base}{PAIR_SEPARATOR}{quote}"))

    @property
    def key(self) -> str:
        """``"USD/IDR"``."""
        return f"{self.base}{PAIR_SEPARATOR}{self.quote}"

    def convert_minor(self, amount_minor: int, *, rounding: str = ROUND_UP) -> int:
        """Convert an amount from :attr:`base` into :attr:`quote`, in minor units.

        The amount goes through major units on purpose: the two currencies have different
        exponents (IDR 0, USD 2), and scaling minor units by a rate directly is the classic
        cross-currency money bug. ``Money.from_major`` re-applies the target's exponent and
        validates the result, so the answer comes back as a validated amount, not a bare number.
        """
        if rounding not in ROUNDINGS:
            raise FxRateInvalid(
                f"unsupported rounding {rounding!r}", supported=list(ROUNDINGS), pair=self.key
            )
        source = Money(amount_minor, self.base)
        target_major = source.to_decimal() * self.rate
        return Money.from_major(target_major, self.quote, rounding=rounding).amount_minor

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "quote": self.quote,
            "rate": decimal_to_str(self.rate),
            "derived": self.derived,
        }


class FxTable:
    """The published rates, resolved by pair. Immutable, and read-only by construction."""

    def __init__(self, rates: Any) -> None:
        self._rates = parse_rates(rates)

    @classmethod
    def from_policy(cls, payload: Any) -> "FxTable":
        """Build from the value of ``commercial.fx.rates`` as the policy store hands it over."""
        return cls(payload)

    def pairs(self) -> tuple[str, ...]:
        """The pairs that were published, sorted — for messages and for audit."""
        return tuple(sorted(self._rates))

    def has(self, base: Any, quote: Any) -> bool:
        """True when this pair can be answered, published direction or inverted."""
        key = pair_key(base, quote)
        return key in self._rates or _flip(key) in self._rates

    def rate(self, base: Any, quote: Any) -> FxRate:
        """The rate converting ``base`` into ``quote`` — published, or inverted and marked so."""
        key = pair_key(base, quote)
        if key in self._rates:
            return FxRate(base, quote, self._rates[key])
        flipped = _flip(key)
        if flipped in self._rates:
            return FxRate(base, quote, Decimal(1) / self._rates[flipped], derived=True)
        raise FxRateNotPublished(
            f"no published rate converts {key}: publish {key} (or {flipped}) before relying on it",
            pair=key,
            published=list(self.pairs()),
            policy_key=FX_RATES_KEY,
        )

    def convert_minor(
        self,
        amount_minor: int,
        *,
        from_currency: Any,
        to_currency: Any,
        rounding: str = ROUND_UP,
    ) -> int:
        """Convert between two currencies; converting a currency into itself is the identity."""
        source = normalise_currency(from_currency)
        target = normalise_currency(to_currency)
        if source == target:
            return Money(amount_minor, source).amount_minor
        return self.rate(source, target).convert_minor(amount_minor, rounding=rounding)

    def to_dict(self) -> dict[str, Any]:
        return {"rates": {key: decimal_to_str(rate) for key, rate in self._rates.items()}}


__all__ = [
    "FX_RATES_KEY",
    "FxError",
    "FxRate",
    "FxRateInvalid",
    "FxRateNotPublished",
    "FxTable",
    "PAIR_SEPARATOR",
    "ROUNDINGS",
    "ROUND_NEAREST",
    "ROUND_UP",
    "normalise_currency",
    "pair_key",
    "parse_rate",
    "parse_rates",
]
