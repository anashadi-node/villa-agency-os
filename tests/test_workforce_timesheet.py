"""Timesheet — angka upah lahir dari bukti, dan tidak bisa diubah setelah disetujui (T1.4).

Yang dikunci di sini:

* menit dihitung dari event kehadiran, termasuk baris yang sudah dikoreksi;
* istirahat memotong menit kerja, dan lembur hanya menit di luar jadwal terbit;
* periode dengan punch menggantung **ditolak**, bukan ditebak;
* periode ≥2400 menit ditolak (butuh pemeriksaan manusia);
* SUBMITTED/APPROVED tidak bisa ditutup ulang; APPROVED membekukan isi lewat
  ``lock_hash``; REJECTED bisa disusun ulang.

Waktu di fixture sintetis, bukan data orang sungguhan.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from primitives import DomainError, EntityNotFound, FrozenClock
from workforce.attendance import AttendanceRecord, AttendanceWriter
from workforce.roster import RosterWriter, ShiftSpec
from workforce.timesheet import (
    InvalidPeriod,
    InvalidTransition,
    OpenPunch,
    PeriodEmpty,
    PeriodTimezoneMismatch,
    PeriodTooLong,
    TimesheetLocked,
    TimesheetNotFound,
    TimesheetWriter,
)

AGENCY = "agency_default"
PROP_BALI = "prop_bali"
PROP_YOGYA = "prop_yogya"
NOW = "2026-11-14T00:00:00Z"
PERIOD_START = "2026-11-09"
PERIOD_END = "2026-11-15"


def add_property(conn, property_id: str, timezone: str = "Asia/Makassar", province: str = "Bali") -> str:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, 'Villa Uji', 'villa', 'Ubud', ?, ?, 'SELLABLE', 'ACTIVATED', ?, ?)",
        (property_id, AGENCY, province, timezone, NOW, NOW),
    )
    return property_id


def add_staff(conn, staff_id: str = "stf_1", employee_no: str = "EMP-1") -> str:
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, home_property_id, status, hired_on, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Kadek Uji', 'HOUSEKEEPING', 'PERMANENT', ?, 'ACTIVE',"
        " '2026-01-01', ?, ?)",
        (staff_id, AGENCY, employee_no, PROP_BALI, NOW, NOW),
    )
    return staff_id


def add_template(conn, template_id: str = "tpl_1", property_id: str = PROP_BALI) -> str:
    conn.execute(
        "INSERT INTO workforce_shift_templates (shift_template_id, agency_id, property_id,"
        " shift_code, starts_local, ends_local, effective_from)"
        " VALUES (?, ?, ?, 'MORNING', '07:00', '15:00', '2026-01-01')",
        (template_id, AGENCY, property_id),
    )
    return template_id


def publish_shift(roster, staff_id: str = "stf_1", work_date: str = "2026-11-10",
                  starts: str = "2026-11-10T00:00:00Z", ends: str = "2026-11-10T08:00:00Z",
                  property_id: str = PROP_BALI, template_id: str = "tpl_1") -> dict:
    """Jadwalkan lalu terbitkan — hanya shift terbit yang dihitung sebagai jadwal."""
    row, _ = roster.add_shift(
        ShiftSpec(
            staff_id=staff_id,
            property_id=property_id,
            shift_template_id=template_id,
            work_date=work_date,
            starts_at_utc=starts,
            ends_at_utc=ends,
        )
    )
    return roster.publish_shift(str(row["roster_entry_id"]), published_by="sup_1")


@pytest.fixture()
def world(conn, ids):
    # Jam dipatok SETELAH periode uji: absensi tidak boleh berada di masa depan
    # relatif terhadap jam perekamnya.
    clock = FrozenClock.at("2026-11-16T00:00:00Z")
    add_property(conn, PROP_BALI)
    add_staff(conn)
    add_template(conn)
    roster = RosterWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)
    attendance = AttendanceWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)
    timesheet = TimesheetWriter(conn, agency_id=AGENCY, tz_name="Asia/Makassar",
                                clock=clock, ids=ids)
    return roster, attendance, timesheet


def punch(attendance, key, event_type, at, staff_id="stf_1"):
    row, created = attendance.record(
        AttendanceRecord(
            staff_id=staff_id,
            event_type=event_type,
            occurred_at_utc=at,
            idempotency_key=key,
            actor_id="device_1",
        )
    )
    assert created is True
    return row


# --------------------------------------------------------------------------------------
# hitung
# --------------------------------------------------------------------------------------

def test_minutes_come_from_punches_not_from_input(world):
    roster, attendance, timesheet = world
    publish_shift(roster)
    punch(attendance, "a1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "a2", "CLOCK_OUT", "2026-11-10T08:00:00Z")

    minutes = timesheet.compute("stf_1", PERIOD_START, PERIOD_END)

    assert minutes.regular_minutes == 480
    assert minutes.overtime_minutes == 0
    assert minutes.break_minutes == 0
    assert minutes.days_worked == 1


def test_break_is_subtracted_and_recorded(world):
    roster, attendance, timesheet = world
    publish_shift(roster)
    punch(attendance, "b1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "b2", "BREAK_START", "2026-11-10T04:00:00Z")
    punch(attendance, "b3", "BREAK_END", "2026-11-10T04:30:00Z")
    punch(attendance, "b4", "CLOCK_OUT", "2026-11-10T08:00:00Z")

    minutes = timesheet.compute("stf_1", PERIOD_START, PERIOD_END)

    assert minutes.break_minutes == 30
    assert minutes.regular_minutes == 450


def test_minutes_beyond_published_shift_are_overtime(world):
    roster, attendance, timesheet = world
    publish_shift(roster)
    punch(attendance, "c1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "c2", "CLOCK_OUT", "2026-11-10T10:00:00Z")

    minutes = timesheet.compute("stf_1", PERIOD_START, PERIOD_END)

    assert minutes.regular_minutes == 480
    assert minutes.overtime_minutes == 120


def test_unrostered_work_is_regular_and_counted_visibly(world):
    _, attendance, timesheet = world
    punch(attendance, "d1", "CLOCK_IN", "2026-11-11T00:00:00Z")
    punch(attendance, "d2", "CLOCK_OUT", "2026-11-11T04:00:00Z")

    minutes = timesheet.compute("stf_1", PERIOD_START, PERIOD_END)

    assert minutes.regular_minutes == 240
    assert minutes.overtime_minutes == 0
    assert minutes.unrostered_minutes == 240


def test_corrected_punch_is_used_without_touching_the_original(world):
    roster, attendance, timesheet = world
    publish_shift(roster)
    original = punch(attendance, "e1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "e2", "CLOCK_OUT", "2026-11-10T08:00:00Z")
    attendance.correct(
        str(original["attendance_event_id"]),
        occurred_at_utc="2026-11-10T01:00:00Z",
        reason="GPS meleset satu jam",
        actor_id="sup_1",
        idempotency_key="e1-fix",
    )

    minutes = timesheet.compute("stf_1", PERIOD_START, PERIOD_END)
    rows = attendance.events_for_staff("stf_1")

    assert minutes.regular_minutes == 420  # 07:00 → 08:00 UTC WITA
    assert len(rows) == 3  # baris asli tetap ada
    assert any(row["supersedes_event_id"] for row in rows)


def test_open_punch_refuses_to_guess(world):
    roster, attendance, timesheet = world
    publish_shift(roster)
    punch(attendance, "f1", "CLOCK_IN", "2026-11-10T00:00:00Z")

    with pytest.raises(OpenPunch) as caught:
        timesheet.compute("stf_1", PERIOD_START, PERIOD_END)

    assert caught.value.code == "WORKFORCE_TIMESHEET_OPEN_PUNCH"


def test_period_of_forty_hours_or_more_needs_human_review(world):
    roster, attendance, timesheet = world
    for day in range(9, 16):
        publish_shift(
            roster,
            work_date=f"2026-11-{day:02d}",
            starts=f"2026-11-{day:02d}T00:00:00Z",
            ends=f"2026-11-{day:02d}T08:00:00Z",
        )
        punch(attendance, f"g{day}a", "CLOCK_IN", f"2026-11-{day:02d}T00:00:00Z")
        punch(attendance, f"g{day}b", "CLOCK_OUT", f"2026-11-{day:02d}T08:00:00Z")

    with pytest.raises(PeriodTooLong):
        timesheet.compute("stf_1", PERIOD_START, PERIOD_END)


def test_period_dates_must_be_ordered(world):
    _, _, timesheet = world
    with pytest.raises(InvalidPeriod):
        timesheet.compute("stf_1", PERIOD_END, PERIOD_START)


def test_unknown_staff_is_a_not_found(world):
    _, _, timesheet = world
    with pytest.raises(EntityNotFound):
        timesheet.compute("stf_tidak_ada", PERIOD_START, PERIOD_END)


def test_rostered_staff_in_another_timezone_is_refused(world, conn, ids, clock):
    roster, _, _ = world
    add_property(conn, PROP_YOGYA, timezone="Asia/Jakarta", province="DI Yogyakarta")
    add_template(conn, "tpl_yogya", PROP_YOGYA)
    roster_yogya = RosterWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)
    publish_shift(
        roster_yogya,
        work_date="2026-11-10",
        starts="2026-11-10T00:00:00Z",
        ends="2026-11-10T08:00:00Z",
        property_id=PROP_YOGYA,
        template_id="tpl_yogya",
    )
    timesheet = TimesheetWriter(conn, agency_id=AGENCY, tz_name="Asia/Makassar",
                                clock=clock, ids=ids)
    with pytest.raises(PeriodTimezoneMismatch):
        timesheet.compute("stf_1", PERIOD_START, PERIOD_END)


# --------------------------------------------------------------------------------------
# siklus hidup
# --------------------------------------------------------------------------------------

def test_close_creates_open_sheet_and_is_rewritable(world):
    roster, attendance, timesheet = world
    publish_shift(roster)
    punch(attendance, "h1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "h2", "CLOCK_OUT", "2026-11-10T08:00:00Z")

    row, created = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    assert created is True
    assert row["status"] == "OPEN"
    assert int(row["regular_minutes"]) == 480

    again, created_again = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    assert created_again is False
    assert again["timesheet_id"] == row["timesheet_id"]
    assert int(again["version"]) == 2


def test_submit_requires_worked_minutes(world):
    _, _, timesheet = world
    row, _ = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    with pytest.raises(PeriodEmpty):
        timesheet.submit(str(row["timesheet_id"]), submitted_by="sup_1")


def test_approve_without_submission_is_refused(world):
    roster, attendance, timesheet = world
    punch(attendance, "i1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "i2", "CLOCK_OUT", "2026-11-10T04:00:00Z")
    row, _ = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    with pytest.raises(InvalidTransition):
        timesheet.approve(str(row["timesheet_id"]), approved_by="owner_1")


def test_approval_freezes_content_with_a_lock_hash(world):
    _, attendance, timesheet = world
    punch(attendance, "j1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "j2", "CLOCK_OUT", "2026-11-10T04:00:00Z")
    row, _ = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    timesheet.submit(str(row["timesheet_id"]), submitted_by="sup_1")

    approved = timesheet.approve(str(row["timesheet_id"]), approved_by="owner_1")

    assert approved["status"] == "APPROVED"
    assert approved["submitted_at"] is not None
    assert approved["approved_at"] is not None
    assert len(str(approved["lock_hash"])) == 64


def test_locked_period_cannot_be_recomputed(world):
    _, attendance, timesheet = world
    punch(attendance, "k1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "k2", "CLOCK_OUT", "2026-11-10T04:00:00Z")
    row, _ = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    timesheet.submit(str(row["timesheet_id"]), submitted_by="sup_1")
    timesheet.approve(str(row["timesheet_id"]), approved_by="owner_1")

    with pytest.raises(TimesheetLocked):
        timesheet.close("stf_1", PERIOD_START, PERIOD_END)


def test_rejected_period_can_be_recomputed_and_resubmitted(world):
    _, attendance, timesheet = world
    punch(attendance, "l1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "l2", "CLOCK_OUT", "2026-11-10T04:00:00Z")
    row, _ = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    timesheet.submit(str(row["timesheet_id"]), submitted_by="sup_1")
    rejected = timesheet.reject(
        str(row["timesheet_id"]), reason="jam istirahat belum dicatat", rejected_by="owner_1"
    )
    assert rejected["status"] == "REJECTED"

    punch(attendance, "l3", "BREAK_START", "2026-11-10T02:00:00Z")
    punch(attendance, "l4", "BREAK_END", "2026-11-10T02:30:00Z")
    reopened, created = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    assert created is False
    assert int(reopened["regular_minutes"]) == 210

    resubmitted = timesheet.submit(str(row["timesheet_id"]), submitted_by="sup_1")
    assert resubmitted["status"] == "SUBMITTED"
    assert resubmitted["rejected_reason"] is None


def test_reject_requires_a_reason(world):
    _, attendance, timesheet = world
    punch(attendance, "m1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "m2", "CLOCK_OUT", "2026-11-10T04:00:00Z")
    row, _ = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    timesheet.submit(str(row["timesheet_id"]), submitted_by="sup_1")
    with pytest.raises(DomainError):
        timesheet.reject(str(row["timesheet_id"]), reason="no", rejected_by="owner_1")


def test_unknown_timesheet_is_a_not_found(world):
    _, _, timesheet = world
    with pytest.raises(TimesheetNotFound):
        timesheet.get("timesheet_tidak_ada")


class _NaiveClock:
    """Jam yang lupa zona waktu — kesalahan konfigurasi yang harus ditolak, bukan ditebak."""

    def now(self):
        return datetime(2026, 11, 16, 0, 0, 0)  # tanpa tzinfo


def test_naive_clock_is_never_accepted(world):
    _, attendance, timesheet = world
    punch(attendance, "n1", "CLOCK_IN", "2026-11-10T00:00:00Z")
    punch(attendance, "n2", "CLOCK_OUT", "2026-11-10T04:00:00Z")
    row, _ = timesheet.close("stf_1", PERIOD_START, PERIOD_END)
    timesheet.clock = _NaiveClock()
    with pytest.raises(DomainError) as caught:
        timesheet.submit(str(row["timesheet_id"]), submitted_by="sup_1")
    assert caught.value.code == "NAIVE_TIMESTAMP"
