"""Tests for the /outbox dashboard page (commit series: dashboard lanjutan).

Pins the page contract: the queue summary matches the outbox row counts, a
pending letter is visible with its attempt budget, a dead letter shows its last
error, the inbox exposes unprocessed work, another agency's events never
appear, the page renders for its three entitled roles only, and nothing leaks
raw guest PII even though payloads sometimes contain it.
"""

from __future__ import annotations

import re

import pytest

import dashboard as D
from outbox import OutboxService
from primitives import IdFactory, to_iso
from schema_loader import connect_sqlite

from test_dashboard import make_app, render


AGENCY = "agency_default"


# --------------------------------------------------------------------------- #
# Local fixtures (mirrors of test_dashboard's, which are not importable)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def seeded(clock):
    """An empty (schema-applied) database — the outbox page must render it."""
    return connect_sqlite(":memory:")


@pytest.fixture()
def seeded_app(seeded, clock):
    return make_app(seeded, clock)


def _open_page(app) -> str:
    return render(app, "outbox", "operations")


def _status_count(page: str, status: str) -> int:
    """Count rows in the status table whose first cell is ``status``.

    The table renders ``<tr><td>STATUS</td><td>n</td>…``; count that pair.
    """
    return len(
        re.findall(rf"<tr><td>{status}</td><td>(\d+)</td>", page)
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def outbox_app(seeded_app):
    """The seeded dashboard plus a working clock handle for emitting events."""
    return seeded_app


def _emit(app, clock, *, event_type="booking.shifted", key_suffix="a", **overrides):
    service = OutboxService(
        app.conn, clock=clock, ids=IdFactory(True, "obx"), agency_id=AGENCY
    )
    kwargs = dict(
        event_type=event_type,
        aggregate_type="booking",
        aggregate_id="book-0001",
        payload={"booking_id": "book-0001", "note": "shifted by guest"},
        actor="reservation-ops-1",
        idempotency_key=f"idem-outbox-{key_suffix}",
    )
    kwargs.update(overrides)
    return service.emit(**kwargs)


# --------------------------------------------------------------------------- #
# Empty database
# --------------------------------------------------------------------------- #
def test_outbox_empty_db_renders_every_section_with_notes(seeded_app):
    page = _open_page(seeded_app)
    assert "Outbox &amp; integrations" in page
    assert "empty_note" in page or "outbox event" in page
    # the note must say the page sends nothing — the send path is a worker, not a page
    assert "publish_due" in page
    assert "data-page=\"outbox\"" in page


def test_outbox_note_counts_zero_waiting_when_no_events(seeded_app):
    page = _open_page(seeded_app)
    assert "<strong>0</strong>" in page


# --------------------------------------------------------------------------- #
# Pending / published / dead letters
# --------------------------------------------------------------------------- #
def test_outbox_pending_event_is_listed_with_attempt_budget(seeded_app, clock):
    event = _emit(seeded_app, clock, key_suffix="pend-1")
    page = _open_page(seeded_app)
    assert event.outbox_event_id in page
    assert "booking.shifted" in page
    assert "0/5" in page  # attempts/max_attempts for a fresh letter
    assert _status_count(page, "PENDING") == 1


def test_outbox_published_event_shows_published_at(seeded_app, clock):
    service = OutboxService(
        seeded_app.conn, clock=clock, ids=IdFactory(True, "obx"), agency_id=AGENCY
    )
    event = _emit(seeded_app, clock, key_suffix="pub-1")
    service.publish_due(lambda _e: None)  # a publisher that always succeeds
    page = _open_page(seeded_app)
    assert "PUBLISHED" in page
    assert event.outbox_event_id in page


def test_outbox_dead_letter_shows_last_error(seeded_app, clock):
    conn = seeded_app.conn
    event = _emit(seeded_app, clock, key_suffix="dead-1")
    # simulate a delivery history: four attempts already burned, this one is the last
    conn.execute("UPDATE outbox_events SET attempts = 4 WHERE outbox_event_id = ?",
                 (event.outbox_event_id,))
    service = OutboxService(
        seeded_app.conn, clock=clock, ids=IdFactory(True, "obx"), agency_id=AGENCY,
    )

    def _explode(_event):
        raise RuntimeError("transport down")

    service.publish_due(_explode)
    page = _open_page(seeded_app)
    assert event.outbox_event_id in page
    assert "transport down" in page
    assert "DEAD_LETTER" in page


def test_outbox_replayed_idempotency_key_does_not_double_count(seeded_app, clock):
    _emit(seeded_app, clock, key_suffix="dup-1")
    _emit(seeded_app, clock, key_suffix="dup-1")  # same content → same letter
    page = _open_page(seeded_app)
    assert _status_count(page, "PENDING") == 1


# --------------------------------------------------------------------------- #
# Inbox
# --------------------------------------------------------------------------- #
def test_outbox_inbox_shows_unprocessed_work(seeded_app, clock):
    iso = to_iso(clock.now())
    conn = seeded_app.conn
    conn.execute(
        "INSERT INTO integration_inbox (inbox_id, agency_id, source, external_id,"
        " payload_sha256, received_at)"
        " VALUES ('inbox-1', ?, 'channel_gmail', 'ext-1',"
        " ?, ?)",
        (AGENCY, "a" * 64, iso),
    )
    page = _open_page(seeded_app)
    assert "channel_gmail" in page
    assert "<tr><td>channel_gmail</td><td>1</td><td>1</td>" in page  # received=1, unprocessed=1


# --------------------------------------------------------------------------- #
# Tenant scope + roles + PII
# --------------------------------------------------------------------------- #
def test_outbox_never_shows_another_agencys_events(seeded_app, clock):
    iso = to_iso(clock.now())
    conn = seeded_app.conn
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_other', 'Other Agency', 'ACTIVE', ?, ?)",
        (iso, iso),
    )
    other = OutboxService(conn, clock=clock, ids=IdFactory(True, "obx"),
                          agency_id="agency_other")
    other.emit(
        event_type="booking.shifted",
        aggregate_type="booking",
        aggregate_id="book-other-1",
        payload={"note": "other tenant letter"},
        actor="reservation-ops-1",
        idempotency_key="idem-other-outbox1",
    )
    page = _open_page(seeded_app)
    assert "book-other-1" not in page
    assert _status_count(page, "PENDING") == 0


def test_outbox_dead_letter_payload_text_is_not_the_guest_copy(seeded_app, clock):
    """A payload may carry free text; the page shows only the recorded error."""
    conn = seeded_app.conn
    service = OutboxService(
        seeded_app.conn, clock=clock, ids=IdFactory(True, "obx"), agency_id=AGENCY,
    )
    event = service.emit(
        event_type="booking.shifted",
        aggregate_type="booking",
        aggregate_id="book-0001",
        payload={"guest_email": "hidden@example.com"},
        actor="reservation-ops-1",
        idempotency_key="idem-payload-001",
    )
    conn.execute("UPDATE outbox_events SET attempts = 4 WHERE outbox_event_id = ?",
                 (event.outbox_event_id,))

    def _explode(_event):
        raise RuntimeError("driver refused")

    service.publish_due(_explode)
    page = _open_page(seeded_app)
    assert "hidden@example.com" not in page  # payloads are never rendered
    assert "driver refused" in page


@pytest.mark.parametrize("role", ["operations", "owner", "auditor"])
def test_outbox_renders_for_entitled_roles(seeded_app, role):
    page = render(seeded_app, "outbox", role)
    assert "Outbox &amp; integrations" in page


@pytest.mark.parametrize("role", ["reservation", "finance"])
def test_outbox_refused_for_unentitled_roles(seeded_app, role):
    with pytest.raises(D.Forbidden):
        render(seeded_app, "outbox", role)


def test_outbox_route_is_registered_for_dump(seeded, clock, tmp_path):
    """dump_all walks NAV; the outbox page must be part of an owner dump."""
    import dashboard as dash

    written = dash.dump_all(
        tmp_path / "dump", conn=seeded, clock=clock,
        actor="owner-sakti-1", actor_role="owner",
    )
    names = {path.name for path in written}
    assert "outbox.html" in names
