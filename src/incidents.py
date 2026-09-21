"""Incident management with **deterministic playbooks**.

An incident is not a free-form conversation: it is a *typed* event with a fixed
response plan, an append-only history, and — when personal data is involved — a
regulatory clock that starts ticking the moment the incident is detected.

Storage contract
----------------
* ``incidents``      — one row per incident (status, severity, detection moment,
  regulatory obligation + ``notice_due_at``, resolution/post-mortem columns).
* ``incident_steps`` — append-only history. Triggers
  ``trg_incident_steps_no_update`` / ``trg_incident_steps_no_delete`` reject
  ``UPDATE``/``DELETE``, and ``UNIQUE(incident_id, seq)`` forbids a duplicated
  sequence number. Rows may be inserted; they may never be rewritten.

Policy
------
The playbooks below are a direct translation of
``docs/RETENTION_AND_INCIDENT_RESPONSE.md`` §2 (P1..P6) plus the severity table
(S1..S4 → ``SEV1..SEV4``); every step carries a ``policy_ref`` so a reviewer can
walk from the code back to the paragraph that demanded it. Nothing here invents a
new policy: a step exists only if the document says so.

Deliberate decisions (documented so they are reviewable, not accidental)
-----------------------------------------------------------------------
* ``personal_data_involved=True`` on ``SEV1``/``SEV2`` **always** sets
  ``regulatory_notification_required=1`` with ``notice_due_at = detected_at + 72h``
  — the caller cannot opt out of the regulator clock.
* "Overdue" is strict: ``notice_due_at < now``. At exactly 72 hours the deadline
  has not passed yet (the document's "maksimal 72 jam"), one second later it has.
* ``notify()`` **records** an obligation. It never sends anything: no SMTP, no
  webhook, no HTTP. The row it appends *is* the notification evidence, and
  ``notified_at`` is the moment a human/operator declares it was sent.
* Skipping a **mandatory** playbook step is refused *before* any row is written,
  so a refusal leaves the database exactly as it was.
* The history stays append-only after ``POSTMORTEM_DONE`` (late evidence is still
  evidence) but a playbook step can never be recorded twice.
* ``created_at`` is the write moment, ``detected_at`` may be backdated by the
  caller. The writer uses ``min(created_at, detected_at)`` as the floor for
  explicit timestamps, mirroring ``CaseService``'s "no event before the case".

Machine-readable failure codes (``DomainError.code``)
-----------------------------------------------------
``INVALID_REF``, ``INVALID_FIELD``, ``INVALID_SEVERITY``, ``INVALID_STATUS``,
``INVALID_TRANSITION``, ``INVALID_TIMESTAMP``, ``UNKNOWN_PLAYBOOK``,
``INCIDENT_NOT_FOUND``, ``PLAYBOOK_STEP_VIOLATED``, ``DUPLICATE_STEP``,
``RESOLUTION_BLOCKED``, ``NOTIFICATION_BLOCKED``, ``STORAGE_CONSTRAINT``.

Readable through: ``get``, ``timeline``, ``open_incidents()``, ``by_severity``,
``overdue_notifications(now)``, ``summary_counts()``, ``verify(incident_id)``.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterator, Mapping, Optional, Sequence

from audit import AuditLog
from primitives import (
    DomainError,
    EntityNotFound,
    IdFactory,
    SystemClock,
    UTC,
    canonical_json,
    parse_iso,
    require_ref,
    require_text,
    to_iso,
)

__all__ = [
    "PLAYBOOKS",
    "PLAYBOOK_CODES",
    "Playbook",
    "PlaybookStep",
    "SEVERITIES",
    "INCIDENT_STATUSES",
    "OPEN_STATUSES",
    "SEVERITY_ORDER",
    "STATUS_ORDER",
    "ALLOWED_TRANSITIONS",
    "SEVERITY_SLA_MINUTES",
    "NOTICE_WINDOW",
    "NOTIFICATION_CHANNELS",
    "NOTIFICATION_PREFIX",
    "MIN_RESOLUTION_SUMMARY_CHARS",
    "PLAYBOOK_DOCUMENT",
    "Incident",
    "IncidentStep",
    "IncidentError",
    "InvalidRef",
    "InvalidField",
    "InvalidSeverity",
    "InvalidStatus",
    "InvalidTransition",
    "InvalidTimestamp",
    "UnknownPlaybook",
    "UnknownPlaybookError",
    "IncidentNotFound",
    "PlaybookStepViolated",
    "DuplicateStep",
    "ResolutionBlocked",
    "NotificationBlocked",
    "StorageConstraint",
    "IncidentService",
    "get_playbook",
    "playbook_codes",
    "step_codes",
    "mandatory_codes",
]


# --------------------------------------------------------------------------- #
# Domain constants
# --------------------------------------------------------------------------- #
PLAYBOOK_DOCUMENT = "docs/RETENTION_AND_INCIDENT_RESPONSE.md"

#: Severity codes are byte-for-byte what the DDL's CHECK accepts.
SEVERITIES: tuple[str, ...] = ("SEV1", "SEV2", "SEV3", "SEV4")

#: Lifecycle. Only forward, one step at a time.
INCIDENT_STATUSES: tuple[str, ...] = ("OPEN", "MITIGATING", "RESOLVED", "POSTMORTEM_DONE")
OPEN_STATUSES: tuple[str, ...] = ("OPEN", "MITIGATING")
CLOSED_STATUSES: tuple[str, ...] = ("RESOLVED", "POSTMORTEM_DONE")

STATUS_ORDER: dict[str, int] = {name: index for index, name in enumerate(INCIDENT_STATUSES)}
SEVERITY_ORDER: dict[str, int] = {name: index for index, name in enumerate(SEVERITIES)}

ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "OPEN": ("MITIGATING",),
    "MITIGATING": ("RESOLVED",),
    "RESOLVED": ("POSTMORTEM_DONE",),
    "POSTMORTEM_DONE": (),
}

#: Severity -> (first response window, resolution window) in minutes, from the
#: severity table in the policy document (S1..S4).
SEVERITY_SLA_MINUTES: dict[str, tuple[int, int]] = {
    "SEV1": (15, 1440),
    "SEV2": (60, 2880),
    "SEV3": (240, 4320),
    "SEV4": (1440, 10080),
}

#: Personal data + SEV1/SEV2 → regulator notice within 72 hours.
NOTICE_WINDOW_MINUTES = 72 * 60
NOTICE_WINDOW = timedelta(minutes=NOTICE_WINDOW_MINUTES)

#: Steps that carry a regulator clock are exactly these severities.
REGULATORY_SEVERITIES: tuple[str, ...] = ("SEV1", "SEV2")

#: Channels a notification may be *recorded* against. The system never sends
#: anything: this is the closed vocabulary the audit trail accepts, so a typo
#: cannot become a "we told someone" claim.
NOTIFICATION_CHANNELS: tuple[str, ...] = (
    "email",
    "phone",
    "whatsapp",
    "sms",
    "in_app",
    "regulator_portal",
    "written_report",
)

#: Recorded notifications are steps whose action starts with this prefix.
NOTIFICATION_PREFIX = "NOTIFY:"

MIN_RESOLUTION_SUMMARY_CHARS = 10

MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 4000
MAX_ACTION_CHARS = 500
MAX_OUTCOME_CHARS = 500
MAX_ACTOR_CHARS = 120
MAX_ROLE_CHARS = 60
MAX_SOURCE_CHARS = 60
MAX_CHANNEL_CHARS = 32
MAX_REASON_CHARS = 300
MAX_REFS = 64
MAX_POSTMORTEM_REF_CHARS = 128

DEFAULT_ACTOR = "system:kernel"
DEFAULT_ACTOR_ROLE = "UNSPECIFIED"
DEFAULT_SOURCE = "operations"

#: Audit actions (same vocabulary style as the rest of the repository).
AUDIT_INCIDENT_OPENED = "incident.opened"
AUDIT_INCIDENT_STATUS_CHANGED = "incident.status_changed"
AUDIT_INCIDENT_NOTIFIED = "incident.notified"


# --------------------------------------------------------------------------- #
# Deterministic playbooks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlaybookStep:
    """One ordered response step: *who* does *what* by *when*.

    ``mandatory`` is the flag the state machine enforces: a mandatory step cannot
    be skipped (the service refuses a later step while an earlier mandatory one is
    still unrecorded). Optional steps are documented, tracked, and never blocking.
    """

    code: str
    action: str
    owner_role: str
    due_minutes: int
    mandatory: bool = True
    policy_ref: str = ""

    def matches(self, action: str) -> bool:
        """True when ``action`` *is* this step (``"DB-01 ..."``) rather than a note."""
        if not isinstance(action, str):
            return False
        text = action.strip().upper()
        code = self.code.upper()
        return text == code or text.startswith(code + " ") or text.startswith(code + ":")

    def due_at(self, anchor: datetime) -> datetime:
        return anchor + timedelta(minutes=self.due_minutes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "action": self.action,
            "owner_role": self.owner_role,
            "due_minutes": self.due_minutes,
            "mandatory": self.mandatory,
            "policy_ref": self.policy_ref,
        }


@dataclass(frozen=True)
class Playbook:
    """An ordered, closed set of steps for one class of incident."""

    code: str
    title: str
    policy_ref: str
    steps: tuple[PlaybookStep, ...]

    @property
    def mandatory_steps(self) -> tuple[PlaybookStep, ...]:
        return tuple(step for step in self.steps if step.mandatory)

    @property
    def optional_steps(self) -> tuple[PlaybookStep, ...]:
        return tuple(step for step in self.steps if not step.mandatory)

    @property
    def mandatory_codes(self) -> tuple[str, ...]:
        return tuple(step.code for step in self.mandatory_steps)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(step.code for step in self.steps)

    def step(self, code: str) -> PlaybookStep:
        wanted = str(code).strip().upper()
        for step in self.steps:
            if step.code == wanted:
                return step
        raise InvalidRef(f"langkah tidak ada di playbook {self.code}: {code}", field="step.code")

    def index_of(self, code: str) -> int:
        wanted = str(code).strip().upper()
        for index, step in enumerate(self.steps):
            if step.code == wanted:
                return index
        raise InvalidRef(f"langkah tidak ada di playbook {self.code}: {code}", field="step.code")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "policy_ref": self.policy_ref,
            "steps": tuple(step.as_dict() for step in self.steps),
        }


def _step(code, action, owner_role, due_minutes, mandatory=True) -> PlaybookStep:
    return PlaybookStep(
        code=code,
        action=action,
        owner_role=owner_role,
        due_minutes=due_minutes,
        mandatory=mandatory,
        policy_ref=f"{PLAYBOOK_DOCUMENT} §2",
    )


#: 1:1 from the document. P3/P1 → overbooking; P2 → money; P6 → data & outages;
#: P5 → payment provider/fraud; P4 → force majeure (safety first).
_PLAYBOOK_LIST: tuple[Playbook, ...] = (
    Playbook(
        code="DOUBLE_BOOKING",
        title="Dua tamu memegang unit yang sama",
        policy_ref=f"{PLAYBOOK_DOCUMENT} §2 P3 (+P1)",
        steps=(
            _step("DB-01", "Hentikan kerusakan: suspend unit, jangan hapus record apa pun",
                  "OPERATIONS", 15),
            _step("DB-02", "Tutup kalender kanal luar setelah konfirmasi internal",
                  "CHANNEL_MANAGER", 10),
            _step("DB-03", "Tetapkan hak pertama dari waktu konfirmasi paling awal (bukti audit_events)",
                  "OPERATIONS", 30),
            _step("DB-04", "Relokasi tamu berikutnya: unit setara, batas jawaban 2 jam",
                  "GUEST_RELATIONS", 120),
            _step("DB-05", "Kompensasi sesuai policy + keputusan manusia (tercatat, bukan otomatis)",
                  "FINANCE", 240, mandatory=False),
            _step("DB-06", "Root cause: kenapa overlap lolos; perbaikan di sumber + item dicek 30 hari",
                  "OPS_MANAGER", 1440),
            _step("DB-07", "Komunikasi: satu narasi, satu penanggung jawab, tanpa janji di luar kewenangan",
                  "GUEST_RELATIONS", 180, mandatory=False),
        ),
    ),
    Playbook(
        code="PAYMENT_MISMATCH",
        title="Pembayaran tidak cocok dengan booking",
        policy_ref=f"{PLAYBOOK_DOCUMENT} §2 P2",
        steps=(
            _step("PM-01", "Tandai booking PAYMENT_UNMATCHED; jangan tandai lunas",
                  "OPERATIONS", 15),
            _step("PM-02", "Cek empat hal: nominal, mata uang, waktu, referensi/berita transfer",
                  "FINANCE", 30),
            _step("PM-03", "Kumpulkan bukti layak tutup: mutasi bank/PSP feed (screenshot/chat tidak pernah cukup)",
                  "FINANCE", 60),
            _step("PM-04", "Selisih biaya bank → penyesuaian dengan approval finance, bukan dibulatkan",
                  "FINANCE", 120, mandatory=False),
            _step("PM-05", "Jika tidak selesai <48 jam: eskalasi ke manager; hold tidak diperpanjang tanpa approval",
                  "FINANCE_MANAGER", 2880),
        ),
    ),
    Playbook(
        code="DATA_BREACH",
        title="Kebocoran/tanda kebocoran data pribadi",
        policy_ref=f"{PLAYBOOK_DOCUMENT} §2 P6",
        steps=(
            _step("BR-01", "Rotasi kredensial terdampak (rahasia tidak ditulis di dokumen/chat)",
                  "SECURITY", 15),
            _step("BR-02", "Cabut sesi & token terdampak; matikan integrasi terkait",
                  "SECURITY", 30),
            _step("BR-03", "Catat lingkup: apa yang bocor, berapa subjek, sejak kapan",
                  "DPO", 60),
            _step("BR-04", "Notifikasi internal <1 jam ke pemilik data + penanggung jawab",
                  "DPO", 60),
            _step("BR-05", "Keputusan notifikasi eksternal (subjek/otoritas) sesuai UU 27/2022 & kontrak; bukan oleh tim teknis sendirian",
                  "DPO", NOTICE_WINDOW_MINUTES),
            _step("BR-06", "Post-mortem sistem + perbaikan kontrol dengan item dicek 30 hari",
                  "OPS_MANAGER", 10080, mandatory=False),
        ),
    ),
    Playbook(
        code="VENUE_OUTAGE",
        title="Unit/venue tidak bisa dihuni atau tidak tersedia",
        policy_ref=f"{PLAYBOOK_DOCUMENT} §2 P1",
        steps=(
            _step("VO-01", "Hentikan kerusakan <15 menit: suspend unit, jangan hapus record apa pun",
                  "OPERATIONS", 15),
            _step("VO-02", "Verifikasi penyebab dan tingkat keparahan dari bukti, bukan dugaan",
                  "MAINTENANCE", 60),
            _step("VO-03", "Relokasi tamu terdampak: unit setara/lebih baik, selisih ditanggung agency, batas jawaban 2 jam",
                  "GUEST_RELATIONS", 120),
            _step("VO-04", "Kompensasi sesuai policy + keputusan manusia (tercatat)",
                  "FINANCE", 240, mandatory=False),
            _step("VO-05", "Root cause: kenapa lolos; perbaikan di sumber + item dicek 30 hari",
                  "OPS_MANAGER", 1440),
            _step("VO-06", "Komunikasi: satu narasi, satu penanggung jawab, tanpa janji di luar kewenangan",
                  "GUEST_RELATIONS", 180, mandatory=False),
        ),
    ),
    Playbook(
        code="SAFETY_EMERGENCY",
        title="Darurat keselamatan / force majeure",
        policy_ref=f"{PLAYBOOK_DOCUMENT} §2 P4",
        steps=(
            _step("SE-01", "Prioritaskan keselamatan: instruksi/evakuasi tamu, lalu akomodasi alternatif, lalu dana",
                  "SAFETY_OFFICER", 15),
            _step("SE-02", "Aktifkan FORCE_MAJEURE pada properti terdampak (unit SUSPENDED)",
                  "OPS_MANAGER", 30),
            _step("SE-03", "Bacakan klausul force majeure per properti apa adanya; jangan improvisasi janji refund",
                  "LEGAL", 60),
            _step("SE-04", "Notifikasi massal memakai template yang disetujui manager",
                  "GUEST_RELATIONS", 120),
            _step("SE-05", "Tidak ada janji nominal sebelum dihitung finance; penyelesaian dana dicatat",
                  "FINANCE", 240, mandatory=False),
            _step("SE-06", "Laporan pasca-insiden: satu item perbaikan dicek 30 hari",
                  "OPS_MANAGER", 2880, mandatory=False),
        ),
    ),
    Playbook(
        code="PROVIDER_FAILURE",
        title="Penyedia pembayaran/integrasi gagal atau dicurigai fraud",
        policy_ref=f"{PLAYBOOK_DOCUMENT} §2 P5",
        steps=(
            _step("PF-01", "Jangan pernah menyimpan data kartu; semua penanganan lewat PSP",
                  "SECURITY", 15),
            _step("PF-02", "Isolasi penyedia/integrasi terdampak; hentikan sinkronisasi yang merusak data",
                  "OPERATIONS", 30),
            _step("PF-03", "Kumpulkan bukti chargeback <24 jam (kontrak, policy disetujui, log konfirmasi, bukti layanan)",
                  "FINANCE", 1440),
            _step("PF-04", "Indikasi fraud (identitas palsu, minta OTP, rekening pribadi, urgensi ekstrem): berhenti, eskalasi, jangan lanjutkan pembayaran",
                  "FINANCE_MANAGER", 60),
            _step("PF-05", "Rekening tujuan hanya dari master terverifikasi; perubahan via chat = verifikasi dua langkah + jeda 24 jam",
                  "FINANCE", 1440),
            _step("PF-06", "Petakan dampak ke booking in-flight & hold berjalan; jangan perpanjang hold tanpa approval",
                  "OPERATIONS", 120, mandatory=False),
            _step("PF-07", "Komunikasi pemulihan; aktifkan ulang integrasi hanya setelah verifikasi",
                  "OPS_MANAGER", 240, mandatory=False),
        ),
    ),
)

PLAYBOOKS: dict[str, Playbook] = {playbook.code: playbook for playbook in _PLAYBOOK_LIST}
PLAYBOOK_CODES: tuple[str, ...] = tuple(PLAYBOOKS)


def _assert_contract() -> None:
    """Import-time sanity: the catalogue is closed, ordered, and enforced."""
    for playbook in PLAYBOOKS.values():
        if not playbook.steps:
            raise AssertionError(f"playbook tanpa langkah: {playbook.code}")  # pragma: no cover
        if not playbook.mandatory_steps:
            raise AssertionError(f"playbook tanpa langkah WAJIB: {playbook.code}")  # pragma: no cover
        codes = playbook.codes
        if len(set(codes)) != len(codes):
            raise AssertionError(f"kode langkah duplikat di {playbook.code}")  # pragma: no cover
        for step in playbook.steps:
            if step.due_minutes <= 0:
                raise AssertionError(f"tenggat langkah tidak masuk akal: {step.code}")  # pragma: no cover
    if set(SEVERITIES) != {"SEV1", "SEV2", "SEV3", "SEV4"}:  # pragma: no cover
        raise AssertionError("severities tidak sesuai DDL")


_assert_contract()


def get_playbook(code: Any) -> Playbook:
    """Return the playbook, refusing anything that is not in the catalogue."""
    if not isinstance(code, str) or not code.strip():
        raise UnknownPlaybook(f"playbook wajib diisi: {code!r}", field="playbook")
    text = code.strip()
    playbook = PLAYBOOKS.get(text)
    if playbook is None:
        raise UnknownPlaybook(
            f"playbook tidak dikenal: {text}",
            field="playbook",
            playbook=text,
            known=list(PLAYBOOK_CODES),
        )
    return playbook


def playbook_codes() -> tuple[str, ...]:
    return PLAYBOOK_CODES


def step_codes(playbook: Any) -> tuple[str, ...]:
    return get_playbook(playbook).codes


def mandatory_codes(playbook: Any) -> tuple[str, ...]:
    return get_playbook(playbook).mandatory_codes


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class IncidentError(DomainError):
    """Base class for every deliberate incident-management failure."""

    code = "INCIDENT_ERROR"


class InvalidRef(IncidentError):
    code = "INVALID_REF"


class InvalidField(IncidentError):
    code = "INVALID_FIELD"


class InvalidSeverity(IncidentError):
    code = "INVALID_SEVERITY"


class InvalidStatus(IncidentError):
    code = "INVALID_STATUS"


class InvalidTransition(IncidentError):
    code = "INVALID_TRANSITION"


class InvalidTimestamp(IncidentError):
    code = "INVALID_TIMESTAMP"


class UnknownPlaybook(IncidentError):
    code = "UNKNOWN_PLAYBOOK"


#: Alias kept for callers that spell the failure with an ``Error`` suffix.
UnknownPlaybookError = UnknownPlaybook


class IncidentNotFound(IncidentError, EntityNotFound):
    code = "INCIDENT_NOT_FOUND"


class PlaybookStepViolated(IncidentError):
    """A mandatory playbook step was skipped, or the mandatory set is incomplete."""

    code = "PLAYBOOK_STEP_VIOLATED"


class DuplicateStep(IncidentError):
    code = "DUPLICATE_STEP"


class ResolutionBlocked(IncidentError):
    code = "RESOLUTION_BLOCKED"


class NotificationBlocked(IncidentError):
    code = "NOTIFICATION_BLOCKED"


class StorageConstraint(IncidentError):
    code = "STORAGE_CONSTRAINT"


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _bounded_text(
    value: Any,
    field_name: str,
    *,
    max_len: int,
    minimum: int = 1,
    code: str = InvalidField.code,
) -> str:
    if not isinstance(value, str):
        raise InvalidField(f"{field_name} wajib teks", code=code, field=field_name)
    try:
        text = require_text(value, field_name, max_len=max_len)
    except DomainError as exc:
        raise InvalidField(
            f"{field_name} tidak valid: {exc.message}", code=code, field=field_name
        ) from exc
    if len(text) < minimum:
        raise InvalidField(
            f"{field_name} minimal {minimum} karakter",
            code=code,
            field=field_name,
            length=len(text),
        )
    return text


def _bounded_ref(value: Any, field_name: str, *, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise InvalidRef(f"{field_name} harus teks", field=field_name)
    try:
        ref = require_ref(value, field_name)
    except DomainError as exc:
        raise InvalidRef(f"{field_name} tidak valid: {exc.message}", field=field_name) from exc
    if len(ref) < minimum:
        raise InvalidRef(
            f"{field_name} minimal {minimum} karakter", field=field_name, length=len(ref)
        )
    return ref


def _require_refs(refs: Any, field_name: str = "affected_refs") -> tuple[str, ...]:
    if refs is None:
        return ()
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
        raise InvalidRef(f"{field_name} harus urutan id", field=field_name)
    out: list[str] = []
    for item in refs:
        out.append(_bounded_ref(item, f"{field_name}[]"))
    if len(out) > MAX_REFS:
        raise InvalidRef(f"{field_name} maksimal {MAX_REFS} item", field=field_name, length=len(out))
    return tuple(out)


def _as_datetime(value: Any, field_name: str) -> datetime:
    """Accept an aware ``datetime`` or an ISO-8601 string; refuse everything else."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise InvalidTimestamp(f"{field_name} harus timezone-aware", field=field_name)
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            return parse_iso(value).astimezone(UTC)
        except DomainError as exc:
            raise InvalidTimestamp(f"{field_name} bukan timestamp ISO-8601", field=field_name) from exc
        except ValueError as exc:  # pragma: no cover - parse_iso raises DomainError
            raise InvalidTimestamp(f"{field_name} bukan timestamp ISO-8601", field=field_name) from exc
    raise InvalidTimestamp(f"{field_name} harus timestamp", field=field_name)


def _refs_from_json(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = __import__("json").loads(raw)
    except (TypeError, ValueError):  # pragma: no cover - DDL default is '[]'
        return ()
    if not isinstance(parsed, list):  # pragma: no cover - defensive
        return ()
    return tuple(str(item) for item in parsed)


def _notification_channel(action: str) -> Optional[str]:
    if not isinstance(action, str):
        return None
    text = action.strip()
    if not text.upper().startswith(NOTIFICATION_PREFIX):
        return None
    return text[len(NOTIFICATION_PREFIX):].strip() or None


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Incident:
    incident_id: str
    playbook: str
    title: str
    severity: str
    status: str
    detected_at: str
    detected_by: str
    source: str
    affected_refs: tuple[str, ...]
    personal_data_involved: bool
    regulatory_notification_required: bool
    notice_due_at: Optional[str]
    notified_at: Optional[str]
    summary: Optional[str]
    resolved_at: Optional[str]
    postmortem_ref: Optional[str]
    created_at: str
    sla_response_minutes: Optional[int] = None
    sla_resolution_minutes: Optional[int] = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES

    @property
    def response_due_at(self) -> Optional[datetime]:
        if self.sla_response_minutes is None:
            return None
        return parse_iso(self.detected_at) + timedelta(minutes=self.sla_response_minutes)

    @property
    def resolution_due_at(self) -> Optional[datetime]:
        if self.sla_resolution_minutes is None:
            return None
        return parse_iso(self.detected_at) + timedelta(minutes=self.sla_resolution_minutes)

    @property
    def notice_due(self) -> Optional[datetime]:
        return None if self.notice_due_at is None else parse_iso(self.notice_due_at)

    @property
    def notified(self) -> Optional[datetime]:
        return None if self.notified_at is None else parse_iso(self.notified_at)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Incident":
        sla = SEVERITY_SLA_MINUTES.get(row["severity"])
        return cls(
            incident_id=row["incident_id"],
            playbook=row["playbook"],
            title=row["title"],
            severity=row["severity"],
            status=row["status"],
            detected_at=row["detected_at"],
            detected_by=row["detected_by"],
            source=row["source"],
            affected_refs=_refs_from_json(row["affected_refs"]),
            personal_data_involved=bool(row["personal_data_involved"]),
            regulatory_notification_required=bool(row["regulatory_notification_required"]),
            notice_due_at=row["notice_due_at"],
            notified_at=row["notified_at"],
            summary=row["summary"],
            resolved_at=row["resolved_at"],
            postmortem_ref=row["postmortem_ref"],
            created_at=row["created_at"],
            sla_response_minutes=None if sla is None else sla[0],
            sla_resolution_minutes=None if sla is None else sla[1],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "playbook": self.playbook,
            "title": self.title,
            "severity": self.severity,
            "status": self.status,
            "detected_at": self.detected_at,
            "detected_by": self.detected_by,
            "source": self.source,
            "affected_refs": list(self.affected_refs),
            "personal_data_involved": int(self.personal_data_involved),
            "regulatory_notification_required": int(self.regulatory_notification_required),
            "notice_due_at": self.notice_due_at,
            "notified_at": self.notified_at,
            "summary": self.summary,
            "resolved_at": self.resolved_at,
            "postmortem_ref": self.postmortem_ref,
            "created_at": self.created_at,
            "sla_response_minutes": self.sla_response_minutes,
            "sla_resolution_minutes": self.sla_resolution_minutes,
        }


@dataclass(frozen=True)
class IncidentStep:
    """One append-only history row, plus the playbook meaning read back out of it."""

    step_id: str
    incident_id: str
    seq: int
    action: str
    actor: str
    actor_role: str
    outcome: str
    occurred_at: str
    evidence_refs: tuple[str, ...] = ()
    step_code: Optional[str] = None
    mandatory_step: Optional[bool] = None
    notification_channel: Optional[str] = None

    @property
    def is_notification(self) -> bool:
        return self.notification_channel is not None

    @property
    def occurred(self) -> datetime:
        return parse_iso(self.occurred_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "incident_id": self.incident_id,
            "seq": self.seq,
            "action": self.action,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "outcome": self.outcome,
            "occurred_at": self.occurred_at,
            "evidence_refs": list(self.evidence_refs),
            "step_code": self.step_code,
            "mandatory_step": self.mandatory_step,
            "notification_channel": self.notification_channel,
        }


# --------------------------------------------------------------------------- #
# Actor role: kept inside the row, because `incident_steps` has no role column
# --------------------------------------------------------------------------- #
_ROLE_TAG_RE = re.compile(r"^\[([^\]]{1,%d})\]\s+(?=\S)" % MAX_ROLE_CHARS)


def tag_role(action: str, role: str) -> str:
    """Persist the actor role as a reversible ``[ROLE]`` header on the action text.

    Schema ``001_initial.sql`` gives ``incident_steps`` no ``actor_role`` column, yet
    the role belongs to the audit record.  Rather than inventing a column, the role
    rides along in the ``action`` text and :func:`split_role_tag` takes it back out,
    so ``IncidentStep.actor_role`` reads exactly what the caller passed.
    """
    if not role or role == DEFAULT_ACTOR_ROLE:
        return action
    return f"[{role}] {action}"


def split_role_tag(action: str) -> tuple[str, Optional[str]]:
    """Return ``(action_text, role)`` for the stored form of an action."""
    match = _ROLE_TAG_RE.match(action)
    if match is None:
        return action, None
    return action[match.end():], match.group(1)


@dataclass(frozen=True)
class _PreparedStep:
    """One validated history row, ready for INSERT (nothing written yet)."""

    seq: int
    occurred_at: str
    action: str
    actor: str
    actor_role: str
    outcome: str
    evidence_json: str
    plan_step: Optional[PlaybookStep]


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class IncidentService:
    """Deterministic incident lifecycle over one SQLite/PostgreSQL connection.

    The service is a *writer of obligations*, never a sender: nothing in this
    module opens a socket. Every refusal happens before (or inside a transaction
    that rolls back) any row is touched, so a rejected call leaves the ``incidents``
    and ``incident_steps`` tables byte-identical.
    """

    def __init__(self, conn: sqlite3.Connection, clock: Any = None, ids: Optional[IdFactory] = None) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self._in_tx = False
        self._audit = AuditLog(conn, clock=self.clock, ids=self.ids)

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #
    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes the lock before it reads."""
        if self._in_tx:
            raise IncidentError("transaksi bersarang tidak didukung", code="INCIDENT_NESTED_TRANSACTION")
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            self._in_tx = False
            raise
        self.conn.execute("COMMIT")
        self._in_tx = False

    def _now(self) -> datetime:
        return self.clock.now().astimezone(UTC)

    def _stamp(self) -> str:
        return to_iso(self._now())

    def _normalise_now(self, now: Any) -> datetime:
        if now is None:
            return self._now()
        return _as_datetime(now, "now")

    def _require_row(self, incident_id: Any, conn: Optional[sqlite3.Connection] = None) -> sqlite3.Row:
        ref = _bounded_ref(incident_id, "incident_id")
        db = conn if conn is not None else self.conn
        row = db.execute("SELECT * FROM incidents WHERE incident_id = ?", (ref,)).fetchone()
        if row is None:
            raise IncidentNotFound(f"insiden tidak ditemukan: {ref}", incident_id=ref)
        return row

    def _anchor(self, incident: Incident) -> datetime:
        """Floor for explicit timestamps: the earlier of write time and detection."""
        created = parse_iso(incident.created_at)
        detected = parse_iso(incident.detected_at)
        return detected if detected < created else created

    def _moment(self, at: Any, incident: Incident, field_name: str) -> datetime:
        moment = self._now() if at is None else _as_datetime(at, field_name)
        anchor = self._anchor(incident)
        if moment < anchor:
            raise InvalidTimestamp(
                f"{field_name} mendahului deteksi insiden",
                incident_id=incident.incident_id,
                detected_at=incident.detected_at,
            )
        return moment

    def _step_rows(self, incident_id: str, conn: Optional[sqlite3.Connection] = None) -> list[sqlite3.Row]:
        db = conn if conn is not None else self.conn
        return list(
            db.execute(
                "SELECT * FROM incident_steps WHERE incident_id = ? ORDER BY seq, step_id",
                (incident_id,),
            ).fetchall()
        )

    def _to_step(self, row: Mapping[str, Any], playbook: Optional[Playbook]) -> IncidentStep:
        action, role = split_role_tag(row["action"])
        code: Optional[str] = None
        mandatory: Optional[bool] = None
        if playbook is not None:
            for step in playbook.steps:
                if step.matches(action):
                    code = step.code
                    mandatory = step.mandatory
                    break
        channel = None if code is not None else _notification_channel(action)
        return IncidentStep(
            step_id=row["step_id"],
            incident_id=row["incident_id"],
            seq=int(row["seq"]),
            action=action,
            actor=row["actor"],
            actor_role=role or DEFAULT_ACTOR_ROLE,
            outcome=row["outcome"],
            occurred_at=row["occurred_at"],
            evidence_refs=_refs_from_json(row["evidence_refs"]),
            step_code=code,
            mandatory_step=mandatory,
            notification_channel=channel,
        )

    def _playbook_of(self, row: Mapping[str, Any]) -> Optional[Playbook]:
        return PLAYBOOKS.get(row["playbook"])

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def open_incident(
        self,
        *,
        playbook: Any,
        title: Any,
        severity: Any,
        detected_by: Any = None,
        source: Any = None,
        affected_refs: Any = (),
        personal_data_involved: Any = False,
        summary: Any = None,
        at: Any = None,
        incident_id: Any = None,
    ) -> Incident:
        """Open an incident and stamp its regulatory clock.

        ``personal_data_involved=True`` on ``SEV1``/``SEV2`` sets
        ``regulatory_notification_required=1`` and ``notice_due_at`` exactly 72 hours
        after ``detected_at``. Lower severities stay unregulated (``notice_due_at``
        is ``NULL``): the document demands a 72-hour regulator notice where personal
        data and severity meet, and does not set a clock where they do not.
        """
        plan = get_playbook(playbook)
        if not isinstance(severity, str) or severity not in SEVERITIES:
            raise InvalidSeverity(
                f"severity tidak dikenal: {severity!r}",
                field="severity",
                severity=severity,
                allowed=list(SEVERITIES),
            )
        title_text = _bounded_text(title, "title", max_len=MAX_TITLE_CHARS, minimum=8)
        detected_by_text = (
            DEFAULT_ACTOR if detected_by is None else _bounded_ref(detected_by, "detected_by")
        )
        source_text = (
            DEFAULT_SOURCE if source is None else _bounded_text(source, "source", max_len=MAX_SOURCE_CHARS, minimum=3)
        )
        refs = _require_refs(affected_refs)
        if personal_data_involved not in (True, False):
            raise InvalidField("personal_data_involved harus boolean", field="personal_data_involved")
        summary_text = None if summary is None else _bounded_text(
            summary, "summary", max_len=MAX_SUMMARY_CHARS
        )

        moment = self._now() if at is None else _as_datetime(at, "at")
        detected_iso = to_iso(moment)
        created_iso = self._stamp()

        regulated = bool(personal_data_involved) and severity in REGULATORY_SEVERITIES
        notice_due_iso = to_iso(moment + NOTICE_WINDOW) if regulated else None

        new_id = (
            self.ids.new("inc")
            if incident_id is None
            else _bounded_ref(incident_id, "incident_id")
        )

        with self._write() as conn:
            existing = conn.execute(
                "SELECT incident_id FROM incidents WHERE incident_id = ?", (new_id,)
            ).fetchone()
            if existing is not None:
                raise StorageConstraint(
                    f"incident_id sudah dipakai: {new_id}", incident_id=new_id
                )
            conn.execute(
                """
                INSERT INTO incidents (
                    incident_id, playbook, title, severity, status, detected_at, detected_by,
                    source, affected_refs, personal_data_involved,
                    regulatory_notification_required, notice_due_at, notified_at, summary,
                    resolved_at, postmortem_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    plan.code,
                    title_text,
                    severity,
                    "OPEN",
                    detected_iso,
                    detected_by_text,
                    source_text,
                    canonical_json(list(refs)),
                    1 if personal_data_involved else 0,
                    1 if regulated else 0,
                    notice_due_iso,
                    None,
                    summary_text,
                    None,
                    None,
                    created_iso,
                ),
            )
            row = self._require_row(new_id, conn)
            incident = Incident.from_row(row)
            self._audit.append(
                action=AUDIT_INCIDENT_OPENED,
                entity_type="incident",
                entity_id=new_id,
                actor=detected_by_text,
                actor_role=DEFAULT_ACTOR_ROLE,
                after=self._audit_projection(incident),
                reason=f"playbook={plan.code} severity={severity}",
            )
        return incident

    def _audit_projection(self, incident: Incident) -> dict[str, Any]:
        """Minimal, PII-free projection: the summary/title may hold personal data."""
        return {
            "status": incident.status,
            "severity": incident.severity,
            "playbook": incident.playbook,
            "personal_data_involved": int(incident.personal_data_involved),
            "regulatory_notification_required": int(incident.regulatory_notification_required),
            "postmortem_ref": incident.postmortem_ref,
        }

    def _prepare_step(
        self,
        conn: sqlite3.Connection,
        incident: Incident,
        *,
        action: Any,
        actor: Any,
        actor_role: Any,
        outcome: Any,
        evidence_refs: Any,
        at: Any,
        allow_playbook_check: bool = True,
    ) -> "_PreparedStep":
        """Validate one history row and reserve its ``seq`` — read-only until INSERT."""
        action_text = _bounded_text(
            action, "action", max_len=MAX_ACTION_CHARS - (MAX_ROLE_CHARS + 3), minimum=3
        )
        actor_text = DEFAULT_ACTOR if actor is None else _bounded_ref(actor, "actor")
        role_text = (
            DEFAULT_ACTOR_ROLE if actor_role is None else _bounded_text(actor_role, "actor_role", max_len=MAX_ROLE_CHARS, minimum=2)
        )
        outcome_text = _bounded_text(outcome, "outcome", max_len=MAX_OUTCOME_CHARS, minimum=3)
        refs = _require_refs(evidence_refs, "evidence_refs")
        moment = self._moment(at, incident, "at")

        plan = PLAYBOOKS.get(incident.playbook)
        plan_step: Optional[PlaybookStep] = None
        if plan is not None and allow_playbook_check:
            for step in plan.steps:
                if step.matches(action_text):
                    plan_step = step
                    break

        rows = self._step_rows(incident.incident_id, conn)
        if plan_step is not None:
            self._require_step_allowed(plan, plan_step, rows)

        next_seq = 0
        for row in rows:
            seq = int(row["seq"])
            if seq > next_seq:
                next_seq = seq
        return _PreparedStep(
            seq=next_seq + 1,
            occurred_at=to_iso(moment),
            action=tag_role(action_text, role_text),
            actor=actor_text,
            actor_role=role_text,
            outcome=outcome_text,
            evidence_json=canonical_json(list(refs)),
            plan_step=plan_step,
        )

    def _require_step_allowed(
        self, plan: Playbook, step: PlaybookStep, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        """Refuse a duplicated step, and refuse skipping an earlier mandatory one."""
        recorded: list[str] = []
        for row in rows:
            stored, _role = split_role_tag(row["action"])
            for candidate in plan.steps:
                if candidate.matches(stored):
                    recorded.append(candidate.code)
                    break
        if step.code in recorded:
            raise DuplicateStep(
                f"langkah {step.code} sudah tercatat untuk insiden ini",
                step_code=step.code,
                playbook=plan.code,
            )
        position = plan.index_of(step.code)
        missing = [
            earlier.code
            for earlier in plan.mandatory_steps
            if plan.index_of(earlier.code) < position and earlier.code not in recorded
        ]
        if missing:
            raise PlaybookStepViolated(
                f"langkah WAJIB belum tercatat sebelum {step.code}: {', '.join(missing)}",
                playbook=plan.code,
                step_code=step.code,
                missing=tuple(missing),
                recorded=tuple(recorded),
            )

    def record_step(
        self,
        *,
        incident_id: Any,
        action: Any,
        actor: Any = None,
        actor_role: Any = None,
        outcome: Any,
        evidence_refs: Any = (),
        at: Any = None,
    ) -> IncidentStep:
        """Append exactly one history row (``seq = max(seq) + 1``, no gaps, no reuse).

        If the action names a playbook step, the step must be next in line: any
        earlier *mandatory* step still missing raises :class:`PlaybookStepViolated`
        and no row is written. Free-form actions (evidence notes) are always
        accepted, and history stays append-only even after ``POSTMORTEM_DONE``.
        """
        with self._write() as conn:
            row = self._require_row(incident_id, conn)
            incident = Incident.from_row(row)
            prepared = self._prepare_step(
                conn,
                incident,
                action=action,
                actor=actor,
                actor_role=actor_role,
                outcome=outcome,
                evidence_refs=evidence_refs,
                at=at,
            )
            step_id = self.ids.new("istep")
            conn.execute(
                """
                INSERT INTO incident_steps (
                    step_id, incident_id, seq, action, actor, outcome, evidence_refs,
                    occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    incident.incident_id,
                    prepared.seq,
                    prepared.action,
                    prepared.actor,
                    prepared.outcome,
                    prepared.evidence_json,
                    prepared.occurred_at,
                ),
            )
            stored = conn.execute(
                "SELECT * FROM incident_steps WHERE step_id = ?", (step_id,)
            ).fetchone()
            if stored is None:  # pragma: no cover - INSERT above must land
                raise StorageConstraint("langkah gagal ditulis", incident_id=incident.incident_id)
            return self._to_step(stored, PLAYBOOKS.get(incident.playbook))

    def notify(
        self,
        *,
        incident_id: Any,
        channel: Any,
        sent_to_role: Any,
        actor: Any = None,
        at: Any = None,
        evidence_refs: Any = (),
    ) -> IncidentStep:
        """**Record** that a notification went out (never send it).

        The row appended here is the evidence trail — channel, destination role,
        who declared it, and when — and ``incidents.notified_at`` is stamped, which
        is what stops ``overdue_notifications`` from reporting the incident. An
        incident with no recorded step yet cannot claim a notification: an empty
        history means no response has started, and an empty response cannot have
        informed anyone.
        """
        with self._write() as conn:
            row = self._require_row(incident_id, conn)
            incident = Incident.from_row(row)
            channel_text = _bounded_text(
                channel, "channel", max_len=MAX_CHANNEL_CHARS, minimum=2
            ).lower()
            if channel_text not in NOTIFICATION_CHANNELS:
                raise InvalidField(
                    f"kanal notifikasi tidak dikenal: {channel_text}",
                    field="channel",
                    allowed=list(NOTIFICATION_CHANNELS),
                )
            role_text = _bounded_text(sent_to_role, "sent_to_role", max_len=MAX_ROLE_CHARS, minimum=2)
            actor_text = DEFAULT_ACTOR if actor is None else _bounded_ref(actor, "actor")
            existing_rows = self._step_rows(incident.incident_id, conn)
            if not existing_rows:
                raise NotificationBlocked(
                    "insiden belum punya langkah apa pun; catat respons dulu sebelum mencatat notifikasi",
                    incident_id=incident.incident_id,
                    channel=channel_text,
                )
            moment = self._moment(at, incident, "at")
            if incident.notified is not None and moment < incident.notified:
                raise InvalidTimestamp(
                    "notifikasi mundur dari notified_at",
                    incident_id=incident.incident_id,
                    notified_at=incident.notified_at,
                )
            occurred_iso = to_iso(moment)
            next_seq = 0
            for step_row in existing_rows:
                seq = int(step_row["seq"])
                if seq > next_seq:
                    next_seq = seq
            seq = next_seq + 1
            step_id = self.ids.new("istep")
            action = f"{NOTIFICATION_PREFIX}{channel_text}"
            outcome = f"notifikasi dicatat; kanal={channel_text} tujuan={role_text}"
            conn.execute(
                """
                INSERT INTO incident_steps (
                    step_id, incident_id, seq, action, actor, outcome, evidence_refs,
                    occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    incident.incident_id,
                    seq,
                    action,
                    actor_text,
                    outcome,
                    canonical_json(list(_require_refs(evidence_refs, "evidence_refs"))),
                    occurred_iso,
                ),
            )
            conn.execute(
                "UPDATE incidents SET notified_at = ? WHERE incident_id = ?",
                (occurred_iso, incident.incident_id),
            )
            stored = conn.execute(
                "SELECT * FROM incident_steps WHERE step_id = ?", (step_id,)
            ).fetchone()
            if stored is None:  # pragma: no cover - INSERT above must land
                raise StorageConstraint("notifikasi gagal dicatat", incident_id=incident.incident_id)
            self._audit.append(
                action=AUDIT_INCIDENT_NOTIFIED,
                entity_type="incident",
                entity_id=incident.incident_id,
                actor=actor_text,
                actor_role=DEFAULT_ACTOR_ROLE,
                after={"channel": channel_text, "sent_to_role": role_text, "notified_at": occurred_iso},
                reason="notifikasi dicatat (tidak dikirim oleh sistem)",
            )
            return self._to_step(stored, PLAYBOOKS.get(incident.playbook))

    def transition(
        self,
        incident_id: Any,
        to_status: Any,
        *,
        actor: Any = None,
        actor_role: Any = None,
        summary: Any = None,
        postmortem_ref: Any = None,
        reason: Any = None,
        at: Any = None,
    ) -> Incident:
        """Move one step forward: ``OPEN → MITIGATING → RESOLVED → POSTMORTEM_DONE``.

        * ``RESOLVED`` demands a summary of at least
          :data:`MIN_RESOLUTION_SUMMARY_CHARS` characters (given here or already
          stored) **and** every mandatory step of the playbook recorded.
        * ``POSTMORTEM_DONE`` demands ``postmortem_ref`` pointing at the document.
        * Any refusal leaves the row untouched (the transaction rolls back, and the
          validation happens before the ``UPDATE``).
        """
        status_text = _bounded_text(to_status, "to_status", max_len=32, minimum=2)
        if status_text not in INCIDENT_STATUSES:
            raise InvalidStatus(
                f"status tidak dikenal: {status_text}",
                field="to_status",
                allowed=list(INCIDENT_STATUSES),
            )
        if actor is not None:
            actor_text = _bounded_ref(actor, "actor")
        else:
            actor_text = DEFAULT_ACTOR
        role_text = (
            DEFAULT_ACTOR_ROLE
            if actor_role is None
            else _bounded_text(actor_role, "actor_role", max_len=MAX_ROLE_CHARS, minimum=2)
        )
        reason_text = None if reason is None else _bounded_text(reason, "reason", max_len=MAX_REASON_CHARS)

        with self._write() as conn:
            row = self._require_row(incident_id, conn)
            incident = Incident.from_row(row)
            if status_text == incident.status:
                raise InvalidTransition(
                    f"insiden sudah berstatus {status_text}",
                    incident_id=incident.incident_id,
                    status=incident.status,
                )
            if status_text not in ALLOWED_TRANSITIONS[incident.status]:
                raise InvalidTransition(
                    f"transisi {incident.status} → {status_text} tidak diizinkan",
                    incident_id=incident.incident_id,
                    from_status=incident.status,
                    to_status=status_text,
                    allowed=list(ALLOWED_TRANSITIONS[incident.status]),
                )

            moment = self._moment(at, incident, "at")
            moment_iso = to_iso(moment)
            updates: dict[str, Any] = {"status": status_text}
            stored_summary = incident.summary
            if summary is not None:
                stored_summary = _bounded_text(summary, "summary", max_len=MAX_SUMMARY_CHARS)
                updates["summary"] = stored_summary

            if status_text == "RESOLVED":
                if not stored_summary or len(stored_summary.strip()) < MIN_RESOLUTION_SUMMARY_CHARS:
                    raise ResolutionBlocked(
                        f"penyelesaian butuh ringkasan minimal {MIN_RESOLUTION_SUMMARY_CHARS} karakter",
                        incident_id=incident.incident_id,
                        summary_chars=0 if not stored_summary else len(stored_summary.strip()),
                    )
                plan = PLAYBOOKS.get(incident.playbook)
                if plan is not None:
                    recorded: list[str] = []
                    for step_row in self._step_rows(incident.incident_id, conn):
                        stored, _role = split_role_tag(step_row["action"])
                        for candidate in plan.steps:
                            if candidate.matches(stored):
                                recorded.append(candidate.code)
                                break
                    missing = tuple(code for code in plan.mandatory_codes if code not in recorded)
                    if missing:
                        raise PlaybookStepViolated(
                            f"langkah WAJIB playbook belum lengkap: {', '.join(missing)}",
                            incident_id=incident.incident_id,
                            playbook=plan.code,
                            missing=missing,
                            recorded=tuple(recorded),
                        )
                updates["resolved_at"] = moment_iso

            if status_text == "POSTMORTEM_DONE":
                if postmortem_ref is None:
                    raise ResolutionBlocked(
                        "POSTMORTEM_DONE butuh postmortem_ref",
                        incident_id=incident.incident_id,
                        field="postmortem_ref",
                    )
                updates["postmortem_ref"] = _bounded_ref(postmortem_ref, "postmortem_ref")

            assignments = ", ".join(f"{column} = ?" for column in sorted(updates))
            params = [updates[column] for column in sorted(updates)]
            params.append(incident.incident_id)
            conn.execute(
                f"UPDATE incidents SET {assignments} WHERE incident_id = ?", tuple(params)
            )
            updated = Incident.from_row(self._require_row(incident.incident_id, conn))
            if updated.status != status_text:  # pragma: no cover - UPDATE above must land
                raise StorageConstraint(
                    "status tidak berubah setelah transisi",
                    incident_id=incident.incident_id,
                    status=updated.status,
                )
            self._audit.append(
                action=AUDIT_INCIDENT_STATUS_CHANGED,
                entity_type="incident",
                entity_id=incident.incident_id,
                actor=actor_text,
                actor_role=role_text,
                before=self._audit_projection(incident),
                after=self._audit_projection(updated),
                reason=reason_text,
            )
            return updated

    # Readable convenience wrappers — same state machine, one verb each.
    def mitigate(self, incident_id: Any, *, actor: Any = None, actor_role: Any = None, reason: Any = None, at: Any = None) -> Incident:
        return self.transition(incident_id, "MITIGATING", actor=actor, actor_role=actor_role, reason=reason, at=at)

    def resolve(self, incident_id: Any, *, summary: Any = None, actor: Any = None, actor_role: Any = None, reason: Any = None, at: Any = None) -> Incident:
        return self.transition(incident_id, "RESOLVED", summary=summary, actor=actor, actor_role=actor_role, reason=reason, at=at)

    def complete_postmortem(self, incident_id: Any, *, postmortem_ref: Any, actor: Any = None, actor_role: Any = None, reason: Any = None, at: Any = None) -> Incident:
        return self.transition(incident_id, "POSTMORTEM_DONE", postmortem_ref=postmortem_ref, actor=actor, actor_role=actor_role, reason=reason, at=at)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get(self, incident_id: Any) -> Incident:
        return Incident.from_row(self._require_row(incident_id))

    def timeline(self, incident_id: Any) -> tuple[IncidentStep, ...]:
        """Ordered append-only history (seq ascending)."""
        row = self._require_row(incident_id)
        plan = self._playbook_of(row)
        return tuple(self._to_step(step, plan) for step in self._step_rows(row["incident_id"]))

    #: Alias: the timeline *is* the list of steps.
    steps = timeline

    def open_incidents(self) -> tuple[Incident, ...]:
        """Everything not yet resolved, most severe first."""
        rows = self.conn.execute(
            "SELECT * FROM incidents WHERE status IN (?, ?)",
            OPEN_STATUSES,
        ).fetchall()
        incidents = [Incident.from_row(row) for row in rows]
        incidents.sort(key=lambda item: (SEVERITY_ORDER.get(item.severity, 9), item.detected_at, item.incident_id))
        return tuple(incidents)

    def by_severity(self, severity: Any = None, *, open_only: bool = False) -> Any:
        """``by_severity("SEV1")`` → incidents; ``by_severity()`` → counts per severity."""
        if severity is None:
            counts = {name: 0 for name in SEVERITIES}
            rows = self.conn.execute("SELECT severity, COUNT(*) AS n FROM incidents GROUP BY severity").fetchall()
            for row in rows:
                if row["severity"] in counts:
                    counts[row["severity"]] = int(row["n"])
            return counts
        if not isinstance(severity, str) or severity not in SEVERITIES:
            raise InvalidSeverity(
                f"severity tidak dikenal: {severity!r}",
                field="severity",
                allowed=list(SEVERITIES),
            )
        if open_only:
            rows = self.conn.execute(
                "SELECT * FROM incidents WHERE severity = ? AND status IN (?, ?)",
                (severity,) + OPEN_STATUSES,
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM incidents WHERE severity = ?", (severity,)
            ).fetchall()
        incidents = [Incident.from_row(row) for row in rows]
        incidents.sort(key=lambda item: (item.detected_at, item.incident_id))
        return tuple(incidents)

    def overdue_notifications(self, now: Any = None) -> tuple[Incident, ...]:
        """Regulated incidents whose 72-hour notice is **strictly** past due.

        At exactly ``notice_due_at`` the deadline has not passed (the document says
        "maksimal 72 jam"); one second later it has.
        """
        moment = self._normalise_now(now)
        rows = self.conn.execute(
            "SELECT * FROM incidents WHERE regulatory_notification_required = 1 AND notified_at IS NULL"
        ).fetchall()
        overdue = []
        for row in rows:
            incident = Incident.from_row(row)
            due_iso = incident.notice_due_at
            if due_iso is None:
                continue
            if moment > parse_iso(due_iso):
                overdue.append(incident)
        overdue.sort(key=lambda item: (item.notice_due_at or "", item.incident_id))
        return tuple(overdue)

    def summary_counts(self) -> dict[str, Any]:
        """Dashboard-shaped totals; every key is present even when zero."""
        rows = self.conn.execute("SELECT * FROM incidents").fetchall()
        incidents = [Incident.from_row(row) for row in rows]
        status_counts = {name: 0 for name in INCIDENT_STATUSES}
        severity_counts = {name: 0 for name in SEVERITIES}
        for incident in incidents:
            if incident.status in status_counts:
                status_counts[incident.status] += 1
            if incident.severity in severity_counts:
                severity_counts[incident.severity] += 1
        step_total = int(
            self.conn.execute("SELECT COUNT(*) AS n FROM incident_steps").fetchone()["n"]
        )
        notification_total = int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM incident_steps WHERE UPPER(action) LIKE ?",
                (NOTIFICATION_PREFIX + "%",),
            ).fetchone()["n"]
        )
        return {
            "total": len(incidents),
            "open": status_counts["OPEN"] + status_counts["MITIGATING"],
            "closed": status_counts["RESOLVED"] + status_counts["POSTMORTEM_DONE"],
            "by_status": status_counts,
            "by_severity": severity_counts,
            "personal_data_involved": sum(1 for item in incidents if item.personal_data_involved),
            "regulatory_pending": sum(
                1
                for item in incidents
                if item.regulatory_notification_required and item.notified_at is None
            ),
            "notice_overdue": len(self.overdue_notifications()),
            "steps": step_total,
            "notifications": notification_total,
        }

    def mandatory_progress(self, incident_id: Any) -> dict[str, Any]:
        """Which mandatory steps are recorded, which are still owed."""
        row = self._require_row(incident_id)
        plan = self._playbook_of(row)
        required: tuple[str, ...] = () if plan is None else plan.mandatory_codes
        recorded: list[str] = []
        if plan is not None:
            for step_row in self._step_rows(row["incident_id"]):
                stored, _role = split_role_tag(step_row["action"])
                for candidate in plan.steps:
                    if candidate.matches(stored):
                        recorded.append(candidate.code)
                        break
        missing = tuple(code for code in required if code not in recorded)
        return {
            "incident_id": row["incident_id"],
            "playbook": row["playbook"],
            "required": required,
            "recorded": tuple(recorded),
            "missing": missing,
            "complete": not missing,
        }

    def verify(self, incident_id: Any) -> dict[str, Any]:
        """Audit one incident: mandatory steps complete **and** time columns coherent.

        Returns a report rather than raising on inconsistency, so a reviewer sees
        every defect at once. ``ok`` is ``True`` only when ``issues`` is empty.
        """
        row = self._require_row(incident_id)
        incident = Incident.from_row(row)
        plan = self._playbook_of(row)
        steps = self.timeline(incident.incident_id)
        issues: list[str] = []

        required: tuple[str, ...] = () if plan is None else plan.mandatory_codes
        recorded = tuple(step.step_code for step in steps if step.step_code)
        missing = tuple(code for code in required if code not in recorded)
        if plan is None:
            issues.append(f"playbook tidak dikenal di katalog: {incident.playbook}")
        if missing:
            issues.append(f"langkah WAJIB belum tercatat: {', '.join(missing)}")

        seqs = [step.seq for step in steps]
        if seqs != list(range(1, len(seqs) + 1)):
            issues.append(f"urutan seq tidak rapat: {seqs}")

        detected = parse_iso(incident.detected_at)
        created = parse_iso(incident.created_at)
        resolved = None if incident.resolved_at is None else parse_iso(incident.resolved_at)
        notified = None if incident.notified_at is None else parse_iso(incident.notified_at)
        for step in steps:
            if step.occurred < detected:
                issues.append(f"langkah {step.seq} mendahului detected_at")
            if step.occurred < created:
                issues.append(f"langkah {step.seq} mendahului created_at")
        if resolved is not None and resolved < detected:
            issues.append("resolved_at mendahului detected_at")
        if resolved is not None and steps and steps[-1].occurred > resolved:
            issues.append("langkah tercatat setelah resolved_at")
        if notified is not None and notified < detected:
            issues.append("notified_at mendahului detected_at")
        if incident.status in ("RESOLVED", "POSTMORTEM_DONE") and resolved is None:
            issues.append(f"status {incident.status} tanpa resolved_at")
        if incident.status in OPEN_STATUSES and resolved is not None:
            issues.append(f"status {incident.status} tetapi resolved_at terisi")
        if incident.status == "POSTMORTEM_DONE" and not incident.postmortem_ref:
            issues.append("POSTMORTEM_DONE tanpa postmortem_ref")

        if incident.regulatory_notification_required:
            if incident.notice_due_at is None:
                issues.append("regulatory_notification_required tanpa notice_due_at")
            else:
                delta = parse_iso(incident.notice_due_at) - detected
                if delta != NOTICE_WINDOW:
                    issues.append(f"notice_due_at tidak 72 jam dari detected_at: {delta}")
        elif incident.notice_due_at is not None:
            issues.append("notice_due_at terisi tanpa kewajiban notifikasi")
        if incident.personal_data_involved and incident.severity in REGULATORY_SEVERITIES:
            if not incident.regulatory_notification_required:
                issues.append("data pribadi pada SEV1/SEV2 tanpa kewajiban notifikasi regulatori")

        return {
            "incident_id": incident.incident_id,
            "playbook": incident.playbook,
            "severity": incident.severity,
            "status": incident.status,
            "required_steps": required,
            "recorded_steps": recorded,
            "missing_required_steps": missing,
            "steps": len(steps),
            "notifications": sum(1 for step in steps if step.is_notification),
            "last_seq": seqs[-1] if seqs else 0,
            "seq_contiguous": seqs == list(range(1, len(seqs) + 1)),
            "mandatory_complete": not missing,
            "regulatory_notification_required": incident.regulatory_notification_required,
            "notice_due_at": incident.notice_due_at,
            "notified_at": incident.notified_at,
            "issues": tuple(issues),
            "ok": not issues,
        }
