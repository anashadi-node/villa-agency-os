"""WAVE 1-H tests: the policy engine as a first-class component (Master Spec §16).

§16 is explicit: policy "wajib menjadi first-class backend component, bukan angka
tersebar di source code", it is versioned with effective_from/effective_to, and a
historical decision stores the policy version that governed it. The repository
already keeps *priced* policy (deposit %, tax, quote validity) in
``policy_versions``; this suite covers the governance half — the security and
operational numbers that still live as Python constants in ``identity.py``,
``approvals.py`` and ``gates.py``.

Five properties are pinned here:

1. Every §16 policy example has a versioned home — a governance key, or a column
   of the existing pricing table. No example is left as a bare constant.
2. A value resolves from the store together with the revision id and fingerprint
   that produced it, so a decision can cite the exact policy it obeyed.
3. Resolution fails closed: an unknown key, a value that was never published, an
   instant before the first revision and a mistyped value are all refusals.
4. A policy change needs no code change: publish a revision and the security
   timeouts follow. A backdated revision is refused so history cannot be rewritten.
5. Code defaults equal the published baseline (drift guard). The store is the
   authority; divergence fails the suite instead of drifting silently.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from primitives import DomainError, FrozenClock
from policy_engine import (
    POLICY_DOMAINS,
    POLICY_VALUE_TYPES,
    InvalidPolicyValue,
    InvalidPolicyWindow,
    PolicyApproverRequired,
    PolicyBackdatedRevision,
    PolicyResolver,
    PolicySourceRequired,
    PolicyValueNotPublished,
    UnknownPolicyKey,
)

import approvals
import gates
import identity

NOW = "2026-09-26T09:00:00Z"
TODAY = "2026-09-26"
LATER = "2026-10-01"
AFTER_SUPERSESSION = "2026-10-02T09:00:00Z"
BEFORE_ANY_REVISION = "2025-12-31"
AGENCY = "agency_default"


def resolver(conn, *, at: str = NOW, agency_id: str = AGENCY) -> PolicyResolver:
    return PolicyResolver(conn, clock=FrozenClock.at(at), agency_id=agency_id)


def publish(conn, key: str, value: str, *, effective_from: str = LATER,
            effective_to: str = "9999-12-31", source: str = "ADR-0005 §step-up",
            approved_by: str = "owner_anas", at: str = NOW):
    return resolver(conn, at=at).publish(
        key,
        value,
        effective_from=effective_from,
        effective_to=effective_to,
        source_doc_ref=source,
        approved_by=approved_by,
    )


# -- 1. the catalogue -------------------------------------------------------- #

#: §16's example list, and where each one is versioned. The priced examples are
#: already first-class in ``policy_versions`` (property/unit scoped, approved_by,
#: fingerprint); duplicating them here would create a second source of truth.
SPEC_EXAMPLES = {
    "client deposit %": ("pricing", "deposit_pct"),
    "balance due days": ("pricing", "quote_valid_hours"),
    "discount authority": ("defs", "commercial.discount.authority_roles"),
    "margin floors": ("defs", "commercial.margin_floor_pct"),
    "payment approval thresholds": ("defs", "finance.approval.payment_thresholds_minor"),
    "dual-approval thresholds": ("defs", "finance.approval.dual_control_min_approvals"),
    "hold reminders": ("defs", "availability.hold.reminder_minutes"),
    "reconfirmation freshness": ("defs", "availability.reconfirmation.freshness_hours"),
    "incident escalation": ("defs", "operations.incident.escalation_minutes_by_severity"),
    "step-up authentication": ("defs", "security.step_up.window_minutes"),
    "retention policy": ("defs", "retention.records.retention_days_by_class"),
    "booking financial gates": ("defs", "finance.booking.financial_gates"),
}


def test_every_spec_policy_example_has_a_versioned_home(conn):
    catalogue = {definition.policy_key for definition in resolver(conn).catalogue()}
    pricing_columns = {column.name for column in _pricing_columns(conn)}
    for example, (home, name) in SPEC_EXAMPLES.items():
        if home == "defs":
            assert name in catalogue, f"§16 example {example!r} has no policy key"
        else:
            assert name in pricing_columns, f"§16 example {example!r} lost its home"


def _pricing_columns(conn):
    from schema_loader import parse_schema_model

    model = parse_schema_model("sqlite")
    table = model.tables.get("policy_versions")
    assert table is not None, "policy_versions is missing from the schema"
    return table.columns


def test_a_definition_records_owner_risk_and_the_spec_clause(conn):
    for definition in resolver(conn).catalogue():
        assert definition.domain in POLICY_DOMAINS
        assert definition.value_type in POLICY_VALUE_TYPES
        assert definition.risk_level in ("LOW", "MEDIUM", "HIGH")
        assert definition.owner_role, definition.policy_key
        assert definition.spec_ref.startswith("§"), definition.policy_key
        assert definition.description, definition.policy_key


# -- 2. resolving ------------------------------------------------------------ #

def test_a_published_value_resolves_with_its_revision(conn):
    value = resolver(conn).resolve("security.step_up.window_minutes")
    assert value.as_int() == 10
    assert value.version == 1
    assert value.policy_revision_id
    assert len(value.fingerprint) == 64
    assert value.source_doc_ref
    assert value.approved_by


def test_resolving_returns_the_revision_that_governed_that_instant(conn):
    before = resolver(conn).resolve("security.step_up.window_minutes", at=TODAY)
    assert before.as_int() == 10
    publish(conn, "security.step_up.window_minutes", "15", effective_from=LATER)
    assert resolver(conn).resolve("security.step_up.window_minutes", at=TODAY).as_int() == 10
    assert resolver(conn).resolve("security.step_up.window_minutes", at=LATER).as_int() == 15
    assert (
        resolver(conn).resolve("security.step_up.window_minutes", at=LATER).policy_revision_id
        != before.policy_revision_id
    )


# -- 3. fail-closed refusals ------------------------------------------------- #

def test_an_unknown_key_is_refused(conn):
    with pytest.raises(UnknownPolicyKey) as exc:
        resolver(conn).resolve("security.no.such.policy")
    assert exc.value.code == "POLICY_UNKNOWN_KEY"


def test_a_value_that_was_never_published_is_refused(conn):
    """§49 leaves retention open, so the key exists and has no value — inventing a
    number here would be a fabricated business rule."""
    with pytest.raises(PolicyValueNotPublished) as exc:
        resolver(conn).resolve("retention.records.retention_days_by_class")
    assert exc.value.code == "POLICY_VALUE_NOT_PUBLISHED"
    assert exc.value.details["policy_key"] == "retention.records.retention_days_by_class"


def test_an_instant_before_the_first_revision_is_refused(conn):
    with pytest.raises(PolicyValueNotPublished):
        resolver(conn).resolve("security.step_up.window_minutes", at=BEFORE_ANY_REVISION)


def test_a_revision_window_that_ends_before_it_starts_is_refused(conn):
    with pytest.raises(InvalidPolicyWindow) as exc:
        publish(conn, "security.step_up.window_minutes", "15",
                effective_from="2026-10-05", effective_to="2026-10-01")
    assert exc.value.code == "POLICY_WINDOW_INVALID"


def test_a_mistyped_value_is_refused(conn):
    with pytest.raises(InvalidPolicyValue) as exc:
        publish(conn, "security.step_up.window_minutes", "ten minutes")
    assert exc.value.code == "POLICY_VALUE_INVALID"


def test_a_percentage_outside_its_range_is_refused(conn):
    with pytest.raises(InvalidPolicyValue):
        publish(conn, "commercial.margin_floor_pct", "140")


def test_publishing_needs_a_source_document_and_an_approver(conn):
    with pytest.raises(PolicySourceRequired):
        publish(conn, "security.step_up.window_minutes", "15", source="")
    with pytest.raises(PolicyApproverRequired):
        publish(conn, "security.step_up.window_minutes", "15", approved_by="")


def test_a_backdated_revision_is_refused(conn):
    """Versions may only move forward, otherwise a later publish would silently
    rewrite what a past decision was allowed to do."""
    publish(conn, "security.step_up.window_minutes", "15", effective_from=LATER)
    with pytest.raises(PolicyBackdatedRevision) as exc:
        publish(conn, "security.step_up.window_minutes", "20", effective_from=TODAY)
    assert exc.value.code == "POLICY_BACKDATED_REVISION"


def test_publishing_an_unknown_key_is_refused(conn):
    with pytest.raises(UnknownPolicyKey):
        publish(conn, "security.invented.key", "1")


# -- 4. versioning ----------------------------------------------------------- #

def test_the_version_advances_and_history_is_preserved(conn):
    publish(conn, "security.step_up.window_minutes", "15", effective_from=LATER)
    history = resolver(conn).history("security.step_up.window_minutes")
    assert [value.version for value in history] == [1, 2]
    assert [value.as_int() for value in history] == [10, 15]


def test_revisions_are_append_only(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE policy_revisions SET value = '99'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM policy_revisions")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM policy_defs")


def test_a_revision_cannot_be_replayed_at_the_same_version(conn):
    publish(conn, "security.step_up.window_minutes", "15", effective_from=LATER)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version,"
            " value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint,"
            " created_at) VALUES ('pol_rev_dup', 'agency_default',"
            " 'security.step_up.window_minutes', 1, '99', '2026-10-01', '9999-12-31',"
            " 'seed', 'owner_anas', 'f' * 64, '2026-09-26T00:00:00+00:00')"
        )


# -- 5. the store governs the running system --------------------------------- #

def test_code_defaults_match_the_published_baseline(conn):
    """Drift guard: the constants stay as bootstrap defaults, so they must equal
    the published policy. A change in one place without the other fails here."""
    policy = resolver(conn)
    assert identity.STEP_UP_WINDOW == timedelta(
        minutes=policy.resolve("security.step_up.window_minutes").as_int()
    )
    assert identity.SESSION_IDLE_TIMEOUT == timedelta(
        minutes=policy.resolve("security.session.idle_timeout_minutes").as_int()
    )
    assert identity.SESSION_ABSOLUTE_TIMEOUT == timedelta(
        hours=policy.resolve("security.session.absolute_timeout_hours").as_int()
    )
    assert tuple(identity.MFA_REQUIRED_ROLES) == policy.resolve(
        "security.mfa.required_roles"
    ).as_roles()
    assert tuple(identity.HIGH_RISK_SUBJECT_TYPES) == tuple(
        policy.resolve("security.step_up.high_risk_subjects").as_json()
    )
    assert dict(approvals.EXPIRY_MINUTES) == policy.resolve(
        "finance.approval.expiry_minutes_by_risk"
    ).as_json()
    assert tuple(approvals.DUAL_CONTROL_SUBJECTS) == tuple(
        policy.resolve("finance.approval.dual_control_subjects").as_json()
    )
    assert approvals.DUAL_CONTROL_MIN_APPROVALS == policy.resolve(
        "finance.approval.dual_control_min_approvals"
    ).as_int()
    assert approvals.MAX_REQUIRED_APPROVALS == policy.resolve(
        "finance.approval.max_required_approvals"
    ).as_int()
    assert dict(gates.LEVEL_FRESHNESS_SECONDS) == policy.resolve(
        "availability.evidence.freshness_seconds_by_level"
    ).as_json()
    assert dict(gates.LEVEL_MAX_FRESHNESS_SECONDS) == policy.resolve(
        "availability.evidence.max_freshness_seconds_by_level"
    ).as_json()


def test_a_session_service_can_be_built_from_the_policy_store(conn):
    service = identity.SessionService.from_policy(conn, resolver(conn))
    assert service.step_up_window == timedelta(minutes=10)
    assert service.idle_timeout == timedelta(minutes=30)
    assert service.absolute_timeout == timedelta(hours=12)
    assert tuple(service.mfa_required_roles) == ("owner", "finance")
    assert tuple(service.high_risk_subject_types) == ("refund", "payout", "bank_change")


def test_the_security_timeouts_follow_the_store_not_the_constants(conn):
    """The point of §16: a policy change without a code change."""
    publish(conn, "security.step_up.window_minutes", "15", effective_from=LATER)
    publish(conn, "security.session.idle_timeout_minutes", "45", effective_from=LATER)
    later = identity.SessionService.from_policy(conn, resolver(conn, at=AFTER_SUPERSESSION))
    assert later.step_up_window == timedelta(minutes=15)
    assert later.idle_timeout == timedelta(minutes=45)
    # the module constants are untouched — the store is what moved
    assert identity.STEP_UP_WINDOW == timedelta(minutes=10)
    assert identity.SESSION_IDLE_TIMEOUT == timedelta(minutes=30)


def test_the_high_risk_subject_set_follows_the_store(conn):
    publish(conn, "security.step_up.high_risk_subjects", '["refund", "payout", "bank_change", "credit_note"]',
            effective_from=LATER)
    later = identity.SessionService.from_policy(conn, resolver(conn, at=AFTER_SUPERSESSION))
    assert "credit_note" in later.high_risk_subject_types


def test_an_agency_that_has_published_nothing_falls_back_to_the_baseline(conn):
    """Policy is scoped to the agency; a second agency must not inherit silence."""
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_b', 'Agency B', 'ACTIVE', ?, ?)",
        (NOW, NOW),
    )
    other = PolicyResolver(conn, clock=FrozenClock.at(NOW), agency_id="agency_b")
    value = other.resolve("security.step_up.window_minutes")
    assert value.as_int() == 10
    assert value.agency_id == AGENCY


def test_a_governance_key_is_readable_as_its_declared_type(conn):
    policy = resolver(conn)
    assert isinstance(policy.resolve("security.step_up.window_minutes").as_int(), int)
    assert policy.resolve("security.mfa.required_roles").as_roles() == ("owner", "finance")
    assert policy.resolve("finance.approval.dual_control_min_approvals").as_int() == 2
    assert isinstance(policy.resolve("finance.approval.expiry_minutes_by_risk").as_json(), dict)
    with pytest.raises(DomainError):
        policy.resolve("security.step_up.window_minutes").as_json()
