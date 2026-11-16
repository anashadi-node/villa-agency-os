"""Crash resilience of the outbox publisher (§19 Resilience, ADR-0007).

``test_outbox.py`` proves the outbox by its *refusals*: a transport that raises, a retry cap,
a dead letter. What it does not exercise is the worker dying. Two moments matter:

* the process dies *inside* ``publish`` (the letter may or may not have left the building);
* the process dies *between* the transport succeeding and the outcome being written.

The contract that survives both is **at-least-once with a stable identity**: the event is
never lost, never half-recorded, and every redelivery carries the same event id and
idempotency key so the receiver can discard the duplicate. These tests state that contract.
Each one that guards a mechanism has a paired negative control that removes the mechanism
and shows the same assertion failing - a check that cannot fail proves nothing.
"""

from __future__ import annotations

import pytest

import outbox as OB
from primitives import FrozenClock, IdFactory

NOW = "2026-10-02T08:00:00+00:00"
AGENCY = "agency_default"


@pytest.fixture()
def service(conn):
    return OB.OutboxService(
        conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "outbox"), agency_id=AGENCY
    )


def _emit(service, key="idem-crash-0001"):
    return service.emit(
        event_type="hold.expiring",
        aggregate_type="hold",
        aggregate_id="hold_0001",
        payload={"minutes_left": 15, "unit_code": "UB-01"},
        actor="service:ai_reservation",
        idempotency_key=key,
    )


def _delivery_count(conn, event_id):
    return conn.execute(
        "SELECT COUNT(*) FROM outbox_deliveries WHERE outbox_event_id = ?", (event_id,)
    ).fetchone()[0]


class _FailOnDeliveryId:
    """Id factory that raises when the delivery row id is requested, and is otherwise real."""

    def __init__(self, real):
        self._real = real

    def new(self, prefix):
        if prefix == "dlv":
            raise RuntimeError("disk full while writing the delivery log")
        return self._real.new(prefix)


def _crash_while_recording_success(conn, service):
    """Transport succeeds; the outcome write dies half way (after the UPDATE, at the INSERT)."""
    event = _emit(service)
    service._ids = _FailOnDeliveryId(service._ids)
    with pytest.raises(RuntimeError, match="disk full"):
        service.publish_due(lambda ev: None)
    return event


# -- 1. a process that dies inside publish loses nothing ------------------------------ #

def test_a_worker_killed_inside_publish_leaves_the_event_due(conn, service):
    event = _emit(service)

    def killed(_event):
        raise KeyboardInterrupt  # BaseException: the worker is stopped, not "a failed send"

    with pytest.raises(KeyboardInterrupt):
        service.publish_due(killed)

    stored = service.get(event.outbox_event_id)
    assert stored.status == OB.PENDING, "a killed worker must not demote or lose the event"
    assert stored.attempts == 0, "no outcome was observed, so no attempt may be recorded"
    assert _delivery_count(conn, event.outbox_event_id) == 0
    assert [e.outbox_event_id for e in service.pending()] == [event.outbox_event_id]


def test_the_redelivery_after_a_crash_carries_the_same_identity(conn, service):
    """At-least-once is only safe if the receiver can recognise the repeat."""
    event = _emit(service)
    seen: list[tuple[str, str]] = []

    def record_then_die(ev):
        seen.append((ev.outbox_event_id, ev.idempotency_key))
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        service.publish_due(record_then_die)

    service.publish_due(lambda ev: seen.append((ev.outbox_event_id, ev.idempotency_key)))

    assert len(seen) == 2, "the letter is delivered again after the crash (at-least-once)"
    assert seen[0] == seen[1] == (event.outbox_event_id, "idem-crash-0001")
    assert service.get(event.outbox_event_id).status == OB.PUBLISHED


# -- 2. an outcome write that dies half way leaves no half-record --------------------- #

def test_a_crash_while_recording_success_rolls_the_whole_outcome_back(conn, service):
    event = _crash_while_recording_success(conn, service)

    stored = service.get(event.outbox_event_id)
    assert stored.status == OB.PENDING, "PUBLISHED without a delivery row would be a lie"
    assert stored.published_at is None
    assert stored.attempts == 0
    assert _delivery_count(conn, event.outbox_event_id) == 0
    assert conn.in_transaction is False, "the failed write must not leak an open transaction"


def test_after_that_crash_the_next_run_completes_with_exactly_one_delivery_row(conn, service):
    event = _crash_while_recording_success(conn, service)
    service._ids = service._ids._real  # the disk came back

    report = service.publish_due(lambda ev: None)

    assert report.published == (event.outbox_event_id,)
    assert service.get(event.outbox_event_id).status == OB.PUBLISHED
    assert _delivery_count(conn, event.outbox_event_id) == 1


def test_negative_control_without_the_transaction_the_half_record_appears(
    conn, service, monkeypatch
):
    """Prove the rollback test above can fail: swap the transaction for a bare ``yield``."""
    from contextlib import contextmanager

    @contextmanager
    def no_transaction(_conn):
        yield

    monkeypatch.setattr(OB, "_write_transaction", no_transaction)
    event = _crash_while_recording_success(conn, service)

    stored = service.get(event.outbox_event_id)
    half_record = (
        stored.status == OB.PUBLISHED and _delivery_count(conn, event.outbox_event_id) == 0
    )
    assert half_record, "sabotage must reproduce the half record, or the guard above is blind"


# -- 3. a repeat run does not re-send what was already confirmed ---------------------- #

def test_repeated_publisher_runs_send_a_confirmed_event_exactly_once(conn, service):
    event = _emit(service)
    sent: list[str] = []

    for _ in range(3):
        service.publish_due(lambda ev: sent.append(ev.outbox_event_id))

    assert sent == [event.outbox_event_id]
    assert _delivery_count(conn, event.outbox_event_id) == 1


def test_negative_control_a_pending_that_ignored_status_would_resend(conn, service, monkeypatch):
    """The 'exactly once' assertion has teeth: if ``pending`` ignored status, it fails."""
    event = _emit(service)

    def every_event(self, *, limit=None, at=None):
        rows = self._conn.execute(
            f"SELECT {OB._EVENT_COLUMNS} FROM outbox_events WHERE agency_id = ?", (self.agency_id,)
        ).fetchall()
        return tuple(OB._event(r) for r in rows)

    monkeypatch.setattr(OB.OutboxService, "pending", every_event)
    sent: list[str] = []
    for _ in range(3):
        service.publish_due(lambda ev: sent.append(ev.outbox_event_id))

    assert sent == [event.outbox_event_id] * 3, "sabotage must cause re-sends, or the guard is blind"
