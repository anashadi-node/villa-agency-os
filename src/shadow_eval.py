"""§22/D7 shadow-mode measurement — did the agent decide what the human decided?

Authority chain: ADR-0038 (the owner's bar: agreement ≥ 95%, unsupported claims = exactly 0,
volume ≥ 100 cases, rolling per agent and per worker), ``intake_proposals.method='llm_shadow'``
(the estate's built-in shadow channel), and ``message_drafts.grounding`` (the gate that keeps an
unsupported sentence from reaching a guest).

What this module is, and what it refuses to be:

* **It measures.** For every extractor that proposed fields in shadow mode, it pairs each
  decided field with the human's review: ``ACCEPTED`` is agreement, ``REJECTED`` is
  disagreement, and a proposal with no source span is an unsupported claim — text that claims
  a fact but is not anchored to the message it supposedly came from.
* **It does not graduate anyone.** The bar is machine-readable; the *decision* is a human
  approval (ADR-0038), not a flag this module flips. ``meets_bar`` is an input to that
  approval, never the approval itself.
* **A tenant may set a stricter bar, never a looser one.** The bars are policy keys with the
  platform baseline (``governance.shadow.min_agreement_rate``, ``governance.shadow.min_cases``);
  a tenant revision below the baseline is refused by :class:`TenantBarLooser`, because
  "looser" is exactly what a bar exists to refuse.

Every number is a count over recorded facts — no message text, no identity (§8/§56).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Optional

import policy_engine as PE
from decimal import Decimal  # exact arithmetic for a bar, never float
from primitives import Clock, DomainError, SystemClock, require_ref

__all__ = [
    "AGREEMENT_BAR_KEY",
    "CASES_BAR_KEY",
    "ShadowEvalError",
    "TenantBarLooser",
    "ShadowReport",
    "ExtractorMeasurement",
    "collect",
]

#: The bars, as policy keys with a platform baseline (ADR-0038: machine-readable, a change is
#: a revision with a reason, a tenant may go stricter but never looser).
AGREEMENT_BAR_KEY = "governance.shadow.min_agreement_rate"
CASES_BAR_KEY = "governance.shadow.min_cases"

#: The platform baselines, spelled once — the seed and this module must agree, and the test
#: pins them to ADR-0038's numbers.
BASELINE_AGREEMENT_PCT = 95
BASELINE_MIN_CASES = 100


class ShadowEvalError(DomainError):
    """Base class for deliberate refusals from this module."""

    code = "SHADOW_EVAL_ERROR"


class TenantBarLooser(ShadowEvalError):
    """A tenant tried to set a bar below the platform baseline."""

    code = "SHADOW_BAR_LOOSER_THAN_BASELINE"


@dataclass(frozen=True)
class ExtractorMeasurement:
    """One shadow extractor's rolling numbers — counts and ratios, nothing person-shaped."""

    extractor: str
    cases: int = 0
    decided: int = 0
    agreed: int = 0
    disagreed: int = 0
    pending: int = 0
    unsupported: int = 0
    agreement_rate: Optional[float] = None
    meets_bar: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "extractor": self.extractor,
            "cases": self.cases,
            "decided": self.decided,
            "agreed": self.agreed,
            "disagreed": self.disagreed,
            "pending": self.pending,
            "unsupported": self.unsupported,
            "agreement_rate": self.agreement_rate,
            "meets_bar": self.meets_bar,
        }


@dataclass(frozen=True)
class ShadowReport:
    """One instant's answer: per-extractor measurements, plus the one platform invariant."""

    window_days: int = 90
    extractors: Mapping[str, ExtractorMeasurement] = field(default_factory=dict)
    #: Drafts that reached a guest (``APPROVED``/``SENT``) without being ``GROUNDED`` — the
    #: schema refuses them, so this must always be 0; it is measured, not assumed.
    ungrounded_reached_guest: int = 0
    bars: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "extractors": {name: m.as_dict() for name, m in self.extractors.items()},
            "ungrounded_reached_guest": self.ungrounded_reached_guest,
            "bars": dict(self.bars),
        }


def _cutoff(clock: Clock, window_days: int, at: Any) -> str:
    moment = at if at is not None else clock.now()
    if hasattr(moment, "tzinfo") and moment.tzinfo is not None:
        return (moment - timedelta(days=window_days)).isoformat()
    raise ValueError("at must be an aware datetime")


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    return int(conn.execute(sql, params).fetchone()["n"])


def _bars(conn: sqlite3.Connection, agency_id: str, clock: Clock, at: Any) -> dict[str, Any]:
    """The effective bars: the tenant's revision, but never below the platform baseline.

    The revision is resolved at the *same* instant the measurements are taken — a report
    about September must not read October's bar.
    """
    resolver = PE.PolicyResolver(conn, clock=clock, agency_id=agency_id)
    moment = at if at is not None else clock.now()
    agreement_pct = resolver.resolve(AGREEMENT_BAR_KEY, at=moment).as_decimal()
    cases = resolver.resolve(CASES_BAR_KEY, at=moment).as_int()
    baseline_fraction = Decimal(BASELINE_AGREEMENT_PCT) / 100
    if agreement_pct / 100 < baseline_fraction:
        raise TenantBarLooser(
            "a tenant's agreement bar may be stricter than the platform baseline, never looser",
            policy_key=AGREEMENT_BAR_KEY,
            tenant=agreement_pct,
            baseline=BASELINE_AGREEMENT_PCT,
        )
    if cases < BASELINE_MIN_CASES:
        raise TenantBarLooser(
            "a tenant's volume bar may be stricter than the platform baseline, never looser",
            policy_key=CASES_BAR_KEY,
            tenant=cases,
            baseline=BASELINE_MIN_CASES,
        )
    return {"agreement_rate": agreement_pct / 100, "min_cases": cases}


def collect(
    conn: sqlite3.Connection,
    *,
    agency_id: str,
    window_days: int = 90,
    at: Any = None,
    clock: Optional[Clock] = None,
) -> ShadowReport:
    """Measure every shadow extractor over a rolling window, against the effective bars."""
    require_ref(agency_id, "agency_id")
    if window_days < 1:
        raise ValueError("window_days must be at least 1")
    clock = clock or SystemClock()
    cutoff = _cutoff(clock, window_days, at)
    bars = _bars(conn, agency_id, clock, at)

    extractors: dict[str, ExtractorMeasurement] = {}
    rows = conn.execute(
        "SELECT DISTINCT p.extractor FROM intake_proposals p"
        " JOIN cases c ON c.case_id = p.case_id"
        " WHERE c.agency_id = ? AND p.method = 'llm_shadow' AND p.produced_at >= ?"
        " ORDER BY p.extractor",
        (agency_id, cutoff),
    ).fetchall()
    for row in rows:
        extractor = str(row["extractor"])
        base = (
            " FROM intake_fields f JOIN intake_proposals p ON p.proposal_id = f.proposal_id"
            " JOIN cases c ON c.case_id = f.case_id"
            " WHERE c.agency_id = ? AND p.method = 'llm_shadow' AND p.extractor = ?"
            " AND p.produced_at >= ?"
        )
        params = (agency_id, extractor, cutoff)
        cases = _count(
            conn,
            "SELECT COUNT(DISTINCT f.case_id) AS n" + base,
            params,
        )
        decided = _count(
            conn, "SELECT COUNT(*) AS n" + base + " AND f.status IN ('ACCEPTED','REJECTED')", params
        )
        agreed = _count(conn, "SELECT COUNT(*) AS n" + base + " AND f.status = 'ACCEPTED'", params)
        pending = _count(conn, "SELECT COUNT(*) AS n" + base + " AND f.status = 'PROPOSED'", params)
        unsupported = _count(
            conn,
            "SELECT COUNT(*) AS n" + base + " AND f.source_span_start IS NULL",
            params,
        )
        disagreed = decided - agreed
        rate = round(agreed / decided, 4) if decided else None
        meets = (
            cases >= bars["min_cases"]
            and decided > 0
            and rate is not None
            and rate >= bars["agreement_rate"]
            and unsupported == 0
        )
        extractors[extractor] = ExtractorMeasurement(
            extractor=extractor,
            cases=cases,
            decided=decided,
            agreed=agreed,
            disagreed=disagreed,
            pending=pending,
            unsupported=unsupported,
            agreement_rate=rate,
            meets_bar=meets,
        )

    ungrounded = _count(
        conn,
        "SELECT COUNT(*) AS n FROM message_drafts d"
        " JOIN cases c ON c.case_id = d.case_id"
        " WHERE c.agency_id = ? AND d.status IN ('APPROVED','SENT') AND d.grounding <> 'GROUNDED'",
        (agency_id,),
    )
    return ShadowReport(
        window_days=window_days,
        extractors=extractors,
        ungrounded_reached_guest=ungrounded,
        bars=bars,
    )
