"""Independent verification of the operational task queue ``src/ops_tasks.py``.

Authority chain: ``src/schema/001_initial.sql`` owns the storage contract for
``ops_tasks`` (plus the parent ``properties``/``units``/``bookings``/``cases``
rows a task may point at). Nothing here mocks storage: every test drives the real
``ops_tasks.OpsTaskService`` against an in-memory SQLite database created by
``schema_loader.connect_sqlite`` (the same DDL PostgreSQL mirrors), with time
from ``primitives.FrozenClock`` and identifiers from a deterministic
``IdFactory`` so ``task_id`` assertions stay exact.

The ten facts this file pins down, and where:

1. a created row carries every column the DDL declares, and a replayed
   ``idempotency_key`` writes no second row -> TestCreateTaskStorage,
   TestIdempotencyKeySemantics.
2. a dead reference is refused *before* the INSERT (``TaskRefNotFound``, not a
   raw foreign-key ``IntegrityError``) -> TestDeadReferences.
3. ``status`` only moves along ``TASK_TRANSITIONS``; ``BLOCKED`` needs a stated
   reason; ``DONE``/``CANCELLED`` are terminal -> TestStatusGraph.
4. ``DONE`` means the runbook ran: an open step blocks completion, and a
   checklist over 50 steps or with a blank/oversized/duplicate label is refused
   -> TestCompletion, TestChecklistValidation.
5. deadline maths: ``overdue()`` is strict (``due_at`` strictly before *now*),
   ``due_within()`` is the half-open ``[now, now + hours)``, both read active
   tasks only -> TestDeadline.
6. the queue filters return exactly the right set, ordered by
   ``(due_at, priority desc, task_id)`` -> TestListFilters.
7. ``counts_by_status``/``counts_by_kind``/``next_due`` agree with the rows ->
   TestCounts, TestNextDue.
8. ``progress``/``open_steps``/``checklist_complete`` (properties) agree with
   the stored runbook -> TestChecklistProjection.
9. ticking a step twice changes nothing and never misfiles a look-alike label ->
   TestChecklistCommands.
10. every stored stamp is an ISO-8601 instant with an offset, and no mandatory
    column holds ``None`` or a float -> TestStoredShape.

Every refusal is checked against the database, not only against the exception:
``assertFrozen`` compares the whole row (status, checklist, reference columns,
both stamps) and the row count before and after the failed command, so a
"refused" command that secretly wrote is caught here.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path

import ops_tasks as OT
import primitives as P
from schema_loader import connect_sqlite

START = "2026-09-21T08:00:00Z"
NOW = P.parse_iso(START)
#: The canonical form the storage layer keeps (``to_iso`` drops the ``Z``).
NOW_ISO = P.to_iso(NOW)
DUE = "2026-09-22T08:00:00Z"
DUE_ISO = P.to_iso(P.parse_iso(DUE))
LATER = "2026-09-23T08:00:00Z"

#: Canonical stamp shape: UTC, seconds precision, explicit offset.
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

#: Columns this module always fills (tighter than the DDL, which lets
#: ``assignee`` be NULL until the module gives the task an owner).
REQUIRED_COLUMNS = (
    "task_id",
    "kind",
    "due_at",
    "priority",
    "assignee",
    "status",
    "checklist",
    "idempotency_key",
    "created_at",
    "updated_at",
)
OPTIONAL_COLUMNS = ("booking_id", "unit_id", "case_id", "completed_at", "blocked_reason")

SOURCE_PATH = Path(__file__).resolve().parents[1] / "src" / "ops_tasks.py"


def seed_world(conn) -> None:
    """Parent chain a task may reference: property -> unit -> booking -> case.

    The values satisfy every CHECK constraint in ``001_initial.sql`` (property in
    Bali, ACTIVE unit, active booking slot, case SLA window ordered).
    """
    conn.execute(
        "INSERT INTO properties (property_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('prop_1', 'Villa Satu', 'villa', 'Ubud', 'Bali', 'Asia/Makassar',"
        " 'SELLABLE', 'ACTIVATED', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
    )
    for unit_id, name in (("unit_1", "Unit Satu"), ("unit_2", "Unit Dua")):
        conn.execute(
            "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
            " base_capacity, max_guests, status, created_at, updated_at)"
            " VALUES (?, 'prop_1', ?, 'entire_villa', 2, 2, 2, 4, 'ACTIVE',"
            " '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')",
            (unit_id, name),
        )
    conn.execute(
        "INSERT INTO policy_versions (policy_version_id, property_id, unit_scope, version,"
        " effective_from, effective_to, currency, deposit_pct, service_fee_pct, tax_pbjt_pct,"
        " max_discount_pct, quote_valid_hours, hold_ttl_minutes, max_hold_extensions, payload,"
        " fingerprint, source_doc_ref, approved_by, created_at)"
        " VALUES ('policy_v1', 'prop_1', 'property', 1, '2026-01-01', '2027-01-01', 'IDR', '30',"
        " '10', '5', '10', 48, 60, 1, '{}', ?, 'doc.pdf', 'ops_1', '2026-09-01T00:00:00+00:00')",
        ("0" * 64,),
    )
    for booking_id, check_in in (("book_1", "2026-10-01"), ("book_2", "2026-11-01")):
        conn.execute(
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
            " status, guest_ref, adults, children, infants, currency, policy_version_id,"
            " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
            " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
            " updated_at, confirmed_at, cancelled_at)"
            " VALUES (?, 'prop_1', 'unit_1', ?, ?, 2, 'CONFIRMED', 'guest_0001', 2, 0, 0, 'IDR',"
            " 'policy_v1', ?, 'quote_1', 'hold_1', 1500000, 0, 0, 'UNPAID', ?, 1, ?, ?, ?, NULL)",
            (
                booking_id,
                check_in,
                "2026-10-03" if booking_id == "book_1" else "2026-11-03",
                "0" * 64,
                "idem_" + booking_id,
                NOW_ISO,
                NOW_ISO,
                NOW_ISO,
            ),
        )
    conn.execute(
        "INSERT INTO cases (case_id, channel, channel_ref, guest_ref, status, priority,"
        " owner_actor, booking_id, sla_policy, first_response_due_at, quote_due_at,"
        " first_responded_at, quoted_at, idempotency_key, created_at, updated_at, closed_at,"
        " close_reason)"
        " VALUES ('case_1', 'website', 'web_1', 'guest_0001', 'NEW', 'NORMAL', 'agent_01',"
        " 'book_1', 'web_v1', '2026-09-21T08:15:00+00:00', '2026-09-21T12:00:00+00:00', NULL,"
        " NULL, 'idem_case_0001', '2026-09-21T07:00:00+00:00', '2026-09-21T07:00:00+00:00',"
        " NULL, NULL)"
    )


class OpsTaskTestBase(unittest.TestCase):
    """Shared rig: in-memory SQLite + frozen clock + deterministic ids."""

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        seed_world(self.conn)
        self.clock = P.FrozenClock(P.parse_iso(START))
        self.ids = P.IdFactory(deterministic=True, seed="ops")
        self.svc = OT.OpsTaskService(self.conn, clock=self.clock, ids=self.ids)

    # -- helpers ---------------------------------------------------------
    def make(self, **overrides) -> OT.Task:
        """File a cleaning task with sane defaults; override anything."""
        kwargs = {
            "kind": OT.KIND_CLEANING,
            "assignee": "staff_014",
            "due_at": DUE,
            "idempotency_key": "idem_task_0001",
            "unit_id": "unit_1",
        }
        kwargs.update(overrides)
        return self.svc.create_task(**kwargs)

    def raw(self, task_id: str):
        return self.conn.execute(
            "SELECT * FROM ops_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

    def rows(self):
        return self.conn.execute("SELECT * FROM ops_tasks ORDER BY task_id").fetchall()

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM ops_tasks").fetchone()["n"])

    def snapshot(self, task_id: str) -> dict:
        row = self.raw(task_id)
        self.assertIsNotNone(row, f"task {task_id} is not in the table")
        return {key: row[key] for key in row.keys()}

    def freeze_row(self, task_id: str) -> tuple[int, dict]:
        """Capture everything a refused command could have changed."""
        return self.count(), self.snapshot(task_id)

    def assertFrozen(self, task_id: str, frozen: tuple[int, dict]) -> None:
        """The refused command left the row and the row count exactly as they were."""
        count, row = frozen
        self.assertEqual(self.count(), count, "a refused command changed the row count")
        self.assertEqual(
            self.snapshot(task_id), row, "a refused command rewrote the stored row"
        )

    def refuse(self, exc_type, fn, *args, **kwargs):
        """Run a command that must be refused; return the raised error."""
        with self.assertRaises(exc_type) as raised:
            fn(*args, **kwargs)
        return raised.exception

    def refuse_creation(self, exc_type, **kwargs):
        """A refused ``create_task``/``ensure_task`` must add no row at all."""
        before = self.count()
        error = self.refuse(exc_type, self.svc.create_task, **kwargs)
        self.assertEqual(self.count(), before, "a refused create wrote a row")
        return error

    def checklist_of(self, task_id: str) -> list:
        return json.loads(self.raw(task_id)["checklist"])

    def make_queue(self):
        """Four tasks spanning statuses/kinds/priorities/refs and distinct due instants."""
        clean = self.make(
            kind=OT.KIND_CLEANING,
            unit_id="unit_1",
            due_at="2026-09-22T06:00:00Z",
            idempotency_key="idem_set_0001",
            assignee="staff_014",
        )
        repair = self.make(
            kind=OT.KIND_MAINTENANCE,
            unit_id="unit_2",
            due_at="2026-09-22T09:00:00Z",
            priority=OT.PRIORITY_URGENT,
            idempotency_key="idem_set_0002",
            assignee="staff_015",
        )
        prep = self.make(
            kind=OT.KIND_CHECKIN_PREP,
            booking_id="book_1",
            unit_id=None,
            due_at="2026-09-23T06:00:00Z",
            idempotency_key="idem_set_0003",
            assignee="staff_014",
        )
        follow = self.make(
            kind=OT.KIND_GUEST_FOLLOWUP,
            case_id="case_1",
            unit_id=None,
            due_at="2026-09-24T06:00:00Z",
            idempotency_key="idem_set_0004",
            assignee="staff_016",
        )
        self.svc.start(repair.task_id)
        self.svc.complete(repair.task_id)
        self.svc.cancel(follow.task_id)
        return clean, repair, prep, follow

    def ids_of(self, tasks) -> list[str]:
        return [task.task_id for task in tasks]


class TestCreateTaskStorage(OpsTaskTestBase):
    """Invariant 1: the row is the record, and it holds what was commanded."""

    def test_create_task_persists_every_declared_column(self):
        task = self.make(
            priority=OT.PRIORITY_HIGH,
            checklist=["Cuci linen", "Keringkan"],
            booking_id="book_1",
            case_id="case_1",
        )
        row = self.raw(task.task_id)
        self.assertEqual(task.task_id, "task_ops000001")
        self.assertEqual(row["task_id"], "task_ops000001")
        self.assertEqual(row["kind"], OT.KIND_CLEANING)
        self.assertEqual(row["status"], OT.STATUS_OPEN)
        self.assertEqual(row["priority"], OT.PRIORITY_HIGH)
        self.assertEqual(row["assignee"], "staff_014")
        self.assertEqual(row["due_at"], DUE_ISO)
        self.assertEqual(row["unit_id"], "unit_1")
        self.assertEqual(row["booking_id"], "book_1")
        self.assertEqual(row["case_id"], "case_1")
        self.assertEqual(row["idempotency_key"], "idem_task_0001")
        self.assertEqual(row["created_at"], NOW_ISO)
        self.assertEqual(row["updated_at"], NOW_ISO)
        self.assertIsNone(row["completed_at"])
        self.assertIsNone(row["blocked_reason"])
        self.assertEqual(self.count(), 1)

    def test_created_task_projection_matches_the_stored_row(self):
        task = self.make(checklist=["Cuci linen"], priority=OT.PRIORITY_LOW)
        row = self.raw(task.task_id)
        self.assertEqual(task.kind, row["kind"])
        self.assertEqual(task.status, row["status"])
        self.assertEqual(task.priority, row["priority"])
        self.assertEqual(task.assignee, row["assignee"])
        self.assertEqual(task.due_at, row["due_at"])
        self.assertEqual(task.idempotency_key, row["idempotency_key"])
        self.assertEqual(task.created_at, row["created_at"])
        self.assertEqual(task.updated_at, row["updated_at"])
        self.assertEqual([item.label for item in task.checklist], ["Cuci linen"])
        self.assertEqual(task.subject_refs, (("unit_id", "unit_1"),))

    def test_default_priority_is_normal_and_status_is_open(self):
        task = self.make()
        row = self.raw(task.task_id)
        self.assertEqual(task.priority, OT.DEFAULT_PRIORITY)
        self.assertEqual(row["priority"], "NORMAL")
        self.assertEqual(row["status"], OT.STATUS_OPEN)
        self.assertTrue(task.is_active)
        self.assertFalse(task.is_closed)
        self.assertFalse(task.is_blocked)

    def test_checklist_defaults_to_an_empty_json_array(self):
        task = self.make()
        self.assertEqual(self.raw(task.task_id)["checklist"], "[]")
        self.assertEqual(task.checklist, ())
        self.assertEqual(task.progress, (0, 0))
        self.assertEqual(task.open_steps, ())
        self.assertTrue(task.checklist_complete)

    def test_checklist_steps_are_stored_open_with_the_documented_shape(self):
        task = self.make(checklist=["Cuci linen", "Keringkan", "Set ulang"])
        self.assertEqual(
            self.checklist_of(task.task_id),
            [
                {"label": "Cuci linen", "done": False, "done_at": None},
                {"label": "Keringkan", "done": False, "done_at": None},
                {"label": "Set ulang", "done": False, "done_at": None},
            ],
        )

    def test_explicit_created_at_backdates_both_stamps(self):
        back = "2026-09-20T00:00:00Z"
        task = self.make(created_at=back)
        row = self.raw(task.task_id)
        self.assertEqual(row["created_at"], P.to_iso(P.parse_iso(back)))
        self.assertEqual(row["updated_at"], row["created_at"])
        self.assertEqual(task.created_at, P.to_iso(P.parse_iso(back)))

    def test_any_of_subject_rule_accepts_a_booking_or_a_unit(self):
        by_unit = self.make(kind=OT.KIND_CLEANING, unit_id="unit_2", booking_id=None)
        by_booking = self.make(
            kind=OT.KIND_CLEANING,
            unit_id=None,
            booking_id="book_2",
            idempotency_key="idem_task_0002",
        )
        self.assertEqual(self.raw(by_unit.task_id)["unit_id"], "unit_2")
        self.assertIsNone(self.raw(by_unit.task_id)["booking_id"])
        self.assertEqual(self.raw(by_booking.task_id)["booking_id"], "book_2")
        self.assertIsNone(self.raw(by_booking.task_id)["unit_id"])

    def test_assignee_due_at_and_key_are_normalised_before_storage(self):
        task = self.svc.create_task(
            kind=OT.KIND_CLEANING,
            assignee="  staff_014  ",
            due_at=P.parse_iso(DUE),
            idempotency_key="idem_task_0009",
            unit_id="unit_1",
        )
        row = self.raw(task.task_id)
        self.assertEqual(row["assignee"], "staff_014")
        self.assertEqual(row["due_at"], DUE_ISO)
        self.assertEqual(row["idempotency_key"], "idem_task_0009")


class TestCreateTaskValidation(OpsTaskTestBase):
    """Invariant 6: validation happens before any row is written."""

    def test_short_idempotency_key_is_refused_before_any_write(self):
        error = self.refuse_creation(
            OT.InvalidTaskField,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem7c",
            unit_id="unit_1",
        )
        self.assertIsInstance(error, OT.OpsTaskError)
        self.assertEqual(error.code, "OPS_TASK_IDEMPOTENCY_KEY_INVALID")
        self.assertEqual(self.count(), 0)

    def test_idempotency_key_of_exactly_eight_characters_is_accepted(self):
        task = self.make(idempotency_key="idem8chr")
        self.assertEqual(self.raw(task.task_id)["idempotency_key"], "idem8chr")

    def test_idempotency_key_longer_than_the_maximum_is_refused(self):
        self.refuse_creation(
            OT.InvalidTaskField,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="k" * 129,
            unit_id="unit_1",
        )
        self.assertEqual(self.count(), 0)

    def test_idempotency_key_with_whitespace_is_refused(self):
        error = self.refuse_creation(
            OT.InvalidTaskField,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem task 0001",
            unit_id="unit_1",
        )
        self.assertEqual(error.code, "OPS_TASK_IDEMPOTENCY_KEY_INVALID")
        self.assertEqual(self.count(), 0)

    def test_unknown_kind_is_refused(self):
        error = self.refuse_creation(
            OT.InvalidTaskKind,
            kind="IRRIGATION",
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0011",
            unit_id="unit_1",
        )
        self.assertEqual(error.code, "OPS_TASK_KIND_INVALID")
        self.assertEqual(self.count(), 0)

    def test_unknown_priority_is_refused(self):
        error = self.refuse_creation(
            OT.InvalidTaskPriority,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0012",
            unit_id="unit_1",
            priority="SOMEDAY",
        )
        self.assertEqual(error.code, "OPS_TASK_PRIORITY_INVALID")
        self.assertEqual(self.count(), 0)

    def test_missing_subject_reference_is_refused(self):
        error = self.refuse_creation(
            OT.InvalidTaskField,
            kind=OT.KIND_CHECKIN_PREP,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0013",
        )
        self.assertEqual(error.code, "OPS_TASK_SUBJECT_MISSING")
        self.assertEqual(self.count(), 0)

    def test_blank_or_unusable_assignee_is_refused(self):
        for assignee in ("", "   ", "ab", "staff 014", "s" * 65, None, 7):
            with self.subTest(assignee=assignee):
                before = self.count()
                self.refuse(
                    (OT.InvalidTaskField, P.DomainError),
                    self.svc.create_task,
                    kind=OT.KIND_CLEANING,
                    assignee=assignee,
                    due_at=DUE,
                    idempotency_key="idem_task_0014",
                    unit_id="unit_1",
                )
                self.assertEqual(self.count(), before)

    def test_due_at_without_a_timezone_is_refused(self):
        for due_at in ("2026-09-22T08:00:00", datetime(2026, 9, 22, 8, 0, 0)):
            with self.subTest(due_at=due_at):
                before = self.count()
                error = self.refuse(
                    OT.InvalidTaskField,
                    self.svc.create_task,
                    kind=OT.KIND_CLEANING,
                    assignee="staff_014",
                    due_at=due_at,
                    idempotency_key="idem_task_0015",
                    unit_id="unit_1",
                )
                self.assertEqual(error.code, "OPS_TASK_DUE_AT_INVALID")
                self.assertEqual(self.count(), before)

    def test_checklist_that_is_not_a_sequence_is_refused(self):
        for checklist in ("Cuci linen", {"label": "Cuci linen"}, 42):
            with self.subTest(checklist=checklist):
                before = self.count()
                error = self.refuse(
                    OT.InvalidChecklist,
                    self.svc.create_task,
                    kind=OT.KIND_CLEANING,
                    assignee="staff_014",
                    due_at=DUE,
                    idempotency_key="idem_task_0016",
                    unit_id="unit_1",
                    checklist=checklist,
                )
                self.assertEqual(error.code, "OPS_TASK_CHECKLIST_INVALID")
                self.assertEqual(self.count(), before)


class TestIdempotencyKeySemantics(OpsTaskTestBase):
    """Invariant 3: scheduled jobs do not duplicate tasks."""

    def test_replayed_key_is_refused_and_writes_no_second_row(self):
        first = self.make()
        frozen = self.freeze_row(first.task_id)
        error = self.refuse(
            OT.DuplicateIdempotencyKey,
            self.svc.create_task,
            kind=OT.KIND_CLEANING,
            assignee="staff_099",
            due_at=LATER,
            idempotency_key="idem_task_0001",
            unit_id="unit_2",
        )
        self.assertEqual(error.code, "OPS_TASK_DUPLICATE_IDEMPOTENCY_KEY")
        self.assertFrozen(first.task_id, frozen)

    def test_replayed_key_reports_the_row_it_collided_with(self):
        first = self.make()
        error = self.refuse(
            OT.DuplicateIdempotencyKey,
            self.svc.create_task,
            kind=OT.KIND_CLEANING,
            assignee="staff_099",
            due_at=LATER,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
        )
        self.assertEqual(error.details["task_id"], first.task_id)

    def test_ensure_task_with_the_same_slot_returns_the_stored_task(self):
        first, created = self.svc.ensure_task(
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
        )
        self.assertTrue(created)
        frozen = self.freeze_row(first.task_id)
        again, created_again = self.svc.ensure_task(
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
        )
        self.assertFalse(created_again)
        self.assertEqual(again.task_id, first.task_id)
        self.assertFrozen(first.task_id, frozen)

    def test_ensure_task_tolerates_operator_drift_on_the_stored_slot(self):
        first, _ = self.svc.ensure_task(
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
        )
        self.svc.assign(first.task_id, "staff_020")
        self.svc.set_priority(first.task_id, OT.PRIORITY_URGENT)
        self.svc.set_checklist(first.task_id, ["Cuci linen"])
        frozen = self.freeze_row(first.task_id)
        again, created = self.svc.ensure_task(
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
            priority=OT.PRIORITY_NORMAL,
            checklist=["Cuci linen", "Keringkan"],
        )
        self.assertFalse(created)
        self.assertEqual(again.task_id, first.task_id)
        self.assertFrozen(first.task_id, frozen)

    def test_ensure_task_with_a_different_slot_is_refused(self):
        first, _ = self.svc.ensure_task(
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
        )
        frozen = self.freeze_row(first.task_id)
        error = self.refuse(
            OT.DuplicateIdempotencyKey,
            self.svc.ensure_task,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=LATER,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
        )
        self.assertEqual(error.code, "OPS_TASK_DUPLICATE_IDEMPOTENCY_KEY")
        self.assertEqual(self.raw(first.task_id)["due_at"], DUE_ISO)
        self.assertFrozen(first.task_id, frozen)

    def test_ensure_task_with_a_different_kind_on_the_same_key_is_refused(self):
        first, _ = self.svc.ensure_task(
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
        )
        frozen = self.freeze_row(first.task_id)
        self.refuse(
            OT.DuplicateIdempotencyKey,
            self.svc.ensure_task,
            kind=OT.KIND_MAINTENANCE,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
        )
        self.assertFrozen(first.task_id, frozen)

    def test_ensure_task_with_a_different_reference_on_the_same_key_is_refused(self):
        first, _ = self.svc.ensure_task(
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0001",
            unit_id="unit_1",
        )
        frozen = self.freeze_row(first.task_id)
        self.refuse(
            OT.DuplicateIdempotencyKey,
            self.svc.ensure_task,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0001",
            unit_id="unit_2",
        )
        self.assertFrozen(first.task_id, frozen)

    def test_ensure_task_short_key_writes_nothing(self):
        before = self.count()
        self.refuse(
            OT.InvalidTaskField,
            self.svc.ensure_task,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="shorty",
            unit_id="unit_1",
        )
        self.assertEqual(self.count(), before)

    def test_find_by_idempotency_key_agrees_with_the_stored_rows(self):
        task = self.make()
        found = self.svc.find_by_idempotency_key("idem_task_0001")
        self.assertIsNotNone(found)
        self.assertEqual(found.task_id, task.task_id)
        self.assertIsNone(self.svc.find_by_idempotency_key("idem_task_9999"))


class TestDeadReferences(OpsTaskTestBase):
    """Invariant 2: the referenced row must exist, checked before the INSERT."""

    def test_unknown_booking_reference_is_refused_without_a_row(self):
        error = self.refuse_creation(
            OT.TaskRefNotFound,
            kind=OT.KIND_CHECKIN_PREP,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0021",
            booking_id="book_tidak_ada",
        )
        self.assertEqual(error.code, "OPS_TASK_REF_NOT_FOUND")
        self.assertEqual(error.details["field"], "booking_id")
        self.assertEqual(error.details["ref"], "book_tidak_ada")
        self.assertEqual(self.count(), 0)

    def test_unknown_unit_reference_is_refused_without_a_row(self):
        error = self.refuse_creation(
            OT.TaskRefNotFound,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0022",
            unit_id="unit_tidak_ada",
        )
        self.assertEqual(error.details["field"], "unit_id")
        self.assertEqual(self.count(), 0)

    def test_unknown_case_reference_is_refused_without_a_row(self):
        error = self.refuse_creation(
            OT.TaskRefNotFound,
            kind=OT.KIND_GUEST_FOLLOWUP,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0023",
            case_id="case_tidak_ada",
        )
        self.assertEqual(error.details["field"], "case_id")
        self.assertEqual(self.count(), 0)

    def test_dead_reference_leaves_the_connection_usable(self):
        self.refuse_creation(
            OT.TaskRefNotFound,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0024",
            unit_id="unit_tidak_ada",
        )
        task = self.make(idempotency_key="idem_task_0025")
        self.assertEqual(self.count(), 1)
        self.assertIsNotNone(self.raw(task.task_id))

    def test_ensure_task_with_a_dead_reference_writes_nothing(self):
        before = self.count()
        self.refuse(
            OT.TaskRefNotFound,
            self.svc.ensure_task,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0026",
            unit_id="unit_tidak_ada",
        )
        self.assertEqual(self.count(), before)

    def test_link_case_to_an_unknown_case_is_refused_and_the_row_is_frozen(self):
        task = self.make()
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(OT.TaskRefNotFound, self.svc.link_case, task.task_id, "case_x")
        self.assertEqual(error.code, "OPS_TASK_REF_NOT_FOUND")
        self.assertFrozen(task.task_id, frozen)

    def test_refusal_is_a_domain_error_not_a_raw_integrity_error(self):
        with self.assertRaises(OT.OpsTaskError) as raised:
            self.svc.create_task(
                kind=OT.KIND_CLEANING,
                assignee="staff_014",
                due_at=DUE,
                idempotency_key="idem_task_0027",
                unit_id="unit_tidak_ada",
            )
        self.assertIsInstance(raised.exception, OT.TaskRefNotFound)
        self.assertIsInstance(raised.exception, P.DomainError)
        self.assertNotIsInstance(raised.exception, sqlite3.Error)

    def test_the_ddl_foreign_key_backs_the_precheck(self):
        """The module's pre-check is a better error, not a replacement for the FK."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO ops_tasks (task_id, kind, unit_id, due_at, priority, assignee,"
                " status, checklist, idempotency_key, created_at, updated_at, completed_at,"
                " blocked_reason)"
                " VALUES ('task_raw', 'CLEANING', 'unit_tidak_ada', ?, 'NORMAL', 'staff_014',"
                " 'OPEN', '[]', 'idem_task_0028', ?, ?, NULL, NULL)",
                (DUE_ISO, NOW_ISO, NOW_ISO),
            )
        self.assertEqual(self.count(), 0)


class TestStatusGraph(OpsTaskTestBase):
    """Invariant 4: ``status`` moves only along ``TASK_TRANSITIONS``."""

    def test_open_task_starts_then_completes(self):
        task = self.make()
        self.clock.advance(seconds=60)
        started = self.svc.start(task.task_id)
        self.assertEqual(started.status, OT.STATUS_IN_PROGRESS)
        self.assertEqual(
            self.raw(task.task_id)["updated_at"], P.to_iso(self.clock.now())
        )
        self.clock.advance(seconds=60)
        done = self.svc.complete(task.task_id)
        self.assertEqual(done.status, OT.STATUS_DONE)
        row = self.raw(task.task_id)
        self.assertEqual(row["completed_at"], P.to_iso(self.clock.now()))
        self.assertIsNone(row["blocked_reason"])
        self.assertTrue(done.is_closed)

    def test_start_on_an_in_progress_task_is_refused(self):
        task = self.make()
        self.svc.start(task.task_id)
        frozen = self.freeze_row(task.task_id)
        self.clock.advance(seconds=30)
        error = self.refuse(OT.InvalidTaskTransition, self.svc.start, task.task_id)
        self.assertEqual(error.code, "OPS_TASK_TRANSITION_INVALID")
        self.assertFrozen(task.task_id, frozen)

    def test_transition_to_the_current_status_is_refused(self):
        task = self.make()
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(
            OT.InvalidTaskTransition, self.svc.transition, task.task_id, OT.STATUS_OPEN
        )
        self.assertEqual(error.code, "OPS_TASK_STATUS_UNCHANGED")
        self.assertFrozen(task.task_id, frozen)

    def test_transition_to_an_unknown_status_is_refused(self):
        task = self.make()
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(
            P.DomainError, self.svc.transition, task.task_id, "FINISHED"
        )
        self.assertEqual(error.code, "OPS_TASK_STATUS_INVALID")
        self.assertEqual(self.raw(task.task_id)["status"], OT.STATUS_OPEN)
        self.assertFrozen(task.task_id, frozen)

    def test_blocking_a_task_outside_the_graph_is_refused(self):
        task = self.make()
        self.svc.start(task.task_id)
        self.svc.block(task.task_id, "menunggu tukang")
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(
            OT.InvalidTaskTransition, self.svc.transition, task.task_id, OT.STATUS_DONE
        )
        self.assertEqual(error.code, "OPS_TASK_TRANSITION_INVALID")
        self.assertEqual(error.details["allowed"], list(OT.TASK_TRANSITIONS[OT.STATUS_BLOCKED]))
        self.assertFrozen(task.task_id, frozen)

    def test_blocking_requires_a_reason_of_at_least_three_characters(self):
        task = self.make()
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(
            OT.InvalidBlockedReason, self.svc.block, task.task_id, "ab"
        )
        self.assertEqual(error.code, "OPS_TASK_BLOCKED_REASON_INVALID")
        self.assertEqual(self.raw(task.task_id)["status"], OT.STATUS_OPEN)
        self.assertFrozen(task.task_id, frozen)

    def test_blank_blocking_reason_is_refused(self):
        task = self.make()
        frozen = self.freeze_row(task.task_id)
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                self.refuse(
                    (OT.InvalidBlockedReason, P.DomainError),
                    self.svc.block,
                    task.task_id,
                    reason,
                )
                self.assertFrozen(task.task_id, frozen)

    def test_block_stores_the_reason_and_keeps_completed_at_null(self):
        task = self.make()
        blocked = self.svc.block(task.task_id, "vendor belum datang")
        self.assertEqual(blocked.status, OT.STATUS_BLOCKED)
        self.assertEqual(blocked.blocked_reason, "vendor belum datang")
        row = self.raw(task.task_id)
        self.assertEqual(row["blocked_reason"], "vendor belum datang")
        self.assertIsNone(row["completed_at"])
        self.assertTrue(blocked.is_blocked)

    def test_blocking_an_already_blocked_task_is_refused(self):
        task = self.make()
        self.svc.block(task.task_id, "ban bocor")
        frozen = self.freeze_row(task.task_id)
        self.refuse(OT.InvalidTaskTransition, self.svc.block, task.task_id, "ban bocor lagi")
        self.assertFrozen(task.task_id, frozen)

    def test_unblock_returns_the_task_to_in_progress_and_clears_the_reason(self):
        task = self.make()
        self.svc.start(task.task_id)
        self.svc.block(task.task_id, "menunggu spare part")
        resumed = self.svc.unblock(task.task_id)
        self.assertEqual(resumed.status, OT.STATUS_IN_PROGRESS)
        self.assertIsNone(self.raw(task.task_id)["blocked_reason"])
        self.assertIsNone(resumed.blocked_reason)

    def test_unblock_can_return_the_task_to_the_open_queue(self):
        task = self.make()
        self.svc.block(task.task_id, "menunggu konfirmasi")
        reopened = self.svc.unblock(task.task_id, resume=False)
        self.assertEqual(reopened.status, OT.STATUS_OPEN)
        self.assertIsNone(self.raw(task.task_id)["blocked_reason"])

    def test_unblock_on_a_task_that_is_not_blocked_is_refused(self):
        task = self.make()
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(OT.TaskNotBlocked, self.svc.unblock, task.task_id)
        self.assertEqual(error.code, "OPS_TASK_NOT_BLOCKED")
        self.assertFrozen(task.task_id, frozen)

    def test_done_is_terminal(self):
        task = self.make()
        self.svc.start(task.task_id)
        self.svc.complete(task.task_id)
        frozen = self.freeze_row(task.task_id)
        self.clock.advance(seconds=60)
        for target in (OT.STATUS_OPEN, OT.STATUS_IN_PROGRESS, OT.STATUS_BLOCKED, OT.STATUS_CANCELLED):
            with self.subTest(target=target):
                error = self.refuse(
                    OT.TaskClosed, self.svc.transition, task.task_id, target
                )
                self.assertEqual(error.code, "OPS_TASK_CLOSED")
                self.assertFrozen(task.task_id, frozen)

    def test_cancelled_is_terminal(self):
        task = self.make()
        self.svc.cancel(task.task_id)
        frozen = self.freeze_row(task.task_id)
        for target in (OT.STATUS_OPEN, OT.STATUS_IN_PROGRESS, OT.STATUS_DONE):
            with self.subTest(target=target):
                self.refuse(OT.TaskClosed, self.svc.transition, task.task_id, target)
                self.assertFrozen(task.task_id, frozen)
        self.refuse(OT.TaskClosed, self.svc.complete, task.task_id)
        self.refuse(OT.TaskClosed, self.svc.cancel, task.task_id)
        # The convenience wrappers refuse the move with their own error class
        # (``start`` reports an illegal transition, ``unblock`` reports "not blocked"
        # rather than TaskClosed); what matters here is that the row never moves.
        self.refuse(OT.InvalidTaskTransition, self.svc.start, task.task_id)
        self.refuse(OT.TaskNotBlocked, self.svc.unblock, task.task_id)
        self.assertFrozen(task.task_id, frozen)

    def test_cancel_stamps_completed_at_and_closes_the_row(self):
        task = self.make()
        self.clock.advance(seconds=120)
        cancelled = self.svc.cancel(task.task_id)
        row = self.raw(task.task_id)
        self.assertEqual(cancelled.status, OT.STATUS_CANCELLED)
        self.assertEqual(row["status"], "CANCELLED")
        self.assertEqual(row["completed_at"], P.to_iso(self.clock.now()))
        self.assertEqual(row["updated_at"], P.to_iso(self.clock.now()))

    def test_blocked_reason_is_cleared_when_the_work_resumes_and_finishes(self):
        task = self.make()
        self.svc.start(task.task_id)
        self.svc.block(task.task_id, "hujan deras")
        self.svc.unblock(task.task_id)
        done = self.svc.complete(task.task_id)
        self.assertEqual(done.status, OT.STATUS_DONE)
        self.assertIsNone(done.blocked_reason)
        self.assertIsNone(self.raw(task.task_id)["blocked_reason"])

    def test_field_commands_refuse_a_closed_task(self):
        task = self.make()
        self.svc.start(task.task_id)
        self.svc.complete(task.task_id)
        frozen = self.freeze_row(task.task_id)
        self.clock.advance(seconds=60)
        self.refuse(OT.TaskClosed, self.svc.assign, task.task_id, "staff_099")
        self.refuse(OT.TaskClosed, self.svc.reschedule, task.task_id, LATER)
        self.refuse(OT.TaskClosed, self.svc.set_priority, task.task_id, OT.PRIORITY_URGENT)
        self.refuse(OT.TaskClosed, self.svc.link_case, task.task_id, "case_1")
        self.refuse(OT.TaskClosed, self.svc.add_checklist_item, task.task_id, "Set ulang")
        self.refuse(OT.TaskClosed, self.svc.mark_checklist_item, task.task_id, "Cuci linen")
        self.refuse(OT.TaskClosed, self.svc.set_checklist, task.task_id, ["Lain"])
        self.assertFrozen(task.task_id, frozen)

    def test_assign_reschedule_and_set_priority_move_the_row_forward(self):
        task = self.make()
        column_before = self.raw(task.task_id)["updated_at"]
        self.clock.advance(seconds=60)
        assigned = self.svc.assign(task.task_id, "staff_099")
        self.assertEqual(assigned.assignee, "staff_099")
        self.clock.advance(seconds=60)
        moved = self.svc.reschedule(task.task_id, LATER)
        self.assertEqual(moved.due_at, P.to_iso(P.parse_iso(LATER)))
        self.clock.advance(seconds=60)
        ranked = self.svc.set_priority(task.task_id, OT.PRIORITY_URGENT)
        self.assertEqual(ranked.priority, OT.PRIORITY_URGENT)
        row = self.raw(task.task_id)
        self.assertEqual(row["due_at"], P.to_iso(P.parse_iso(LATER)))
        self.assertEqual(row["priority"], "URGENT")
        self.assertEqual(row["updated_at"], P.to_iso(self.clock.now()))
        self.assertNotEqual(row["updated_at"], column_before)
        self.assertEqual(row["status"], OT.STATUS_OPEN)

    def test_assign_to_the_same_owner_is_refused(self):
        task = self.make()
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(OT.TaskFieldUnchanged, self.svc.assign, task.task_id, "staff_014")
        self.assertEqual(error.code, "OPS_TASK_ASSIGNEE_UNCHANGED")
        self.assertFrozen(task.task_id, frozen)

    def test_reschedule_to_the_same_instant_is_refused(self):
        task = self.make()
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(OT.TaskFieldUnchanged, self.svc.reschedule, task.task_id, DUE_ISO)
        self.assertEqual(error.code, "OPS_TASK_DUE_AT_UNCHANGED")
        self.assertFrozen(task.task_id, frozen)

    def test_set_priority_to_the_same_rank_is_refused(self):
        task = self.make()
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(
            OT.TaskFieldUnchanged, self.svc.set_priority, task.task_id, OT.PRIORITY_NORMAL
        )
        self.assertEqual(error.code, "OPS_TASK_PRIORITY_UNCHANGED")
        self.assertFrozen(task.task_id, frozen)

    def test_link_case_sets_the_reference_once(self):
        task = self.make()
        self.clock.advance(seconds=60)
        linked = self.svc.link_case(task.task_id, "case_1")
        self.assertEqual(linked.case_id, "case_1")
        self.assertEqual(self.raw(task.task_id)["case_id"], "case_1")
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(OT.TaskFieldUnchanged, self.svc.link_case, task.task_id, "case_1")
        self.assertEqual(error.code, "OPS_TASK_CASE_UNCHANGED")
        self.assertFrozen(task.task_id, frozen)

    def test_task_helpers_agree_with_the_stored_status(self):
        task = self.make()
        self.assertEqual(task.allowed_transitions, tuple(OT.TASK_TRANSITIONS[OT.STATUS_OPEN]))
        self.svc.start(task.task_id)
        started = self.svc.get(task.task_id)
        self.assertEqual(started.allowed_transitions, tuple(OT.TASK_TRANSITIONS[OT.STATUS_IN_PROGRESS]))
        self.svc.block(task.task_id, "tunggu material")
        blocked = self.svc.get(task.task_id)
        self.assertTrue(blocked.is_blocked)
        self.assertTrue(blocked.is_active)
        self.svc.cancel(task.task_id)
        closed = self.svc.get(task.task_id)
        self.assertTrue(closed.is_closed)
        self.assertEqual(closed.allowed_transitions, ())

    def test_unknown_task_id_is_reported_as_not_found(self):
        error = self.refuse(OT.TaskNotFound, self.svc.get, "task_tidak_ada")
        self.assertEqual(error.code, "OPS_TASK_NOT_FOUND")
        self.assertEqual(self.count(), 0)


class TestCompletion(OpsTaskTestBase):
    """Invariant 5: ``DONE`` means the runbook ran."""

    def test_complete_with_an_open_step_is_refused(self):
        task = self.make(checklist=["Cuci linen", "Keringkan"])
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(OT.ChecklistIncomplete, self.svc.complete, task.task_id)
        self.assertEqual(error.code, "OPS_TASK_CHECKLIST_INCOMPLETE")
        self.assertEqual(error.details["open_steps"], ["Cuci linen", "Keringkan"])
        self.assertFrozen(task.task_id, frozen)

    def test_transition_to_done_enforces_the_runbook_too(self):
        task = self.make(checklist=["Cuci linen"])
        frozen = self.freeze_row(task.task_id)
        self.refuse(
            OT.ChecklistIncomplete, self.svc.transition, task.task_id, OT.STATUS_DONE
        )
        self.assertFrozen(task.task_id, frozen)

    def test_complete_is_accepted_once_every_step_is_done(self):
        task = self.make(checklist=["Cuci linen", "Keringkan"])
        self.svc.mark_checklist_item(task.task_id, "Cuci linen")
        self.clock.advance(seconds=60)
        self.svc.mark_checklist_item(task.task_id, "Keringkan")
        self.clock.advance(seconds=60)
        done = self.svc.complete(task.task_id)
        self.assertEqual(done.status, OT.STATUS_DONE)
        self.assertTrue(done.checklist_complete)
        self.assertEqual(done.progress, (2, 2))
        row = self.raw(task.task_id)
        self.assertEqual(row["completed_at"], P.to_iso(self.clock.now()))

    def test_an_extra_step_blocks_the_next_completion_attempt(self):
        task = self.make(checklist=["Cuci linen"])
        self.svc.mark_checklist_item(task.task_id, "Cuci linen")
        self.svc.add_checklist_item(task.task_id, "Keringkan")
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(OT.ChecklistIncomplete, self.svc.complete, task.task_id)
        self.assertEqual(error.details["open_steps"], ["Keringkan"])
        self.assertFrozen(task.task_id, frozen)

    def test_a_completed_task_never_grows_a_step(self):
        task = self.make(checklist=["Cuci linen"])
        self.svc.mark_checklist_item(task.task_id, "Cuci linen")
        crossed = self.svc.complete(task.task_id)
        self.assertEqual(crossed.status, OT.STATUS_DONE)
        frozen = self.freeze_row(task.task_id)
        self.refuse(OT.TaskClosed, self.svc.add_checklist_item, task.task_id, "Keringkan")
        self.assertFrozen(task.task_id, frozen)

    def test_reopening_a_step_blocks_completion_again(self):
        task = self.make(checklist=["Cuci linen"])
        self.svc.mark_checklist_item(task.task_id, "Cuci linen")
        self.svc.mark_checklist_item(task.task_id, "Cuci linen", done=False)
        frozen = self.freeze_row(task.task_id)
        self.refuse(OT.ChecklistIncomplete, self.svc.complete, task.task_id)
        self.assertFrozen(task.task_id, frozen)

    def test_cancel_is_allowed_while_the_runbook_is_open(self):
        task = self.make(checklist=["Cuci linen", "Keringkan"])
        cancelled = self.svc.cancel(task.task_id)
        self.assertEqual(cancelled.status, OT.STATUS_CANCELLED)
        self.assertEqual(cancelled.progress, (0, 2))
        row = self.raw(task.task_id)
        self.assertEqual(row["status"], "CANCELLED")
        self.assertIsNotNone(row["completed_at"])


class TestChecklistValidation(OpsTaskTestBase):
    """Invariant 5: the runbook itself is bounded and unambiguous."""

    def test_more_than_fifty_steps_is_refused(self):
        labels = [f"Langkah {index:02d}" for index in range(OT.MAX_CHECKLIST_ITEMS + 1)]
        before = self.count()
        error = self.refuse(
            OT.InvalidChecklist,
            self.svc.create_task,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0031",
            unit_id="unit_1",
            checklist=labels,
        )
        self.assertEqual(error.code, "OPS_TASK_CHECKLIST_TOO_LONG")
        self.assertEqual(self.count(), before)

    def test_exactly_fifty_steps_is_accepted(self):
        labels = [f"Langkah {index:02d}" for index in range(OT.MAX_CHECKLIST_ITEMS)]
        task = self.make(checklist=labels, idempotency_key="idem_task_0032")
        self.assertEqual(len(task.checklist), OT.MAX_CHECKLIST_ITEMS)
        self.assertEqual(task.progress, (0, OT.MAX_CHECKLIST_ITEMS))

    def test_adding_a_step_to_a_full_runbook_is_refused(self):
        labels = [f"Langkah {index:02d}" for index in range(OT.MAX_CHECKLIST_ITEMS)]
        task = self.make(checklist=labels, idempotency_key="idem_task_0033")
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(
            OT.InvalidChecklist, self.svc.add_checklist_item, task.task_id, "Langkah 51"
        )
        self.assertEqual(error.code, "OPS_TASK_CHECKLIST_TOO_LONG")
        self.assertFrozen(task.task_id, frozen)

    def test_blank_label_is_refused(self):
        for label in ("", "   "):
            with self.subTest(label=label):
                before = self.count()
                error = self.refuse(
                    (OT.InvalidChecklist, P.DomainError),
                    self.svc.create_task,
                    kind=OT.KIND_CLEANING,
                    assignee="staff_014",
                    due_at=DUE,
                    idempotency_key="idem_task_0034",
                    unit_id="unit_1",
                    checklist=[label],
                )
                self.assertEqual(error.code, "OPS_TASK_CHECKLIST_LABEL_INVALID")
                self.assertEqual(self.count(), before)

    def test_a_step_that_is_not_a_label_is_refused(self):
        for label in (None, 42, ["nested"]):
            with self.subTest(label=label):
                before = self.count()
                error = self.refuse(
                    OT.InvalidChecklist,
                    self.svc.create_task,
                    kind=OT.KIND_CLEANING,
                    assignee="staff_014",
                    due_at=DUE,
                    idempotency_key="idem_task_0038",
                    unit_id="unit_1",
                    checklist=[label],
                )
                self.assertEqual(error.code, "OPS_TASK_CHECKLIST_INVALID")
                self.assertEqual(self.count(), before)

    def test_label_longer_than_the_maximum_is_refused(self):
        label = "L" * (OT.MAX_LABEL_LEN + 1)
        before = self.count()
        error = self.refuse(
            OT.InvalidChecklist,
            self.svc.create_task,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0035",
            unit_id="unit_1",
            checklist=[label],
        )
        self.assertEqual(error.code, "OPS_TASK_CHECKLIST_LABEL_INVALID")
        self.assertEqual(self.count(), before)

    def test_label_of_exactly_the_maximum_is_accepted(self):
        label = "L" * OT.MAX_LABEL_LEN
        task = self.make(checklist=[label], idempotency_key="idem_task_0036")
        self.assertEqual(task.checklist[0].label, label)
        self.assertEqual(self.checklist_of(task.task_id)[0]["label"], label)

    def test_duplicate_labels_are_refused_at_creation(self):
        before = self.count()
        error = self.refuse(
            OT.InvalidChecklist,
            self.svc.create_task,
            kind=OT.KIND_CLEANING,
            assignee="staff_014",
            due_at=DUE,
            idempotency_key="idem_task_0037",
            unit_id="unit_1",
            checklist=["Cuci linen", "Cuci linen"],
        )
        self.assertEqual(error.code, "OPS_TASK_CHECKLIST_DUPLICATE")
        self.assertEqual(self.count(), before)

    def test_adding_a_duplicate_step_is_refused_and_the_runbook_is_frozen(self):
        task = self.make(checklist=["Cuci linen"])
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(
            OT.InvalidChecklist, self.svc.add_checklist_item, task.task_id, "Cuci linen"
        )
        self.assertEqual(error.code, "OPS_TASK_CHECKLIST_DUPLICATE")
        self.assertFrozen(task.task_id, frozen)

    def test_set_checklist_refuses_duplicates_and_keeps_the_stored_runbook(self):
        task = self.make(checklist=["Cuci linen"])
        frozen = self.freeze_row(task.task_id)
        self.refuse(
            OT.InvalidChecklist, self.svc.set_checklist, task.task_id, ["A", "B", "A"]
        )
        self.assertFrozen(task.task_id, frozen)


class TestDeadline(OpsTaskTestBase):
    """Invariant 7: strict lateness and a half-open horizon, active tasks only."""

    def test_overdue_excludes_a_task_due_exactly_now(self):
        task = self.make(due_at=START)
        self.assertEqual(self.svc.overdue(NOW), ())
        self.assertEqual(self.svc.overdue(), ())
        self.assertFalse(self.svc.get(task.task_id).is_overdue(NOW))

    def test_overdue_includes_a_task_one_second_past_due(self):
        task = self.make(due_at=START)
        self.assertEqual(self.svc.overdue(NOW), ())
        late = self.svc.overdue(P.parse_iso("2026-09-21T08:00:01Z"))
        self.assertEqual(self.ids_of(late), [task.task_id])
        self.assertEqual(self.svc.overdue(NOW.replace(second=NOW.second + 1)), self.svc.overdue(P.parse_iso("2026-09-21T08:00:01Z")))

    def test_overdue_is_strict_and_ordered_by_due_at(self):
        early = self.make(due_at="2026-09-21T07:00:00Z", idempotency_key="idem_task_0041")
        middle = self.make(due_at="2026-09-21T07:30:00Z", idempotency_key="idem_task_0042")
        boundary = self.make(due_at=START, idempotency_key="idem_task_0043")
        found = self.svc.overdue(NOW)
        self.assertEqual([item.task_id for item in found], [early.task_id, middle.task_id])
        self.assertNotIn(boundary.task_id, [item.task_id for item in found])
        self.assertTrue(self.svc.get(early.task_id).is_overdue(NOW))

    def test_overdue_ignores_closed_tasks(self):
        task = self.make(due_at="2026-09-21T07:00:00Z")
        self.svc.cancel(task.task_id)
        self.assertEqual(self.svc.overdue(NOW), ())
        self.assertEqual(self.svc.overdue(NOW, statuses=[OT.STATUS_CANCELLED]), ())

    def test_overdue_only_reads_active_tasks(self):
        active = self.make(due_at="2026-09-21T07:00:00Z")
        done = self.make(due_at="2026-09-21T06:00:00Z", idempotency_key="idem_task_0044")
        self.svc.start(done.task_id)
        self.svc.complete(done.task_id)
        self.assertEqual(
            self.ids_of(self.svc.overdue(NOW)), self.ids_of(self.svc.open_tasks(due_before=NOW))
        )
        self.assertEqual(self.ids_of(self.svc.overdue(NOW)), [active.task_id])

    def test_due_within_is_inclusive_at_now_and_exclusive_at_the_horizon(self):
        due_now = self.make(due_at=START, idempotency_key="idem_task_0045")
        inside = self.make(due_at="2026-09-22T07:59:59Z", idempotency_key="idem_task_0046")
        horizon = self.make(due_at="2026-09-22T08:00:00Z", idempotency_key="idem_task_0047")
        past = self.make(due_at="2026-09-21T07:00:00Z", idempotency_key="idem_task_0048")
        found = self.ids_of(self.svc.due_within(24, NOW))
        self.assertIn(due_now.task_id, found)
        self.assertIn(inside.task_id, found)
        self.assertNotIn(horizon.task_id, found)
        self.assertNotIn(past.task_id, found)

    def test_due_within_zero_hours_returns_nothing(self):
        self.make(due_at=START)
        self.assertEqual(self.svc.due_within(0, NOW), ())

    def test_due_within_ignores_closed_tasks(self):
        task = self.make(due_at=LATER)
        self.svc.start(task.task_id)
        self.svc.complete(task.task_id)
        self.assertEqual(self.svc.due_within(48, NOW), ())

    def test_due_within_refuses_a_negative_or_non_numeric_horizon(self):
        for hours in (-1, "24", None, True):
            with self.subTest(hours=hours):
                before = self.count()
                error = self.refuse(
                    (OT.InvalidTaskField, P.DomainError),
                    self.svc.due_within,
                    hours,
                    NOW,
                )
                self.assertEqual(getattr(error, "code", ""), "OPS_TASK_HORIZON_INVALID")
                self.assertEqual(self.count(), before)

    def test_is_overdue_agrees_with_the_service_view(self):
        task = self.make(due_at=START)
        self.assertFalse(self.svc.get(task.task_id).is_overdue(NOW))
        self.assertTrue(self.svc.get(task.task_id).is_overdue(P.parse_iso("2026-09-21T08:00:01Z")))
        late = self.ids_of(self.svc.overdue(P.parse_iso("2026-09-21T08:00:01Z")))
        self.assertEqual(late, [task.task_id])


class TestListFilters(OpsTaskTestBase):
    """Invariant 6: the queue filters return exactly the right set, in order."""

    def test_without_filters_the_whole_table_comes_back(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(
            self.ids_of(self.svc.list_tasks()),
            self.ids_of([clean, repair, prep, follow]),
        )

    def test_status_filter_returns_exactly_the_matching_rows(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(self.ids_of(self.svc.list_tasks(statuses=[OT.STATUS_OPEN])), [clean.task_id, prep.task_id])
        self.assertEqual(self.ids_of(self.svc.list_tasks(statuses=[OT.STATUS_DONE])), [repair.task_id])
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(statuses=[OT.STATUS_DONE, OT.STATUS_CANCELLED])),
            [repair.task_id, follow.task_id],
        )

    def test_empty_status_filter_returns_nothing(self):
        self.make_queue()
        self.assertEqual(self.svc.list_tasks(statuses=()), ())

    def test_kind_filter_returns_exactly_the_matching_rows(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(kinds=[OT.KIND_CLEANING, OT.KIND_MAINTENANCE])),
            [clean.task_id, repair.task_id],
        )
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(kinds=[OT.KIND_VERIFICATION])), []
        )

    def test_priority_filter_returns_exactly_the_matching_rows(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(priorities=[OT.PRIORITY_URGENT])), [repair.task_id]
        )
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(priorities=[OT.PRIORITY_NORMAL])),
            [clean.task_id, prep.task_id, follow.task_id],
        )

    def test_assignee_filter_returns_exactly_the_matching_rows(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(assignees=["staff_014"])),
            [clean.task_id, prep.task_id],
        )
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(assignees=["staff_015", "staff_016"])),
            [repair.task_id, follow.task_id],
        )

    def test_unit_filter_returns_exactly_the_matching_rows(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(self.ids_of(self.svc.list_tasks(unit_id="unit_2")), [repair.task_id])
        self.assertEqual(
            self.ids_of(self.svc.by_unit("unit_1")), [clean.task_id]
        )
        self.assertEqual(
            self.ids_of(self.svc.by_unit("unit_1", include_closed=False)), [clean.task_id]
        )

    def test_booking_filter_returns_exactly_the_matching_rows(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(self.ids_of(self.svc.list_tasks(booking_id="book_1")), [prep.task_id])
        self.assertEqual(self.ids_of(self.svc.by_booking("book_1")), [prep.task_id])
        self.assertEqual(self.ids_of(self.svc.by_booking("book_2")), [])

    def test_case_filter_returns_exactly_the_matching_rows(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(self.ids_of(self.svc.list_tasks(case_id="case_1")), [follow.task_id])
        self.assertEqual(self.ids_of(self.svc.by_case("case_1")), [follow.task_id])
        self.assertEqual(self.ids_of(self.svc.by_case("case_1", include_closed=False)), [])

    def test_due_before_is_exclusive(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(due_before="2026-09-22T09:00:00Z")),
            [clean.task_id],
        )
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(due_before="2026-09-22T09:00:01Z")),
            [clean.task_id, repair.task_id],
        )

    def test_due_after_is_inclusive(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(
            self.ids_of(self.svc.list_tasks(due_after="2026-09-22T09:00:00Z")),
            [repair.task_id, prep.task_id, follow.task_id],
        )

    def test_filters_combine_with_and(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(
            self.ids_of(
                self.svc.list_tasks(
                    statuses=OT.ACTIVE_STATUSES,
                    kinds=[OT.KIND_CLEANING, OT.KIND_CHECKIN_PREP],
                    assignees=["staff_014"],
                    unit_id="unit_1",
                )
            ),
            [clean.task_id],
        )
        self.assertEqual(
            self.ids_of(
                self.svc.list_tasks(kinds=[OT.KIND_CLEANING], priorities=[OT.PRIORITY_URGENT])
            ),
            [],
        )

    def test_queue_order_is_due_then_priority_then_id(self):
        late = self.make(due_at="2026-09-25T06:00:00Z", idempotency_key="idem_task_0051")
        low = self.make(
            due_at="2026-09-26T06:00:00Z",
            priority=OT.PRIORITY_LOW,
            idempotency_key="idem_task_0052",
        )
        urgent = self.make(
            due_at="2026-09-26T06:00:00Z",
            priority=OT.PRIORITY_URGENT,
            idempotency_key="idem_task_0053",
        )
        normal = self.make(
            due_at="2026-09-26T06:00:00Z",
            priority=OT.PRIORITY_NORMAL,
            idempotency_key="idem_task_0054",
        )
        self.assertEqual(
            self.ids_of(self.svc.list_tasks()),
            [late.task_id, urgent.task_id, normal.task_id, low.task_id],
        )

    def test_unknown_filter_values_are_refused_without_touching_rows(self):
        clean, repair, prep, follow = self.make_queue()
        frozen = self.freeze_row(clean.task_id)
        self.assertEqual(
            self.refuse(
                P.DomainError, self.svc.list_tasks, statuses=["PENDING"]
            ).code,
            "OPS_TASK_STATUS_INVALID",
        )
        self.refuse(OT.InvalidTaskKind, self.svc.list_tasks, kinds=["IRRIGATION"])
        self.refuse(OT.InvalidTaskPriority, self.svc.list_tasks, priorities=["SOMEDAY"])
        self.refuse(OT.InvalidTaskField, self.svc.list_tasks, assignees=["ab"])
        self.refuse(OT.InvalidTaskField, self.svc.list_tasks, unit_id="unit 1")
        self.refuse(P.DomainError, self.svc.list_tasks, due_before="besok")
        self.assertFrozen(clean.task_id, frozen)

    def test_open_tasks_excludes_terminal_rows(self):
        clean, repair, prep, follow = self.make_queue()
        self.assertEqual(
            self.ids_of(self.svc.open_tasks()), [clean.task_id, prep.task_id]
        )


class TestCountsAndNextDue(OpsTaskTestBase):
    """Invariant 7: the counters and the next-due view agree with the rows."""

    def test_counts_by_status_is_zero_filled_and_exact(self):
        self.make_queue()
        counts = self.svc.counts_by_status()
        self.assertEqual(list(counts), list(OT.TASK_STATUSES))
        self.assertEqual(
            counts,
            {
                OT.STATUS_OPEN: 2,
                OT.STATUS_IN_PROGRESS: 0,
                OT.STATUS_BLOCKED: 0,
                OT.STATUS_DONE: 1,
                OT.STATUS_CANCELLED: 1,
            },
        )

    def test_counts_by_status_tracks_moves_without_residue(self):
        task = self.make()
        self.svc.start(task.task_id)
        self.svc.block(task.task_id, "tunggu vendor")
        self.assertEqual(self.svc.counts_by_status()[OT.STATUS_BLOCKED], 1)
        self.assertEqual(self.svc.counts_by_status()[OT.STATUS_IN_PROGRESS], 0)
        self.svc.unblock(task.task_id)
        self.svc.complete(task.task_id)
        counts = self.svc.counts_by_status()
        self.assertEqual(counts[OT.STATUS_DONE], 1)
        self.assertEqual(counts[OT.STATUS_BLOCKED], 0)
        self.assertEqual(sum(counts.values()), self.count())

    def test_counts_by_kind_is_zero_filled_and_exact(self):
        self.make_queue()
        counts = self.svc.counts_by_kind()
        self.assertEqual(list(counts), list(OT.TASK_KINDS))
        self.assertEqual(counts[OT.KIND_CLEANING], 1)
        self.assertEqual(counts[OT.KIND_CHECKIN_PREP], 1)
        self.assertEqual(counts[OT.KIND_MAINTENANCE], 1)
        self.assertEqual(counts[OT.KIND_GUEST_FOLLOWUP], 1)
        for kind in (OT.KIND_CHECKOUT_INSPECTION, OT.KIND_PAYMENT_CHASE, OT.KIND_DOCUMENT_CHASE, OT.KIND_VERIFICATION):
            self.assertEqual(counts[kind], 0)
        self.assertEqual(sum(counts.values()), self.count())

    def test_next_due_returns_the_earliest_active_task(self):
        clean, repair, prep, follow = self.make_queue()
        first = self.svc.next_due()
        self.assertEqual(first.task_id, clean.task_id)
        self.assertEqual(first.due_at, P.to_iso(P.parse_iso("2026-09-22T06:00:00Z")))

    def test_next_due_skips_terminal_tasks_and_old_deadlines(self):
        done = self.make(due_at="2026-09-21T06:00:00Z")
        self.svc.start(done.task_id)
        self.svc.complete(done.task_id)
        open_task = self.make(due_at=LATER, idempotency_key="idem_task_0061")
        self.assertEqual(self.svc.next_due().task_id, open_task.task_id)

    def test_next_due_honours_the_assignee_filter(self):
        self.make_queue()
        self.assertIsNone(self.svc.next_due(assignee="staff_015"))
        mine = self.make(assignee="staff_015", due_at=LATER, idempotency_key="idem_task_0062")
        self.assertEqual(self.svc.next_due(assignee="staff_015").task_id, mine.task_id)

    def test_next_due_is_none_on_an_empty_queue(self):
        self.assertIsNone(self.svc.next_due())
        self.assertEqual(self.svc.counts_by_status()[OT.STATUS_OPEN], 0)


class TestChecklistProjection(OpsTaskTestBase):
    """Invariant 8: the projection properties agree with the stored runbook."""

    def test_progress_counts_done_steps(self):
        task = self.make(checklist=["A", "B", "C"])
        self.assertEqual(task.progress, (0, 3))
        self.svc.mark_checklist_item(task.task_id, "B")
        self.assertEqual(self.svc.get(task.task_id).progress, (1, 3))
        self.svc.mark_checklist_item(task.task_id, "A")
        self.assertEqual(self.svc.get(task.task_id).progress, (2, 3))

    def test_open_steps_follow_the_runbook_order(self):
        task = self.make(checklist=["A", "B", "C"])
        self.assertEqual(task.open_steps, ("A", "B", "C"))
        self.svc.mark_checklist_item(task.task_id, "B")
        self.assertEqual(self.svc.get(task.task_id).open_steps, ("A", "C"))
        self.assertFalse(self.svc.get(task.task_id).checklist_complete)

    def test_checklist_complete_is_vacuously_true_without_a_runbook(self):
        task = self.make()
        self.assertTrue(task.checklist_complete)
        self.assertEqual(task.open_steps, ())
        self.assertEqual(task.progress, (0, 0))

    def test_checklist_complete_only_after_the_last_step(self):
        task = self.make(checklist=["A", "B"])
        self.svc.mark_checklist_item(task.task_id, "A")
        self.assertFalse(self.svc.get(task.task_id).checklist_complete)
        self.svc.mark_checklist_item(task.task_id, "B")
        self.assertTrue(self.svc.get(task.task_id).checklist_complete)

    def test_projection_follows_reopening_and_replacement(self):
        task = self.make(checklist=["A", "B"])
        self.svc.mark_checklist_item(task.task_id, "A")
        self.svc.mark_checklist_item(task.task_id, "A", done=False)
        self.assertEqual(self.svc.get(task.task_id).progress, (0, 2))
        self.svc.set_checklist(task.task_id, ["C"])
        refreshed = self.svc.get(task.task_id)
        self.assertEqual(refreshed.progress, (0, 1))
        self.assertEqual(refreshed.open_steps, ("C",))


class TestChecklistCommands(OpsTaskTestBase):
    """Invariant 9: ticking is idempotent and label-exact."""

    def test_marking_a_step_twice_is_refused_and_does_not_duplicate_it(self):
        task = self.make(checklist=["Cuci linen", "Keringkan"])
        first = self.svc.mark_checklist_item(task.task_id, "Cuci linen")
        self.assertEqual(first.checklist[0].done, True)
        stored = self.checklist_of(task.task_id)
        frozen = self.freeze_row(task.task_id)
        self.clock.advance(seconds=600)
        error = self.refuse(
            OT.TaskFieldUnchanged, self.svc.mark_checklist_item, task.task_id, "Cuci linen"
        )
        self.assertEqual(error.code, "OPS_TASK_CHECKLIST_ITEM_UNCHANGED")
        self.assertFrozen(task.task_id, frozen)
        self.assertEqual(self.checklist_of(task.task_id), stored)
        self.assertEqual(len(self.checklist_of(task.task_id)), 2)
        self.assertEqual(self.svc.get(task.task_id).progress, (1, 2))

    def test_marking_a_step_twice_after_other_steps_move_still_refuses(self):
        task = self.make(checklist=["Cuci linen", "Keringkan"])
        self.svc.mark_checklist_item(task.task_id, "Keringkan")
        self.svc.mark_checklist_item(task.task_id, "Cuci linen")
        frozen = self.freeze_row(task.task_id)
        self.refuse(
            OT.TaskFieldUnchanged, self.svc.mark_checklist_item, task.task_id, "Keringkan"
        )
        self.assertFrozen(task.task_id, frozen)
        self.assertEqual(self.svc.get(task.task_id).progress, (2, 2))

    def test_lookalike_labels_are_not_cross_ticked(self):
        task = self.make(checklist=["Cuci linen", "Cuci linen dalam"])
        self.clock.advance(seconds=30)
        self.svc.mark_checklist_item(task.task_id, "Cuci linen")
        stored = self.checklist_of(task.task_id)
        self.assertEqual(
            stored[0],
            {"label": "Cuci linen", "done": True, "done_at": P.to_iso(self.clock.now())},
        )
        self.assertEqual(
            stored[1], {"label": "Cuci linen dalam", "done": False, "done_at": None}
        )
        self.clock.advance(seconds=30)
        self.svc.mark_checklist_item(task.task_id, "Cuci linen dalam")
        stored = self.checklist_of(task.task_id)
        self.assertEqual(stored[1]["done_at"], P.to_iso(self.clock.now()))
        self.assertEqual(len(stored), 2)

    def test_marking_an_unknown_step_is_refused(self):
        task = self.make(checklist=["Cuci linen"])
        frozen = self.freeze_row(task.task_id)
        error = self.refuse(
            OT.InvalidChecklist, self.svc.mark_checklist_item, task.task_id, "Langkah hantu"
        )
        self.assertEqual(error.code, "OPS_TASK_CHECKLIST_ITEM_NOT_FOUND")
        self.assertFrozen(task.task_id, frozen)

    def test_reopening_a_step_clears_its_done_at(self):
        task = self.make(checklist=["Cuci linen"])
        self.svc.mark_checklist_item(task.task_id, "Cuci linen")
        self.clock.advance(seconds=60)
        reopened = self.svc.mark_checklist_item(task.task_id, "Cuci linen", done=False)
        self.assertFalse(reopened.checklist[0].done)
        self.assertIsNone(reopened.checklist[0].done_at)
        self.assertEqual(
            self.checklist_of(task.task_id),
            [{"label": "Cuci linen", "done": False, "done_at": None}],
        )

    def test_set_checklist_replaces_the_runbook_and_reopens_every_step(self):
        task = self.make(checklist=["A", "B"])
        self.svc.mark_checklist_item(task.task_id, "A")
        self.clock.advance(seconds=60)
        replaced = self.svc.set_checklist(task.task_id, ["C", "D"])
        self.assertEqual([item.label for item in replaced.checklist], ["C", "D"])
        self.assertEqual(replaced.progress, (0, 2))
        self.assertEqual(
            self.checklist_of(task.task_id),
            [
                {"label": "C", "done": False, "done_at": None},
                {"label": "D", "done": False, "done_at": None},
            ],
        )

    def test_add_checklist_item_appends_once(self):
        task = self.make(checklist=["A"])
        grown = self.svc.add_checklist_item(task.task_id, "B")
        self.assertEqual([item.label for item in grown.checklist], ["A", "B"])
        self.assertEqual(self.checklist_of(task.task_id)[1]["label"], "B")
        self.assertEqual(self.checklist_of(task.task_id)[1]["done"], False)

    def test_add_checklist_item_refuses_an_unknown_task(self):
        error = self.refuse(OT.TaskNotFound, self.svc.add_checklist_item, "task_hantu", "A")
        self.assertEqual(error.code, "OPS_TASK_NOT_FOUND")
        self.assertEqual(self.count(), 0)


class TestStoredShape(OpsTaskTestBase):
    """Invariant 10: canonical stamps and typed columns, no floats, no NULLs."""

    def walk_a_task_through_its_life(self) -> OT.Task:
        task = self.make(
            checklist=["Cuci linen", "Keringkan"],
            priority=OT.PRIORITY_HIGH,
            case_id="case_1",
            booking_id="book_1",
        )
        self.clock.advance(seconds=60)
        self.svc.start(task.task_id)
        self.clock.advance(seconds=60)
        self.svc.block(task.task_id, "mesin cuci rusak")
        self.clock.advance(seconds=60)
        self.svc.unblock(task.task_id)
        self.clock.advance(seconds=60)
        self.svc.mark_checklist_item(task.task_id, "Cuci linen")
        self.clock.advance(seconds=60)
        self.svc.mark_checklist_item(task.task_id, "Keringkan")
        self.clock.advance(seconds=60)
        return self.svc.complete(task.task_id)

    def test_every_stored_stamp_is_iso_8601_with_an_offset(self):
        task = self.walk_a_task_through_its_life()
        row = self.raw(task.task_id)
        for column in ("created_at", "updated_at", "due_at", "completed_at"):
            with self.subTest(column=column):
                value = row[column]
                self.assertIsInstance(value, str)
                self.assertRegex(value, ISO_Z)
                self.assertEqual(P.to_iso(P.parse_iso(value)), value)

    def test_no_mandatory_column_is_null_or_a_float(self):
        self.walk_a_task_through_its_life()
        self.assertEqual(self.count(), 1)
        for row in self.rows():
            for column in REQUIRED_COLUMNS:
                with self.subTest(column=column):
                    value = row[column]
                    self.assertIsNotNone(value, f"{column} is NULL")
                    self.assertNotIsInstance(value, float, f"{column} is a float")
                    self.assertIsInstance(value, (str, int))

    def test_optional_columns_are_null_or_strings_never_floats(self):
        self.walk_a_task_through_its_life()
        for row in self.rows():
            for column in OPTIONAL_COLUMNS:
                with self.subTest(column=column):
                    value = row[column]
                    self.assertNotIsInstance(value, float)
                    if value is not None:
                        self.assertIsInstance(value, str)

    def test_checklist_column_is_a_json_array_of_the_documented_shape(self):
        self.make(checklist=["Cuci linen"], idempotency_key="idem_task_0071")
        self.make(checklist=["Test pompa"], idempotency_key="idem_task_0072")
        for row in self.rows():
            with self.subTest(task_id=row["task_id"]):
                payload = json.loads(row["checklist"])
                self.assertIsInstance(payload, list)
                for entry in payload:
                    self.assertEqual(set(entry), {"label", "done", "done_at"})
                    self.assertIsInstance(entry["label"], str)
                    self.assertIsInstance(entry["done"], bool)
                    if entry["done"]:
                        self.assertRegex(entry["done_at"], ISO_Z)
                    else:
                        self.assertIsNone(entry["done_at"])

    def test_closed_rows_carry_the_completed_at_stamp_the_ddl_demands(self):
        task = self.walk_a_task_through_its_life()
        row = self.raw(task.task_id)
        self.assertEqual(row["status"], "DONE")
        self.assertIsNotNone(row["completed_at"])
        self.assertEqual(row["completed_at"], P.to_iso(self.clock.now()))
        self.assertIsNone(row["blocked_reason"])
        cancelled = self.make(idempotency_key="idem_task_0073")
        self.svc.cancel(cancelled.task_id)
        self.assertIsNotNone(self.raw(cancelled.task_id)["completed_at"])

    def test_blocked_row_satisfies_the_ddl_blocked_reason_check(self):
        task = self.make()
        self.svc.start(task.task_id)
        self.svc.block(task.task_id, "menunggu tukang")
        row = self.raw(task.task_id)
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIsInstance(row["blocked_reason"], str)
        self.assertGreaterEqual(len(row["blocked_reason"]), OT.MIN_BLOCKED_REASON_LEN)
        self.assertIsNone(row["completed_at"])

    def test_assignee_is_never_blank_in_the_stored_row(self):
        self.walk_a_task_through_its_life()
        for row in self.rows():
            with self.subTest(task_id=row["task_id"]):
                self.assertTrue(str(row["assignee"]).strip())


class TestSourceHygiene(unittest.TestCase):
    """The module may not reach for wall-clock time, randomness or the network."""

    def test_ops_tasks_module_uses_the_injected_clock_only(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "datetime.now",
            "utcnow",
            "time.time",
            "import random",
            "import requests",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_only_float_conversion_is_the_horizon_argument(self):
        """``due_within`` converts the caller's hour horizon, never a stored value."""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("float("), 1)
        self.assertIn("timedelta(hours=float(hours))", source)

    def test_no_stored_amount_column_is_handled_as_a_float(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        for forbidden in ("float(due", "float(total", "float(amount", "float(minor", "0.0"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
