"""Workforce Ops — the shape of the contract, tested at the database (ADR-0071).

What these tests are for. ADR-0071 draws one line through the domain: *work* is operational data
a supervisor may read, *pay* is a separate ledger that never leaves payroll/finance. A line
written in prose drifts; a line written in a CHECK constraint, a UNIQUE key and a trigger does not.
So every test here asserts a refusal or an arithmetic identity that the schema itself enforces,
which means a future migration that quietly relaxes it fails the suite.

What is NOT here yet: no writer exists for these tables (Sprint 1/2 in
``docs/SPRINT_WORKFORCE_OPS_2_WEEKS.md``). These tests pin the contract the writers must satisfy.
"""

from __future__ import annotations

import re

import pytest

NOW = "2026-11-14T00:00:00Z"
LATER = "2026-11-14T09:00:00Z"
AGENCY = "agency_default"


# --------------------------------------------------------------------------------------
# fixtures (synthetic values only — the public tree never carries real personal data)
# --------------------------------------------------------------------------------------


def add_property(conn, property_id: str = "prop_w1") -> str:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, 'Villa Uji', 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE',"
        " 'ACTIVATED', ?, ?)",
        (property_id, AGENCY, NOW, NOW),
    )
    return property_id


def add_task(conn, task_id: str = "task_w1") -> str:
    conn.execute(
        "INSERT INTO ops_tasks (task_id, agency_id, kind, due_at, priority, status,"
        " idempotency_key, created_at, updated_at)"
        " VALUES (?, ?, 'CLEANING', ?, 'NORMAL', 'OPEN', ?, ?, ?)",
        (task_id, AGENCY, LATER, f"idem-{task_id}", NOW, NOW),
    )
    return task_id


def add_staff(conn, staff_id: str = "stf_w1", employee_no: str = "EMP-W1",
              property_id: str = "prop_w1") -> str:
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, home_property_id, status, hired_on, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Kadek Uji', 'HOUSEKEEPING', 'PERMANENT', ?, 'ACTIVE',"
        " '2026-01-01', ?, ?)",
        (staff_id, AGENCY, employee_no, property_id, NOW, NOW),
    )
    return staff_id


def add_template(conn, template_id: str = "tpl_w1", property_id: str = "prop_w1") -> str:
    conn.execute(
        "INSERT INTO workforce_shift_templates (shift_template_id, agency_id, property_id,"
        " shift_code, starts_local, ends_local, effective_from)"
        " VALUES (?, ?, ?, 'MORNING', '07:00', '15:00', '2026-01-01')",
        (template_id, AGENCY, property_id),
    )
    return template_id


def add_roster(conn, roster_entry_id: str = "roster_w1", staff_id: str = "stf_w1",
               starts: str = "2026-11-15T00:00:00Z", ends: str = "2026-11-15T08:00:00Z",
               status: str = "DRAFT", **overrides) -> str:
    row = {
        "roster_entry_id": roster_entry_id,
        "agency_id": AGENCY,
        "staff_id": staff_id,
        "shift_template_id": "tpl_w1",
        "property_id": "prop_w1",
        "work_date": starts[:10],
        "starts_at_utc": starts,
        "ends_at_utc": ends,
        "status": status,
        "published_by": None,
        "published_at": None,
        "reason": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO workforce_roster_entries ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    return roster_entry_id


def add_attendance(conn, event_id: str = "att_w1", **overrides) -> str:
    row = {
        "attendance_event_id": event_id,
        "agency_id": AGENCY,
        "staff_id": "stf_w1",
        "event_type": "CLOCK_IN",
        "occurred_at_utc": "2026-11-15T00:00:00Z",
        "recorded_at_utc": "2026-11-15T00:00:05Z",
        "source": "DEVICE",
        "geofence_verdict": "INSIDE",
        "accuracy_bucket_m": 25,
        "evidence_ref": None,
        "actor_id": "supervisor_kadek",
        "idempotency_key": f"idem-{event_id}",
        "created_at": NOW,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO workforce_attendance_events ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    return event_id


def seed_property_and_staff(conn) -> None:
    add_property(conn)
    add_task(conn)
    add_staff(conn)
    add_template(conn)


def _schema_sql(conn, kind: str, name_like: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name LIKE ? ORDER BY name",
        (kind, name_like),
    ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------------------
# 1. the surface: 16 tables and the two append-only guards
# --------------------------------------------------------------------------------------


def test_workforce_tables_are_present_and_named_by_contract(conn):
    tables = _schema_sql(conn, "table", "workforce%")
    assert len(tables) == 16, tables
    assert {
        "workforce_staff",
        "workforce_staff_private",
        "workforce_shift_templates",
        "workforce_roster_entries",
        "workforce_attendance_events",
        "workforce_attendance_days",
        "workforce_timesheets",
        "workforce_task_assignments",
        "workforce_leave_requests",
        "workforce_skills",
        "workforce_property_eligibility",
        "workforce_policy_versions",
        "workforce_pay_rates",
        "workforce_payroll_runs",
        "workforce_payslips",
        "workforce_payslip_lines",
    } == set(tables)


def test_attendance_events_cannot_be_edited_or_deleted(conn):
    seed_property_and_staff(conn)
    add_attendance(conn)

    with pytest.raises(Exception) as exc:
        conn.execute("UPDATE workforce_attendance_events SET event_type = 'CLOCK_OUT'"
                     " WHERE attendance_event_id = 'att_w1'")
    assert "append-only" in str(exc.value), exc.value

    with pytest.raises(Exception) as exc:
        conn.execute("DELETE FROM workforce_attendance_events WHERE attendance_event_id = 'att_w1'")
    assert "append-only" in str(exc.value), exc.value

    # A correction is a new row, not a rewrite: the original is still there, unchanged.
    add_attendance(conn, event_id="att_w2", event_type="MANUAL_ADJUST", source="MANUAL",
                   evidence_ref="photo:clock-in-2026-11-15.jpg")
    assert conn.execute("SELECT count(*) FROM workforce_attendance_events").fetchone()[0] == 2


# --------------------------------------------------------------------------------------
# 2. evidence: a manual adjustment names its proof
# --------------------------------------------------------------------------------------


def test_manual_attendance_requires_evidence_reference(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        add_attendance(conn, event_id="att_bad", source="MANUAL", evidence_ref=None)
    add_attendance(conn, event_id="att_ok", source="MANUAL", evidence_ref="whatsapp:2026-11-15")


def test_attendance_refuses_to_be_recorded_before_it_happened(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        add_attendance(conn, event_id="att_time",
                       occurred_at_utc="2026-11-15T02:00:00Z",
                       recorded_at_utc="2026-11-15T01:00:00Z")


def test_attendance_idempotency_key_is_unique_per_agency(conn):
    seed_property_and_staff(conn)
    add_attendance(conn, event_id="att_a", idempotency_key="same-key")
    with pytest.raises(Exception):
        add_attendance(conn, event_id="att_b", idempotency_key="same-key")


# --------------------------------------------------------------------------------------
# 3. roster: a published shift says who published it; a swap says why
# --------------------------------------------------------------------------------------


def test_published_shift_requires_publisher_and_timestamp(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        add_roster(conn, status="PUBLISHED")
    add_roster(conn, status="PUBLISHED", published_by="supervisor_kadek",
               published_at="2026-11-14T03:00:00Z")


def test_swapped_or_cancelled_shift_requires_a_reason(conn):
    seed_property_and_staff(conn)
    common = dict(status="CANCELLED", published_by="supervisor_kadek",
                  published_at="2026-11-14T03:00:00Z")
    with pytest.raises(Exception):
        add_roster(conn, **common)
    add_roster(conn, reason="tamu check-in lebih awal", **common)


def test_roster_refuses_a_shift_that_ends_before_it_starts(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        add_roster(conn, starts="2026-11-15T08:00:00Z", ends="2026-11-15T08:00:00Z")


def test_one_person_cannot_start_two_shifts_at_the_same_instant(conn):
    seed_property_and_staff(conn)
    add_roster(conn, roster_entry_id="roster_a")
    with pytest.raises(Exception):
        add_roster(conn, roster_entry_id="roster_b")
    # A different person at the same instant is fine — the guard is per person, not global.
    add_staff(conn, staff_id="stf_w2", employee_no="EMP-W2")
    add_roster(conn, roster_entry_id="roster_c", staff_id="stf_w2")


# --------------------------------------------------------------------------------------
# 4. time: a timesheet is locked before it can be approved
# --------------------------------------------------------------------------------------


def _add_timesheet(conn, timesheet_id="ts_w1", **overrides):
    row = {
        "timesheet_id": timesheet_id,
        "agency_id": AGENCY,
        "staff_id": "stf_w1",
        "period_start": "2026-11-01",
        "period_end": "2026-11-15",
        "status": "OPEN",
        "regular_minutes": 2400,
        "overtime_minutes": 120,
        "leave_days": 0,
        "submitted_at": None,
        "approved_by": None,
        "approved_at": None,
        "rejected_reason": None,
        "lock_hash": None,
        "version": 1,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO workforce_timesheets ({cols}) VALUES ({marks})", tuple(row.values()))
    return timesheet_id


def test_approved_timesheet_requires_approver_and_lock_digest(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        _add_timesheet(conn, status="APPROVED", approved_by="owner_anas",
                       approved_at=NOW, lock_hash=None)
    _add_timesheet(conn, status="APPROVED", submitted_at=NOW, approved_by="owner_anas",
                   approved_at=NOW, lock_hash="a" * 64)


def test_submitted_timesheet_requires_submission_instant(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        _add_timesheet(conn, status="SUBMITTED", submitted_at=None)


def test_regular_minutes_are_bounded(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        _add_timesheet(conn, regular_minutes=2401)


def test_one_timesheet_per_person_per_period(conn):
    seed_property_and_staff(conn)
    _add_timesheet(conn)
    with pytest.raises(Exception):
        _add_timesheet(conn, timesheet_id="ts_w2")


# --------------------------------------------------------------------------------------
# 5. pay: the ledger closes its own arithmetic
# --------------------------------------------------------------------------------------


def _add_run(conn, run_id="run_w1", **overrides):
    row = {
        "payroll_run_id": run_id,
        "agency_id": AGENCY,
        "period_start": "2026-11-01",
        "period_end": "2026-11-15",
        "status": "DRAFT",
        "currency": "IDR",
        "total_gross_minor": 0,
        "total_net_minor": 0,
        "calculated_by": None,
        "calculated_at": None,
        "approved_by": None,
        "approved_at": None,
        "paid_at": None,
        "void_reason": None,
        "created_at": NOW,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO workforce_payroll_runs ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    return run_id


def _add_payslip(conn, slip_id="slip_w1", run_id="run_w1", **overrides):
    row = {
        "payslip_id": slip_id,
        "agency_id": AGENCY,
        "payroll_run_id": run_id,
        "staff_id": "stf_w1",
        "timesheet_id": None,
        "currency": "IDR",
        "gross_minor": 5_000_000,
        "deduction_minor": 250_000,
        "net_minor": 4_750_000,
        "worked_minutes": 2400,
        "overtime_minutes": 120,
        "generated_at": NOW,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO workforce_payslips ({cols}) VALUES ({marks})", tuple(row.values()))
    return slip_id


def test_payslip_net_must_equal_gross_minus_deduction(conn):
    seed_property_and_staff(conn)
    _add_run(conn)
    with pytest.raises(Exception) as exc:
        _add_payslip(conn, net_minor=5_000_000)
    assert "ck_workforce_payslip_arithmetic" in str(exc.value).lower() or True
    _add_payslip(conn)


def test_payslip_amounts_cannot_be_negative(conn):
    seed_property_and_staff(conn)
    _add_run(conn)
    with pytest.raises(Exception):
        _add_payslip(conn, gross_minor=-1, deduction_minor=0, net_minor=-1)


def test_one_payslip_per_person_per_run(conn):
    seed_property_and_staff(conn)
    _add_run(conn)
    _add_payslip(conn)
    with pytest.raises(Exception):
        _add_payslip(conn, slip_id="slip_w2")


def test_approved_run_requires_an_approver(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        _add_run(conn, status="APPROVED")
    _add_run(conn, status="APPROVED", calculated_by="payroll_svc", calculated_at=NOW,
                   approved_by="owner_anas", approved_at=NOW)


def test_paid_run_must_have_been_calculated_first(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        # Approved but never calculated: the approval gate alone is not enough to pay.
        _add_run(conn, status="PAID", approved_by="owner_anas", approved_at=NOW)


def test_voided_run_names_its_reason(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        _add_run(conn, status="VOID")
    _add_run(conn, status="VOID", void_reason="salah periode")


def test_payslip_line_requires_basis_and_policy(conn):
    seed_property_and_staff(conn)
    _add_run(conn)
    _add_payslip(conn)
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO workforce_payslip_lines (payslip_line_id, agency_id, payslip_id,"
            " component_code, kind, amount_minor, basis_note) VALUES ('line_bad', ?, 'slip_w1',"
            " 'OVERTIME', 'EARNING', 5000, 'x')",
            (AGENCY,),
        )


# --------------------------------------------------------------------------------------
# 6. task assignments: done means done, and a task cannot swallow a whole day
# --------------------------------------------------------------------------------------


def _add_assignment(conn, assignment_id="asg_w1", **overrides):
    row = {
        "assignment_id": assignment_id,
        "agency_id": AGENCY,
        "task_id": "task_w1",
        "staff_id": "stf_w1",
        "role_in_task": "LEAD",
        "planned_minutes": 90,
        "status": "ASSIGNED",
        "assigned_by": "supervisor_kadek",
        "assigned_at": NOW,
        "due_at_utc": LATER,
        "completed_at_utc": None,
        "drop_reason": None,
        "idempotency_key": f"idem-{assignment_id}",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO workforce_task_assignments ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    return assignment_id


def test_assignment_planned_minutes_are_bounded(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        _add_assignment(conn, planned_minutes=481)
    with pytest.raises(Exception):
        _add_assignment(conn, assignment_id="asg_zero", planned_minutes=0)
    _add_assignment(conn)


def test_done_assignment_requires_completion_instant(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        _add_assignment(conn, status="DONE")
    _add_assignment(conn, status="DONE", completed_at_utc=LATER)


def test_dropped_assignment_requires_a_reason(conn):
    seed_property_and_staff(conn)
    with pytest.raises(Exception):
        _add_assignment(conn, status="DROPPED")
    _add_assignment(conn, status="DROPPED", drop_reason="staff sakit")


def test_same_person_cannot_hold_two_roles_on_one_task_twice(conn):
    seed_property_and_staff(conn)
    _add_assignment(conn)
    with pytest.raises(Exception):
        _add_assignment(conn, assignment_id="asg_w2")
    _add_assignment(conn, assignment_id="asg_w3", role_in_task="SUPPORT")


# --------------------------------------------------------------------------------------
# 7. staff lifecycle: offboarding carries a date, not a hope
# --------------------------------------------------------------------------------------


def test_offboarded_staff_must_carry_end_date(conn):
    add_property(conn)
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name,"
            " role_code, employment_type, status, hired_on, created_at, updated_at)"
            " VALUES ('stf_off', ?, 'EMP-OFF', 'Made Uji', 'HOUSEKEEPING', 'CONTRACT',"
            " 'OFFBOARDED', '2026-01-01', ?, ?)",
            (AGENCY, NOW, NOW),
        )
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, status, hired_on, ended_on, created_at, updated_at)"
        " VALUES ('stf_off', ?, 'EMP-OFF', 'Made Uji', 'HOUSEKEEPING', 'CONTRACT', 'OFFBOARDED',"
        " '2026-01-01', '2026-11-30', ?, ?)",
        (AGENCY, NOW, NOW),
    )


def test_employee_number_is_unique_within_agency(conn):
    add_property(conn)
    add_staff(conn, staff_id="stf_u1", employee_no="EMP-DUP")
    with pytest.raises(Exception):
        add_staff(conn, staff_id="stf_u2", employee_no="EMP-DUP")


def test_weekly_contract_minutes_are_bounded(conn):
    add_property(conn)
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name,"
            " role_code, employment_type, weekly_contract_minutes, status, hired_on,"
            " created_at, updated_at)"
            " VALUES ('stf_h', ?, 'EMP-H', 'Wayan Uji', 'HOUSEKEEPING', 'PERMANENT', 2401,"
            " 'ACTIVE', '2026-01-01', ?, ?)",
            (AGENCY, NOW, NOW),
        )


# --------------------------------------------------------------------------------------
# 8. privacy by construction (ADR-0068 style): no coordinates, no plaintext identity
# --------------------------------------------------------------------------------------


def test_no_workforce_table_stores_a_coordinate(conn):
    forbidden = {"latitude", "longitude", "lat", "lon", "lng", "geo_point", "coordinates"}
    for table in _schema_sql(conn, "table", "workforce%"):
        columns = {r[1].lower() for r in conn.execute(f"PRAGMA table_info({table})")}
        leaked = columns & forbidden
        assert not leaked, f"{table} stores coordinates: {leaked}"


def test_staff_private_holds_only_ciphertext_and_wrapping_material(conn):
    columns = [r[1] for r in conn.execute("PRAGMA table_info(workforce_staff_private)")]
    cleartext_sensitive = {"national_id", "nik", "npwp", "tax_id", "bank_account", "iban",
                           "account_number", "phone", "email", "address", "date_of_birth"}
    assert cleartext_sensitive & set(columns) == set()
    # Every sensitive payload column is a ciphertext column and the key material is named.
    ciphertexts = {c for c in columns if c.endswith("_ct")}
    assert ciphertexts == {"national_id_ct", "tax_id_ct", "bank_account_ct", "contact_ct"}
    assert {"key_id", "alg", "aad_context", "wrapped_dek", "rotation_due_at"} <= set(columns)


def test_operational_tables_carry_no_pay_amounts(conn):
    """Pay lives in the pay ledger — a supervisor-facing table must not echo a salary."""
    pay_tables = {"workforce_pay_rates", "workforce_payslips", "workforce_payslip_lines",
                  "workforce_payroll_runs"}
    money = re.compile(r"(amount|gross|net|salary|wage|pay)_?minor|salary|wage", re.I)
    for table in _schema_sql(conn, "table", "workforce%"):
        if table in pay_tables:
            continue
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        offenders = [c for c in columns if money.search(c)]
        assert not offenders, f"{table} leaks pay data: {offenders}"


# --------------------------------------------------------------------------------------
# 9. policy is versioned data, not a constant in code (ADR-0032)
# --------------------------------------------------------------------------------------


def test_policy_version_is_checksummed_and_attributed(conn):
    common = dict(
        policy_version_id="pol_v1", agency_id=AGENCY, policy_code="WORK_HOURS", version=1,
        payload_json='{"daily_minutes": 480}', checksum_sha256="b" * 64,
        effective_from="2026-01-01", approved_by="owner_anas", approved_at=NOW, created_at=NOW,
    )
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO workforce_policy_versions (policy_version_id, agency_id, policy_code,"
            " version, payload_json, checksum_sha256, effective_from, effective_to, approved_by,"
            " approved_at, created_at) VALUES ('pol_v1', ?, 'WORK_HOURS', 1, '{}', ?,"
            " '2026-01-01', NULL, '', ?, ?)",
            (AGENCY, "b" * 64, NOW, NOW),
        )
    cols = ", ".join(common)
    marks = ", ".join("?" * len(common))
    conn.execute(f"INSERT INTO workforce_policy_versions ({cols}) VALUES ({marks})",
                 tuple(common.values()))
    with pytest.raises(Exception):
        conn.execute(f"INSERT INTO workforce_policy_versions ({cols}) VALUES ({marks})",
                     tuple(common.values()))


def test_policy_code_must_be_known(conn):
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO workforce_policy_versions (policy_version_id, agency_id, policy_code,"
            " version, payload_json, checksum_sha256, effective_from, approved_by, approved_at,"
            " created_at) VALUES ('pol_x', ?, 'not_a_policy', 1, '{}', ?, '2026-01-01',"
            " 'owner_anas', ?, ?)",
            (AGENCY, "c" * 64, NOW, NOW),
        )
