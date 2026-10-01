"""The human role vocabulary and the per-role authority matrix — §18, ADR-0035, ADR-0036.

§18 requires a per-role matrix test, and it is the test that has to carry the *rules* rather
than the names. ADR-0035 listed them when it named the holders; ADR-0036 added the GM tier and
one negative that matters more than the others:

    no AI identity may ever appear as an approver on a money-out request.

Five properties are pinned here:

1. **One home.** The three vocabularies that disagreed (``dashboard.ROLES``,
   ``approvals.ROLE_RANK``, ``identity.MFA_REQUIRED_ROLES``) now read their ladder from
   ``roles``; a second copy would be free to drift again.
2. **The ladder** is the ADR-0036 table, and ``gm`` is the only new tier.
3. **A rename is not an authority change.** Ranks 1–3 keep the integers the legacy names had,
   and the legacy names keep resolving — approval rows are append-only history.
4. **The authority matrix**: system_admin holds no finance or bank authority, the finance
   manager cannot be the maker of what it approves, the reservation manager cannot approve its
   own discount, the auditor is read-only, the greeting service reads stays and writes notes.
5. **No AI on the ladder**, at both the vocabulary and the engine.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import approvals
import dashboard as D
import roles
from approvals import ApprovalService, InvalidApprover
from primitives import FrozenClock, IdFactory

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = Path(os.environ.get("VILLA_DB", REPO_ROOT / "data" / "villa.db"))

#: Every capability the vocabulary declares, in one tuple, so the negative assertions below
#: can say "none of these" instead of listing four names and forgetting the fifth.
WRITE_CAPABILITIES = (
    "approve.money_out",
    "approve.pricing",
    "make.money_out",
    "finance.write",
    "booking.write",
    "operations.write",
    "notes.write",
    "system.write",
    "commercial.write",
    "partnership.write",
    "legal.record",
)


# --------------------------------------------------------------------------- #
# 1. One home for the vocabulary
# --------------------------------------------------------------------------- #
def test_the_ladder_has_exactly_one_home():
    """``approvals.ROLE_RANK`` is not a copy of the ladder — it *is* the ladder."""
    assert approvals.ROLE_RANK is roles.ROLE_RANK, (
        "approvals carries its own copy again: the reconciliation of ADR-0035 §5 is undone"
    )


def test_every_role_the_dashboard_acts_as_is_canonical_and_ranked():
    """A dashboard value the kernel cannot rank silences a whole role (the rename trap)."""
    for dashboard_role, approval_role in D.APPROVAL_ROLE_BY_ROLE.items():
        assert roles.canonical_role(approval_role) == approval_role, (
            f"{dashboard_role} still maps to the legacy name {approval_role!r}"
        )
        assert roles.role_rank(approval_role) is not None, dashboard_role


def test_every_page_role_that_may_act_has_a_mapped_approval_role():
    """The mapping must be complete, and a page word must not be a ladder name by accident.

    ``reservation`` is a page, ``reservation_agent`` is a rank. A page role that may act and
    has no mapped approval role reaches the kernel as itself and is refused for a reason that
    names no cause — so the two lists are checked together, and the read-only page role is
    checked to be absent on purpose rather than by oversight.
    """
    assert set(D.WRITE_ROLES) <= set(D.APPROVAL_ROLE_BY_ROLE)
    for page_role in D.WRITE_ROLES:
        mapped = D.APPROVAL_ROLE_BY_ROLE[page_role]
        assert roles.role_rank(mapped) is not None, page_role
    assert "auditor" in D.ROLES
    assert "auditor" not in D.APPROVAL_ROLE_BY_ROLE
    # ``owner`` is the one word that is both a page and a rank, and that is on purpose: the
    # owner is the owner in both lists. Every other page word must not be a ladder name.
    for page_role in D.ROLES:
        if page_role == "owner":
            continue
        assert page_role not in roles.ADJUDICATOR_ROLES, page_role


# --------------------------------------------------------------------------- #
# 2. The ladder is ADR-0036's table
# --------------------------------------------------------------------------- #
def test_the_ladder_is_the_adr_0036_table():
    assert dict(roles.ROLE_RANK) == {
        "reservation_agent": 1,
        "reservation_manager": 2,
        "finance_manager": 3,
        "gm": 4,
        "owner": 5,
    }
    assert roles.role_rank("gm") > roles.role_rank("finance_manager")
    assert roles.role_rank("gm") < roles.role_rank("owner")
    assert tuple(roles.ADJUDICATOR_ROLES) == tuple(roles.ROLE_RANK)


def test_gm_is_the_only_added_tier():
    """``gm`` is new; nothing else moved. An authority change would hide behind a rename."""
    legacy_ranks = {"agent": 1, "manager": 2, "accountant": 3}
    for legacy, rank in legacy_ranks.items():
        assert roles.role_rank(legacy) == rank, legacy
    assert roles.role_rank("owner") == max(roles.ROLE_RANK.values())


# --------------------------------------------------------------------------- #
# 3. A role that is not on the ladder cannot adjudicate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "role",
    ["auditor", "greeting_service", "system_admin", "commercial_manager",
     "partnership_manager", "revenue_manager", "legal_compliance", "finance_staff"],
)
def test_a_known_role_with_no_ladder_rank_cannot_decide_anything(role):
    assert roles.is_known_role(role), role
    assert roles.role_rank(role) is None, role
    assert roles.may(role, "approve.money_out") is False, role


# --------------------------------------------------------------------------- #
# 4. The authority matrix (ADR-0035's "control that rides on it", executable)
# --------------------------------------------------------------------------- #
def test_the_system_admin_holds_no_finance_or_bank_authority():
    """ADR-0035: Technology / Security has **zero** business or financial authority."""
    for capability in WRITE_CAPABILITIES:
        if capability == "system.write":
            continue
        assert roles.may("system_admin", capability) is False, capability
    assert roles.may("system_admin", "system.write") is True
    assert roles.may("system_admin", "finance.read") is False
    assert roles.role_rank("system_admin") is None


def test_the_finance_manager_is_a_checker_and_never_the_maker():
    """The maker function moved to the AI principal (ADR-0035 amendment) — not to the checker."""
    assert roles.may("finance_manager", "approve.money_out") is True
    assert roles.may("finance_manager", "make.money_out") is False
    assert roles.may("owner", "make.money_out") is False, "the owner is not a maker either"
    # The maker tier exists and is ranked below every checker, so it can never approve itself.
    assert roles.role_rank("reservation_agent") < roles.role_rank("finance_manager")


def test_the_maker_ids_are_reserved_for_the_ai_and_handed_to_no_human():
    for role in ("finance_staff", "reservation_agent"):
        assert role in roles.ROLE_IDS, role
        assert role not in roles.HUMAN_ROLE_IDS, role
        assert roles.ROLE_CAPABILITIES[role] == frozenset(), role
    for role in roles.HUMAN_ROLE_IDS:
        assert roles.may(role, "make.money_out") is False, role


def test_the_reservation_manager_approves_pricing_and_cannot_approve_money_out():
    assert roles.may("reservation_manager", "approve.pricing") is True
    assert roles.may("reservation_manager", "approve.money_out") is False
    assert roles.may("reservation_manager", "finance.write") is False


def test_the_auditor_is_read_only():
    assert roles.may("auditor", "audit.read") is True
    for capability in WRITE_CAPABILITIES:
        assert roles.may("auditor", capability) is False, capability
    assert roles.role_rank("auditor") is None
    assert D.Principal("auditor-external-1", "auditor").approval_role is None
    assert D.Principal("auditor-external-1", "auditor").may_write is False


def test_the_greeting_service_reads_stays_and_writes_notes_and_nothing_else():
    """ADR-0035 §4: it may see who is staying where, and add a note. Nothing else at all."""
    assert roles.ROLE_CAPABILITIES["greeting_service"] == frozenset({"stays.read", "notes.write"})
    for capability in roles.CAPABILITIES:
        if capability in ("stays.read", "notes.write"):
            continue
        assert roles.may("greeting_service", capability) is False, capability


def test_the_declared_capabilities_are_all_spelled_in_the_vocabulary():
    """A capability nobody declares is a typo, and a typo reads as 'not allowed' — silently."""
    declared = set(roles.CAPABILITIES)
    used = set().union(*roles.ROLE_CAPABILITIES.values())
    assert used <= declared, f"undeclared capability: {sorted(used - declared)}"


# --------------------------------------------------------------------------- #
# 5. No AI identity on the ladder — at the vocabulary and at the engine
# --------------------------------------------------------------------------- #
def test_the_ai_identity_is_not_a_role_and_cannot_be_ranked():
    assert roles.is_ai_role("ai_service")
    assert roles.canonical_role("ai_service") is None
    assert roles.role_rank("ai_service") is None
    assert roles.is_known_role("ai_service") is False
    for capability in roles.CAPABILITIES:
        assert roles.may("ai_service", capability) is False, capability


def test_the_ai_identity_is_not_an_adjudicator():
    assert set(roles.AI_ROLE_IDS).isdisjoint(roles.ADJUDICATOR_ROLES)
    with pytest.raises(roles.UnknownRole):
        roles.approval_ladder(["ai_service"])
    with pytest.raises(roles.UnknownRole):
        roles.approval_ladder(["finance_manager", "ai_service"])


def test_the_engine_refuses_an_ai_identity_as_the_required_role(conn):
    """A money-out request whose required tier is the AI is refused, not merely unusual."""
    svc = ApprovalService(conn, clock=FrozenClock.at("2026-10-02T03:00:00Z"), ids=IdFactory(seed="roles"))
    for subject, action in (("refund", "refund.approve"), ("payout", "payment.approve")):
        with pytest.raises(InvalidApprover):
            svc.request(
                subject_type=subject,
                subject_id=f"{subject}_9001",
                action=action,
                requested_by="service:ai_service",
                requested_role="ai_service",
                reason="AI mengajukan sendiri pembayaran keluar",
                required_role="ai_service",
                required_approvals=2,
                idempotency_key=f"idem_ai_{subject}",
            )


def test_the_engine_refuses_a_machine_principal_as_an_approver(conn):
    """The other half: even with a human tier, the *approver* cannot be a machine."""
    svc = ApprovalService(conn, clock=FrozenClock.at("2026-10-02T03:00:00Z"), ids=IdFactory(seed="roles"))
    request = svc.request(
        subject_type="refund",
        subject_id="refund_9002",
        action="refund.approve",
        requested_by="service:ai_service",
        requested_role="reservation_agent",
        reason="tamu membatalkan, deposit harus dikembalikan",
        required_role="finance_manager",
        required_approvals=2,
        money=None,
        idempotency_key="idem_ai_approver",
    )
    assert request.required_role == "finance_manager"
    with pytest.raises(InvalidApprover):
        svc.decide(
            request.approval_id,
            approver="service:ai_service",
            approver_role="gm",
            decision="APPROVE",
            note="AI menyetujui pembayarannya sendiri",
        )


# --------------------------------------------------------------------------- #
# 6. The published ladder must name roles the engine knows
# --------------------------------------------------------------------------- #
def test_the_payment_ladder_document_prefers_canonical_names():
    """ADR-0036's two tiers, and a legacy ladder resolves to the same ranks."""
    assert roles.approval_ladder(["finance_manager", "gm"]) == ("finance_manager", "gm")
    assert roles.approval_ladder(["accountant", "gm"]) == ("finance_manager", "gm")
    assert [roles.role_rank(r) for r in roles.approval_ladder(["finance_manager", "gm"])] == [3, 4]


@pytest.mark.parametrize(
    "ladder",
    [
        [],
        ["gm", "finance_manager"],          # descends: an authority nobody holds
        ["finance_manager", "finance_manager"],  # the same tier twice
        ["auditor"],                        # known role, no ladder rank
        ["supervisor"],                     # unknown
        ["manager"],                        # a legacy name with no canonical equal is fine…
        "finance_manager",                  # …but a bare string is not a ladder
    ],
)
def test_a_ladder_that_cannot_be_honoured_is_refused(ladder):
    if ladder == ["manager"]:
        assert roles.approval_ladder(ladder) == ("reservation_manager",)
        return
    with pytest.raises(roles.UnknownRole):
        roles.approval_ladder(ladder)


@pytest.mark.skipif(not LIVE_DB.exists(), reason="no live database in this checkout")
def test_the_ladder_published_in_this_database_names_known_roles():
    """The drift the reconciliation was written to catch, checked against the real store.

    ``finance.approval.payment_thresholds_minor`` was published naming
    ``["finance_manager", "gm"]`` while neither name existed in the ladder, which would have
    answered the first real payment with a refusal that named no cause. The value and the
    vocabulary are checked together here — and this check belongs in preflight, not only here,
    because a fresh checkout has no ``data/villa.db`` to read.
    """
    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM policy_revisions WHERE policy_key = ?"
            " ORDER BY version DESC LIMIT 1",
            ("finance.approval.payment_thresholds_minor",),
        ).fetchone()
    finally:
        conn.close()
    if row is None:  # pragma: no cover - depends on the store's contents
        pytest.skip("the key is unpublished in this database")

    import json

    value = json.loads(row[0])
    ladder = value.get("ladder")
    if ladder is None:  # pragma: no cover - the pre-ADR-0036 shape named levels instead
        pytest.skip("this revision predates the ladder shape")
    assert roles.approval_ladder(ladder) == ("finance_manager", "gm")
    assert roles.MONEY_OUT_APPROVER_ROLES >= set(roles.approval_ladder(ladder))
