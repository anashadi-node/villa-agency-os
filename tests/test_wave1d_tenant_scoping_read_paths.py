"""Wave 1-D RED tests: tenant scope for dashboard/reporting + ops/incidents/connectors.

Master Spec authority: backend/domain read paths must enforce ``agency_id``.
A dashboard, ops queue, incident view, or connector outbox for agency B must not
see agency A rows even when identifiers are known.
"""

from __future__ import annotations

import pytest

import connectors as K
import dashboard as D
import incidents as INC
import ops_tasks as OT
from primitives import FrozenClock, IdFactory, canonical_json, sha256_hex, to_iso
from schema_loader import connect_sqlite

NOW = "2026-09-21T08:00:00Z"
DUE = "2026-09-20T08:00:00+00:00"
LATER = "2026-09-22T08:00:00+00:00"
ZERO_HASH = "0" * 64
TOKENS = {f"tok-{role}": (f"agent-{role}-1", role) for role in D.ROLES}


def seed_agencies(conn, *agency_ids: str) -> None:
    for agency_id in agency_ids:
        conn.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'ACTIVE', ?, ?)"
            " ON CONFLICT(agency_id) DO NOTHING",
            (agency_id, agency_id, NOW, NOW),
        )


def insert_case(conn, *, agency_id: str, case_id: str, status: str = "NEW", guest_ref: str = "guest_a") -> None:
    conn.execute(
        "INSERT INTO cases (case_id, agency_id, channel, channel_ref, guest_ref, status, priority,"
        " owner_actor, booking_id, sla_policy, first_response_due_at, quote_due_at,"
        " first_responded_at, quoted_at, idempotency_key, created_at, updated_at, closed_at, close_reason)"
        " VALUES (?, ?, 'website', ?, ?, ?, 'NORMAL', NULL, NULL, 'std', ?, ?,"
        " NULL, NULL, ?, ?, ?, NULL, NULL)",
        (
            case_id,
            agency_id,
            f"ref_{case_id}",
            guest_ref,
            status,
            LATER,
            "2026-09-23T08:00:00+00:00",
            f"idem_{case_id}",
            NOW,
            NOW,
        ),
    )


def insert_task(conn, *, agency_id: str, task_id: str, due_at: str = DUE) -> None:
    conn.execute(
        "INSERT INTO ops_tasks (task_id, agency_id, kind, booking_id, unit_id, case_id, due_at,"
        " priority, assignee, status, checklist, idempotency_key, created_at, updated_at,"
        " completed_at, blocked_reason)"
        " VALUES (?, ?, 'VERIFICATION', NULL, NULL, NULL, ?, 'NORMAL', 'ops_1',"
        " 'OPEN', '[]', ?, ?, ?, NULL, NULL)",
        (task_id, agency_id, due_at, f"idem_{task_id}", NOW, NOW),
    )


def insert_incident(conn, *, agency_id: str, incident_id: str, status: str = "OPEN") -> None:
    conn.execute(
        "INSERT INTO incidents (incident_id, agency_id, severity, title, detected_at, detected_by,"
        " source, playbook, affected_refs, personal_data_involved, regulatory_notification_required,"
        " notice_due_at, notified_at, status, summary, resolved_at, postmortem_ref, created_at)"
        " VALUES (?, ?, 'SEV2', 'Tenant leak check', ?, 'ops_1', 'ops_report',"
        " 'DOUBLE_BOOKING', '[]', 0, 0, NULL, NULL, ?, NULL, NULL, NULL, ?)",
        (incident_id, agency_id, NOW, status, NOW),
    )


def insert_draft(conn, *, agency_id: str, case_id: str, draft_id: str) -> None:
    insert_case(conn, agency_id=agency_id, case_id=case_id, guest_ref=f"guest_{agency_id}")
    body = f"Halo dari {agency_id}"
    conn.execute(
        "INSERT INTO message_drafts (draft_id, case_id, booking_id, channel, template, locale,"
        " recipient_ref, body_text, body_sha256, claim_bindings, grounding, gate_decision,"
        " status, approval_id, created_by, created_at, approved_by, approved_at, sent_by,"
        " sent_at, external_ref)"
        " VALUES (?, ?, NULL, 'email', 'reply', 'id-ID', ?, ?, ?, '[]', 'GROUNDED',"
        " 'ALLOW', 'APPROVED', NULL, 'agent_1', ?, 'owner_1', ?, NULL, NULL, NULL)",
        (draft_id, case_id, f"guest_{agency_id}", body, sha256_hex(body), NOW, NOW),
    )


def make_dashboard(conn, clock, agency_id: str) -> D.Dashboard:
    return D.Dashboard(
        conn,
        clock=clock,
        actor="owner-sakti-1",
        actor_role="owner",
        tokens=TOKENS,
        agency_id=agency_id,
    )


def test_dashboard_kpi_and_reporting_read_paths_are_agency_scoped(conn, clock):
    seed_agencies(conn, "agency_a", "agency_b")
    insert_case(conn, agency_id="agency_a", case_id="case_a", status="NEW")
    insert_case(conn, agency_id="agency_b", case_id="case_b", status="ESCALATED")
    insert_task(conn, agency_id="agency_a", task_id="task_a")
    insert_task(conn, agency_id="agency_b", task_id="task_b")
    insert_incident(conn, agency_id="agency_a", incident_id="inc_a")
    insert_incident(conn, agency_id="agency_b", incident_id="inc_b")

    a = make_dashboard(conn, clock, "agency_a")
    b = make_dashboard(conn, clock, "agency_b")

    assert a.kpi_values()["cases_total"] == 1
    assert a.kpi_values()["cases_by_status"] == {"NEW": 1}
    assert a.kpi_values()["late_ops_tasks"] == 1
    assert a.kpi_values()["open_incidents"] == 1
    assert b.kpi_values()["cases_total"] == 1
    assert b.kpi_values()["cases_by_status"] == {"ESCALATED": 1}


def test_ops_task_read_service_masks_cross_agency_rows(conn, clock):
    seed_agencies(conn, "agency_a", "agency_b")
    insert_task(conn, agency_id="agency_a", task_id="task_a")
    insert_task(conn, agency_id="agency_b", task_id="task_b")

    svc_a = OT.OpsTaskService(conn, clock=clock, ids=IdFactory(seed="ops-a"), agency_id="agency_a")
    assert [task.task_id for task in svc_a.list_tasks()] == ["task_a"]
    assert svc_a.counts_by_status()[OT.STATUS_OPEN] == 1
    with pytest.raises(OT.TaskNotFound):
        svc_a.get("task_b")


def test_incident_read_service_masks_cross_agency_rows(conn, clock):
    seed_agencies(conn, "agency_a", "agency_b")
    svc_a = INC.IncidentService(conn, clock=clock, ids=IdFactory(seed="inc-a"), agency_id="agency_a")
    svc_b = INC.IncidentService(conn, clock=clock, ids=IdFactory(seed="inc-b"), agency_id="agency_b")

    inc_a = svc_a.open_incident(
        playbook="DOUBLE_BOOKING",
        title="Overbooking agency A",
        severity="SEV2",
        detected_by="ops_a",
        source="ops_report",
        at=FrozenClock.at(NOW).now(),
    )
    inc_b = svc_b.open_incident(
        playbook="DOUBLE_BOOKING",
        title="Overbooking agency B",
        severity="SEV2",
        detected_by="ops_b",
        source="ops_report",
        at=FrozenClock.at(NOW).now(),
    )

    assert [item.incident_id for item in svc_a.open_incidents()] == [inc_a.incident_id]
    assert svc_a.summary_counts()["open"] == 1
    with pytest.raises(INC.IncidentNotFound):
        svc_a.get(inc_b.incident_id)


def test_connectors_inbound_and_outbound_read_paths_are_agency_scoped(conn, tmp_path):
    seed_agencies(conn, "agency_a", "agency_b")
    insert_draft(conn, agency_id="agency_a", case_id="case_a", draft_id="draft_a")
    insert_draft(conn, agency_id="agency_b", case_id="case_b", draft_id="draft_b")
    conn.execute(
        "INSERT INTO case_messages (case_message_id, case_id, direction, channel, external_ref,"
        " sender_ref, received_at, content_class, body_redacted, body_sha256, attachment_count,"
        " untrusted, created_at) VALUES ('msg_a', 'case_a', 'INBOUND', 'email', 'ext_1',"
        " 'guest_a', ?, 'P2', 'halo', ?, 0, 1, ?)",
        (NOW, ZERO_HASH, NOW),
    )

    queue_a = K.OutboundQueue(tmp_path, conn=conn, agency_id="agency_a")
    assert [draft.draft_id for draft in queue_a.pending()] == ["draft_a"]

    assert K.find_open_case(conn, "website", "guest_agency_a", agency_id="agency_a") == "case_a"
    assert K.find_open_case(conn, "website", "guest_agency_a", agency_id="agency_b") is None
    assert K.find_message_by_external_ref(conn, "email", "ext_1", agency_id="agency_a")["case_message_id"] == "msg_a"
    assert K.find_message_by_external_ref(conn, "email", "ext_1", agency_id="agency_b") is None
    with pytest.raises(K.ConnectorError):
        K.read_draft(conn, "draft_a", agency_id="agency_b")
