"""Agent 6 — the Revenue & BI AI Agent: revenue telemetry with zero write paths.

§10 of the AI Workforce Architecture V1.1, under the worker catalogue frozen in
ADR-0064 (``RBI-W01…RBI-W12``). Slice 1 owns the core commercial metrics:

- **M1 occupancy** — with exactly one official denominator (ADR-0064 part 2):
  the nights inside the window of every unit whose recorded ``status`` is
  ``ACTIVE``, clipped by ``units.created_at`` so a villa that joined mid-window
  contributes only the nights it was managed. Nights *sold* come from
  ``reservation_kpi.BOOKED_STATUSES`` — the reservation kernel's own
  "this is a confirmed sale" vocabulary, read and never re-declared — and each
  stay's nights are clipped to the window, so a stay across a boundary is counted
  once, in part.
- **M2 ADR**, **M3 RevPAR** — per currency, always. RevPAR divides by the *same*
  official denominator as occupancy, so the two can never disagree about supply.
  ADR v1 is the gross nightly rate: discounts are excluded and said to be
  excluded (net ADR arrives with M12 in slice 2).
- **M6 ALOS**, **M8 cancellation rate** — the arrival cohort is every booking
  whose ``check_in`` falls inside the window; a cancellation is counted against
  that cohort, and a cancelled stay is never also counted as sold nights.
- **M9 net billed revenue** — invoices issued in the window that were not
  voided, minus payments the provider reported as ``REFUNDED``, **per currency**.
  No metric in this module ever adds IDR to USD.

What the module refuses to do, structurally:

- it writes nothing: the collector only SELECTs, and ``collect()`` twice at one
  instant returns equal bundles with every row count untouched;
- it registers no tool: :data:`REVENUE_TOOLS` is a declaration of read intent
  (ADR-0061 option B), so the catalogue keeps one writer — the schema — and the
  gateway's tier and policy layer, not this module's good intentions, is what
  keeps ``payment.approve`` out of reach;
- it never publishes a number it cannot defend: empty denominators give ``None``
  (never a fabricated 0%), the evidence-based companion occupancy stays ``None``
  until ``availability_evidence.occupancy_ranges`` is actually populated by some
  producer, and every published figure carries its denominator label and the
  measured evidence coverage beside it;
- it selects no ``guest_ref`` and produces no person-shaped field — every value
  is a number, a rate, a label or an ISO instant.

Every metric carries its lineage: the tables it read, the window, and the filter
that produced it. A number without an origin is not a report, it is a rumour.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Optional

import primitives as P

#: ADR-0064 part 1 — the canonical twelve §10 workers. Column two is the stable
#: id a report or a test may key on; column three is what a GM reads. The five
#: numbers other documents already cite (W04 demand, W09 forecast, W10 margin,
#: W11 anomaly, W12 executive report) keep their positions: renaming those would
#: silently invalidate documents that predate this module.
REVENUE_BI_WORKERS: tuple[tuple[str, str, str], ...] = (
    ("RBI-W01", "revenue_kpi", "KPI Pendapatan"),
    ("RBI-W02", "conversion", "Konversi & Funnel"),
    ("RBI-W03", "pace_leadtime", "Pace & Lead Time"),
    ("RBI-W04", "demand_intel", "Intel Permintaan"),
    ("RBI-W05", "pricing_intel", "Intel Harga & Diskon"),
    ("RBI-W06", "villa_performance", "Performa Villa"),
    ("RBI-W07", "partner_performance", "Performa Mitra"),
    ("RBI-W08", "channel_performance", "Performa Kanal"),
    ("RBI-W09", "forecast", "Ramalan"),
    ("RBI-W10", "margin", "Margin (parsial)"),
    ("RBI-W11", "anomaly", "Anomali"),
    ("RBI-W12", "exec_report", "Laporan Eksekutif"),
)

#: §10 read-tier names — *declared intent, not registered tools* (ADR-0061 option B).
#: Every verb here reads recorded facts so a human can decide. Registering them would
#: add rows the gateway's ``TOOL_EFFECTS`` map cannot classify, and widening READ
#: erodes the read-only rung for every tenant at once.
REVENUE_TOOLS: tuple[tuple[str, str, Optional[str], str], ...] = (
    ("revenue.kpi", "LOW", None, "Read occupancy, ADR, RevPAR, ALOS, cancellations"),
    ("revenue.revenue", "LOW", None, "Read net billed revenue per currency"),
    ("revenue.report", "LOW", None, "Read the bundle with lineage for a human"),
)

#: The name the Control Tower's closed vocabulary routes REVENUE_BI to.
REVENUE_AGENT_NAME = "revenue_bi_ai"

#: Default horizon: the four-week baseline the research charter measures against.
DEFAULT_WINDOW_DAYS = 30

#: The label every published occupancy must carry (ADR-0064 part 2, decision 1).
DENOMINATOR_LABEL = "unit-nights under management (units.status='ACTIVE')"

#: Coverage below which the evidence-based companion occupancy is not published.
EVIDENCE_COVERAGE_MINIMUM = 0.70

#: Why the companion is missing, stated with the measurement behind it — so the day
#: a producer starts filling ``occupancy_ranges`` the report says so instead of
#: staying silently empty.
SELLABLE_WITHOUT_PRODUCER = (
    "no producer defines availability_evidence.occupancy_ranges keys and no reader "
    "exists (measured 2026-10-07: 0 rows carry a range, whole test corpus included)"
)


class RevenueClientError(P.DomainError):
    """The telemetry cannot answer against the storage it was given."""

    code = "REVENUE_TELEMETRY_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# report rows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OccupancySurvey:
    """M1 with both denominators, labelled, plus the evidence honesty beside it."""

    booked_nights: int
    managed_unit_nights: int
    unavailable_unit_nights: int
    occupancy_managed: Optional[float]
    occupancy_sellable: Optional[float]
    denominator_label: str
    evidence_rows: int
    evidence_rows_with_ranges: int
    evidence_coverage: Optional[float]
    sellable_unavailable_reason: Optional[str]


@dataclass(frozen=True)
class CurrencyLine:
    """Every money metric for one currency. Two currencies are two rows — never one sum."""

    currency: str
    nights: int
    nightly_minor: int
    invoiced_minor: int
    refunded_minor: int
    revenue_net_minor: int
    adr_minor: Optional[float]
    revpar_minor: Optional[float]


@dataclass(frozen=True)
class RevenueSnapshot:
    """Everything slice 1 asks for, as of one instant, with its origin attached."""

    window_start: str
    window_end: str
    window_days: int
    occupancy: OccupancySurvey
    arrivals_in_window: int
    cancellation_rate: Optional[float]
    booking_count: int
    alos_nights: Optional[float]
    money: Mapping[str, CurrencyLine]
    lineage: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    caveats: tuple[str, ...] = ()
    generated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The JSON-shaped bundle a dashboard or an executive report reads."""

        return asdict(self)


def _rate(numerator: float, denominator: float) -> Optional[float]:
    """A rate over nothing is unknown, not 0%."""
    if denominator <= 0:
        return None
    return numerator / denominator


def _booked_statuses() -> tuple[str, ...]:
    """The reservation kernel's own "confirmed sale" vocabulary (read, not copied)."""
    import reservation_kpi as RK

    return tuple(RK.BOOKED_STATUSES)


def _unavailable_unit_statuses() -> tuple[str, ...]:
    """Supply that exists but was not sellable — derived from the owner's vocabulary."""
    import gates as G

    return tuple(status for status in G.UNIT_STATUSES if status != "ACTIVE")


def _night_overlap(start: date, end: date, window_start: date, window_end: date) -> int:
    """Nights of ``[start, end)`` that fall inside ``[window_start, window_end)``."""
    first = start if start > window_start else window_start
    last = end if end < window_end else window_end
    return max(0, (last - first).days)


# --------------------------------------------------------------------------- #
# collector — SELECT only
# --------------------------------------------------------------------------- #
def collect(
    conn: Any,
    *,
    clock: Any = None,
    at: Any = None,
    agency_id: str = "agency_default",
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> RevenueSnapshot:
    """Read the commercial facts for one agency at one instant. Writes nothing."""
    try:
        _require_tables(
            conn, "bookings", "invoices", "invoice_lines", "payments", "units",
            "availability_evidence",
        )
        if clock is None and at is None:
            clock = P.SystemClock()
        moment = at if at is not None else clock.now()  # type: ignore[union-attr]
        now_iso = P.to_iso(moment)
        end_date = moment.date()
        start_date = (moment - timedelta(days=window_days)).date()
        start_iso = P.to_iso(moment - timedelta(days=window_days))

        # ---- supply: unit-nights under management (ADR-0064 part 2) --------
        managed_unit_nights = 0
        unavailable_unit_nights = 0
        unavailable = set(_unavailable_unit_statuses())
        for row in conn.execute(
            "SELECT status, created_at FROM units WHERE agency_id = ?",
            (agency_id,),
        ).fetchall():
            status = str(row[0])
            created = P.parse_iso(str(row[1])).date()
            nights = _night_overlap(created, end_date + timedelta(days=1),
                                    start_date, end_date)
            if status == "ACTIVE":
                managed_unit_nights += nights
            elif status in unavailable:
                unavailable_unit_nights += nights

        # ---- demand: sold nights, arrivals, cancellations ------------------
        booked_states = set(_booked_statuses())
        booked_nights = 0
        nights_by_currency: dict[str, int] = {}
        booked_nights_in_stay = 0
        bookings_in_window = 0
        arrivals_in_window = 0
        cancelled_arrivals = 0
        for row in conn.execute(
            "SELECT check_in, check_out, nights, status, currency, cancelled_at"
            " FROM bookings WHERE agency_id = ?",
            (agency_id,),
        ).fetchall():
            check_in = P.parse_iso(str(row[0])).date()
            check_out = P.parse_iso(str(row[1])).date()
            status = str(row[3])
            currency = str(row[4])
            if check_in < end_date and check_out > start_date:
                if status in booked_states:
                    stay_nights = _night_overlap(
                        check_in, check_out, start_date, end_date
                    )
                    booked_nights += stay_nights
                    nights_by_currency[currency] = (
                        nights_by_currency.get(currency, 0) + stay_nights
                    )
                    booked_nights_in_stay += int(row[2])
                    bookings_in_window += 1
            if start_date <= check_in < end_date:
                arrivals_in_window += 1
                if row[5] is not None:
                    cancelled_arrivals += 1

        # ---- revenue: per currency, never summed across currencies ---------
        invoiced = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                "SELECT currency, COALESCE(SUM(total_minor), 0) FROM invoices"
                " WHERE agency_id = ? AND voided_at IS NULL"
                "   AND issued_at >= ? AND issued_at < ? GROUP BY currency",
                (agency_id, start_iso, now_iso),
            ).fetchall()
        }
        refunded = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                "SELECT currency, COALESCE(SUM(amount_minor), 0) FROM payments"
                " WHERE agency_id = ? AND status = 'REFUNDED'"
                "   AND COALESCE(settled_at, created_at) >= ?"
                "   AND COALESCE(settled_at, created_at) < ? GROUP BY currency",
                (agency_id, start_iso, now_iso),
            ).fetchall()
        }
        nightly = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                "SELECT i.currency, COALESCE(SUM(l.amount_minor), 0)"
                " FROM invoice_lines l JOIN invoices i ON i.invoice_id = l.invoice_id"
                " WHERE i.agency_id = ? AND i.voided_at IS NULL AND l.kind = 'NIGHTLY'"
                "   AND i.issued_at >= ? AND i.issued_at < ? GROUP BY i.currency",
                (agency_id, start_iso, now_iso),
            ).fetchall()
        }
        currencies = sorted(
            set(invoiced) | set(refunded) | set(nightly) | set(nights_by_currency)
        )
        money: dict[str, CurrencyLine] = {}
        for currency in currencies:
            currency_nights = nights_by_currency.get(currency, 0)
            gross = nightly.get(currency, 0)
            billed = invoiced.get(currency, 0)
            returned = refunded.get(currency, 0)
            money[currency] = CurrencyLine(
                currency=currency,
                nights=currency_nights,
                nightly_minor=gross,
                invoiced_minor=billed,
                refunded_minor=returned,
                revenue_net_minor=billed - returned,
                adr_minor=_rate(gross, currency_nights),
                revpar_minor=_rate(gross, managed_unit_nights),
            )

        # ---- the evidence-based companion, gated ---------------------------------
        evidence_rows, evidence_with_ranges = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN occupancy_ranges NOT IN"
            " ('[]', '') THEN 1 ELSE 0 END), 0) FROM availability_evidence"
            " WHERE agency_id = ?",
            (agency_id,),
        ).fetchone()
        evidence_rows = int(evidence_rows or 0)
        evidence_with_ranges = int(evidence_with_ranges or 0)
        coverage = _rate(evidence_with_ranges, evidence_rows)
        if evidence_with_ranges == 0:
            sellable_reason: Optional[str] = SELLABLE_WITHOUT_PRODUCER
        elif coverage is not None and coverage < EVIDENCE_COVERAGE_MINIMUM:
            sellable_reason = (
                f"evidence covers {coverage:.0%} of rows, below the"
                f" {EVIDENCE_COVERAGE_MINIMUM:.0%} gate (ADR-0064 part 2, decision 4)"
            )
        else:
            sellable_reason = (
                "range parser not wired yet: keys inside occupancy_ranges are"
                " defined by no producer, so a number here would be invented"
            )

        occupancy = OccupancySurvey(
            booked_nights=booked_nights,
            managed_unit_nights=managed_unit_nights,
            unavailable_unit_nights=unavailable_unit_nights,
            occupancy_managed=_rate(booked_nights, managed_unit_nights),
            occupancy_sellable=None,
            denominator_label=DENOMINATOR_LABEL,
            evidence_rows=evidence_rows,
            evidence_rows_with_ranges=evidence_with_ranges,
            evidence_coverage=coverage,
            sellable_unavailable_reason=sellable_reason,
        )

        lineage = _lineage(window_days, start_iso, now_iso)
        caveats = (
            "units.status has no history: maintenance/suspended nights are excluded"
            " from supply using today's snapshot, and are reported beside the ratio",
            "cancellations under review (CANCELLATION_REQUESTED, DISPUTED) are not"
            " sold nights; they are counted in the cancellation cohort instead",
            "ADR v1 is the gross nightly rate — discounts are excluded, so net ADR"
            " needs M12 (slice 2)",
            "forecast, anomaly, margin and channel metrics are not part of slice 1",
            "every money figure is per currency; no total across currencies exists",
        )
        return RevenueSnapshot(
            window_start=start_iso,
            window_end=now_iso,
            window_days=window_days,
            occupancy=occupancy,
            arrivals_in_window=arrivals_in_window,
            cancellation_rate=_rate(cancelled_arrivals, arrivals_in_window),
            booking_count=bookings_in_window,
            alos_nights=_rate(booked_nights_in_stay, bookings_in_window),
            money=money,
            lineage=lineage,
            caveats=caveats,
            generated_at=now_iso,
        )
    except RevenueClientError:
        raise
    except Exception as exc:                      # noqa: BLE001
        raise RevenueClientError(
            "revenue telemetry unavailable: query over recorded facts failed",
            detail=str(exc),
        ) from exc


def _lineage(window_days: int, start_iso: str, end_iso: str) -> dict[str, dict[str, Any]]:
    """Where every metric came from: tables, window and filter, per metric."""

    def entry(tables: tuple[str, ...], why: str) -> dict[str, Any]:
        return {
            "tables": list(tables),
            "window_days": window_days,
            "window_start": start_iso,
            "window_end": end_iso,
            "filter": why,
        }

    return {
        "M1": entry(("units", "bookings"),
                    "ACTIVE unit-nights vs sold nights, both clipped to the window"),
        "M2": entry(("invoice_lines", "invoices", "bookings"),
                    "kind='NIGHTLY' on non-voided invoices ÷ that currency's nights"),
        "M3": entry(("invoice_lines", "invoices", "units"),
                    "kind='NIGHTLY' ÷ the official managed unit-nights"),
        "M6": entry(("bookings",),
                    "mean stay length over stays overlapping the window"),
        "M8": entry(("bookings",),
                    "cancelled_at set ÷ arrivals whose check_in is in the window"),
        "M9": entry(("invoices", "payments"),
                    "non-voided invoices issued in window − REFUNDED payments"),
    }


def _require_tables(conn: Any, *tables: str) -> None:
    for table in tables:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not row:
            raise RevenueClientError(
                f"revenue telemetry unavailable: table {table!r} is missing"
            )


# --------------------------------------------------------------------------- #
# identity — read-tier only, enforced by the gateway's ceiling
# --------------------------------------------------------------------------- #
def register_principal(ai: Any, *, secret_ref: str, owner_actor: str,
                       at: Any = None) -> Any:
    """Register this agent as one machine principal among the workforce.

    It registers no tool of its own (ADR-0061 option B): :data:`REVENUE_TOOLS` is a
    declaration of intent, and the gateway's tier and policy layer — not this
    module's good intentions — is what keeps ``payment.approve`` and every other
    write verb out of reach. One agent, twelve workers (ADR-0064 part 1, rule 1).
    """
    if ai is None:
        raise RevenueClientError(
            "no AI service wired: the agent cannot mint its own identity",
            code="REVENUE_NO_AI_SERVICE",
        )
    return ai.register_principal(
        REVENUE_AGENT_NAME, secret_ref=secret_ref, actor=owner_actor, at=at
    )
