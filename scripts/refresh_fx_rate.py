#!/usr/bin/env python3
"""Fetch the day's real rate and publish it — a market fact, on a daily schedule.

§15 puts thresholds in configuration, not in code, and §11's absolute floor is
denominated in ``commercial.margin_floor_currency``. A booking priced in another
currency therefore needs a rate — and that rate is not a number anyone *decides*: it
is the day's price of one currency in another. So it is **fetched** from a named
provider, published as a policy revision that carries the provider's own quote time,
and left alone until the next day.

The owner decides the *rule* (which provider, how old a quote may be, how far a quote
may move before someone looks). The feed supplies the *value*. Nothing here invents a
rate: an error payload, a quote the provider itself dates days ago, or a move no market
made is a refusal — never a fallback to yesterday's number or to parity.

    refresh_fx_rate.py --pair USD/IDR --approved-by <principal> [--dry-run]

``--dry-run`` fetches and validates and prints what it would publish, and writes
nothing. A run that would change nothing (the same quote is already effective today)
also writes nothing: a revision that says the same thing is noise in the audit trail.

Exit codes: 0 published (or a dry run that would publish), 1 refused — by this tool's
validation or by the policy store, 2 usage or configuration, 3 the provider could not
be read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fx as FX  # noqa: E402  (path setup must run first)
from policy_engine import POLICY_OPEN_END, PolicyResolver  # noqa: E402
from primitives import DomainError, IdFactory, SystemClock, decimal_to_str  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

DEFAULT_DB = "data/villa.db"
DEFAULT_AGENCY = "agency_default"

#: The provider this platform relies on by default: daily, no key, and it states the
#: time of its own quote — which is what makes a stale quote detectable at all.
DEFAULT_URL = "https://open.er-api.com/v6/latest/USD"
DEFAULT_PAIR = "USD/IDR"

#: A quote the provider itself dates older than this is a dead feed, not a rate.
DEFAULT_MAX_FEED_AGE_HOURS = 48
#: A move larger than this against the rate already published is not a market move;
#: it is a bad payload, and someone should look before the floor moves with it.
DEFAULT_MAX_MOVE_PCT = Decimal("20")


class FeedError(Exception):
    """The provider could not be read, or answered something this tool will not use."""


class Refused(Exception):
    """The quote was read but will not be published."""


@dataclass(frozen=True)
class Quote:
    """One provider answer: a pair, its price, and when the provider said so."""

    pair: str
    rate: Decimal
    quoted_at: datetime
    source_ref: str
    provider: str

    @property
    def age_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.quoted_at).total_seconds() / 3600


@dataclass(frozen=True)
class Published:
    """The last rate table this tool published: its version, its window, its pairs."""

    version: int
    effective_from: str
    value: Any  # already parsed: {"USD/IDR": "17000"}


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def quote_time(payload: Mapping[str, Any], *, url: str) -> datetime:
    """The moment the provider says its numbers are from — required, not inferred.

    A rate with no quote time cannot be judged fresh, and an unjudgeable rate is one
    this tool will not publish. Three shapes are accepted because providers differ:
    an epoch (``time_last_update_unix``), an RFC-2822 stamp (``time_last_update_utc``),
    and a plain date (``date``) for a daily reference series.
    """
    epoch = payload.get("time_last_update_unix")
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    stamp = payload.get("time_last_update_utc")
    if isinstance(stamp, str) and stamp.strip():
        try:
            return _utc(parsedate_to_datetime(stamp.strip()))
        except (TypeError, ValueError) as bad:
            raise FeedError(f"{url}: quote time not readable: {stamp!r}") from bad
    day = payload.get("date")
    if isinstance(day, str) and day.strip():
        try:
            return datetime.fromisoformat(day.strip()).replace(tzinfo=timezone.utc)
        except ValueError as bad:
            raise FeedError(f"{url}: quote date not readable: {day!r}") from bad
    raise FeedError(
        f"{url}: the payload carries no quote time — a rate nobody dated cannot be "
        "checked for freshness, so it is not published"
    )


def quote_from_payload(payload: Any, *, pair: str, url: str) -> Quote:
    """Read one pair out of a provider answer, or refuse the answer.

    Refuses: a payload that is not an object, a provider-reported failure, a missing
    rate table, a base that is not the pair's base, a rate that is not an exact
    positive decimal, and a payload with no quote time.
    """
    if not isinstance(payload, Mapping):
        raise FeedError(f"{url}: expected a JSON object, got {type(payload).__name__}")
    result = payload.get("result")
    if isinstance(result, str) and result.lower() not in ("success", "ok"):
        raise FeedError(f"{url}: provider reported {result!r}")
    rates = payload.get("rates")
    if not isinstance(rates, Mapping) or not rates:
        raise FeedError(f"{url}: payload carries no rate table")
    base, quote = pair.split(FX.PAIR_SEPARATOR)
    stated_base = payload.get("base_code") or payload.get("base")
    if isinstance(stated_base, str) and FX.normalise_currency(stated_base) != base:
        raise FeedError(f"{url}: payload is based on {stated_base}, not {base}")
    if quote not in rates:
        raise FeedError(f"{url}: no {quote} in the payload (keys: {sorted(rates)[:8]}…)")
    raw = rates[quote]
    # JSON has no decimal type: the float is re-read from its shortest exact text, and
    # fx.parse_rate refuses anything that is not an exact positive decimal.
    try:
        exact = raw if isinstance(raw, str) else Decimal(repr(raw) if isinstance(raw, float) else str(raw))
    except (InvalidOperation, ValueError) as bad:
        raise FeedError(f"{url}: {quote} is not a number: {raw!r}") from bad
    rate = _exact_rate(exact, pair=pair, url=url)
    quoted_at = quote_time(payload, url=url)
    provider = str(payload.get("provider") or url)
    # The kernel caps source_doc_ref at 200 chars (policy_engine.require_text). A long
    # provider URL (or a file:// probe URL in tests) would make the kernel refuse the
    # publish — so the URL is recorded truncated and the *full* provenance (URL, rate,
    # quote time) is preserved by the per-field anchors. Keep this in sync with the
    # kernel cap: the suffixes below are ~80 chars, leaving ~120 for the URL.
    source_url = url[:120]
    return Quote(
        pair=pair,
        rate=rate,
        quoted_at=quoted_at,
        source_ref=f"{source_url}#{pair}={decimal_to_str(rate)}#quoted_at={quoted_at.isoformat()}",
        provider=provider,
    )


def _exact_rate(value: Any, *, pair: str, url: str) -> Decimal:
    """``fx.parse_rate``'s refusals belong to *this* tool's refusal vocabulary.

    A rate the store would reject is a feed that answered something unusable, and the
    CLI must report it as an unreadable provider — not crash with the store's error.
    """
    try:
        return FX.parse_rate(value, pair=pair)
    except FX.FxError as unusable:
        raise FeedError(f"{url}: {unusable}") from unusable


def fetch_json(url: str, *, timeout: float = 20.0) -> Any:
    """Read the provider over HTTPS. No retries: a cron run will try again tomorrow."""
    # The docstring promises HTTPS, so a plain-http feed is refused: an http:// URL can
    # be rewritten in transit, and a rewritten FX rate mis-prices every booking. The
    # local ``file://`` form stays allowed because the CLI and its tests read a saved
    # quote from disk — content the operator could already read, and it still has to
    # parse as a provider payload before it can publish anything.
    if not url.lower().startswith(("https://", "file://")):
        raise FeedError(f"{url}: only https feeds are accepted")
    request = urllib.request.Request(
        url, headers={"User-Agent": "villa-agency-os/refresh_fx_rate", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as raw:  # nosemgrep: dynamic-urllib-use-detected - scheme asserted above
            body = raw.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as unreachable:
        raise FeedError(f"{url}: not reachable ({unreachable})") from unreachable
    try:
        return json.loads(body)
    except json.JSONDecodeError as bad:
        raise FeedError(f"{url}: answer is not JSON ({bad.msg})") from bad


def check_feed_age(quote: Quote, *, now: datetime, max_age_hours: float) -> None:
    """Refuse a quote the provider itself dates too far back."""
    age = (_utc(now) - quote.quoted_at).total_seconds() / 3600
    if age > max_age_hours:
        raise Refused(
            f"the provider's own quote is {age:.1f}h old (limit {max_age_hours:g}h): "
            f"{quote.quoted_at.isoformat()} — a dead feed must not become a rate"
        )
    if age < -1:
        raise Refused(
            f"the provider dates its quote {abs(age):.1f}h in the future "
            f"({quote.quoted_at.isoformat()}): the clock and the feed disagree"
        )


def check_move(rate: Decimal, previous: Decimal | None, *, max_pct: Decimal) -> None:
    """Refuse a move no market made — the guard against a bad payload."""
    if previous is None or previous <= 0:
        return
    move = abs(rate - previous) / previous * Decimal(100)
    if move > max_pct:
        raise Refused(
            f"{decimal_to_str(rate)} is {move:.2f}% away from the published "
            f"{decimal_to_str(previous)} (limit {max_pct}%): a payload this far off is "
            "reviewed by a human, not published by a schedule"
        )


def latest_revision(conn, agency_id: str):
    """The most recent ``commercial.fx.rates`` revision, whatever day it applies from.

    **Not** the revision *effective today*. A refresh tool has to compare against the last
    rate it published and carry the pairs it published beside it; asking the resolver for
    "today" ties the tool's behaviour to the calendar — a rate published for tomorrow is
    invisible today, so the next run would see no previous rate, skip the out-of-band guard
    and drop every other published pair on the way past. That is exactly what this function
    was rewritten to stop doing.
    """
    row = conn.execute(
        "SELECT version, value, effective_from FROM policy_revisions"
        " WHERE policy_key = ? AND agency_id = ?"
        " ORDER BY version DESC LIMIT 1",
        (FX.FX_RATES_KEY, agency_id),
    ).fetchone()
    if row is None:
        return None
    try:
        parsed = json.loads(str(row["value"]))
    except json.JSONDecodeError as unreadable:
        raise Refused(
            f"the last published {FX.FX_RATES_KEY} (version {row['version']}) is not JSON "
            f"({unreadable}) — fix it by hand rather than publishing over it blind"
        ) from unreadable
    return Published(
        version=int(row["version"]), effective_from=str(row["effective_from"]), value=parsed
    )


def previous_rate(published: Any, *, pair: str) -> Decimal | None:
    """The pair's rate in the effective table, or ``None`` when it is not there yet."""
    if not isinstance(published, Mapping) or pair not in published:
        return None
    try:
        return FX.parse_rate(published[pair], pair=pair)
    except FX.FxError as unusable:
        raise Refused(
            f"the published table already holds a {pair} this tool cannot read "
            f"({unusable}) — fix it by hand rather than replacing it blind"
        ) from unusable


def build_payload(current: Any, *, pair: str, rate: Decimal) -> dict[str, str]:
    """The new rate table: this pair updated, every other published pair untouched."""
    payload: dict[str, str] = {}
    if isinstance(current, Mapping):
        for raw_pair, raw_rate in current.items():
            try:
                key = FX.pair_key(*str(raw_pair).split(FX.PAIR_SEPARATOR))
            except (DomainError, TypeError):
                raise Refused(
                    f"the published table holds a key this tool cannot read: {raw_pair!r} — "
                    "fix it by hand rather than dropping it silently"
                ) from None
            try:
                payload[key] = decimal_to_str(FX.parse_rate(raw_rate, pair=key))
            except FX.FxError as unusable:
                raise Refused(
                    f"the published table holds a rate this tool cannot read: "
                    f"{raw_pair!r} = {raw_rate!r} ({unusable}) — fix it by hand rather "
                    "than dropping it silently"
                ) from unusable
    payload[pair] = decimal_to_str(rate)
    return payload


def _publish(conn, agency_id: str, args: argparse.Namespace, quote: Quote, payload: dict[str, str]):
    resolver = PolicyResolver(conn, clock=SystemClock(), ids=IdFactory(), agency_id=agency_id)
    return resolver.publish(
        FX.FX_RATES_KEY,
        payload,
        effective_from=args.effective_from,
        effective_to=args.effective_to,
        source_doc_ref=quote.source_ref,
        approved_by=args.approved_by,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pair", default=DEFAULT_PAIR, help="pair to refresh (default: %(default)s)")
    parser.add_argument("--url", default=DEFAULT_URL, help="provider URL (default: %(default)s)")
    parser.add_argument("--agency", default=DEFAULT_AGENCY, help="agency scope")
    parser.add_argument(
        "--approved-by",
        help=(
            "the principal this revision is published under — the owner's standing rule "
            "for the feed, or the operator who ran it (required to write)"
        ),
    )
    parser.add_argument(
        "--effective-from",
        default=None,
        help=(
            "first day the rate applies (default: today, platform clock, in UTC). Pass the "
            "platform's day, not your own: east of UTC the local day starts hours earlier, and a "
            "revision dated by local time begins in the future — where the freshness gate refuses "
            "it as a future-dated rate and the store refuses to backdate the correction"
        ),
    )
    parser.add_argument("--effective-to", default=POLICY_OPEN_END, help="last day it applies")
    parser.add_argument(
        "--max-feed-age-hours",
        type=float,
        default=DEFAULT_MAX_FEED_AGE_HOURS,
        help="refuse a quote the provider dates older than this (default: %(default)s)",
    )
    parser.add_argument(
        "--max-move-pct",
        type=Decimal,
        default=DEFAULT_MAX_MOVE_PCT,
        help="refuse a move against the published rate larger than this (default: %(default)s)",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="provider timeout in seconds")
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch, validate and report; write nothing"
    )
    args = parser.parse_args(argv)

    try:
        base, quote_currency = args.pair.split(FX.PAIR_SEPARATOR)
        pair = FX.pair_key(base, quote_currency)
    except (ValueError, DomainError) as bad_pair:
        print(f"--pair must read BASE/QUOTE: {args.pair!r} ({bad_pair})", file=sys.stderr)
        return 2

    db_path = Path(os.environ.get("VILLA_DB", "").strip() or DEFAULT_DB)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    if not os.environ.get("VILLA_DB", "").strip():
        print(
            "VILLA_DB must be set explicitly: a rate that lands silently in whatever "
            "data/villa.db happens to sit in the working directory is a rate nobody chose",
            file=sys.stderr,
        )
        return 2
    if not args.dry_run and not (args.approved_by or "").strip():
        print(
            "--approved-by is required to publish: a revision with no principal is a rate "
            "nobody stands behind (the store refuses it anyway)",
            file=sys.stderr,
        )
        return 2
    if args.effective_from is None:
        args.effective_from = SystemClock().today().isoformat()

    try:
        quote = quote_from_payload(fetch_json(args.url, timeout=args.timeout), pair=pair, url=args.url)
    except FeedError as unreadable:
        print(f"PROVIDER REFUSED {unreadable}", file=sys.stderr)
        return 3

    conn = connect_sqlite(str(db_path))
    try:
        current = latest_revision(conn, args.agency)
        published_value = current.value if current is not None else None
        try:
            previous = previous_rate(published_value, pair=pair)
            payload = build_payload(published_value, pair=pair, rate=quote.rate)
            check_feed_age(quote, now=datetime.now(timezone.utc), max_age_hours=args.max_feed_age_hours)
            check_move(quote.rate, previous, max_pct=args.max_move_pct)
        except (Refused, FX.FxError, DomainError) as refused:
            print(f"REFUSED {refused}", file=sys.stderr)
            return 1

        same_day = current is not None and str(current.effective_from) == str(args.effective_from)
        if same_day and previous == quote.rate:
            print(
                f"UNCHANGED {pair} = {decimal_to_str(quote.rate)} already effective from "
                f"{current.effective_from} (version {current.version}) — nothing published"
            )
            return 0

        print(f"FETCHED  {pair} = {decimal_to_str(quote.rate)}")
        print(f"  provider : {quote.provider}")
        print(f"  quoted   : {quote.quoted_at.isoformat()} ({quote.age_hours:.1f}h ago)")
        print(f"  previous : {decimal_to_str(previous) if previous is not None else 'none published'}")
        print(f"  table    : {json.dumps(payload, sort_keys=True)}")

        if args.dry_run:
            print(f"DRY RUN — would publish version after {current.version if current else 'none'}")
            return 0

        published = _publish(conn, args.agency, args, quote, payload)
        print(
            f"PUBLISHED {FX.FX_RATES_KEY} version {published.version} = "
            f"{json.dumps(published.value, sort_keys=True)}\n"
            f"  window     : {published.effective_from} → {published.effective_to}\n"
            f"  approved by: {published.approved_by}\n"
            f"  source     : {published.source_doc_ref}\n"
            f"  fingerprint: {published.fingerprint[:16]}…"
        )
        return 0
    except DomainError as refused:
        print(f"REFUSED [{refused.code}] {refused}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
