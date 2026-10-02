"""The exception engine: routing, counting, and the clock a failure starts.

The tests below are written from the failure's point of view, because that is the module's
whole job: given something already went wrong, prove that the right obligation was created,
exactly once, with a report that survives a crash.
"""

from __future__ import annotations

import pytest

import exception_engine as EE
import incidents as INC
import outbox as OB
from primitives import FrozenClock, IdFactory

NOW = "2026-10-02T08:00:00+00:00"
AGENCY = "agency_default"
WINDOW = EE.DEFAULT_DEDUPE_WINDOW_MINUTES


@pytest.fixture()
def engine(conn):
    return EE.ExceptionEngine(
        conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "exceptions"), agency_id=AGENCY
    )


def capture(engine, **overrides):
    args = dict(
        category="PAYMENT_UNRECONCILED",
        code="MATCH_TIMEOUT",
        message="rekonsiliasi pembayaran waktu habis tanpa pasangan",
        source="job.reconcile",
        aggregate_id="pay_0007",
    )
    args.update(overrides)
    return engine.capture(**args)


def at(moment: str):
    return FrozenClock.at(moment).now()


# -- 1. severity is looked up, never guessed ------------------------------- #

def test_a_category_with_no_route_is_refused(conn, engine):
    """An unknown failure must not acquire a severity by accident."""
    with pytest.raises(EE.UnknownFailureCategory) as caught:
        capture(engine, category="SEMACAM_MASALAH")
    assert caught.value.code == "UNKNOWN_FAILURE_CATEGORY"
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_every_route_names_a_playbook_that_exists(conn):
    """A route pointing at a playbook nobody wrote would fail at the worst moment."""
    for category, route in EE.FAILURE_ROUTES.items():
        assert route.playbook in INC.PLAYBOOK_CODES, category
        assert route.severity in INC.SEVERITIES, category


def test_the_capture_opens_an_incident_with_the_routes_severity(conn, engine):
    result = capture(engine)
    assert result.severity == "SEV2"
    assert result.playbook == "PAYMENT_MISMATCH"
    assert result.is_new_incident is True
    row = conn.execute("SELECT severity, playbook, source, status FROM incidents").fetchone()
    assert row["severity"] == "SEV2"
    assert row["playbook"] == "PAYMENT_MISMATCH"
    assert row["source"] == "job.reconcile"
    assert row["status"] == "OPEN"


def test_a_data_exposure_starts_the_regulator_clock_and_player_cannot_opt_out(conn, engine):
    """SEV1 + personal data = the 72-hour notice. A caller cannot lower it, only raise it."""
    result = capture(
        engine,
        category="DATA_EXPOSURE",
        code="EXPORT_MISROUTE",
        message="ekspor P2 terkirim ke alamat yang salah",
        source="gate.outbound",
        personal_data_involved=False,  # the caller tries to wave it away
    )
    assert result.personal_data_involved is True
    row = conn.execute(
        "SELECT severity, regulatory_notification_required, notice_due_at, detected_at FROM incidents"
    ).fetchone()
    assert row["severity"] == "SEV1"
    assert int(row["regulatory_notification_required"]) == 1
    assert row["notice_due_at"] == "2026-10-05T08:00:00+00:00"  # detected + 72 hours


def test_a_caller_may_raise_the_personal_data_flag(conn, engine):
    result = capture(
        engine,
        category="PROVIDER_OUTAGE",
        code="PSP_5XX",
        message="provider pembayaran menjawab 500 berulang",
        source="connector.psp",
        personal_data_involved=True,
    )
    assert result.personal_data_involved is True


# -- 2. the report is a record, not a callback ----------------------------- #

def test_the_capture_writes_an_outbox_event_tied_to_the_incident(conn, engine):
    result = capture(engine)
    event = OB.OutboxService(conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "o"), agency_id=AGENCY).get(
        result.outbox_event_id
    )
    assert event.event_type == EE.CAPTURE_EVENT_TYPE
    assert event.aggregate_type == "incident"
    assert event.aggregate_id == result.incident_id
    assert event.correlation_id == result.incident_id
    assert event.payload_json["severity"] == "SEV2"
    assert event.payload_json["occurrences"] == 1


# -- 3. one incident per failure, and a count that survives ---------------- #

def test_the_same_failure_inside_the_window_counts_instead_of_reopening(conn, engine):
    first = capture(engine)
    second = capture(engine, at=at("2026-10-02T08:05:00+00:00"))
    assert second.is_new_incident is False
    assert second.incident_id == first.incident_id
    assert second.occurrences == 2
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
    assert engine.occurrences(first.dedupe_key) == 2
    # every arrival still leaves a record: the report is not batched away
    assert conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 2


def test_the_same_failure_after_the_window_is_a_new_obligation(conn, engine):
    first = capture(engine)
    later = capture(engine, at=at("2026-10-02T09:30:00+00:00"))  # 90 minutes later
    assert later.is_new_incident is True
    assert later.incident_id != first.incident_id
    assert later.occurrences == 1
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 2
    assert engine.occurrences(first.dedupe_key) == 1


def test_a_different_code_on_the_same_aggregate_is_a_different_failure(conn, engine):
    first = capture(engine)
    second = capture(engine, code="AMOUNT_MISMATCH")
    assert second.dedupe_key != first.dedupe_key
    assert second.is_new_incident is True
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 2


def test_the_window_rolls_with_each_arrival(conn, engine):
    """The window is measured from the last arrival: a continuing loop stays one incident.

    At exactly the boundary it is still the same obligation (inclusive, like the incident
    clock in incidents.py); if the failure keeps arriving, the count keeps growing rather than
    opening an incident a minute. Only a real quiet spell makes the next arrival news.
    """
    first = capture(engine)
    at_boundary = capture(engine, at=at("2026-10-02T08:30:00+00:00"))
    assert at_boundary.is_new_incident is False
    still_going = capture(engine, at=at("2026-10-02T08:31:00+00:00"))
    assert still_going.is_new_incident is False
    assert engine.occurrences(first.dedupe_key) == 3
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1

    after_quiet_spell = capture(engine, at=at("2026-10-02T09:30:00+00:00"))
    assert after_quiet_spell.is_new_incident is True
    assert engine.occurrences(after_quiet_spell.dedupe_key) == 1
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 2


def test_fingerprints_are_scoped_to_their_agency(conn, engine):
    capture(engine)
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_dua', 'Agency Dua', 'ACTIVE', ?, ?)",
        (NOW, NOW),
    )
    other = EE.ExceptionEngine(
        conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "other"), agency_id="agency_dua"
    )
    theirs = capture(other)
    assert theirs.is_new_incident is True
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 2


# -- 4. input hygiene ------------------------------------------------------ #

def test_a_failure_without_a_readable_message_is_refused(conn, engine):
    with pytest.raises(EE.InvalidFailure):
        capture(engine, message="pendek")
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_a_window_smaller_than_one_minute_is_refused(conn):
    with pytest.raises(EE.InvalidFailure):
        EE.ExceptionEngine(conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "x"), dedupe_window_minutes=0)
