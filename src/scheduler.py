"""The heartbeat: one tick that charges every deadline in the system.

Nothing here makes a decision a human should make. The tick only does the work
that is *mechanical and overdue*:

* end holds whose TTL has passed (delegated to the reservation kernel, the single
  writer for inventory),
* expire approval requests nobody answered in time (delegated to the approval
  service, so "expired" keeps meaning exactly what it means there),
* record an SLA breach for a case that is past its first-response or quote time,
* record an SLA breach for an operation task that is overdue,
* record an SLA breach for an unmatched payment past its reconciliation window,
* record an SLA breach for a regulator notice that is past its legal deadline,
* open a payment-chase task for an invoice that is past its due date.

Three properties are non-negotiable, and every one of them is tested:

**Idempotent.** Running the tick twice changes nothing the second time. A cron
entry that fires every five minutes must be boring, not cumulative.

**Bounded.** The tick never confirms a booking, never sends a guest message,
never moves money, and never overwrites a human decision. It only charges
deadlines that a policy already set.

**Honest.** A sweep that cannot run (no kernel, no approval service) says so in
its report instead of reporting zero changes as if all were well.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import AuditLog  # noqa: E402
from primitives import (  # noqa: E402
    DomainError,
    IdFactory,
    SystemClock,
    parse_iso,
    require_ref,
    to_iso,
)
from schema_loader import connect_sqlite  # noqa: E402

DEFAULT_SWEEPS: tuple[str, ...] = (
    "holds",
    "approvals",
    "case_sla",
    "ops_sla",
    "payments",
    "regulator_notice",
    "collections",
)

CHASE_KIND = "PAYMENT_CHASE"
CHASE_HORIZON_HOURS = 24
CHASE_PRIORITY_AFTER_DAYS = 7
CHASE_ESCALATED_PRIORITY = "HIGH"
DEFAULT_CHASE_PRIORITY = "NORMAL"


class SchedulerError(DomainError):
    """Raised when a sweep is misconfigured rather than merely unable to run."""


@dataclass(frozen=True)
class SweepOutcome:
    name: str
    changed: int = 0
    examined: int = 0
    skipped: Optional[str] = None
    notes: tuple[str, ...] = ()

    @property
    def ran(self) -> bool:
        return self.skipped is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "changed": self.changed,
            "examined": self.examined,
            "skipped": self.skipped,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TickReport:
    started_at: str
    finished_at: str
    dry_run: bool
    outcomes: tuple[SweepOutcome, ...] = field(default_factory=tuple)

    @property
    def changed_total(self) -> int:
        return sum(outcome.changed for outcome in self.outcomes)

    @property
    def skipped(self) -> tuple[str, ...]:
        return tuple(o.name for o in self.outcomes if not o.ran)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "changed_total": self.changed_total,
            "skipped": list(self.skipped),
            "outcomes": [o.as_dict() for o in self.outcomes],
        }

    def render(self) -> str:
        lines = [
            f"tick {self.finished_at}{' (dry run)' if self.dry_run else ''}: "
            f"{self.changed_total} change(s)"
        ]
        for outcome in self.outcomes:
            if outcome.skipped:
                lines.append(f"  {outcome.name:<17} skipped: {outcome.skipped}")
            else:
                lines.append(
                    f"  {outcome.name:<17} examined={outcome.examined} "
                    f"changed={outcome.changed}"
                )
            for note in outcome.notes:
                lines.append(f"      - {note}")
        return "\n".join(lines)


def _minutes_between(earlier: str, later: str) -> int:
    seconds = (parse_iso(later) - parse_iso(earlier)).total_seconds()
    return max(int(seconds // 60), 0)


class Scheduler:
    """One object, one tick, every deadline."""

    def __init__(
        self,
        conn: Any,
        *,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        audit: Optional[AuditLog] = None,
        kernel: Any = None,
        approvals: Any = None,
        sweeps: Sequence[str] = DEFAULT_SWEEPS,
        actor: str = "ops.scheduler",
        actor_role: str = "operations",
    ) -> None:
        self.conn = conn
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.audit = audit or AuditLog(conn, clock=self.clock, ids=self.ids)
        self.kernel = kernel
        self.approvals = approvals
        self.sweeps = tuple(sweeps)
        self.actor = require_ref(actor, "actor")
        self.actor_role = require_ref(actor_role, "actor_role")
        self._validate(self.sweeps)

    @staticmethod
    def _validate(sweeps: Iterable[str]) -> tuple[str, ...]:
        names = tuple(sweeps)
        for name in names:
            if not hasattr(Scheduler, f"_sweep_{name}"):
                raise SchedulerError(
                    f"unknown sweep: {name}", code="UNKNOWN_SWEEP", sweep=name,
                )
        return names

    def configure(self, sweeps: Iterable[str]) -> None:
        """Choose which sweeps run; unknown names fail here, not mid-tick."""

        self.sweeps = self._validate(sweeps)

    # -- entry point -------------------------------------------------------
    def tick(self, *, dry_run: bool = False) -> TickReport:
        started = to_iso(self.clock.now())
        outcomes: list[SweepOutcome] = []
        for name in self.sweeps:
            handler = getattr(self, f"_sweep_{name}")
            outcomes.append(handler(dry_run=dry_run))
        finished = to_iso(self.clock.now())
        report = TickReport(
            started_at=started, finished_at=finished, dry_run=dry_run,
            outcomes=tuple(outcomes),
        )
        if not dry_run:
            # One event per tick, not one per row: the tick is the unit of work an
            # operator needs to find in the log, and the rows it touched are in the
            # note. A dry run leaves no trace at all, by design.
            self.audit.append(
                action="scheduler.tick",
                entity_type="system",
                entity_id="scheduler",
                actor=self.actor,
                actor_role=self.actor_role,
                after=report.as_dict(),
            )
        return report

    # -- helpers -----------------------------------------------------------
    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[Any]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def _now_iso(self) -> str:
        return to_iso(self.clock.now())

    def _value(self, row: Any, key: str) -> Any:
        if isinstance(row, Mapping):
            return row[key]
        return row[key] if not isinstance(row, (int, str)) else row

    def _record_breach(
        self,
        *,
        subject_type: str,
        subject_id: str,
        kind: str,
        due_at: str,
        case_id: Optional[str],
        owner_actor: Optional[str],
        dry_run: bool,
        now_iso: str,
    ) -> bool:
        """Insert one breach row; return True when a row was written.

        The UNIQUE(subject_type, subject_id, kind) constraint is the real guard:
        the SELECT below only keeps the common path fast, and a race that slips
        through it is rejected by the database rather than double-counted.
        """

        existing = self.conn.execute(
            "SELECT 1 FROM sla_breaches WHERE subject_type = ? AND subject_id = ? AND kind = ?",
            (subject_type, subject_id, kind),
        ).fetchone()
        if existing is not None:
            return False
        if dry_run:
            return True
        breach_id = self.ids.new("breach")
        try:
            self.conn.execute(
                """
                INSERT INTO sla_breaches (
                    breach_id, subject_type, subject_id, case_id, kind, due_at,
                    detected_at, breach_minutes, owner_actor, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                (
                    breach_id, subject_type, subject_id, case_id, kind, due_at,
                    now_iso, _minutes_between(due_at, now_iso), owner_actor, now_iso,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - unique race or FK problem
            if "UNIQUE" in str(exc).upper():
                return False
            raise SchedulerError(
                f"cannot record {kind} breach for {subject_id}: {exc}",
                code="BREACH_WRITE_FAILED", subject_id=subject_id, kind=kind,
            ) from exc
        self.audit.append(
            action="sla.breach_recorded",
            entity_type=subject_type,
            entity_id=subject_id,
            actor=self.actor,
            actor_role=self.actor_role,
            after={"kind": kind, "due_at": due_at, "detected_at": now_iso},
            reason=f"{kind} due {due_at}",
        )
        return True

    # -- sweeps ------------------------------------------------------------
    def _sweep_holds(self, *, dry_run: bool) -> SweepOutcome:
        now_iso = self._now_iso()
        due = self._rows(
            "SELECT hold_id FROM holds WHERE status = 'ACTIVE' AND expires_at <= ? "
            "ORDER BY hold_id",
            (now_iso,),
        )
        if dry_run:
            return SweepOutcome("holds", changed=len(due), examined=len(due))
        if self.kernel is None:
            return SweepOutcome(
                "holds", examined=len(due),
                skipped="no reservation kernel wired in (inventory has one writer)",
            )
        changed = self.kernel.sweep_expired_holds(now=now_iso)
        return SweepOutcome(
            "holds", changed=len(changed), examined=len(due),
            notes=tuple(changed[:5]),
        )

    def _sweep_approvals(self, *, dry_run: bool) -> SweepOutcome:
        now_iso = self._now_iso()
        due = self._rows(
            "SELECT approval_id FROM approval_requests WHERE status = 'PENDING' "
            "AND expires_at <= ? ORDER BY approval_id",
            (now_iso,),
        )
        if dry_run:
            return SweepOutcome("approvals", changed=len(due), examined=len(due))
        if self.approvals is None:
            return SweepOutcome(
                "approvals", examined=len(due),
                skipped="no approval service wired in (expiry must keep its meaning)",
            )
        changed = self.approvals.expire_due(now=self.clock.now())
        return SweepOutcome(
            "approvals", changed=len(changed), examined=len(due),
            notes=tuple(changed[:5]),
        )

    def _sweep_case_sla(self, *, dry_run: bool) -> SweepOutcome:
        now_iso = self._now_iso()
        rows = self._rows(
            """
            SELECT case_id, first_response_due_at, quote_due_at, owner_actor
            FROM cases
            WHERE status NOT LIKE 'CLOSED_%'
              AND (
                (first_responded_at IS NULL AND first_response_due_at <= ?)
                OR (quoted_at IS NULL AND quote_due_at <= ?)
              )
            ORDER BY case_id
            """,
            (now_iso, now_iso),
        )
        changed = 0
        for row in rows:
            case_id = row["case_id"]
            owner = row["owner_actor"]
            if row["first_response_due_at"] <= now_iso:
                if self.conn.execute(
                    "SELECT first_responded_at FROM cases WHERE case_id = ?", (case_id,)
                ).fetchone()["first_responded_at"] is None:
                    changed += self._record_breach(
                        subject_type="case", subject_id=case_id, kind="FIRST_RESPONSE",
                        due_at=row["first_response_due_at"], case_id=case_id,
                        owner_actor=owner, dry_run=dry_run, now_iso=now_iso,
                    )
            if row["quote_due_at"] <= now_iso:
                if self.conn.execute(
                    "SELECT quoted_at FROM cases WHERE case_id = ?", (case_id,)
                ).fetchone()["quoted_at"] is None:
                    changed += self._record_breach(
                        subject_type="case", subject_id=case_id, kind="QUOTE",
                        due_at=row["quote_due_at"], case_id=case_id,
                        owner_actor=owner, dry_run=dry_run, now_iso=now_iso,
                    )
        return SweepOutcome("case_sla", changed=changed, examined=len(rows))

    def _sweep_ops_sla(self, *, dry_run: bool) -> SweepOutcome:
        now_iso = self._now_iso()
        rows = self._rows(
            "SELECT task_id, due_at, assignee FROM ops_tasks "
            "WHERE status IN ('OPEN','IN_PROGRESS','BLOCKED') AND due_at <= ? "
            "ORDER BY task_id",
            (now_iso,),
        )
        changed = 0
        for row in rows:
            changed += self._record_breach(
                subject_type="ops_task", subject_id=row["task_id"], kind="OPS_TASK",
                due_at=row["due_at"], case_id=None, owner_actor=row["assignee"],
                dry_run=dry_run, now_iso=now_iso,
            )
        return SweepOutcome("ops_sla", changed=changed, examined=len(rows))

    def _sweep_payments(self, *, dry_run: bool) -> SweepOutcome:
        now_iso = self._now_iso()
        rows = self._rows(
            "SELECT recon_id, due_at, owner_actor FROM reconciliation_cases "
            "WHERE status = 'OPEN' AND due_at <= ? ORDER BY recon_id",
            (now_iso,),
        )
        changed = 0
        for row in rows:
            changed += self._record_breach(
                subject_type="payment", subject_id=row["recon_id"], kind="UNMATCHED_PAYMENT",
                due_at=row["due_at"], case_id=None, owner_actor=row["owner_actor"],
                dry_run=dry_run, now_iso=now_iso,
            )
        return SweepOutcome("payments", changed=changed, examined=len(rows))

    def _sweep_regulator_notice(self, *, dry_run: bool) -> SweepOutcome:
        now_iso = self._now_iso()
        rows = self._rows(
            "SELECT incident_id, notice_due_at FROM incidents "
            "WHERE regulatory_notification_required = 1 AND notified_at IS NULL "
            "AND notice_due_at <= ? ORDER BY incident_id",
            (now_iso,),
        )
        changed = 0
        for row in rows:
            changed += self._record_breach(
                subject_type="incident", subject_id=row["incident_id"],
                kind="REGULATOR_NOTICE", due_at=row["notice_due_at"], case_id=None,
                owner_actor=None, dry_run=dry_run, now_iso=now_iso,
            )
        return SweepOutcome("regulator_notice", changed=changed, examined=len(rows))

    def _sweep_collections(self, *, dry_run: bool) -> SweepOutcome:
        """Chase unpaid invoices — by opening a task for a human, never by contact."""

        now_iso = self._now_iso()
        rows = self._rows(
            """
            SELECT invoice_id, due_at, total_minor, currency
            FROM invoices
            WHERE status = 'ISSUED' AND payment_status IN ('UNPAID','PARTIAL') AND due_at <= ?
            ORDER BY due_at, invoice_id
            """,
            (now_iso,),
        )
        now = self.clock.now()
        changed = 0
        notes: list[str] = []
        for row in rows:
            idempotency_key = f"chase:{row['invoice_id']}:{row['due_at']}"
            existing = self.conn.execute(
                "SELECT 1 FROM ops_tasks WHERE idempotency_key = ?", (idempotency_key,),
            ).fetchone()
            if existing is not None:
                continue
            overdue_days = (now - parse_iso(row["due_at"])).days
            priority = (
                CHASE_ESCALATED_PRIORITY if overdue_days >= CHASE_PRIORITY_AFTER_DAYS
                else DEFAULT_CHASE_PRIORITY
            )
            changed += 1
            if dry_run:
                notes.append(f"{row['invoice_id']}: chase task would be opened ({priority})")
                continue
            task_id = self.ids.new("task")
            due_at = to_iso(now + timedelta(hours=CHASE_HORIZON_HOURS))
            self.conn.execute(
                """
                INSERT INTO ops_tasks (
                    task_id, kind, due_at, priority, status, checklist,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
                """,
                (
                    task_id, CHASE_KIND, due_at, priority,
                    json.dumps([
                        f"cek mutasi untuk invoice {row['invoice_id']}",
                        "hubungi tamu lewat DRAFT (bukan kirim otomatis)",
                    ]),
                    idempotency_key, now_iso, now_iso,
                ),
            )
            self.audit.append(
                action="collections.chase_opened",
                entity_type="invoice",
                entity_id=row["invoice_id"],
                actor=self.actor,
                actor_role=self.actor_role,
                after={"task_id": task_id, "priority": priority, "due_at": due_at},
                reason=f"invoice overdue since {row['due_at']}",
            )
            notes.append(f"{row['invoice_id']}: chase task {task_id} ({priority})")
            changed += self._record_breach(
                subject_type="invoice", subject_id=row["invoice_id"],
                kind="PAYMENT_DUNNING", due_at=row["due_at"], case_id=None,
                owner_actor=None, dry_run=False, now_iso=now_iso,
            )
        return SweepOutcome("collections", changed=changed, examined=len(rows), notes=tuple(notes[:5]))


def build_scheduler(conn: Any, *, clock: Any = None, ids: Optional[IdFactory] = None,
                    with_kernel: bool = True, with_approvals: bool = True) -> Scheduler:
    """Wire the real collaborators; import them lazily so the module stays importable."""

    clock = clock or SystemClock()
    ids = ids or IdFactory()
    kernel = None
    approvals = None
    if with_kernel:
        from reservation_kernel import ReservationKernel

        kernel = ReservationKernel(conn, clock=clock, ids=ids)
    if with_approvals:
        from approvals import ApprovalService

        approvals = ApprovalService(conn, clock=clock, ids=ids)
    return Scheduler(
        conn, clock=clock, ids=ids,
        audit=AuditLog(conn, clock=clock, ids=ids),
        kernel=kernel, approvals=approvals,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.environ.get("VILLA_DB", "data/villa.db"))
    parser.add_argument("--now", default=None, help="ISO timestamp to run the tick at")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--json", action="store_true", help="machine readable report")
    parser.add_argument(
        "--sweeps", default=",".join(DEFAULT_SWEEPS),
        help="comma separated subset of sweeps to run",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2

    clock = SystemClock() if args.now is None else _frozen(args.now)
    conn = connect_sqlite(str(db_path))
    try:
        scheduler = build_scheduler(conn, clock=clock)
        scheduler.configure(s for s in args.sweeps.split(",") if s)
        report = scheduler.tick(dry_run=args.dry_run)
    except DomainError as exc:
        print(f"SCHEDULER REFUSED [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(report.render())
    return 0


def _frozen(moment: str) -> Any:
    from primitives import FrozenClock

    return FrozenClock.at(moment)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
