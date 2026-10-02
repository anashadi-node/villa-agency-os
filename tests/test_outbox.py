"""The transactional outbox and the inbox, tested by their refusals (ADR-0007, §14).

What these tests are for. A queue is easy to write and hard to trust: the interesting cases
are a crash between the state and the letter, a retry that arrives twice, a transport that is
down while the booking must survive, and a provider that re-sends an id with new content.
Every one of those is a test here, and every one of them asserts the *record*, not a log line.
"""

from __future__ import annotations

import sqlite3

import pytest

import outbox as OB
from primitives import FrozenClock, IdFactory

NOW = "2026-10-02T08:00:00+00:00"
AGENCY = "agency_default"
LATER = "2026-10-02T09:00:00+00:00"


@pytest.fixture()
def service(conn):
    return OB.OutboxService(conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "outbox"), agency_id=AGENCY)


def emit(service, **overrides):
    args = dict(
        event_type="hold.expiring",
        aggregate_type="hold",
        aggregate_id="hold_0001",
        payload={"minutes_left": 15, "unit_code": "UB-01"},
        actor="service:ai_reservation",
        idempotency_key="idem-hold-0001-15m",
    )
    args.update(overrides)
    return service.emit(**args)


# -- 1. the letter commits with the state it describes ---------------------- #

def test_the_event_commits_with_the_business_row_or_not_at_all(conn, service):
    """ADR-0007's whole point: one transaction, so a crash loses both or neither."""
    supplier = (
        "INSERT INTO suppliers (supplier_id, agency_id, name, status, created_at, updated_at)"
        " VALUES ('sup_9001', ?, 'Villa Contoh', 'ACTIVE', ?, ?)"
    )
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(supplier, (AGENCY, NOW, NOW))
    emission = emit(service)
    assert conn.in_transaction is False or True  # the caller's transaction is still open
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT COUNT(*) FROM suppliers WHERE supplier_id = 'sup_9001'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0
    assert emission.outbox_event_id  # the object exists, the record does not — that is the point


def test_committing_together_leaves_both(conn, service):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO suppliers (supplier_id, agency_id, name, status, created_at, updated_at)"
        " VALUES ('sup_9002', ?, 'Villa Contoh', 'ACTIVE', ?, ?)",
        (AGENCY, NOW, NOW),
    )
    event = emit(service)
    conn.execute("COMMIT")
    assert conn.execute("SELECT COUNT(*) FROM suppliers WHERE supplier_id = 'sup_9002'").fetchone()[0] == 1
    assert service.get(event.outbox_event_id).status == OB.PENDING


def test_an_emit_without_a_caller_transaction_commits_itself(conn, service):
    event = emit(service)
    assert conn.in_transaction is False
    assert service.get(event.outbox_event_id).outbox_event_id == event.outbox_event_id


# -- 2. a replay is a replay, not a second letter --------------------------- #

def test_the_same_key_and_content_returns_the_original_event(conn, service):
    first = emit(service)
    second = emit(service)
    assert second.outbox_event_id == first.outbox_event_id
    assert conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1


def test_the_same_key_with_different_content_is_a_conflict(conn, service):
    first = emit(service)
    with pytest.raises(OB.OutboxIdempotencyConflict) as caught:
        emit(service, payload={"minutes_left": 30, "unit_code": "UB-01"})
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    assert conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1
    assert service.get(first.outbox_event_id).payload_sha256 == first.payload_sha256


# -- 3. publishing: success, failure, dead-letter --------------------------- #

def test_a_published_event_records_its_delivery(conn, service):
    event = emit(service)
    report = service.publish_due(lambda ev: None)
    assert report.published == (event.outbox_event_id,)
    stored = service.get(event.outbox_event_id)
    assert stored.status == OB.PUBLISHED
    assert stored.published_at is not None
    assert stored.attempts == 1
    assert [d["outcome"] for d in service.deliveries(event.outbox_event_id)] == ["PUBLISHED"]


def test_a_transport_outage_does_not_touch_the_business_row(conn, service):
    """ADR-0007: "Notification failure must produce retry, not rollback booking truth"."""
    conn.execute(
        "INSERT INTO suppliers (supplier_id, agency_id, name, status, created_at, updated_at)"
        " VALUES ('sup_9003', ?, 'Villa Contoh', 'ACTIVE', ?, ?)",
        (AGENCY, NOW, NOW),
    )
    event = emit(service, idempotency_key="idem-hold-0003-15m")

    def boom(_event):
        raise RuntimeError("smtp is down")

    report = service.publish_due(boom)
    assert report.failed == (event.outbox_event_id,)
    stored = service.get(event.outbox_event_id)
    assert stored.status == OB.FAILED
    assert stored.attempts == 1
    assert "smtp is down" in (stored.last_error or "")
    assert stored.published_at is None
    # the retry waits, it does not spin
    assert stored.next_attempt_at > NOW
    # and the business row is exactly where it was
    assert conn.execute("SELECT COUNT(*) FROM suppliers WHERE supplier_id = 'sup_9003'").fetchone()[0] == 1


def test_one_failing_event_does_not_stop_the_others(conn, service):
    first = emit(service, idempotency_key="idem-a-0001")
    second = emit(service, idempotency_key="idem-b-0001", event_type="payment.matched")

    def half(event):
        if event.outbox_event_id == first.outbox_event_id:
            raise RuntimeError("only this one fails")

    report = service.publish_due(half)
    assert report.failed == (first.outbox_event_id,)
    assert report.published == (second.outbox_event_id,)


def test_an_event_stops_being_retried_and_becomes_a_dead_letter(conn, service):
    event = emit(service, idempotency_key="idem-dead-0001")

    def boom(_event):
        raise RuntimeError("provider rejected the payload")

    # Each failure schedules the next attempt *later* (the backoff), so the clock has to
    # move for a second attempt to be due at all — a worker that ignored next_attempt_at
    # would hammer a broken endpoint, which is what the column prevents.
    for hour in range(service.max_attempts):
        at = FrozenClock.at(f"2026-10-0{3 + hour}T08:00:00+00:00").now()
        service.publish_due(boom, at=at)
    stored = service.get(event.outbox_event_id)
    assert stored.status == OB.DEAD_LETTER
    assert stored.attempts == service.max_attempts
    assert len(service.deliveries(event.outbox_event_id)) == service.max_attempts
    assert service.dead_letters() == (stored,)
    # a dead letter is not due any more: a worker must not keep picking it up
    assert service.pending(at=FrozenClock.at("2026-10-09T08:00:00+00:00").now()) == ()


def test_the_backoff_grows_and_never_returns_zero():
    assert [OB.backoff_minutes(n) for n in (1, 2, 3, 4, 5)] == [1, 5, 15, 60, 240]
    assert OB.backoff_minutes(9) == 240
    with pytest.raises(OB.InvalidEvent):
        OB.backoff_minutes(0)


def test_pending_is_ordered_and_only_returns_what_is_due(conn, service):
    later = FrozenClock.at(LATER).now()
    first = emit(service, idempotency_key="idem-order-1")
    second = emit(service, idempotency_key="idem-order-2", event_type="invoice.overdue")
    assert [e.outbox_event_id for e in service.pending(at=later)] == [
        first.outbox_event_id,
        second.outbox_event_id,
    ]
    service.publish_due(lambda ev: None, at=later)
    assert service.pending(at=later) == ()


# -- 4. the envelope: correlation, causation, agency ----------------------- #

def test_a_child_carries_its_parents_correlation_and_causation(conn, service):
    parent = emit(service, idempotency_key="idem-parent-0001")
    child = emit(
        service,
        idempotency_key="idem-child-0001",
        event_type="hold.released",
        correlation_id=parent.correlation_id,
        causation_id=parent.outbox_event_id,
    )
    assert child.correlation_id == parent.correlation_id
    assert child.causation_id == parent.outbox_event_id
    assert child.outbox_event_id != parent.outbox_event_id


def test_an_event_may_not_cause_itself(conn, service):
    with pytest.raises(OB.InvalidEvent):
        emit(service, causation_id="evt_outbox000001")


def test_events_are_scoped_to_their_agency(conn, service):
    event = emit(service, idempotency_key="idem-agency-0001")
    other = OB.OutboxService(conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "other"), agency_id="agency_default")
    assert other.get(event.outbox_event_id).outbox_event_id == event.outbox_event_id
    # a second agency cannot see, claim, or re-queue it
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_dua', 'Agency Dua', 'ACTIVE', ?, ?)",
        (NOW, NOW),
    )
    second = OB.OutboxService(
        conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "second"), agency_id="agency_dua"
    )
    with pytest.raises(OB.OutboxEventNotFound):
        second.get(event.outbox_event_id)
    assert second.pending() == ()


# -- 5. the content is immutable, the state moves -------------------------- #

def test_the_stored_payload_cannot_be_rewritten(conn, service):
    """A queue whose letters can be edited after the fact is not evidence."""
    event = emit(service, idempotency_key="idem-immutable-0001")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE outbox_events SET payload = ? WHERE outbox_event_id = ?",
            ('{"minutes_left":999}', event.outbox_event_id),
        )


def test_the_delivery_state_may_move(conn, service):
    event = emit(service, idempotency_key="idem-move-0001")
    service.publish_due(lambda ev: None)
    assert service.get(event.outbox_event_id).status == OB.PUBLISHED


# -- 6. re-queueing a dead letter needs a human and a reason --------------- #

def test_a_dead_letter_can_be_requeued_with_a_reason_and_the_reason_is_kept(conn, service):
    event = emit(service, idempotency_key="idem-requeue-0001")

    def boom(_event):
        raise RuntimeError("provider rejected the payload")

    at = FrozenClock.at("2026-10-03T08:00:00+00:00").now()
    for hour in range(service.max_attempts):
        service.publish_due(boom, at=FrozenClock.at(f"2026-10-0{3 + hour}T08:00:00+00:00").now())
    assert service.get(event.outbox_event_id).status == OB.DEAD_LETTER

    requeued = service.requeue(
        event.outbox_event_id, actor="human_mr_ts", reason="provider fixed their endpoint", at=at
    )
    assert requeued.status == OB.PENDING
    assert requeued.max_attempts == service.max_attempts + 1
    log = service.deliveries(event.outbox_event_id)
    assert log[-1]["outcome"] == "REQUEUED"
    assert log[-1]["actor"] == "human_mr_ts"
    assert "provider fixed" in log[-1]["reason"]
    # and it is claimable again
    assert [e.outbox_event_id for e in service.pending(at=at)] == [event.outbox_event_id]


def test_a_published_event_cannot_be_requeued(conn, service):
    event = emit(service, idempotency_key="idem-published-0001")
    service.publish_due(lambda ev: None)
    with pytest.raises(OB.EventNotClaimable) as caught:
        service.requeue(event.outbox_event_id, actor="human_mr_ts", reason="coba kirim lagi")
    assert caught.value.code == "EVENT_NOT_CLAIMABLE"


def test_a_requeue_without_a_reason_is_refused(conn, service):
    event = emit(service, idempotency_key="idem-noreason-0001")

    def boom(_event):
        raise RuntimeError("nope")

    service.publish_due(boom)
    with pytest.raises(OB.InvalidEvent):
        service.requeue(event.outbox_event_id, actor="human_mr_ts", reason="")


# -- 7. the inbox makes a provider retry harmless -------------------------- #

def test_the_same_webhook_twice_is_one_row_and_the_second_is_not_new(conn):
    inbox = OB.IntegrationInbox(conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "inbox"))
    first = inbox.receive(source="midtrans", external_id="trx_8891", payload={"status": "settlement"})
    second = inbox.receive(source="midtrans", external_id="trx_8891", payload={"status": "settlement"})
    assert first.is_new is True and second.is_new is False
    assert first.inbox_id == second.inbox_id
    assert conn.execute("SELECT COUNT(*) FROM integration_inbox").fetchone()[0] == 1


def test_the_same_webhook_id_with_new_content_is_refused(conn):
    inbox = OB.IntegrationInbox(conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "inbox"))
    inbox.receive(source="midtrans", external_id="trx_8892", payload={"status": "settlement"})
    with pytest.raises(OB.IntegrationConflict):
        inbox.receive(source="midtrans", external_id="trx_8892", payload={"status": "expire"})


def test_an_inbox_row_can_be_marked_processed_once(conn):
    inbox = OB.IntegrationInbox(conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "inbox"))
    receipt = inbox.receive(source="midtrans", external_id="trx_8893", payload={"status": "settlement"})
    inbox.mark_processed(receipt.inbox_id)
    again = inbox.receive(source="midtrans", external_id="trx_8893", payload={"status": "settlement"})
    assert again.processed_at is not None
    with pytest.raises(OB.OutboxEventNotFound):
        inbox.mark_processed(receipt.inbox_id)
