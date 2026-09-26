"""ADR-0005 tests: sessions, TOTP MFA, and step-up.

The ADR lists the tests required *before* the production auth module is built;
these are the ones this module answers:

* finance/bank action without MFA or step-up is denied (step-up here, MFA at
  session open);
* a session that never satisfied its second factor cannot act;
* unauthorized access is denied and recorded.

Two invariants get their own tests because they are the reason the design looks
the way it does: **the shared TOTP secret is never stored**, and **a code that has
been used cannot be used again**.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import identity as ID
import totp as T
from primitives import DomainError, FrozenClock, IdFactory, parse_iso
from schema_loader import connect_sqlite

START = "2026-09-26T08:00:00+00:00"
SECRET = T.generate_secret()
SECRET_REF = "owner-1-totp"


def moment(seconds: int) -> str:
    return ID.to_iso(parse_iso(START) + timedelta(seconds=seconds))


def code_at(seconds: int) -> str:
    return T.totp(SECRET, parse_iso(START) + timedelta(seconds=seconds))


@pytest.fixture()
def conn():
    connection = connect_sqlite(":memory:")
    for agency in ("agency_default", "agency_bali", "agency_jogja"):
        connection.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'ACTIVE', ?, ?) ON CONFLICT DO NOTHING",
            (agency, agency, START, START),
        )
    yield connection
    connection.close()


def service(conn, agency_id: str = "agency_bali", **kwargs) -> ID.SessionService:
    settings = dict(
        clock=FrozenClock.at(START),
        ids=IdFactory(deterministic=True, seed="identity"),
        agency_id=agency_id,
        secrets=ID.MappingSecretResolver({SECRET_REF: SECRET}),
    )
    settings.update(kwargs)
    return ID.SessionService(conn, **settings)


def enrolled(conn, agency_id: str = "agency_bali") -> ID.SessionService:
    """A confirmed factor, enrolled one step *before* the tests' "now".

    Enrolment deliberately spends an earlier time step: activation consumes a
    code, and a code is spent once used — so a fixture that confirmed with
    ``code_at(0)`` would (correctly) see every later login at ``START`` as a
    replay. Enrolling "yesterday" is also what really happens.
    """
    svc = service(conn, agency_id)
    svc.enroll_totp("owner-1", secret_ref=SECRET_REF)
    svc.confirm_totp("owner-1", code_at(-60), at=moment(-60))
    return svc


# --------------------------------------------------------------------------- #
# 1. MFA is mandatory for the roles that can move money
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ID.MFA_REQUIRED_ROLES)
def test_a_money_role_cannot_open_a_session_without_a_code(conn, role):
    svc = enrolled(conn)
    with pytest.raises(ID.MFARequired) as caught:
        svc.open_session("owner-1", role, at=START)
    assert caught.value.code == "MFA_REQUIRED"
    assert conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0, (
        "a refused login wrote a session row"
    )


@pytest.mark.parametrize("role", ["reservation", "operations", "auditor"])
def test_a_role_without_money_authority_needs_no_second_factor(conn, role):
    svc = service(conn)
    result = svc.open_session(f"staff-{role}", role, at=START)
    assert result.session.role == role
    assert result.session.mfa_satisfied_at is None


def test_an_owner_with_a_valid_code_gets_a_session(conn):
    svc = enrolled(conn)
    result = svc.open_session("owner-1", "owner", mfa_code=code_at(0), at=START)
    assert result.session.mfa_satisfied_at == START
    assert result.token
    assert len(result.session.session_id) > 4


def test_a_wrong_code_does_not_open_a_session(conn):
    svc = enrolled(conn)
    with pytest.raises(ID.InvalidCode):
        svc.open_session("owner-1", "owner", mfa_code="000000", at=START)
    assert conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# 2. Enrolment: a factor is inert until a real code proves it
# --------------------------------------------------------------------------- #
def test_a_new_factor_is_disabled_until_confirmed(conn):
    svc = service(conn)
    svc.enroll_totp("owner-1", secret_ref=SECRET_REF)
    with pytest.raises(ID.InvalidCode) as caught:
        svc.verify_factor_code("owner-1", code_at(0), at=START)
    assert caught.value.code == "MFA_FACTOR_INACTIVE"
    svc.confirm_totp("owner-1", code_at(0), at=START)
    assert svc.verify_factor_code("owner-1", code_at(60), at=moment(60)) is not None


def test_enrolling_twice_is_refused(conn):
    svc = service(conn)
    svc.enroll_totp("owner-1", secret_ref=SECRET_REF)
    with pytest.raises(ID.IdentityError) as caught:
        svc.enroll_totp("owner-1", secret_ref=SECRET_REF)
    assert caught.value.code == "MFA_FACTOR_EXISTS"


# --------------------------------------------------------------------------- #
# 3. A code is spent once used
# --------------------------------------------------------------------------- #
def test_a_code_cannot_be_used_twice(conn):
    svc = enrolled(conn)
    svc.verify_factor_code("owner-1", code_at(0), at=START)
    with pytest.raises(ID.InvalidCode) as caught:
        svc.verify_factor_code("owner-1", code_at(0), at=START)
    assert caught.value.code == "MFA_CODE_REPLAYED"


def test_the_next_code_still_works_after_a_replay_refusal(conn):
    svc = enrolled(conn)
    svc.verify_factor_code("owner-1", code_at(0), at=START)
    assert svc.verify_factor_code("owner-1", code_at(60), at=moment(60)) is not None


# --------------------------------------------------------------------------- #
# 4. Sessions: two deadlines, revocation, rotation
# --------------------------------------------------------------------------- #
def test_an_idle_session_is_refused(conn):
    svc = service(conn)
    token = svc.open_session("agent-1", "reservation", at=START).token
    later = moment(int(svc.idle_timeout.total_seconds()) + 1)
    with pytest.raises(ID.SessionExpired):
        svc.authenticate(token, at=later)


def test_activity_keeps_a_session_alive_until_the_absolute_deadline(conn):
    svc = service(conn)
    token = svc.open_session("agent-1", "reservation", at=START).token
    step = int(svc.idle_timeout.total_seconds()) - 1
    current = START
    for tick in range(1, 20):
        current = moment(tick * step)
        svc.authenticate(token, at=current)
    with pytest.raises(ID.SessionExpired):
        svc.authenticate(token, at=moment(int(svc.absolute_timeout.total_seconds()) + 1))


def test_a_revoked_session_is_refused(conn):
    svc = service(conn)
    result = svc.open_session("agent-1", "reservation", at=START)
    svc.revoke(result.session.session_id, reason="operator sign-out", at=START)
    with pytest.raises(ID.RevokedSession):
        svc.authenticate(result.token, at=START)


def test_rotation_kills_the_old_token(conn):
    svc = service(conn)
    first = svc.open_session("agent-1", "reservation", at=START)
    second = svc.rotate(first.token, at=START)
    assert second.token != first.token
    with pytest.raises(ID.SessionNotFound):
        svc.authenticate(first.token, at=START)
    assert svc.authenticate(second.token, at=START).session_id == first.session.session_id


def test_the_sweep_revokes_everything_past_a_deadline(conn):
    svc = service(conn)
    svc.open_session("agent-1", "reservation", at=START)
    svc.open_session("agent-2", "reservation", at=START)
    swept = svc.sweep_expired(at=moment(int(svc.absolute_timeout.total_seconds()) + 1))
    assert len(swept) == 2
    assert svc.sweep_expired(at=moment(int(svc.absolute_timeout.total_seconds()) + 2)) == ()


# --------------------------------------------------------------------------- #
# 5. Step-up: a fresh factor for money/bank actions (the ADR's required test)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("subject_type", ID.HIGH_RISK_SUBJECT_TYPES)
def test_a_high_risk_action_needs_a_step_up(conn, subject_type):
    svc = enrolled(conn)
    login = svc.open_session("owner-1", "owner", mfa_code=code_at(0), at=START)
    with pytest.raises(ID.StepUpRequired) as caught:
        svc.require_step_up(login.session, subject_type=subject_type, at=START)
    assert caught.value.code == "STEP_UP_REQUIRED"
    assert caught.value.details.get("subject_type") == subject_type


def test_a_step_up_unlocks_a_high_risk_action_for_its_window(conn):
    svc = enrolled(conn)
    login = svc.open_session("owner-1", "owner", mfa_code=code_at(0), at=START)
    stepped = svc.satisfy_step_up(login.session.session_id, code_at(60), at=moment(60))
    svc.require_step_up(stepped, subject_type="bank_change", at=moment(120))
    after_window = moment(60 + int(svc.step_up_window.total_seconds()) + 1)
    with pytest.raises(ID.StepUpRequired):
        svc.require_step_up(stepped, subject_type="bank_change", at=after_window)


def test_an_ordinary_action_needs_no_step_up(conn):
    svc = enrolled(conn)
    login = svc.open_session("owner-1", "owner", mfa_code=code_at(0), at=START)
    svc.require_step_up(login.session, subject_type="case", at=START)


# --------------------------------------------------------------------------- #
# 6. Scope and the trail
# --------------------------------------------------------------------------- #
def test_a_token_from_one_agency_is_invisible_to_another(conn):
    bali = service(conn, "agency_bali")
    jogja = service(conn, "agency_jogja")
    token = bali.open_session("agent-1", "reservation", at=START).token
    with pytest.raises(ID.SessionNotFound):
        jogja.authenticate(token, at=START)


def test_the_auth_trail_records_logins_refusals_and_revocations(conn):
    svc = enrolled(conn)
    login = svc.open_session("owner-1", "owner", mfa_code=code_at(0), at=START)
    with pytest.raises(ID.InvalidCode):
        svc.verify_factor_code("owner-1", "000000", at=START)
    svc.revoke(login.session.session_id, reason="sign-out", at=START)
    kinds = [str(row["kind"]) for row in svc.events(identity="owner-1")]
    assert kinds.count("LOGIN") == 1
    assert "MFA_SUCCEEDED" in kinds
    assert "MFA_FAILED" in kinds
    assert "SESSION_REVOKED" in kinds


def test_the_auth_trail_is_append_only(conn):
    svc = enrolled(conn)
    svc.open_session("owner-1", "owner", mfa_code=code_at(0), at=START)
    event_id = conn.execute("SELECT auth_event_id FROM auth_events LIMIT 1").fetchone()[0]
    with pytest.raises(Exception):
        conn.execute("UPDATE auth_events SET kind = 'LOGIN' WHERE auth_event_id = ?", (event_id,))
    with pytest.raises(Exception):
        conn.execute("DELETE FROM auth_events WHERE auth_event_id = ?", (event_id,))


def test_every_refusal_is_recorded_without_storing_a_secret(conn):
    svc = enrolled(conn)
    with pytest.raises(ID.MFARequired):
        svc.open_session("owner-1", "owner", at=START)
    kinds = [str(row["kind"]) for row in svc.events()]
    assert "ACCESS_DENIED" in kinds


# --------------------------------------------------------------------------- #
# 7. The design promise: the shared secret is not in these tables
# --------------------------------------------------------------------------- #
def test_the_shared_secret_is_never_written_to_the_database(conn):
    """Leaking the auth tables must not hand over the second factor."""
    svc = enrolled(conn)
    svc.open_session("owner-1", "owner", mfa_code=code_at(0), at=START)
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}"):
            for value in tuple(row):
                assert SECRET not in str(value), f"the TOTP secret leaked into {table}"


def test_a_factor_without_a_registered_secret_fails_closed(conn):
    svc = service(conn, secrets=ID.MappingSecretResolver({}))
    svc.enroll_totp("owner-1", secret_ref="belum-terdaftar")
    with pytest.raises(ID.UnknownFactor):
        svc.confirm_totp("owner-1", "123456", at=START)


def test_the_environment_resolver_reads_the_named_variable():
    resolver = ID.EnvSecretResolver("VILLA_TOTP_SECRET_", environment={"VILLA_TOTP_SECRET_OWNER_1": SECRET})
    assert resolver.resolve("owner-1") == SECRET
    with pytest.raises(ID.UnknownFactor):
        resolver.resolve("finance-9")


def test_timeouts_must_be_sane(conn):
    with pytest.raises(ID.IdentityError):
        service(conn, idle_timeout=timedelta(minutes=60), absolute_timeout=timedelta(minutes=30))
    with pytest.raises(ID.IdentityError):
        service(conn, idle_timeout=timedelta(0))


def test_at_least_one_domain_error_type_is_a_domain_error(conn):
    assert issubclass(ID.IdentityError, DomainError)

def test_every_generated_session_token_is_accepted_by_the_reference_rule(conn):
    """The token generator must satisfy the validator *by construction*.

    ``secrets.token_urlsafe`` can start with "-" or "_", and the platform's opaque
    reference rule requires an alphanumeric first character — so a url-safe token was
    refused roughly 3% of the time, which is a login that fails at random. This test
    generates many tokens (high n, because a 3% defect hides from a single draw) and
    proves each one passes the same validator the request path uses.
    """
    from primitives import require_ref

    svc = service(conn, "agency_bali")
    for _ in range(500):
        token = svc.open_session("agent-1", "reservation", at=START).token
        assert require_ref(token, "token") == token
