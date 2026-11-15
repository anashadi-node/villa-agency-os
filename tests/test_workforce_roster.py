"""Jadwal shift — penulis roster yang menolak orang di dua tempat sekaligus (T1.2).

Empat janji yang dikunci di sini, semuanya berujung pada uang dan keselamatan tamu:

* satu orang tidak pernah punya dua shift hidup yang beririsan, **lintas properti**;
* shift bersambung (15:00 selesai, 15:00 mulai) tetap sah — rentangnya setengah terbuka;
* ``work_date`` mengikuti tanggal properti, bukan tanggal server UTC;
* DRAFT ditulis di satu tempat, publikasi adalah tindakan manusia berlabel di tempat lain.

Fixture-nya sintetis: nama, properti, dan jam di sini tidak mewakili siapa pun.
"""

from __future__ import annotations

import pytest

from primitives import DomainError, EntityNotFound
from workforce.roster import (
    RosterDuplicateStart,
    RosterError,
    RosterOverlap,
    RosterWriter,
    ShiftSpec,
    StaffNotBookable,
)

AGENCY = "agency_default"
NOW = "2026-11-14T00:00:00Z"
PROP_BALI = "prop_bali"
PROP_YOGYA = "prop_yogya"


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def add_property(conn, property_id: str, timezone: str = "Asia/Makassar", province: str = "Bali") -> str:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, 'Villa Uji', 'villa', 'Ubud', ?, ?, 'SELLABLE', 'ACTIVATED', ?, ?)",
        (property_id, AGENCY, province, timezone, NOW, NOW),
    )
    return property_id


def add_staff(conn, staff_id: str = "stf_1", employee_no: str = "EMP-1", status: str = "ACTIVE",
              property_id: str = PROP_BALI) -> str:
    ended_on = "2026-11-01" if status == "OFFBOARDED" else None
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, home_property_id, status, hired_on, ended_on, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Kadek Uji', 'HOUSEKEEPING', 'PERMANENT', ?, ?, '2026-01-01', ?, ?, ?)",
        (staff_id, AGENCY, employee_no, property_id, status, ended_on, NOW, NOW),
    )
    return staff_id


def add_template(conn, template_id: str = "tpl_1", property_id: str | None = PROP_BALI) -> str:
    conn.execute(
        "INSERT INTO workforce_shift_templates (shift_template_id, agency_id, property_id,"
        " shift_code, starts_local, ends_local, effective_from)"
        " VALUES (?, ?, ?, 'MORNING', '07:00', '15:00', '2026-01-01')",
        (template_id, AGENCY, property_id),
    )
    return template_id


@pytest.fixture()
def world(conn, clock, ids):
    """Satu agensi, dua properti (Bali WITA, Yogyakarta WIB), satu staf aktif."""
    add_property(conn, PROP_BALI)
    add_property(conn, PROP_YOGYA, timezone="Asia/Jakarta", province="DI Yogyakarta")
    add_staff(conn)
    add_template(conn, "tpl_bali", PROP_BALI)
    add_template(conn, "tpl_yogya", PROP_YOGYA)
    return RosterWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)


def spec(starts_at_utc: str = "2026-11-19T23:30:00Z", ends_at_utc: str = "2026-11-20T07:30:00Z",
         **overrides) -> ShiftSpec:
    fields = {
        "staff_id": "stf_1",
        "shift_template_id": "tpl_bali",
        "property_id": PROP_BALI,
        "starts_at_utc": starts_at_utc,
        "ends_at_utc": ends_at_utc,
    }
    fields.update(overrides)
    return ShiftSpec(**fields)


def count_entries(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM workforce_roster_entries").fetchone()[0]


# --------------------------------------------------------------------------------------
# penulisan dasar
# --------------------------------------------------------------------------------------


def test_add_shift_stores_a_draft_with_property_local_work_date(world, conn):
    row, created = world.add_shift(spec())

    assert created is True
    assert row["status"] == "DRAFT"
    assert row["published_by"] is None
    assert row["published_at"] is None
    # 23:30 UTC tanggal 19 adalah 07:30 WITA tanggal 20 — tanggal properti, bukan UTC.
    assert row["work_date"] == "2026-11-20"
    assert row["starts_at_utc"] == "2026-11-19T23:30:00+00:00"
    assert row["ends_at_utc"] == "2026-11-20T07:30:00+00:00"
    assert count_entries(conn) == 1


def test_work_date_uses_yogyakarta_clock_not_the_server_clock(world, conn):
    """17:30 UTC tanggal 19 = 00:30 WIB tanggal 20. Server UTC akan menulis tanggal 19."""
    add_staff(conn, "stf_2", "EMP-2", property_id=PROP_YOGYA)
    row, _ = world.add_shift(
        spec(staff_id="stf_2", shift_template_id="tpl_yogya", property_id=PROP_YOGYA,
             starts_at_utc="2026-11-19T17:30:00Z", ends_at_utc="2026-11-20T01:30:00Z")
    )

    assert row["work_date"] == "2026-11-20"


def test_replaying_the_same_shift_returns_the_existing_row_and_creates_nothing(world, conn):
    first, created = world.add_shift(spec())
    replay, replay_created = world.add_shift(spec())

    assert created is True
    assert replay_created is False
    assert replay["roster_entry_id"] == first["roster_entry_id"]
    assert replay["status"] == first["status"]
    assert replay["updated_at"] == first["updated_at"]
    assert count_entries(conn) == 1


def test_same_start_with_different_content_is_a_duplicate_start(world):
    world.add_shift(spec())

    with pytest.raises(RosterDuplicateStart) as caught:
        world.add_shift(spec(ends_at_utc="2026-11-20T09:00:00Z"))

    assert caught.value.code == "WORKFORCE_ROSTER_DUPLICATE_START"
    assert caught.value.details["staff_id"] == "stf_1"


# --------------------------------------------------------------------------------------
# bentrok waktu
# --------------------------------------------------------------------------------------


def test_overlapping_shift_is_refused_even_at_another_property(world, conn):
    world.add_shift(spec())

    with pytest.raises(RosterOverlap) as caught:
        world.add_shift(
            spec(shift_template_id="tpl_yogya", property_id=PROP_YOGYA,
                 starts_at_utc="2026-11-20T06:00:00Z", ends_at_utc="2026-11-20T14:00:00Z")
        )

    assert caught.value.code == "WORKFORCE_ROSTER_OVERLAP"
    assert caught.value.details["property_id"] == PROP_BALI
    assert count_entries(conn) == 1


def test_back_to_back_shifts_are_allowed_because_the_window_is_half_open(world, conn):
    world.add_shift(spec())
    second, created = world.add_shift(spec(starts_at_utc="2026-11-20T07:30:00Z",
                                           ends_at_utc="2026-11-20T15:30:00Z"))

    assert created is True
    assert second["work_date"] == "2026-11-20"
    assert count_entries(conn) == 2


def test_overlap_inside_one_batch_is_refused_before_anything_is_written(world, conn):
    with pytest.raises(RosterOverlap):
        world.add_shifts([
            spec(starts_at_utc="2026-11-20T00:00:00Z", ends_at_utc="2026-11-20T08:00:00Z"),
            spec(starts_at_utc="2026-11-20T04:00:00Z", ends_at_utc="2026-11-20T12:00:00Z"),
        ])

    assert count_entries(conn) == 0


def test_batch_with_one_bad_row_writes_nothing_at_all(world, conn):
    add_staff(conn, "stf_2", "EMP-2")

    with pytest.raises(EntityNotFound):
        world.add_shifts([
            spec(starts_at_utc="2026-11-20T00:00:00Z", ends_at_utc="2026-11-20T08:00:00Z"),
            spec(staff_id="stf_missing", starts_at_utc="2026-11-20T00:00:00Z",
                 ends_at_utc="2026-11-20T08:00:00Z"),
        ])

    assert count_entries(conn) == 0


# --------------------------------------------------------------------------------------
# validasi jadwal
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["SUSPENDED", "OFFBOARDED"])
def test_staff_who_is_not_active_cannot_be_rostered(world, conn, status):
    add_staff(conn, "stf_x", "EMP-X", status=status)

    with pytest.raises(StaffNotBookable) as caught:
        world.add_shift(spec(staff_id="stf_x"))

    assert caught.value.code == "WORKFORCE_STAFF_NOT_BOOKABLE"
    assert caught.value.details["status"] == status


def test_unknown_staff_is_refused(world):
    with pytest.raises(EntityNotFound):
        world.add_shift(spec(staff_id="stf_ghost"))


def test_unknown_property_is_refused(world, conn):
    add_template(conn, "tpl_any", None)  # template tanpa properti: validasi berhenti di properti
    with pytest.raises(EntityNotFound):
        world.add_shift(spec(property_id="prop_ghost", shift_template_id="tpl_any"))


def test_template_belonging_to_a_different_property_is_refused(world):
    with pytest.raises(RosterError) as caught:
        world.add_shift(spec(shift_template_id="tpl_yogya"))

    assert caught.value.code == "WORKFORCE_TEMPLATE_PROPERTY_MISMATCH"
    assert caught.value.details["template_property_id"] == PROP_YOGYA


def test_explicit_work_date_that_disagrees_with_the_property_clock_is_refused(world):
    with pytest.raises(RosterError) as caught:
        world.add_shift(spec(work_date="2026-11-19"))

    assert caught.value.code == "WORKFORCE_WORK_DATE_MISMATCH"
    assert caught.value.details["expected"] == "2026-11-20"


def test_explicit_work_date_that_matches_is_accepted(world):
    row, _ = world.add_shift(spec(work_date="2026-11-20"))
    assert row["work_date"] == "2026-11-20"


def test_naive_datetime_is_refused(world):
    """Jadwal tanpa zona waktu adalah jadwal yang salah dibaca 8 jam."""
    with pytest.raises(DomainError) as caught:
        world.add_shift(spec(starts_at_utc="2026-11-19T23:30:00"))

    assert caught.value.code == "NAIVE_TIMESTAMP"


def test_shift_that_ends_before_it_starts_is_refused(world):
    with pytest.raises(RosterError) as caught:
        world.add_shift(spec(starts_at_utc="2026-11-20T08:00:00Z", ends_at_utc="2026-11-20T08:00:00Z"))

    assert caught.value.code == "WORKFORCE_INVALID_WINDOW"


def test_unknown_status_is_refused(world):
    with pytest.raises(RosterError) as caught:
        world.add_shift(spec(status="APPROVED"))

    assert caught.value.code == "WORKFORCE_INVALID_STATUS"


@pytest.mark.parametrize("status", ["PUBLISHED", "CONFIRMED", "SWAPPED", "CANCELLED", "NO_SHOW"])
def test_add_shift_writes_draft_only(world, conn, status):
    with pytest.raises(RosterError) as caught:
        world.add_shift(spec(status=status, reason="cuti mendadak"))

    assert caught.value.code == "WORKFORCE_STATUS_NOT_DRAFT"
    assert count_entries(conn) == 0


# --------------------------------------------------------------------------------------
# publikasi
# --------------------------------------------------------------------------------------


def test_publish_shift_records_the_human_who_published_it(world):
    row, _ = world.add_shift(spec())
    published = world.publish_shift(row["roster_entry_id"], published_by="sup_1")

    assert published["status"] == "PUBLISHED"
    assert published["published_by"] == "sup_1"
    assert published["published_at"] is not None
    # Satu tindakan manusia = satu cap waktu; dua pembacaan jam bisa berbeda mikrodetik.
    assert published["published_at"] == published["updated_at"]
    assert published["published_at"] == "2026-09-21T08:00:00+00:00"
    assert published["updated_at"] == "2026-09-21T08:00:00+00:00"


def test_publishing_twice_is_a_replay_not_a_new_write(world):
    row, _ = world.add_shift(spec())
    first = world.publish_shift(row["roster_entry_id"], published_by="sup_1")
    second = world.publish_shift(row["roster_entry_id"], published_by="sup_1")

    assert second == first


def test_a_cancelled_shift_cannot_be_published(world):
    row, _ = world.add_shift(spec())
    world.cancel_shift(row["roster_entry_id"], reason="villa tutup")

    with pytest.raises(RosterError) as caught:
        world.publish_shift(row["roster_entry_id"], published_by="sup_1")

    assert caught.value.code == "WORKFORCE_CANCELLED_NOT_PUBLISHABLE"


def test_publish_requires_an_actor(world):
    row, _ = world.add_shift(spec())

    with pytest.raises(DomainError):
        world.publish_shift(row["roster_entry_id"], published_by="   ")


def test_publish_of_an_unknown_shift_is_refused(world):
    with pytest.raises(EntityNotFound):
        world.publish_shift("roster_ghost", published_by="sup_1")


# --------------------------------------------------------------------------------------
# pembatalan
# --------------------------------------------------------------------------------------


def test_cancel_shift_records_the_reason_and_frees_the_window(world, conn):
    row, _ = world.add_shift(spec())
    cancelled = world.cancel_shift(row["roster_entry_id"], reason="tamu batal masuk")

    assert cancelled["status"] == "CANCELLED"
    assert cancelled["reason"] == "tamu batal masuk"

    # Slot yang dibebaskan adalah rentang waktunya, bukan detik mulainya:
    # baris batal tetap disimpan sebagai jejak audit dan mengunci jam mulainya.
    replacement, created = world.add_shift(
        spec(starts_at_utc="2026-11-20T02:00:00Z", ends_at_utc="2026-11-20T10:00:00Z")
    )
    assert created is True
    assert replacement["roster_entry_id"] != row["roster_entry_id"]
    assert count_entries(conn) == 2


def test_the_cancelled_start_instant_stays_reserved_for_the_audit_trail(world, conn):
    row, _ = world.add_shift(spec())
    world.cancel_shift(row["roster_entry_id"], reason="villa tutup")

    with pytest.raises(RosterDuplicateStart):
        world.add_shift(spec())

    assert count_entries(conn) == 1


def test_cancelling_twice_is_a_replay(world):
    row, _ = world.add_shift(spec())
    first = world.cancel_shift(row["roster_entry_id"], reason="villa tutup")
    second = world.cancel_shift(row["roster_entry_id"], reason="villa tutup")

    assert second == first


def test_cancelled_shifts_stay_out_of_the_blocking_read(world):
    row, _ = world.add_shift(spec())
    world.cancel_shift(row["roster_entry_id"], reason="villa tutup")

    assert world.shifts_for_staff("stf_1") == []


def test_cancel_reason_must_explain_itself(world):
    row, _ = world.add_shift(spec())

    with pytest.raises(RosterError) as caught:
        world.cancel_shift(row["roster_entry_id"], reason="x")

    assert caught.value.code == "WORKFORCE_REASON_TOO_SHORT"


def test_cancel_of_an_unknown_shift_is_refused(world):
    with pytest.raises(EntityNotFound):
        world.cancel_shift("roster_ghost", reason="villa tutup")


def test_shifts_for_staff_returns_only_live_shifts_in_start_order(world):
    late, _ = world.add_shift(spec(starts_at_utc="2026-11-21T00:00:00Z",
                                   ends_at_utc="2026-11-21T08:00:00Z"))
    early, _ = world.add_shift(spec(starts_at_utc="2026-11-20T00:00:00Z",
                                    ends_at_utc="2026-11-20T08:00:00Z"))

    ordered = world.shifts_for_staff("stf_1")

    assert [row["roster_entry_id"] for row in ordered] == [early["roster_entry_id"],
                                                           late["roster_entry_id"]]
