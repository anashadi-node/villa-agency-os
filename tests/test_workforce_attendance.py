"""Bukti kehadiran — koreksi tidak pernah menimpa, hanya menunjuk (T1.3).

Yang dikunci di sini menyangkut upah dan sengketa kerja, bukan tampilan:

* tabel bukti hanya bisa ditambah — ``UPDATE`` dan ``DELETE`` ditolak database;
* koreksi lahir sebagai baris baru yang menunjuk baris lama, dan baris lama tetap utuh;
* koreksi mewarisi orang dan shift dari baris yang dikoreksi, tidak memilih sendiri;
* satu baris dikoreksi paling banyak sekali, supaya tidak ada dua "kebenaran";
* koreksi manual wajib menyebut alasan yang bisa dibaca manusia;
* kiriman ulang perangkat mengembalikan baris lama, bukan baris kedua.

Fixture sintetis: nama, properti, dan jam di sini tidak mewakili siapa pun.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from primitives import DomainError, EntityNotFound
from workforce.attendance import (
    AlreadySuperseded,
    AttendanceEventNotFound,
    AttendanceError,
    AttendanceRecord,
    AttendanceWriter,
    EventInTheFuture,
    ManualTargetRequired,
    RosterMismatch,
    StaffNotClockable,
)
from workforce.roster import RosterWriter, ShiftSpec

AGENCY = "agency_default"
NOW = "2026-11-14T00:00:00Z"
PROP_BALI = "prop_bali"


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


def add_staff(conn, staff_id: str = "stf_1", employee_no: str = "EMP-1", status: str = "ACTIVE") -> str:
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, home_property_id, status, hired_on, ended_on, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Kadek Uji', 'HOUSEKEEPING', 'PERMANENT', ?, ?, '2026-01-01', NULL, ?, ?)",
        (staff_id, AGENCY, employee_no, PROP_BALI, status, NOW, NOW),
    )
    return staff_id


def add_template(conn, template_id: str = "tpl_1") -> str:
    conn.execute(
        "INSERT INTO workforce_shift_templates (shift_template_id, agency_id, property_id,"
        " shift_code, starts_local, ends_local, effective_from)"
        " VALUES (?, ?, ?, 'MORNING', '07:00', '15:00', '2026-01-01')",
        (template_id, AGENCY, PROP_BALI),
    )
    return template_id


@pytest.fixture()
def world(conn, clock, ids):
    """Satu agensi, satu properti, dua staf aktif, satu template shift."""
    add_property(conn)
    add_staff(conn, "stf_1", "EMP-1")
    add_staff(conn, "stf_2", "EMP-2")
    add_template(conn)
    return AttendanceWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)


@pytest.fixture()
def roster(conn, clock, ids):
    return RosterWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)


def record(staff_id: str = "stf_1", event_type: str = "CLOCK_IN",
           occurred_at_utc: str = "2026-09-21T07:00:00Z", key: str | None = None, **overrides) -> AttendanceRecord:
    fields: dict[str, Any] = {
        "staff_id": staff_id,
        "event_type": event_type,
        "occurred_at_utc": occurred_at_utc,
        "idempotency_key": key or f"dev-{staff_id}-{event_type}-{occurred_at_utc}",
        "actor_id": "act_device_gate",
    }
    fields.update(overrides)
    return AttendanceRecord(**fields)


def count_events(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM workforce_attendance_events").fetchone()[0]


# --------------------------------------------------------------------------------------
# penulisan dasar
# --------------------------------------------------------------------------------------


def test_record_stores_the_device_report_as_utc(world, conn):
    row, created = world.record(record(occurred_at_utc="2026-09-21T07:00:00+08:00"))

    assert created is True
    assert row["event_type"] == "CLOCK_IN"
    assert row["source"] == "DEVICE"
    assert row["occurred_at_utc"] == "2026-09-20T23:00:00+00:00"
    assert row["supersedes_event_id"] is None
    assert count_events(conn) == 1


def test_replaying_the_same_device_report_returns_the_existing_row(world, conn):
    first, created = world.record(record())
    replay, replay_created = world.record(record())

    assert created is True
    assert replay_created is False
    assert replay["attendance_event_id"] == first["attendance_event_id"]
    assert replay["recorded_at_utc"] == first["recorded_at_utc"]
    assert count_events(conn) == 1


def test_evidence_table_refuses_update_and_delete(conn):
    add_property(conn)
    add_staff(conn)
    conn.execute(
        "INSERT INTO workforce_attendance_events (agency_id, attendance_event_id, staff_id,"
        " event_type, occurred_at_utc, recorded_at_utc, source, geofence_verdict, actor_id,"
        " idempotency_key, created_at)"
        " VALUES (?, 'att_1', 'stf_1', 'CLOCK_IN', '2026-09-21T00:00:00+00:00',"
        " '2026-09-21T00:00:05+00:00', 'DEVICE', 'UNKNOWN', 'act_1', 'k1', '2026-09-21T00:00:05+00:00')",
        (AGENCY,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE workforce_attendance_events SET event_type = 'CLOCK_OUT'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM workforce_attendance_events")
    assert conn.execute(
        "SELECT event_type FROM workforce_attendance_events WHERE attendance_event_id = 'att_1'"
    ).fetchone()[0] == "CLOCK_IN"


def test_suspended_staff_cannot_produce_attendance_evidence(world, conn):
    add_staff(conn, "stf_3", "EMP-3", status="SUSPENDED")

    with pytest.raises(StaffNotClockable) as caught:
        world.record(record(staff_id="stf_3"))

    assert caught.value.details["status"] == "SUSPENDED"
    assert count_events(conn) == 0


def test_unknown_staff_is_not_found_rather_than_created(world, conn):
    with pytest.raises(EntityNotFound):
        world.record(record(staff_id="stf_ghost"))

    assert count_events(conn) == 0


def test_manual_source_must_carry_evidence(world, conn):
    with pytest.raises(AttendanceError) as caught:
        world.record(record(source="MANUAL"))

    assert caught.value.code == "WORKFORCE_REASON_TOO_SHORT"
    assert count_events(conn) == 0


def test_event_cannot_be_recorded_before_it_happened(world, conn):
    with pytest.raises(EventInTheFuture) as caught:
        world.record(record(occurred_at_utc="2026-09-21T09:00:00Z"))  # jam server 08:00Z

    assert caught.value.details["occurred_at_utc"] == "2026-09-21T09:00:00+00:00"
    assert count_events(conn) == 0


def test_naive_timestamp_is_refused(world):
    with pytest.raises(DomainError) as caught:
        world.record(record(occurred_at_utc="2026-09-21T07:00:00"))

    assert caught.value.code == "NAIVE_TIMESTAMP"


# --------------------------------------------------------------------------------------
# koreksi: baris baru yang menunjuk baris lama
# --------------------------------------------------------------------------------------


def test_correction_appends_and_leaves_the_corrected_row_untouched(world, conn):
    original, _ = world.record(record(occurred_at_utc="2026-09-21T07:00:00Z"))

    fix, created = world.correct(
        original["attendance_event_id"],
        occurred_at_utc="2026-09-21T07:35:00Z",
        reason="staf lupa tap, absen rumah sakit 07:35",
        actor_id="act_supervisor_1",
        idempotency_key="corr-1",
    )

    assert created is True
    assert fix["event_type"] == "MANUAL_ADJUST"
    assert fix["source"] == "MANUAL"
    assert fix["supersedes_event_id"] == original["attendance_event_id"]
    assert fix["staff_id"] == original["staff_id"]
    assert fix["occurred_at_utc"] == "2026-09-21T07:35:00+00:00"
    assert "rumah sakit" in fix["evidence_ref"]
    # Baris lama masih ada dan isinya tidak berubah.
    still = conn.execute(
        "SELECT * FROM workforce_attendance_events WHERE attendance_event_id = ?",
        (original["attendance_event_id"],),
    ).fetchone()
    assert still["event_type"] == "CLOCK_IN"
    assert still["occurred_at_utc"] == original["occurred_at_utc"]
    assert count_events(conn) == 2


def test_correction_inherits_the_staff_of_the_corrected_event(world, conn):
    """Supervisor tidak bisa memindahkan jam kerja orang lain kepada orang lain."""
    other, _ = world.record(record(staff_id="stf_2"))

    fix, _ = world.correct(
        other["attendance_event_id"],
        occurred_at_utc="2026-09-21T07:10:00Z",
        reason="tap ganda, aktual 07:10",
        actor_id="act_supervisor_1",
        idempotency_key="corr-2",
    )

    assert fix["staff_id"] == "stf_2"
    assert fix["actor_id"] == "act_supervisor_1"


def test_a_row_can_only_be_corrected_once(world, conn):
    original, _ = world.record(record())
    fix, _ = world.correct(
        original["attendance_event_id"],
        occurred_at_utc="2026-09-21T07:05:00Z",
        reason="aktual 07:05",
        actor_id="act_supervisor_1",
        idempotency_key="corr-3a",
    )

    with pytest.raises(AlreadySuperseded) as caught:
        world.correct(
            original["attendance_event_id"],
            occurred_at_utc="2026-09-21T07:06:00Z",
            reason="aktual 07:06",
            actor_id="act_supervisor_2",
            idempotency_key="corr-3b",
        )

    assert caught.value.details["supersedes_event_id"] == fix["attendance_event_id"]
    # Koreksi atas koreksi tetap sah: rantainya lurus, bukan bercabang.
    world.correct(
        fix["attendance_event_id"],
        occurred_at_utc="2026-09-21T07:07:00Z",
        reason="koreksi kedua: aktual 07:07",
        actor_id="act_supervisor_2",
        idempotency_key="corr-3c",
    )
    assert count_events(conn) == 3


def test_correction_must_name_a_real_row(world, conn):
    with pytest.raises(AttendanceEventNotFound):
        world.correct(
            "att_tidak_ada",
            occurred_at_utc="2026-09-21T07:05:00Z",
            reason="aktual 07:05",
            actor_id="act_supervisor_1",
            idempotency_key="corr-4",
        )

    assert count_events(conn) == 0


def test_correction_cannot_switch_the_shift(roster, world, conn):
    mine, _ = roster.add_shift(
        ShiftSpec(staff_id="stf_1", shift_template_id="tpl_1", property_id=PROP_BALI,
                  starts_at_utc="2026-09-20T23:00:00Z", ends_at_utc="2026-09-21T07:00:00Z")
    )
    theirs, _ = roster.add_shift(
        ShiftSpec(staff_id="stf_2", shift_template_id="tpl_1", property_id=PROP_BALI,
                  starts_at_utc="2026-09-21T00:00:00Z", ends_at_utc="2026-09-21T08:00:00Z")
    )
    original, _ = world.record(record(roster_entry_id=mine["roster_entry_id"]))

    with pytest.raises(RosterMismatch) as caught:
        world.correct(
            original["attendance_event_id"],
            occurred_at_utc="2026-09-21T07:05:00Z",
            reason="aktual 07:05",
            actor_id="act_supervisor_1",
            idempotency_key="corr-5",
            roster_entry_id=theirs["roster_entry_id"],
        )

    assert caught.value.code == "WORKFORCE_ATTENDANCE_ROSTER_MISMATCH"
    assert count_events(conn) == 1


def test_device_cannot_report_against_another_staff_shift(world, roster):
    theirs, _ = roster.add_shift(
        ShiftSpec(staff_id="stf_2", shift_template_id="tpl_1", property_id=PROP_BALI,
                  starts_at_utc="2026-09-20T23:00:00Z", ends_at_utc="2026-09-21T07:00:00Z")
    )

    with pytest.raises(RosterMismatch):
        world.record(record(staff_id="stf_1", roster_entry_id=theirs["roster_entry_id"]))


def test_correction_without_a_usable_reason_is_refused(world, conn):
    original, _ = world.record(record())

    with pytest.raises(AttendanceError) as caught:
        world.correct(
            original["attendance_event_id"],
            occurred_at_utc="2026-09-21T07:05:00Z",
            reason="???",
            actor_id="act_supervisor_1",
            idempotency_key="corr-6",
        )

    assert caught.value.code == "WORKFORCE_REASON_TOO_SHORT"
    assert count_events(conn) == 1


def test_manual_adjust_cannot_be_written_through_record(world, conn):
    with pytest.raises(ManualTargetRequired) as caught:
        world.record(record(event_type="MANUAL_ADJUST", source="MANUAL", evidence_ref="aktual 07:05"))

    assert caught.value.code == "WORKFORCE_ATTENDANCE_MANUAL_TARGET_REQUIRED"
    assert count_events(conn) == 0


def test_replaying_a_correction_returns_the_existing_correction(world, conn):
    original, _ = world.record(record())
    first, _ = world.correct(
        original["attendance_event_id"],
        occurred_at_utc="2026-09-21T07:05:00Z",
        reason="aktual 07:05",
        actor_id="act_supervisor_1",
        idempotency_key="corr-7",
    )
    replay, replay_created = world.correct(
        original["attendance_event_id"],
        occurred_at_utc="2026-09-21T07:05:00Z",
        reason="aktual 07:05",
        actor_id="act_supervisor_1",
        idempotency_key="corr-7",
    )

    assert replay_created is False
    assert replay["attendance_event_id"] == first["attendance_event_id"]
    assert count_events(conn) == 2
