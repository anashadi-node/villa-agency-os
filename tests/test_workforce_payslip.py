"""Slip gaji: satu slip per orang per run, segel di rantai audit (T2.3).

Yang dikunci di sini, dan mengapa:

* **satu orang, satu slip, satu run** — mengulang hitung tidak boleh menimpa
  slip lama maupun membayar dua kali; yang kedua harus gagal terang-terangan;
* **aritmetika dihitung ulang dari baris**, bukan dipercaya dari pemanggil —
  objek hitung yang angkanya tidak konsisten dengan barisnya bukan slip gaji;
* **run beku menolak slip baru** — ``APPROVED``/``PAID``/``VOID`` adalah
  keputusan manusia, bukan status yang boleh disusul angka baru;
* **periode dan mata uang slip harus sama dengan run** — slip bulan lain di run
  ini akan membuat total run berbohong;
* **timesheet yang dibayar harus disetujui dan milik orang itu** — membayar
  klaim yang belum disetujui berarti membayar janji, bukan bukti;
* **segel terdeteksi berubah** — slip yang disunting setelah diterbitkan harus
  ketahuan, bukan lolos diam-diam;
* **total run dari slip tersimpan**, tidak pernah dari argumen pemanggil;
* **run idempoten per periode**, dan mata uang run tidak boleh berpindah diam-diam.

Seluruh data di sini sintetis (nama "Kadek Uji", nominal bulat), bukan upah orang
sungguhan.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime

import pytest

from primitives import CurrencyMismatch, EntityNotFound, FrozenClock, to_iso
from workforce.payroll_calc import (
    EARNING,
    TAX,
    PayLine,
    PayrollCalculation,
    calculate,
)
from workforce.payroll_policy import load_bundle
from workforce.payslip import (
    EmptyPayslip,
    PayrollRunFrozen,
    PayslipExists,
    PayslipInconsistent,
    PayslipWriter,
    TimesheetNotPayable,
)

AGENCY = "agency_default"
OTHER_AGENCY = "agency_other"
STAFF = "stf_1"
STAFF_2 = "stf_2"
NOW = "2026-11-15T00:00:00Z"
FIXED_NOW = datetime(2026, 11, 15, tzinfo=UTC)
POLICY = "pol_work_hours_v1"


# --------------------------------------------------------------------------------------
# fixture data — lewat SQL, bukan lewat angka di modul
# --------------------------------------------------------------------------------------


def seed_agency(conn, agency_id: str = AGENCY) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES (?, ?, 'ACTIVE', ?, ?)",
        (agency_id, f"Agency Uji {agency_id}", NOW, NOW),
    )
    return agency_id


def add_staff(conn, staff_id: str = STAFF, agency_id: str = AGENCY) -> str:
    seed_agency(conn, agency_id)
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, home_property_id, status, hired_on, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Kadek Uji', 'HOUSEKEEPING', 'PERMANENT', NULL, 'ACTIVE',"
        " '2026-01-01', ?, ?)",
        (staff_id, agency_id, f"EMP-{staff_id}", NOW, NOW),
    )
    return staff_id


def seed_policy(conn, payload: dict | None = None, *, agency_id: str = AGENCY) -> str:
    from primitives import canonical_json, sha256_hex

    seed_agency(conn, agency_id)
    payload = {"daily_minutes": 480, "attendance_bonus_max_absence_days": 1} if payload is None else payload
    raw = canonical_json(payload)
    conn.execute(
        "INSERT OR IGNORE INTO workforce_policy_versions (policy_version_id, agency_id, policy_code,"
        " version, payload_json, checksum_sha256, effective_from, effective_to, approved_by,"
        " approved_at, created_at) VALUES (?, ?, 'WORK_HOURS', 1, ?, ?, '2026-01-01', NULL,"
        " 'owner_anas', ?, ?)",
        (POLICY, agency_id, raw, sha256_hex(raw), NOW, NOW),
    )
    return POLICY


def seed_rate(
    conn,
    component_code: str,
    amount_minor: int,
    unit: str,
    *,
    staff_id: str = STAFF,
) -> None:
    conn.execute(
        "INSERT INTO workforce_pay_rates (pay_rate_id, agency_id, staff_id, component_code,"
        " unit, amount_minor, currency, effective_from, effective_to, policy_version_id,"
        " approved_by, approved_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'IDR', '2026-01-01', NULL, ?, 'owner_anas', ?, ?)",
        (
            f"rate_{staff_id}_{component_code}",
            AGENCY,
            staff_id,
            component_code,
            unit,
            amount_minor,
            POLICY,
            NOW,
            NOW,
        ),
    )


def seed_package(conn, *, staff_id: str = STAFF, monthly: int = 5_000_000) -> str:
    add_staff(conn, staff_id)
    seed_policy(conn)
    seed_rate(conn, "BASE_MONTHLY", monthly, "MONTH", staff_id=staff_id)
    seed_rate(conn, "OVERTIME_HOURLY", 50_000, "HOUR", staff_id=staff_id)
    seed_rate(conn, "MEAL_ALLOWANCE", 25_000, "DAY", staff_id=staff_id)
    return staff_id


def seed_timesheet(
    conn,
    *,
    timesheet_id: str = "ts_1",
    staff_id: str = STAFF,
    status: str = "APPROVED",
    agency_id: str = AGENCY,
) -> str:
    conn.execute(
        "INSERT INTO workforce_timesheets (timesheet_id, agency_id, staff_id, period_start,"
        " period_end, status, regular_minutes, overtime_minutes, break_minutes, leave_days,"
        " submitted_at, approved_by, approved_at, lock_hash, version)"
        " VALUES (?, ?, ?, '2026-11-01', '2026-11-30', ?, 2400, 300, 300, 0, ?, ?, ?, ?, 1)",
        (
            timesheet_id,
            agency_id,
            staff_id,
            status,
            NOW,
            "ops_lead" if status == "APPROVED" else None,
            NOW if status == "APPROVED" else None,
            "a" * 64 if status == "APPROVED" else None,
        ),
    )
    return timesheet_id


def work_summary(*, staff_id: str = STAFF, overtime_minutes: int = 300, days_worked: int = 22):
    from workforce.payroll_calc import WorkSummary

    return WorkSummary(
        staff_id=staff_id,
        currency="IDR",
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        regular_minutes=9_600,
        overtime_minutes=overtime_minutes,
        days_worked=days_worked,
        absence_days=0,
    )


def calc_for(conn, *, staff_id: str = STAFF, tax_minor: int = 250_000) -> PayrollCalculation:
    bundle = load_bundle(conn, staff_id, date(2026, 11, 15))
    deductions: list[PayLine] = []
    if tax_minor:
        deductions.append(
            PayLine(
                component_code="PAYROLL_TAX",
                kind=TAX,
                amount_minor=tax_minor,
                basis_note="policy PAYROLL_TAX v1: potongan sesuai payload berversi",
                policy_version_id=POLICY,
            )
        )
    return calculate(work_summary(staff_id=staff_id), bundle, deductions=deductions)


def writer(conn, *, clock=None, ids=None, agency_id: str = AGENCY) -> PayslipWriter:
    return PayslipWriter(conn, agency_id=agency_id, clock=clock, ids=ids)


def open_run(conn, writer_obj: PayslipWriter | None = None) -> dict:
    w = writer_obj or writer(conn)
    run, _ = w.open_run("2026-11-01", "2026-11-30", "IDR", created_by="ops_lead")
    return run


# --------------------------------------------------------------------------------------
# T2.3 — terbitkan slip
# --------------------------------------------------------------------------------------


def test_first_slip_persists_numbers_and_moves_run_to_calculated(conn):
    seed_package(conn)
    w = writer(conn, clock=FrozenClock(FIXED_NOW))
    run = open_run(conn, w)
    assert run["status"] == "DRAFT"

    calc = calc_for(conn)
    slip = w.issue(run["payroll_run_id"], calc, issued_by="ops_lead")

    assert slip["staff_id"] == STAFF
    assert slip["gross_minor"] == calc.gross_minor
    assert slip["deduction_minor"] == calc.deduction_minor
    assert slip["net_minor"] == calc.net_minor
    assert slip["worked_minutes"] == calc.regular_minutes
    assert slip["overtime_minutes"] == calc.overtime_minutes
    assert slip["currency"] == "IDR"
    assert slip["line_count"] == len(calc.lines)

    refreshed = w.get_run(run["payroll_run_id"])
    assert refreshed["status"] == "CALCULATED"
    assert refreshed["calculated_by"] == "ops_lead"
    assert refreshed["calculated_at"] == to_iso(FIXED_NOW)
    assert refreshed["total_gross_minor"] == calc.gross_minor
    assert refreshed["total_net_minor"] == calc.net_minor


def test_lines_are_stored_with_basis_note_and_policy_version(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    calc = calc_for(conn)
    slip = w.issue(run["payroll_run_id"], calc, issued_by="ops_lead")

    stored = w.lines(slip["payslip_id"])
    assert len(stored) == len(calc.lines)
    for line in stored:
        assert line["basis_note"] and len(line["basis_note"]) >= 3
        assert line["policy_version_id"] == POLICY
        assert line["kind"] in {line.kind for line in calc.lines}


def test_second_slip_for_the_same_person_in_one_run_is_refused_not_overwritten(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    first = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")

    with pytest.raises(PayslipExists) as excinfo:
        w.issue(run["payroll_run_id"], calc_for(conn, tax_minor=100_000), issued_by="ops_lead")

    assert excinfo.value.code == "WORKFORCE_PAYSLIP_EXISTS"
    assert excinfo.value.details["payslip_id"] == first["payslip_id"]
    still_one = w.for_run(run["payroll_run_id"])
    assert len(still_one) == 1
    assert still_one[0]["net_minor"] == first["net_minor"]


@pytest.mark.parametrize("frozen", ["APPROVED", "PAID", "VOID"])
def test_frozen_run_refuses_new_slips(conn, frozen):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")
    conn.execute(
        "UPDATE workforce_payroll_runs SET approved_by = 'owner_anas', approved_at = ?,"
        " status = ?, void_reason = ? WHERE payroll_run_id = ?",
        (NOW, frozen, "void uji" if frozen == "VOID" else None, run["payroll_run_id"]),
    )
    add_staff(conn, STAFF_2)
    seed_rate(conn, "BASE_MONTHLY", 4_000_000, "MONTH", staff_id=STAFF_2)
    seed_rate(conn, "MEAL_ALLOWANCE", 25_000, "DAY", staff_id=STAFF_2)

    with pytest.raises(PayrollRunFrozen) as excinfo:
        w.issue(run["payroll_run_id"], calc_for(conn, staff_id=STAFF_2), issued_by="ops_lead")

    assert excinfo.value.code == "WORKFORCE_PAYROLL_RUN_FROZEN"
    assert excinfo.value.details["status"] == frozen
    assert len(w.for_run(run["payroll_run_id"])) == 1


def test_calculation_for_another_period_is_refused(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    calc = calc_for(conn)
    shifted = dataclasses.replace(
        calc, period_start=date(2026, 10, 1), period_end=date(2026, 10, 31)
    )

    with pytest.raises(PayslipInconsistent) as excinfo:
        w.issue(run["payroll_run_id"], shifted, issued_by="ops_lead")

    assert excinfo.value.code == "WORKFORCE_PAYSLIP_INCONSISTENT"
    assert excinfo.value.details["calc_period_start"] == "2026-10-01"
    assert w.for_run(run["payroll_run_id"]) == []


def test_calculation_currency_must_match_the_run(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    calc = dataclasses.replace(calc_for(conn), currency="USD")

    with pytest.raises(CurrencyMismatch):
        w.issue(run["payroll_run_id"], calc, issued_by="ops_lead")
    assert w.for_run(run["payroll_run_id"]) == []


def test_gross_that_disagrees_with_its_lines_is_refused(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    calc = calc_for(conn)
    inflated = dataclasses.replace(
        calc,
        gross_minor=calc.gross_minor + 1,
        net_minor=calc.net_minor + 1,
    )

    with pytest.raises(PayslipInconsistent) as excinfo:
        w.issue(run["payroll_run_id"], inflated, issued_by="ops_lead")

    assert excinfo.value.details["lines_gross_minor"] == calc.gross_minor
    assert excinfo.value.details["declared_gross_minor"] == calc.gross_minor + 1


def test_net_that_is_not_gross_minus_deduction_is_refused(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    calc = calc_for(conn)
    off = dataclasses.replace(calc, net_minor=calc.net_minor - 1)

    with pytest.raises(PayslipInconsistent) as excinfo:
        w.issue(run["payroll_run_id"], off, issued_by="ops_lead")
    assert excinfo.value.details["net_minor"] == calc.net_minor - 1


def test_calculation_without_lines_never_becomes_a_slip(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    empty = PayrollCalculation(
        staff_id=STAFF,
        currency="IDR",
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        lines=(),
        gross_minor=0,
        deduction_minor=0,
        net_minor=0,
        regular_minutes=0,
        overtime_minutes=0,
        days_worked=0,
    )

    with pytest.raises(EmptyPayslip) as excinfo:
        w.issue(run["payroll_run_id"], empty, issued_by="ops_lead")
    assert excinfo.value.code == "WORKFORCE_PAYSLIP_EMPTY"
    assert w.for_run(run["payroll_run_id"]) == []


def test_zero_net_pay_never_becomes_a_slip(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    calc = calc_for(conn)
    zeroed = dataclasses.replace(
        calc, lines=(PayLine("BASE_MONTHLY", EARNING, 0, "nol uji"),), gross_minor=0,
        deduction_minor=0, net_minor=0,
    )

    with pytest.raises(EmptyPayslip):
        w.issue(run["payroll_run_id"], zeroed, issued_by="ops_lead")


def test_unknown_run_is_reported_not_created_on_the_fly(conn):
    seed_package(conn)
    w = writer(conn)
    with pytest.raises(EntityNotFound):
        w.issue("prun_tidak_ada", calc_for(conn), issued_by="ops_lead")


def test_run_of_another_agency_is_invisible(conn):
    seed_package(conn)
    seed_agency(conn, OTHER_AGENCY)
    other = writer(conn, agency_id=OTHER_AGENCY)
    run = open_run(conn, other)

    with pytest.raises(EntityNotFound):
        writer(conn).issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")


# --------------------------------------------------------------------------------------
# T2.3 — timesheet yang boleh dibayar
# --------------------------------------------------------------------------------------


def test_timesheet_must_be_approved_before_it_is_paid(conn):
    seed_package(conn)
    seed_timesheet(conn, status="SUBMITTED")
    w = writer(conn)
    run = open_run(conn, w)

    with pytest.raises(TimesheetNotPayable) as excinfo:
        w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead", timesheet_id="ts_1")

    assert excinfo.value.details["status"] == "SUBMITTED"
    assert w.for_run(run["payroll_run_id"]) == []


def test_timesheet_of_another_person_cannot_pay_this_slip(conn):
    seed_package(conn)
    seed_package(conn, staff_id=STAFF_2)
    seed_timesheet(conn, staff_id=STAFF_2)
    w = writer(conn)
    run = open_run(conn, w)

    with pytest.raises(TimesheetNotPayable) as excinfo:
        w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead", timesheet_id="ts_1")

    assert excinfo.value.details["timesheet_staff_id"] == STAFF_2


def test_missing_timesheet_is_reported(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    with pytest.raises(EntityNotFound):
        w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead", timesheet_id="ts_ghost")


def test_approved_timesheet_is_recorded_on_the_slip(conn):
    seed_package(conn)
    seed_timesheet(conn)
    w = writer(conn)
    run = open_run(conn, w)
    slip = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead", timesheet_id="ts_1")
    assert slip["timesheet_id"] == "ts_1"


# --------------------------------------------------------------------------------------
# T2.3 — segel di rantai audit
# --------------------------------------------------------------------------------------


def test_seal_is_written_to_the_audit_chain_with_the_actor(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    calc = calc_for(conn)
    slip = w.issue(run["payroll_run_id"], calc, issued_by="ops_lead", actor_role="PAYROLL_OPERATOR")

    row = conn.execute(
        "SELECT action, actor, actor_role, evidence_ref, after_state FROM audit_events"
        " WHERE entity_type = 'workforce_payslip' AND entity_id = ?",
        (slip["payslip_id"],),
    ).fetchone()
    assert row is not None
    assert row["action"] == "WORKFORCE_PAYSLIP_ISSUED"
    assert row["actor"] == "ops_lead"
    assert row["actor_role"] == "PAYROLL_OPERATOR"
    assert row["evidence_ref"] == slip["seal"]
    assert slip["seal"] == calc.digest()


def test_verify_seal_accepts_an_untouched_slip(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    slip = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")

    result = w.verify_seal(slip["payslip_id"])
    assert result["matches"] is True
    assert result["drift"] == []
    assert result["recomputed"] == result["seal"] == slip["seal"]


def test_verify_seal_detects_an_edited_line(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    slip = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")
    conn.execute(
        "UPDATE workforce_payslip_lines SET amount_minor = amount_minor + 1"
        " WHERE payslip_id = ? AND component_code = 'BASE_MONTHLY'",
        (slip["payslip_id"],),
    )

    result = w.verify_seal(slip["payslip_id"])
    assert result["matches"] is False
    assert "line:BASE_MONTHLY" in result["drift"]


def test_verify_seal_detects_edited_payslip_totals(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    slip = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")
    conn.execute(
        "UPDATE workforce_payslips SET gross_minor = gross_minor + 1, net_minor = net_minor + 1"
        " WHERE payslip_id = ?",
        (slip["payslip_id"],),
    )

    result = w.verify_seal(slip["payslip_id"])
    assert result["matches"] is False
    assert "gross_minor" in result["drift"]
    assert "net_minor" in result["drift"]
    # Sidik jarinya sendiri tetap utuh: yang berubah adalah angka tersimpan.
    assert result["recomputed"] == result["seal"]


def test_verify_seal_detects_a_dropped_line(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    slip = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")
    conn.execute(
        "DELETE FROM workforce_payslip_lines WHERE payslip_id = ? AND kind = ?",
        (slip["payslip_id"], TAX),
    )

    result = w.verify_seal(slip["payslip_id"])
    assert result["matches"] is False
    assert "line_count" in result["drift"]


def test_slip_without_a_seal_is_an_error_not_a_pass(conn):
    seed_package(conn)
    w = writer(conn)
    run = open_run(conn, w)
    slip = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")
    conn.execute("DROP TRIGGER trg_audit_events_no_delete")
    conn.execute(
        "DELETE FROM audit_events WHERE entity_type = 'workforce_payslip' AND entity_id = ?",
        (slip["payslip_id"],),
    )

    with pytest.raises(EntityNotFound) as excinfo:
        w.verify_seal(slip["payslip_id"])
    assert excinfo.value.code == "ENTITY_NOT_FOUND"
    assert excinfo.value.details["entity"] == "payslip_seal"


# --------------------------------------------------------------------------------------
# T2.3 — run
# --------------------------------------------------------------------------------------


def test_open_run_is_idempotent_for_the_same_period(conn):
    seed_package(conn)
    w = writer(conn)
    first, created_first = w.open_run("2026-11-01", "2026-11-30", "IDR", created_by="ops_lead")
    second, created_second = w.open_run(date(2026, 11, 1), date(2026, 11, 30), "IDR",
                                       created_by="ops_lead")

    assert created_first is True
    assert created_second is False
    assert first["payroll_run_id"] == second["payroll_run_id"]
    assert conn.execute("SELECT COUNT(*) FROM workforce_payroll_runs").fetchone()[0] == 1


def test_opening_a_run_leaves_one_audit_event_for_the_opener(conn):
    seed_package(conn)
    w = writer(conn)
    run, _ = w.open_run("2026-11-01", "2026-11-30", "IDR", created_by="ops_lead")
    w.open_run("2026-11-01", "2026-11-30", "IDR", created_by="ops_lead")

    rows = conn.execute(
        "SELECT actor, entity_type, entity_id FROM audit_events"
        " WHERE action = 'WORKFORCE_PAYROLL_RUN_OPENED'"
    ).fetchall()

    assert len(rows) == 1, "run yang dibuka dua kali tetap satu jejak audit"
    assert rows[0][0] == "ops_lead"
    assert rows[0][1] == "workforce_payroll_run"
    assert rows[0][2] == run["payroll_run_id"]


def test_open_run_refuses_to_switch_currency_silently(conn):
    seed_package(conn)
    w = writer(conn)
    w.open_run("2026-11-01", "2026-11-30", "IDR", created_by="ops_lead")

    with pytest.raises(CurrencyMismatch):
        w.open_run("2026-11-01", "2026-11-30", "USD", created_by="ops_lead")


def test_open_run_refuses_a_reversed_period(conn):
    w = writer(conn)
    with pytest.raises(PayslipInconsistent):
        w.open_run("2026-11-30", "2026-11-01", "IDR", created_by="ops_lead")
    assert conn.execute("SELECT COUNT(*) FROM workforce_payroll_runs").fetchone()[0] == 0


def test_run_totals_sum_the_stored_slips_only(conn):
    seed_package(conn)
    seed_package(conn, staff_id=STAFF_2, monthly=4_000_000)
    w = writer(conn)
    run = open_run(conn, w)
    first = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")
    second = w.issue(run["payroll_run_id"], calc_for(conn, staff_id=STAFF_2), issued_by="ops_lead")

    totals = w.run_totals(run["payroll_run_id"])
    assert totals["slip_count"] == 2
    assert totals["total_gross_minor"] == first["gross_minor"] + second["gross_minor"]
    assert totals["total_net_minor"] == first["net_minor"] + second["net_minor"]
    assert totals["currency"] == "IDR"

    refreshed = w.get_run(run["payroll_run_id"])
    assert refreshed["total_gross_minor"] == totals["total_gross_minor"]
    assert refreshed["total_net_minor"] == totals["total_net_minor"]


def test_run_totals_of_an_empty_run_are_zero_not_null(conn):
    w = writer(conn)
    run = open_run(conn, w)
    totals = w.run_totals(run["payroll_run_id"])
    assert totals == {
        "payroll_run_id": run["payroll_run_id"],
        "slip_count": 0,
        "total_gross_minor": 0,
        "total_deduction_minor": 0,
        "total_net_minor": 0,
        "currency": "IDR",
    }
