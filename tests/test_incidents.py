"""Tests for :mod:`incidents` — deterministic incident playbooks.

The policy under test lives in ``docs/RETENTION_AND_INCIDENT_RESPONSE.md`` §2–§5
(six response playbooks, the severity/SLA table, and the "data minimal satu
insiden" contract).  Nothing here invents policy: every assertion traces back to
either that document, the schema CHECKs in ``src/schema/001_initial.sql``, or the
task contract (severity SEV1–SEV4, append-only step history, 72-hour regulatory
notice window under UU 27/2022).

Two invariants get special attention because they are the whole point of the
module:

* **no silent state** — a refused operation must leave the database byte-for-byte
  unchanged (no incident row, no step row, no audit row);
* **no hole in history** — ``incident_steps.seq`` is contiguous, and the
  append-only triggers reject any direct ``UPDATE``/``DELETE``.

Only stdlib is used (``datetime``, ``sqlite3``, ``unittest``).
"""

import datetime as dt
import pathlib
import sqlite3
import unittest

import incidents as INC
import primitives as P
from schema_loader import connect_sqlite

START = "2026-09-21T08:00:00Z"
HOUR = dt.timedelta(hours=1)
DAY = dt.timedelta(days=1)

REQUIRED_PLAYBOOKS = (
    "DOUBLE_BOOKING",
    "PAYMENT_MISMATCH",
    "DATA_BREACH",
    "VENUE_OUTAGE",
    "SAFETY_EMERGENCY",
    "PROVIDER_FAILURE",
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class IncidentTestCase(unittest.TestCase):
    """Shared fixture: in-memory SQLite, frozen clock, deterministic id factory."""

    def setUp(self):
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.start = P.parse_iso(START)
        self.clock = P.FrozenClock(self.start)
        self.ids = P.IdFactory(deterministic=True, seed="incidents")
        self.svc = INC.IncidentService(self.conn, clock=self.clock, ids=self.ids)

    # ---------------------------------------------------------------- helpers
    def t(self, minutes=0, seconds=0):
        """A wall-clock instant ``minutes``/``seconds`` after detection."""
        return self.start + dt.timedelta(minutes=minutes, seconds=seconds)

    def open_incident(self, playbook="DOUBLE_BOOKING", title="Overbooking unit U1",
                      severity="SEV2", detected_by="ops_1", source="ops_report",
                      affected_refs=("unit_1",), personal_data_involved=False,
                      summary="dua tamu satu unit", at=None, **extra):
        return self.svc.open_incident(
            playbook=playbook,
            title=title,
            severity=severity,
            detected_by=detected_by,
            source=source,
            affected_refs=affected_refs,
            personal_data_involved=personal_data_involved,
            summary=summary,
            at=self.start if at is None else at,
            **extra,
        )

    def plan(self, playbook):
        return INC.PLAYBOOKS[playbook]

    def record(self, incident_id, playbook, code, at=None, outcome="selesai sesuai playbook"):
        """Record one playbook step by its code, using the step's own owner role."""
        step = next(item for item in self.plan(playbook).steps if item.code == code)
        return self.svc.record_step(
            incident_id=incident_id,
            action=f"{step.code} {step.action}",
            actor="op_1",
            actor_role=step.owner_role,
            outcome=outcome,
            evidence_refs=("ev_1",),
            at=self.t(5, seconds=code.count("-")) if at is None else at,
        )

    def record_all_mandatory(self, incident_id, playbook, start_minute=5):
        """Record every WAJIB step of the playbook, in declared order."""
        out = []
        for index, step in enumerate(self.plan(playbook).steps):
            if not step.mandatory:
                continue
            out.append(self.record(incident_id, playbook, step.code,
                                   at=self.t(start_minute + index)))
        return out

    def drive_to_resolved(self, playbook="DOUBLE_BOOKING", severity="SEV2", **open_kwargs):
        """Open → MITIGATING → all mandatory steps → RESOLVED."""
        incident = self.open_incident(playbook=playbook, severity=severity, **open_kwargs)
        iid = incident.incident_id
        self.svc.mitigate(iid, actor="op_1", at=self.t(2))
        self.record_all_mandatory(iid, playbook)
        resolved = self.svc.resolve(iid, summary="insiden selesai ditangani penuh",
                                    actor="op_1", at=self.t(90))
        return resolved

    # ------------------------------------------------------------- snapshots
    def incident_row(self, incident_id):
        return self.conn.execute(
            "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()

    def step_rows(self, incident_id=None):
        if incident_id is None:
            return self.conn.execute("SELECT * FROM incident_steps ORDER BY seq").fetchall()
        return self.conn.execute(
            "SELECT * FROM incident_steps WHERE incident_id = ? ORDER BY seq", (incident_id,)
        ).fetchall()

    def counts(self):
        """(incidents, incident_steps, audit_events) row counts."""
        def one(table):
            return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return one("incidents"), one("incident_steps"), one("audit_events")

    def snapshot(self, incident_id):
        """Everything a refused operation could conceivably touch."""
        row = self.incident_row(incident_id)
        return (
            None if row is None else tuple(row),
            tuple(tuple(step) for step in self.step_rows(incident_id)),
            self.counts(),
        )

    def assert_rejected(self, exc, callable_, incident_id):
        """The call raises ``exc`` **and** leaves the database untouched."""
        before = self.snapshot(incident_id)
        with self.assertRaises(exc):
            callable_()
        self.assertEqual(before, self.snapshot(incident_id),
                         f"{exc.__name__} left a trace behind")
        return True

    def assert_rejected_without_row(self, exc, callable_):
        """Same, for calls that must not create a row (e.g. bad open_incident)."""
        before = self.counts()
        with self.assertRaises(exc):
            callable_()
        self.assertEqual(before, self.counts(), f"{exc.__name__} left a trace behind")
        return True


# ===========================================================================
# 1. Playbook catalogue (the deterministic part)
# ===========================================================================
class TestPlaybookCatalog(IncidentTestCase):

    def test_all_required_playbooks_present(self):
        for code in REQUIRED_PLAYBOOKS:
            self.assertIn(code, INC.PLAYBOOKS, f"playbook {code} hilang")

    def test_catalog_holds_no_surprise_playbooks(self):
        self.assertEqual(set(INC.PLAYBOOKS), set(REQUIRED_PLAYBOOKS))

    def test_every_playbook_declares_ordered_unique_step_codes(self):
        for code, playbook in INC.PLAYBOOKS.items():
            codes = [step.code for step in playbook.steps]
            self.assertGreaterEqual(len(codes), 3, f"{code} terlalu sedikit langkah")
            self.assertEqual(len(codes), len(set(codes)), f"{code} punya kode duplikat")
            prefixes = {step.code.split("-")[0] for step in playbook.steps}
            self.assertEqual(len(prefixes), 1, f"{code} memakai prefix tak seragam: {prefixes}")
            self.assertTrue(prefixes.pop().strip(), f"{code} tanpa prefix langkah")

    def test_every_step_has_owner_role_deadline_and_action(self):
        for code, playbook in INC.PLAYBOOKS.items():
            for step in playbook.steps:
                self.assertTrue(step.action.strip(), f"{code}/{step.code} tanpa aksi")
                self.assertGreaterEqual(len(step.action.strip()), 3)
                self.assertTrue(step.owner_role.strip(), f"{code}/{step.code} tanpa peran")
                self.assertIsInstance(step.due_minutes, int)
                self.assertGreater(step.due_minutes, 0,
                                   f"{code}/{step.code} tenggat tidak positif")

    def test_every_step_has_a_mandatory_flag(self):
        for code, playbook in INC.PLAYBOOKS.items():
            for step in playbook.steps:
                self.assertIsInstance(step.mandatory, bool,
                                      f"{code}/{step.code} tanpa penanda WAJIB")

    def test_every_playbook_has_at_least_one_mandatory_step(self):
        for code, playbook in INC.PLAYBOOKS.items():
            self.assertTrue(playbook.mandatory_codes, f"{code} tanpa langkah WAJIB")

    def test_mandatory_codes_are_a_subset_of_step_codes(self):
        for code, playbook in INC.PLAYBOOKS.items():
            declared = {step.code for step in playbook.steps}
            self.assertTrue(set(playbook.mandatory_codes) <= declared,
                            f"{code} punya WAJIB yang tidak ada di daftar langkah")

    def test_mandatory_first_step_is_the_earliest(self):
        """The first declared step of every playbook is WAJIB (detect-then-act)."""
        for code, playbook in INC.PLAYBOOKS.items():
            self.assertTrue(playbook.steps[0].mandatory,
                            f"{code} langkah pertamanya tidak WAJIB")

    def test_deadlines_are_intra_playbook_sane(self):
        for code, playbook in INC.PLAYBOOKS.items():
            for step in playbook.steps:
                self.assertLessEqual(step.due_minutes, 7 * 24 * 60,
                                     f"{code}/{step.code} tenggat > 7 hari")

    def test_playbook_document_exists_on_disk(self):
        doc = REPO_ROOT / INC.PLAYBOOK_DOCUMENT
        self.assertTrue(doc.is_file(), f"{INC.PLAYBOOK_DOCUMENT} tidak ada")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("Enam playbook insiden", text)

    def test_playbooks_cite_real_policy_sections(self):
        for code, playbook in INC.PLAYBOOKS.items():
            self.assertIn(INC.PLAYBOOK_DOCUMENT, playbook.policy_ref,
                          f"{code} tidak merujuk dokumen kebijakan")
            self.assertIn("\u00a72", playbook.policy_ref,
                          f"{code} tidak merujuk bagian \u00a72 kebijakan")

    def test_severity_scale_is_sev1_to_sev4(self):
        self.assertEqual(tuple(INC.SEVERITIES), ("SEV1", "SEV2", "SEV3", "SEV4"))

    def test_unknown_playbook_lookup_is_refused(self):
        with self.assertRaises(INC.UnknownPlaybook):
            INC.get_playbook("NOT_A_PLAYBOOK")
        with self.assertRaises(KeyError):
            INC.PLAYBOOKS["double_booking"]
        with self.assertRaises(INC.UnknownPlaybook):
            INC.get_playbook("double_booking")  # case-sensitive on purpose
        with self.assertRaises(INC.UnknownPlaybook):
            INC.get_playbook("")


# ===========================================================================
# 2. open_incident
# ===========================================================================
class TestOpenIncident(IncidentTestCase):

    def test_open_persists_an_open_incident(self):
        incident = self.open_incident()
        self.assertEqual(incident.status, "OPEN")
        self.assertEqual(incident.playbook, "DOUBLE_BOOKING")
        self.assertEqual(incident.severity, "SEV2")
        self.assertEqual(incident.detected_at, P.to_iso(self.start))
        self.assertIsNone(incident.resolved_at)
        self.assertIsNone(incident.notified_at)
        self.assertIsNone(incident.postmortem_ref)
        self.assertEqual(self.incident_row(incident.incident_id)["title"],
                         "Overbooking unit U1")

    def test_open_starts_with_no_steps(self):
        incident = self.open_incident()
        self.assertEqual(list(self.step_rows(incident.incident_id)), [])
        self.assertEqual(self.counts()[1], 0)

    def test_open_writes_an_audit_event(self):
        incident = self.open_incident()
        self.assertEqual(self.counts()[2], 1)
        self.assertEqual(self.svc.get(incident.incident_id).incident_id,
                         incident.incident_id)

    def test_open_accepts_all_four_severities(self):
        for severity in ("SEV1", "SEV2", "SEV3", "SEV4"):
            incident = self.open_incident(severity=severity)
            self.assertEqual(incident.severity, severity)

    def test_open_rejects_unknown_playbook(self):
        self.assert_rejected_without_row(
            INC.UnknownPlaybook,
            lambda: self.open_incident(playbook="TSUNAMI"),
        )

    def test_open_rejects_unknown_severity(self):
        self.assert_rejected_without_row(
            INC.InvalidSeverity, lambda: self.open_incident(severity="SEV5"))

    def test_open_rejects_lowercase_severity(self):
        self.assert_rejected_without_row(
            INC.InvalidSeverity, lambda: self.open_incident(severity="sev1"))

    def test_open_rejects_non_string_severity(self):
        for bad in (1, None, ["SEV1"]):
            with self.subTest(bad=bad):
                self.assert_rejected_without_row(
                    INC.InvalidSeverity, lambda b=bad: self.open_incident(severity=b))

    def test_open_rejects_short_title(self):
        self.assert_rejected_without_row(
            INC.InvalidField, lambda: self.open_incident(title="bocor"))

    def test_open_rejects_short_source(self):
        self.assert_rejected_without_row(
            INC.InvalidField, lambda: self.open_incident(source="ab"))

    def test_open_rejects_non_boolean_personal_data_flag(self):
        self.assert_rejected_without_row(
            INC.InvalidField,
            lambda: self.open_incident(personal_data_involved="ya"))

    def test_open_rejects_malformed_affected_refs(self):
        for bad in (["bad ref"], ["unit_1", ""], "unit_1", [123]):
            with self.subTest(bad=bad):
                self.assert_rejected_without_row(
                    INC.InvalidRef, lambda b=bad: self.open_incident(affected_refs=b))

    def test_open_rejects_unparsable_timestamp(self):
        self.assert_rejected_without_row(
            INC.InvalidTimestamp, lambda: self.open_incident(at="kemarin sore"))

    def test_open_rejects_duplicate_incident_id(self):
        incident = self.open_incident()
        self.assert_rejected_without_row(
            INC.StorageConstraint,
            lambda: self.open_incident(incident_id=incident.incident_id, title="Insiden lain"),
        )

    def test_open_detected_at_follows_the_clock_injection(self):
        incident = self.open_incident(at=self.t(30))
        self.assertEqual(incident.detected_at, P.to_iso(self.t(30)))
        self.assertEqual(incident.created_at, P.to_iso(self.start))
        self.assertEqual(self.svc.get(incident.incident_id).detected_at,
                         P.to_iso(self.t(30)))


# ===========================================================================
# 3. Regulatory notification obligation (personal data)
# ===========================================================================
class TestRegulatoryObligation(IncidentTestCase):

    def test_sev1_with_personal_data_requires_notification(self):
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV1", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        self.assertTrue(incident.regulatory_notification_required)
        stored = self.incident_row(incident.incident_id)
        self.assertEqual(stored["regulatory_notification_required"], 1)

    def test_sev2_with_personal_data_requires_notification(self):
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV2", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        self.assertTrue(incident.regulatory_notification_required)

    def test_notice_due_is_exactly_seventy_two_hours(self):
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV1", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        due = P.parse_iso(incident.notice_due_at)
        self.assertEqual(due - P.parse_iso(incident.detected_at), dt.timedelta(hours=72))
        self.assertEqual(INC.NOTICE_WINDOW, dt.timedelta(hours=72))
        self.assertEqual(due, self.start + 3 * DAY)

    def test_sev3_personal_data_creates_no_regulatory_obligation(self):
        incident = self.open_incident(severity="SEV3", personal_data_involved=True)
        self.assertFalse(incident.regulatory_notification_required)
        self.assertIsNone(incident.notice_due_at)

    def test_sev4_personal_data_creates_no_regulatory_obligation(self):
        incident = self.open_incident(severity="SEV4", personal_data_involved=True)
        self.assertFalse(incident.regulatory_notification_required)

    def test_sev1_without_personal_data_creates_no_obligation(self):
        incident = self.open_incident(severity="SEV1", personal_data_involved=False)
        self.assertFalse(incident.regulatory_notification_required)
        self.assertIsNone(incident.notice_due_at)

    def test_regulatory_severities_are_sev1_and_sev2(self):
        self.assertEqual(tuple(INC.REGULATORY_SEVERITIES), ("SEV1", "SEV2"))

    def test_obligation_is_visible_through_verify(self):
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV1", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        report = self.svc.verify(incident.incident_id)
        self.assertTrue(report["regulatory_notification_required"])
        self.assertEqual(report["notice_due_at"], incident.notice_due_at)
        self.assertIsNone(report["notified_at"])


# ===========================================================================
# 4. Append-only steps
# ===========================================================================
class TestStepAppendOnly(IncidentTestCase):

    def test_first_recorded_step_has_seq_one(self):
        incident = self.open_incident()
        step = self.record(incident.incident_id, "DOUBLE_BOOKING",
                           self.plan("DOUBLE_BOOKING").steps[0].code)
        self.assertEqual(step.seq, 1)
        self.assertEqual(step.step_code, self.plan("DOUBLE_BOOKING").steps[0].code)

    def test_seq_increases_without_gaps_or_duplicates(self):
        incident = self.open_incident()
        steps = self.record_all_mandatory(incident.incident_id, "DOUBLE_BOOKING")
        seqs = [step.seq for step in steps]
        self.assertEqual(seqs, list(range(1, len(steps) + 1)))
        stored = [row["seq"] for row in self.step_rows(incident.incident_id)]
        self.assertEqual(stored, list(range(1, len(stored) + 1)))
        self.assertEqual(len(stored), len(set(stored)))
        self.assertTrue(self.svc.verify(incident.incident_id)["seq_contiguous"])

    def test_skipping_a_mandatory_step_is_refused_without_row(self):
        incident = self.open_incident()
        plan = self.plan("DOUBLE_BOOKING")
        late = [step for step in plan.steps if step.code == "DB-04"][0]
        self.assert_rejected(
            INC.PlaybookStepViolated,
            lambda: self.svc.record_step(
                incident_id=incident.incident_id, action=f"DB-04 {late.action}",
                actor="gr_1", actor_role=late.owner_role, outcome="coba lompat",
                evidence_refs=("ev_1",), at=self.t(5)),
            incident.incident_id,
        )
        self.assertEqual(self.svc.steps(incident.incident_id), ())

    def test_out_of_order_mandatory_step_is_refused_without_row(self):
        incident = self.open_incident()
        self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-01")
        plan = self.plan("DOUBLE_BOOKING")
        third = next(step for step in plan.steps if step.code == "DB-03")
        self.assert_rejected(
            INC.PlaybookStepViolated,
            lambda: self.svc.record_step(
                incident_id=incident.incident_id, action=f"DB-03 {third.action}",
                actor="cm_1", actor_role=third.owner_role, outcome="coba lompat",
                evidence_refs=("ev_1",), at=self.t(9)),
            incident.incident_id,
        )
        self.assertEqual([step.step_code for step in self.svc.steps(incident.incident_id)],
                         ["DB-01"])

    def test_duplicate_step_code_is_refused_without_row(self):
        incident = self.open_incident()
        self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-01")
        plan = self.plan("DOUBLE_BOOKING")
        first = plan.steps[0]
        self.assert_rejected(
            INC.DuplicateStep,
            lambda: self.svc.record_step(
                incident_id=incident.incident_id, action=f"DB-01 {first.action}",
                actor="ops_1", actor_role=first.owner_role, outcome="ulangi lagi",
                evidence_refs=("ev_1",), at=self.t(9)),
            incident.incident_id,
        )
        self.assertEqual(len(self.svc.steps(incident.incident_id)), 1)

    def test_optional_step_may_be_skipped_and_the_incident_still_resolves(self):
        incident = self.open_incident()
        plan = self.plan("DOUBLE_BOOKING")
        self.assertTrue(any(not step.mandatory for step in plan.steps),
                        "fixture mengasumsikan ada langkah opsional")
        self.record_all_mandatory(incident.incident_id, "DOUBLE_BOOKING")
        progress = self.svc.mandatory_progress(incident.incident_id)
        self.assertTrue(progress["complete"])
        self.assertEqual(progress["missing"], ())

    def test_free_form_action_is_recorded_without_plan_code(self):
        incident = self.open_incident()
        step = self.svc.record_step(
            incident_id=incident.incident_id, action="catatan bebas tanpa kode",
            actor="ops_1", actor_role="OPERATIONS", outcome="dicatat sebagai konteks",
            evidence_refs=("ev_1",), at=self.t(5))
        self.assertIsNone(step.step_code)
        self.assertEqual(step.seq, 1)

    def test_foreign_playbook_code_is_not_treated_as_this_plan_step(self):
        incident = self.open_incident(playbook="DOUBLE_BOOKING")
        step = self.svc.record_step(
            incident_id=incident.incident_id, action="PM-01 langkah playbook lain",
            actor="fin_1", actor_role="FINANCE", outcome="catatan lintas playbook",
            evidence_refs=("ev_1",), at=self.t(5))
        self.assertIsNone(step.step_code)
        self.assertEqual(self.svc.mandatory_progress(incident.incident_id)["missing"],
                         self.plan("DOUBLE_BOOKING").mandatory_codes)

    def test_unknown_incident_is_refused(self):
        self.assert_rejected(
            INC.IncidentNotFound,
            lambda: self.svc.record_step(
                incident_id="inc_tidak_ada", action="DB-01 coba", actor="ops_1",
                actor_role="OPERATIONS", outcome="tidak akan tercatat",
                evidence_refs=(), at=self.t(5)),
            "inc_tidak_ada",
        )

    def test_step_before_detection_is_refused_without_row(self):
        # The floor is the earlier of created_at and detected_at: nothing may be
        # recorded as having happened before the incident row existed.
        incident = self.open_incident()
        iid = incident.incident_id
        self.assert_rejected(
            INC.InvalidTimestamp,
            lambda: self.svc.record_step(
                incident_id=iid, action="catatan mundur waktu",
                actor="ops_1", actor_role="OPERATIONS", outcome="ditolak",
                evidence_refs=(), at=self.start - dt.timedelta(minutes=1)),
            iid,
        )

    def test_step_rejects_unparsable_timestamp_without_row(self):
        incident = self.open_incident()
        self.assert_rejected(
            INC.InvalidTimestamp,
            lambda: self.svc.record_step(
                incident_id=incident.incident_id, action="catatan waktu rusak",
                actor="ops_1", actor_role="OPERATIONS", outcome="ditolak",
                evidence_refs=(), at="bukan timestamp"),
            incident.incident_id,
        )

    def test_step_requires_minimum_outcome_length(self):
        incident = self.open_incident()
        self.assert_rejected(
            INC.InvalidField,
            lambda: self.svc.record_step(
                incident_id=incident.incident_id, action="catatan singkat",
                actor="ops_1", actor_role="OPERATIONS", outcome="xy",
                evidence_refs=(), at=self.t(5)),
            incident.incident_id,
        )

    def test_step_requires_minimum_action_length(self):
        incident = self.open_incident()
        self.assert_rejected(
            INC.InvalidField,
            lambda: self.svc.record_step(
                incident_id=incident.incident_id, action="xy", actor="ops_1",
                actor_role="OPERATIONS", outcome="terlalu pendek",
                evidence_refs=(), at=self.t(5)),
            incident.incident_id,
        )

    def test_step_rejects_malformed_evidence_ref_without_row(self):
        incident = self.open_incident()
        self.assert_rejected(
            INC.InvalidRef,
            lambda: self.svc.record_step(
                incident_id=incident.incident_id, action="catatan bukti rusak",
                actor="ops_1", actor_role="OPERATIONS", outcome="ditolak",
                evidence_refs=("bad ref",), at=self.t(5)),
            incident.incident_id,
        )

    def test_direct_update_is_rejected_by_trigger(self):
        incident = self.open_incident()
        self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-01")
        before = self.snapshot(incident.incident_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE incident_steps SET outcome = 'diubah diam-diam' WHERE seq = 1")
        self.assertEqual(before, self.snapshot(incident.incident_id))

    def test_direct_delete_is_rejected_by_trigger(self):
        incident = self.open_incident()
        self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-01")
        before = self.snapshot(incident.incident_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM incident_steps WHERE seq = 1")
        self.assertEqual(before, self.snapshot(incident.incident_id))

    def test_direct_insert_with_duplicate_seq_is_rejected(self):
        incident = self.open_incident()
        self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-01")
        before = self.snapshot(incident.incident_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO incident_steps (step_id, incident_id, seq, action, actor,"
                " outcome, evidence_refs, occurred_at) VALUES (?,?,?,?,?,?,?,?)",
                ("step_sneaky", incident.incident_id, 1, "DB-01 disisipkan", "ops_1",
                 "baris palsu", "[]", P.to_iso(self.t(5))),
            )
        self.assertEqual(before, self.snapshot(incident.incident_id))

    def test_rejected_step_does_not_advance_seq(self):
        incident = self.open_incident()
        self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-01")
        plan = self.plan("DOUBLE_BOOKING")
        late = next(step for step in plan.steps if step.code == "DB-04")
        with self.assertRaises(INC.PlaybookStepViolated):
            self.svc.record_step(
                incident_id=incident.incident_id, action=f"DB-04 {late.action}",
                actor="gr_1", actor_role=late.owner_role, outcome="gagal",
                evidence_refs=(), at=self.t(7))
        second = self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-02")
        self.assertEqual(second.seq, 2)

    def test_step_history_survives_resolution_and_postmortem(self):
        incident = self.drive_to_resolved()
        iid = incident.incident_id
        self.svc.complete_postmortem(iid, postmortem_ref="doc_pm_7", actor="ops_1",
                                     at=self.t(120))
        step = self.svc.record_step(
            incident_id=iid, action="arsip bukti ditutup", actor="ops_1",
            actor_role="OPERATIONS", outcome="arsip ditutup", evidence_refs=("ev_9",),
            at=self.t(150))
        self.assertEqual(step.seq, len(self.svc.steps(iid)))
        self.assertEqual(self.svc.verify(iid)["seq_contiguous"], True)


# ===========================================================================
# 5. Status transitions
# ===========================================================================
class TestTransitions(IncidentTestCase):

    def test_open_to_mitigating_succeeds(self):
        incident = self.open_incident()
        self.assertEqual(self.svc.get(incident.incident_id).status, "OPEN")
        updated = self.svc.mitigate(incident.incident_id, actor="ops_1", at=self.t(5))
        self.assertEqual(updated.status, "MITIGATING")
        self.assertIsNone(updated.resolved_at)

    def test_open_to_resolved_is_refused_without_trace(self):
        incident = self.open_incident()
        self.assert_rejected(
            INC.InvalidTransition,
            lambda: self.svc.resolve(incident.incident_id,
                                     summary="ringkasan cukup panjang", actor="ops_1",
                                     at=self.t(5)),
            incident.incident_id,
        )

    def test_resolved_requires_every_mandatory_step(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.svc.mitigate(iid, actor="ops_1", at=self.t(2))
        plan = self.plan("DOUBLE_BOOKING")
        partial = [step for step in plan.steps if step.mandatory][:1]
        for step in partial:
            self.record(iid, "DOUBLE_BOOKING", step.code)
        self.assert_rejected(
            INC.PlaybookStepViolated,
            lambda: self.svc.resolve(iid, summary="saya kira sudah selesai",
                                     actor="ops_1", at=self.t(60)),
            iid,
        )

    def test_resolved_requires_a_ten_character_summary(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.svc.mitigate(iid, actor="ops_1", at=self.t(2))
        self.record_all_mandatory(iid, "DOUBLE_BOOKING")
        self.assert_rejected(
            INC.ResolutionBlocked,
            lambda: self.svc.resolve(iid, summary="pendek", actor="ops_1", at=self.t(60)),
            iid,
        )

    def test_resolved_rejects_missing_summary(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.svc.mitigate(iid, actor="ops_1", at=self.t(2))
        self.record_all_mandatory(iid, "DOUBLE_BOOKING")
        self.assert_rejected(
            INC.ResolutionBlocked,
            lambda: self.svc.resolve(iid, summary="pendek", actor="ops_1", at=self.t(60)),
            iid,
        )
        # A missing summary field is not a substitute either: the stored one is
        # absent here, so the resolution is refused on the same rule.
        bare = self.open_incident(summary=None)
        self.svc.mitigate(bare.incident_id, actor="ops_1", at=self.t(2))
        self.record_all_mandatory(bare.incident_id, "DOUBLE_BOOKING")
        self.assert_rejected(
            INC.ResolutionBlocked,
            lambda: self.svc.resolve(bare.incident_id, summary=None, actor="ops_1",
                                     at=self.t(60)),
            bare.incident_id,
        )

    def test_resolved_succeeds_when_complete(self):
        incident = self.drive_to_resolved()
        self.assertEqual(incident.status, "RESOLVED")
        self.assertIsNotNone(incident.resolved_at)
        self.assertEqual(self.incident_row(incident.incident_id)["status"], "RESOLVED")

    def test_postmortem_requires_resolved_first(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.svc.mitigate(iid, actor="ops_1", at=self.t(2))
        self.record_all_mandatory(iid, "DOUBLE_BOOKING")
        self.assert_rejected(
            INC.InvalidTransition,
            lambda: self.svc.complete_postmortem(iid, postmortem_ref="doc_pm_1",
                                                 actor="ops_1", at=self.t(70)),
            iid,
        )

    def test_postmortem_requires_a_reference(self):
        incident = self.drive_to_resolved()
        self.assert_rejected(
            INC.ResolutionBlocked,
            lambda: self.svc.complete_postmortem(incident.incident_id,
                                                 postmortem_ref=None, actor="ops_1",
                                                 at=self.t(120)),
            incident.incident_id,
        )

    def test_postmortem_requires_resolved_status_not_just_open(self):
        incident = self.open_incident()
        self.assert_rejected(
            INC.InvalidTransition,
            lambda: self.svc.complete_postmortem(incident.incident_id,
                                                 postmortem_ref="doc_pm_1",
                                                 actor="ops_1", at=self.t(30)),
            incident.incident_id,
        )

    def test_postmortem_done_succeeds_with_reference(self):
        incident = self.drive_to_resolved()
        iid = incident.incident_id
        done = self.svc.complete_postmortem(iid, postmortem_ref="doc_pm_9",
                                            actor="ops_1", at=self.t(120))
        self.assertEqual(done.status, "POSTMORTEM_DONE")
        self.assertEqual(done.postmortem_ref, "doc_pm_9")
        self.assertEqual(self.incident_row(iid)["postmortem_ref"], "doc_pm_9")

    def test_backward_transition_is_refused_without_trace(self):
        incident = self.drive_to_resolved()
        iid = incident.incident_id
        self.svc.complete_postmortem(iid, postmortem_ref="doc_pm_3", actor="ops_1",
                                     at=self.t(120))
        self.assert_rejected(
            INC.InvalidTransition,
            lambda: self.svc.mitigate(iid, actor="ops_1", at=self.t(150)),
            iid,
        )
        self.assert_rejected(
            INC.InvalidTransition,
            lambda: self.svc.resolve(iid, summary="mengulang penyelesaian",
                                     actor="ops_1", at=self.t(150)),
            iid,
        )

    def test_same_status_transition_is_refused_without_trace(self):
        incident = self.open_incident()
        self.assert_rejected(
            INC.InvalidTransition,
            lambda: self.svc.transition(incident.incident_id, "OPEN", actor="ops_1",
                                        at=self.t(3)),
            incident.incident_id,
        )
        self.svc.mitigate(incident.incident_id, actor="ops_1", at=self.t(5))
        self.assert_rejected(
            INC.InvalidTransition,
            lambda: self.svc.transition(incident.incident_id, "MITIGATING",
                                        actor="ops_1", at=self.t(9)),
            incident.incident_id,
        )

    def test_unknown_status_is_refused_without_trace(self):
        incident = self.open_incident()
        self.assert_rejected(
            INC.InvalidStatus,
            lambda: self.svc.transition(incident.incident_id, "CANCELLED",
                                        actor="ops_1", at=self.t(3)),
            incident.incident_id,
        )

    def test_unknown_incident_transition_is_refused(self):
        self.assert_rejected(
            INC.IncidentNotFound,
            lambda: self.svc.mitigate("inc_tidak_ada", actor="ops_1", at=self.t(3)),
            "inc_tidak_ada",
        )

    def test_allowed_transitions_are_the_policy_chain(self):
        self.assertEqual(INC.ALLOWED_TRANSITIONS["OPEN"], ("MITIGATING",))
        self.assertEqual(INC.ALLOWED_TRANSITIONS["MITIGATING"], ("RESOLVED",))
        self.assertEqual(INC.ALLOWED_TRANSITIONS["RESOLVED"], ("POSTMORTEM_DONE",))
        self.assertEqual(INC.ALLOWED_TRANSITIONS["POSTMORTEM_DONE"], ())


# ===========================================================================
# 6. Notification *recording* (never sending)
# ===========================================================================
class TestNotifying(IncidentTestCase):

    def test_notify_without_any_step_is_blocked_without_trace(self):
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV1", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        self.assert_rejected(
            INC.NotificationBlocked,
            lambda: self.svc.notify(incident_id=incident.incident_id, channel="email",
                                    sent_to_role="dpo_lead", actor="ops_1", at=self.t(5)),
            incident.incident_id,
        )
        self.assertIsNone(self.svc.get(incident.incident_id).notified_at)

    def test_notify_records_a_step_and_stamps_notified_at(self):
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV1", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        iid = incident.incident_id
        self.record(iid, "DATA_BREACH", self.plan("DATA_BREACH").steps[0].code)
        step = self.svc.notify(incident_id=iid, channel="regulator_portal",
                               sent_to_role="regulator_lead", actor="dpo_1", at=self.t(30))
        self.assertTrue(step.is_notification)
        self.assertTrue(step.action.startswith(INC.NOTIFICATION_PREFIX))
        self.assertEqual(self.svc.get(iid).notified_at, P.to_iso(self.t(30)))

    def test_notify_is_recorded_not_sent(self):
        """Only a database row changes: no delivery, no status change."""
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV1", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        iid = incident.incident_id
        self.record(iid, "DATA_BREACH", self.plan("DATA_BREACH").steps[0].code)
        before_status = self.svc.get(iid).status
        self.svc.notify(incident_id=iid, channel="email", sent_to_role="dpo_lead",
                        actor="dpo_1", at=self.t(30))
        after = self.svc.get(iid)
        self.assertEqual(after.status, before_status)
        self.assertIsNone(after.resolved_at)
        self.assertEqual(self.svc.verify(iid)["notifications"], 1)

    def test_notify_does_not_satisfy_a_mandatory_plan_step(self):
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV1", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        iid = incident.incident_id
        self.record(iid, "DATA_BREACH", self.plan("DATA_BREACH").steps[0].code)
        self.svc.notify(incident_id=iid, channel="email", sent_to_role="dpo_lead",
                        actor="dpo_1", at=self.t(30))
        progress = self.svc.mandatory_progress(iid)
        self.assertFalse(progress["complete"])
        self.assertNotIn(INC.NOTIFICATION_PREFIX, progress["recorded"])

    def test_notify_rejects_unknown_channel_without_trace(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.record(iid, "DOUBLE_BOOKING", "DB-01")
        self.assert_rejected(
            INC.InvalidField,
            lambda: self.svc.notify(incident_id=iid, channel="merpati",
                                    sent_to_role="ops_lead", actor="ops_1", at=self.t(30)),
            iid,
        )

    def test_notify_rejects_short_role_without_trace(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.record(iid, "DOUBLE_BOOKING", "DB-01")
        self.assert_rejected(
            INC.InvalidField,
            lambda: self.svc.notify(incident_id=iid, channel="email", sent_to_role="x",
                                    actor="ops_1", at=self.t(30)),
            iid,
        )

    def test_notify_rejects_timestamp_before_detection_without_trace(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.record(iid, "DOUBLE_BOOKING", self.plan("DOUBLE_BOOKING").steps[0].code,
                    at=self.t(5))
        self.assert_rejected(
            INC.InvalidTimestamp,
            lambda: self.svc.notify(incident_id=iid, channel="email",
                                    sent_to_role="ops_lead", actor="ops_1",
                                    at=self.start - dt.timedelta(minutes=1)),
            iid,
        )

    def test_notify_cannot_move_the_clock_backwards(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.record(iid, "DOUBLE_BOOKING", self.plan("DOUBLE_BOOKING").steps[0].code,
                    at=self.t(5))
        self.svc.notify(incident_id=iid, channel="email", sent_to_role="ops_lead",
                        actor="ops_1", at=self.t(20))
        self.assert_rejected(
            INC.InvalidTimestamp,
            lambda: self.svc.notify(incident_id=iid, channel="sms",
                                    sent_to_role="duty_manager", actor="ops_1",
                                    at=self.t(10)),
            iid,
        )

    def test_notify_rejects_unknown_incident(self):
        self.assert_rejected(
            INC.IncidentNotFound,
            lambda: self.svc.notify(incident_id="inc_tidak_ada", channel="email",
                                    sent_to_role="ops_lead", actor="ops_1", at=self.t(5)),
            "inc_tidak_ada",
        )

    def test_multiple_channels_each_get_their_own_seq(self):
        incident = self.open_incident(playbook="SAFETY_EMERGENCY",
                                      title="Insiden keselamatan tamu", severity="SEV1",
                                      affected_refs=("guest_2",))
        iid = incident.incident_id
        self.record(iid, "SAFETY_EMERGENCY", self.plan("SAFETY_EMERGENCY").steps[0].code,
                    at=self.t(5))
        first = self.svc.notify(incident_id=iid, channel="phone", sent_to_role="ops_lead",
                                actor="ops_1", at=self.t(10))
        second = self.svc.notify(incident_id=iid, channel="email", sent_to_role="gm",
                                 actor="ops_1", at=self.t(20))
        self.assertEqual((first.seq, second.seq), (2, 3))
        self.assertEqual(self.svc.verify(iid)["notifications"], 2)

    def test_notification_channels_are_a_closed_allowlist(self):
        self.assertIsInstance(INC.NOTIFICATION_CHANNELS, tuple)
        self.assertIn("email", INC.NOTIFICATION_CHANNELS)
        self.assertNotIn("carrier_pigeon", INC.NOTIFICATION_CHANNELS)


# ===========================================================================
# 7. overdue_notifications (the 72-hour boundary)
# ===========================================================================
class TestOverdueNotifications(IncidentTestCase):

    def breach(self, severity="SEV1", at=None):
        return self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                  severity=severity, personal_data_involved=True,
                                  affected_refs=("guest_1",), source="siem_alert", at=at)

    def test_exactly_seventy_two_hours_is_not_overdue(self):
        self.breach()
        self.assertEqual(self.svc.overdue_notifications(self.start + 3 * DAY), ())

    def test_one_second_past_seventy_two_hours_is_overdue(self):
        incident = self.breach()
        overdue = self.svc.overdue_notifications(self.start + 3 * DAY + dt.timedelta(seconds=1))
        self.assertEqual([item.incident_id for item in overdue], [incident.incident_id])

    def test_one_second_before_seventy_two_hours_is_not_overdue(self):
        self.breach()
        self.assertEqual(
            self.svc.overdue_notifications(self.start + 3 * DAY - dt.timedelta(seconds=1)), ())

    def test_exact_boundary_via_explicit_deadline(self):
        incident = self.breach()
        due = P.parse_iso(incident.notice_due_at)
        self.assertEqual(self.svc.overdue_notifications(due), ())
        self.assertEqual(len(self.svc.overdue_notifications(due + dt.timedelta(seconds=1))), 1)

    def test_a_notified_incident_leaves_the_overdue_queue(self):
        incident = self.breach()
        iid = incident.incident_id
        self.record(iid, "DATA_BREACH", self.plan("DATA_BREACH").steps[0].code)
        self.svc.notify(incident_id=iid, channel="regulator_portal",
                        sent_to_role="regulator_lead", actor="dpo_1", at=self.t(45))
        self.assertEqual(self.svc.overdue_notifications(self.start + 5 * DAY), ())

    def test_non_regulatory_incident_is_never_overdue(self):
        self.open_incident(severity="SEV1", personal_data_involved=False)
        self.assertEqual(self.svc.overdue_notifications(self.start + 30 * DAY), ())

    def test_personal_data_on_sev3_is_never_overdue(self):
        self.open_incident(severity="SEV3", personal_data_involved=True)
        self.assertEqual(self.svc.overdue_notifications(self.start + 30 * DAY), ())

    def test_overdue_is_ordered_and_deterministic(self):
        first = self.breach(at=self.t(0))
        second = self.breach(at=self.t(10))
        overdue = self.svc.overdue_notifications(self.start + 4 * DAY)
        self.assertEqual([item.incident_id for item in overdue],
                         [first.incident_id, second.incident_id])

    def test_overdue_accepts_datetime_and_iso_string(self):
        incident = self.breach()
        moment = self.start + 4 * DAY
        self.assertEqual(
            [item.incident_id for item in self.svc.overdue_notifications(P.to_iso(moment))],
            [incident.incident_id])

    def test_overdue_rejects_unparsable_now(self):
        self.breach()
        with self.assertRaises(INC.InvalidTimestamp):
            self.svc.overdue_notifications("kemarin")

    def test_summary_counts_reports_pending_and_overdue(self):
        self.breach()
        counts = self.svc.summary_counts()
        self.assertEqual(counts["personal_data_involved"], 1)
        self.assertEqual(counts["regulatory_pending"], 1)
        self.assertEqual(counts["notice_overdue"], 0)


# ===========================================================================
# 8. Read models
# ===========================================================================
class TestReads(IncidentTestCase):

    def test_get_returns_the_incident(self):
        incident = self.open_incident()
        self.assertEqual(self.svc.get(incident.incident_id).incident_id, incident.incident_id)

    def test_get_unknown_incident_raises(self):
        with self.assertRaises(INC.IncidentNotFound):
            self.svc.get("inc_hantu")

    def test_timeline_is_ordered_by_seq(self):
        incident = self.open_incident()
        recorded = self.record_all_mandatory(incident.incident_id, "DOUBLE_BOOKING")
        timeline = self.svc.timeline(incident.incident_id)
        self.assertEqual([step.seq for step in timeline], list(range(1, len(recorded) + 1)))
        self.assertEqual([step.step_code for step in timeline],
                         [step.step_code for step in recorded])

    def test_timeline_includes_notification_rows(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.record(iid, "DOUBLE_BOOKING", "DB-01")
        self.svc.notify(incident_id=iid, channel="email", sent_to_role="ops_lead",
                        actor="ops_1", at=self.t(20))
        timeline = self.svc.timeline(iid)
        self.assertEqual(len(timeline), 2)
        self.assertFalse(timeline[0].is_notification)
        self.assertTrue(timeline[1].is_notification)

    def test_timeline_of_unknown_incident_raises(self):
        with self.assertRaises(INC.IncidentNotFound):
            self.svc.timeline("inc_hantu")

    def test_open_incidents_excludes_resolved_and_postmortem(self):
        incident = self.drive_to_resolved()
        self.assertEqual(self.svc.open_incidents(), ())
        self.svc.complete_postmortem(incident.incident_id, postmortem_ref="doc_pm_1",
                                     actor="ops_1", at=self.t(120))
        self.assertEqual(self.svc.open_incidents(), ())

    def test_open_incidents_includes_open_and_mitigating(self):
        first = self.open_incident(severity="SEV2")
        second = self.open_incident(severity="SEV1")
        self.svc.mitigate(second.incident_id, actor="ops_1", at=self.t(3))
        ids = [item.incident_id for item in self.svc.open_incidents()]
        self.assertEqual(set(ids), {first.incident_id, second.incident_id})

    def test_open_incidents_orders_most_severe_first(self):
        low = self.open_incident(severity="SEV4")
        high = self.open_incident(severity="SEV1")
        mid = self.open_incident(severity="SEV3")
        ids = [item.incident_id for item in self.svc.open_incidents()]
        self.assertEqual(ids, [high.incident_id, mid.incident_id, low.incident_id])

    def test_by_severity_filters(self):
        sev1 = self.open_incident(severity="SEV1")
        self.open_incident(severity="SEV2")
        self.assertEqual([item.incident_id for item in self.svc.by_severity("SEV1")],
                         [sev1.incident_id])

    def test_by_severity_without_argument_returns_per_severity_counts(self):
        self.open_incident(severity="SEV1")
        self.open_incident(severity="SEV3")
        self.open_incident(severity="SEV3")
        counts = self.svc.by_severity()
        self.assertEqual(counts, {"SEV1": 1, "SEV2": 0, "SEV3": 2, "SEV4": 0})

    def test_by_severity_rejects_unknown_severity(self):
        with self.assertRaises(INC.InvalidSeverity):
            self.svc.by_severity("SEV9")

    def test_by_severity_open_only_excludes_resolved(self):
        resolved = self.drive_to_resolved(severity="SEV2")
        open_one = self.open_incident(severity="SEV2")
        ids = [item.incident_id for item in self.svc.by_severity("SEV2", open_only=True)]
        self.assertEqual(ids, [open_one.incident_id])
        self.assertNotIn(resolved.incident_id, ids)

    def test_summary_counts_shape(self):
        self.open_incident(severity="SEV1")
        counts = self.svc.summary_counts()
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["open"], 1)
        self.assertEqual(counts["closed"], 0)
        self.assertEqual(set(counts["by_status"]), {"OPEN", "MITIGATING", "RESOLVED",
                                                   "POSTMORTEM_DONE"})
        self.assertEqual(set(counts["by_severity"]), {"SEV1", "SEV2", "SEV3", "SEV4"})
        self.assertEqual(counts["by_severity"]["SEV1"], 1)

    def test_summary_counts_tracks_closed_incidents(self):
        incident = self.drive_to_resolved()
        self.svc.complete_postmortem(incident.incident_id, postmortem_ref="doc_pm_2",
                                     actor="ops_1", at=self.t(120))
        counts = self.svc.summary_counts()
        self.assertEqual(counts["closed"], 1)
        self.assertEqual(counts["open"], 0)
        self.assertEqual(counts["by_status"]["POSTMORTEM_DONE"], 1)
        self.assertEqual(counts["steps"], len(self.svc.steps(incident.incident_id)))


# ===========================================================================
# 9. verify()
# ===========================================================================
class TestVerify(IncidentTestCase):

    def test_verify_ok_for_a_fully_handled_incident(self):
        incident = self.drive_to_resolved()
        report = self.svc.verify(incident.incident_id)
        self.assertTrue(report["ok"], report["issues"])
        self.assertTrue(report["mandatory_complete"])
        self.assertTrue(report["seq_contiguous"])
        self.assertEqual(report["missing_required_steps"], ())
        self.assertEqual(report["issues"], ())

    def test_verify_ok_for_a_postmortem_done_incident(self):
        incident = self.drive_to_resolved()
        self.svc.complete_postmortem(incident.incident_id, postmortem_ref="doc_pm_5",
                                     actor="ops_1", at=self.t(120))
        report = self.svc.verify(incident.incident_id)
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["status"], "POSTMORTEM_DONE")

    def test_verify_reports_missing_mandatory_steps(self):
        incident = self.open_incident()
        report = self.svc.verify(incident.incident_id)
        self.assertFalse(report["ok"])
        self.assertFalse(report["mandatory_complete"])
        self.assertEqual(report["missing_required_steps"],
                         self.plan("DOUBLE_BOOKING").mandatory_codes)
        self.assertTrue(any("WAJIB" in issue for issue in report["issues"]))

    def test_verify_lists_required_and_recorded_steps(self):
        incident = self.open_incident()
        self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-01")
        report = self.svc.verify(incident.incident_id)
        self.assertEqual(report["required_steps"], self.plan("DOUBLE_BOOKING").mandatory_codes)
        self.assertIn("DB-01", report["recorded_steps"])

    def test_verify_detects_a_seq_gap(self):
        incident = self.open_incident()
        self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-01")
        self.conn.execute(
            "INSERT INTO incident_steps (step_id, incident_id, seq, action, actor, outcome,"
            " evidence_refs, occurred_at) VALUES (?,?,?,?,?,?,?,?)",
            ("step_gap", incident.incident_id, 5, "catatan bolong", "ops_1",
             "baris menyisipkan celah", "[]", P.to_iso(self.t(9))),
        )
        report = self.svc.verify(incident.incident_id)
        self.assertFalse(report["seq_contiguous"])
        self.assertFalse(report["ok"])
        self.assertTrue(any("seq" in issue for issue in report["issues"]))

    def test_incoherent_writes_are_rejected_by_the_schema(self):
        # Defence in depth: the schema CHECKs refuse the incoherent writes that
        # verify() would otherwise have to report afterwards.
        plain = self.open_incident(severity="SEV3")
        regulatory = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                        severity="SEV2", personal_data_involved=True,
                                        affected_refs=("guest_1",), source="siem_alert")
        before = (self.snapshot(plain.incident_id), self.snapshot(regulatory.incident_id))

        # A regulatory flag is only coherent on SEV1/SEV2.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE incidents SET regulatory_notification_required = 1 "
                "WHERE incident_id = ?", (plain.incident_id,))
        self.conn.rollback()

        # A closed status is only coherent with a usable resolution summary.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE incidents SET status = 'RESOLVED' WHERE incident_id = ?",
                              (regulatory.incident_id,))
        self.conn.rollback()

        # A notice deadline only exists with a required notice, and it can never
        # be at or before detection (a zero-minute window is not a deadline).
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE incidents SET notice_due_at = ? WHERE incident_id = ?",
                              (P.to_iso(self.start - dt.timedelta(hours=1)),
                               regulatory.incident_id))
        self.conn.rollback()

        self.assertEqual((self.snapshot(plain.incident_id),
                          self.snapshot(regulatory.incident_id)), before)
        # The refusals changed no playbook accounting either: both incidents are
        # still exactly as incomplete as they were before the attempts.
        for iid, playbook in ((plain.incident_id, "DOUBLE_BOOKING"),
                              (regulatory.incident_id, "DATA_BREACH")):
            report = self.svc.verify(iid)
            self.assertEqual(report["missing_required_steps"],
                             self.plan(playbook).mandatory_codes)
            self.assertTrue(report["seq_contiguous"])

    def test_verify_detects_notice_due_that_is_not_72_hours(self):
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV1", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        self.conn.execute(
            "UPDATE incidents SET notice_due_at = ? WHERE incident_id = ?",
            (P.to_iso(self.start + DAY), incident.incident_id),
        )
        report = self.svc.verify(incident.incident_id)
        self.assertFalse(report["ok"])
        self.assertTrue(any("72" in issue or "notice_due_at" in issue
                            for issue in report["issues"]))

    def test_verify_detects_missing_regulatory_flag(self):
        incident = self.open_incident(playbook="DATA_BREACH", title="Kebocoran data tamu",
                                      severity="SEV1", personal_data_involved=True,
                                      affected_refs=("guest_1",), source="siem_alert")
        self.conn.execute(
            "UPDATE incidents SET regulatory_notification_required = 0, notice_due_at = NULL"
            " WHERE incident_id = ?", (incident.incident_id,))
        report = self.svc.verify(incident.incident_id)
        self.assertFalse(report["ok"])
        self.assertTrue(any("notifikasi" in issue for issue in report["issues"]))

    def test_verify_detects_notice_due_without_obligation(self):
        incident = self.open_incident()
        self.conn.execute(
            "UPDATE incidents SET notice_due_at = ? WHERE incident_id = ?",
            (P.to_iso(self.t(10)), incident.incident_id),
        )
        report = self.svc.verify(incident.incident_id)
        self.assertFalse(report["ok"])
        self.assertTrue(any("notice_due_at terisi" in issue for issue in report["issues"]))

    def test_verify_reports_counts_and_last_seq(self):
        incident = self.open_incident()
        iid = incident.incident_id
        self.record(iid, "DOUBLE_BOOKING", "DB-01")
        self.svc.notify(incident_id=iid, channel="email", sent_to_role="ops_lead",
                        actor="ops_1", at=self.t(20))
        report = self.svc.verify(iid)
        self.assertEqual(report["steps"], 2)
        self.assertEqual(report["notifications"], 1)
        self.assertEqual(report["last_seq"], 2)

    def test_verify_unknown_incident_raises(self):
        with self.assertRaises(INC.IncidentNotFound):
            self.svc.verify("inc_hantu")

    def test_verify_is_read_only(self):
        incident = self.open_incident()
        self.record(incident.incident_id, "DOUBLE_BOOKING", "DB-01")
        before = self.snapshot(incident.incident_id)
        self.svc.verify(incident.incident_id)
        self.assertEqual(before, self.snapshot(incident.incident_id))


# ===========================================================================
# 10. Source hygiene (stdlib only, nothing is ever sent)
# ===========================================================================
class TestSourceHygiene(IncidentTestCase):

    def setUp(self):
        super().setUp()
        self.source = pathlib.Path(INC.__file__).read_text(encoding="utf-8")

    def test_module_imports_only_stdlib(self):
        import ast

        tree = ast.parse(self.source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        allowed = {"datetime", "re", "sqlite3", "typing", "collections", "json",
                   "hashlib", "uuid", "dataclasses", "enum", "math", "itertools",
                   "contextlib", "primitives", "audit", "cases", "storage",
                   "__future__"}
        self.assertTrue(imported <= allowed, f"impor tak diizinkan: {imported - allowed}")

    def test_no_network_libraries_are_imported(self):
        for banned in ("smtplib", "requests", "urllib", "httplib", "http.client",
                       "socket", "ftplib", "telnetlib", "smtpd"):
            self.assertNotIn(f"import {banned}", self.source)
            self.assertNotIn(f"from {banned}", self.source)

    def test_no_outbound_send_primitives_exist(self):
        for banned in ("sendmail", "urlopen", "HTTPConnection", "requests.post",
                       "smtp.send"):
            self.assertNotIn(banned, self.source,
                             f"{banned} terdeteksi: notifikasi harus DICATAT, bukan dikirim")

    def test_notification_prefix_is_a_recording_marker(self):
        self.assertTrue(INC.NOTIFICATION_PREFIX)
        self.assertTrue(INC.NOTIFICATION_PREFIX.endswith(":"))

    def test_policy_document_reference_matches_the_repository(self):
        self.assertEqual(INC.PLAYBOOK_DOCUMENT, "docs/RETENTION_AND_INCIDENT_RESPONSE.md")
        self.assertTrue((REPO_ROOT / INC.PLAYBOOK_DOCUMENT).is_file())


# ===========================================================================
# 11. Cross-playbook equivalence (every playbook is enforceable, not decoration)
# ===========================================================================
class TestEveryPlaybookIsEnforceable(IncidentTestCase):

    def test_every_playbook_enforces_its_mandatory_first_step(self):
        for code in REQUIRED_PLAYBOOKS:
            with self.subTest(playbook=code):
                incident = self.open_incident(playbook=code,
                                              title=f"Uji playbook {code}",
                                              source="ops_report",
                                              affected_refs=("unit_9",))
                iid = incident.incident_id
                plan = self.plan(code)
                second = plan.steps[1]
                self.assert_rejected(
                    INC.PlaybookStepViolated,
                    lambda i=iid, s=second: self.svc.record_step(
                        incident_id=i, action=f"{s.code} {s.action}", actor="op_1",
                        actor_role=s.owner_role, outcome="coba lompat",
                        evidence_refs=("ev_1",), at=self.t(5)),
                    iid,
                )

    def test_every_playbook_can_be_driven_to_postmortem_done(self):
        for index, code in enumerate(REQUIRED_PLAYBOOKS):
            with self.subTest(playbook=code):
                incident = self.drive_to_resolved(
                    playbook=code,
                    title=f"Uji {code}",
                    severity="SEV3",
                    affected_refs=("unit_9",),
                    summary=None,
                    at=self.t(index),
                )
                done = self.svc.complete_postmortem(incident.incident_id,
                                                    postmortem_ref=f"doc_pm_{index}",
                                                    actor="op_1", at=self.t(60 + index))
                self.assertEqual(done.status, "POSTMORTEM_DONE")
                self.assertTrue(self.svc.verify(done.incident_id)["ok"],
                                self.svc.verify(done.incident_id)["issues"])

    def test_every_playbook_resolution_is_blocked_until_mandatory_steps_exist(self):
        for code in REQUIRED_PLAYBOOKS:
            with self.subTest(playbook=code):
                incident = self.open_incident(playbook=code, title=f"Uji {code}",
                                              severity="SEV3", affected_refs=("unit_9",))
                iid = incident.incident_id
                self.svc.mitigate(iid, actor="op_1", at=self.t(2))
                self.assert_rejected(
                    INC.PlaybookStepViolated,
                    lambda i=iid: self.svc.resolve(i, summary="ringkasan cukup panjang",
                                                   actor="op_1", at=self.t(60)),
                    iid,
                )


if __name__ == "__main__":
    unittest.main()
