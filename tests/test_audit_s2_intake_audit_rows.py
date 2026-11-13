"""Audit 2026-11-13, S2: a guest and a case are born *with* their audit row.

Before the fix the website could create a guest record and open a case without one
line in the hash-chained ``audit_events`` log, so the chain could not answer "who
created this guest record, and when". Both rows now land in the same transaction as
the record, carry no identifier, and keep the chain verifiable.
"""

from __future__ import annotations

import json

import pytest

import marketing_site as M
from audit import AuditLog
from crm import CrmService
from form_token_support import signed_token
from primitives import IdFactory
from schema_loader import connect_sqlite


@pytest.fixture()
def site(clock):
    conn = connect_sqlite(":memory:")
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_default', 'Default', 'ACTIVE', '2026-09-21T08:00:00+00:00',"
        " '2026-09-21T08:00:00+00:00') ON CONFLICT(agency_id) DO NOTHING"
    )
    return conn, M.MarketingSite(conn, clock=clock, ids=IdFactory(seed="s2"), agency_id="agency_default")


def _submit(site, clock, email="sari@example.com"):
    return site.intake.submit(
        form={"name": "Sari Dewi", "email": email,
              "message": "Two adults, five nights in Canggu, early December please.",
              "form_token": signed_token(), M.HONEYPOT_FIELD: "", "property_ref": ""},
        remote_addr="203.0.113.80",
        rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
        expected_origin_host=None, origin_header=None,
    )


def _rows(conn, action):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit_events WHERE action = ? ORDER BY seq", (action,))]


def test_website_inquiry_writes_guest_and_case_audit_rows(site, clock):
    conn, s = site
    result = _submit(s, clock)
    guests = _rows(conn, "guest.registered")
    cases = _rows(conn, "case.opened")
    assert [g["entity_id"] for g in guests] == [result["guest_id"]]
    assert [c["entity_id"] for c in cases] == [result["case_id"]]
    assert guests[0]["actor"] == "system:crm"
    after = json.loads(cases[0]["after_state"])
    assert after["status"] == "NEW" and after["channel"] == "website"


def test_audit_rows_carry_no_contact_details(site, clock):
    conn, s = site
    _submit(s, clock)
    blob = " ".join(
        str(v) for r in _rows(conn, "guest.registered") + _rows(conn, "case.opened")
        for v in r.values() if v is not None
    )
    for needle in ("sari@example.com", "Sari Dewi", "Canggu"):
        assert needle not in blob
    after = json.loads(_rows(conn, "guest.registered")[0]["after_state"])
    assert after["mail_on_file"] is True and after["tel_on_file"] is False


def test_returning_guest_gets_a_case_row_but_no_second_guest_row(site, clock):
    conn, s = site
    _submit(s, clock)
    _submit(s, clock)  # same email, fresh token: a second case for the same guest
    assert len(_rows(conn, "guest.registered")) == 1
    assert len(_rows(conn, "case.opened")) == 2


def test_chain_still_verifies(site, clock):
    conn, s = site
    _submit(s, clock, "a@example.com")
    _submit(s, clock, "b@example.com")
    report = AuditLog(conn, clock=clock, ids=IdFactory(seed="v")).verify()  # raises if broken
    assert report.count == conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] >= 4


def test_failed_audit_append_rolls_back_the_guest(clock, monkeypatch):
    """Same transaction: if the audit row cannot be written, the guest is not either."""
    import crm as crm_module

    conn = connect_sqlite(":memory:")
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_default', 'Default', 'ACTIVE', '2026-09-21T08:00:00+00:00',"
        " '2026-09-21T08:00:00+00:00') ON CONFLICT(agency_id) DO NOTHING"
    )
    service = CrmService(conn, clock=clock, ids=IdFactory(seed="s2r"), agency_id="agency_default")

    def boom(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(crm_module, "append_event", boom)
    with pytest.raises(RuntimeError):
        service.register_guest("Rina Putri", email="rina@example.com")
    assert conn.execute("SELECT COUNT(*) FROM guests").fetchone()[0] == 0
