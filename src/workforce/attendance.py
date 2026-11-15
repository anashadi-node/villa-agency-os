"""Attendance events — bukti dulu, turunan kemudian (T1.3).

Aturan yang ditegakkan di sini, dan alasannya:

1. **Kehadiran adalah bukti, bukan pendapat.** Tabel ``workforce_attendance_events``
   hanya bisa ditambah; trigger di skema menolak ``UPDATE`` dan ``DELETE``. Karena
   itu koreksi tidak boleh menimpa baris lama — ia menulis baris ``MANUAL_ADJUST``
   yang **menunjuk** baris yang dikoreksinya lewat ``supersedes_event_id``.
2. **Koreksi mewarisi orangnya, tidak memilihnya.** Staf pada baris koreksi
   diambil dari baris yang dikoreksi, sehingga satu supervisi tidak bisa tanpa
   sengaja memindahkan jam kerja orang lain kepada orang ini.
3. **Satu baris dikoreksi paling banyak satu kali.** Rantai koreksi harus lurus:
   mengoreksi baris yang sudah punya pengganti ditolak, kalau tidak dua pembaca
   akan melihat dua "kebenaran" yang berbeda untuk detik yang sama.
4. **Koreksi manual wajib menyebut alasan.** ``evidence_ref`` minimal 4 karakter;
   tanpa itu, ``MANUAL_ADJUST`` ditolak skema maupun penulis ini.
5. **Perangkat tidak boleh mengaku masa depan.** ``occurred_at_utc`` tidak boleh
   melampaui jam server saat dicatat, karena jam perangkat yang meleset akan
   membuat laporan kehadiran berbohong tentang hari yang belum terjadi.
6. **Kiriman ulang bukan kesalahan.** ``(agency_id, idempotency_key)`` unik:
   kiriman identik mengembalikan baris lama dengan ``created=False``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from primitives import (
    UTC,
    DomainError,
    EntityNotFound,
    IdFactory,
    SystemClock,
    parse_iso,
    require_text,
    to_iso,
)
from workforce.repository import insert_once, write_transaction

ATTENDANCE_EVENT_ID_PREFIX = "attendance"

#: Tipe yang boleh ditulis langsung oleh perangkat atau impor.
RECORDED_EVENT_TYPES = (
    "CLOCK_IN",
    "CLOCK_OUT",
    "BREAK_START",
    "BREAK_END",
    "DEVICE_PING",
    "GEOFENCE_REJECT",
)

#: ``MANUAL_ADJUST`` hanya lahir dari :meth:`AttendanceWriter.correct`.
EVENT_TYPES = RECORDED_EVENT_TYPES + ("MANUAL_ADJUST",)

SOURCES = ("DEVICE", "MANUAL", "IMPORT", "API")
GEOFENCE_VERDICTS = ("INSIDE", "OUTSIDE", "UNKNOWN")

ACTIVE_STAFF_STATUS = "ACTIVE"
MIN_EVIDENCE_LENGTH = 4
MAX_ACCURACY_BUCKET_M = 10_000


class AttendanceError(DomainError):
    """Kegagalan penulisan bukti kehadiran."""

    code = "WORKFORCE_ATTENDANCE_ERROR"


class AttendanceEventNotFound(AttendanceError):
    code = "WORKFORCE_ATTENDANCE_EVENT_NOT_FOUND"


class AlreadySuperseded(AttendanceError):
    code = "WORKFORCE_ATTENDANCE_ALREADY_SUPERSEDED"


class ManualTargetRequired(AttendanceError):
    code = "WORKFORCE_ATTENDANCE_MANUAL_TARGET_REQUIRED"


class EventInTheFuture(AttendanceError):
    code = "WORKFORCE_ATTENDANCE_FUTURE_EVENT"


class StaffNotClockable(AttendanceError):
    code = "WORKFORCE_STAFF_NOT_CLOCKABLE"


class RosterMismatch(AttendanceError):
    code = "WORKFORCE_ATTENDANCE_ROSTER_MISMATCH"


class IdempotencyKeyReused(AttendanceError):
    """Kunci kiriman dipakai ulang untuk isi yang berbeda — bukan replay."""

    code = "WORKFORCE_ATTENDANCE_IDEMPOTENCY_MISMATCH"


#: Kolom yang menentukan arti sebuah bukti. Kunci kiriman hanya boleh dipakai ulang
#: untuk baris yang identik pada semua kolom ini; kalau tidak, itu penulisan baru yang
#: salah kunci, bukan kiriman ulang.
REPLAY_COMPARABLE_FIELDS = (
    "staff_id",
    "roster_entry_id",
    "supersedes_event_id",
    "event_type",
    "occurred_at_utc",
    "source",
    "device_ref",
    "geofence_verdict",
    "accuracy_bucket_m",
    "evidence_ref",
    "actor_id",
)


def _aware(value: Any, field_name: str) -> datetime:
    moment = parse_iso(value) if not isinstance(value, datetime) else value
    if moment.tzinfo is None:
        raise AttendanceError(
            f"{field_name} must be timezone-aware",
            field=field_name,
            code="WORKFORCE_NAIVE_DATETIME",
        )
    return moment.astimezone(UTC)


def _require_choice(value: Any, field_name: str, allowed: tuple[str, ...]) -> str:
    text = require_text(value, field_name, max_len=32)
    if text not in allowed:
        raise AttendanceError(
            f"{field_name} must be one of: " + ", ".join(allowed),
            code="WORKFORCE_INVALID_VALUE",
            **{field_name: text},
        )
    return text


def _require_evidence(value: Any, field_name: str = "evidence_ref") -> str:
    text = require_text(value, field_name, max_len=200)
    if len(text) < MIN_EVIDENCE_LENGTH:
        raise AttendanceError(
            f"{field_name} must explain the correction",
            code="WORKFORCE_REASON_TOO_SHORT",
            **{field_name: text},
        )
    return text


def _accuracy_bucket(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AttendanceError(
            "accuracy_bucket_m must be an integer number of metres",
            code="WORKFORCE_INVALID_VALUE",
            accuracy_bucket_m=value,
        )
    if value < 0 or value > MAX_ACCURACY_BUCKET_M:
        raise AttendanceError(
            f"accuracy_bucket_m must be between 0 and {MAX_ACCURACY_BUCKET_M}",
            code="WORKFORCE_INVALID_VALUE",
            accuracy_bucket_m=value,
        )
    return value


@dataclass(frozen=True)
class AttendanceRecord:
    """Satu kiriman perangkat atau impor. ``MANUAL_ADJUST`` tidak lewat sini."""

    staff_id: str
    event_type: str
    occurred_at_utc: Any
    idempotency_key: str
    actor_id: str
    source: str = "DEVICE"
    roster_entry_id: Optional[str] = None
    device_ref: Optional[str] = None
    geofence_verdict: str = "UNKNOWN"
    accuracy_bucket_m: Optional[int] = None
    evidence_ref: Optional[str] = None


class AttendanceWriter:
    """Penulis ``workforce_attendance_events`` untuk satu agency."""

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
    def events_for_staff(self, staff_id: str) -> list[dict[str, Any]]:
        """Semua bukti kehadiran staf ini, urut waktu kejadian."""
        rows = self.conn.execute(
            "SELECT * FROM workforce_attendance_events"
            " WHERE agency_id = ? AND staff_id = ? ORDER BY occurred_at_utc, recorded_at_utc",
            (self.agency_id, staff_id),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- penulisan -------------------------------------------------------- #
    def record(self, spec: AttendanceRecord) -> tuple[dict[str, Any], bool]:
        """Catat satu kejadian. ``(row, created)`` — ``created=False`` berarti kiriman ulang."""
        event_type = _require_choice(spec.event_type, "event_type", EVENT_TYPES)
        if event_type == "MANUAL_ADJUST":
            raise ManualTargetRequired(
                "MANUAL_ADJUST must name the event it corrects; call correct() instead",
                event_type=event_type,
            )
        source = _require_choice(spec.source, "source", SOURCES)
        staff_id = require_text(spec.staff_id, "staff_id", max_len=64)
        actor_id = require_text(spec.actor_id, "actor_id", max_len=64)
        key = require_text(spec.idempotency_key, "idempotency_key", max_len=128)
        verdict = _require_choice(spec.geofence_verdict, "geofence_verdict", GEOFENCE_VERDICTS)
        accuracy = _accuracy_bucket(spec.accuracy_bucket_m)
        occurred = _aware(spec.occurred_at_utc, "occurred_at_utc")

        device_ref = None
        if spec.device_ref is not None:
            device_ref = require_text(spec.device_ref, "device_ref", max_len=128)

        evidence_ref = None
        if spec.evidence_ref is not None:
            evidence_ref = require_text(spec.evidence_ref, "evidence_ref", max_len=200)
        if source == "MANUAL":
            # Skema menuntut bukti untuk sumber MANUAL; tanpa ini pesan errornya
            # akan datang sebagai OperationalError, bukan kesalahan domain.
            if evidence_ref is None:
                raise AttendanceError(
                    "MANUAL source must carry evidence_ref",
                    code="WORKFORCE_REASON_TOO_SHORT",
                    source=source,
                )
            evidence_ref = _require_evidence(evidence_ref)

        self._require_clockable(staff_id)
        self._require_roster(spec.roster_entry_id, staff_id)

        return self._write(
            {
                "staff_id": staff_id,
                "roster_entry_id": spec.roster_entry_id,
                "supersedes_event_id": None,
                "event_type": event_type,
                "occurred_at_utc": occurred,
                "source": source,
                "device_ref": device_ref,
                "geofence_verdict": verdict,
                "accuracy_bucket_m": accuracy,
                "evidence_ref": evidence_ref,
                "actor_id": actor_id,
                "idempotency_key": key,
            }
        )

    def correct(
        self,
        target_event_id: str,
        *,
        occurred_at_utc: Any,
        reason: str,
        actor_id: str,
        idempotency_key: str,
        roster_entry_id: Optional[str] = None,
    ) -> tuple[dict[str, Any], bool]:
        """Tulis ``MANUAL_ADJUST`` yang menunjuk baris lama; baris lama tidak disentuh."""
        target_id = require_text(target_event_id, "target_event_id", max_len=64)
        actor = require_text(actor_id, "actor_id", max_len=64)
        key = require_text(idempotency_key, "idempotency_key", max_len=128)
        evidence = _require_evidence(reason, "reason")
        occurred = _aware(occurred_at_utc, "occurred_at_utc")

        with write_transaction(self.conn):
            # Kiriman ulang diperiksa paling awal: koreksi yang sama persis sudah
            # membuat baris target "punya pengganti", jadi cek supersede dulu akan
            # menolak kiriman ulang yang sebenarnya tidak berbahaya.
            replay = self.conn.execute(
                "SELECT * FROM workforce_attendance_events"
                " WHERE agency_id = ? AND idempotency_key = ?",
                (self.agency_id, key),
            ).fetchone()
            if replay is not None:
                return dict(replay), False

            target = self.conn.execute(
                "SELECT * FROM workforce_attendance_events"
                " WHERE agency_id = ? AND attendance_event_id = ?",
                (self.agency_id, target_id),
            ).fetchone()
            if target is None:
                raise AttendanceEventNotFound(
                    "attendance event not found",
                    attendance_event_id=target_id,
                )
            replacement = self.conn.execute(
                "SELECT attendance_event_id FROM workforce_attendance_events"
                " WHERE agency_id = ? AND supersedes_event_id = ?",
                (self.agency_id, target_id),
            ).fetchone()
            if replacement is not None:
                raise AlreadySuperseded(
                    "this event already has a correction; correct that one instead",
                    attendance_event_id=target_id,
                    supersedes_event_id=str(replacement["attendance_event_id"]),
                )

            staff_id = str(target["staff_id"])
            inherited_roster = target["roster_entry_id"]
            if roster_entry_id is not None and roster_entry_id != inherited_roster:
                raise RosterMismatch(
                    "a correction cannot move an event onto another shift",
                    roster_entry_id=roster_entry_id,
                    target_roster_entry_id=inherited_roster,
                )

            return self._write(
                {
                    "staff_id": staff_id,
                    "roster_entry_id": inherited_roster,
                    "supersedes_event_id": target_id,
                    "event_type": "MANUAL_ADJUST",
                    "occurred_at_utc": occurred,
                    "source": "MANUAL",
                    "device_ref": target["device_ref"],
                    "geofence_verdict": str(target["geofence_verdict"]),
                    "accuracy_bucket_m": target["accuracy_bucket_m"],
                    "evidence_ref": evidence,
                    "actor_id": actor,
                    "idempotency_key": key,
                },
                # Sudah di dalam transaksi koreksi: cek "belum pernah dikoreksi"
                # dan INSERT harus terjadi di transaksi yang sama.
                transactional=False,
            )

    # -- internal --------------------------------------------------------- #
    def _write(self, fields: dict[str, Any], *, transactional: bool = True
               ) -> tuple[dict[str, Any], bool]:
        recorded = _aware(self.clock.now(), "recorded_at_utc")
        occurred = fields.pop("occurred_at_utc")
        if occurred > recorded:
            raise EventInTheFuture(
                "occurred_at_utc cannot be later than the moment it was recorded",
                occurred_at_utc=to_iso(occurred),
                recorded_at_utc=to_iso(recorded),
            )
        stamp = to_iso(recorded)
        record: dict[str, Any] = {
            "agency_id": self.agency_id,
            "attendance_event_id": self.ids.new(ATTENDANCE_EVENT_ID_PREFIX),
            **fields,
            "occurred_at_utc": to_iso(occurred),
            "recorded_at_utc": stamp,
            "created_at": stamp,
        }
        if transactional:
            with write_transaction(self.conn):
                return self._insert(record)
        return self._insert(record)

    def _insert(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Satu ``INSERT ... ON CONFLICT DO NOTHING``; pemanggil yang urus transaksinya."""
        created = insert_once(
            self.conn,
            table="workforce_attendance_events",
            columns=record,
            conflict_on=("agency_id", "idempotency_key"),
        )
        if created:
            return dict(record), True
        existing = self.conn.execute(
            "SELECT * FROM workforce_attendance_events"
            " WHERE agency_id = ? AND idempotency_key = ?",
            (self.agency_id, record["idempotency_key"]),
        ).fetchone()
        mutated = [
            name
            for name in REPLAY_COMPARABLE_FIELDS
            if str(existing[name]) != str(record[name])
        ]
        if mutated:
            # Kunci yang sama untuk isi berbeda tidak boleh lolos sebagai "replay":
            # perangkat yang mengirim ulang dengan jam berbeda sedang melaporkan
            # kejadian lain, dan menelannya diam-diam akan menghilangkan bukti.
            raise IdempotencyKeyReused(
                "this idempotency_key was already used for a different event",
                idempotency_key=str(record["idempotency_key"]),
                differing_fields=",".join(mutated),
                existing_event_id=str(existing["attendance_event_id"]),
            )
        return dict(existing), False

    def _require_clockable(self, staff_id: str) -> None:
        row = self.conn.execute(
            "SELECT status FROM workforce_staff WHERE agency_id = ? AND staff_id = ?",
            (self.agency_id, staff_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("staff not found", code="ENTITY_NOT_FOUND", staff_id=staff_id)
        if row["status"] != ACTIVE_STAFF_STATUS:
            raise StaffNotClockable(
                "only ACTIVE staff can produce attendance evidence",
                staff_id=staff_id,
                status=str(row["status"]),
            )

    def _require_roster(self, roster_entry_id: Optional[str], staff_id: str) -> None:
        if roster_entry_id is None:
            return
        row = self.conn.execute(
            "SELECT staff_id FROM workforce_roster_entries"
            " WHERE agency_id = ? AND roster_entry_id = ?",
            (self.agency_id, roster_entry_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound(
                "roster entry not found",
                code="ENTITY_NOT_FOUND",
                roster_entry_id=roster_entry_id,
            )
        if str(row["staff_id"]) != staff_id:
            raise RosterMismatch(
                "this roster entry belongs to another staff member",
                roster_entry_id=roster_entry_id,
                staff_id=staff_id,
            )
