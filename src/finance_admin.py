"""Agent 5 — the Finance Admin AI Agent: read-only financial telemetry.

§11 of the AI Workforce Architecture V1.1. The agent *administers* — it reads
the money facts the services already recorded and turns them into ranked
reports for the human Finance Manager. It moves no money, resolves no
exception, and opens no case: those paths stay in ``payments.py`` /
``invoice_views.py`` / ``supplier_settlements.py`` behind human decisions.

What this module owns — and refuses to do:

- ``collect()`` answers one question in one instant: how much AR is open and
  how stale (aged from ``due_at``), how much was collected, how much AP is
  open per supplier route, how many exceptions the payments service opened
  and how many are past their deadline, how many webhook rows were recorded
  without a verified signature, what the ledger says per currency, and what
  the KPI families of §11 read from the same facts.
- Every number is read from recorded rows — invoices for AR and vacancies,
  supplier_settlements for AP, reconciliation_cases for exceptions,
  payment_events for webhook hygiene, ledger_entries for cash. Nothing is
  estimated between them.
- The report writes nothing. Read-only is enforced by query shape: the
  collector only SELECTs. Missing facts are refused loudly
  (``FinanceClientError``), never swallowed into a zero.

Authority is structural: the report is derived data with zero write paths;
every §56 money action (refund, payout, charge) stays a HIGH-tier human call
in the services that already own it. The agent may *recommend* — the human
*decides*.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

import primitives as P

ZERO_SHA256 = "0" * 64

AP_ACTIONS: tuple[str, ...] = ("DUE", "PAID")

AR_BUCKETS: tuple[str, ...] = ("0-30", "31-60", "61-90", "90+")

_BUCKET_LABELS: dict[str, str] = {
    "0-30": "0-30 days from due date",
    "31-60": "31-60 days from due date",
    "61-90": "61-90 days from due date",
    "90+": "more than 90 days from due date",
}


class FinanceClientError(P.DomainError):
    """The telemetry cannot answer against the storage it was given."""

    code = "FINANCE_TELEMETRY_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# report rows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AgedBucket:
    """One aging bucket: open invoices and their open minor total per currency."""

    label: str
    count: int
    minor_by_currency: dict[str, int]

    @property
    def count_and_minor(self) -> tuple[int, int]:
        return (self.count, sum(self.minor_by_currency.values()))


@dataclass(frozen=True)
class ARAging:
    buckets: dict[str, AgedBucket]              # 0-30 … 90+ -> bucket
    open_count: int                             # open invoices
    total_open_minor_by_currency: dict[str, int]
    total_collected_minor_by_currency: dict[str, int]
    void_count: int


@dataclass(frozen=True)
class APPayables:
    open_count: int                             # bookings with remaining AP
    total_open_minor_by_currency: dict[str, int]
    settled_minor_by_currency: dict[str, int]
    paid_without_due_count: int


@dataclass(frozen=True)
class DepositSurvey:
    resolutions_by_action: dict[str, int]       # RETURNED / FORFEITED / APPLIED


@dataclass(frozen=True)
class ExceptionSurvey:
    open_count: int
    open_by_kind: dict[str, int]
    overdue_count: int
    unverified_webhook_events: int


@dataclass(frozen=True)
class Exposure:
    cash_by_currency: dict[str, int]


@dataclass(frozen=True)
class FinanceKPI:
    collected_rate: float | None
    overdue_ar_share: float | None
    open_exception_count: int


@dataclass(frozen=True)
class FinanceSnapshot:
    ar: ARAging
    ap: APPayables
    deposits: DepositSurvey
    exceptions: ExceptionSurvey
    exposure: Exposure
    kpi: FinanceKPI
    generated_at: str


def _sum_by_currency(rows: list[Any], amount_index: int,
                     currency_index: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        amount = int(row[amount_index])
        currency = str(row[currency_index])
        out[currency] = out.get(currency, 0) + amount
    return out


def _rate(numerator: int | None, denominator: int | None) -> float | None:
    if not denominator:
        return None
    return round((numerator or 0) / denominator, 4)


def _bucket_for(days_overdue: int) -> str:
    if days_overdue <= 30:
        return "0-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


# --------------------------------------------------------------------------- #
# collector — read-only
# --------------------------------------------------------------------------- #
def collect(conn: Any, *, clock: Any, agency_id: str) -> FinanceSnapshot:
    """Answer the §11 families from the recorded facts. SELECTs only."""
    now_iso = P.to_iso(clock.now())
    _require_tables(conn, "invoices", "supplier_settlements",
                    "deposit_resolutions", "ledger_entries",
                    "payment_events")
    _require_tables(conn, "reconciliation_cases",
                    table_hint="reconciliation cases")
    try:
        # ---- AR: open invoices aged from due_at, VOID excluded ------------
        void_count = conn.execute(
            "SELECT COUNT(*) FROM invoices"
            " WHERE agency_id = ? AND status = 'VOID'", (agency_id,)
        ).fetchone()[0]
        open_rows = conn.execute(
            "SELECT (total_minor - amount_paid_minor) AS open_minor,"
            " currency, CAST(julianday(?) - julianday(due_at) AS INTEGER)"
            " AS age_days"
            " FROM invoices"
            " WHERE agency_id = ? AND status = 'ISSUED'"
            " AND (total_minor - amount_paid_minor) > 0",
            (now_iso, agency_id),
        ).fetchall()
        buckets: dict[str, AgedBucket] = {}
        tot_open: dict[str, int] = {}
        open_count = 0
        overdue_minor = 0
        for row in open_rows:
            open_minor = int(row[0])
            currency = str(row[1])
            age_days = int(row[2])
            label = _bucket_for(max(age_days, 0))
            bucket = buckets.get(label) or AgedBucket(
                label=_BUCKET_LABELS[label], count=0,
                minor_by_currency={},
            )
            buckets[label] = AgedBucket(
                label=bucket.label, count=bucket.count + 1,
                minor_by_currency={
                    **bucket.minor_by_currency,
                    currency: bucket.minor_by_currency.get(currency, 0)
                    + open_minor,
                },
            )
            tot_open[currency] = tot_open.get(currency, 0) + open_minor
            open_count += 1
            if age_days > 0:
                overdue_minor += open_minor
        for label in _BUCKET_LABELS:
            if label not in buckets:
                buckets[label] = AgedBucket(
                    label=_BUCKET_LABELS[label], count=0, minor_by_currency={},
                )
        collected = conn.execute(
            "SELECT amount_paid_minor, currency FROM invoices"
            " WHERE agency_id = ? AND status = 'ISSUED'"
            " AND amount_paid_minor > 0", (agency_id,),
        ).fetchall()
        collected_by_ccy = _sum_by_currency(collected, 0, 1)
        ar = ARAging(
            buckets=buckets, open_count=open_count,
            total_open_minor_by_currency=tot_open,
            total_collected_minor_by_currency=collected_by_ccy,
            void_count=int(void_count),
        )

        # ---- AP: settlements per route/booking ----------------------------
        ap_by_booking: dict[str, dict[str, int]] = {}
        actions = conn.execute(
            "SELECT booking_id, action, amount_minor, currency, created_at"
            " FROM supplier_settlements WHERE agency_id = ?"
            " ORDER BY created_at, supplier_settlement_id", (agency_id,),
        ).fetchall()
        for booking_id, action, amount, currency, _ in actions:
            slot = ap_by_booking.setdefault(booking_id, {})
            key = f"{action}:{currency}"
            slot[key] = slot.get(key, 0) + int(amount)
        open_ap: dict[str, int] = {}
        open_ap_count = 0
        settled_open: dict[str, int] = {}
        paid_without_due_count = 0
        for booking_id, slot in ap_by_booking.items():
            for currency in sorted({
                key.split(":", 1)[1] for key in slot
            }):
                due = slot.get(f"DUE:{currency}", 0)
                paid = slot.get(f"PAID:{currency}", 0)
                # A PAID with no DUE is money out against nothing owed, and an
                # overpayment is the same defect at a different scale.
                if paid > 0 and (due == 0 or paid > due):
                    paid_without_due_count += 1
                if due:
                    remaining = max(due - paid, 0)
                    if remaining > 0:
                        open_ap[currency] = open_ap.get(currency, 0) + remaining
                        settled_open[currency] = settled_open.get(
                            currency, 0
                        ) + paid
                        open_ap_count += 1
        ap = APPayables(
            open_count=open_ap_count,
            total_open_minor_by_currency=open_ap,
            settled_minor_by_currency=settled_open,
            paid_without_due_count=paid_without_due_count,
        )

        # ---- deposits -----------------------------------------------------
        dep_rows = conn.execute(
            "SELECT action, COUNT(*) FROM deposit_resolutions"
            " WHERE agency_id = ? GROUP BY action", (agency_id,),
        ).fetchall()
        deposits = DepositSurvey(
            resolutions_by_action={str(r[0]): int(r[1]) for r in dep_rows}
        )

        # ---- exceptions -----------------------------------------------------
        exc_rows = conn.execute(
            "SELECT kind, COUNT(*),"
            " SUM(CASE WHEN due_at <= ? THEN 1 ELSE 0 END)"
            " FROM reconciliation_cases"
            " WHERE agency_id = ? AND status = 'OPEN' GROUP BY kind",
            (now_iso, agency_id),
        ).fetchall()
        open_by_kind = {str(r[0]): int(r[1]) for r in exc_rows}
        open_count_cases = sum(open_by_kind.values())
        overdue = sum(int(r[2] or 0) for r in exc_rows)
        unverified = conn.execute(
            "SELECT COUNT(*) FROM payment_events"
            " WHERE signature_verified = 0", (),
        ).fetchone()[0]
        exceptions = ExceptionSurvey(
            open_count=open_count_cases,
            open_by_kind=open_by_kind,
            overdue_count=overdue,
            unverified_webhook_events=int(unverified),
        )

        # ---- exposure: ledger cash per currency ----------------------------
        # Cash on hand is the CASH_IN account balance only. Summing DEBIT minus
        # CREDIT across every account nets to zero on a balanced journal.
        cash_rows = conn.execute(
            "SELECT currency,"
            " SUM(CASE WHEN direction = 'DEBIT' THEN amount_minor"
            "          ELSE -amount_minor END)"
            " FROM ledger_entries WHERE account = 'CASH_IN' GROUP BY currency"
        ).fetchall()
        cash = {str(r[0]): int(r[1] or 0) for r in cash_rows}
        exposure = Exposure(cash_by_currency=cash)

        # ---- KPI ------------------------------------------------------------
        denom = sum(tot_open.values()) + sum(collected_by_ccy.values())
        kpi = FinanceKPI(
            collected_rate=_rate(sum(collected_by_ccy.values()), denom),
            overdue_ar_share=_rate(overdue_minor, sum(tot_open.values())),
            open_exception_count=open_count_cases,
        )

        return FinanceSnapshot(
            ar=ar, ap=ap, deposits=deposits, exceptions=exceptions,
            exposure=exposure, kpi=kpi, generated_at=now_iso,
        )
    except FinanceClientError:
        raise
    except Exception as exc:                      # noqa: BLE001
        raise FinanceClientError(
            "finance telemetry unavailable: query over recorded facts failed",
            detail=str(exc),
        ) from exc


def _require_tables(conn: Any, *tables: str, table_hint: str | None = None) -> None:
    for table in tables:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not row:
            raise FinanceClientError(
                f"finance telemetry unavailable: table {table!r} is missing"
            )


def entry_fingerprint(entry: dict[str, Any]) -> str:
    """sha256 of the canonical JSON body — reuse the payments conventions."""
    body = {key: entry[key] for key in sorted(entry)}
    return hashlib.sha256(
        repr(body).encode("utf-8")
    ).hexdigest()


def aging_bucket_label(bucket: str) -> str:
    """Human label for one aging bucket (used by the report renderer)."""
    return _BUCKET_LABELS.get(bucket, bucket)


def shift_instant(iso: str, days: int) -> str:
    """Day-shift one ISO instant by ``days`` (aging window maths helper)."""
    return P.to_iso(P.parse_iso(iso) + timedelta(days=days))


# --------------------------------------------------------------------------- #
# identity — read-tier only, enforced by the gateway's ceiling
# --------------------------------------------------------------------------- #
#: §11 read-tier tools. Every verb here is a read: the agent ranks money facts
#: for a human. No payment/refund/payout/bank verb may be added from this
#: module — those stay HIGH-tier human calls in the services that own them.
FINANCE_TOOLS: tuple[tuple[str, str, Optional[str], str], ...] = (
    ("finance.ar_aging", "LOW", None, "Read open AR and its age from due_at"),
    ("finance.ap_payables", "LOW", None, "Read open AP per supplier route"),
    ("finance.exposure", "LOW", None, "Read ledger cash and exception counts"),
)

#: The name the Control Tower's closed vocabulary routes FINANCE_ADMIN to.
FINANCE_AGENT_NAME = "finance-admin-ai"


def ensure_finance_tools(conn: Any, *, at: str) -> int:
    """Insert the finance agent's read-tier tools if absent. Idempotent; a policy act."""
    inserted = 0
    for name, tier, policy_key, description in FINANCE_TOOLS:
        try:
            conn.execute(
                "INSERT INTO ai_tools (tool, tier, policy_key, spec_ref,"
                " description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, tier, policy_key, "§11", description, at),
            )
            inserted += 1
        except Exception:  # noqa: BLE001 - already present is the happy path
            pass  # idempotent by design
    return inserted


def register_principal(ai: Any, *, secret_ref: str, owner_actor: str,
                       at: Any = None) -> Any:
    """Register the finance agent as one machine principal among the workforce.

    Its tools are read-tier (see :data:`FINANCE_TOOLS`); the gateway's tier
    ceiling — not this module's good intentions — is what keeps an administrator
    from approving money.
    """
    if ai is None:
        raise FinanceClientError(
            "no AI service wired: the agent cannot mint its own identity",
            code="FIN_NO_AI_SERVICE",
        )
    return ai.register_principal(
        FINANCE_AGENT_NAME, secret_ref=secret_ref, actor=owner_actor, at=at
    )
