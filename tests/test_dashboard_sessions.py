"""Session tokens (ADR-0005) on the dashboard, beside the static machine tokens.

The dashboard used to accept only one static token per role. These tests pin the
new path: a session minted by ``identity.SessionService`` authenticates as the
page role its canonical role maps to, expires on the idle deadline, stops working
the moment it is revoked, and can never widen a role. The static tokens must keep
working unchanged — they are the automation credential, not a fallback.
"""

from __future__ import annotations

import pytest

import dashboard as D
import identity as I
import totp as T
from primitives import FrozenClock

SECRET = "JBSWY3DPEHPK3PXP"
STATIC_TOKEN = "static-owner-token"


def make_dashboard(conn, clock: FrozenClock, **kwargs) -> D.Dashboard:
    return D.Dashboard(
        conn,
        clock=clock,
        actor="ops-1",
        actor_role="owner",
        tokens={STATIC_TOKEN: ("ops-1", "owner")},
        **kwargs,
    )


def service(conn, clock: FrozenClock, identity: str = "alice") -> I.SessionService:
    return I.SessionService(
        conn, clock=clock, secrets=I.MappingSecretResolver({identity: SECRET})
    )


def enroll_active(conn, clock: FrozenClock, identity: str = "alice") -> None:
    svc = service(conn, clock, identity)
    svc.enroll_totp(identity, secret_ref=identity)
    step = int(clock.now().timestamp()) // T.DEFAULT_STEP_SECONDS
    svc.confirm_totp(identity, T.hotp(SECRET, step))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_canonical_roles_map_to_the_six_page_roles():
    assert dict(D.PAGE_ROLE_BY_CANONICAL) == {
        "owner": "owner",
        "gm": "gm",
        "finance_manager": "finance",
        "reservation_manager": "operations",
        "reservation_agent": "reservation",
        "auditor": "auditor",
    }


def test_session_authenticates_as_its_page_role(conn, clock):
    token = service(conn, clock).open_session("bob", "auditor").token
    dashboard = make_dashboard(conn, clock)

    principal = dashboard.authenticate(bearer(token))

    assert principal.identity == "bob"
    assert principal.role == "auditor"
    assert dashboard.page_allowed("audit", principal.role) is True
    assert dashboard.page_allowed("finance", principal.role) is False
    assert principal.may_write is False


def test_mfa_role_session_maps_to_owner(conn, clock):
    enroll_active(conn, clock)
    step = int(clock.now().timestamp()) // T.DEFAULT_STEP_SECONDS
    token = service(conn, clock).open_session(
        "alice", "owner", mfa_code=T.hotp(SECRET, step + 1)
    ).token

    principal = make_dashboard(conn, clock).authenticate(bearer(token))

    assert (principal.identity, principal.role) == ("alice", "owner")
    assert principal.may_write is True
    assert principal.approval_role == "owner"


def test_owner_session_without_mfa_never_exists(conn, clock):
    enroll_active(conn, clock)
    with pytest.raises(I.MFARequired):
        service(conn, clock).open_session("alice", "owner")
    assert conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0


def test_revoked_session_is_refused(conn, clock):
    svc = service(conn, clock)
    result = svc.open_session("bob", "auditor")
    dashboard = make_dashboard(conn, clock)
    assert dashboard.authenticate(bearer(result.token)).role == "auditor"

    svc.revoke(result.session.session_id, reason="test")

    with pytest.raises(D.Unauthenticated):
        dashboard.authenticate(bearer(result.token))


def test_idle_deadline_ends_the_session(conn, clock):
    token = service(conn, clock).open_session("bob", "auditor").token
    dashboard = make_dashboard(conn, clock)
    dashboard.authenticate(bearer(token))

    clock.advance(minutes=31)

    with pytest.raises(D.Unauthenticated):
        dashboard.authenticate(bearer(token))


def test_absolute_deadline_survives_a_busy_session(conn, clock):
    token = service(conn, clock).open_session("bob", "auditor").token
    dashboard = make_dashboard(conn, clock)

    for _ in range(6):  # keep touching the idle deadline
        clock.advance(minutes=25)
        dashboard.authenticate(bearer(token))

    clock.set("2026-09-22T09:00:00Z")  # past the 12 h absolute deadline
    with pytest.raises(D.Unauthenticated):
        dashboard.authenticate(bearer(token))


def test_canonical_role_without_a_page_set_is_refused(conn, clock):
    token = service(conn, clock).open_session("root", "system_admin").token
    with pytest.raises(D.Unauthenticated):
        make_dashboard(conn, clock).authenticate(bearer(token))


def test_static_token_still_authenticates(conn, clock):
    principal = make_dashboard(conn, clock).authenticate(bearer(STATIC_TOKEN))
    assert (principal.identity, principal.role) == ("ops-1", "owner")


def test_unknown_token_is_refused(conn, clock):
    with pytest.raises(D.Unauthenticated):
        make_dashboard(conn, clock).authenticate(bearer("token-palsu"))


def test_login_is_recorded_as_evidence(conn, clock):
    service(conn, clock).open_session("bob", "auditor")
    rows = conn.execute(
        "SELECT identity, kind FROM auth_events WHERE identity = 'bob'"
    ).fetchall()
    assert [(row["identity"], row["kind"]) for row in rows] == [("bob", "LOGIN")]
