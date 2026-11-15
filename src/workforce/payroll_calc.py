"""Hitung upah: aritmetika pasti, batas lembur, dan kegagalan yang jujur (T2.2).

Kontrak modul:

1. **Uang tidak pernah menyentuh ``float``.** Semua perhitungan lewat
   ``Decimal`` dan baru dibulatkan sekali di akhir tiap baris
   (``ROUND_HALF_UP`` ke satuan minor). ``0.1 + 0.2 != 0.3`` bukan keanehan
   akademis: pada 3.000 baris upah ia menjadi selisih rupiah yang tidak bisa
   dijelaskan ke pemilik villa.
2. **Tidak ada tarif, pajak, atau persentase di dalam kode.** Semua angka datang
   dari ``PolicyBundle`` (T2.1). Potongan hanya masuk kalau pemanggil
   menyerahkannya — dari policy yang sudah disetujui manusia.
3. **Batas lembur diperiksa sebagai batas atas yang bisa dibuktikan.** Total
   lembur satu periode tidak boleh melebihi ``240 menit × jumlah hari`` (tidak
   mungkin ada hari yang patuh kalau totalnya lebih) dan tidak boleh melebihi
   ``1080 menit × jumlah pekan`` (batas 18 jam/pekan). Kedua batas adalah syarat
   perlu, bukan bukti kepatuhan per hari — karena itu hasilnya menyebut
   ``checked`` alih-alih mengklaim "lewat validasi".
4. **Upah gaji bulanan pada periode bukan sebulan penuh ditolak**, bukan
   diprorata diam-diam. Prorata adalah kebijakan pemilik villa (hari kalender?
   hari kerja? potong dari tanggal masuk?), dan menebaknya berarti memotong upah
   orang dengan angka karangan.
5. **Slip dengan upah bersih negatif tidak pernah dibuat.** Itu tanda policy
   potongan salah, bukan tanda orang berutang ke agency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Optional, Sequence

from primitives import CurrencyMismatch, DomainError, fingerprint, to_iso_date
from workforce.payroll_policy import PolicyBundle, PayRate, PolicyPayloadInvalid

#: Batas lembur yang lazim dipakai UU Ketenagakerjaan: 4 jam/hari, 18 jam/pekan.
OVERTIME_MAX_DAILY_MINUTES = 240
OVERTIME_MAX_WEEKLY_MINUTES = 1080

PER_HOUR_MINUTES = 60

EARNING = "EARNING"
DEDUCTION = "DEDUCTION"
TAX = "TAX"
EMPLOYER_CONTRIBUTION = "EMPLOYER_CONTRIBUTION"

#: Komponen yang mengurangi upah bersih. Iuran pemberi kerja tidak.
NET_REDUCING_KINDS = (DEDUCTION, TAX)


class PayrollError(DomainError):
    """Kegagalan yang disengaja pada perhitungan upah."""

    code = "WORKFORCE_PAYROLL_ERROR"

    def __init__(self, message: str, details: Optional[Mapping[str, Any]] = None, *,
                 code: Optional[str] = None) -> None:
        super().__init__(message, code=code, **(dict(details) if details else {}))


class InvalidWorkSummary(PayrollError):
    code = "WORKFORCE_PAYROLL_INVALID_SUMMARY"


class StaffMismatch(PayrollError):
    code = "WORKFORCE_PAYROLL_STAFF_MISMATCH"


class OvertimeLimitExceeded(PayrollError):
    code = "WORKFORCE_OVERTIME_LIMIT_EXCEEDED"


class MonthlyProrationRequired(PayrollError):
    code = "WORKFORCE_PAYROLL_PRORATION_REQUIRED"


class InvalidLine(PayrollError):
    code = "WORKFORCE_PAYROLL_INVALID_LINE"


class NegativeNetPay(PayrollError):
    code = "WORKFORCE_PAYROLL_NEGATIVE_NET"


def _quantize_minor(exact: Decimal) -> int:
    """Bulatkan ke satuan minor sekali saja, setengah ke atas."""
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class WorkSummary:
    """Bahan hitung: menit dan hari yang sudah dibuktikan timesheet (T1.4)."""

    staff_id: str
    currency: str
    period_start: date
    period_end: date
    regular_minutes: int
    overtime_minutes: int
    days_worked: int
    absence_days: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or len(self.currency) != 3:
            raise InvalidWorkSummary("currency must be a three-letter code",
                                     {"currency": self.currency})
        if self.period_end < self.period_start:
            raise InvalidWorkSummary("periode terbalik",
                                     {"start": to_iso_date(self.period_start),
                                      "end": to_iso_date(self.period_end)})
        for name in ("regular_minutes", "overtime_minutes", "days_worked", "absence_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidWorkSummary(f"{name} must be an integer >= 0",
                                         {"field": name, "value": value})
        if self.days_worked == 0 and self.regular_minutes > 0:
            raise InvalidWorkSummary(
                "ada menit kerja tanpa satu hari pun tercatat",
                {"regular_minutes": self.regular_minutes},
            )

    @property
    def period_days(self) -> int:
        return (self.period_end - self.period_start).days + 1

    @property
    def total_minutes(self) -> int:
        return self.regular_minutes + self.overtime_minutes


@dataclass(frozen=True)
class PayLine:
    """Satu baris slip: komponen, jenis, nominal minor, dan alasannya."""

    component_code: str
    kind: str
    amount_minor: int
    basis_note: str
    policy_version_id: Optional[str] = None
    quantity_minutes: Optional[int] = None
    rate_minor: Optional[int] = None

    def __post_init__(self) -> None:
        if self.kind not in (EARNING, DEDUCTION, TAX, EMPLOYER_CONTRIBUTION):
            raise InvalidLine(f"unknown pay line kind: {self.kind}",
                              {"kind": self.kind})
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise InvalidLine("amount_minor must be an integer",
                              {"amount_minor": self.amount_minor})
        if self.amount_minor < 0:
            raise InvalidLine("amount_minor must not be negative",
                              {"component_code": self.component_code})
        if not isinstance(self.basis_note, str) or len(self.basis_note) < 3:
            raise InvalidLine("basis_note must explain where the amount comes from",
                              {"component_code": self.component_code})


@dataclass(frozen=True)
class PayrollCalculation:
    """Hasil hitung yang bisa diaudit baris per baris."""

    staff_id: str
    currency: str
    period_start: date
    period_end: date
    lines: Sequence[PayLine]
    gross_minor: int
    deduction_minor: int
    net_minor: int
    regular_minutes: int
    overtime_minutes: int
    days_worked: int
    overtime_check: str = field(default="within-upper-bound")

    def to_payload(self) -> dict[str, Any]:
        return {
            "staff_id": self.staff_id,
            "currency": self.currency,
            "period_start": to_iso_date(self.period_start),
            "period_end": to_iso_date(self.period_end),
            "gross_minor": self.gross_minor,
            "deduction_minor": self.deduction_minor,
            "net_minor": self.net_minor,
            "regular_minutes": self.regular_minutes,
            "overtime_minutes": self.overtime_minutes,
            "days_worked": self.days_worked,
            "lines": [
                {
                    "component_code": line.component_code,
                    "kind": line.kind,
                    "amount_minor": line.amount_minor,
                    "quantity_minutes": line.quantity_minutes,
                    "rate_minor": line.rate_minor,
                    "policy_version_id": line.policy_version_id,
                    "basis_note": line.basis_note,
                }
                for line in self.lines
            ],
        }

    def digest(self) -> str:
        """Sidik jari isi hitung — dipakai slip gaji untuk membekukan angka."""
        return fingerprint(self.to_payload(), domain="workforce.payroll")


def check_overtime_bounds(work: WorkSummary) -> str:
    """Tolak total lembur yang secara aritmetika pasti melanggar batas.

    Batas harian (240 menit) memberi batas atas ``240 × hari``; batas pekanan
    (1080 menit) memberi ``1080 × pekan``. Melebihi salah satunya berarti ada
    hari/pekan yang dilanggar — tidak perlu tebakan.
    """
    days = work.period_days
    weeks = (days + 6) // 7
    daily_cap = OVERTIME_MAX_DAILY_MINUTES * days
    weekly_cap = OVERTIME_MAX_WEEKLY_MINUTES * weeks
    cap = min(daily_cap, weekly_cap)
    if work.overtime_minutes > cap:
        raise OvertimeLimitExceeded(
            f"overtime {work.overtime_minutes} minutes exceeds the cap of {cap} minutes"
            f" for a {days}-day period",
            {
                "overtime_minutes": work.overtime_minutes,
                "upper_bound_minutes": cap,
                "daily_cap_minutes": daily_cap,
                "weekly_cap_minutes": weekly_cap,
                "period_days": days,
            },
        )
    return "within-upper-bound"


def _last_day_of_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def is_full_calendar_month(work: WorkSummary) -> bool:
    return work.period_start.day == 1 and work.period_end == _last_day_of_month(work.period_start)


def _prorated(rate_minor: int, quantity_minutes: int, unit_minutes: int) -> int:
    """Tarif per unit × menit / menit-per-unit, satu kali pembulatan."""
    exact = Decimal(rate_minor) * Decimal(quantity_minutes) / Decimal(unit_minutes)
    return _quantize_minor(exact)


def _attendance_bonus_threshold(bundle: PolicyBundle) -> int:
    """Ambang absen untuk bonus kehadiran — wajib datang dari policy."""
    policy = bundle.policy("WORK_HOURS")
    payload = policy.payload.get("attendance_bonus_max_absence_days")
    if payload is None:
        raise PolicyPayloadInvalid(
            "an ATTENDANCE_BONUS rate exists, but the WORK_HOURS policy does not carry"
            " 'attendance_bonus_max_absence_days'",
            {"policy_version_id": policy.policy_version_id},
        )
    return policy.number("attendance_bonus_max_absence_days")


def _earning_line(component: str, rate: PayRate, work: WorkSummary,
                  bundle: PolicyBundle) -> Optional[PayLine]:
    """Satu komponen upah → satu baris, atau ``None`` bila memang nol."""
    if component == "BASE_MONTHLY":
        if rate.unit != "MONTH":
            raise PayRateUnitMismatchFor(component, rate.unit, "MONTH")
        if not is_full_calendar_month(work):
            raise MonthlyProrationRequired(
                "a monthly salary over a partial calendar month: proration must be"
                " decided by the villa owner, never guessed by the system",
                {"period_start": to_iso_date(work.period_start),
                 "period_end": to_iso_date(work.period_end),
                 "policy_version_id": rate.policy_version_id},
            )
        return PayLine(
            component_code=component,
            kind=EARNING,
            amount_minor=rate.amount_minor,
            basis_note=f"gaji bulanan penuh {to_iso_date(work.period_start)}"
                       f"–{to_iso_date(work.period_end)}",
            policy_version_id=rate.policy_version_id,
            rate_minor=rate.amount_minor,
        )

    if component == "BASE_DAILY":
        if rate.unit != "DAY":
            raise PayRateUnitMismatchFor(component, rate.unit, "DAY")
        amount = rate.amount_minor * work.days_worked
        return PayLine(
            component_code=component,
            kind=EARNING,
            amount_minor=amount,
            basis_note=f"{work.days_worked} hari × {rate.amount_minor}",
            policy_version_id=rate.policy_version_id,
            rate_minor=rate.amount_minor,
        )

    if component == "BASE_HOURLY":
        if rate.unit != "HOUR":
            raise PayRateUnitMismatchFor(component, rate.unit, "HOUR")
        amount = _prorated(rate.amount_minor, work.regular_minutes, PER_HOUR_MINUTES)
        if amount == 0 and work.regular_minutes == 0:
            return None
        return PayLine(
            component_code=component,
            kind=EARNING,
            amount_minor=amount,
            basis_note=f"{work.regular_minutes} menit kerja × {rate.amount_minor}/jam",
            policy_version_id=rate.policy_version_id,
            quantity_minutes=work.regular_minutes,
            rate_minor=rate.amount_minor,
        )

    if component == "OVERTIME_HOURLY":
        if rate.unit != "HOUR":
            raise PayRateUnitMismatchFor(component, rate.unit, "HOUR")
        if work.overtime_minutes == 0:
            return None
        amount = _prorated(rate.amount_minor, work.overtime_minutes, PER_HOUR_MINUTES)
        return PayLine(
            component_code=component,
            kind=EARNING,
            amount_minor=amount,
            basis_note=f"{work.overtime_minutes} menit lembur × {rate.amount_minor}/jam",
            policy_version_id=rate.policy_version_id,
            quantity_minutes=work.overtime_minutes,
            rate_minor=rate.amount_minor,
        )

    if component in ("MEAL_ALLOWANCE", "TRANSPORT_ALLOWANCE"):
        if rate.unit not in ("PER_OCCURRENCE", "DAY"):
            raise PayRateUnitMismatchFor(component, rate.unit, "PER_OCCURRENCE")
        amount = rate.amount_minor * work.days_worked
        return PayLine(
            component_code=component,
            kind=EARNING,
            amount_minor=amount,
            basis_note=f"{work.days_worked} hari hadir × {rate.amount_minor}",
            policy_version_id=rate.policy_version_id,
            rate_minor=rate.amount_minor,
        )

    if component == "ATTENDANCE_BONUS":
        if rate.unit != "PER_OCCURRENCE":
            raise PayRateUnitMismatchFor(component, rate.unit, "PER_OCCURRENCE")
        threshold = _attendance_bonus_threshold(bundle)
        if work.absence_days > threshold:
            return None
        return PayLine(
            component_code=component,
            kind=EARNING,
            amount_minor=rate.amount_minor,
            basis_note=f"absen {work.absence_days} hari ≤ ambang {threshold}",
            policy_version_id=rate.policy_version_id,
            rate_minor=rate.amount_minor,
        )

    raise InvalidLine(f"unknown pay component: {component}",
                      {"component_code": component})


class PayRateUnitMismatchFor(PayrollError):
    """Satuan tarif tidak cocok dengan cara komponen itu dihitung."""

    code = "WORKFORCE_PAY_RATE_UNIT_MISMATCH"

    def __init__(self, component_code: str, unit: str, expected: str) -> None:
        super().__init__(
            f"tarif '{component_code}' bersatuan {unit}, diharapkan {expected}",
            {"component_code": component_code, "unit": unit, "expected": expected},
        )


def calculate(work: WorkSummary, bundle: PolicyBundle, *,
              deductions: Iterable[PayLine] = ()) -> PayrollCalculation:
    """Hitung satu slip: baris pendapatan, potongan eksplisit, dan upah bersih."""
    if bundle.staff_id != work.staff_id:
        raise StaffMismatch("policy bundle belongs to another staff member",
                            {"summary_staff": work.staff_id, "bundle_staff": bundle.staff_id})
    currency = bundle.currency
    if currency != work.currency:
        raise CurrencyMismatch(
            "calculation currency and rate currency differ",
            summary=work.currency,
            rates=currency,
        )

    overtime_check = check_overtime_bounds(work)

    earnings: list[PayLine] = []
    for component in sorted(bundle.rates):
        line = _earning_line(component, bundle.rates[component], work, bundle)
        if line is not None:
            earnings.append(line)

    deduction_lines = [line for line in deductions if line.kind in NET_REDUCING_KINDS]
    other_lines = [line for line in deductions if line.kind not in NET_REDUCING_KINDS]

    gross = sum(line.amount_minor for line in earnings)
    deduction_total = sum(line.amount_minor for line in deduction_lines)
    net = gross - deduction_total
    if net < 0:
        raise NegativeNetPay(
            "potongan melebihi pendapatan: periksa policy potongan",
            {"gross_minor": gross, "deduction_minor": deduction_total},
        )

    return PayrollCalculation(
        staff_id=work.staff_id,
        currency=currency,
        period_start=work.period_start,
        period_end=work.period_end,
        lines=tuple(earnings + deduction_lines + other_lines),
        gross_minor=gross,
        deduction_minor=deduction_total,
        net_minor=net,
        regular_minutes=work.regular_minutes,
        overtime_minutes=work.overtime_minutes,
        days_worked=work.days_worked,
        overtime_check=overtime_check,
    )
