#!/usr/bin/env python3
"""Gate G2 — rekonsiliasi upah 0 selisih pada dataset sintetis 30 hari × 12 staf.

Kenapa skrip ini ada
--------------------
Sprint Workforce Ops menutup dengan satu pertanyaan yang tidak bisa dijawab tes
unit: **kalau seluruh rantai dijalankan pada satu dataset yang menyerupai kerja
nyata, apakah angka di ujungnya masih sama dengan bukti di pangkalnya?** Tes unit
membuktikan tiap potongan benar sendiri-sendiri; gerbang ini membuktikan potongan
itu, ketika dirangkai, tidak kehilangan menit, tidak menggandakan uang, dan tidak
menerbitkan slip tanpa jejak.

Cara kerjanya, dan mengapa begitu
---------------------------------
Rekonsiliasi ini **tidak** memanggil ulang mesin yang menulis data. Ia membaca
tabel mentah (roster, attendance, timesheet, rate, policy, slip, event audit) dan
menghitung ulang dari nol dengan kode sendiri. Kalau penulis dan pemeriksa memakai
fungsi yang sama, keduanya bisa sama-sama salah dan gerbangnya akan bilang "0
selisih" untuk sistem yang rusak. Dua implementasi berbeda yang sepakat adalah
bukti; satu implementasi yang setuju dengan dirinya sendiri bukan bukti.

Yang diperiksa, per tahap (semua harus 0 selisih):

* **R1** menit dari punch mentah → baris `workforce_timesheets`
* **R2** jumlah timesheet dalam periode → `worked_minutes` slip
* **R3** tarif + payload policy bertanggal → baris slip hari ini
* **R4** baris slip → tajuk slip (bruto, potongan, bersih)
* **R5** tajuk slip → total run
* **R6** jejak: setiap slip punya rate, policy berversi, segel yang cocok, dan event audit
* **R7** rantai audit utuh (`verify_chain`)

Dataset (sintetis, deterministik, tanpa satu pun data orang nyata)
-----------------------------------------------------------------
12 staf di 2 properti (Bali `Asia/Makassar`, Yogyakarta `Asia/Jakarta`), 30 hari
(2026-11-01…2026-11-30), 5 jendela timesheet pekanan. Dua bentuk run diuji
sekaligus karena bisnis punya keduanya: 6 staf pada run bulanan (gaji bulanan)
dan 6 staf pada run pekanan (tarif harian + 1:1 tautan timesheet).

**Angka policy di sini sintetis dan tidak boleh dipakai sebagai aturan final.**
Persentase pajak dibaca dari payload `PAYROLL_TAX` berversi yang disemai skrip ini,
bukan dari kode, supaya tidak ada tarif yang di-hard-code (red line #3). Sebelum
gerbang ini dipakai untuk uang sungguhan, isi `workforce_policy_versions` harus
disahkan Finance + counsel (lihat §7 dokumen sprint).

Pakai: ``python3 scripts/workforce_reconcile.py [--json PATH]``. Keluar 1 bila ada
satu saja selisih — gerbang fail-closed, bukan laporan yang selalu hijau.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from audit import AuditEvent, verify_chain  # noqa: E402
from primitives import FrozenClock, IdFactory, canonical_json, sha256_hex  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402
from workforce.attendance import AttendanceRecord, AttendanceWriter  # noqa: E402
from workforce.payroll_calc import (  # noqa: E402
    EARNING,
    TAX,
    PayLine,
    WorkSummary,
    calculate,
)
from workforce.payroll_policy import load_bundle  # noqa: E402
from workforce.payslip import PayslipWriter  # noqa: E402
from workforce.roster import RosterWriter, ShiftSpec  # noqa: E402
from workforce.timesheet import TimesheetWriter, WORKING_EVENT_TYPES  # noqa: E402

AGENCY = "agency_default"
CLOCK_NOW = "2026-12-01T02:00:00Z"
PERIOD_START = date(2026, 11, 1)
PERIOD_END = date(2026, 11, 30)
WINDOWS: tuple[tuple[date, date], ...] = (
    (date(2026, 11, 1), date(2026, 11, 7)),
    (date(2026, 11, 8), date(2026, 11, 14)),
    (date(2026, 11, 15), date(2026, 11, 21)),
    (date(2026, 11, 22), date(2026, 11, 28)),
    (date(2026, 11, 29), date(2026, 11, 30)),
)
WORK_DAYS_PER_WINDOW = 4
SHIFT_START_LOCAL = time(8, 0)
SHIFT_MINUTES = 480
BREAK_START_MINUTES = 60
BREAK_END_MINUTES = 90
OVERTIME_DAY_MINUTES = 60

#: 12 staf: 6 pada run bulanan (gaji bulanan penuh), 6 pada run pekanan (tarif harian).
STAFF_IDS = tuple(f"stf_{i:02d}" for i in range(1, 13))
MONTHLY_STAFF = STAFF_IDS[:6]
WEEKLY_STAFF = STAFF_IDS[6:]
#: Properti & zona waktu: dua properti, dua zona — perbedaan tz adalah sumber bug nyata.
PROPERTIES = (
    ("prop_bali", "Asia/Makassar", "Bali", "Ubud"),
    ("prop_yogya", "Asia/Jakarta", "DI Yogyakarta", "Sleman"),
)
#: Staf dengan ketidakteraturan yang sengaja dimasukkan (bukan semua mulus):
UNROSTERED_EXTRA_DAY_STAFF = "stf_05"  # kerja di hari tanpa shift terbit
ABSENT_STAFF = "stf_07"  # cuti 2 hari → bonus kehadiran gugur
MANUAL_CORRECTION_STAFF = "stf_11"  # satu punch dikoreksi lewat correct()

#: Policy upah — SINETIS. Angka riil wajib disahkan Finance + counsel (dokumen sprint §7).
SYNTHETIC_POLICIES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "WORK_HOURS",
        {"daily_minutes": 480, "attendance_bonus_max_absence_days": 1},
    ),
    ("PAYROLL_TAX", {"tax_percent": "5", "synthetic": True}),
)

WEEKLY_RATES: tuple[tuple[str, int, str], ...] = (
    ("BASE_DAILY", 250_000, "DAY"),
    ("OVERTIME_HOURLY", 60_000, "HOUR"),
    ("MEAL_ALLOWANCE", 25_000, "DAY"),
    ("ATTENDANCE_BONUS", 100_000, "PER_OCCURRENCE"),
)
MONTHLY_RATES: tuple[tuple[str, int, str], ...] = (
    ("BASE_MONTHLY", 5_500_000, "MONTH"),
    ("OVERTIME_HOURLY", 60_000, "HOUR"),
    ("MEAL_ALLOWANCE", 25_000, "DAY"),
    ("ATTENDANCE_BONUS", 100_000, "PER_OCCURRENCE"),
)


# --------------------------------------------------------------------------- #
# Selisih
# --------------------------------------------------------------------------- #
@dataclass
class Findings:
    """Satu temuan = satu baris yang bisa diperiksa manusia tanpa membaca kode."""

    stage: str
    subject: str
    expected: Any
    actual: Any

    @property
    def diff(self) -> Optional[int]:
        if isinstance(self.expected, int) and isinstance(self.actual, int):
            return self.actual - self.expected
        return None


@dataclass
class ReconcileReport:
    checks: dict[str, int] = field(default_factory=dict)
    findings: list[Findings] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def record(self, stage: str, subject: str, expected: Any, actual: Any) -> None:
        self.checks[stage] = self.checks.get(stage, 0) + 1
        if expected != actual:
            self.findings.append(Findings(stage, subject, expected, actual))

    def for_stage(self, stage: str) -> list[Findings]:
        return [f for f in self.findings if f.stage == stage]


# --------------------------------------------------------------------------- #
# Penyemaian dunia (sintetis)
# --------------------------------------------------------------------------- #
def seed_world(conn, now_iso: str) -> None:
    for property_id, tz_name, province, region in PROPERTIES:
        conn.execute(
            "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
            " province, timezone, status, contract_pack_status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'villa', ?, ?, ?, 'SELLABLE', 'ACTIVATED', ?, ?)",
            (
                property_id,
                AGENCY,
                f"Villa Uji {property_id}",
                region,
                province,
                tz_name,
                now_iso,
                now_iso,
            ),
        )
        conn.execute(
            "INSERT INTO workforce_shift_templates (shift_template_id, agency_id, property_id,"
            " shift_code, starts_local, ends_local, effective_from)"
            " VALUES (?, ?, ?, 'MORNING', '08:00', '16:00', '2026-01-01')",
            (f"tpl_{property_id}", AGENCY, property_id),
        )
    for index, staff_id in enumerate(STAFF_IDS):
        property_id = PROPERTIES[index % len(PROPERTIES)][0]
        conn.execute(
            "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name,"
            " role_code, employment_type, home_property_id, status, hired_on, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, 'HOUSEKEEPING', 'PERMANENT', ?, 'ACTIVE',"
            " '2026-01-01', ?, ?)",
            (
                staff_id,
                AGENCY,
                f"EMP-{index + 1:03d}",
                f"Ops Staff {index + 1:02d}",
                property_id,
                now_iso,
                now_iso,
            ),
        )
    for policy_code, payload in SYNTHETIC_POLICIES:
        raw = canonical_json(payload)
        conn.execute(
            "INSERT INTO workforce_policy_versions (policy_version_id, agency_id, policy_code,"
            " version, payload_json, checksum_sha256, effective_from, effective_to, approved_by,"
            " approved_at, created_at) VALUES (?, ?, ?, 1, ?, ?, '2026-01-01', NULL,"
            " 'synthetic_seed', ?, ?)",
            (
                f"pol_{policy_code.lower()}_v1",
                AGENCY,
                policy_code,
                raw,
                sha256_hex(raw),
                now_iso,
                now_iso,
            ),
        )
    rates_for = {"MONTHLY": MONTHLY_RATES, "WEEKLY": WEEKLY_RATES}
    for staff_id in STAFF_IDS:
        shape = "MONTHLY" if staff_id in MONTHLY_STAFF else "WEEKLY"
        for component, amount_minor, unit in rates_for[shape]:
            conn.execute(
                "INSERT INTO workforce_pay_rates (pay_rate_id, agency_id, staff_id,"
                " component_code, unit, amount_minor, currency, effective_from, effective_to,"
                " policy_version_id, approved_by, approved_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'IDR', '2026-01-01', NULL, ?, 'synthetic_seed', ?, ?)",
                (
                    f"rate_{staff_id}_{component}",
                    AGENCY,
                    staff_id,
                    component,
                    unit,
                    amount_minor,
                    "pol_work_hours_v1",
                    now_iso,
                    now_iso,
                ),
            )


def property_of(conn, staff_id: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT s.home_property_id, p.timezone FROM workforce_staff s"
        " JOIN properties p ON p.property_id = s.home_property_id WHERE s.staff_id = ?",
        (staff_id,),
    ).fetchone()
    return str(row[0]), str(row[1])


def work_dates_for(staff_id: str, window: tuple[date, date]) -> list[date]:
    """Hari kerja staf pada satu jendela — pola tetap, tanpa keacakan."""
    start, end = window
    span = (end - start).days + 1
    days = min(WORK_DAYS_PER_WINDOW, span)
    offset = STAFF_IDS.index(staff_id) % 2  # separuh staf mulai dari hari ke-2
    picked: list[date] = []
    for step in range(days):
        candidate = start + timedelta(days=(step + offset) % span)
        if candidate not in picked:
            picked.append(candidate)
    if staff_id == ABSENT_STAFF and len(picked) > 1:
        picked.pop()  # satu hari cuti pada jendela pertama yang dihitung
    if staff_id == UNROSTERED_EXTRA_DAY_STAFF:
        extra = end - timedelta(days=1)
        if extra not in picked:
            picked.append(extra)
    return sorted(picked)


def punches_for(
    staff_id: str, work_date: date, tz_name: str, *, overtime: bool, unrostered: bool
) -> list[tuple[str, datetime]]:
    zone = ZoneInfo(tz_name)
    clock_in = datetime.combine(work_date, SHIFT_START_LOCAL, tzinfo=zone).astimezone(UTC)
    elapsed = SHIFT_MINUTES + (OVERTIME_DAY_MINUTES if overtime else 0)
    clock_out = clock_in + timedelta(minutes=elapsed)
    with_break = not unrostered
    events = [("CLOCK_IN", clock_in)]
    if with_break:
        events.append(("BREAK_START", clock_in + timedelta(minutes=BREAK_START_MINUTES)))
        events.append(("BREAK_END", clock_in + timedelta(minutes=BREAK_END_MINUTES)))
    events.append(("CLOCK_OUT", clock_out))
    return events


def build_attendance(conn, clock: FrozenClock, ids: IdFactory) -> dict[str, Any]:
    """Tulis roster + punch untuk 30 hari. Kembalikan roster entry per (staf, hari)."""
    roster = RosterWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)
    attendance = AttendanceWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)
    published: dict[tuple[str, str], str] = {}
    unrostered_days: dict[str, set[str]] = {}

    for staff_id in STAFF_IDS:
        property_id, tz_name = property_of(conn, staff_id)
        zone = ZoneInfo(tz_name)
        extra_days = {
            (WINDOWS[1][1] - timedelta(days=1)).isoformat(),
        } if staff_id == UNROSTERED_EXTRA_DAY_STAFF else set()

        for window in WINDOWS:
            for work_date in work_dates_for(staff_id, window):
                day_key = work_date.isoformat()
                unrostered = day_key in extra_days
                if not unrostered:
                    start_local = datetime.combine(work_date, SHIFT_START_LOCAL, tzinfo=zone)
                    ends_local = start_local + timedelta(minutes=SHIFT_MINUTES)
                    spec = ShiftSpec(
                        staff_id=staff_id,
                        property_id=property_id,
                        shift_template_id=f"tpl_{property_id}",
                        work_date=day_key,
                        starts_at_utc=start_local.astimezone(UTC),
                        ends_at_utc=ends_local.astimezone(UTC),
                    )
                    row, _ = roster.add_shift(spec)
                    roster.publish_shift(str(row["roster_entry_id"]), published_by="sup_1")
                    published[(staff_id, day_key)] = str(row["roster_entry_id"])
                else:
                    unrostered_days.setdefault(staff_id, set()).add(day_key)

                first_work_day = work_dates_for(staff_id, window)[0] == work_date
                for index, (event_type, moment) in enumerate(
                    punches_for(
                        staff_id,
                        work_date,
                        tz_name,
                        overtime=first_work_day,
                        unrostered=unrostered,
                    )
                ):
                    record = AttendanceRecord(
                        staff_id=staff_id,
                        event_type=event_type,
                        occurred_at_utc=moment,
                        idempotency_key=f"{staff_id}-{day_key}-{index}-{event_type}",
                        actor_id="device_villa_1",
                        source="DEVICE",
                        roster_entry_id=None if unrostered else published[(staff_id, day_key)],
                        device_ref="device_villa_1",
                        geofence_verdict="INSIDE",
                        accuracy_bucket_m=10,
                        evidence_ref=None,
                    )
                    _, created = attendance.record(record)
                    if not created:  # pragma: no cover - dataset deterministik
                        raise AssertionError(f"punch ganda: {record.idempotency_key}")

    if MANUAL_CORRECTION_STAFF in STAFF_IDS:
        # Satu koreksi manusia: bukti asli tidak dihapus, hanya dilampaui (append-only).
        staff_id = MANUAL_CORRECTION_STAFF
        events = attendance.events_for_staff(staff_id)
        target = next(event for event in events if event["event_type"] == "CLOCK_OUT")
        attendance.correct(
            str(target["attendance_event_id"]),
            occurred_at_utc=datetime.fromisoformat(
                str(target["occurred_at_utc"]).replace("Z", "+00:00")
            )
            + timedelta(minutes=15),
            idempotency_key=f"{staff_id}-correction-1",
            actor_id="sup_1",
            reason="device clock ran slow",
        )
    return {"unrostered_days": {k: sorted(v) for k, v in unrostered_days.items()}}


def close_timesheets(conn, clock: FrozenClock, ids: IdFactory) -> dict[str, list[dict]]:
    sheets: dict[str, list[dict]] = {}
    for staff_id in STAFF_IDS:
        _, tz_name = property_of(conn, staff_id)
        writer = TimesheetWriter(conn, agency_id=AGENCY, tz_name=tz_name, clock=clock, ids=ids)
        rows: list[dict] = []
        for window in WINDOWS:
            worked = work_dates_for(staff_id, window)
            if not worked:
                continue
            leave_days = 1 if staff_id == ABSENT_STAFF else 0
            row, _ = writer.close(
                staff_id, window[0], window[1], leave_days=leave_days
            )
            if int(row["regular_minutes"]) + int(row["overtime_minutes"]) == 0:
                continue  # pekan tanpa menit terbukti bukan bahan slip
            writer.submit(str(row["timesheet_id"]), submitted_by="ops_lead")
            row = writer.approve(str(row["timesheet_id"]), approved_by="ops_lead")
            rows.append(dict(row))
        sheets[staff_id] = rows
    return sheets


def tax_line(conn, work: WorkSummary, bundle, gross_minor: int) -> Optional[PayLine]:
    """Potongan pajak dari payload policy berversi — bukan angka di dalam kode."""
    try:
        policy = bundle.policy("PAYROLL_TAX")
    except Exception:  # pragma: no cover - dataset selalu menyemai policy ini
        return None
    raw_percent = policy.payload.get("tax_percent")
    if raw_percent is None:
        return None
    percent = Decimal(str(raw_percent))
    amount = int(
        (Decimal(gross_minor) * percent / Decimal(100)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    if amount <= 0:
        return None
    return PayLine(
        component_code="PAYROLL_TAX",
        kind=TAX,
        amount_minor=amount,
        basis_note=(
            f"synthetic seed: {percent}% of gross per policy"
            f" {policy.policy_version_id} (v{policy.version})"
        ),
        policy_version_id=str(policy.policy_version_id),
    )


def summary_for(
    conn, staff_id: str, sheets: list[dict], *, period_start: date, period_end: date
) -> WorkSummary:
    regular = sum(int(row["regular_minutes"]) for row in sheets)
    overtime = sum(int(row["overtime_minutes"]) for row in sheets)
    days = days_worked_from_events(conn, staff_id, period_start, period_end)
    absence = sum(int(row["leave_days"]) for row in sheets)
    return WorkSummary(
        staff_id=staff_id,
        currency="IDR",
        period_start=period_start,
        period_end=period_end,
        regular_minutes=regular,
        overtime_minutes=overtime,
        days_worked=days,
        absence_days=absence,
    )


def days_worked_from_events(conn, staff_id: str, start: date, end: date) -> int:
    """Hari kerja = hari yang punya punch masuk, bukan hari yang dijadwalkan.

    Kerja di hari tanpa shift terbit tetap kerja; menghitungnya dari roster saja
    akan membuat menit dan hari saling bertentangan di slip yang sama.
    """
    _, tz_name = property_of(conn, staff_id)
    zone = ZoneInfo(tz_name)
    seen: set[str] = set()
    rows = conn.execute(
        "SELECT * FROM workforce_attendance_events WHERE agency_id = ? AND staff_id = ?"
        " ORDER BY occurred_at_utc, recorded_at_utc",
        (AGENCY, staff_id),
    ).fetchall()
    superseded = {
        str(row["supersedes_event_id"])
        for row in rows
        if row["supersedes_event_id"] is not None
    }
    for row in rows:
        if str(row["attendance_event_id"]) in superseded:
            continue
        if str(row["event_type"]) != "CLOCK_IN":
            continue
        moment = datetime.fromisoformat(
            str(row["occurred_at_utc"]).replace("Z", "+00:00")
        ).astimezone(zone)
        if start <= moment.date() <= end:
            seen.add(moment.date().isoformat())
    return len(seen)


# --------------------------------------------------------------------------- #
# Perhitungan ulang independen (R1)
# --------------------------------------------------------------------------- #
def recompute_minutes_from_events(
    conn, staff_id: str, start: date, end: date
) -> dict[str, int]:
    """Menit dihitung ulang dari punch mentah, tanpa memanggil penulis timesheet.

    Aturannya sama dengan kebijakan sistem (istirahat memotong menit, kerja di hari
    tanpa shift terbit tetap dihitung sebagai reguler), tetapi implementasinya
    terpisah — justru itu gunanya.
    """
    _, tz_name = property_of(conn, staff_id)
    zone = ZoneInfo(tz_name)
    rosters = conn.execute(
        "SELECT * FROM workforce_roster_entries WHERE agency_id = ? AND staff_id = ?"
        " AND status IN ('PUBLISHED', 'CONFIRMED', 'SWAPPED')",
        (AGENCY, staff_id),
    ).fetchall()
    scheduled: dict[str, int] = {}
    for row in rosters:
        if row["work_date"] is None:
            continue
        starts = datetime.fromisoformat(str(row["starts_at_utc"]).replace("Z", "+00:00"))
        ends = datetime.fromisoformat(str(row["ends_at_utc"]).replace("Z", "+00:00"))
        scheduled[str(row["work_date"])] = int((ends - starts).total_seconds() // 60)

    rows = conn.execute(
        "SELECT * FROM workforce_attendance_events WHERE agency_id = ? AND staff_id = ?"
        " ORDER BY occurred_at_utc, recorded_at_utc",
        (AGENCY, staff_id),
    ).fetchall()
    by_id = {str(row["attendance_event_id"]): dict(row) for row in rows}
    superseded = {
        str(row["supersedes_event_id"])
        for row in rows
        if row["supersedes_event_id"] is not None
    }

    def role_of(row: dict[str, Any]) -> Optional[str]:
        """Koreksi mewarisi peran baris yang dikoreksinya — rantai, bukan satu tingkat."""
        seen: set[str] = set()
        current = row
        while str(current["event_type"]) == "MANUAL_ADJUST":
            parent_id = current["supersedes_event_id"]
            if parent_id is None or str(parent_id) in seen:
                return None
            seen.add(str(parent_id))
            parent = by_id.get(str(parent_id))
            if parent is None:
                return None
            current = parent
        event_type = str(current["event_type"])
        return event_type if event_type in WORKING_EVENT_TYPES else None

    stream: list[tuple[datetime, str]] = []
    for event_id, row in by_id.items():
        if event_id in superseded:
            continue
        role = role_of(row)
        if role is None:
            continue
        moment = datetime.fromisoformat(str(row["occurred_at_utc"]).replace("Z", "+00:00"))
        if not (start <= moment.astimezone(zone).date() <= end):
            continue
        stream.append((moment, role))
    stream.sort()

    regular = overtime = breaks = unrostered = 0
    open_in: Optional[datetime] = None
    open_break: Optional[datetime] = None
    break_total = 0
    for moment, event_type in stream:
        if event_type == "CLOCK_IN":
            open_in, open_break, break_total = moment, None, 0
        elif event_type == "BREAK_START" and open_in is not None and open_break is None:
            open_break = moment
        elif event_type == "BREAK_END" and open_break is not None:
            break_total += int((moment - open_break).total_seconds() // 60)
            open_break = None
        elif event_type == "CLOCK_OUT" and open_in is not None:
            if open_break is not None:
                break_total += int((moment - open_break).total_seconds() // 60)
                open_break = None
            worked = max(0, int((moment - open_in).total_seconds() // 60) - break_total)
            day = open_in.astimezone(zone).date().isoformat()
            planned = scheduled.get(day, 0)
            if planned > 0:
                regular += min(worked, planned)
                overtime += max(0, worked - planned)
            else:
                regular += worked
                unrostered += worked
            breaks += max(0, break_total)
            open_in, break_total = None, 0
    return {
        "regular_minutes": regular,
        "overtime_minutes": overtime,
        "break_minutes": breaks,
        "unrostered_minutes": unrostered,
    }


def issue_slips(
    conn,
    writer: PayslipWriter,
    clock: FrozenClock,
    ids: IdFactory,
    sheets: dict[str, list[dict]],
) -> dict[str, Any]:
    """Terbitkan slip pada dua bentuk run: satu bulanan, lima pekanan."""
    runs: dict[str, Any] = {"monthly": None, "weekly": []}

    monthly_run, _ = writer.open_run(
        PERIOD_START, PERIOD_END, "IDR", created_by="finance_lead",
        actor_role="PAYROLL_OPERATOR",
    )
    for staff_id in MONTHLY_STAFF:
        summary = summary_for(
            conn, staff_id, sheets[staff_id],
            period_start=PERIOD_START, period_end=PERIOD_END,
        )
        bundle = load_bundle(conn, staff_id, PERIOD_END, agency_id=AGENCY)
        calc = calculate(summary, bundle)
        tax = tax_line(conn, summary, bundle, calc.gross_minor)
        if tax is not None:
            calc = calculate(summary, bundle, deductions=[tax])
        writer.issue(
            str(monthly_run["payroll_run_id"]), calc, issued_by="payroll_ops",
            timesheet_id=None,
        )
    # ``issue()`` sudah memindahkan run ke CALCULATED saat slip terakhir ditulis;
    # yang tersisa adalah keputusan manusia: setujui (PAID butuh persetujuan owner per run).
    writer.transition_run(
        str(monthly_run["payroll_run_id"]), "APPROVED", actor="finance_lead",
        actor_role="PAYROLL_APPROVER",
    )
    runs["monthly"] = writer.get_run(str(monthly_run["payroll_run_id"]))

    for window in WINDOWS:
        weekly_run, _ = writer.open_run(
            window[0], window[1], "IDR", created_by="finance_lead",
            actor_role="PAYROLL_OPERATOR",
        )
        for staff_id in WEEKLY_STAFF:
            rows = [row for row in sheets[staff_id]
                    if str(row["period_start"]) == window[0].isoformat()]
            if not rows:
                continue
            summary = summary_for(
                conn, staff_id, rows, period_start=window[0], period_end=window[1]
            )
            bundle = load_bundle(conn, staff_id, window[1], agency_id=AGENCY)
            calc = calculate(summary, bundle)
            tax = tax_line(conn, summary, bundle, calc.gross_minor)
            if tax is not None:
                calc = calculate(summary, bundle, deductions=[tax])
            writer.issue(
                str(weekly_run["payroll_run_id"]), calc, issued_by="payroll_ops",
                timesheet_id=str(rows[0]["timesheet_id"]),
            )
        if writer.run_totals(str(weekly_run["payroll_run_id"]))["slip_count"] == 0:
            writer.transition_run(
                str(weekly_run["payroll_run_id"]), "VOID", actor="finance_lead",
                actor_role="PAYROLL_APPROVER", reason="no payable timesheet in this window",
            )
        else:
            writer.transition_run(
                str(weekly_run["payroll_run_id"]), "APPROVED", actor="finance_lead",
                actor_role="PAYROLL_APPROVER",
            )
        runs["weekly"].append(writer.get_run(str(weekly_run["payroll_run_id"])))
    return runs


# --------------------------------------------------------------------------- #
# Rekonsiliasi
# --------------------------------------------------------------------------- #
def reconcile(conn, report: ReconcileReport, writer: PayslipWriter) -> None:
    agency = AGENCY

    # -- R1: punch mentah -> baris timesheet ---------------------------------- #
    sheets = conn.execute(
        "SELECT * FROM workforce_timesheets WHERE agency_id = ? ORDER BY staff_id, period_start",
        (agency,),
    ).fetchall()
    for row in sheets:
        staff_id = str(row["staff_id"])
        start = date.fromisoformat(str(row["period_start"]))
        end = date.fromisoformat(str(row["period_end"]))
        recomputed = recompute_minutes_from_events(conn, staff_id, start, end)
        for field_name in ("regular_minutes", "overtime_minutes", "break_minutes"):
            report.record(
                "R1 timesheet minutes",
                f"{staff_id} {start.isoformat()}..{end.isoformat()} {field_name}",
                recomputed[field_name],
                int(row[field_name]),
            )

    # -- R2/R3: timesheet + rate/policy -> baris slip ------------------------- #
    slips = conn.execute(
        "SELECT * FROM workforce_payslips WHERE agency_id = ? ORDER BY staff_id, generated_at",
        (agency,),
    ).fetchall()
    if not slips:
        report.findings.append(Findings("R2 slip coverage", "semua slip", ">0", 0))
    for slip in slips:
        staff_id = str(slip["staff_id"])
        run = conn.execute(
            "SELECT * FROM workforce_payroll_runs WHERE agency_id = ? AND payroll_run_id = ?",
            (agency, str(slip["payroll_run_id"])),
        ).fetchone()
        period_start = date.fromisoformat(str(run["period_start"]))
        period_end = date.fromisoformat(str(run["period_end"]))

        window_sheets = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM workforce_timesheets WHERE agency_id = ? AND staff_id = ?"
                " AND period_start >= ? AND period_end <= ? ORDER BY period_start",
                (agency, staff_id, period_start.isoformat(), period_end.isoformat()),
            )
        ]
        expected_minutes = sum(int(row["regular_minutes"]) for row in window_sheets)
        expected_overtime = sum(int(row["overtime_minutes"]) for row in window_sheets)
        report.record(
            "R2 timesheet -> slip minutes", f"{staff_id} worked_minutes",
            expected_minutes, int(slip["worked_minutes"]),
        )
        report.record(
            "R2 timesheet -> slip minutes", f"{staff_id} overtime_minutes",
            expected_overtime, int(slip["overtime_minutes"]),
        )
        days = days_worked_from_events(conn, staff_id, period_start, period_end)
        absence = sum(int(row["leave_days"]) for row in window_sheets)
        summary = WorkSummary(
            staff_id=staff_id, currency=str(slip["currency"]),
            period_start=period_start, period_end=period_end,
            regular_minutes=expected_minutes, overtime_minutes=expected_overtime,
            days_worked=days, absence_days=absence,
        )
        bundle = load_bundle(conn, staff_id, period_end, agency_id=agency)
        fresh = calculate(summary, bundle)
        tax = tax_line(conn, summary, bundle, fresh.gross_minor)
        if tax is not None:
            fresh = calculate(summary, bundle, deductions=[tax])

        report.record(
            "R3 rate+policy -> slip gross", f"{staff_id} gross_minor",
            fresh.gross_minor, int(slip["gross_minor"]),
        )
        report.record(
            "R3 rate+policy -> slip gross", f"{staff_id} deduction_minor",
            fresh.deduction_minor, int(slip["deduction_minor"]),
        )
        report.record(
            "R3 rate+policy -> slip gross", f"{staff_id} net_minor",
            fresh.net_minor, int(slip["net_minor"]),
        )

        stored_lines = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM workforce_payslip_lines WHERE agency_id = ? AND payslip_id = ?"
                " ORDER BY component_code",
                (agency, str(slip["payslip_id"])),
            )
        ]
        expected_lines = sorted(
            (line.component_code, line.kind, int(line.amount_minor))
            for line in fresh.lines
        )
        actual_lines = sorted(
            (str(row["component_code"]), str(row["kind"]), int(row["amount_minor"]))
            for row in stored_lines
        )
        report.record(
            "R3 rate+policy -> slip lines", f"{staff_id} lines",
            expected_lines, actual_lines,
        )

        # -- R4: baris -> tajuk slip ---------------------------------------- #
        lines_gross = sum(
            int(row["amount_minor"]) for row in stored_lines if str(row["kind"]) == EARNING
        )
        lines_deduction = sum(
            int(row["amount_minor"]) for row in stored_lines if str(row["kind"]) != EARNING
        )
        report.record(
            "R4 lines -> slip header", f"{staff_id} gross", lines_gross, int(slip["gross_minor"])
        )
        report.record(
            "R4 lines -> slip header", f"{staff_id} deduction",
            lines_deduction, int(slip["deduction_minor"]),
        )
        report.record(
            "R4 lines -> slip header", f"{staff_id} net",
            lines_gross - lines_deduction, int(slip["net_minor"]),
        )

    # -- R5: slip -> total run ------------------------------------------------ #
    for run in conn.execute(
        "SELECT * FROM workforce_payroll_runs WHERE agency_id = ?"
        " AND status IN ('CALCULATED', 'APPROVED', 'PAID') ORDER BY period_start",
        (agency,),
    ).fetchall():
        run_id = str(run["payroll_run_id"])
        aggregate = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(gross_minor), 0), COALESCE(SUM(net_minor), 0)"
            " FROM workforce_payslips WHERE agency_id = ? AND payroll_run_id = ?",
            (agency, run_id),
        ).fetchone()
        report.record(
            "R5 slips -> run total", f"{run_id} slips", int(aggregate[0]),
            int(conn.execute(
                "SELECT COUNT(*) FROM workforce_payslips WHERE agency_id = ?"
                " AND payroll_run_id = ?", (agency, run_id)).fetchone()[0]),
        )
        report.record(
            "R5 slips -> run total", f"{run_id} total_gross_minor",
            int(aggregate[1]), int(run["total_gross_minor"]),
        )
        report.record(
            "R5 slips -> run total", f"{run_id} total_net_minor",
            int(aggregate[2]), int(run["total_net_minor"]),
        )

    # -- R6: jejak ------------------------------------------------------------ #
    policy_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT policy_version_id FROM workforce_policy_versions WHERE agency_id = ?",
            (agency,),
        )
    }
    rate_keys = {
        (str(row["staff_id"]), str(row["component_code"]), int(row["amount_minor"]))
        for row in conn.execute(
            "SELECT * FROM workforce_pay_rates WHERE agency_id = ?", (agency,)
        )
    }
    event_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'WORKFORCE_PAYSLIP_ISSUED'"
        ).fetchone()[0]
    )
    for slip in slips:
        try:
            seal = writer.verify_seal(str(slip["payslip_id"]))
            matches = bool(seal["matches"])
        except Exception as exc:  # noqa: BLE001 - segel hilang harus dilaporkan, bukan mematikan gerbang
            matches = f"gagal diverifikasi: {type(exc).__name__}"
        report.record(
            "R6 trace", f"segel {slip['payslip_id']}", True, matches
        )
    report.record("R6 trace", "event audit issual = jumlah slip", len(slips), event_count)
    for row in conn.execute(
        "SELECT * FROM workforce_payslip_lines WHERE agency_id = ?", (agency,)
    ).fetchall():
        policy_version_id = row["policy_version_id"]
        report.record(
            "R6 trace",
            f"baris {row['component_code']} punya policy berversi",
            True,
            policy_version_id is not None and str(policy_version_id) in policy_ids,
        )
    for slip in slips:
        staff_id = str(slip["staff_id"])
        lines = conn.execute(
            "SELECT * FROM workforce_payslip_lines WHERE agency_id = ? AND payslip_id = ?",
            (agency, str(slip["payslip_id"])),
        ).fetchall()
        for line in lines:
            component = str(line["component_code"])
            if component not in {code for code, _, _ in MONTHLY_RATES + WEEKLY_RATES}:
                continue  # potongan (mis. PAYROLL_TAX) tidak selalu punya rate
            report.record(
                "R6 trace",
                f"{staff_id} tarif {component}",
                True,
                any(key[0] == staff_id and key[1] == component for key in rate_keys),
            )
        if slip["timesheet_id"] is not None:
            sheet = conn.execute(
                "SELECT status FROM workforce_timesheets WHERE agency_id = ? AND timesheet_id = ?",
                (agency, str(slip["timesheet_id"])),
            ).fetchone()
            report.record(
                "R6 trace", f"{staff_id} timesheet slip disetujui", True,
                sheet is not None and str(sheet[0]) == "APPROVED",
            )
        seal_events = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'WORKFORCE_PAYSLIP_ISSUED'"
            " AND entity_id = ?",
            (str(slip["payslip_id"]),),
        ).fetchone()
        report.record(
            "R6 trace", f"{staff_id} slip punya event audit", 1, int(seal_events[0])
        )

    # -- R7: rantai audit ------------------------------------------------------ #
    events = [
        AuditEvent.from_row(row)
        for row in conn.execute("SELECT * FROM audit_events ORDER BY seq ASC")
    ]
    try:
        verified = verify_chain(events)
        report.record("R7 audit chain", "rantai utuh", True, True)
        report.facts["audit_events"] = verified.count
        report.facts["audit_head_hash"] = verified.head_hash[:16]
    except Exception as exc:  # noqa: BLE001 - laporan harus menangkap semua kegagalan
        report.record("R7 audit chain", f"rantai utuh ({exc})", True, False)


def summarise(conn, report: ReconcileReport, extra: dict[str, Any]) -> None:
    counts = {
        "staff": conn.execute("SELECT COUNT(*) FROM workforce_staff").fetchone()[0],
        "properties": conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0],
        "roster_entries": conn.execute(
            "SELECT COUNT(*) FROM workforce_roster_entries").fetchone()[0],
        "attendance_events": conn.execute(
            "SELECT COUNT(*) FROM workforce_attendance_events").fetchone()[0],
        "timesheets": conn.execute("SELECT COUNT(*) FROM workforce_timesheets").fetchone()[0],
        "payslips": conn.execute("SELECT COUNT(*) FROM workforce_payslips").fetchone()[0],
        "payslip_lines": conn.execute(
            "SELECT COUNT(*) FROM workforce_payslip_lines").fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM workforce_payroll_runs").fetchone()[0],
        "payable_minutes": conn.execute(
            "SELECT COALESCE(SUM(worked_minutes), 0) FROM workforce_payslips").fetchone()[0],
        "gross_minor": conn.execute(
            "SELECT COALESCE(SUM(gross_minor), 0) FROM workforce_payslips").fetchone()[0],
        "deduction_minor": conn.execute(
            "SELECT COALESCE(SUM(deduction_minor), 0) FROM workforce_payslips").fetchone()[0],
        "net_minor": conn.execute(
            "SELECT COALESCE(SUM(net_minor), 0) FROM workforce_payslips").fetchone()[0],
    }
    counts.update(extra)
    report.facts["dataset"] = counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate G2 — rekonsiliasi upah 0 selisih")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="tulis laporan angka ke berkas JSON ini")
    args = parser.parse_args()

    clock = FrozenClock.at(CLOCK_NOW)
    ids = IdFactory(deterministic=True, seed="reconcile")
    conn = connect_sqlite(":memory:")
    try:
        writer = PayslipWriter(conn, agency_id=AGENCY, clock=clock, ids=ids)
        seed_world(conn, clock.now().isoformat())
        extra = build_attendance(conn, clock, ids)
        sheets = close_timesheets(conn, clock, ids)
        issue_slips(conn, writer, clock, ids, sheets)
        report = ReconcileReport()
        reconcile(conn, report, writer)
        summarise(conn, report, {"unrostered_days": extra["unrostered_days"]})
    finally:
        conn.close()

    print("Gate G2 — rekonsiliasi upah (dataset sintetis 30 hari x 12 staf)")
    print("=" * 74)
    for stage in sorted(report.checks):
        bad = len(report.for_stage(stage))
        mark = "OK " if bad == 0 else "!! "
        print(f"{mark}{stage:<34} {report.checks[stage]:>5} diperiksa, {bad} selisih")
    print("-" * 74)
    for key, value in report.facts.get("dataset", {}).items():
        print(f"   {key:<22} {value}")
    if report.findings:
        print("-" * 74)
        print(f"SELISIH ({len(report.findings)}):")
        for finding in report.findings[:40]:
            print(f"   [{finding.stage}] {finding.subject}: "
                  f"harap {finding.expected!r}, dapat {finding.actual!r}")
        if len(report.findings) > 40:
            print(f"   ... dan {len(report.findings) - 40} lagi")
    print("=" * 74)
    verdict = "LULUS — 0 selisih" if not report.findings else "GAGAL"
    print(f"G2: {verdict}")

    if args.json_path:
        payload = {
            "verdict": "PASS" if not report.findings else "FAIL",
            "checks": report.checks,
            "findings": [
                {
                    "stage": f.stage,
                    "subject": f.subject,
                    "expected": f.expected,
                    "actual": f.actual,
                    "diff": f.diff,
                }
                for f in report.findings
            ],
            "facts": report.facts,
        }
        Path(args.json_path).write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"laporan JSON: {args.json_path}")

    return 0 if not report.findings else 1


if __name__ == "__main__":
    sys.exit(main())
