"""Tugas operasional yang ditugaskan kepada orang, bukan kepada teks bebas (T1.5).

``ops_tasks.assignee`` dulu hanya teks: siapa pun bisa menulis nama apa saja, dan
tidak ada satu pun tempat yang bisa menjawab "berapa jam kerja orang ini sudah
dijanjikan minggu ini". Modul ini menggantinya dengan penugasan yang punya
identitas staf, peran, durasi, dan batas kapasitas. Aturan yang ditegakkan di
sini, dan alasannya:

1. **Hanya staf ``ACTIVE`` yang bisa ditugaskan, dan tugas harus masih hidup.**
   Staf ``SUSPENDED``/``OFFBOARDED`` ditolak ``WORKFORCE_STAFF_NOT_ASSIGNABLE``;
   tugas berstatus ``DONE``/``CANCELLED`` ditolak ``WORKFORCE_TASK_CLOSED``.
   Menugaskan pekerjaan kepada orang yang tidak boleh bekerja adalah cara paling
   halus untuk membuat pekerjaan itu tidak pernah selesai.
2. **Kapasitas diukur, bukan diasumsikan.** ``planned_minutes`` wajib 1..480 per
   penugasan. Total menit yang masih dijanjikan kepada seorang staf pada minggu
   yang memuat tenggat (Senin 00:00 UTC sampai Senin berikutnya) tidak boleh
   melebihi ``workforce_staff.weekly_contract_minutes``. Penugasan ``DROPPED``
   tidak dihitung, karena pekerjaan yang dibatalkan mengembalikan waktunya.
3. **``DROPPED`` wajib menyebut alasan** minimal 3 karakter. Penugasan yang
   hilang tanpa alasan membuat selisih kapasitas tidak bisa dipertanggungjawabkan
   saat rekap upah. Aturan yang sama juga dipasang sebagai ``CHECK`` di basis data,
   jadi jalur tulis mana pun terkena.
4. **Kiriman ulang bukan kesalahan, kiriman berbeda dengan kunci sama adalah
   kesalahan.** Kunci idempotensi (minimal 8 karakter) yang sama dengan isi yang
   sama mengembalikan baris lama (``created=False``); kunci sama dengan isi
   berbeda ditolak ``WORKFORCE_ASSIGNMENT_KEY_REUSED``. Kombinasi
   ``(task_id, staff_id, role_in_task)`` yang sudah ada dengan durasi berbeda
   ditolak ``WORKFORCE_ALREADY_ASSIGNED`` — bukan ditimpa diam-diam.
5. **Status hanya bergerak maju, dan ``DONE`` membawa waktu selesainya.**
   ``ASSIGNED`` → ``ACCEPTED`` → ``IN_PROGRESS`` → ``DONE``; ``DROPPED`` boleh
   dari ketiga status terbuka. Status terminal tidak bisa dibuka kembali, dan
   ``completed_at_utc`` diisi dari jam penulis — bukan dari ingatan pemanggil.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

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
from workforce.repository import (
    MIN_IDEMPOTENCY_KEY_LENGTH,
    InvalidIdempotencyKey,
    insert_once,
    write_transaction,
)

ASSIGNMENT_ID_PREFIX = "assign"

#: Peran yang dikenal skema. ``LEAD`` memegang hasil, ``SUPPORT`` membantu,
#: ``REVIEWER`` memeriksa — ketiganya boleh dirangkap orang yang sama pada tugas
#: berbeda, tetapi tidak pada tugas yang sama.
ROLE_IN_TASK = ("LEAD", "SUPPORT", "REVIEWER")

OPEN_ASSIGNMENT_STATUSES = ("ASSIGNED", "ACCEPTED", "IN_PROGRESS")
TERMINAL_ASSIGNMENT_STATUSES = ("DONE", "DROPPED")
ASSIGNMENT_STATUSES = OPEN_ASSIGNMENT_STATUSES + TERMINAL_ASSIGNMENT_STATUSES

#: Tugas yang sudah ditutup tidak menerima penugasan baru.
CLOSED_TASK_STATUSES = ("DONE", "CANCELLED")

ACTIVE_STAFF_STATUS = "ACTIVE"

MIN_PLANNED_MINUTES = 1
MAX_PLANNED_MINUTES = 480
MIN_REASON_LENGTH = 3

#: Minggu kapasitas dipotong pada Senin 00:00 UTC. Ini batas perencanaan, bukan
#: batas upah (upah memakai zona properti di modul timesheet), jadi zona tunggal
#: dipilih supaya angka yang sama tidak berubah karena server berada di zona lain.
WEEK_START_WEEKDAY = 0


class TaskAssignmentError(DomainError):
    """Kegagalan penugasan tugas operasional."""

    code = "WORKFORCE_ASSIGNMENT_ERROR"


class StaffNotAssignable(TaskAssignmentError):
    code = "WORKFORCE_STAFF_NOT_ASSIGNABLE"


class TaskClosed(TaskAssignmentError):
    code = "WORKFORCE_TASK_CLOSED"


class AlreadyAssigned(TaskAssignmentError):
    code = "WORKFORCE_ALREADY_ASSIGNED"


class AssignmentKeyReused(TaskAssignmentError):
    code = "WORKFORCE_ASSIGNMENT_KEY_REUSED"


class CapacityExceeded(TaskAssignmentError):
    code = "WORKFORCE_CAPACITY_EXCEEDED"


class DropReasonRequired(TaskAssignmentError):
    code = "WORKFORCE_REASON_TOO_SHORT"


class InvalidTransition(TaskAssignmentError):
    code = "WORKFORCE_INVALID_TRANSITION"


@dataclass(frozen=True)
class AssignmentSpec:
    """Satu penugasan yang diminta. ``due_at_utc`` boleh kosong → ikut tugas."""

    task_id: str
    staff_id: str
    planned_minutes: int
    assigned_by: str
    idempotency_key: str
    role_in_task: str = "LEAD"
    due_at_utc: Any = None


def _aware(value: Any, field_name: str) -> datetime:
    moment = parse_iso(value) if not isinstance(value, datetime) else value
    if moment.tzinfo is None:
        raise TaskAssignmentError(
            f"{field_name} must be timezone-aware",
            field=field_name,
            code="WORKFORCE_NAIVE_DATETIME",
        )
    return moment.astimezone(UTC)


def _count_minutes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskAssignmentError(
            "planned_minutes must be a whole number of minutes",
            code="WORKFORCE_INVALID_MINUTES",
            got=type(value).__name__,
        )
    if not MIN_PLANNED_MINUTES <= value <= MAX_PLANNED_MINUTES:
        raise TaskAssignmentError(
            f"planned_minutes must be between {MIN_PLANNED_MINUTES} and {MAX_PLANNED_MINUTES}",
            code="WORKFORCE_INVALID_MINUTES",
            planned_minutes=value,
        )
    return value


def _role(value: Any) -> str:
    role = require_text(value, "role_in_task", max_len=16)
    if role not in ROLE_IN_TASK:
        raise TaskAssignmentError(
            "role_in_task must be one of: " + ", ".join(ROLE_IN_TASK),
            code="WORKFORCE_INVALID_ROLE",
            role_in_task=role,
        )
    return role


def _week_start(moment: datetime) -> datetime:
    day: date = moment.astimezone(UTC).date()
    return datetime.combine(
        day - timedelta(days=(day.weekday() - WEEK_START_WEEKDAY) % 7), datetime.min.time()
    ).replace(tzinfo=UTC)


class TaskAssignmentWriter:
    """Penulis ``workforce_task_assignments`` untuk satu agency."""

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
    def get(self, assignment_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM workforce_task_assignments WHERE agency_id = ? AND assignment_id = ?",
            (self.agency_id, assignment_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound(
                "assignment not found",
                code="ENTITY_NOT_FOUND",
                assignment_id=assignment_id,
            )
        return dict(row)

    def assignments_for_staff(
        self, staff_id: str, *, statuses: Sequence[str] = ASSIGNMENT_STATUSES
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM workforce_task_assignments"
            f" WHERE agency_id = ? AND staff_id = ? AND status IN ({', '.join('?' * len(statuses))})"
            " ORDER BY due_at_utc, assignment_id",
            (self.agency_id, staff_id, *statuses),
        ).fetchall()
        return [dict(row) for row in rows]

    def assignments_for_task(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM workforce_task_assignments WHERE agency_id = ? AND task_id = ?"
            " ORDER BY assigned_at, assignment_id",
            (self.agency_id, task_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def capacity_used(self, staff_id: str, due_at_utc: Any) -> int:
        """Menit yang sudah dijanjikan pada minggu yang memuat ``due_at_utc``."""
        start = _week_start(_aware(due_at_utc, "due_at_utc"))
        end = start + timedelta(days=7)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(planned_minutes), 0) AS used FROM workforce_task_assignments"
            " WHERE agency_id = ? AND staff_id = ? AND status <> 'DROPPED'"
            " AND due_at_utc >= ? AND due_at_utc < ?",
            (self.agency_id, staff_id, to_iso(start), to_iso(end)),
        ).fetchone()
        return int(row["used"])

    def remaining_capacity(self, staff_id: str, due_at_utc: Any) -> int:
        staff = self._staff(staff_id)
        return int(staff["weekly_contract_minutes"]) - self.capacity_used(staff_id, due_at_utc)

    # -- penugasan -------------------------------------------------------- #
    def assign(self, spec: AssignmentSpec) -> tuple[dict[str, Any], bool]:
        """Tugaskan seorang staf. ``(row, created)`` — ``created=False`` = kiriman ulang."""
        minutes = _count_minutes(spec.planned_minutes)
        role = _role(spec.role_in_task)
        actor = require_text(spec.assigned_by, "assigned_by", max_len=64)
        if len(actor) < 3:
            raise TaskAssignmentError(
                "assigned_by must identify a person", code="WORKFORCE_ACTOR_TOO_SHORT"
            )
        key = require_text(spec.idempotency_key, "idempotency_key", max_len=128)
        if len(key) < MIN_IDEMPOTENCY_KEY_LENGTH:
            raise InvalidIdempotencyKey(
                f"idempotency_key must be at least {MIN_IDEMPOTENCY_KEY_LENGTH} characters",
                idempotency_key=key,
            )
        task = self._task(require_text(spec.task_id, "task_id", max_len=64))
        staff = self._staff(require_text(spec.staff_id, "staff_id", max_len=64))
        due = _aware(spec.due_at_utc if spec.due_at_utc is not None else task["due_at"], "due_at_utc")
        due_iso = to_iso(due)

        with write_transaction(self.conn):
            previous = self._by_key(key)
            if previous is not None:
                if self._same_assignment(previous, task, staff, role, minutes, due_iso):
                    return previous, False
                raise AssignmentKeyReused(
                    "idempotency key already used for a different assignment",
                    idempotency_key=key,
                    assignment_id=str(previous["assignment_id"]),
                )
            if task["status"] in CLOSED_TASK_STATUSES:
                raise TaskClosed(
                    "cannot assign work on a closed task",
                    task_id=task["task_id"],
                    status=task["status"],
                )
            if staff["status"] != ACTIVE_STAFF_STATUS:
                raise StaffNotAssignable(
                    "staff is not ACTIVE and may not receive work",
                    staff_id=staff["staff_id"],
                    status=staff["status"],
                )
            same_slot = self._by_slot(task["task_id"], staff["staff_id"], role)
            if same_slot is not None:
                if self._same_assignment(same_slot, task, staff, role, minutes, due_iso):
                    return same_slot, False
                raise AlreadyAssigned(
                    "this staff already holds a different assignment in this role on this task",
                    assignment_id=str(same_slot["assignment_id"]),
                    task_id=task["task_id"],
                    staff_id=staff["staff_id"],
                    role_in_task=role,
                )
            used = self.capacity_used(staff["staff_id"], due)
            contract = int(staff["weekly_contract_minutes"])
            if used + minutes > contract:
                raise CapacityExceeded(
                    "assignment would exceed the staff weekly contract minutes",
                    staff_id=staff["staff_id"],
                    weekly_contract_minutes=contract,
                    minutes_already_planned=used,
                    planned_minutes=minutes,
                )
            record = {
                "assignment_id": self.ids.new(ASSIGNMENT_ID_PREFIX),
                "agency_id": self.agency_id,
                "task_id": task["task_id"],
                "staff_id": staff["staff_id"],
                "role_in_task": role,
                "planned_minutes": minutes,
                "status": "ASSIGNED",
                "assigned_by": actor,
                "assigned_at": self._now_iso(),
                "due_at_utc": due_iso,
                "completed_at_utc": None,
                "drop_reason": None,
                "idempotency_key": key,
            }
            inserted = insert_once(
                self.conn,
                table="workforce_task_assignments",
                columns=record,
                conflict_on=("agency_id", "idempotency_key"),
            )
            if not inserted:  # pragma: no cover - dijaga transaksi IMMEDIATE
                raise AlreadyAssigned(
                    "assignment was written concurrently", idempotency_key=key
                )
            return dict(record), True

    # -- perpindahan status ----------------------------------------------- #
    def accept(self, assignment_id: str) -> dict[str, Any]:
        """``ASSIGNED`` → ``ACCEPTED``; sudah ``ACCEPTED`` dikembalikan apa adanya."""
        return self._move(assignment_id, "ACCEPTED", allowed=("ASSIGNED",))

    def start(self, assignment_id: str) -> dict[str, Any]:
        """``ASSIGNED``/``ACCEPTED`` → ``IN_PROGRESS``."""
        return self._move(assignment_id, "IN_PROGRESS", allowed=("ASSIGNED", "ACCEPTED"))

    def complete(self, assignment_id: str) -> dict[str, Any]:
        """``IN_PROGRESS`` → ``DONE`` dengan waktu selesai dari jam penulis."""
        return self._move(assignment_id, "DONE", allowed=("IN_PROGRESS",))

    def drop(self, assignment_id: str, *, reason: str) -> dict[str, Any]:
        """Batalkan penugasan dengan alasan wajib."""
        text = require_text(reason, "reason", max_len=200, allow_empty=True)
        if len(text) < MIN_REASON_LENGTH:
            raise DropReasonRequired(
                "dropping an assignment must explain why",
                assignment_id=assignment_id,
                reason=text,
            )
        existing = self.get(assignment_id)
        if existing["status"] == "DROPPED":
            return existing
        return self._move(assignment_id, "DROPPED", allowed=OPEN_ASSIGNMENT_STATUSES, reason=text)

    def _move(
        self,
        assignment_id: str,
        target: str,
        *,
        allowed: Iterable[str],
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        with write_transaction(self.conn):
            row = self.get(assignment_id)
            current = str(row["status"])
            if current == target:
                return row
            if current not in tuple(allowed):
                raise InvalidTransition(
                    f"cannot move an assignment from {current} to {target}",
                    assignment_id=assignment_id,
                    status=current,
                    target=target,
                )
            now = self._now_iso()
            self.conn.execute(
                "UPDATE workforce_task_assignments SET status = ?, completed_at_utc = ?, drop_reason = ?"
                " WHERE agency_id = ? AND assignment_id = ?",
                (
                    target,
                    now if target == "DONE" else row["completed_at_utc"],
                    reason if target == "DROPPED" else row["drop_reason"],
                    self.agency_id,
                    assignment_id,
                ),
            )
            return self.get(assignment_id)

    # -- pembantu ---------------------------------------------------------- #
    def _now_iso(self) -> str:
        moment = self.clock.now()
        if moment.tzinfo is None:
            raise DomainError(
                "clock returned a naive datetime; refusing to stamp work records",
                code="NAIVE_TIMESTAMP",
            )
        return to_iso(moment.astimezone(UTC))

    def _staff(self, staff_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM workforce_staff WHERE agency_id = ? AND staff_id = ?",
            (self.agency_id, staff_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound(
                "staff not found", code="ENTITY_NOT_FOUND", staff_id=staff_id
            )
        return dict(row)

    def _task(self, task_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM ops_tasks WHERE agency_id = ? AND task_id = ?",
            (self.agency_id, task_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound(
                "task not found", code="ENTITY_NOT_FOUND", task_id=task_id
            )
        return dict(row)

    def _by_key(self, key: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM workforce_task_assignments WHERE agency_id = ? AND idempotency_key = ?",
            (self.agency_id, key),
        ).fetchone()
        return dict(row) if row is not None else None

    def _by_slot(self, task_id: str, staff_id: str, role: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM workforce_task_assignments"
            " WHERE agency_id = ? AND task_id = ? AND staff_id = ? AND role_in_task = ?",
            (self.agency_id, task_id, staff_id, role),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _same_assignment(
        row: dict[str, Any],
        task: dict[str, Any],
        staff: dict[str, Any],
        role: str,
        minutes: int,
        due_iso: str,
    ) -> bool:
        return (
            row["task_id"] == task["task_id"]
            and row["staff_id"] == staff["staff_id"]
            and row["role_in_task"] == role
            and int(row["planned_minutes"]) == minutes
            and row["due_at_utc"] == due_iso
        )
