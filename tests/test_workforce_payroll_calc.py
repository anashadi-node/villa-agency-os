"""Policy upah berversi + aritmetika slip — angka tidak boleh dikarang (T2.1, T2.2).

Yang dikunci di sini, dan mengapa:

* **jendela setengah terbuka** ``[effective_from, effective_to)`` — tanpa itu,
  hari pergantian tarif punya dua kebenaran sekaligus;
* **dua versi/dua tarif untuk satu tanggal = gagal**, bukan "ambil yang terbaru" —
  memilih diam-diam berarti mengubah upah orang tanpa persetujuan;
* **checksum** menolak payload yang diubah tanpa versi baru;
* **tidak ada tarif = gagal**, karena orang tanpa tarif yang dihitung sebagai nol
  adalah slip gaji palsu;
* **gaji bulanan pada periode tidak penuh ditolak**, bukan diprorata tebakan;
* **lembur** ditolak saat melewati batas atas yang bisa dibuktikan;
* **pembulatan sekali** per baris (``ROUND_HALF_UP``), tidak pernah ``float``;
* **upah bersih negatif** tidak pernah menjadi slip;
* **potongan** hanya dari policy yang diserahkan pemanggil, bukan dari modul.

Seluruh data di sini sintetis (nama "Kadek Uji", nominal bulat), bukan upah orang
sungguhan.
"""

from __future__ import annotations

from datetime import date

import pytest

from primitives import DomainError, canonical_json, sha256_hex
from workforce.payroll_calc import (
    DEDUCTION,
    EARNING,
    EMPLOYER_CONTRIBUTION,
    TAX,
    InvalidLine,
    InvalidWorkSummary,
    MonthlyProrationRequired,
    NegativeNetPay,
    OvertimeLimitExceeded,
    PayLine,
    PayRateUnitMismatchFor,
    PayrollCalculation,
    StaffMismatch,
    WorkSummary,
    _prorated,
    calculate,
    check_overtime_bounds,
)
from workforce.payroll_policy import (
    PayRate,
    PayRateAmbiguous,
    PayRateCurrencyMismatch,
    PayRateNotFound,
    PayRateUnitMismatch,
    PolicyAmbiguous,
    PolicyBundle,
    PolicyChecksumMismatch,
    PolicyNotFound,
    PolicyPayloadInvalid,
    load_bundle,
    load_pay_rates,
    load_policy_version,
)

AGENCY = "agency_default"
STAFF = "stf_1"
NOW = "2026-11-15T00:00:00Z"


# --------------------------------------------------------------------------------------
# fixture: data upah lewat SQL, bukan lewat angka di modul
# --------------------------------------------------------------------------------------


def add_staff(conn, staff_id: str = STAFF, agency_id: str = AGENCY) -> str:
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, home_property_id, status, hired_on, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Kadek Uji', 'HOUSEKEEPING', 'PERMANENT', NULL, 'ACTIVE',"
        " '2026-01-01', ?, ?)",
        (staff_id, agency_id, f"EMP-{staff_id}", NOW, NOW),
    )
    return staff_id


def seed_policy(conn, policy_code: str = "WORK_HOURS", payload: dict | None = None, *,
                policy_version_id: str | None = None, version: int = 1,
                effective_from: str = "2026-01-01", effective_to: str | None = None,
                checksum: str | None = None, agency_id: str = AGENCY) -> str:
    payload = {"daily_minutes": 480} if payload is None else payload
    raw = canonical_json(payload)
    policy_version_id = policy_version_id or f"pol_{policy_code.lower()}_v{version}"
    conn.execute(
        "INSERT INTO workforce_policy_versions (policy_version_id, agency_id, policy_code,"
        " version, payload_json, checksum_sha256, effective_from, effective_to, approved_by,"
        " approved_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'owner_anas', ?, ?)",
        (
            policy_version_id, agency_id, policy_code, version, raw,
            checksum or sha256_hex(raw), effective_from, effective_to, NOW, NOW,
        ),
    )
    return policy_version_id


def seed_rate(conn, component_code: str = "BASE_MONTHLY", amount_minor: int = 5_000_000,
              *, unit: str = "MONTH", currency: str = "IDR", staff_id: str = STAFF,
              policy_version_id: str = "pol_work_hours_v1",
              effective_from: str = "2026-01-01", effective_to: str | None = None,
              agency_id: str = AGENCY, pay_rate_id: str | None = None) -> str:
    pay_rate_id = pay_rate_id or f"rate_{component_code.lower()}_{effective_from}"
    conn.execute(
        "INSERT INTO workforce_pay_rates (pay_rate_id, agency_id, staff_id, component_code,"
        " unit, amount_minor, currency, effective_from, effective_to, policy_version_id,"
        " approved_by, approved_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'owner_anas', ?, ?)",
        (pay_rate_id, agency_id, staff_id, component_code, unit, amount_minor, currency,
         effective_from, effective_to, policy_version_id, NOW, NOW),
    )
    return pay_rate_id


def seed_standard_package(conn, *, monthly: int = 5_000_000, overtime_rate: int = 50_000,
                          meal: int = 25_000, bonus: int | None = None) -> str:
    """Satu paket upah lengkap yang sah — dipakai tes yang fokus ke aritmetika."""
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480, "attendance_bonus_max_absence_days": 1})
    seed_rate(conn, "BASE_MONTHLY", monthly, unit="MONTH")
    seed_rate(conn, "OVERTIME_HOURLY", overtime_rate, unit="HOUR")
    seed_rate(conn, "MEAL_ALLOWANCE", meal, unit="DAY")
    if bonus is not None:
        seed_rate(conn, "ATTENDANCE_BONUS", bonus, unit="PER_OCCURRENCE")
    return STAFF


def work(**overrides) -> WorkSummary:
    base = dict(
        staff_id=STAFF,
        currency="IDR",
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        regular_minutes=9_600,
        overtime_minutes=300,
        days_worked=22,
        absence_days=0,
    )
    base.update(overrides)
    return WorkSummary(**base)


# --------------------------------------------------------------------------------------
# T2.1 — policy berversi
# --------------------------------------------------------------------------------------


def test_policy_window_is_half_open_at_the_seam(conn):
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480}, version=1,
                effective_from="2026-01-01", effective_to="2026-11-01")
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 420}, version=2,
                effective_from="2026-11-01", effective_to=None)

    assert load_policy_version(conn, "WORK_HOURS", date(2026, 10, 31)).payload["daily_minutes"] == 480
    # Tepat di titik sambung: versi lama sudah berakhir, versi baru mulai.
    assert load_policy_version(conn, "WORK_HOURS", date(2026, 11, 1)).payload["daily_minutes"] == 420
    assert load_policy_version(conn, "WORK_HOURS", date(2026, 12, 31)).version == 2


def test_two_open_policy_versions_are_ambiguous_not_newest_wins(conn):
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480}, version=1)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 400}, version=2)

    with pytest.raises(PolicyAmbiguous) as excinfo:
        load_policy_version(conn, "WORK_HOURS", date(2026, 6, 1))
    assert excinfo.value.code == "WORKFORCE_POLICY_AMBIGUOUS"
    assert len(excinfo.value.details["versions"]) == 2


def test_policy_absent_on_date_is_not_found(conn):
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480},
                effective_from="2026-06-01", effective_to="2026-07-01")
    with pytest.raises(PolicyNotFound):
        load_policy_version(conn, "WORK_HOURS", date(2026, 8, 1))


def test_policy_checksum_rejects_payload_edit_without_new_version(conn):
    policy_version_id = seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    conn.execute(
        "UPDATE workforce_policy_versions SET payload_json = ? WHERE policy_version_id = ?",
        (canonical_json({"daily_minutes": 240}), policy_version_id),
    )
    with pytest.raises(PolicyChecksumMismatch) as excinfo:
        load_policy_version(conn, "WORK_HOURS", date(2026, 6, 1))
    assert excinfo.value.code == "WORKFORCE_POLICY_CHECKSUM_MISMATCH"


def test_checksum_is_stable_under_json_reformatting(conn):
    payload = {"daily_minutes": 480, "overtime_max_minutes": 240}
    raw_reformatted = '{ "overtime_max_minutes" : 240, "daily_minutes" : 480 }'
    conn.execute(
        "INSERT INTO workforce_policy_versions (policy_version_id, agency_id, policy_code,"
        " version, payload_json, checksum_sha256, effective_from, approved_by, approved_at,"
        " created_at) VALUES ('pol_fmt', ?, 'WORK_HOURS', 1, ?, ?, '2026-01-01',"
        " 'owner_anas', ?, ?)",
        (AGENCY, raw_reformatted, sha256_hex(canonical_json(payload)), NOW, NOW),
    )
    # Spasi dan urutan kunci berbeda bukan tamper; isi yang berubah adalah tamper.
    assert load_policy_version(conn, "WORK_HOURS", date(2026, 6, 1)).version == 1


def test_checksum_accepts_literal_text_form(conn):
    raw = canonical_json({"daily_minutes": 480})
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480}, checksum=sha256_hex(raw))
    assert load_policy_version(conn, "WORK_HOURS", date(2026, 6, 1)).payload["daily_minutes"] == 480


def test_policy_payload_must_be_object(conn):
    conn.execute(
        "INSERT INTO workforce_policy_versions (policy_version_id, agency_id, policy_code,"
        " version, payload_json, checksum_sha256, effective_from, approved_by, approved_at,"
        " created_at) VALUES ('pol_list', ?, 'WORK_HOURS', 1, '[1, 2]', ?, '2026-01-01',"
        " 'owner_anas', ?, ?)",
        (AGENCY, sha256_hex("[1, 2]"), NOW, NOW),
    )
    with pytest.raises(PolicyPayloadInvalid):
        load_policy_version(conn, "WORK_HOURS", date(2026, 6, 1))


def test_policy_number_refuses_booleans_and_negatives(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": True})
    seed_rate(conn, "BASE_MONTHLY", 5_000_000, unit="MONTH")
    bundle = load_bundle(conn, STAFF, date(2026, 6, 1))
    # `True` adalah int di Python; kalau lolos, 1 menit/hari jadi "kebijakan".
    with pytest.raises(PolicyPayloadInvalid):
        bundle.policy("WORK_HOURS").number("daily_minutes")


def test_bundle_tolerates_absent_policy_but_fails_when_asked(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    seed_rate(conn, "BASE_MONTHLY", 5_000_000, unit="MONTH")

    bundle = load_bundle(conn, STAFF, date(2026, 6, 1))
    assert set(bundle.policies) == {"WORK_HOURS"}
    with pytest.raises(PolicyNotFound):
        bundle.policy("OVERTIME")


# --------------------------------------------------------------------------------------
# T2.1 — tarif
# --------------------------------------------------------------------------------------


def test_two_rates_for_one_component_on_one_date_are_ambiguous(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    seed_rate(conn, "BASE_MONTHLY", 5_000_000, unit="MONTH")
    seed_rate(conn, "BASE_MONTHLY", 6_000_000, unit="MONTH", effective_from="2026-03-01")

    with pytest.raises(PayRateAmbiguous) as excinfo:
        load_pay_rates(conn, STAFF, date(2026, 6, 1))
    assert excinfo.value.code == "WORKFORCE_PAY_RATE_AMBIGUOUS"


def test_rate_window_end_is_exclusive(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    seed_rate(conn, "BASE_MONTHLY", 5_000_000, unit="MONTH", effective_to="2026-06-01")

    assert load_pay_rates(conn, STAFF, date(2026, 5, 31))["BASE_MONTHLY"].amount_minor == 5_000_000
    # Tepat di titik sambung tarif lama sudah berakhir: bukan "tarif terakhir menang".
    assert load_pay_rates(conn, STAFF, date(2026, 6, 1)) == {}
    with pytest.raises(PayRateNotFound):
        load_bundle(conn, STAFF, date(2026, 6, 1))


def test_other_agency_rates_are_not_visible(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    seed_rate(conn, "BASE_MONTHLY", 5_000_000, unit="MONTH", agency_id="agency_lain")

    assert load_pay_rates(conn, STAFF, date(2026, 6, 1)) == {}
    with pytest.raises(PayRateNotFound):
        load_bundle(conn, STAFF, date(2026, 6, 1))


def test_person_without_any_rate_fails_instead_of_earning_zero(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    with pytest.raises(PayRateNotFound):
        load_bundle(conn, STAFF, date(2026, 6, 1))


def test_mixed_currencies_are_reported_as_currency_problem(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    seed_rate(conn, "BASE_MONTHLY", 5_000_000, unit="MONTH", currency="IDR")
    seed_rate(conn, "MEAL_ALLOWANCE", 15, unit="DAY", currency="USD")
    bundle = load_bundle(conn, STAFF, date(2026, 6, 1))

    with pytest.raises(PayRateCurrencyMismatch) as excinfo:
        _ = bundle.currency
    assert excinfo.value.code == "WORKFORCE_PAY_RATE_CURRENCY_MISMATCH"
    assert excinfo.value.details["currencies"] == ["IDR", "USD"]


def test_bundle_rate_checks_unit(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    seed_rate(conn, "BASE_MONTHLY", 5_000_000, unit="MONTH")
    bundle = load_bundle(conn, STAFF, date(2026, 6, 1))

    assert bundle.rate("BASE_MONTHLY", unit="MONTH").amount_minor == 5_000_000
    with pytest.raises(PayRateUnitMismatch):
        bundle.rate("BASE_MONTHLY", unit="HOUR")


# --------------------------------------------------------------------------------------
# T2.2 — bahan hitung
# --------------------------------------------------------------------------------------


def test_work_summary_rejects_impossible_input():
    with pytest.raises(InvalidWorkSummary):
        work(currency="RUPIAH")
    with pytest.raises(InvalidWorkSummary):
        work(period_start=date(2026, 11, 30), period_end=date(2026, 11, 1))
    with pytest.raises(InvalidWorkSummary):
        work(regular_minutes=-1)
    with pytest.raises(InvalidWorkSummary):
        work(overtime_minutes=True)
    with pytest.raises(InvalidWorkSummary):
        work(days_worked=0)


def test_minutes_without_a_day_are_flagged_as_broken_evidence():
    with pytest.raises(InvalidWorkSummary) as excinfo:
        work(days_worked=0, regular_minutes=480)
    assert "tanpa satu hari pun" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# T2.2 — batas lembur
# --------------------------------------------------------------------------------------


def test_overtime_over_daily_upper_bound_is_refused():
    single_day = work(period_start=date(2026, 11, 9), period_end=date(2026, 11, 9),
                      overtime_minutes=241, days_worked=1)
    with pytest.raises(OvertimeLimitExceeded) as excinfo:
        check_overtime_bounds(single_day)
    assert excinfo.value.details["upper_bound_minutes"] == 240


def test_weekly_upper_bound_binds_longer_periods():
    month = work(period_start=date(2026, 11, 1), period_end=date(2026, 11, 28),
                 overtime_minutes=4_320)
    assert check_overtime_bounds(month) == "within-upper-bound"
    with pytest.raises(OvertimeLimitExceeded):
        check_overtime_bounds(
            work(period_start=date(2026, 11, 1), period_end=date(2026, 11, 28),
                 overtime_minutes=4_321)
        )


# --------------------------------------------------------------------------------------
# T2.2 — aritmetika
# --------------------------------------------------------------------------------------


def test_hourly_amount_rounds_once_half_up():
    # 6001 × 50/60 = 5000.8333… → 5001, bukan 5000 (dipotong) dan bukan float.
    assert _prorated(6_001, 50, 60) == 5_001
    assert _prorated(6_000, 480, 60) == 48_000
    assert _prorated(0, 480, 60) == 0


def test_monthly_salary_on_partial_month_needs_a_human_decision(conn):
    seed_standard_package(conn)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))
    partial = work(period_start=date(2026, 11, 2), period_end=date(2026, 11, 30))

    with pytest.raises(MonthlyProrationRequired) as excinfo:
        calculate(partial, bundle)
    assert excinfo.value.code == "WORKFORCE_PAYROLL_PRORATION_REQUIRED"


def test_full_payslip_numbers_are_exact(conn):
    seed_standard_package(conn, bonus=100_000)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))
    tax = PayLine(component_code="PAYROLL_TAX", kind=TAX, amount_minor=250_000,
                  basis_note="policy PAYROLL_TAX v1: 5% dari bruto")

    result = calculate(work(), bundle, deductions=[tax])

    lines = {line.component_code: line for line in result.lines}
    assert lines["BASE_MONTHLY"].amount_minor == 5_000_000
    assert lines["OVERTIME_HOURLY"].amount_minor == 250_000          # 300 menit × 50.000/jam
    assert lines["MEAL_ALLOWANCE"].amount_minor == 550_000           # 22 hari × 25.000
    assert lines["ATTENDANCE_BONUS"].amount_minor == 100_000
    assert result.gross_minor == 5_900_000
    assert result.deduction_minor == 250_000
    assert result.net_minor == 5_650_000
    assert result.currency == "IDR"
    assert result.overtime_check == "within-upper-bound"


def test_every_line_carries_a_reason_and_a_policy_version(conn):
    seed_standard_package(conn)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))
    result = calculate(work(), bundle)

    for line in result.lines:
        assert line.kind == EARNING
        assert len(line.basis_note) >= 3
        assert line.policy_version_id == "pol_work_hours_v1"


def test_overtime_line_disappears_when_there_is_no_overtime(conn):
    seed_standard_package(conn)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))
    result = calculate(work(overtime_minutes=0), bundle)

    components = [line.component_code for line in result.lines]
    assert "OVERTIME_HOURLY" not in components


def test_attendance_bonus_needs_an_explicit_threshold(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    seed_rate(conn, "BASE_MONTHLY", 5_000_000, unit="MONTH")
    seed_rate(conn, "ATTENDANCE_BONUS", 100_000, unit="PER_OCCURRENCE")
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))

    with pytest.raises(PolicyPayloadInvalid):
        calculate(work(), bundle)


def test_attendance_bonus_is_paid_only_within_threshold(conn):
    seed_standard_package(conn, bonus=100_000)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))

    paid = calculate(work(absence_days=1), bundle)
    assert "ATTENDANCE_BONUS" in [line.component_code for line in paid.lines]

    unpaid = calculate(work(absence_days=2), bundle)
    assert "ATTENDANCE_BONUS" not in [line.component_code for line in unpaid.lines]
    assert unpaid.gross_minor == paid.gross_minor - 100_000


def test_unit_mismatch_between_component_and_rate_is_refused(conn):
    add_staff(conn)
    seed_policy(conn, "WORK_HOURS", {"daily_minutes": 480})
    seed_rate(conn, "BASE_DAILY", 300_000, unit="HOUR")
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))

    with pytest.raises(PayRateUnitMismatchFor) as excinfo:
        calculate(work(), bundle)
    assert excinfo.value.details["expected"] == "DAY"


def test_unknown_component_never_reaches_a_slip():
    bundle = PolicyBundle(
        staff_id=STAFF, on_date=date(2026, 11, 15), policies={},
        rates={"BONUS_MITOS": PayRate(
            pay_rate_id="rate_x", staff_id=STAFF, component_code="BONUS_MITOS", unit="DAY",
            amount_minor=1, currency="IDR", effective_from=date(2026, 1, 1),
            effective_to=None, policy_version_id="pol_x")},
    )
    with pytest.raises(InvalidLine):
        calculate(work(), bundle)


# --------------------------------------------------------------------------------------
# T2.2 — identitas, potongan, dan angka yang mustahil
# --------------------------------------------------------------------------------------


def test_bundle_belonging_to_someone_else_is_refused(conn):
    seed_standard_package(conn)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))
    with pytest.raises(StaffMismatch):
        calculate(work(staff_id="stf_2"), bundle)


def test_summary_currency_must_match_rates(conn):
    seed_standard_package(conn)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))
    with pytest.raises(DomainError):
        calculate(work(currency="USD"), bundle)


def test_deductions_lower_net_but_employer_contributions_do_not(conn):
    seed_standard_package(conn)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))
    lines = [
        PayLine(component_code="LOAN", kind=DEDUCTION, amount_minor=400_000,
                basis_note="cicilan pinjaman karyawan #12"),
        PayLine(component_code="BPJS_EMPLOYER", kind=EMPLOYER_CONTRIBUTION, amount_minor=120_000,
                basis_note="iuran pemberi kerja 4% (tidak memotong upah)"),
    ]
    result = calculate(work(), bundle, deductions=lines)

    assert result.gross_minor == 5_800_000          # tanpa bonus kehadiran
    assert result.deduction_minor == 400_000
    assert result.net_minor == 5_400_000
    # Iuran pemberi kerja tidak boleh menyentuh upah bersih.
    assert result.deduction_minor == 400_000
    assert {line.kind for line in result.lines} == {EARNING, DEDUCTION, EMPLOYER_CONTRIBUTION}


def test_negative_net_pay_is_never_a_payslip(conn):
    seed_standard_package(conn)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))
    too_much = PayLine(component_code="LOAN", kind=DEDUCTION, amount_minor=99_000_000,
                       basis_note="potongan salah input")

    with pytest.raises(NegativeNetPay) as excinfo:
        calculate(work(), bundle, deductions=[too_much])
    assert excinfo.value.code == "WORKFORCE_PAYROLL_NEGATIVE_NET"


def test_line_refuses_negative_or_boolean_amounts():
    with pytest.raises(InvalidLine):
        PayLine(component_code="X", kind=EARNING, amount_minor=-1, basis_note="negatif")
    with pytest.raises(InvalidLine):
        PayLine(component_code="X", kind=EARNING, amount_minor=True, basis_note="boolean")
    with pytest.raises(InvalidLine):
        PayLine(component_code="X", kind="POTONGAN_GAIB", amount_minor=1, basis_note="jenis")
    with pytest.raises(InvalidLine):
        PayLine(component_code="X", kind=EARNING, amount_minor=1, basis_note="")


def test_calculation_digest_is_stable_and_sensitive(conn):
    seed_standard_package(conn)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))

    first = calculate(work(), bundle)
    second = calculate(work(), bundle)
    changed = calculate(work(overtime_minutes=301), bundle)

    assert isinstance(first, PayrollCalculation)
    assert first.digest() == second.digest()
    assert first.digest() != changed.digest()
    assert len(first.digest()) == 64


def test_payload_lists_every_line_for_audit(conn):
    seed_standard_package(conn)
    bundle = load_bundle(conn, STAFF, date(2026, 11, 15))
    payload = calculate(work(), bundle).to_payload()

    assert payload["period_start"] == "2026-11-01"
    assert payload["net_minor"] == payload["gross_minor"] - payload["deduction_minor"]
    assert len(payload["lines"]) == 3
    assert all(line["basis_note"] for line in payload["lines"])