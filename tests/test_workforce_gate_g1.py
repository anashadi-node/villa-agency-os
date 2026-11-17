"""Gerbang G1 — jalur tulis Workforce Ops terbukti idempoten di satu basis data (T1.7).

T1.1–T1.5 masing-masing menguji dirinya sendiri. Yang belum diuji adalah klaim
gabungan yang dipakai gerbang G1: **empat penulis yang berbeda, dijalankan di satu
basis data yang sama, tidak pernah menghasilkan dua baris untuk satu kunci**.
Kegagalan yang dijaga di sini bukan bug per-modul, melainkan bug integrasi:

* dua kali kiriman perangkat yang sama (retry jaringan) menagih dua kali shift;
* dua kali tutup periode menghasilkan dua lembar upah untuk satu orang;
* koreksi absensi yang menimpa baris lama, sehingga bukti aslinya hilang;
* penugasan yang sama ditulis dua kali, sehingga kapasitas orang terpakai dua kali.

Yang dikunci di sini:

1. setiap penulis mengembalikan **baris yang sama** dan ``created=False`` pada kiriman
   ulang — bukan baris baru, bukan galat;
2. jumlah baris per tabel tetap satu setelah kiriman ulang (dihitung, bukan diasumsikan);
3. koreksi absensi adalah **baris baru** yang menunjuk baris lama lewat
   ``supersedes_event_id``; baris lama tidak berubah;
4. bukti absensi tidak bisa di-``UPDATE`` atau di-``DELETE`` (trigger basis data);
5. seluruh jalur tulis hidup di dalam satu transaksi: kegagalan di tengah tidak
   meninggalkan baris separuh jadi;
6. jaminan yang sama dipegang oleh basis data sungguhan (PostgreSQL), bukan hanya
   SQLite: kunci idempoten ditolak di server, bukti absensi tidak bisa diubah.

Fixture-nya sintetis: nama staf, properti, dan jam di sini tidak mewakili siapa pun.
"""

from __future__ import annotations

import pathlib

import sqlite3
from typing import Any

import pytest

from primitives import FrozenClock, IdFactory
from workforce.attendance import AttendanceRecord, AttendanceWriter, IdempotencyKeyReused
from workforce.roster import RosterWriter, ShiftSpec
from workforce.tasks import AssignmentSpec, TaskAssignmentWriter
from workforce.timesheet import TimesheetWriter

AGENCY = "agency_default"
PROP_BALI = "prop_bali"
NOW = "2026-11-14T00:00:00Z"
MONDAY = "2026-11-16T00:00:00Z"


# --------------------------------------------------------------------------------------
# fixtures — satu dunia, dipakai bersama oleh keempat penulis
# --------------------------------------------------------------------------------------


def add_property(conn: sqlite3.Connection, property_id: str = PROP_BALI) -> str:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, 'Villa Uji', 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE',"
        " 'ACTIVATED', ?, ?)",
        (property_id, AGENCY, NOW, NOW),
    )
    return property_id


def add_staff(conn: sqlite3.Connection, staff_id: str = "stf_1") -> str:
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, home_property_id, weekly_contract_minutes, status, hired_on, ended_on,"
        " created_at, updated_at)"
        " VALUES (?, ?, 'EMP-1', 'Kadek Uji', 'HOUSEKEEPING', 'PERMANENT', ?, 2400, 'ACTIVE',"
        " '2026-01-01', NULL, ?, ?)",
        (staff_id, AGENCY, PROP_BALI, NOW, NOW),
    )
    return staff_id


def add_template(conn: sqlite3.Connection, template_id: str = "tpl_1") -> str:
    conn.execute(
        "INSERT INTO workforce_shift_templates (shift_template_id, agency_id, property_id,"
        " shift_code, starts_local, ends_local, effective_from)"
        " VALUES (?, ?, ?, 'MORNING', '07:00', '15:00', '2026-01-01')",
        (template_id, AGENCY, PROP_BALI),
    )
    return template_id


def add_task(conn: sqlite3.Connection, task_id: str = "task_1") -> str:
    conn.execute(
        "INSERT INTO ops_tasks (task_id, agency_id, kind, due_at, priority, status,"
        " idempotency_key, created_at, updated_at, completed_at, blocked_reason)"
        " VALUES (?, ?, 'CLEANING', '2026-11-20T10:00:00Z', 'NORMAL', 'OPEN', ?, ?, ?, NULL, NULL)",
        (task_id, AGENCY, f"seed-{task_id}", NOW, NOW),
    )
    return task_id


class World:
    """Empat penulis di atas satu koneksi — persis seperti satu proses aplikasi."""

    def __init__(self, conn: sqlite3.Connection, clock: Any, ids: IdFactory) -> None:
        self.conn = conn
        self.roster = RosterWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)
        self.attendance = AttendanceWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)
        self.timesheet = TimesheetWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)
        self.tasks = TaskAssignmentWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)


@pytest.fixture()
def world(conn):
    add_property(conn)
    add_staff(conn)
    add_template(conn)
    add_task(conn)
    clock = FrozenClock.at(MONDAY)
    return World(conn, clock, IdFactory(seed="g1"))


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def shift_spec(**overrides: Any) -> ShiftSpec:
    fields: dict[str, Any] = dict(
        staff_id="stf_1",
        shift_template_id="tpl_1",
        property_id=PROP_BALI,
        starts_at_utc="2026-11-19T23:30:00Z",
        ends_at_utc="2026-11-20T07:30:00Z",
    )
    fields.update(overrides)
    return ShiftSpec(**fields)


def attendance_spec(**overrides: Any) -> AttendanceRecord:
    fields: dict[str, Any] = dict(
        staff_id="stf_1",
        event_type="CLOCK_IN",
        occurred_at_utc="2026-11-15T23:29:00Z",
        idempotency_key="att-key-0001",
        actor_id="stf_1",
        source="DEVICE",
    )
    fields.update(overrides)
    return AttendanceRecord(**fields)


def task_spec(**overrides: Any) -> AssignmentSpec:
    fields: dict[str, Any] = dict(
        task_id="task_1",
        staff_id="stf_1",
        planned_minutes=120,
        assigned_by="spv_budi",
        idempotency_key="assign-key-0001",
    )
    fields.update(overrides)
    return AssignmentSpec(**fields)


# --------------------------------------------------------------------------------------
# 1–2. setiap penulis: baris yang sama, satu baris saja
# --------------------------------------------------------------------------------------


def test_roster_replay_returns_the_same_shift_and_one_row(world):
    first, created_first = world.roster.add_shift(shift_spec())
    second, created_second = world.roster.add_shift(shift_spec())

    assert created_first is True
    assert created_second is False, "kiriman ulang shift harus dilaporkan sebagai replay"
    assert second["roster_entry_id"] == first["roster_entry_id"]
    assert count(world.conn, "workforce_roster_entries") == 1


def test_attendance_replay_returns_the_same_event_and_one_row(world):
    first, created_first = world.attendance.record(attendance_spec())
    second, created_second = world.attendance.record(attendance_spec())

    assert created_first is True
    assert created_second is False
    assert second["attendance_event_id"] == first["attendance_event_id"]
    assert count(world.conn, "workforce_attendance_events") == 1


def test_timesheet_replay_keeps_one_row_for_the_period(world):
    first, created_first = world.timesheet.close("stf_1", "2026-11-16", "2026-11-22")
    second, created_second = world.timesheet.close("stf_1", "2026-11-16", "2026-11-22")

    assert created_first is True
    assert created_second is False
    assert second["timesheet_id"] == first["timesheet_id"]
    assert count(world.conn, "workforce_timesheets") == 1


def test_task_assignment_replay_returns_the_same_assignment(world):
    first, created_first = world.tasks.assign(task_spec())
    second, created_second = world.tasks.assign(task_spec())

    assert created_first is True
    assert created_second is False
    assert second["assignment_id"] == first["assignment_id"]
    assert count(world.conn, "workforce_task_assignments") == 1


def test_second_replay_does_not_consume_capacity_twice(world):
    """Kapasitas dihitung dari baris, jadi replay yang salah akan terlihat di sini."""
    world.tasks.assign(task_spec())
    used_once = world.tasks.capacity_used("stf_1", "2026-11-20T10:00:00Z")
    world.tasks.assign(task_spec())
    used_twice = world.tasks.capacity_used("stf_1", "2026-11-20T10:00:00Z")

    assert used_once == 120
    assert used_twice == used_once


# --------------------------------------------------------------------------------------
# 3–4. bukti absensi: koreksi menambah baris, baris lama tidak bisa disentuh
# --------------------------------------------------------------------------------------


def test_correction_is_a_new_row_and_leaves_the_original_untouched(world):
    original, _ = world.attendance.record(attendance_spec())

    correction, created = world.attendance.correct(
        original["attendance_event_id"],
        occurred_at_utc="2026-11-15T23:45:00Z",
        reason="tamper device clock",
        actor_id="spv_budi",
        idempotency_key="corr-key-0001",
    )

    assert created is True
    assert correction["attendance_event_id"] != original["attendance_event_id"]
    assert correction["supersedes_event_id"] == original["attendance_event_id"]
    assert correction["event_type"] == "MANUAL_ADJUST"

    # Baris asli utuh: jam dan tipenya tidak berubah sedikit pun.
    stored = world.conn.execute(
        "SELECT * FROM workforce_attendance_events WHERE attendance_event_id = ?", (original["attendance_event_id"],)
    ).fetchone()
    assert stored["occurred_at_utc"] == original["occurred_at_utc"]
    assert stored["event_type"] == "CLOCK_IN"
    assert count(world.conn, "workforce_attendance_events") == 2


def test_correction_replay_returns_the_same_correction(world):
    original, _ = world.attendance.record(attendance_spec())
    first, _ = world.attendance.correct(
        original["attendance_event_id"],
        occurred_at_utc="2026-11-15T23:45:00Z",
        reason="tamper device clock",
        actor_id="spv_budi",
        idempotency_key="corr-key-0001",
    )
    second, created = world.attendance.correct(
        original["attendance_event_id"],
        occurred_at_utc="2026-11-15T23:45:00Z",
        reason="tamper device clock",
        actor_id="spv_budi",
        idempotency_key="corr-key-0001",
    )

    assert created is False
    assert second["attendance_event_id"] == first["attendance_event_id"]
    assert count(world.conn, "workforce_attendance_events") == 2


def test_attendance_evidence_cannot_be_updated_or_deleted(world):
    row, _ = world.attendance.record(attendance_spec())

    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute(
            "UPDATE workforce_attendance_events SET event_type = 'CLOCK_OUT' WHERE attendance_event_id = ?",
            (row["attendance_event_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute(
            "DELETE FROM workforce_attendance_events WHERE attendance_event_id = ?", (row["attendance_event_id"],)
        )

    assert count(world.conn, "workforce_attendance_events") == 1


# --------------------------------------------------------------------------------------
# 5. matriks gerbang: semua jalur tulis dijalankan dua kali, lalu dihitung
# --------------------------------------------------------------------------------------


def test_g1_matrix_all_write_paths_are_idempotent_in_one_database(world):
    """Inti G1: satu basis data, empat penulis, dua putaran, hitungan tetap."""
    clock_out = attendance_spec(
        event_type="CLOCK_OUT",
        occurred_at_utc="2026-11-15T23:59:00Z",
        idempotency_key="att-key-0002",
    )
    for _ in range(2):
        world.roster.add_shift(shift_spec())
        world.attendance.record(attendance_spec())
        world.attendance.record(clock_out)
        world.timesheet.close("stf_1", "2026-11-16", "2026-11-22")
        world.tasks.assign(task_spec())

    observed = {
        "workforce_roster_entries": count(world.conn, "workforce_roster_entries"),
        "workforce_attendance_events": count(world.conn, "workforce_attendance_events"),
        "workforce_timesheets": count(world.conn, "workforce_timesheets"),
        "workforce_task_assignments": count(world.conn, "workforce_task_assignments"),
    }
    assert observed == {
        "workforce_roster_entries": 1,
        "workforce_attendance_events": 2,
        "workforce_timesheets": 1,
        "workforce_task_assignments": 1,
    }


def test_reused_key_with_different_content_is_rejected_not_overwritten(world):
    """Kunci yang sama untuk isi berbeda = salah pakai, bukan replay."""
    world.attendance.record(attendance_spec(event_type="CLOCK_IN"))

    # Ditemukan oleh gerbang ini: sebelumnya kunci yang dipakai ulang untuk isi
    # berbeda ditelan sebagai "replay" dan bukti pengganti hilang tanpa jejak.
    with pytest.raises(IdempotencyKeyReused) as excinfo:
        world.attendance.record(
            attendance_spec(event_type="CLOCK_OUT", occurred_at_utc="2026-11-15T23:40:00Z")
        )

    assert excinfo.value.code == "WORKFORCE_ATTENDANCE_IDEMPOTENCY_MISMATCH"
    assert "event_type" in excinfo.value.details["differing_fields"]
    assert count(world.conn, "workforce_attendance_events") == 1


# ---------------------------------------------------------------------------
# Sisi PostgreSQL: jaminan yang sama harus dipegang server sungguhan.
#
# SQLite dan PostgreSQL berbeda di satu titik yang mahal: ``ON CONFLICT`` SQLite
# mengandalkan indeks unik yang sama, tetapi tipe, collation, dan trigger bahasa
# PL/pgSQL adalah kode yang berbeda. Kalau kunci idempoten hanya diuji di SQLite,
# gerbang G1 membuktikan mesin yang tidak dipakai produksi.
#
# Di sini skema diterapkan ke *schema* privat (``gate_g1``) supaya tidak
# mengganggu ``public`` milik tes paritas, lalu dua jaminan diuji langsung di
# server: (a) kunci kiriman ganda ditolak, (b) bukti absensi menolak UPDATE dan
# DELETE. Kunci unik penulis lain (roster, timesheet, penugasan) diverifikasi
# keberadaannya di katalog server — perilaku tabel-tabel itu diuji di SQLite.
# ---------------------------------------------------------------------------

PG_SCHEMA = "gate_g1"


def pg_driver():
    """Driver PostgreSQL, diselesaikan sama seperti ``schema_loader.open_pg``.

    ``make check`` memakai ``/usr/bin/python3``; di host ini driver hanya ada di
    ``.venv``. Tanpa resolusi eksplisit, sisi PostgreSQL gerbang G1 gagal di
    import alih-alih melewatkan diri dengan jujur — atau lebih buruk, lulus
    tanpa pernah menyentuh server.
    """
    import importlib

    for name in ("psycopg", "psycopg2"):  # psycopg v3 dulu, sesuai requirements-dev
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def _pg_dsn() -> str | None:
    import os

    dsn = os.environ.get("VILLA_PG_DSN")
    if not dsn and os.environ.get("VILLA_PG_REQUIRED") == "1":
        raise RuntimeError(
            "VILLA_PG_REQUIRED=1 tetapi VILLA_PG_DSN tidak diisi: gerbang G1 tidak "
            "boleh lulus tanpa server PostgreSQL."
        )
    return dsn


@pytest.fixture
def pg_gate():
    """Skema privat di server PostgreSQL; dihapus lagi setelah tes."""
    dsn = _pg_dsn()
    if not dsn:
        pytest.skip("VILLA_PG_DSN tidak diset: sisi PostgreSQL gerbang G1 dilewati")

    import os

    psycopg = pg_driver()
    if psycopg is None:
        if os.environ.get("VILLA_PG_REQUIRED") == "1":
            raise RuntimeError("VILLA_PG_REQUIRED=1 tetapi driver psycopg tidak terpasang.")
        pytest.skip("driver psycopg tidak terpasang: sisi PostgreSQL gerbang G1 dilewati")

    from schema_loader import split_statements
    from test_pg_overlap_concurrency import disposable_database

    root = pathlib.Path(__file__).resolve().parents[1]
    # PACK-04A: the schema below is created inside a database this fixture owns, after the
    # server proved itself a disposable CI service. Nothing on the given DSN is dropped.
    with disposable_database(dsn) as owned_dsn:
        conn = psycopg.connect(owned_dsn, autocommit=True)
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE")
            conn.execute(f"CREATE SCHEMA {PG_SCHEMA}")
            conn.execute(f"SET search_path TO {PG_SCHEMA}, public")
            # Twin PostgreSQL adalah satu berkas gabungan seluruh skema SQLite;
            # memakai twin (bukan berkas SQLite) memastikan yang diuji adalah DDL
            # yang benar-benar dipakai server.
            text = (root / "src" / "schema" / "001_initial.pg.sql").read_text(encoding="utf-8")
            for statement in split_statements(text):
                conn.execute(statement)
            yield conn
        finally:
            conn.close()


AGENCY_ROW = ("agy_g1", "Gate G1 Agency", "ACTIVE", "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z")
STAFF_ROW = (
    "stf_g1", "agy_g1", "G1-001", "Staf Sintetis", "HOUSEKEEPING", "PERMANENT",
    None, 2400, "ACTIVE", "2026-01-05", None, "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z", 1,
)
ATTENDANCE_ROW = (
    "att_g1_1", "agy_g1", "stf_g1", None, None, "CLOCK_IN",
    "2026-11-15T23:00:00Z", "2026-11-15T23:00:05Z", "DEVICE", None, "INSIDE", 10,
    None, "stf_g1", "g1-key-0001", "2026-11-15T23:00:05Z",
)


def _seed_pg(conn) -> None:
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        AGENCY_ROW,
    )
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name,"
        " role_code, employment_type, home_property_id, weekly_contract_minutes, status,"
        " hired_on, ended_on, created_at, updated_at, version)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        STAFF_ROW,
    )
    conn.execute(
        "INSERT INTO workforce_attendance_events (attendance_event_id, agency_id, staff_id,"
        " roster_entry_id, supersedes_event_id, event_type, occurred_at_utc, recorded_at_utc,"
        " source, device_ref, geofence_verdict, accuracy_bucket_m, evidence_ref, actor_id,"
        " idempotency_key, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        ATTENDANCE_ROW,
    )


def test_postgres_rejects_second_row_for_the_same_idempotency_key(pg_gate):
    """Kunci kiriman yang sama dua kali: server harus menolak, bukan menimpa."""
    driver = pg_driver()

    _seed_pg(pg_gate)

    duplicate = ("att_g1_2",) + ATTENDANCE_ROW[1:]
    with pytest.raises(getattr(driver.errors, "UniqueViolation")):
        pg_gate.execute(
            "INSERT INTO workforce_attendance_events (attendance_event_id, agency_id, staff_id,"
            " roster_entry_id, supersedes_event_id, event_type, occurred_at_utc, recorded_at_utc,"
            " source, device_ref, geofence_verdict, accuracy_bucket_m, evidence_ref, actor_id,"
            " idempotency_key, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            duplicate,
        )

    rows = pg_gate.execute("SELECT count(*) FROM workforce_attendance_events").fetchone()[0]
    assert rows == 1


def test_postgres_refuses_to_mutate_attendance_evidence(pg_gate):
    """Bukti absensi bersifat append-only juga di server sungguhan."""
    driver = pg_driver()

    _seed_pg(pg_gate)

    with pytest.raises(driver.Error):
        pg_gate.execute(
            "UPDATE workforce_attendance_events SET event_type = 'CLOCK_OUT'"
            " WHERE attendance_event_id = 'att_g1_1'"
        )
    with pytest.raises(driver.Error):
        pg_gate.execute(
            "DELETE FROM workforce_attendance_events WHERE attendance_event_id = 'att_g1_1'"
        )

    row = pg_gate.execute(
        "SELECT event_type, idempotency_key FROM workforce_attendance_events"
    ).fetchone()
    assert row == ("CLOCK_IN", "g1-key-0001")


def test_postgres_schema_holds_the_other_writer_keys(pg_gate):
    """Kunci unik penulis lain harus ada di katalog server, bukan hanya di SQLite."""
    expected = {
        "uq_workforce_roster_start",
        "uq_workforce_attendance_idem",
        "uq_workforce_timesheets_period",
        "uq_workforce_assignment_idem",
        "uq_workforce_assignment_once",
    }
    rows = pg_gate.execute(
        "SELECT conname FROM pg_constraint c"
        " JOIN pg_namespace n ON n.oid = c.connamespace"
        " WHERE n.nspname = %s AND c.contype = 'u'",
        (PG_SCHEMA,),
    ).fetchall()
    present = {r[0] for r in rows}
    assert expected <= present, f"kunci hilang di PostgreSQL: {sorted(expected - present)}"
