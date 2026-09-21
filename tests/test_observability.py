"""Observability tests: the numbers and the health verdict must be honest.

Two failure modes are tested explicitly, because they are the ones that hurt:
a counter that silently reads the wrong state (dashboards then lie), and a health
check that reports OK when it could not actually evaluate the condition.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import observability as O  # noqa: E402
from anchors import publish_anchor  # noqa: E402
from audit import AuditLog  # noqa: E402
from primitives import FrozenClock, IdFactory  # noqa: E402
from schema_loader import SCHEMA_VERSION, connect_sqlite  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
NOW = FrozenClock.at("2026-09-21T08:00:00Z")
CREATED = "2026-09-18T08:00:00+00:00"   # rows are created before they fall due
DAY = "2026-09-22T08:00:00+00:00"       # tomorrow: still inside every SLA
PAST = "2026-09-20T08:00:00+00:00"      # yesterday: already past every SLA


def _stay(offset: int) -> tuple[str, str]:
    """A distinct four-night stay per seeded row: the schema forbids reused slots."""

    from datetime import date, timedelta

    start = date(2026, 10, 1) + timedelta(days=offset)
    return start.isoformat(), (start + timedelta(days=4)).isoformat()


class ObsCase(unittest.TestCase):
    """A database with exactly the rows each test needs, inserted directly."""

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.clock = FrozenClock.at("2026-09-21T08:00:00Z")
        self.ids = IdFactory(True, "obs")
        self._seed_inventory()

    def _seed_inventory(self) -> None:
        """The minimum real inventory a booking is allowed to point at."""

        self._insert(
            "properties", property_id="prop_0001", name="Villa Uji", property_type="villa",
            region="bali", province="Bali", timezone="Asia/Makassar", status="SELLABLE",
            contract_pack_status="ACTIVATED", contract_pack_evaluated_at=PAST,
            created_at=PAST, updated_at=PAST,
        )
        self._insert(
            "units", unit_id="unit_0001", property_id="prop_0001", name="Unit Uji",
            unit_type="entire_villa", bedrooms=2, bathrooms=2, base_capacity=2, max_guests=4,
            status="ACTIVE", created_at=PAST, updated_at=PAST,
        )
        self._insert(
            "availability_evidence", evidence_id="ev_0001", unit_id="unit_0001",
            evidence_level="A", source_type="manager_written", source_ref="owner_anas",
            captured_at=CREATED, valid_until=DAY, result="AVAILABLE",
            can_quote=1, can_hold=1, can_confirm=1, created_at=CREATED,
        )
        self._insert(
            "policy_versions", policy_version_id="policy_v1", property_id="prop_0001",
            unit_scope="property", version=1, effective_from="2026-01-01",
            # Terbuka (tanpa batas): kolomnya NOT NULL, jadi pakai sentinel jauh.
            effective_to="9999-12-31", currency="IDR", deposit_pct="30", service_fee_pct="0",
            tax_pbjt_pct="10", max_discount_pct="10", quote_valid_hours=24,
            hold_ttl_minutes=120, max_hold_extensions=1, payload="{}",
            fingerprint="f" * 64, source_doc_ref="seed", approved_by="owner_anas",
            created_at=PAST,
        )

    # -- seeding helpers ---------------------------------------------------
    def _insert(self, table: str, **cols):
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        self.conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(cols.values()))

    def seed_case(self, case_id="case_0001", status="NEW", first_due=PAST, quote_due=DAY):
        closed = status.startswith("CLOSED_")
        self._insert(
            "cases", case_id=case_id, channel="website", guest_ref="guest_0001",
            status=status, priority="NORMAL", sla_policy="web_v1",
            first_response_due_at=first_due, quote_due_at=quote_due,
            closed_at=PAST if closed else None,
            close_reason="tamu memilih tanggal lain" if closed else None,
            idempotency_key=f"idem_{case_id}", created_at=CREATED, updated_at=CREATED,
        )

    def seed_hold(self, hold_id="hold_0001", expires_at=DAY, status="ACTIVE"):
        check_in, check_out = _stay(int(hold_id[-1]))
        self._insert(
            "holds", hold_id=hold_id, unit_id="unit_0001", property_id="prop_0001",
            check_in=check_in, check_out=check_out, nights=4, status=status,
            guest_ref="guest_0001", policy_version_id="policy_v1",
            evidence_id="ev_0001", idempotency_key=f"idem_{hold_id}",
            created_at=CREATED, expires_at=expires_at,
        )

    def seed_booking(self, booking_id="book_0001", status="CONFIRMED"):
        check_in, check_out = _stay(int(booking_id[-1]) + 5)
        self._insert(
            "bookings", booking_id=booking_id, property_id="prop_0001",
            unit_id="unit_0001", check_in=check_in, check_out=check_out, nights=4,
            status=status, guest_ref="guest_0001", adults=2, children=0, infants=0,
            currency="IDR", policy_version_id="policy_v1", policy_fingerprint="f" * 64,
            total_minor=1_000_000, payment_status="UNPAID",
            idempotency_key=f"idem_{booking_id}", created_at=CREATED, updated_at=CREATED,
        )

    def seed_invoice(self, invoice_id="inv_0001", payment_status="UNPAID", due_at=PAST,
                     status="ISSUED"):
        voided = status == "VOID"
        self._insert(
            "invoices", invoice_id=invoice_id, invoice_number=f"INV-202609-{invoice_id[-4:]}",
            booking_id="book_0001",
            policy_fingerprint="f" * 64, currency="IDR", subtotal_minor=900_000,
            tax_minor=100_000, service_minor=0, total_minor=1_000_000,
            amount_paid_minor=0, status=status, payment_status=payment_status,
            issued_at=CREATED, due_at=due_at, created_at=CREATED,
            voided_at=PAST if voided else None,
            void_reason="salah tarif, diterbitkan ulang" if voided else None,
        )

    def seed_unmatched_payment_event(self, event_id="pe_0001", applied=0):
        """``applied = 0`` means "recorded but not attributed" — the schema requires
        a written reason for that, so the seeder must supply one too."""

        self._insert(
            "payment_events", event_id=event_id, provider="fake", provider_event_id=f"evt_{event_id}",
            event_type="payment.succeeded", signature_verified=1, applied=applied,
            rejection_reason=None if applied else "no invoice matched yet",
            payload_sha256="a" * 64, received_at=CREATED, created_at=CREATED,
        )

    def seed_reconciliation(self, recon_id="recon_0001", due_at=PAST, status="OPEN"):
        self._insert(
            "reconciliation_cases", recon_id=recon_id, kind="AMOUNT_MISMATCH",
            detected_at=CREATED, due_at=due_at, status=status, created_at=CREATED,
        )

    def seed_approval(self, approval_id="appr_0001", status="PENDING", expires_at=PAST,
                      subject_type="discount"):
        self._insert(
            "approval_requests", approval_id=approval_id, subject_type=subject_type,
            subject_id="case_0001", action="discount.override", risk_level="MEDIUM",
            requested_by="agent_ana", requested_role="reservation", requested_at=CREATED,
            reason="long stay discount request", payload="{}", payload_sha256="b" * 64,
            required_role="owner", required_approvals=1, status=status,
            expires_at=expires_at, idempotency_key=f"idem_{approval_id}", created_at=CREATED,
        )

    def seed_ops_task(self, task_id="task_0001", status="OPEN", due_at=PAST):
        self._insert(
            "ops_tasks", task_id=task_id, kind="CLEANING", due_at=due_at, priority="NORMAL",
            status=status,
            completed_at=PAST if status in ("DONE", "CANCELLED") else None,
            blocked_reason="vendor belum konfirmasi" if status == "BLOCKED" else None,
            idempotency_key=f"idem_{task_id}", created_at=CREATED, updated_at=CREATED,
        )

    def seed_incident(self, incident_id="inc_0001", severity="SEV1", status="OPEN",
                      notice_required=0, notified_at=None, notice_due_at=None,
                      resolved_at=None, summary=None):
        if notice_required and notice_due_at is None:
            # 72 jam sejak deteksi: aturan UU PDP, dijaga di sisi pemanggil.
            notice_due_at = "2026-09-21T08:00:00+00:00"
        self._insert(
            "incidents", incident_id=incident_id, severity=severity, title="Test incident",
            detected_at=CREATED, detected_by="ops.alert", source="monitor",
            playbook="DOUBLE_BOOKING", status=status,
            regulatory_notification_required=notice_required, notified_at=notified_at,
            notice_due_at=notice_due_at,
            resolved_at=resolved_at,
            summary=summary or ("selesai, terdokumentasi" if status in ("RESOLVED", "POSTMORTEM_DONE") else None),
            created_at=CREATED,
        )

    # -- metrics -----------------------------------------------------------
    def test_empty_database_reports_zeroes(self):
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(set(metrics.counters.values()), {0})
        self.assertEqual(metrics.schema_version, SCHEMA_VERSION)
        self.assertEqual(metrics.generated_at, "2026-09-21T08:00:00+00:00")

    def test_open_case_counter_tracks_status_not_row_count(self):
        self.seed_case("case_0001", status="NEW")
        self.seed_case("case_0002", status="CLOSED_WON")
        self.seed_case("case_0003", status="ESCALATED")
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["cases_total"], 3)
        self.assertEqual(metrics.counters["cases_open"], 2)
        self.assertEqual(metrics.labels["cases_by_status"], {"NEW": 1, "CLOSED_WON": 1, "ESCALATED": 1})

    def test_case_sla_counters_split_first_response_from_quote(self):
        self.seed_case("case_0001", first_due=PAST, quote_due=PAST)
        self.seed_case("case_0002", first_due=DAY, quote_due=DAY)
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["cases_first_response_overdue"], 1)
        self.assertEqual(metrics.counters["cases_quote_overdue"], 1)

    def test_answered_case_is_not_counted_as_overdue(self):
        self.seed_case("case_0001", first_due=PAST, quote_due=PAST)
        self.conn.execute(
            "UPDATE cases SET first_responded_at = ?, quoted_at = ? WHERE case_id = ?",
            ("2026-09-20T09:00:00+00:00", "2026-09-20T09:30:00+00:00", "case_0001"),
        )
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["cases_first_response_overdue"], 0)
        self.assertEqual(metrics.counters["cases_quote_overdue"], 0)

    def test_closed_case_is_never_reported_as_an_sla_breach(self):
        self.seed_case("case_0001", status="CLOSED_LOST", first_due=PAST, quote_due=PAST)
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["cases_first_response_overdue"], 0)
        self.assertEqual(metrics.counters["cases_quote_overdue"], 0)

    def test_occupied_booking_counter_uses_the_occupancy_predicate(self):
        self.seed_booking("book_0001", status="CONFIRMED")
        self.seed_booking("book_0002", status="DISPUTED")
        self.seed_booking("book_0003", status="CANCELLED")
        self.seed_booking("book_0004", status="CLOSED")
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["bookings_total"], 4)
        self.assertEqual(metrics.counters["bookings_occupying"], 2)

    def test_hold_expiry_gauges_use_the_clock_not_the_wall_clock(self):
        self.seed_hold("hold_0001", expires_at=PAST)          # already expired
        self.seed_hold("hold_0002", expires_at=DAY)           # expires within 24h
        self.seed_hold("hold_0003", expires_at="2026-10-30T00:00:00+00:00")
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["holds_active"], 3)
        self.assertEqual(metrics.counters["holds_expiring_soon"], 2)
        self.assertEqual(metrics.counters["holds_expired_but_open"], 1)

    def test_invoice_and_payment_counters(self):
        self.seed_booking()
        self.seed_invoice("inv_0001", payment_status="UNPAID", due_at=PAST)
        self.seed_invoice("inv_0002", payment_status="PAID", due_at=PAST)
        self.seed_unmatched_payment_event("pe_0001", applied=0)
        self.seed_unmatched_payment_event("pe_0002", applied=1)
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["invoices_unpaid"], 1)
        self.assertEqual(metrics.counters["invoices_overdue"], 1)
        self.assertEqual(metrics.counters["payments_unmatched"], 1)

    def test_void_invoice_is_not_reported_as_unpaid(self):
        self.seed_booking()
        self.seed_invoice("inv_0001", payment_status="UNPAID", due_at=PAST, status="VOID")
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["invoices_unpaid"], 0)
        self.assertEqual(metrics.counters["invoices_overdue"], 0)

    def test_ops_and_incident_counters(self):
        self.seed_ops_task("task_0001", status="OPEN", due_at=PAST)
        self.seed_ops_task("task_0002", status="DONE", due_at=PAST)
        self.seed_incident("inc_0001", severity="SEV1", status="OPEN")
        self.seed_incident("inc_0002", severity="SEV3", status="RESOLVED",
                           resolved_at=PAST, summary="selesai dan terdokumentasi")
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["ops_tasks_open"], 1)
        self.assertEqual(metrics.counters["ops_tasks_overdue"], 1)
        self.assertEqual(metrics.counters["incidents_open"], 1)
        self.assertEqual(metrics.counters["incidents_sev1_open"], 1)

    def test_pending_regulator_notice_is_counted_until_notified(self):
        self.seed_incident("inc_0001", severity="SEV1", notice_required=1,
                           notice_due_at=DAY)
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["incidents_pending_regulator_notice"], 1)
        self.conn.execute(
            "UPDATE incidents SET notified_at = ? WHERE incident_id = ?",
            ("2026-09-21T09:00:00+00:00", "inc_0001"),
        )
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        self.assertEqual(metrics.counters["incidents_pending_regulator_notice"], 0)

    def test_oldest_open_age_gauges(self):
        self.seed_reconciliation("recon_0001", due_at=DAY)
        self.seed_approval("appr_0001")
        metrics = O.collect_metrics(self.conn, clock=self.clock)
        # CREATED is 2026-09-18T08:00Z, the clock is 2026-09-21T08:00Z -> 72 hours.
        self.assertEqual(metrics.gauges["oldest_open_approval_age_hours"], 72.0)
        self.assertEqual(metrics.gauges["oldest_open_reconciliation_age_hours"], 72.0)
        self.assertEqual(metrics.gauges["oldest_overdue_ops_task_age_hours"], 0.0)

    def test_metrics_are_deterministic_for_a_frozen_clock(self):
        self.seed_case()
        first = json.dumps(O.collect_metrics(self.conn, clock=self.clock).as_dict(), sort_keys=True)
        second = json.dumps(O.collect_metrics(self.conn, clock=self.clock).as_dict(), sort_keys=True)
        self.assertEqual(first, second)

    def test_metrics_carry_no_personal_data(self):
        self.seed_case()
        blob = json.dumps(O.collect_metrics(self.conn, clock=self.clock).as_dict())
        self.assertNotIn("@", blob)
        self.assertNotIn("guest", blob.lower())
        self.assertNotIn("+62", blob)

    def test_collecting_metrics_writes_nothing(self):
        self.seed_case()
        self.seed_booking()
        before = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("cases", "bookings", "audit_events", "sla_breaches")
        }
        O.collect_metrics(self.conn, clock=self.clock)
        O.health_checks(self.conn, clock=self.clock)
        after = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("cases", "bookings", "audit_events", "sla_breaches")
        }
        self.assertEqual(before, after)

    # -- rendering ---------------------------------------------------------
    def test_prometheus_rendering_is_parseable_and_sorted(self):
        self.seed_case()
        text = O.render_prometheus(O.collect_metrics(self.conn, clock=self.clock))
        lines = [line for line in text.splitlines() if line]
        pattern = re.compile(r"^villa_[a-z0-9_]+(\{[a-z_]+=\"[A-Za-z0-9_]+\"\})? -?\d+(\.\d+)?$")
        for line in lines:
            self.assertRegex(line, pattern)
        self.assertEqual(lines, sorted(lines))
        self.assertIn("villa_cases_total 1", lines)

    def test_health_text_and_json_renderings(self):
        checks = O.health_checks(self.conn, clock=self.clock)
        text = O.render_health(checks)
        self.assertIn("OK", text)
        payload = json.loads(O.render_health(checks, style="json"))
        self.assertEqual({item["name"] for item in payload}, {c.name for c in checks})
        with self.assertRaises(O.ObservabilityError):
            O.render_health(checks, style="yaml")

    def test_worst_severity_picks_the_most_severe(self):
        ok = O.CheckResult("a", O.OK, "")
        unknown = O.CheckResult("b", O.UNKNOWN, "")
        warn = O.CheckResult("c", O.WARNING, "")
        crit = O.CheckResult("d", O.CRITICAL, "")
        self.assertEqual(O.worst_severity([ok, unknown, warn, crit]), O.CRITICAL)
        self.assertEqual(O.worst_severity([ok, unknown]), O.UNKNOWN)
        self.assertEqual(O.worst_severity([ok]), O.OK)

    # -- health verdicts ---------------------------------------------------
    def test_healthy_snapshot_without_any_anchor_directory_is_unknown_not_ok(self):
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["audit_chain"].severity, O.OK)
        self.assertEqual(by_name["audit_anchor"].severity, O.UNKNOWN)
        self.assertEqual(O.worst_severity(checks), O.UNKNOWN)

    def test_anchor_check_is_ok_once_an_anchor_exists(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publish_anchor(
                conn=self.conn, audit=AuditLog(self.conn, clock=self.clock, ids=self.ids),
                clock=self.clock, directory=directory, actor="ops.anchor",
                actor_role="operations",
            )
            checks = O.health_checks(self.conn, clock=self.clock, anchor_dir=directory)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["audit_anchor"].severity, O.OK)
        self.assertEqual(O.worst_severity(checks), O.OK)

    def test_broken_chain_is_critical(self):
        audit = AuditLog(self.conn, clock=self.clock, ids=self.ids)
        for index in range(3):
            audit.append(
                action="case.opened", entity_type="case", entity_id=f"case_{index:04d}",
                actor="agent_ana", actor_role="reservation", after={"i": index},
            )
        self.conn.execute("DROP TRIGGER trg_audit_events_no_delete")
        self.conn.execute("DELETE FROM audit_events WHERE seq = 2")
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["audit_chain"].severity, O.CRITICAL)
        self.assertEqual(O.worst_severity(checks), O.CRITICAL)
        self.assertEqual(checks[0].severity, O.CRITICAL)

    def test_sev1_incident_makes_the_snapshot_critical(self):
        self.seed_incident("inc_0001", severity="SEV1")
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["sev1_incidents"].severity, O.CRITICAL)

    def test_pending_regulator_notice_is_reported(self):
        self.seed_incident("inc_0001", severity="SEV2", notice_required=1)
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["regulator_notice"].severity, O.CRITICAL)

    def test_unmatched_payment_past_sla_is_a_warning(self):
        self.seed_unmatched_payment_event()
        self.seed_reconciliation(due_at=PAST)
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["unmatched_payments_sla"].severity, O.WARNING)
        self.assertEqual(O.worst_severity(checks), O.WARNING)

    def test_expired_approval_still_pending_is_a_warning(self):
        self.seed_approval("appr_0001", status="PENDING", expires_at=PAST)
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["approval_expiry"].severity, O.WARNING)

    def test_stale_hold_is_a_warning(self):
        self.seed_hold("hold_0001", expires_at=PAST)
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["stale_holds"].severity, O.WARNING)

    def test_case_and_ops_sla_breaches_are_warnings(self):
        self.seed_case(first_due=PAST)
        self.seed_ops_task(due_at=PAST)
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["case_sla"].severity, O.WARNING)
        self.assertEqual(by_name["ops_sla"].severity, O.WARNING)

    def test_drafts_waiting_for_a_human_are_reported_as_control_working(self):
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["outbound_control"].severity, O.OK)

    def test_schema_version_mismatch_is_critical(self):
        self.conn.execute(
            "UPDATE schema_meta SET schema_version = '000_something_else'",
        )
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["schema_version"].severity, O.CRITICAL)

    def test_foreign_key_check_reports_clean_tree(self):
        checks = O.health_checks(self.conn, clock=self.clock)
        by_name = {check.name: check for check in checks}
        self.assertIn(by_name["foreign_keys"].severity, (O.OK, O.UNKNOWN))


class ObsCliTests(unittest.TestCase):
    """The CLI is the supervisor-facing surface: exit codes must mean something."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "villa.db"
        conn = connect_sqlite(str(self.db))
        try:
            pass
        finally:
            conn.close()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO / "src" / "observability.py"), "--db", str(self.db), *args],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )

    def test_healthy_snapshot_exits_zero(self):
        import tempfile

        with tempfile.TemporaryDirectory() as anchors:
            conn = connect_sqlite(str(self.db))
            try:
                publish_anchor(
                    conn=conn, audit=AuditLog(conn, clock=NOW, ids=IdFactory(True, "cli")),
                    clock=NOW, directory=anchors, actor="ops.anchor", actor_role="operations",
                )
            finally:
                conn.close()
            result = self._run("--anchor-dir", anchors)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WORST SEVERITY", result.stdout)

    def test_prometheus_format_is_machine_readable(self):
        result = self._run("--format", "prometheus")
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in [line for line in result.stdout.splitlines() if line]:
            self.assertRegex(line, r"^villa_[a-z0-9_]+(\{[a-z_]+=\"[A-Za-z0-9_]+\"\})? -?\d+(\.\d+)?$")

    def test_json_format_contains_metrics_and_checks(self):
        result = self._run("--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["metrics"]["schema"], O.METRICS_SCHEMA)
        self.assertTrue(payload["checks"])
        self.assertIn(payload["worst"], {O.OK, O.UNKNOWN})

    def test_missing_database_exits_two(self):
        result = subprocess.run(
            [sys.executable, str(REPO / "src" / "observability.py"), "--db", "/nonexistent/villa.db"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("database not found", result.stderr)

    def test_frozen_now_flag_makes_the_snapshot_deterministic(self):
        first = self._run("--format", "json", "--now", "2026-09-21T08:00:00Z")
        second = self._run("--format", "json", "--now", "2026-09-21T08:00:00Z")
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(
            json.loads(first.stdout)["metrics"]["generated_at"],
            "2026-09-21T08:00:00+00:00",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
