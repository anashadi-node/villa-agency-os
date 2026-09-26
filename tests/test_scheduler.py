"""Scheduler tests: the heartbeat must be boring, bounded and honest.

The interesting cases are the second tick (idempotence), the dry run (no writes),
the missing collaborator (honest skip, never a silent zero), and the boundaries
(a deadline that has just not passed must not be charged).
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import scheduler as SCH  # noqa: E402
from approvals import ApprovalService  # noqa: E402
from audit import AuditLog  # noqa: E402
from primitives import FrozenClock, IdFactory  # noqa: E402
from reservation_kernel import ReservationKernel  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402
from seed_support import CREATED, DAY, NOW, PAST, SeedMixin  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


class SchedulerCase(unittest.TestCase, SeedMixin):
    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.clock = FrozenClock.at(NOW)
        self.ids = IdFactory(True, "sched")
        self.audit = AuditLog(self.conn, clock=self.clock, ids=self.ids)
        self.kernel = ReservationKernel(self.conn, clock=self.clock, ids=self.ids)
        self.approvals = ApprovalService(self.conn, clock=self.clock, ids=self.ids)
        self.scheduler = SCH.Scheduler(
            self.conn, clock=self.clock, ids=self.ids, audit=self.audit,
            kernel=self.kernel, approvals=self.approvals,
        )
        self.inventory()

    # -- helpers -----------------------------------------------------------
    def breaches(self) -> list[tuple[str, str, str]]:
        rows = self.conn.execute(
            "SELECT subject_type, subject_id, kind FROM sla_breaches ORDER BY 1, 2, 3"
        ).fetchall()
        return [tuple(row) for row in rows]

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def tick(self, **kwargs):
        return self.scheduler.tick(**kwargs)

    # -- empty system ------------------------------------------------------
    def test_a_tick_on_an_empty_system_changes_nothing(self):
        report = self.tick()
        self.assertEqual(report.changed_total, 0)
        self.assertEqual(report.skipped, ())
        self.assertEqual(self.breaches(), [])

    def test_a_tick_records_exactly_one_audit_event(self):
        before = self.count("audit_events")
        self.tick()
        self.assertEqual(self.count("audit_events"), before + 1)
        event = self.conn.execute(
            "SELECT action, entity_type, entity_id, after_state FROM audit_events "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["action"], "scheduler.tick")
        self.assertEqual((event["entity_type"], event["entity_id"]), ("system", "scheduler"))
        payload = json.loads(event["after_state"])
        self.assertEqual(payload["changed_total"], 0)
        self.assertIn("outcomes", payload)

    def test_a_dry_run_writes_nothing_at_all(self):
        self.case(first_due=PAST, quote_due=PAST)
        self.ops_task(due_at=PAST)
        self.reconciliation(due_at=PAST)
        self.inventory_rows = self.count("audit_events")
        report = self.tick(dry_run=True)
        self.assertEqual(self.breaches(), [])
        self.assertEqual(self.count("audit_events"), self.inventory_rows)
        self.assertEqual(self.count("sla_breaches"), 0)
        self.assertTrue(report.dry_run)
        self.assertGreater(report.changed_total, 0)
        self.assertIn("(dry run)", report.render())

    def test_an_unknown_sweep_is_refused_at_construction(self):
        with self.assertRaises(SCH.SchedulerError) as caught:
            SCH.Scheduler(self.conn, clock=self.clock, ids=self.ids, sweeps=("holds", "nope"))
        self.assertEqual(caught.exception.code, "UNKNOWN_SWEEP")

    # -- cases -------------------------------------------------------------
    def test_overdue_first_response_and_quote_are_charged_once(self):
        self.case(first_due=PAST, quote_due=PAST)
        first = self.tick()
        self.assertEqual(
            self.breaches(),
            [("case", "case_0001", "FIRST_RESPONSE"), ("case", "case_0001", "QUOTE")],
        )
        second = self.tick()
        self.assertEqual(second.changed_total, 0)
        self.assertEqual(len(self.breaches()), 2)
        self.assertGreater(first.changed_total, 0)

    def test_an_answered_case_is_never_charged(self):
        self.case(first_due=PAST, quote_due=PAST, first_answered=True, quoted=True)
        self.assertEqual(self.tick().changed_total, 0)
        self.assertEqual(self.breaches(), [])

    def test_a_closed_case_is_never_charged(self):
        self.case(status="CLOSED_LOST", first_due=PAST, quote_due=PAST)
        self.assertEqual(self.tick().changed_total, 0)
        self.assertEqual(self.breaches(), [])

    def test_a_deadline_in_the_future_is_not_charged(self):
        self.case(first_due=DAY, quote_due=DAY)
        self.assertEqual(self.tick().changed_total, 0)
        self.assertEqual(self.breaches(), [])

    def test_breach_minutes_reflect_the_real_lateness(self):
        self.case(first_due=PAST, quote_due=DAY)
        self.tick()
        row = self.conn.execute(
            "SELECT due_at, detected_at, breach_minutes, status, owner_actor "
            "FROM sla_breaches WHERE kind = 'FIRST_RESPONSE'"
        ).fetchone()
        # PAST = 2026-09-20T08:00Z, NOW = 2026-09-21T08:00Z -> exactly one day.
        self.assertEqual(row["due_at"], PAST)
        self.assertEqual(row["detected_at"], NOW)
        self.assertEqual(row["breach_minutes"], 24 * 60)
        self.assertEqual(row["status"], "OPEN")
        self.assertEqual(row["owner_actor"], "agent_ana")

    def test_a_breach_is_audited_with_its_deadline(self):
        self.case(first_due=PAST, quote_due=DAY)
        self.tick()
        event = self.conn.execute(
            "SELECT action, entity_type, entity_id, reason FROM audit_events "
            "WHERE action = 'sla.breach_recorded'"
        ).fetchone()
        self.assertEqual(event["entity_id"], "case_0001")
        self.assertIn(PAST, event["reason"])

    # -- operation tasks ---------------------------------------------------
    def test_overdue_operation_task_is_charged_and_the_assignee_becomes_owner(self):
        self.ops_task(due_at=PAST, assignee="ops_bagus")
        self.tick()
        rows = [b for b in self.breaches() if b[2] == "OPS_TASK"]
        self.assertEqual(rows, [("ops_task", "task_0001", "OPS_TASK")])
        owner = self.conn.execute(
            "SELECT owner_actor FROM sla_breaches WHERE kind = 'OPS_TASK'"
        ).fetchone()["owner_actor"]
        self.assertEqual(owner, "ops_bagus")
        self.tick()
        self.assertEqual(len(self.breaches()), 1)

    def test_finished_operation_task_is_not_charged(self):
        self.ops_task(status="DONE", due_at=PAST)
        self.ops_task("task_0002", status="CANCELLED", due_at=PAST)
        self.assertEqual(self.tick().changed_total, 0)

    # -- payments ----------------------------------------------------------
    def test_unmatched_payment_past_its_window_is_charged_once(self):
        self.reconciliation(due_at=PAST)
        self.tick()
        self.assertEqual(self.breaches(), [("payment", "recon_0001", "UNMATCHED_PAYMENT")])
        self.tick()
        self.assertEqual(len(self.breaches()), 1)

    def test_resolved_reconciliation_is_not_charged(self):
        self.reconciliation(status="RESOLVED", due_at=PAST)
        self.assertEqual(self.tick().changed_total, 0)

    # -- regulator notice --------------------------------------------------
    def test_a_missed_regulator_deadline_is_charged(self):
        self.incident(notice_required=1, notice_due_at=PAST)
        self.tick()
        self.assertEqual(self.breaches(), [("incident", "inc_0001", "REGULATOR_NOTICE")])

    def test_a_notice_that_was_sent_is_not_charged(self):
        self.incident(notice_required=1, notice_due_at=PAST, notified_at=CREATED)
        self.assertEqual(self.tick().changed_total, 0)

    def test_a_notice_deadline_still_ahead_is_not_charged(self):
        self.incident(notice_required=1, notice_due_at=DAY)
        self.assertEqual(self.tick().changed_total, 0)

    # -- collections -------------------------------------------------------
    def test_an_overdue_invoice_opens_exactly_one_chase_task(self):
        self.booking()
        self.invoice(due_at=PAST)
        report = self.tick()
        self.assertEqual(self.count("ops_tasks"), 1)
        task = self.conn.execute(
            "SELECT kind, priority, status, due_at, checklist, idempotency_key FROM ops_tasks"
        ).fetchone()
        self.assertEqual(task["kind"], SCH.CHASE_KIND)
        self.assertEqual(task["priority"], SCH.DEFAULT_CHASE_PRIORITY)
        self.assertEqual(task["status"], "OPEN")
        self.assertIn("invoice inv_0001", task["checklist"])
        self.assertEqual(task["idempotency_key"], f"chase:inv_0001:{PAST}")
        self.assertGreater(report.changed_total, 0)

    def test_the_second_tick_does_not_open_a_second_chase_task(self):
        self.booking()
        self.invoice(due_at=PAST)
        self.tick()
        self.tick()
        self.assertEqual(self.count("ops_tasks"), 1)

    def test_a_very_overdue_invoice_is_escalated_in_priority(self):
        self.booking()
        self.invoice(due_at="2026-08-01T08:00:00+00:00")
        self.tick()
        task = self.conn.execute("SELECT priority FROM ops_tasks").fetchone()
        self.assertEqual(task["priority"], SCH.CHASE_ESCALATED_PRIORITY)

    def test_a_paid_invoice_is_never_chased(self):
        self.booking()
        self.invoice(payment_status="PAID", due_at=PAST)
        self.assertEqual(self.tick().changed_total, 0)
        self.assertEqual(self.count("ops_tasks"), 0)

    def test_a_void_invoice_is_never_chased(self):
        self.booking()
        self.invoice(status="VOID", due_at=PAST)
        self.assertEqual(self.count("ops_tasks"), 0)

    def test_an_invoice_not_yet_due_is_not_chased(self):
        self.booking()
        self.invoice(due_at=DAY)
        self.assertEqual(self.tick().changed_total, 0)

    def test_collections_also_charges_the_dunning_sla(self):
        self.booking()
        self.invoice(due_at=PAST)
        self.tick()
        self.assertIn(("invoice", "inv_0001", "PAYMENT_DUNNING"), self.breaches())

    def test_collections_never_contacts_the_guest(self):
        self.booking()
        self.invoice(due_at=PAST)
        before = self.count("message_drafts")
        self.tick()
        self.assertEqual(self.count("message_drafts"), before)

    # -- delegating sweeps -------------------------------------------------
    def test_an_expired_hold_is_ended_by_the_kernel_not_by_the_scheduler(self):
        self.hold(expires_at=PAST)
        report = self.tick()
        status = self.conn.execute("SELECT status, closed_at FROM holds").fetchone()
        self.assertEqual(status["status"], "EXPIRED_PENDING_VERIFICATION")
        self.assertIsNotNone(status["closed_at"])
        self.assertEqual(self.count("sla_breaches"), 0)
        self.assertGreater(report.changed_total, 0)

    def test_an_active_hold_inside_its_ttl_is_left_alone(self):
        self.hold(expires_at=DAY)
        self.tick()
        self.assertEqual(self.conn.execute("SELECT status FROM holds").fetchone()["status"],
            "CONFIRMED",
        )

    def test_an_expired_approval_is_closed_by_the_approval_service(self):
        self.approval(expires_at=PAST)
        self.tick()
        row = self.conn.execute(
            "SELECT status, closed_at FROM approval_requests"
        ).fetchone()
        self.assertEqual(row["status"], "EXPIRED")
        self.assertIsNotNone(row["closed_at"])
        self.assertEqual(self.count("approval_decisions"), 0)

    def test_a_pending_approval_inside_its_window_is_left_alone(self):
        self.approval(expires_at=DAY)
        self.tick()
        self.assertEqual(
            self.conn.execute("SELECT status FROM approval_requests").fetchone()["status"],
            "PENDING",
        )

    def test_a_missing_kernel_is_reported_not_hidden(self):
        self.hold(expires_at=PAST)
        scheduler = SCH.Scheduler(
            self.conn, clock=self.clock, ids=self.ids, audit=self.audit,
            kernel=None, approvals=self.approvals,
        )
        report = scheduler.tick()
        outcome = {o.name: o for o in report.outcomes}["holds"]
        self.assertFalse(outcome.ran)
        self.assertIn("kernel", outcome.skipped)
        self.assertEqual(self.conn.execute("SELECT status FROM holds").fetchone()["status"],
            "CONFIRMED",
        )

    def test_a_missing_approval_service_is_reported_not_hidden(self):
        self.approval(expires_at=PAST)
        scheduler = SCH.Scheduler(
            self.conn, clock=self.clock, ids=self.ids, audit=self.audit,
            kernel=self.kernel, approvals=None,
        )
        report = scheduler.tick()
        outcome = {o.name: o for o in report.outcomes}["approvals"]
        self.assertFalse(outcome.ran)
        self.assertIn("approval", outcome.skipped)

    def test_a_dry_run_still_reports_delegated_work_it_would_do(self):
        self.hold(expires_at=PAST)
        self.approval(expires_at=PAST)
        report = self.tick(dry_run=True)
        outcomes = {o.name: o for o in report.outcomes}
        self.assertEqual(outcomes["holds"].changed, 1)
        self.assertEqual(outcomes["approvals"].changed, 1)
        self.assertEqual(self.conn.execute("SELECT status FROM holds").fetchone()["status"],
            "CONFIRMED",
        )

    # -- report shape ------------------------------------------------------
    def test_report_serialises_and_renders(self):
        self.case(first_due=PAST, quote_due=PAST)
        report = self.tick()
        payload = report.as_dict()
        self.assertEqual(
            {o["name"] for o in payload["outcomes"]}, set(SCH.DEFAULT_SWEEPS),
        )
        self.assertEqual(payload["skipped"], [])
        self.assertIn("changed_total", payload)
        text = report.render()
        self.assertIn("case_sla", text)
        self.assertIn("change(s)", text)

    def test_a_subset_of_sweeps_can_be_selected(self):
        self.case(first_due=PAST, quote_due=PAST)
        scheduler = SCH.Scheduler(
            self.conn, clock=self.clock, ids=self.ids, audit=self.audit,
            kernel=self.kernel, approvals=self.approvals, sweeps=("case_sla",),
        )
        report = scheduler.tick()
        self.assertEqual([o.name for o in report.outcomes], ["case_sla"])
        self.assertEqual(len(self.breaches()), 2)

    def test_the_whole_tick_is_idempotent_on_a_busy_system(self):
        self.case(first_due=PAST, quote_due=PAST)
        self.ops_task(due_at=PAST)
        self.reconciliation(due_at=PAST)
        self.incident(notice_required=1, notice_due_at=PAST)
        self.hold(expires_at=PAST)
        self.approval(expires_at=PAST)
        self.booking()
        self.invoice(due_at=PAST)
        first = self.tick()
        snapshot = {
            table: self.count(table)
            for table in (
                "sla_breaches", "ops_tasks", "holds", "approval_requests",
                "invoices", "message_drafts", "bookings",
            )
        }
        second = self.tick()
        self.assertGreater(first.changed_total, 0)
        self.assertEqual(second.changed_total, 0)
        self.assertEqual(
            snapshot,
            {table: self.count(table) for table in snapshot},
        )


class SchedulerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "villa.db"
        conn = connect_sqlite(str(self.db))
        conn.close()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO / "src" / "scheduler.py"), "--db", str(self.db), *args],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )

    def test_once_exits_zero_and_prints_a_report(self):
        result = self._run("--now", NOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tick", result.stdout)
        self.assertIn("holds", result.stdout)

    def test_json_report_is_machine_readable(self):
        result = self._run("--now", NOW, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("outcomes", payload)
        self.assertFalse(payload["dry_run"])

    def test_dry_run_says_so(self):
        result = self._run("--now", NOW, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run", result.stdout)

    def test_missing_database_exits_two(self):
        result = subprocess.run(
            [sys.executable, str(REPO / "src" / "scheduler.py"), "--db", "/nonexistent/db.sqlite"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("database not found", result.stderr)

    def test_unknown_sweep_exits_one_with_a_clear_code(self):
        result = self._run("--now", NOW, "--sweeps", "holds,nope")
        self.assertEqual(result.returncode, 1)
        self.assertIn("UNKNOWN_SWEEP", result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
