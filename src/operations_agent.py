"""Agent 4 — the Operations AI Agent: workflow telemetry with zero write paths.

§10 of the AI Workforce Architecture V1.1. The agent *orchestrates watch*: it
reads the workflow facts the services already recorded and turns them into the
ranked reports a human Operations Manager works from. It creates no task,
assigns no assignee, forces no state, and resolves no exception — those paths
stay in ``ops_tasks.py`` / ``reservation_service.py`` / ``incidents.py`` behind
human decisions.

What this module owns — and refuses to do:

- ``collect()`` answers one question in one instant: how much work is open per
  task kind and status, how much of it is past its ``due_at`` or due within the
  next 24 hours, how many open incidents there are per severity and how many
  have blown their first-response window (``incidents.SEVERITY_SLA_MINUTES``),
  which confirmed bookings are still missing their ``CONFIRMATION`` document,
  how many bookings are in flight for arrival, and how many exception
  fingerprints / recon cases / unverified webhooks the rest of the system has
  already raised.
- Every number is read from recorded rows — ``ops_tasks`` for W01/W02,
  ``incidents`` for W02/W08, ``documents`` + ``bookings`` for W03,
  ``bookings`` for W05, ``exception_fingerprints`` +
  ``reconciliation_cases`` + ``payment_events`` for W07. Nothing is estimated
  between them: no predicted load, no forecast completion.
- The report writes nothing. Read-only is enforced by query shape: the
  collector only SELECTs. Missing facts are refused loudly
  (``OperationsClientError``), never swallowed into a zero.

Authority is structural: the report is derived data with zero write paths, and
the agent's registered tools are LOW tier — the gateway's tier ceiling, not
this module's good intentions, is what keeps task-writing and state-forcing
tools out of reach. The agent may *recommend*; the human *decides*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

import primitives as P

ZERO_SHA256 = "0" * 64

#: §10 read-tier tools. Every verb here is a read: the agent ranks operational
#: facts for a human. No create/assign/transition/resolve verb may be added
#: from this module — those stay in the services that already own them.
OPERATIONS_TOOLS: tuple[tuple[str, str, Optional[str], str], ...] = (
    ("ops.workflow", "LOW", None, "Read open work per task kind and status"),
    ("ops.sla", "LOW", None, "Read overdue work and incident SLA breaches"),
    ("ops.readiness", "LOW", None, "Read pre-arrival readiness and doc gaps"),
)

#: The name the Control Tower's closed vocabulary routes OPS_PROCESS to.
OPERATIONS_AGENT_NAME = "operations_ai"

#: Task statuses that still represent work someone must do. DONE and CANCELLED
#: are terminal: they neither count as workload nor can they be overdue.
ACTIVE_TASK_STATUSES: tuple[str, ...] = ("OPEN", "IN_PROGRESS", "BLOCKED")

#: Incident statuses that still carry a clock (mirrors incidents.OPEN_STATUSES).
OPEN_INCIDENT_STATUSES: tuple[str, ...] = ("OPEN", "MITIGATING")

#: Booking statuses whose dates are locked and therefore must have their
#: guest-facing confirmation document on file.
CONFIRMED_BOOKING_STATUSES: tuple[str, ...] = (
    "CONFIRMED",
    "BALANCE_DUE",
    "BALANCE_OVERDUE",
    "PRE_ARRIVAL",
    "IN_STAY",
)

#: Document statuses that satisfy a requirement. VOID and SUPERSEDED-in-error
#: do not: a voided confirmation is not a confirmation.
SATISFYING_DOC_STATUSES: tuple[str, ...] = ("GENERATED", "SUPERSEDED")

#: Pre-arrival: the booking is close enough that operations must be ready.
IN_FLIGHT_BOOKING_STATUSES: tuple[str, ...] = ("PRE_ARRIVAL", "IN_STAY")

DUE_SOON_WINDOW = timedelta(hours=24)
ARRIVAL_WINDOW = timedelta(hours=72)

ACTIVE_RECON_STATUS = "OPEN"


class OperationsClientError(P.DomainError):
    """The telemetry cannot answer against the storage it was given."""

    code = "OPS_TELEMETRY_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# report rows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TaskSurvey:
    """W01: open work, sliced by kind and status."""

    open_count: int
    open_by_kind: dict[str, int]
    open_by_status: dict[str, int]
    overdue_count: int
    due_within_24h: int
    blocked_count: int
    unassigned_count: int


@dataclass(frozen=True)
class IncidentSurvey:
    """W02/W08: open incidents and their response-window honesty."""

    open_by_severity: dict[str, int]
    open_count: int
    sla_breached_count: int


@dataclass(frozen=True)
class DocumentSurvey:
    """W03: guest-facing document completeness for locked-in bookings."""

    confirmed_count: int
    confirmed_without_doc: int


@dataclass(frozen=True)
class PreArrivalSurvey:
    """W05: what operations must be ready for."""

    in_flight_count: int
    arriving_within_72h: int


@dataclass(frozen=True)
class ExceptionSurvey:
    """W07: what the rest of the system already flagged."""

    fingerprint_count: int
    total_occurrences: int
    open_recon_cases: int
    unverified_webhook_events: int


@dataclass(frozen=True)
class OperationsKPI:
    """§10 KPI families, read from the same facts. ``None`` when the
    denominator is empty — a rate over nothing is unknown, not 0%."""

    overdue_rate: Optional[float]
    sla_compliance: Optional[float]
    doc_completeness_rate: Optional[float]


@dataclass(frozen=True)
class OperationsSnapshot:
    """Everything §10 asks for, as of one instant."""

    tasks: TaskSurvey
    incidents: IncidentSurvey
    documents: DocumentSurvey
    pre_arrival: PreArrivalSurvey
    exceptions: ExceptionSurvey
    kpi: OperationsKPI
    generated_at: str


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


# --------------------------------------------------------------------------- #
# collector — SELECT only
# --------------------------------------------------------------------------- #
def collect(conn: Any, *, clock: Any, agency_id: str = "agency_default") -> OperationsSnapshot:
    """Read the workflow facts for one agency at one instant. Writes nothing."""
    try:
        _require_tables(
            conn, "ops_tasks", "incidents", "documents", "bookings",
            "exception_fingerprints", "reconciliation_cases", "payment_events",
        )
        now_iso = P.to_iso(clock.now())
        due_soon_iso = P.to_iso(clock.now() + DUE_SOON_WINDOW)
        arrival_iso = P.to_iso(clock.now() + ARRIVAL_WINDOW)

        active_marks = ",".join("?" for _ in ACTIVE_TASK_STATUSES)

        # ---- W01: open work ------------------------------------------------
        open_rows = conn.execute(
            "SELECT kind, status, assignee, due_at FROM ops_tasks"
            f" WHERE agency_id = ? AND status IN ({active_marks})",
            (agency_id, *ACTIVE_TASK_STATUSES),
        ).fetchall()
        open_by_kind: dict[str, int] = {}
        open_by_status: dict[str, int] = {}
        overdue = 0
        due_soon = 0
        unassigned = 0
        for row in open_rows:
            kind = str(row[0])
            status = str(row[1])
            open_by_kind[kind] = open_by_kind.get(kind, 0) + 1
            open_by_status[status] = open_by_status.get(status, 0) + 1
            due_at = str(row[3]) if row[3] is not None else None
            if due_at is not None and due_at < now_iso:
                overdue += 1
            elif due_at is not None and due_at <= due_soon_iso:
                due_soon += 1
            if not row[2]:
                unassigned += 1
        blocked = open_by_status.get("BLOCKED", 0)
        tasks = TaskSurvey(
            open_count=len(open_rows),
            open_by_kind=open_by_kind,
            open_by_status=open_by_status,
            overdue_count=overdue,
            due_within_24h=due_soon,
            blocked_count=blocked,
            unassigned_count=unassigned,
        )

        # ---- W02: incidents and their response window ----------------------
        open_marks = ",".join("?" for _ in OPEN_INCIDENT_STATUSES)
        incident_rows = conn.execute(
            "SELECT severity, detected_at FROM incidents"
            f" WHERE agency_id = ? AND status IN ({open_marks})",
            (agency_id, *OPEN_INCIDENT_STATUSES),
        ).fetchall()
        open_by_severity: dict[str, int] = {}
        breached = 0
        for row in incident_rows:
            severity = str(row[0])
            open_by_severity[severity] = open_by_severity.get(severity, 0) + 1
            window = _response_window_minutes(severity)
            if window is None:
                continue
            deadline = P.to_iso(P.parse_iso(str(row[1])) + timedelta(minutes=window))
            if deadline < now_iso:
                breached += 1
        incidents = IncidentSurvey(
            open_by_severity=open_by_severity,
            open_count=len(incident_rows),
            sla_breached_count=breached,
        )

        # ---- W03: document completeness ------------------------------------
        confirmed_marks = ",".join("?" for _ in CONFIRMED_BOOKING_STATUSES)
        satisfying_marks = ",".join("?" for _ in SATISFYING_DOC_STATUSES)
        confirmed_rows = conn.execute(
            "SELECT booking_id FROM bookings"
            f" WHERE agency_id = ? AND status IN ({confirmed_marks})",
            (agency_id, *CONFIRMED_BOOKING_STATUSES),
        ).fetchall()
        documented = conn.execute(
            "SELECT DISTINCT booking_id FROM documents"
            " WHERE kind = 'CONFIRMATION' AND booking_id IS NOT NULL"
            f"   AND status IN ({satisfying_marks})",
            SATISFYING_DOC_STATUSES,
        ).fetchall()
        documented_ids = {str(r[0]) for r in documented}
        missing = [
            str(r[0]) for r in confirmed_rows if str(r[0]) not in documented_ids
        ]
        documents = DocumentSurvey(
            confirmed_count=len(confirmed_rows),
            confirmed_without_doc=len(missing),
        )

        # ---- W05: pre-arrival readiness ------------------------------------
        in_flight = conn.execute(
            "SELECT COUNT(*) FROM bookings"
            " WHERE agency_id = ? AND status IN ('PRE_ARRIVAL','IN_STAY')",
            (agency_id,),
        ).fetchone()[0]
        arriving = conn.execute(
            "SELECT COUNT(*) FROM bookings"
            " WHERE agency_id = ? AND status = 'PRE_ARRIVAL'"
            "   AND check_in >= ? AND check_in <= ?",
            (agency_id, now_iso, arrival_iso),
        ).fetchone()[0]
        pre_arrival = PreArrivalSurvey(
            in_flight_count=int(in_flight or 0),
            arriving_within_72h=int(arriving or 0),
        )

        # ---- W07: exception survey -----------------------------------------
        fp_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(occurrences), 0)"
            " FROM exception_fingerprints WHERE agency_id = ?",
            (agency_id,),
        ).fetchone()
        recon = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_cases"
            " WHERE agency_id = ? AND status = ?",
            (agency_id, ACTIVE_RECON_STATUS),
        ).fetchone()[0]
        unverified = conn.execute(
            "SELECT COUNT(*) FROM payment_events WHERE signature_verified = 0"
        ).fetchone()[0]
        exceptions = ExceptionSurvey(
            fingerprint_count=int(fp_row[0] or 0),
            total_occurrences=int(fp_row[1] or 0),
            open_recon_cases=int(recon or 0),
            unverified_webhook_events=int(unverified or 0),
        )

        # ---- KPI ------------------------------------------------------------
        kpi = OperationsKPI(
            overdue_rate=_rate(overdue, len(open_rows)),
            sla_compliance=(
                None if not open_rows else 1.0 - (overdue / len(open_rows))
            ),
            doc_completeness_rate=(
                None
                if not confirmed_rows
                else 1.0 - (len(missing) / len(confirmed_rows))
            ),
        )

        return OperationsSnapshot(
            tasks=tasks, incidents=incidents, documents=documents,
            pre_arrival=pre_arrival, exceptions=exceptions, kpi=kpi,
            generated_at=now_iso,
        )
    except OperationsClientError:
        raise
    except Exception as exc:                      # noqa: BLE001
        raise OperationsClientError(
            "operations telemetry unavailable: query over recorded facts failed",
            detail=str(exc),
        ) from exc


def _response_window_minutes(severity: str) -> Optional[int]:
    """First-response window per severity, read from the incidents service.

    Imported lazily so this module carries no hard dependency on the incident
    engine's internals: if the vocabulary ever changes, an unknown severity is
    reported as "no window known" rather than silently treated as breached.
    """
    import incidents as INC

    window = INC.SEVERITY_SLA_MINUTES.get(severity)
    return None if window is None else int(window[0])


def _require_tables(conn: Any, *tables: str) -> None:
    for table in tables:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not row:
            raise OperationsClientError(
                f"operations telemetry unavailable: table {table!r} is missing"
            )


# --------------------------------------------------------------------------- #
# identity — read-tier only, enforced by the gateway's ceiling
# --------------------------------------------------------------------------- #
def ensure_operations_tools(conn: Any, *, at: str) -> int:
    """Insert the operations agent's read-tier tools if absent. Idempotent."""
    inserted = 0
    for name, tier, policy_key, description in OPERATIONS_TOOLS:
        try:
            conn.execute(
                "INSERT INTO ai_tools (tool, tier, policy_key, spec_ref,"
                " description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, tier, policy_key, "§10", description, at),
            )
            inserted += 1
        except Exception:  # noqa: BLE001 - already present is the happy path
            pass  # idempotent by design
    return inserted


def register_principal(ai: Any, *, secret_ref: str, owner_actor: str,
                       at: Any = None) -> Any:
    """Register the operations agent as one machine principal among the workforce.

    Its tools are read-tier (see :data:`OPERATIONS_TOOLS`); the gateway's tier
    ceiling — not this module's good intentions — is what keeps an
    administrator from writing task state.
    """
    if ai is None:
        raise OperationsClientError(
            "no AI service wired: the agent cannot mint its own identity",
            code="OPS_NO_AI_SERVICE",
        )
    return ai.register_principal(
        OPERATIONS_AGENT_NAME, secret_ref=secret_ref, actor=owner_actor, at=at
    )
