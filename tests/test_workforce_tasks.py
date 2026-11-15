"""Penugasan tugas — kapasitas orang diukur, bukan dikarang (T1.5).

Keenam janji yang dikunci di sini, semuanya berujung pada orang yang kelelahan
atau pekerjaan yang tidak pernah dikerjakan:

* hanya staf ``ACTIVE`` yang menerima tugas, dan tugas yang sudah ditutup tidak
  menerima penugasan baru;
* menit yang dijanjikan tidak boleh melebihi kontrak mingguan orang itu, dan
  minggu dipotong pada Senin 00:00 UTC — bukan pada "7 hari terakhir";
* penugasan ``DROPPED`` mengembalikan waktunya, karena pekerjaan yang batal
  memang tidak dikerjakan;
* kiriman ulang mengembalikan baris yang sama, sedangkan isi berbeda dengan
  kunci yang sama ditolak — bukan ditimpa;
* status hanya bergerak maju, ``DONE`` membawa waktu selesai, dan ``DROPPED``
  wajib beralasan (dijaga juga oleh ``CHECK`` di basis data);
* jam yang naive tidak pernah dicap ke catatan kerja.

Fixture-nya sintetis: nama staf, properti, dan jam di sini tidak mewakili siapa pun.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from primitives import DomainError, EntityNotFound
from workforce.repository import InvalidIdempotencyKey
from workforce.tasks import (
    AlreadyAssigned,
    AssignmentKeyReused,
    AssignmentSpec,
    CapacityExceeded,
    DropReasonRequired,
    InvalidTransition,
    StaffNotAssignable,
    TaskAssignmentWriter,
    TaskClosed,
)

AGENCY = "agency_default"
PROP_BALI = "prop_bali"
NOW = "2026-11-14T00:00:00Z"
#: Senin. Dipakai sebagai jam patok supaya pemotongan minggu tidak ambigu.
MONDAY = "2026-11-16T00:00:00Z"
DONE_AT = "2026-11-16T00:00:00+00:00"  # bentuk kanonik to_iso()


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def add_property(conn, property_id: str = PROP_BALI) -> str:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, 'Villa Uji', 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE',"
        " 'ACTIVATED', ?, ?)",
        (property_id, AGENCY, NOW, NOW),
    )
    return property_id


def add_staff(
    conn,
    staff_id: str = "stf_1",
    employee_no: str = "EMP-1",
    status: str = "ACTIVE",
    weekly_minutes: int = 2400,
    property_id: str = PROP_BALI,
) -> str:
    ended_on = "2026-11-01" if status == "OFFBOARDED" else None
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, home_property_id, weekly_contract_minutes, status, hired_on, ended_on,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, 'Kadek Uji', 'HOUSEKEEPING', 'PERMANENT', ?, ?, ?, '2026-01-01', ?, ?, ?)",
        (staff_id, AGENCY, employee_no, property_id, weekly_minutes, status, ended_on, NOW, NOW),
    )
    return staff_id


def add_task(
    conn,
    task_id: str = "task_1",
    status: str = "OPEN",
    due_at: str = "2026-11-20T10:00:00Z",
    kind: str = "CLEANING",
) -> str:
    conn.execute(
        "INSERT INTO ops_tasks (task_id, agency_id, kind, due_at, priority, status,"
        " idempotency_key, created_at, updated_at, completed_at, blocked_reason)"
        " VALUES (?, ?, ?, ?, 'NORMAL', ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            AGENCY,
            kind,
            due_at,
            status,
            f"seed-{task_id}",
            NOW,
            NOW,
            NOW if status in ("DONE", "CANCELLED") else None,
            "menunggu suku cadang" if status == "BLOCKED" else None,
        ),
    )
    return task_id


@pytest.fixture()
def world(conn, ids):
    add_property(conn)
    add_staff(conn)
    add_task(conn)
    writer = TaskAssignmentWriter(
        conn, agency_id=AGENCY, clock=_fixed_clock(MONDAY), ids=ids
    )
    return conn, writer


def _fixed_clock(moment: str):
    from primitives import FrozenClock

    return FrozenClock.at(moment)


def spec(**overrides: Any) -> AssignmentSpec:
    base: dict[str, Any] = dict(
        task_id="task_1",
        staff_id="stf_1",
        planned_minutes=120,
        assigned_by="spv_budi",
        idempotency_key="assign-key-0001",
        role_in_task="LEAD",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


class _NaiveClock:
    def now(self):
        from datetime import datetime

        return datetime(2026, 11, 16, 0, 0, 0)


# --------------------------------------------------------------------------------------
# aturan 1 — orangnya harus ada dan masih boleh bekerja; tugasnya harus masih hidup
# --------------------------------------------------------------------------------------


def test_only_active_staff_receive_work(world):
    conn, writer = world
    add_staff(conn, staff_id="stf_susp", employee_no="EMP-2", status="SUSPENDED")
    add_staff(conn, staff_id="stf_gone", employee_no="EMP-3", status="OFFBOARDED")

    for staff_id in ("stf_susp", "stf_gone"):
        with pytest.raises(StaffNotAssignable) as excinfo:
            writer.assign(spec(staff_id=staff_id, idempotency_key=f"key-{staff_id}"))
        assert excinfo.value.code == "WORKFORCE_STAFF_NOT_ASSIGNABLE"
        assert excinfo.value.details["staff_id"] == staff_id

    assert writer.assignments_for_staff("stf_susp") == []


def test_unknown_staff_and_unknown_task_are_not_found(world):
    conn, writer = world
    with pytest.raises(EntityNotFound):
        writer.assign(spec(staff_id="stf_missing"))
    with pytest.raises(EntityNotFound):
        writer.assign(spec(task_id="task_missing"))
    with pytest.raises(EntityNotFound):
        writer.get("assign_missing")


def test_a_closed_task_refuses_new_work(world):
    conn, writer = world
    for task_id, status in (("task_done", "DONE"), ("task_cancelled", "CANCELLED")):
        add_task(conn, task_id=task_id, status=status)
        with pytest.raises(TaskClosed) as excinfo:
            writer.assign(spec(task_id=task_id, idempotency_key=f"key-{task_id}"))
        assert excinfo.value.code == "WORKFORCE_TASK_CLOSED"
        assert excinfo.value.details["status"] == status


def test_a_blocked_task_still_accepts_work(world):
    """BLOCKED berarti tertahan, bukan selesai — orangnya tetap bisa ditugaskan."""
    conn, writer = world
    add_task(conn, task_id="task_blocked", status="BLOCKED")
    row, created = writer.assign(spec(task_id="task_blocked"))
    assert created is True
    assert row["status"] == "ASSIGNED"


# --------------------------------------------------------------------------------------
# aturan 2 — kapasitas mingguan diukur, dan minggu dipotong pada Senin UTC
# --------------------------------------------------------------------------------------


def test_capacity_is_measured_not_assumed(world):
    conn, writer = world
    add_staff(conn, staff_id="stf_pt", employee_no="EMP-9", weekly_minutes=480)

    first, created = writer.assign(
        spec(staff_id="stf_pt", planned_minutes=480, idempotency_key="pt-key-0001")
    )
    assert created is True
    assert first["planned_minutes"] == 480
    assert writer.capacity_used("stf_pt", "2026-11-20T10:00:00Z") == 480
    assert writer.remaining_capacity("stf_pt", "2026-11-20T10:00:00Z") == 0

    add_task(conn, task_id="task_2")
    with pytest.raises(CapacityExceeded) as excinfo:
        writer.assign(
            spec(task_id="task_2", staff_id="stf_pt", planned_minutes=60, idempotency_key="pt-key-0002")
        )
    details = excinfo.value.details
    assert details["weekly_contract_minutes"] == 480
    assert details["minutes_already_planned"] == 480
    assert details["planned_minutes"] == 60


def test_week_boundary_is_monday_utc(world):
    """Minggu 22 Nov dan Senin 23 Nov adalah dua anggaran kapasitas berbeda."""
    conn, writer = world
    add_staff(conn, staff_id="stf_pt", employee_no="EMP-9", weekly_minutes=480)
    add_task(conn, task_id="task_sun", due_at="2026-11-22T23:59:59Z")
    add_task(conn, task_id="task_mon", due_at="2026-11-23T00:00:00Z")

    writer.assign(
        spec(
            task_id="task_sun",
            staff_id="stf_pt",
            planned_minutes=480,
            due_at_utc="2026-11-22T23:59:59Z",
            idempotency_key="wk-key-0001",
        )
    )
    assert writer.capacity_used("stf_pt", "2026-11-22T23:59:59Z") == 480
    assert writer.capacity_used("stf_pt", "2026-11-23T00:00:00Z") == 0

    row, created = writer.assign(
        spec(
            task_id="task_mon",
            staff_id="stf_pt",
            planned_minutes=480,
            due_at_utc="2026-11-23T00:00:00Z",
            idempotency_key="wk-key-0002",
        )
    )
    assert created is True
    assert row["planned_minutes"] == 480


def test_dropping_an_assignment_returns_its_capacity(world):
    conn, writer = world
    add_staff(conn, staff_id="stf_pt", employee_no="EMP-9", weekly_minutes=480)
    row, _ = writer.assign(spec(staff_id="stf_pt", planned_minutes=480, idempotency_key="dp-key-0001"))
    assert writer.remaining_capacity("stf_pt", row["due_at_utc"]) == 0

    dropped = writer.drop(row["assignment_id"], reason="tamu membatalkan check-in")
    assert dropped["status"] == "DROPPED"
    assert dropped["drop_reason"] == "tamu membatalkan check-in"
    assert writer.remaining_capacity("stf_pt", row["due_at_utc"]) == 480

    add_task(conn, task_id="task_2")
    again, created = writer.assign(
        spec(task_id="task_2", staff_id="stf_pt", planned_minutes=480, idempotency_key="dp-key-0002")
    )
    assert created is True
    assert again["status"] == "ASSIGNED"


def test_completed_work_still_consumes_capacity(world):
    conn, writer = world
    add_staff(conn, staff_id="stf_pt", employee_no="EMP-9", weekly_minutes=480)
    row, _ = writer.assign(spec(staff_id="stf_pt", planned_minutes=480, idempotency_key="dn-key-0001"))
    writer.accept(row["assignment_id"])
    writer.start(row["assignment_id"])
    writer.complete(row["assignment_id"])
    assert writer.capacity_used("stf_pt", row["due_at_utc"]) == 480


# --------------------------------------------------------------------------------------
# aturan 4 — kiriman ulang bukan kesalahan, isi berbeda dengan kunci sama adalah kesalahan
# --------------------------------------------------------------------------------------


def test_resubmission_returns_the_same_row(world):
    conn, writer = world
    first, created = writer.assign(spec())
    assert created is True

    second, created_again = writer.assign(spec())
    assert created_again is False
    assert second["assignment_id"] == first["assignment_id"]
    assert len(writer.assignments_for_task("task_1")) == 1


def test_same_key_with_a_different_payload_is_refused(world):
    conn, writer = world
    writer.assign(spec())
    with pytest.raises(AssignmentKeyReused) as excinfo:
        writer.assign(spec(planned_minutes=240))
    assert excinfo.value.code == "WORKFORCE_ASSIGNMENT_KEY_REUSED"
    assert excinfo.value.details["idempotency_key"] == "assign-key-0001"


def test_the_same_slot_cannot_be_assigned_twice_with_different_effort(world):
    conn, writer = world
    first, _ = writer.assign(spec())
    same, created = writer.assign(spec(idempotency_key="assign-key-0002"))
    assert created is False
    assert same["assignment_id"] == first["assignment_id"]

    with pytest.raises(AlreadyAssigned) as excinfo:
        writer.assign(spec(planned_minutes=90, idempotency_key="assign-key-0003"))
    assert excinfo.value.code == "WORKFORCE_ALREADY_ASSIGNED"
    assert excinfo.value.details["assignment_id"] == first["assignment_id"]


def test_different_roles_are_different_slots(world):
    conn, writer = world
    writer.assign(spec())
    row, created = writer.assign(spec(role_in_task="REVIEWER", idempotency_key="assign-key-0002"))
    assert created is True
    assert row["role_in_task"] == "REVIEWER"
    assert len(writer.assignments_for_task("task_1")) == 2


def test_a_short_idempotency_key_is_refused(world):
    conn, writer = world
    with pytest.raises(InvalidIdempotencyKey):
        writer.assign(spec(idempotency_key="short"))


@pytest.mark.parametrize("minutes", [0, 481, -30, True, "120", 12.5])
def test_planned_minutes_are_whole_and_bounded(world, minutes):
    conn, writer = world
    with pytest.raises(DomainError) as excinfo:
        writer.assign(spec(planned_minutes=minutes))
    assert excinfo.value.code == "WORKFORCE_INVALID_MINUTES"


def test_role_must_be_known(world):
    conn, writer = world
    with pytest.raises(DomainError) as excinfo:
        writer.assign(spec(role_in_task="OWNER"))
    assert excinfo.value.code == "WORKFORCE_INVALID_ROLE"


def test_due_date_defaults_to_the_task_due_date(world):
    conn, writer = world
    row, _ = writer.assign(spec())
    assert row["due_at_utc"] == "2026-11-20T10:00:00+00:00"


# --------------------------------------------------------------------------------------
# aturan 5 — status hanya maju, selesai membawa waktu, batal wajib beralasan
# --------------------------------------------------------------------------------------


def test_status_moves_forward_and_done_carries_its_time(world):
    conn, writer = world
    row, _ = writer.assign(spec())
    assert row["status"] == "ASSIGNED"
    assert row["completed_at_utc"] is None

    accepted = writer.accept(row["assignment_id"])
    assert accepted["status"] == "ACCEPTED"
    started = writer.start(row["assignment_id"])
    assert started["status"] == "IN_PROGRESS"
    done = writer.complete(row["assignment_id"])
    assert done["status"] == "DONE"
    assert done["completed_at_utc"] == DONE_AT


def test_a_terminal_assignment_cannot_be_reopened(world):
    conn, writer = world
    row, _ = writer.assign(spec())
    writer.accept(row["assignment_id"])
    writer.start(row["assignment_id"])
    writer.complete(row["assignment_id"])

    for call in (
        lambda: writer.accept(row["assignment_id"]),
        lambda: writer.start(row["assignment_id"]),
        lambda: writer.drop(row["assignment_id"], reason="sudah selesai"),
    ):
        with pytest.raises(InvalidTransition) as excinfo:
            call()
        assert excinfo.value.code == "WORKFORCE_INVALID_TRANSITION"

    fresh = writer.get(row["assignment_id"])
    assert fresh["status"] == "DONE"
    assert fresh["completed_at_utc"] == DONE_AT


def test_dropping_requires_a_reason_and_is_idempotent(world):
    conn, writer = world
    row, _ = writer.assign(spec())
    with pytest.raises(DropReasonRequired) as excinfo:
        writer.drop(row["assignment_id"], reason="  ")
    assert excinfo.value.code == "WORKFORCE_REASON_TOO_SHORT"
    assert writer.get(row["assignment_id"])["status"] == "ASSIGNED"

    first = writer.drop(row["assignment_id"], reason="villa ditutup untuk renovasi")
    second = writer.drop(row["assignment_id"], reason="alasan lain sama sekali")
    assert second["assignment_id"] == first["assignment_id"]
    assert second["drop_reason"] == "villa ditutup untuk renovasi"


def test_the_database_also_refuses_an_unexplained_drop(world):
    conn, writer = world
    row, _ = writer.assign(spec())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE workforce_task_assignments SET status = 'DROPPED' WHERE assignment_id = ?",
            (row["assignment_id"],),
        )
    conn.rollback()


def test_the_database_also_refuses_done_without_a_time(world):
    conn, writer = world
    row, _ = writer.assign(spec())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE workforce_task_assignments SET status = 'DONE' WHERE assignment_id = ?",
            (row["assignment_id"],),
        )
    conn.rollback()


# --------------------------------------------------------------------------------------
# aturan 6 — jam naive tidak pernah dicap
# --------------------------------------------------------------------------------------


def test_a_naive_clock_never_stamps_work(world):
    conn, writer = world
    writer.clock = _NaiveClock()
    with pytest.raises(DomainError) as excinfo:
        writer.assign(spec())
    assert excinfo.value.code == "NAIVE_TIMESTAMP"
    assert writer.assignments_for_task("task_1") == []


def test_a_naive_due_datetime_is_refused(world):
    from datetime import datetime

    conn, writer = world
    with pytest.raises(DomainError) as excinfo:
        writer.assign(spec(due_at_utc=datetime(2026, 11, 20, 10, 0, 0)))
    assert excinfo.value.code == "WORKFORCE_NAIVE_DATETIME"
