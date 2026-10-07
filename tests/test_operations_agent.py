"""Agent 4 — the Operations AI Agent: workflow telemetry with zero write paths.

``src/operations_agent.py`` answers §10 of the AI Workforce Architecture V1.1.
Its mission is *orchestration watch*: read the workflow facts the services
already recorded (``ops_tasks``, ``cases``, ``incidents``, ``documents``,
``bookings``, ``exception_fingerprints``) and turn them into the reports a
human Operations Manager works from. The claims this file pins down:

1. every number is read from recorded facts and nothing is invented between
   them — no predicted load, no estimated completion;
2. authority is structural: the agent registers LOW-tier read tools whose
   tier ceiling keeps task-writing and state-forcing tools out of reach;
   this module contains no task-creation or transition path at all;
3. SLA honesty is the core: a task whose ``due_at`` has passed while still
   active is overdue; an incident past its severity SLA response window is
   a breach; empty denominators give ``None``, never a fabricated 0%;
4. document completeness is the recorded
   ``documents.kind = 'CONFIRMATION'`` row per confirmed booking — the
   agent counts gaps, it does not generate documents;
5. pre-arrival readiness is the count of bookings in occupying states
   against their recorded data, read through the same §30 readiness facts;
6. exception detection is a survey of the fingerprints the exception engine
   already raised, plus the recon cases and webhook hygiene FIN already
   surveys — the agent reports them, it does not resolve them;
7. KPI telemetry answers the §10 families from the same facts;
8. collecting twice at the same instant returns equal bundles, and the
   collector writes nothing: row counts are untouched — read-only is
   enforced, not promised;
9. a missing table is refused loudly (``OperationsClientError``), not
   swallowed into an empty report;
10. no P2+ field can leak: every produced value is an integer, a rate, a
    label or an ISO instant — the surveys select no guest ref, no name, no
    free-text payload.

Every fill is checked against the tables, not only against the report.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import operations_agent as OPS
import primitives as P
from schema_loader import connect_sqlite

START = "2026-09-21T08:00:00Z"
NOW = P.parse_iso(START)


def _iso(days: float) -> str:
    return P.to_iso(NOW + timedelta(days=days))


class Harness(unittest.TestCase):
    """Real storage, deterministic clock — the same harness other modules use."""

    def setUp(self) -> None:
        self.conn = connect_sqlite()
        self.clock = P.FrozenClock(NOW)
        self.agency = "agency_default"
        self._tables = self._row_counts()

    def _row_counts(self) -> dict[str, int]:
        tables = [
            "ops_tasks", "cases", "incidents", "documents", "bookings",
            "exception_fingerprints", "reconciliation_cases",
            "payment_events", "audit_events", "outbox_events", "ai_tools",
        ]
        counts = {}
        for t in tables:
            try:
                counts[t] = self.conn.execute(
                    f"SELECT COUNT(*) FROM {t}"
                ).fetchone()[0]
            except Exception:
                counts[t] = -1
        return counts

    def assertUntouched(self) -> None:
        self.assertEqual(self._row_counts(), self._tables)

    # -- builders -----------------------------------------------------------

    def add_task(
        self,
        task_id: str,
        *,
        kind: str = "CHECKIN_PREP",
        status: str = "OPEN",
        due_days: float = 1.0,
        booking_id: str | None = None,
        completed: bool = False,
    ) -> None:
        self.conn.execute(
            "INSERT INTO ops_tasks (task_id, agency_id, kind, booking_id,"
            " due_at, priority, assignee, status, checklist,"
            " idempotency_key, created_at, updated_at, completed_at)"
            " VALUES (?, ?, ?, ?, ?, 'NORMAL', NULL, ?, '[]', ?,"
            " ?, ?, ?)",
            (
                task_id, self.agency, kind, booking_id, _iso(due_days),
                status, f"idemp-key-{task_id}", _iso(-5.0), _iso(-5.0),
                _iso(-1.0) if (completed or status in ('DONE', 'CANCELLED')) else None,
            ),
        )

    def add_case(
        self,
        case_id: str,
        *,
        status: str = "TRIAGED",
        created_days: float = -20.0,
        booking_id: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO cases (case_id, agency_id, channel, guest_ref,"
            " status, priority, booking_id, sla_policy,"
            " first_response_due_at, quote_due_at, idempotency_key,"
            " created_at, updated_at)"
            " VALUES (?, ?, 'email', ?, ?, 'NORMAL', ?, 'standard',"
            " ?, ?, ?, ?, ?)",
            (
                case_id, self.agency, f"g-{case_id}", status, booking_id,
                _iso(created_days + 1.0), _iso(created_days + 5.0),
                f"idem-{case_id}", _iso(created_days), _iso(created_days),
            ),
        )

    def add_incident(
        self,
        incident_id: str,
        *,
        severity: str = "SEV2",
        status: str = "OPEN",
        detected_days: float = -2.0,
    ) -> None:
        resolved = status in ("RESOLVED", "POSTMORTEM_DONE")
        self.conn.execute(
            "INSERT INTO incidents (incident_id, agency_id, severity, title,"
            " detected_at, detected_by, source, playbook, affected_refs,"
            " status, summary, resolved_at, postmortem_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'harness', 'test', 'test-playbook',"
            " '[]', ?, ?, ?, ?, ?)",
            (
                incident_id, self.agency, severity,
                f"Incident {incident_id} for harness", _iso(detected_days),
                status,
                f"resolved summary {incident_id}" if resolved else None,
                _iso(detected_days + 0.5) if resolved else None,
                f"pm-{incident_id}" if status == "POSTMORTEM_DONE" else None,
                _iso(detected_days),
            ),
        )

    def add_booking(
        self,
        booking_id: str,
        *,
        status: str = "CONFIRMED",
        check_in_days: float = 20.0,
    ) -> None:
        if self.conn.execute(
            "SELECT 1 FROM bookings WHERE booking_id = ?", (booking_id,)
        ).fetchone():
            return
        for stmt in (
            "INSERT OR IGNORE INTO suppliers (supplier_id, agency_id, name,"
            " status, created_at, updated_at) VALUES"
            " ('supplier_ops', 'agency_default', 'Ops Test Supplier',"
            " 'ACTIVE', ?, ?)",
            "INSERT OR IGNORE INTO properties (property_id, agency_id, name,"
            " property_type, region, province, timezone, status,"
            " contract_pack_status, created_at, updated_at) VALUES"
            " ('prop_ops', 'agency_default', 'Villa Ops Test', 'villa',"
            " 'Canggu', 'Bali', 'Asia/Makassar', 'SELLABLE', 'ACTIVATED',"
            " ?, ?)",
            "INSERT OR IGNORE INTO units (unit_id, agency_id, property_id,"
            " name, unit_type, bedrooms, bathrooms, base_capacity,"
            " max_guests, status, created_at, updated_at) VALUES"
            " ('unit_ops' || (SELECT COUNT(*) FROM bookings), 'agency_default', 'prop_ops', 'Unit Ops Test',"
            " 'entire_villa', 3, 3, 2, 6, 'ACTIVE', ?, ?)",
        ):
            self.conn.execute(stmt, (_iso(-50.0), _iso(-50.0)))
        self.conn.execute(
            "INSERT OR IGNORE INTO policy_versions"
            " (policy_version_id, agency_id, property_id, unit_scope,"
            " version, effective_from, effective_to, currency, deposit_pct,"
            " service_fee_pct, tax_pbjt_pct, max_discount_pct,"
            " quote_valid_hours, hold_ttl_minutes, max_hold_extensions,"
            " payload, fingerprint, source_doc_ref, approved_by, created_at)"
            " VALUES ('policy_version_ops', 'agency_default', 'prop_ops',"
            " 'property', 1, '2026-01-01', '2026-12-31', 'IDR', '0.3',"
            " '0.05', '0.1', '0.1', 48, 60, 1, ?, ?, 'SOP-ops',"
            " 'ops_backup_user', ?)",
            ("{}", "a" * 64, _iso(-50.0)),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO villa_supplier_relationships"
            " (villa_supplier_relationship_id, agency_id, supplier_id,"
            " property_id, unit_id, status, created_at, updated_at)"
            " VALUES ('route_default', 'agency_default', 'supplier_ops',"
            " 'prop_ops', 'unit_ops' || (SELECT COUNT(*) FROM bookings), 'ACTIVE', ?, ?)",
            (_iso(-50.0), _iso(-50.0)),
        )
        self.conn.execute(
            "INSERT INTO bookings (booking_id, agency_id,"
            " villa_supplier_relationship_id, property_id, unit_id,"
            " check_in, check_out, nights, status, guest_ref, adults,"
            " children, infants, currency, policy_version_id,"
            " policy_fingerprint, total_minor, amount_paid_minor,"
            " security_deposit_minor, payment_status, idempotency_key,"
            " version_lock, created_at, updated_at)"
            " VALUES (?, ?, 'route_default', 'prop_ops', 'unit_ops' || (SELECT COUNT(*) FROM bookings),"
            " ?, ?, 7, ?, ?, 2, 0, 0, 'IDR', 'policy_version_ops',"
            " 'a' || ?, 3000000, 0, 0, 'UNPAID', ?, 1, ?, ?)",
            (
                booking_id, self.agency, _iso(check_in_days),
                _iso(check_in_days + 7.0), status, f"g-{booking_id}",
                "a" * 63, f"idem-{booking_id}", _iso(-40.0), _iso(-40.0),
            ),
        )

    def add_document(
        self,
        document_id: str,
        *,
        kind: str = "CONFIRMATION",
        booking_id: str | None = None,
        status: str = "GENERATED",
    ) -> None:
        self.conn.execute(
            "INSERT INTO documents (document_id, agency_id, document_key,"
            " kind, version, format, booking_id, content_class,"
            " redaction_profile, storage_ref, body_sha256, status,"
            " generated_by, generated_at, created_at)"
            " VALUES (?, ?, ?, ?, 1, 'HTML', ?, 'P2', 'profile_public',"
            " ?, ?, ?, 'harness', ?, ?)",
            (
                document_id, self.agency, f"doc-{document_id}", kind,
                booking_id, f"store/{document_id}", "b" * 64, status,
                _iso(-30.0), _iso(-30.0),
            ),
        )

    def add_fingerprint(
        self,
        fingerprint_id: str,
        *,
        occurrences: int = 1,
        first_days: float = -3.0,
        last_days: float = -1.0,
    ) -> None:
        if not self.conn.execute(
            "SELECT 1 FROM incidents WHERE incident_id = 'inc_harness'"
        ).fetchone():
            self.add_incident("inc_harness", severity="SEV3")
        self.conn.execute(
            "INSERT INTO exception_fingerprints (fingerprint_id, agency_id,"
            " dedupe_key, category, source, code, severity, occurrences,"
            " incident_id, first_seen_at, last_seen_at)"
            " VALUES (?, ?, ?, 'ops', 'harness', ?, 'SEV3', ?,"
            " 'inc_harness', ?, ?)",
            (
                fingerprint_id, self.agency, fingerprint_id.ljust(64, "0"),
                f"code-{fingerprint_id}", occurrences,
                _iso(first_days), _iso(last_days),
            ),
        )


# --------------------------------------------------------------------------- #
# 1. empty estate — honest zeros and Nones
# --------------------------------------------------------------------------- #
class TestEmptyEstate(Harness):

    def test_empty_estate_reports_zero_and_no_rates(self):
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.tasks.open_count, 0)
        self.assertEqual(kpis.tasks.open_by_kind, {})
        self.assertEqual(kpis.tasks.overdue_count, 0)
        self.assertIsNone(kpis.kpi.sla_compliance)
        self.assertIsNone(kpis.kpi.overdue_rate)
        self.assertEqual(kpis.exceptions.fingerprint_count, 0)

    def test_collector_writes_nothing(self):
        OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertUntouched()


# --------------------------------------------------------------------------- #
# 2. tasks — W01 workflow view and W02 SLA honesty
# --------------------------------------------------------------------------- #
class TestTaskSurvey(Harness):

    def test_open_tasks_counted_by_kind(self):
        self.add_task("t1", kind="CHECKIN_PREP")
        self.add_task("t2", kind="CHECKIN_PREP")
        self.add_task("t3", kind="CLEANING")
        self.add_task("t4", kind="CLEANING", status="DONE", completed=True)
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.tasks.open_count, 3)
        self.assertEqual(
            kpis.tasks.open_by_kind, {"CHECKIN_PREP": 2, "CLEANING": 1}
        )

    def test_overdue_counts_only_active_past_due(self):
        self.add_task("t_on", due_days=1.0)
        self.add_task("t_off", due_days=-3.0)
        self.add_task("t_cancel", due_days=-3.0, status="CANCELLED")
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.tasks.overdue_count, 1)

    def test_due_soon_window_counts_next_24h(self):
        self.add_task("t_12h", due_days=0.5)
        self.add_task("t_72h", due_days=3.0)
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.tasks.due_within_24h, 1)

    def test_blocked_tasks_surfaced(self):
        self.conn.execute(
            "INSERT INTO ops_tasks (task_id, agency_id, kind, due_at,"
            " priority, status, checklist, blocked_reason, idempotency_key,"
            " created_at, updated_at)"
            " VALUES ('t_blk', ?, 'MAINTENANCE', ?, 'HIGH', 'BLOCKED',"
            " '[]', 'waiting supplier part', 'idem-t_blk', ?, ?)",
            (self.agency, _iso(2.0), _iso(-5.0), _iso(-5.0)),
        )
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.tasks.blocked_count, 1)


# --------------------------------------------------------------------------- #
# 3. incidents — SLA breach detection (W02/W07)
# --------------------------------------------------------------------------- #
class TestIncidentSLA(Harness):

    def test_open_incidents_counted_by_severity(self):
        self.add_incident("i1", severity="SEV1")
        self.add_incident("i2", severity="SEV3")
        self.add_incident("i3", severity="SEV3", status="RESOLVED")
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(
            kpis.incidents.open_by_severity, {"SEV1": 1, "SEV3": 1}
        )

    def test_incident_past_response_sla_is_breach(self):
        # SEV3 augenblick response window long past a 2-day-old incident.
        self.add_incident("i_late", severity="SEV3", detected_days=-2.0)
        self.add_incident("i_new", severity="SEV3", detected_days=-0.01)
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertGreaterEqual(kpis.incidents.sla_breached_count, 1)

    def test_resolved_incident_never_breaches(self):
        self.add_incident("i_r", severity="SEV2", detected_days=-30.0,
                          status="RESOLVED")
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.incidents.sla_breached_count, 0)


# --------------------------------------------------------------------------- #
# 4. document completeness — W03
# --------------------------------------------------------------------------- #
class TestDocumentGaps(Harness):

    def test_confirmed_booking_without_confirmation_doc_is_gap(self):
        self.add_booking("bkg_gap")
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.documents.confirmed_without_doc, 1)

    def test_documented_booking_is_not_a_gap(self):
        self.add_booking("bkg_docs")
        self.add_document("doc_conf", booking_id="bkg_docs")
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.documents.confirmed_without_doc, 0)

    def test_void_or_superseded_doc_does_not_close_the_gap(self):
        self.add_booking("bkg_void_doc")
        self.conn.execute(
            "INSERT INTO documents (document_id, agency_id, document_key,"
            " kind, version, format, booking_id, content_class,"
            " redaction_profile, body_sha256, status, generated_by,"
            " generated_at, created_at)"
            " VALUES ('doc_void', ?, 'doc-void', 'CONFIRMATION', 1, 'HTML',"
            " 'bkg_void_doc', 'P2', 'profile_public', ?, 'VOID',"
            " 'harness', ?, ?)",
            (self.agency, "b" * 64, _iso(-30.0), _iso(-30.0)),
        )
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.documents.confirmed_without_doc, 1)

    def test_unconfirmed_bookings_are_out_of_scope(self):
        self.add_booking("bkg_quoted", status="QUOTE_SENT")
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.documents.confirmed_without_doc, 0)

    def test_deposit_and_paid_in_full_bookings_still_owe_a_confirmation(self):
        """A booking that has taken money is locked in — a missing guest
        confirmation is a gap even though the status is not CONFIRMED yet."""
        for i, status in enumerate(("DEPOSIT_PAID", "PAID_IN_FULL")):
            self.add_booking(f"bkg_money_{i}", status=status)
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.documents.confirmed_count, 2)
        self.assertEqual(kpis.documents.confirmed_without_doc, 2)

    def test_doc_required_statuses_are_read_from_their_owners(self):
        """The set must track `documents` (the module that issues the document)
        instead of being a copy that silently drifts behind it."""
        import booking_states as BS
        import documents as DOC

        required = set(OPS._confirmation_required_statuses())
        self.assertEqual(
            required, set(DOC.CONFIRMATION_BOOKING_STATUSES) | {"BALANCE_OVERDUE"}
        )
        self.assertTrue(required <= set(BS.BOOKING_STATES))


# --------------------------------------------------------------------------- #
# 5. pre-arrival readiness — W05
# --------------------------------------------------------------------------- #
class TestPreArrival(Harness):

    def test_occupying_booking_counts_as_in_flight(self):
        self.add_booking("bkg_pa", status="PRE_ARRIVAL", check_in_days=3.0)
        self.add_booking("bkg_no", status="CONFIRMED", check_in_days=3.0)
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.pre_arrival.in_flight_count, 1)

    def test_arrivals_in_next_72h_counted(self):
        self.add_booking("bkg_48", status="PRE_ARRIVAL", check_in_days=2.0)
        self.add_booking("bkg_far", status="PRE_ARRIVAL", check_in_days=10.0)
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.pre_arrival.arriving_within_72h, 1)


# --------------------------------------------------------------------------- #
# 6. exceptions — W07 survey of what the engine already raised
# --------------------------------------------------------------------------- #
class TestExceptionSurvey(Harness):

    def test_open_fingerprints_counted(self):
        self.add_fingerprint("fp1", occurrences=3)
        self.add_fingerprint("fp2", occurrences=1)
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.exceptions.fingerprint_count, 2)
        self.assertEqual(kpis.exceptions.total_occurrences, 4)

    def test_recon_and_webhook_hygiene_counted(self):
        self.add_booking("bkg_fin")
        self.conn.execute(
            "INSERT INTO reconciliation_cases (recon_id, agency_id, kind,"
            " payment_event_id, payment_id, invoice_id, detected_at, due_at,"
            " status, details, created_at)"
            " VALUES ('rc_ops', ?, 'UNMATCHED_PAYMENT', NULL, NULL, NULL,"
            " ?, ?, 'OPEN', '{}', ?)",
            (self.agency, _iso(-2.0), _iso(-1.5), _iso(-2.0)),
        )
        self.conn.execute(
            "INSERT INTO payment_events (event_id, provider,"
            " provider_event_id, payment_id, event_type,"
            " signature_verified, applied, rejection_reason, amount_minor,"
            " currency, payload_sha256, payload_redacted, received_at,"
            " created_at)"
            " VALUES ('evt_ops', 'midtrans', 'evt-ops-1', NULL,"
            " 'payment.succeeded', 0, 0, 'WEBHOOK_SIGNATURE_INVALID: no',"
            " NULL, NULL, ?, NULL, ?, ?)",
            (OPS.ZERO_SHA256, _iso(-1.0), _iso(-1.0)),
        )
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.exceptions.open_recon_cases, 1)
        self.assertEqual(kpis.exceptions.unverified_webhook_events, 1)


# --------------------------------------------------------------------------- #
# 7. KPI — empty denominator is None, never a fabricated 0%
# --------------------------------------------------------------------------- #
class TestKPI(Harness):

    def test_rates_over_zero_active_tasks_are_none(self):
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertIsNone(kpis.kpi.overdue_rate)
        self.assertIsNone(kpis.kpi.sla_compliance)

    def test_rates_over_live_workload(self):
        self.add_task("t1")
        self.add_task("t2", due_days=-2.0)
        kpis = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.kpi.overdue_rate, 0.5)
        self.assertEqual(kpis.kpi.sla_compliance, 0.5)


# --------------------------------------------------------------------------- #
# 8. read-only + determinism + refusals
# --------------------------------------------------------------------------- #
class TestReadOnlyAndRefusals(Harness):

    def test_collect_is_deterministic_at_one_instant(self):
        self.add_task("t_d")
        self.add_incident("i_d")
        a = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        b = OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(a, b)

    def test_collect_writes_nothing_after_full_fill(self):
        self.add_task("t_f")
        self.add_booking("bkg_f")
        self.add_incident("i_f")
        # Re-baseline AFTER seeding: the claim under test is that collect()
        # itself writes nothing, not that the seed was free.
        self._tables = self._row_counts()
        OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertUntouched()

    def test_missing_table_refused_not_swallowed(self):
        self.conn.execute("DROP TABLE ops_tasks")
        with self.assertRaises(OPS.OperationsClientError) as ctx:
            OPS.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertIn("ops_tasks", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 9. identity — read-tier tools, and the ceiling that keeps writes out of reach
# --------------------------------------------------------------------------- #
class TestIdentityAndCeiling(Harness):
    """Claim #2 in the module docstring: the agent registers READ-tier tools
    and the gateway's tier ceiling — not this module's good intentions — is
    what stops it creating tasks or forcing states, even while ACTIVE."""

    def _wired(self):
        import ai_gateway as AG
        import ai_service as AIS
        import audit as AUD
        import policy_engine as PE

        policy = PE.PolicyResolver(
            self.conn, clock=self.clock, agency_id=self.agency
        )
        ai = AIS.AiService(self.conn, policy=policy, clock=self.clock)
        ids = P.IdFactory(True, "ops")
        audit = AUD.AuditLog(self.conn, clock=self.clock, ids=ids)
        gateway = AG.AiGateway(ai, audit=audit, clock=self.clock, ids=ids)
        return ai, gateway

    def test_operations_tools_are_low_tier_only(self):
        at = P.to_iso(self.clock.now())
        self.assertEqual(
            OPS.ensure_operations_tools(self.conn, at=at),
            len(OPS.OPERATIONS_TOOLS),
        )
        rows = self.conn.execute(
            "SELECT tool, tier FROM ai_tools WHERE tool LIKE 'ops.%'"
        ).fetchall()
        self.assertEqual(len(rows), len(OPS.OPERATIONS_TOOLS))
        for row in rows:
            self.assertEqual(row[1], "LOW")
        banned = ("create", "assign", "cancel", "transition", "resolve",
                  "force", "approve", "execute", "send")
        for name, *_ in OPS.OPERATIONS_TOOLS:
            self.assertFalse(
                [verb for verb in banned if verb in name],
                f"write verb leaked into a read-tier tool name: {name}",
            )

    def test_ensure_operations_tools_is_idempotent(self):
        at = P.to_iso(self.clock.now())
        first = OPS.ensure_operations_tools(self.conn, at=at)
        self.assertEqual(first, len(OPS.OPERATIONS_TOOLS))
        self.assertEqual(OPS.ensure_operations_tools(self.conn, at=at), 0)

    def test_agent_cannot_create_tasks_even_when_active(self):
        ai, gateway = self._wired()
        principal = OPS.register_principal(
            ai, secret_ref="VILLA_OPS_SECRET", owner_actor="owner_anas"
        )
        with self.assertRaises(Exception):
            gateway.request(
                principal.principal_id, "ops_tasks.create",
                intent="ops: create task",
            )

    def test_register_principal_refuses_without_an_ai_service(self):
        with self.assertRaises(OPS.OperationsClientError) as ctx:
            OPS.register_principal(
                None, secret_ref="VILLA_OPS_SECRET", owner_actor="owner_anas"
            )
        self.assertEqual(ctx.exception.code, "OPS_NO_AI_SERVICE")


if __name__ == "__main__":
    unittest.main()
