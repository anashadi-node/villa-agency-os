"""Rupiah-first amounts with an automatic reference conversion (audit 2026-11-13, T8).

Owner's rule: money is **IDR**, and beside it the reader must see what that figure is
worth in **USD**, computed automatically instead of typed by hand.

This module is a *display* layer and nothing else. Three rules keep it honest:

* **IDR is the amount; the USD figure is a reference.** It never enters a quote, an
  invoice total, a ledger entry or any comparison. Nothing here returns money the kernel
  could spend — :func:`reference` returns a formatted sentence to sit beside the price.
* **The rate is a published fact, not a default.** It comes from ``commercial.fx.rates``
  and must pass :func:`fx.require_fresh` with ``commercial.fx.max_age_hours`` — the same
  guard the margin floor uses. No table, no freshness limit, no date, no published pair,
  or a rate too old ⇒ **no reference is shown**. There is no fallback to parity, to
  yesterday's number, or to a constant in this file: a made-up rate is worse than none.
* **Every figure carries the rate and the day that produced it**, because a converted
  amount nobody can re-derive is a claim, not an amount.

Regulatory note: Bank Indonesia requires the price to be quoted in rupiah and restricts
dual quotation. The reference therefore always begins with ``≈`` and is labelled
``perkiraan``, never replaces the IDR figure, and is **off by default for guest-facing
documents** (:data:`GUEST_DOCUMENT_REFERENCE_ENABLED`) until counsel approves it. The
internal dashboard — staff reading their own figures — is where the owner's rule applies
today.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import fx as FX
from primitives import Money

#: The currency the reference is expressed in.
REFERENCE_TARGET = "USD"

#: Locale used for grouping when a caller does not name one (Indonesian convention:
#: ``4.042.500``, not ``4,042,500``).
DEFAULT_LOCALE = "id-ID"

#: Guest-facing documents (quotation, invoice, receipt) stay rupiah-only until counsel
#: approves a reference line: BI's rule binds the price to one currency. Flip this to
#: ``True`` only with that approval on record; the document renderer then prints the
#: same labelled reference the dashboard prints.
GUEST_DOCUMENT_REFERENCE_ENABLED = False

#: The word that tells a reader the converted figure is not the price. English, because
#: ADR-0011 §55 fixes production copy to en-US and this module carries no legacy allowance.
REFERENCE_MARKER = "indicative"


@dataclass(frozen=True)
class ReferenceSource:
    """Everything needed to convert, taken from policy — never from code defaults."""

    table: FX.FxTable
    quoted_on: str
    max_age_hours: int


@dataclass(frozen=True)
class ReferenceNote:
    """One converted figure, with the rate and the day that produced it."""

    amount: Money
    rate: FX.FxRate
    quoted_on: str
    rate_text: str

    @property
    def currency(self) -> str:
        return self.amount.currency

    def text(self, locale: str = DEFAULT_LOCALE) -> str:
        """``≈ USD 248,77`` — the figure alone, for a caller that supplies its own wording."""
        return f"≈ {format_money(self.amount.amount_minor, self.amount.currency, locale)}"

    def basis(self, locale: str = DEFAULT_LOCALE) -> str:
        """``kurs 16.250 IDR per USD (13 Nov 2026)`` — what the figure was computed from."""
        return f"kurs {self.rate_text} ({_date_text(self.quoted_on, locale)})"


def source_from_policy(policy: Any, *, at: Any = None) -> Optional[ReferenceSource]:
    """Read the published rates and freshness limit from policy, or ``None``.

    ``None`` means the platform has not published what a conversion needs, and callers
    must print the rupiah figure alone. A policy read never raises out of a page render:
    a missing or unreadable rate is a reason to omit a nicety, not to fail a request.
    """
    if policy is None:
        return None
    try:
        rates = policy.resolve(FX.FX_RATES_KEY, at=at)
        limit = policy.resolve(FX.FX_MAX_AGE_KEY, at=at).as_int()
    except Exception:  # noqa: BLE001 - policy read failure ⇒ no reference, not a dead page
        return None
    try:
        table = FX.FxTable.from_policy(rates.as_json())
    except FX.FxError:
        return None
    return ReferenceSource(table=table, quoted_on=str(rates.effective_from), max_age_hours=int(limit))


def format_money(minor: Any, currency: str = "", locale: str = DEFAULT_LOCALE) -> str:
    """``IDR 4.042.500`` / ``USD 248,77`` — grouped the reader's way, never via a float."""
    from documents import format_amount  # local: documents pulls invoicing and the gates

    try:
        amount = int(minor)
    except (TypeError, ValueError):
        return "—"
    try:
        money = Money(amount, str(currency or "").upper() or "IDR")
    except Exception:  # noqa: BLE001 - an unknown ISO code is a display concern, not a crash
        return "—"
    return format_amount(money, locale)


def reference(
    amount: Money,
    *,
    source: Optional[ReferenceSource],
    today: Any,
    target: str = REFERENCE_TARGET,
    locale: str = DEFAULT_LOCALE,
) -> Optional[ReferenceNote]:
    """The labelled reference for ``amount``, or ``None`` when it cannot be justified.

    ``None`` is the honest answer whenever the platform cannot prove the figure: no
    published source, an amount already in the target currency, no published pair, or a
    rate that fails the freshness guard. No caller may substitute a rate of its own.
    """
    if source is None or today is None:
        return None
    try:
        money = Money(int(amount.amount_minor), amount.currency)
        wanted = Money.zero(target).currency
        if money.currency == wanted:
            return None
        FX.require_fresh(
            source.quoted_on,
            today=today,
            max_age_hours=source.max_age_hours,
            pair=FX.pair_key(money.currency, wanted),
        )
        rate = source.table.rate(money.currency, wanted)
        converted = Money(
            source.table.convert_minor(
                money.amount_minor,
                from_currency=money.currency,
                to_currency=wanted,
                rounding=FX.ROUND_NEAREST,
            ),
            wanted,
        )
    except (FX.FxError, FX.DomainError):
        return None
    return ReferenceNote(
        amount=converted,
        rate=rate,
        quoted_on=source.quoted_on,
        rate_text=_display_rate(source, money.currency, wanted, locale),
    )


def line(
    minor: Any,
    currency: str = "",
    *,
    source: Optional[ReferenceSource] = None,
    today: Any = None,
    target: str = REFERENCE_TARGET,
    locale: str = DEFAULT_LOCALE,
) -> str:
    """``IDR 4.042.500 (≈ USD 248,77)`` — the whole line a page can print.

    The rupiah figure always comes first and always survives: a missing or stale rate
    removes only the parenthesised reference, never the price.
    """
    primary = format_money(minor, currency, locale)
    if primary == "—":
        return primary
    try:
        amount = Money(int(minor), str(currency or "").upper() or "IDR")
    except Exception:  # noqa: BLE001
        return primary
    note = reference(amount, source=source, today=today, target=target, locale=locale)
    return f"{primary} ({note.text(locale)})" if note is not None else primary


#: Said instead of a rate when the platform has nothing it can stand behind. It names the
#: reason the reader sees no dollar figure, so a missing reference cannot be mistaken for a
#: missing price.
NO_RATE_NOTE = (
    "Primary amounts are in rupiah (IDR). The USD figure is not shown because no current "
    "published rate exists."
)


def footnote(
    source: Optional[ReferenceSource], *, today: Any = None, locale: str = DEFAULT_LOCALE
) -> str:
    """One sentence per page explaining the reference — or why there is none.

    It names the source and the day so a reader can check the arithmetic, and it says
    outright that the figure is informational, which is what keeps the reference from
    being read as a second price. When no rate can be stood behind, the sentence says so
    instead of staying silent: a blank space would look like an omission, not a refusal.
    """
    if source is None or today is None:
        return NO_RATE_NOTE
    try:
        FX.require_fresh(
            source.quoted_on, today=today, max_age_hours=source.max_age_hours, pair=REFERENCE_TARGET
        )
        rate_text = _display_rate(source, "IDR", REFERENCE_TARGET, locale)
    except FX.FxError:
        return NO_RATE_NOTE
    return (
        f"Primary amounts are in rupiah (IDR); the USD figure is {REFERENCE_MARKER} at "
        f"{rate_text} ({_date_text(source.quoted_on, locale)})."
    )


def _display_rate(source: ReferenceSource, amount_currency: str, target: str, locale: str) -> str:
    """The rate in the direction people quote it: ``16.250 IDR per USD``.

    The conversion itself runs whichever way the pair was published; this string is for the
    reader, so the published orientation wins over the derived one. Only when the single
    published direction is the inverse does the note show that inverse — with ``per IDR``
    beside it, so the number is never read backwards.
    """
    candidates = []
    for base, quote in ((target, amount_currency), (amount_currency, target)):
        try:
            candidates.append(source.table.rate(base, quote))
        except FX.FxError:
            continue
    if not candidates:
        raise FX.FxRateNotPublished(f"no published rate converts {amount_currency} and {target}")
    chosen = next((rate for rate in candidates if not rate.derived), candidates[0])
    return f"{_rate_text(chosen.rate, locale)} {chosen.quote} per {chosen.base}"


def _rate_text(rate: Decimal, locale: str, *, max_significant: int = 8) -> str:
    """A rate as people write it: ``16.250`` in id-ID, ``16,250`` in en-US.

    Significant figures, not decimals: an inverted pair can be small (0.0000615…), and a
    string that long stops being readable. Eight significant figures is more precision than
    the conversion needs and few enough to keep the sentence legible.
    """
    thousands, decimals = (".", ",") if locale.lower().startswith("id") else (",", ".")
    value = Decimal(rate)
    places = max(0, max_significant - 1 - value.adjusted())
    value = value.quantize(Decimal(1).scaleb(-places)) if places else value.to_integral_value()
    text = format(value, "f")
    whole, _, fraction = text.partition(".")
    grouped = f"{int(whole):,}".replace(",", thousands)
    fraction = fraction.rstrip("0")
    return f"{grouped}{decimals}{fraction}" if fraction else grouped


def _date_text(value: str, locale: str) -> str:
    """``13 Nov 2026`` — but never at the cost of the page."""
    from documents import format_date

    try:
        return format_date(str(value)[:10], locale)
    except Exception:  # noqa: BLE001 - a display helper may not take a page down
        return str(value)[:10]


__all__ = [
    "DEFAULT_LOCALE",
    "GUEST_DOCUMENT_REFERENCE_ENABLED",
    "NO_RATE_NOTE",
    "REFERENCE_MARKER",
    "REFERENCE_TARGET",
    "ReferenceNote",
    "ReferenceSource",
    "footnote",
    "format_money",
    "line",
    "reference",
    "source_from_policy",
]
