"""Metrics and health for the operations room.

Two jobs, both read-only:

``collect_metrics``
    Turn the database into a flat set of numbers (counters and gauges) that a
    dashboard, a cron mail, or Prometheus can consume. Nothing is computed from
    guesswork: every number is a ``SELECT COUNT(*)`` over a state the schema
    already defines.

``health_checks``
    Answer "may this system be trusted right now?" with a list of named checks.
    A check that cannot be evaluated (missing table, unreadable anchor) reports
    ``UNKNOWN`` rather than silently passing: an alerting system that reports
    healthy when it is blind is worse than no alerting at all.

The module never writes. It is safe to run against production from a read-only
connection, and it never emits personal data: only counts, ages, and ids of
system objects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from anchors import AnchorError, verify_published_anchors  # noqa: E402
from audit import AuditLog  # noqa: E402
from primitives import (  # noqa: E402
    DomainError,
    FrozenClock,
    IdFactory,
    SystemClock,
    parse_iso,
    to_iso,
)
from schema_loader import SCHEMA_VERSION, applied_version, connect_sqlite  # noqa: E402

METRICS_SCHEMA = "villa-metrics/1"
CRITICAL = "CRITICAL"
WARNING = "WARNING"
OK = "OK"
UNKNOWN = "UNKNOWN"
SEVERITY_ORDER = {OK: 0, UNKNOWN: 1, WARNING: 2, CRITICAL: 3}

# Open work is anything not in a terminal state of its own lifecycle.
OPEN_CASE_STATUSES = (
    "NEW", "TRIAGED", "MATCHING", "AWAITING_EVIDENCE",
    "QUOTED", "HOLD_PENDING", "BOOKED", "ESCALATED",
)
TERMINAL_CASE_STATUSES = ("CLOSED_WON", "CLOSED_LOST", "CLOSED_DUPLICATE")
OCCUPYING_HOLD_STATUSES = ("ACTIVE",)
UNPAID_INVOICE_STATUSES = ("UNPAID", "PARTIAL")


class ObservabilityError(DomainError):
    """Raised when a metric or check cannot be evaluated at all."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: str
    detail: str
    value: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity,
            "detail": self.detail,
            "value": self.value,
        }


@dataclass(frozen=True)
class Metrics:
    generated_at: str
    schema_version: str
    counters: Mapping[str, int] = field(default_factory=dict)
    gauges: Mapping[str, float] = field(default_factory=dict)
    labels: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": METRICS_SCHEMA,
            "generated_at": self.generated_at,
            "schema_version": self.schema_version,
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "labels": {name: dict(values) for name, values in self.labels.items()},
        }

    @property
    def worst_severity(self) -> str:  # pragma: no cover - convenience for callers
        return OK


def _scalar(conn: Any, sql: str, params: Iterable[Any] = ()) -> int:
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
    except Exception as exc:  # noqa: BLE001 - surfaced as a domain error with context
        raise ObservabilityError(
            f"metric query failed: {exc}", code="METRIC_QUERY_FAILED", sql=sql,
        ) from exc
    if row is None:
        return 0
    value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
    return int(value or 0)


def _grouped(conn: Any, sql: str) -> dict[str, int]:
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as exc:  # noqa: BLE001
        raise ObservabilityError(
            f"metric query failed: {exc}", code="METRIC_QUERY_FAILED", sql=sql,
        ) from exc
    out: dict[str, int] = {}
    for row in rows:
        if isinstance(row, dict):
            key, value = row["key"], row["value"]
        else:
            key, value = row[0], row[1]
        if key is None:
            continue
        out[str(key)] = int(value or 0)
    return out


def collect_metrics(conn: Any, *, clock: Any = None) -> Metrics:
    """Read every operational counter in one pass. Read-only, no writes."""

    clock = clock or SystemClock()
    now = clock.now()
    now_iso = to_iso(now)

    counters = {
        "audit_events_total": _scalar(conn, "SELECT COUNT(*) FROM audit_events"),
        "bookings_total": _scalar(conn, "SELECT COUNT(*) FROM bookings"),
        "bookings_occupying": _scalar(
            conn,
            "SELECT COUNT(*) FROM bookings WHERE status IN "
            "('CONFIRMED','BALANCE_DUE','BALANCE_OVERDUE','PRE_ARRIVAL','IN_STAY',"
            "'CANCELLATION_REQUESTED','CANCELLATION_REVIEW','DISPUTED')",
        ),
        "holds_active": _scalar(
            conn,
            "SELECT COUNT(*) FROM holds WHERE status = 'ACTIVE'",
        ),
        "holds_expiring_soon": _scalar(
            conn,
            "SELECT COUNT(*) FROM holds WHERE status = 'ACTIVE' "
            "AND expires_at <= ?",
            (_plus_hours(now, 24),),
        ),
        "holds_expired_but_open": _scalar(
            conn,
            "SELECT COUNT(*) FROM holds WHERE status = 'ACTIVE' "
            "AND expires_at <= ?",
            (now_iso,),
        ),
        "cases_total": _scalar(conn, "SELECT COUNT(*) FROM cases"),
        "cases_open": _scalar(
            conn,
            "SELECT COUNT(*) FROM cases WHERE status IN "
            "('NEW','TRIAGED','MATCHING','AWAITING_EVIDENCE','QUOTED',"
            "'HOLD_PENDING','BOOKED','ESCALATED')",
        ),
        "cases_first_response_overdue": _scalar(
            conn,
            "SELECT COUNT(*) FROM cases WHERE first_responded_at IS NULL "
            "AND first_response_due_at <= ? AND status NOT LIKE 'CLOSED_%'",
            (now_iso,),
        ),
        "cases_quote_overdue": _scalar(
            conn,
            "SELECT COUNT(*) FROM cases WHERE quoted_at IS NULL AND quote_due_at <= ? "
            "AND status NOT LIKE 'CLOSED_%'",
            (now_iso,),
        ),
        "sla_breaches_open": _scalar(
            conn, "SELECT COUNT(*) FROM sla_breaches WHERE status = 'OPEN'",
        ),
        "approvals_pending": _scalar(
            conn, "SELECT COUNT(*) FROM approval_requests WHERE status = 'PENDING'",
        ),
        "approvals_expired_but_pending": _scalar(
            conn,
            "SELECT COUNT(*) FROM approval_requests WHERE status = 'PENDING' "
            "AND expires_at <= ?",
            (now_iso,),
        ),
        "invoices_unpaid": _scalar(
            conn,
            "SELECT COUNT(*) FROM invoices WHERE status = 'ISSUED' "
            "AND payment_status IN ('UNPAID','PARTIAL')",
        ),
        "invoices_overdue": _scalar(
            conn,
            "SELECT COUNT(*) FROM invoices WHERE status = 'ISSUED' "
            "AND payment_status IN ('UNPAID','PARTIAL') AND due_at <= ?",
            (now_iso,),
        ),
        "payments_unmatched": _scalar(
            conn,
            "SELECT COUNT(*) FROM payment_events WHERE applied = 0",
        ),
        "reconciliation_open": _scalar(
            conn, "SELECT COUNT(*) FROM reconciliation_cases WHERE status = 'OPEN'",
        ),
        "reconciliation_overdue": _scalar(
            conn,
            "SELECT COUNT(*) FROM reconciliation_cases WHERE status = 'OPEN' "
            "AND due_at <= ?",
            (now_iso,),
        ),
        "ops_tasks_open": _scalar(
            conn,
            "SELECT COUNT(*) FROM ops_tasks WHERE status IN ('OPEN','IN_PROGRESS','BLOCKED')",
        ),
        "ops_tasks_overdue": _scalar(
            conn,
            "SELECT COUNT(*) FROM ops_tasks WHERE status IN ('OPEN','IN_PROGRESS','BLOCKED') "
            "AND due_at <= ?",
            (now_iso,),
        ),
        "incidents_open": _scalar(
            conn, "SELECT COUNT(*) FROM incidents WHERE status IN ('OPEN','MITIGATING')",
        ),
        "incidents_sev1_open": _scalar(
            conn,
            "SELECT COUNT(*) FROM incidents WHERE severity = 'SEV1' "
            "AND status IN ('OPEN','MITIGATING')",
        ),
        "incidents_pending_regulator_notice": _scalar(
            conn,
            "SELECT COUNT(*) FROM incidents WHERE regulatory_notification_required = 1 "
            "AND notified_at IS NULL",
        ),
        "incidents_notice_overdue": _scalar(
            conn,
            "SELECT COUNT(*) FROM incidents WHERE regulatory_notification_required = 1 "
            "AND notified_at IS NULL AND notice_due_at <= ?",
            (now_iso,),
        ),
        "documents_generated": _scalar(
            conn, "SELECT COUNT(*) FROM documents WHERE status = 'GENERATED'",
        ),
        "drafts_awaiting_approval": _scalar(
            conn, "SELECT COUNT(*) FROM message_drafts WHERE status = 'PENDING_APPROVAL'",
        ),
        "drafts_ungrounded": _scalar(
            conn, "SELECT COUNT(*) FROM message_drafts WHERE grounding <> 'GROUNDED'",
        ),
        "intake_fields_proposed": _scalar(
            conn, "SELECT COUNT(*) FROM intake_fields WHERE status = 'PROPOSED'",
        ),
        "ledger_entries_total": _scalar(conn, "SELECT COUNT(*) FROM ledger_entries"),
    }

    labels = {
        "cases_by_status": _grouped(
            conn, "SELECT status AS key, COUNT(*) AS value FROM cases GROUP BY status",
        ),
        "bookings_by_status": _grouped(
            conn, "SELECT status AS key, COUNT(*) AS value FROM bookings GROUP BY status",
        ),
        "invoices_by_payment_status": _grouped(
            conn,
            "SELECT payment_status AS key, COUNT(*) AS value FROM invoices GROUP BY payment_status",
        ),
        "payments_by_status": _grouped(
            conn, "SELECT status AS key, COUNT(*) AS value FROM payments GROUP BY status",
        ),
        "ops_tasks_by_status": _grouped(
            conn, "SELECT status AS key, COUNT(*) AS value FROM ops_tasks GROUP BY status",
        ),
        "incidents_by_severity": _grouped(
            conn,
            "SELECT severity AS key, COUNT(*) AS value FROM incidents "
            "WHERE status IN ('OPEN','MITIGATING') GROUP BY severity",
        ),
        "reconciliation_by_kind": _grouped(
            conn,
            "SELECT kind AS key, COUNT(*) AS value FROM reconciliation_cases "
            "WHERE status = 'OPEN' GROUP BY kind",
        ),
    }

    gauges = {
        "audit_chain_head_seq": float(_scalar(conn, "SELECT COALESCE(MAX(seq), 0) FROM audit_events")),
        "oldest_open_approval_age_hours": _age_hours(
            conn,
            "SELECT MIN(requested_at) FROM approval_requests WHERE status = 'PENDING'",
            now,
        ),
        "oldest_open_reconciliation_age_hours": _age_hours(
            conn,
            "SELECT MIN(detected_at) FROM reconciliation_cases WHERE status = 'OPEN'",
            now,
        ),
        "oldest_overdue_ops_task_age_hours": _age_hours(
            conn,
            "SELECT MIN(due_at) FROM ops_tasks "
            "WHERE status IN ('OPEN','IN_PROGRESS','BLOCKED') AND due_at <= ?",
            now,
            (now_iso,),
        ),
    }

    return Metrics(
        generated_at=now_iso,
        schema_version=SCHEMA_VERSION,
        counters=counters,
        gauges=gauges,
        labels=labels,
    )


def _plus_hours(now: Any, hours: int) -> str:
    from datetime import timedelta

    return to_iso(now + timedelta(hours=hours))


def _age_hours(conn: Any, sql: str, now: Any, params: Iterable[Any] = ()) -> float:
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
    except Exception:  # noqa: BLE001 - an unreadable clock column is not fatal here
        return 0.0
    if row is None:
        return 0.0
    value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
    if value is None:
        return 0.0
    try:
        moment = parse_iso(str(value))
    except DomainError:
        return 0.0
    seconds = (now - moment).total_seconds()
    return round(max(seconds, 0.0) / 3600.0, 2)


def health_checks(
    conn: Any,
    *,
    clock: Any = None,
    anchor_dir: Optional[str | os.PathLike[str]] = None,
) -> tuple[CheckResult, ...]:
    """Named checks, worst first. ``UNKNOWN`` is never ``OK``."""

    clock = clock or SystemClock()
    checks: list[CheckResult] = []

    # 1. The audit chain must verify. This is the root of every other guarantee.
    try:
        report = AuditLog(conn, clock=clock, ids=IdFactory(True, "obs")).verify()
        checks.append(CheckResult(
            "audit_chain", OK,
            f"chain intact up to seq {getattr(report, 'count', 0)}",
            value=int(getattr(report, "count", 0) or 0),
        ))
    except DomainError as exc:
        checks.append(CheckResult(
            "audit_chain", CRITICAL, f"{getattr(exc, 'code', 'AUDIT_ERROR')}: {exc}",
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(CheckResult("audit_chain", UNKNOWN, f"cannot read the chain: {exc}"))

    # 2. Published anchors: without one, deletions at the tail are invisible.
    if anchor_dir is None:
        checks.append(CheckResult(
            "audit_anchor", UNKNOWN, "no anchor directory configured (VILLA_ANCHOR_DIR)",
        ))
    else:
        try:
            verification = verify_published_anchors(
                conn=conn, audit=AuditLog(conn, clock=clock, ids=IdFactory(True, "obs")),
                directory=anchor_dir,
            )
            checks.append(CheckResult(
                "audit_anchor", OK,
                f"anchor {verification.latest_file} matches the chain "
                f"({verification.files_checked} file(s))",
                value=verification.files_checked,
            ))
        except (AnchorError, DomainError) as exc:
            checks.append(CheckResult(
                "audit_anchor", CRITICAL, f"{getattr(exc, 'code', 'ANCHOR_ERROR')}: {exc}",
            ))

    # 3. Schema version must be the one this code was written for.
    try:
        version = applied_version(conn)
    except Exception as exc:  # noqa: BLE001
        version = None
        checks.append(CheckResult("schema_version", UNKNOWN, f"cannot read schema_meta: {exc}"))
    if version is not None:
        if version == SCHEMA_VERSION:
            checks.append(CheckResult("schema_version", OK, version))
        else:
            checks.append(CheckResult(
                "schema_version", CRITICAL,
                f"database reports {version!r}, code expects {SCHEMA_VERSION!r}",
            ))

    # 4. Referential integrity, if the backend can tell us.
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    except Exception:
        rows = None
    if rows is None:
        checks.append(CheckResult(
            "foreign_keys", UNKNOWN, "backend cannot run PRAGMA foreign_key_check",
        ))
    elif rows:
        checks.append(CheckResult(
            "foreign_keys", CRITICAL, f"{len(rows)} dangling reference(s)", value=len(rows),
        ))
    else:
        checks.append(CheckResult("foreign_keys", OK, "no dangling references"))

    metrics = collect_metrics(conn, clock=clock)
    c = metrics.counters

    # 5-9. States that must not persist unnoticed.
    if c["incidents_sev1_open"]:
        checks.append(CheckResult(
            "sev1_incidents", CRITICAL, f"{c['incidents_sev1_open']} open SEV1 incident(s)",
            value=c["incidents_sev1_open"],
        ))
    else:
        checks.append(CheckResult("sev1_incidents", OK, "none open"))

    if c["incidents_pending_regulator_notice"]:
        checks.append(CheckResult(
            "regulator_notice", CRITICAL,
            f"{c['incidents_pending_regulator_notice']} incident(s) awaiting a regulator notice",
            value=c["incidents_pending_regulator_notice"],
        ))
    else:
        checks.append(CheckResult("regulator_notice", OK, "no pending notice"))

    if c["incidents_notice_overdue"]:
        checks.append(CheckResult(
            "regulator_notice_sla", WARNING,
            f"{c['incidents_notice_overdue']} incident(s) past the notice deadline and not notified",
            value=c["incidents_notice_overdue"],
        ))
    else:
        checks.append(CheckResult("regulator_notice_sla", OK, "no notice past its deadline"))

    if c["reconciliation_overdue"]:
        checks.append(CheckResult(
            "unmatched_payments_sla", WARNING,
            f"{c['reconciliation_overdue']} unmatched/duplicate payment(s) past the 48h SLA",
            value=c["reconciliation_overdue"],
        ))
    else:
        checks.append(CheckResult("unmatched_payments_sla", OK, "within SLA"))

    if c["approvals_expired_but_pending"]:
        checks.append(CheckResult(
            "approval_expiry", WARNING,
            f"{c['approvals_expired_but_pending']} approval(s) past expiry, still PENDING",
            value=c["approvals_expired_but_pending"],
        ))
    else:
        checks.append(CheckResult("approval_expiry", OK, "no expired approvals pending"))

    if c["holds_expired_but_open"]:
        checks.append(CheckResult(
            "stale_holds", WARNING,
            f"{c['holds_expired_but_open']} hold(s) past expiry but still open",
            value=c["holds_expired_but_open"],
        ))
    else:
        checks.append(CheckResult("stale_holds", OK, "no stale holds"))

    if c["cases_first_response_overdue"] or c["cases_quote_overdue"]:
        checks.append(CheckResult(
            "case_sla", WARNING,
            f"{c['cases_first_response_overdue']} first-response and "
            f"{c['cases_quote_overdue']} quote SLA breach(es)",
            value=c["cases_first_response_overdue"] + c["cases_quote_overdue"],
        ))
    else:
        checks.append(CheckResult("case_sla", OK, "no case SLA breach"))

    if c["ops_tasks_overdue"]:
        checks.append(CheckResult(
            "ops_sla", WARNING, f"{c['ops_tasks_overdue']} overdue operation task(s)",
            value=c["ops_tasks_overdue"],
        ))
    else:
        checks.append(CheckResult("ops_sla", OK, "no overdue operation task"))

    # 10. Honesty about automation: nothing may be sent that a human did not approve.
    if c["drafts_awaiting_approval"]:
        checks.append(CheckResult(
            "outbound_control", OK,
            f"{c['drafts_awaiting_approval']} draft(s) correctly waiting for a human",
            value=c["drafts_awaiting_approval"],
        ))
    else:
        checks.append(CheckResult("outbound_control", OK, "no draft waiting"))

    checks.sort(key=lambda item: (-SEVERITY_ORDER[item.severity], item.name))
    return tuple(checks)


def render_prometheus(metrics: Metrics) -> str:
    """Prometheus text exposition format, deterministic ordering."""

    lines: list[str] = []
    for name in sorted(metrics.counters):
        lines.append(f"villa_{name} {metrics.counters[name]}")
    for name in sorted(metrics.labels):
        for key in sorted(metrics.labels[name]):
            lines.append(f'villa_{name}{{key="{key}"}} {metrics.labels[name][key]}')
    for name in sorted(metrics.gauges):
        lines.append(f"villa_{name} {metrics.gauges[name]}")
    # Deterministic output matters more than grouping here: two snapshots of the
    # same database must produce byte-identical text, which makes diffing a
    # scrape against the previous one meaningful.
    return "\n".join(sorted(lines)) + "\n"


def render_health(checks: Iterable[CheckResult], *, style: str = "text") -> str:
    checks = tuple(checks)
    if style == "text":
        return "\n".join(f"{c.severity:<8} {c.name:<24} {c.detail}" for c in checks)
    if style == "json":
        return json.dumps([c.as_dict() for c in checks], sort_keys=True)
    raise ObservabilityError(f"unknown style: {style}", code="UNKNOWN_STYLE")


def worst_severity(checks: Iterable[CheckResult]) -> str:
    worst = OK
    for check in checks:
        if SEVERITY_ORDER[check.severity] > SEVERITY_ORDER[worst]:
            worst = check.severity
    return worst


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.environ.get("VILLA_DB", "data/villa.db"))
    parser.add_argument("--anchor-dir", default=os.environ.get("VILLA_ANCHOR_DIR"))
    parser.add_argument("--format", choices=("text", "json", "prometheus"), default="text")
    parser.add_argument(
        "--now", default=None,
        help="ISO timestamp to evaluate the snapshot at (defaults to the real clock)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2

    clock = FrozenClock.at(args.now) if args.now else SystemClock()
    conn = connect_sqlite(str(db_path))
    try:
        metrics = collect_metrics(conn, clock=clock)
        checks = health_checks(conn, clock=clock, anchor_dir=args.anchor_dir)
    finally:
        conn.close()

    worst = worst_severity(checks)
    if args.format == "prometheus":
        sys.stdout.write(render_prometheus(metrics))
        return 0 if worst in (OK, UNKNOWN) else 1
    if args.format == "json":
        print(json.dumps(
            {
                "metrics": metrics.as_dict(),
                "checks": [c.as_dict() for c in checks],
                "worst": worst,
            },
            indent=2, sort_keys=True,
        ))
    else:
        print(render_health(checks))
        print()
        for name in sorted(metrics.counters):
            print(f"{name:<38} {metrics.counters[name]}")
        print()
        print(f"WORST SEVERITY: {worst}")
    return 0 if worst in (OK, UNKNOWN) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
