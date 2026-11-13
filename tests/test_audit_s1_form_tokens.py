"""Audit 2026-11-13, S1: the public inquiry form only accepts tokens it signed.

Before the fix any 16-64 hex string passed, so a script could post to /inquiry
without loading the page. Tokens are now ``issued_at | nonce | HMAC`` and must be
between three seconds and two hours old.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

import marketing_site as M
from form_token_support import TEST_FORM_TOKEN_KEY, signed_token
from primitives import IdFactory
from schema_loader import connect_sqlite


def _site(clock):
    conn = connect_sqlite(":memory:")
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_default', 'Default', 'ACTIVE', '2026-09-21T08:00:00+00:00',"
        " '2026-09-21T08:00:00+00:00') ON CONFLICT(agency_id) DO NOTHING"
    )
    return conn, M.MarketingSite(conn, clock=clock, ids=IdFactory(seed="s1"), agency_id="agency_default")


def _submit(site, clock, token):
    return site.intake.submit(
        form={
            "name": "Budi Santoso",
            "email": "budi@example.com",
            "message": "We are two families looking for a villa near Ubud in July.",
            "form_token": token,
            M.HONEYPOT_FIELD: "",
            "property_ref": "",
        },
        remote_addr="203.0.113.70",
        rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
        expected_origin_host=None,
        origin_header=None,
    )


def _cases(conn):
    return conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]


def test_signed_token_in_window_is_accepted(clock):
    conn, site = _site(clock)
    assert _submit(site, clock, signed_token())["reference"]
    assert _cases(conn) == 1


def test_unsigned_random_hex_is_refused(clock):
    conn, site = _site(clock)
    with pytest.raises(M.FormInvalid):
        _submit(site, clock, uuid4().hex)
    assert _cases(conn) == 0


def test_tampered_mac_is_refused(clock):
    conn, site = _site(clock)
    token = signed_token()
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(M.FormInvalid):
        _submit(site, clock, tampered)
    assert _cases(conn) == 0


def test_backdated_timestamp_without_key_is_refused(clock):
    """Changing the issued-at prefix breaks the MAC: age cannot be forged."""
    conn, site = _site(clock)
    token = signed_token(seconds_before_now=10 * 60 * 60)
    forged = f"{int(clock.now().timestamp()) - 60:010x}" + token[10:]
    with pytest.raises(M.FormInvalid):
        _submit(site, clock, forged)
    assert _cases(conn) == 0


def test_instant_submission_is_refused(clock):
    conn, site = _site(clock)
    with pytest.raises(M.FormInvalid):
        _submit(site, clock, signed_token(seconds_before_now=1))
    assert _cases(conn) == 0


def test_stale_form_is_refused(clock):
    conn, site = _site(clock)
    with pytest.raises(M.FormInvalid):
        _submit(site, clock, signed_token(seconds_before_now=M.FORM_TOKEN_MAX_AGE_SECONDS + 1))
    assert _cases(conn) == 0


def test_token_from_another_key_is_refused(clock):
    conn, _site_default = _site(clock)
    other = M.MarketingSite(conn, clock=clock, ids=IdFactory(seed="s1b"), agency_id="agency_default",
                            form_token_key="another-test-key-that-is-long-enough-000")
    with pytest.raises(M.FormInvalid):
        _submit(other, clock, signed_token())
    assert _cases(conn) == 0


def test_rendered_form_carries_a_verifiable_token(clock):
    _conn, site = _site(clock)
    html = site.inquiry_form_html()
    token = html.split('name="form_token" value="', 1)[1].split('"', 1)[0]
    clock.advance(seconds=30)
    site.form_tokens.verify(token)  # does not raise


def test_short_key_is_refused():
    with pytest.raises(ValueError):
        M.FormTokens(None, key="too-short")


def test_key_comes_from_environment(clock):
    tokens = M.FormTokens(clock, env={M.FORM_TOKEN_KEY_ENV_VAR: TEST_FORM_TOKEN_KEY})
    token = tokens.issue(issued_at=clock.now() - timedelta(seconds=60))
    assert token == token.lower() and len(token) == 58
    tokens.verify(token)
