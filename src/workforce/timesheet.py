"""Timesheet — mengubah bukti kehadiran menjadi angka yang bisa diperiksa (T1.4).

Aturan yang ditegakkan di sini, dan alasannya:

1. **Angka lahir dari bukti, bukan dari ingatan.** Semua menit dihitung dari baris
   ``workforce_attendance_events`` di dalam periode; tidak ada jalur untuk menulis
   menit langsung. Koreksi (``MANUAL_ADJUST``) tetap dihormati: baris yang
   dikoreksi tidak dipakai, baris koreksinya dipakai dengan **peran** baris lama.
2. **Ponsel yang lupa clock-out bukan alasan menebak.** Kehadiran yang belum
   berpasangan menolak penutupan periode (``OpenPunch``); angka yang ditebak akan
   menjadi upah yang salah dibayar atau salah ditahan.
3. **Pembagian hari memakai jam properti, bukan jam server.** ``tz_name`` wajib
   sama dengan zona properti shift yang dikerjakan staf pada periode itu; selisih
   zona adalah kesalahan konfigurasi payroll, bukan sesuatu yang diratakan diam-diam.
4. **Lembur hanya yang bisa dibuktikan.** Menit di luar jadwal shift yang
   dipublikasikan dihitung lembur; jam tanpa jadwal tetap reguler dan dicatat
   sebagai ``unrostered_minutes`` supaya terlihat, bukan disembunyikan.
5. **Periode ≥2400 menit ditolak.** Empat puluh jam dalam satu periode tidak bisa
   diverifikasi sistem ini; ia harus lewat pemeriksaan manusia di luar alur biasa.
6. **Setelah disetujui, angka tidak bisa berubah.** Persetujuan menyimpan
   ``lock_hash`` atas isi yang disetujui; ``close()`` pada lembar berstatus
   SUBMITTED/APPROVED/LOCKED ditolak, sehingga slip upah selalu bisa menunjuk
   angka yang persis sama dengan yang disetujui.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from primitives import (
    UTC,
    DomainError,
    EntityNotFound,
    IdFactory,
    SystemClock,
    fingerprint,
    local_date,
    parse_iso,
    parse_iso_date,
    require_text,
    to_iso,
    to_iso_date,
)
from workforce.repository import write_transaction

TIMESHEET_ID_PREFIX = "timesheet"

#: Batas periode yang ditolak, bukan diratakan: 40 jam.
PERIOD_MAX_MINUTES = 2400
#: Batas kolom ``overtime_minutes`` di skema; dipakai agar pesan errornya domain.
OVERTIME_MAX_MINUTES = 1080

WORKING_EVENT_TYPES = ("CLOCK_IN", "CLOCK_OUT", "BREAK_START", "BREAK_END")
EDITABLE_STATUSES = ("OPEN", "REJECTED")
ROSTERED_STATUSES = ("PUBLISHED", "CONFIRMED", "SWAPPED")


class TimesheetError(DomainError):
    """Kegagalan menyusun atau menyetujui timesheet."""

    code = "WORKFORCE_TIMESHEET_ERROR"


class TimesheetNotFound(TimesheetError):
    code = "WORKFORCE_TIMESHEET_NOT_FOUND"


class TimesheetLocked(TimesheetError):
    code = "WORKFORCE_TIMESHEET_LOCKED"


class InvalidTransition(TimesheetError):
    code = "WORKFORCE_TIMESHEET_INVALID_TRANSITION"


class OpenPunch(TimesheetError):
    code = "WORKFORCE_TIMESHEET_OPEN_PUNCH"


class PeriodTooLong(TimesheetError):
    code = "WORKFORCE_TIMESHEET_PERIOD_TOO_LONG"


class PeriodEmpty(TimesheetError):
    code = "WORKFORCE_TIMESHEET_PERIOD_EMPTY"


class PeriodTimezoneMismatch(TimesheetError):
    code = "WORKFORCE_TIMESHEET_TIMEZONE_MISMATCH"


class InvalidPeriod(TimesheetError):
    code = "WORKFORCE_TIMESHEET_INVALID_PERIOD"


@dataclass(frozen=True)
class PeriodMinutes:
    """Hasil hitung satu periode, sebelum disimpan."""

    regular_minutes: int
    overtime_minutes: int
    break_minutes: int
    unrostered_minutes: int = 0
    days_worked: int = 0
    open_punches: tuple[str, ...] = field(default_factory=tuple)

    @property
    def worked_minutes(self) -> int:
        return self.regular_minutes + self.overtime_minutes


def _period_dates(period_start: Any, period_end: Any) -> tuple[date, date]:
    def as_date(value: Any, field_name: str) -> date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return parse_iso_date(require_text(value, field_name, max_len=10))

    start = as_date(period_start, "period_start")
    end = as_date(period_end, "period_end")
    if end < start:
        raise InvalidPeriod(
            "period_end cannot precede period_start",
            period_start=to_iso_date(start),
            period_end=to_iso_date(end),
        )
    return start, end


def _require_reason(value: Any, field_name: str = "reason") -> str:
    text = require_text(value, field_name, max_len=200)
    if len(text) < 3:
        raise InvalidTransition(
            f"{field_name} must explain the decision",
            code="WORKFORCE_REASON_TOO_SHORT",
            **{field_name: text},
        )
    return text


class TimesheetWriter:
    """Penutupan periode, pengajuan, dan persetujuan untuk satu agency."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        agency_id: str = "agency_default",
        tz_name: str = "Asia/Makassar",
        clock: Any = None,
        ids: Optional[IdFactory] = None,
    ) -> None:
        self.conn = conn
        self.agency_id = require_text(agency_id, "agency_id", max_len=64)
        self.tz_name = require_text(tz_name, "tz_name", max_len=64)
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()

    # -- pembacaan -------------------------------------------------------- #
    def get(self, timesheet_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM workforce_timesheets WHERE agency_id = ? AND timesheet_id = ?",
            (self.agency_id, require_text(timesheet_id, "timesheet_id", max_len=64)),
        ).fetchone()
        if row is None:
            raise TimesheetNotFound("timesheet not found", timesheet_id=timesheet_id)
        return dict(row)

    def for_period(self, staff_id: str, period_start: Any) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM workforce_timesheets"
            " WHERE agency_id = ? AND staff_id = ? AND period_start = ?",
            (
                self.agency_id,
                require_text(staff_id, "staff_id", max_len=64),
                require_text(period_start, "period_start", max_len=10),
            ),
        ).fetchone()
        return dict(row) if row is not None else None

    # -- hitung ------------------------------------------------------------ #
    def compute(self, staff_id: str, period_start: Any, period_end: Any) -> PeriodMinutes:
        """Hitung menit periode dari bukti kehadiran. Tidak menulis apa pun."""
        staff = require_text(staff_id, "staff_id", max_len=64)
        start, end = _period_dates(period_start, period_end)
        self._require_staff(staff)
        self._require_same_timezone(staff, start, end)

        events = self._effective_events(staff, start, end)
        scheduled = self._scheduled_minutes(staff, start, end)

        regular = overtime = breaks = unrostered = 0
        days: set[str] = set()
        open_punches: list[str] = []
        open_in: Optional[datetime] = None
        open_break: Optional[datetime] = None
        break_total = 0

        for moment, role in events:
            if role == "CLOCK_IN":
                if open_in is not None:
                    open_punches.append(to_iso(open_in))
                    break_total = 0
                open_in = moment
                open_break = None
                break_total = 0
            elif role == "BREAK_START":
                if open_in is not None and open_break is None:
                    open_break = moment
            elif role == "BREAK_END":
                if open_break is not None:
                    break_total += int((moment - open_break).total_seconds() // 60)
                    open_break = None
            elif role == "CLOCK_OUT":
                if open_in is None:
                    continue  # clock-out tanpa clock-in: tidak menambah menit, terlihat di audit
                if open_break is not None:
                    break_total += int((moment - open_break).total_seconds() // 60)
                    open_break = None
                worked = int((moment - open_in).total_seconds() // 60) - break_total
                worked = max(0, worked)
                day = local_date(open_in, self.tz_name).isoformat()
                planned = scheduled.get(day, 0)
                days.add(day)
                if planned > 0:
                    regular += min(worked, planned)
                    overtime += max(0, worked - planned)
                else:
                    regular += worked
                    unrostered += worked
                breaks += max(0, break_total)
                open_in = None
                break_total = 0

        if open_in is not None:
            open_punches.append(to_iso(open_in))

        if open_punches:
            raise OpenPunch(
                "attendance has punches that never closed; correct them before closing the period",
                staff_id=staff,
                open_punches=list(open_punches),
            )

        if regular + overtime >= PERIOD_MAX_MINUTES:
            raise PeriodTooLong(
                f"a period of {PERIOD_MAX_MINUTES} minutes or more needs human review",
                staff_id=staff,
                worked_minutes=regular + overtime,
                limit_minutes=PERIOD_MAX_MINUTES,
            )
        if overtime > OVERTIME_MAX_MINUTES:
            raise PeriodTooLong(
                "overtime exceeds the number of minutes the schema can hold",
                staff_id=staff,
                overtime_minutes=overtime,
                limit_minutes=OVERTIME_MAX_MINUTES,
            )

        return PeriodMinutes(
            regular_minutes=regular,
            overtime_minutes=overtime,
            break_minutes=breaks,
            unrostered_minutes=unrostered,
            days_worked=len(days),
        )

    # -- siklus hidup ------------------------------------------------------ #
    def close(
        self,
        staff_id: str,
        period_start: Any,
        period_end: Any,
        *,
        leave_days: int = 0,
    ) -> tuple[dict[str, Any], bool]:
        """Tulis atau perbarui angka periode. ``(row, created)``."""
        start, end = _period_dates(period_start, period_end)
        if isinstance(leave_days, bool) or not isinstance(leave_days, int):
            raise TimesheetError("leave_days must be an integer", leave_days=leave_days)
        if leave_days < 0 or leave_days > 31:
            raise TimesheetError(
                "leave_days must be between 0 and 31", leave_days=leave_days
            )

        minutes = self.compute(staff_id, start, end)
        existing = self.for_period(staff_id, start.isoformat())

        with write_transaction(self.conn):
            if existing is None:
                record: dict[str, Any] = {
                    "agency_id": self.agency_id,
                    "timesheet_id": self.ids.new(TIMESHEET_ID_PREFIX),
                    "staff_id": staff_id,
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "status": "OPEN",
                    "regular_minutes": minutes.regular_minutes,
                    "overtime_minutes": minutes.overtime_minutes,
                    "break_minutes": minutes.break_minutes,
                    "leave_days": leave_days,
                    "version": 1,
                }
                columns = ", ".join(record)
                marks = ", ".join("?" for _ in record)
                self.conn.execute(
                    f"INSERT INTO workforce_timesheets ({columns}) VALUES ({marks})",
                    tuple(record.values()),
                )
                return self.get(str(record["timesheet_id"])), True

            status = str(existing["status"])
            if status not in EDITABLE_STATUSES:
                raise TimesheetLocked(
                    "this period is already submitted or approved; reopen it before editing",
                    timesheet_id=str(existing["timesheet_id"]),
                    status=status,
                )
            self.conn.execute(
                "UPDATE workforce_timesheets SET period_end = ?, regular_minutes = ?,"
                " overtime_minutes = ?, break_minutes = ?, leave_days = ?, version = version + 1"
                " WHERE agency_id = ? AND timesheet_id = ?",
                (
                    end.isoformat(),
                    minutes.regular_minutes,
                    minutes.overtime_minutes,
                    minutes.break_minutes,
                    leave_days,
                    self.agency_id,
                    str(existing["timesheet_id"]),
                ),
            )
            return self.get(str(existing["timesheet_id"])), False

    def submit(self, timesheet_id: str, *, submitted_by: str) -> dict[str, Any]:
        """Ajukan periode untuk disetujui; hanya dari OPEN atau REJECTED."""
        # Identitas pengaju divalidasi; kolomnya belum ada di skema sehingga tidak disimpan.
        require_text(submitted_by, "submitted_by", max_len=64)
        row = self.get(timesheet_id)
        status = str(row["status"])
        if status not in EDITABLE_STATUSES:
            raise TimesheetLocked(
                "only OPEN or REJECTED timesheets can be submitted",
                timesheet_id=str(row["timesheet_id"]),
                status=status,
            )
        if int(row["regular_minutes"]) + int(row["overtime_minutes"]) <= 0:
            raise PeriodEmpty(
                "a period without worked minutes is not submitted for approval",
                timesheet_id=str(row["timesheet_id"]),
            )
        with write_transaction(self.conn):
            self.conn.execute(
                "UPDATE workforce_timesheets SET status = 'SUBMITTED', submitted_at = ?,"
                " rejected_reason = NULL, version = version + 1"
                " WHERE agency_id = ? AND timesheet_id = ?",
                (self._now_iso(), self.agency_id, str(row["timesheet_id"])),
            )
        return self.get(str(row["timesheet_id"]))

    def approve(self, timesheet_id: str, *, approved_by: str) -> dict[str, Any]:
        """Setujui periode: isi dibekukan oleh ``lock_hash``."""
        approver = require_text(approved_by, "approved_by", max_len=64)
        row = self.get(timesheet_id)
        if str(row["status"]) != "SUBMITTED":
            raise InvalidTransition(
                "only a submitted timesheet can be approved",
                timesheet_id=str(row["timesheet_id"]),
                status=str(row["status"]),
            )
        stamp = self._now_iso()
        lock_hash = fingerprint(
            {
                "agency_id": self.agency_id,
                "timesheet_id": str(row["timesheet_id"]),
                "staff_id": str(row["staff_id"]),
                "period_start": str(row["period_start"]),
                "period_end": str(row["period_end"]),
                "regular_minutes": int(row["regular_minutes"]),
                "overtime_minutes": int(row["overtime_minutes"]),
                "break_minutes": int(row["break_minutes"]),
                "leave_days": int(row["leave_days"]),
                "version": int(row["version"]),
            },
            domain="workforce-timesheet",
        )
        with write_transaction(self.conn):
            self.conn.execute(
                "UPDATE workforce_timesheets SET status = 'APPROVED', approved_by = ?,"
                " approved_at = ?, lock_hash = ?, version = version + 1"
                " WHERE agency_id = ? AND timesheet_id = ?",
                (approver, stamp, lock_hash, self.agency_id, str(row["timesheet_id"])),
            )
        return self.get(str(row["timesheet_id"]))

    def reject(self, timesheet_id: str, *, reason: str, rejected_by: str) -> dict[str, Any]:
        """Kembalikan periode ke penyusun; alasan wajib."""
        text = _require_reason(reason)
        # Identitas penolak divalidasi; kolomnya belum ada di skema sehingga tidak disimpan.
        require_text(rejected_by, "rejected_by", max_len=64)
        row = self.get(timesheet_id)
        if str(row["status"]) != "SUBMITTED":
            raise InvalidTransition(
                "only a submitted timesheet can be rejected",
                timesheet_id=str(row["timesheet_id"]),
                status=str(row["status"]),
            )
        with write_transaction(self.conn):
            self.conn.execute(
                "UPDATE workforce_timesheets SET status = 'REJECTED', rejected_reason = ?,"
                " version = version + 1 WHERE agency_id = ? AND timesheet_id = ?",
                (text, self.agency_id, str(row["timesheet_id"])),
            )
        return self.get(str(row["timesheet_id"]))

    # -- internal ---------------------------------------------------------- #
    def _now_iso(self) -> str:
        moment = self.clock.now()
        if not isinstance(moment, datetime):
            moment = parse_iso(moment)
        if moment.tzinfo is None:
            raise TimesheetError("clock returned a naive datetime", code="NAIVE_TIMESTAMP")
        return to_iso(moment.astimezone(UTC))

    def _require_staff(self, staff_id: str) -> None:
        row = self.conn.execute(
            "SELECT status FROM workforce_staff WHERE agency_id = ? AND staff_id = ?",
            (self.agency_id, staff_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("staff not found", code="ENTITY_NOT_FOUND", staff_id=staff_id)

    def _require_same_timezone(self, staff_id: str, start: date, end: date) -> None:
        rows = self.conn.execute(
            "SELECT DISTINCT p.timezone FROM workforce_roster_entries r"
            " JOIN properties p ON p.property_id = r.property_id"
            " WHERE r.agency_id = ? AND r.staff_id = ? AND r.work_date BETWEEN ? AND ?",
            (self.agency_id, staff_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        foreign = sorted(str(row["timezone"]) for row in rows if str(row["timezone"]) != self.tz_name)
        if foreign:
            raise PeriodTimezoneMismatch(
                "this staff member is rostered in another timezone during this period",
                staff_id=staff_id,
                tz_name=self.tz_name,
                property_timezones=foreign,
            )

    def _scheduled_minutes(self, staff_id: str, start: date, end: date) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT work_date, starts_at_utc, ends_at_utc FROM workforce_roster_entries"
            " WHERE agency_id = ? AND staff_id = ? AND work_date BETWEEN ? AND ?"
            " AND status IN ('PUBLISHED','CONFIRMED','SWAPPED')",
            (self.agency_id, staff_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        planned: dict[str, int] = {}
        for row in rows:
            begins = parse_iso(str(row["starts_at_utc"])).astimezone(UTC)
            ends = parse_iso(str(row["ends_at_utc"])).astimezone(UTC)
            minutes = max(0, int((ends - begins).total_seconds() // 60))
            planned[str(row["work_date"])] = planned.get(str(row["work_date"]), 0) + minutes
        return planned

    def _effective_events(self, staff_id: str, start: date, end: date) -> list[tuple[datetime, str]]:
        """Baris yang belum dikoreksi, dengan peran aslinya, urut waktu."""
        rows = self.conn.execute(
            "SELECT * FROM workforce_attendance_events WHERE agency_id = ? AND staff_id = ?"
            " ORDER BY occurred_at_utc, recorded_at_utc",
            (self.agency_id, staff_id),
        ).fetchall()
        by_id = {str(row["attendance_event_id"]): dict(row) for row in rows}
        superseded = {
            str(row["supersedes_event_id"]) for row in rows if row["supersedes_event_id"]
        }

        def role_of(row: dict[str, Any]) -> Optional[str]:
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

        effective: list[tuple[datetime, str]] = []
        for event_id, row in by_id.items():
            if event_id in superseded:
                continue
            role = role_of(row)
            if role is None:
                continue
            moment = parse_iso(str(row["occurred_at_utc"])).astimezone(UTC)
            if not (start <= local_date(moment, self.tz_name) <= end):
                # Zona waktu bisa membuat punch di tepi periode jatuh di luar rentang.
                continue
            effective.append((moment, role))
        effective.sort(key=lambda pair: pair[0])
        return effective

