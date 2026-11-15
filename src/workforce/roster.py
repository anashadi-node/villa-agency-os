"""Roster shift — penulis jadwal dengan penolakan bentrok (T1.2).

Aturan yang ditegakkan di sini, dan alasannya:

1. **Satu orang tidak bisa berada di dua tempat pada satu detik.** Untuk staf
   yang sama, rentang ``[starts_at_utc, ends_at_utc)`` tidak boleh beririsan
   dengan shift mana pun yang masih hidup (semua status kecuali ``CANCELLED``),
   **lintas properti sekalipun**. Shift bersambung (selesai 15:00, mulai 15:00)
   sah karena rentangnya setengah terbuka.
2. **Kiriman ulang bukan kesalahan, kiriman berbeda adalah kesalahan.** Kalau
   ``(staff_id, starts_at_utc)`` sudah ada dengan isi identik, penulis
   mengembalikan baris lama (``created=False``). Kalau jam mulai sama tetapi isi
   berbeda, itu dua jadwal berbeda yang bertabrakan — ditolak
   ``WORKFORCE_ROSTER_DUPLICATE_START``.
3. **Tanggal kerja adalah tanggal properti, bukan tanggal server.** ``work_date``
   wajib sama dengan tanggal lokal ``starts_at_utc`` di zona waktu properti
   (Bali WITA, Yogyakarta WIB). Tanpa aturan ini, shift pukul 07:00 WITA bisa
   tercatat pada hari sebelumnya hanya karena server menghitung UTC.
4. **Staf harus aktif dan template harus milik properti itu.** Staf bersatus
   ``SUSPENDED``/``OFFBOARDED`` ditolak; template dengan properti lain ditolak.
5. **``PUBLISHED`` butuh penanda manusia.** Status selain ``DRAFT`` wajib
   menyertakan ``published_by``; ``SWAPPED``/``CANCELLED``/``NO_SHOW`` juga wajib
   menyertakan alasan minimal 4 karakter, karena ketiganya mengubah hak kerja
   atau upah seseorang dan harus bisa dipertanggungjawabkan.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from primitives import (
    UTC,
    DomainError,
    EntityNotFound,
    IdFactory,
    SystemClock,
    local_date,
    parse_iso,
    require_text,
    to_iso,
)
from workforce.repository import write_transaction

ROSTER_ENTRY_ID_PREFIX = "roster"

#: Status yang masih memakai waktu seseorang. ``CANCELLED`` sengaja tidak
#: memblokir, karena shift batal justru membebaskan slot itu untuk dijadwalkan ulang.
BLOCKING_STATUSES = ("DRAFT", "PUBLISHED", "CONFIRMED", "SWAPPED", "NO_SHOW")

#: Status yang wajib menyebut alasan.
REASON_REQUIRED_STATUSES = ("SWAPPED", "CANCELLED", "NO_SHOW")

ROSTER_STATUSES = BLOCKING_STATUSES + ("CANCELLED",)

ACTIVE_STAFF_STATUS = "ACTIVE"
MIN_REASON_LENGTH = 4


class RosterError(DomainError):
    """Kegagalan penulisan jadwal."""

    code = "WORKFORCE_ROSTER_ERROR"


class RosterOverlap(RosterError):
    code = "WORKFORCE_ROSTER_OVERLAP"


class RosterDuplicateStart(RosterError):
    code = "WORKFORCE_ROSTER_DUPLICATE_START"


class StaffNotBookable(RosterError):
    code = "WORKFORCE_STAFF_NOT_BOOKABLE"


@dataclass(frozen=True)
class ShiftSpec:
    """Satu shift yang diminta. Semua waktu harus tz-aware."""

    staff_id: str
    shift_template_id: str
    property_id: str
    starts_at_utc: Any
    ends_at_utc: Any
    work_date: Optional[str] = None
    status: str = "DRAFT"
    reason: Optional[str] = None


def _aware(value: Any, field_name: str) -> datetime:
    moment = parse_iso(value) if not isinstance(value, datetime) else value
    if moment.tzinfo is None:
        raise RosterError(f"{field_name} must be timezone-aware", field=field_name, code="WORKFORCE_NAIVE_DATETIME")
    return moment.astimezone(UTC)


def _require_status(value: Any) -> str:
    status = require_text(value, "status", max_len=20)
    if status not in ROSTER_STATUSES:
        raise RosterError(
            "status must be one of: " + ", ".join(ROSTER_STATUSES),
            code="WORKFORCE_INVALID_STATUS",
            status=status,
        )
    return status


class RosterWriter:
    """Penulis ``workforce_roster_entries`` untuk satu agency."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        agency_id: str = "agency_default",
        clock: Any = None,
        ids: Optional[IdFactory] = None,
    ) -> None:
        self.conn = conn
        self.agency_id = require_text(agency_id, "agency_id", max_len=64)
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()

    # -- pembacaan -------------------------------------------------------- #
    def shifts_for_staff(self, staff_id: str, *, statuses: Sequence[str] = BLOCKING_STATUSES) -> list[dict[str, Any]]:
        """Semua shift staf ini yang masih memakai waktunya, urut jam mulai."""
        rows = self.conn.execute(
            "SELECT * FROM workforce_roster_entries"
            f" WHERE agency_id = ? AND staff_id = ? AND status IN ({', '.join('?' * len(statuses))})"
            " ORDER BY starts_at_utc",
            (self.agency_id, staff_id, *statuses),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- penulisan -------------------------------------------------------- #
    def add_shift(self, spec: ShiftSpec) -> tuple[dict[str, Any], bool]:
        """Tulis satu shift. ``(row, created)`` — ``created=False`` berarti kiriman ulang."""
        rows = self.add_shifts([spec])
        return rows[0]

    def add_shifts(self, specs: Iterable[ShiftSpec]) -> list[tuple[dict[str, Any], bool]]:
        """Tulis sekumpulan shift dalam satu transaksi.

        Semua shift divalidasi lebih dulu; satu shift yang ditolak membatalkan
        seluruh batch. Tidak ada jadwal separuh jadi yang ditinggalkan.
        """
        prepared = [self._prepare(spec) for spec in specs]
        if not prepared:
            return []
        self._reject_internal_overlap(prepared)
        now = to_iso(self.clock.now().astimezone(UTC))

        results: list[tuple[dict[str, Any], bool]] = []
        with write_transaction(self.conn):
            for row in prepared:
                existing = self._find_by_start(row["staff_id"], row["starts_at_utc"])
                if existing is not None:
                    if self._same_shift(existing, row):
                        results.append((existing, False))
                        continue
                    raise RosterDuplicateStart(
                        "a shift for this staff already starts at that instant",
                        staff_id=row["staff_id"],
                        starts_at_utc=row["starts_at_utc"],
                        roster_entry_id=str(existing["roster_entry_id"]),
                    )
                self._reject_conflict(row)
                record = dict(row)
                record["roster_entry_id"] = self.ids.new(ROSTER_ENTRY_ID_PREFIX)
                record["created_at"] = now
                record["updated_at"] = now
                columns, values = self._columns(record)
                self.conn.execute(
                    f"INSERT INTO workforce_roster_entries ({columns}) VALUES ({values})",
                    tuple(record.values()),
                )
                results.append((dict(record), True))
        return results

    def cancel_shift(self, roster_entry_id: str, *, reason: str) -> dict[str, Any]:
        """Batalkan shift dengan alasan. Slot waktunya langsung bebas lagi."""
        text = require_text(reason, "reason", max_len=200)
        if len(text) < MIN_REASON_LENGTH:
            raise RosterError("reason must explain the cancellation", code="WORKFORCE_REASON_TOO_SHORT")
        with write_transaction(self.conn):
            row = self.conn.execute(
                "SELECT * FROM workforce_roster_entries WHERE agency_id = ? AND roster_entry_id = ?",
                (self.agency_id, roster_entry_id),
            ).fetchone()
            if row is None:
                raise EntityNotFound(
                    "roster entry not found",
                    code="ENTITY_NOT_FOUND",
                    roster_entry_id=roster_entry_id,
                )
            if row["status"] == "CANCELLED":
                return dict(row)
            self.conn.execute(
                "UPDATE workforce_roster_entries SET status = 'CANCELLED', reason = ?, updated_at = ?"
                " WHERE agency_id = ? AND roster_entry_id = ?",
                (text, to_iso(self.clock.now().astimezone(UTC)), self.agency_id, roster_entry_id),
            )
            updated = self.conn.execute(
                "SELECT * FROM workforce_roster_entries WHERE agency_id = ? AND roster_entry_id = ?",
                (self.agency_id, roster_entry_id),
            ).fetchone()
        return dict(updated)

    def publish_shift(self, roster_entry_id: str, *, published_by: str) -> dict[str, Any]:
        """Publikasikan shift DRAFT. Replay aman: shift yang sudah PUBLISHED dikembalikan apa adanya."""
        actor = require_text(published_by, "published_by", max_len=64)
        with write_transaction(self.conn):
            row = self.conn.execute(
                "SELECT * FROM workforce_roster_entries WHERE agency_id = ? AND roster_entry_id = ?",
                (self.agency_id, roster_entry_id),
            ).fetchone()
            if row is None:
                raise EntityNotFound(
                    "roster entry not found",
                    code="ENTITY_NOT_FOUND",
                    roster_entry_id=roster_entry_id,
                )
            if row["status"] == "CANCELLED":
                raise RosterError(
                    "a cancelled shift cannot be published",
                    code="WORKFORCE_CANCELLED_NOT_PUBLISHABLE",
                    roster_entry_id=roster_entry_id,
                )
            if row["status"] == "PUBLISHED":
                return dict(row)
            now = to_iso(self.clock.now().astimezone(UTC))
            self.conn.execute(
                "UPDATE workforce_roster_entries SET status = 'PUBLISHED', published_by = ?, published_at = ?,"
                " updated_at = ? WHERE agency_id = ? AND roster_entry_id = ?",
                (actor, now, now, self.agency_id, roster_entry_id),
            )
            updated = self.conn.execute(
                "SELECT * FROM workforce_roster_entries WHERE agency_id = ? AND roster_entry_id = ?",
                (self.agency_id, roster_entry_id),
            ).fetchone()
        return dict(updated)

    # -- validasi --------------------------------------------------------- #
    def _prepare(self, spec: ShiftSpec) -> dict[str, Any]:
        if not isinstance(spec, ShiftSpec):
            raise RosterError("spec must be a ShiftSpec", code="WORKFORCE_INVALID_SPEC")
        staff_id = require_text(spec.staff_id, "staff_id", max_len=64)
        template_id = require_text(spec.shift_template_id, "shift_template_id", max_len=64)
        property_id = require_text(spec.property_id, "property_id", max_len=64)
        starts = _aware(spec.starts_at_utc, "starts_at_utc")
        ends = _aware(spec.ends_at_utc, "ends_at_utc")
        if ends <= starts:
            raise RosterError(
                "ends_at_utc must be after starts_at_utc",
                code="WORKFORCE_INVALID_WINDOW",
                starts_at_utc=to_iso(starts),
                ends_at_utc=to_iso(ends),
            )
        status = _require_status(spec.status)

        self._require_staff(staff_id)
        self._require_template(template_id, property_id)
        local_day = local_date(starts, self._timezone(property_id)).isoformat()
        if spec.work_date is not None and require_text(spec.work_date, "work_date", max_len=10) != local_day:
            raise RosterError(
                "work_date must match the property-local date of starts_at_utc",
                code="WORKFORCE_WORK_DATE_MISMATCH",
                work_date=spec.work_date,
                expected=local_day,
            )

        reason = None
        if status in REASON_REQUIRED_STATUSES:
            reason = require_text(spec.reason, "reason", max_len=200)
            if len(reason) < MIN_REASON_LENGTH:
                raise RosterError("reason must explain the status change", code="WORKFORCE_REASON_TOO_SHORT")
        elif spec.reason is not None:
            reason = require_text(spec.reason, "reason", max_len=200)

        published_by = None
        published_at = None
        if status != "DRAFT":
            # Publikasi adalah tindakan terpisah dengan penanda manusia, bukan
            # efek samping pembuatan baris; lihat ``publish_shift``.
            raise RosterError(
                "add_shifts writes DRAFT only; call publish_shift to publish",
                code="WORKFORCE_STATUS_NOT_DRAFT",
                status=status,
            )

        return {
            "agency_id": self.agency_id,
            "staff_id": staff_id,
            "shift_template_id": template_id,
            "property_id": property_id,
            "work_date": local_day,
            "starts_at_utc": to_iso(starts),
            "ends_at_utc": to_iso(ends),
            "status": status,
            "published_by": published_by,
            "published_at": published_at,
            "reason": reason,
        }

    def _require_staff(self, staff_id: str) -> None:
        row = self.conn.execute(
            "SELECT status FROM workforce_staff WHERE agency_id = ? AND staff_id = ?",
            (self.agency_id, staff_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("staff not found", code="ENTITY_NOT_FOUND", staff_id=staff_id)
        if row["status"] != ACTIVE_STAFF_STATUS:
            raise StaffNotBookable(
                "only ACTIVE staff can be rostered",
                staff_id=staff_id,
                status=str(row["status"]),
            )

    def _require_template(self, template_id: str, property_id: str) -> None:
        row = self.conn.execute(
            "SELECT property_id FROM workforce_shift_templates WHERE agency_id = ? AND shift_template_id = ?",
            (self.agency_id, template_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("shift template not found", code="ENTITY_NOT_FOUND", shift_template_id=template_id)
        if row["property_id"] not in (None, property_id):
            raise RosterError(
                "shift template belongs to another property",
                code="WORKFORCE_TEMPLATE_PROPERTY_MISMATCH",
                shift_template_id=template_id,
                template_property_id=str(row["property_id"]),
                property_id=property_id,
            )

    def _timezone(self, property_id: str) -> str:
        row = self.conn.execute(
            "SELECT timezone FROM properties WHERE agency_id = ? AND property_id = ?",
            (self.agency_id, property_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("property not found", code="ENTITY_NOT_FOUND", property_id=property_id)
        return str(row["timezone"])

    @staticmethod
    def _reject_internal_overlap(prepared: Sequence[Mapping[str, Any]]) -> None:
        """Tangkap bentrok di dalam batch sebelum satu baris pun ditulis."""
        ordered = sorted(prepared, key=lambda row: (row["staff_id"], row["starts_at_utc"]))
        for left, right in zip(ordered, ordered[1:]):
            if left["staff_id"] != right["staff_id"]:
                continue
            if right["starts_at_utc"] < left["ends_at_utc"]:
                raise RosterOverlap(
                    "two shifts in this batch overlap for the same staff",
                    staff_id=left["staff_id"],
                    first_start=left["starts_at_utc"],
                    second_start=right["starts_at_utc"],
                )

    def _reject_conflict(self, row: Mapping[str, Any]) -> None:
        hit = self.conn.execute(
            "SELECT roster_entry_id, starts_at_utc, ends_at_utc, property_id FROM workforce_roster_entries"
            " WHERE agency_id = ? AND staff_id = ? AND status <> 'CANCELLED'"
            " AND starts_at_utc < ? AND ends_at_utc > ?"
            " LIMIT 1",
            (self.agency_id, row["staff_id"], row["ends_at_utc"], row["starts_at_utc"]),
        ).fetchone()
        if hit is not None:
            raise RosterOverlap(
                "this staff already has a shift in that window",
                staff_id=row["staff_id"],
                roster_entry_id=str(hit["roster_entry_id"]),
                existing_start=str(hit["starts_at_utc"]),
                existing_end=str(hit["ends_at_utc"]),
                property_id=str(hit["property_id"]),
            )

    def _find_by_start(self, staff_id: str, starts_at_utc: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM workforce_roster_entries"
            " WHERE agency_id = ? AND staff_id = ? AND starts_at_utc = ?",
            (self.agency_id, staff_id, starts_at_utc),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _same_shift(existing: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
        fields = ("shift_template_id", "property_id", "ends_at_utc", "work_date", "status", "reason")
        return all(existing[field] == row[field] for field in fields)

    @staticmethod
    def _columns(record: Mapping[str, Any]) -> tuple[str, str]:
        columns = ", ".join(record)
        values = ", ".join("?" * len(record))
        return columns, values
