"""PACK-005C C6: pin the signer/contact-reader policy and its known MFA gap.

This module is intentionally read-only. It does not add a role, change a tuple, or
weaken an auth check. The strict xfail is the regression target for the proposed
Option A fix; it will turn into a failure the moment the Owner-approved change is
applied without updating this test, which is the point.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import roles  # noqa: E402
from supplier_assertion import CONTACT_READER_ROLES, CONTACT_REGISTRY_ROLES  # noqa: E402


def test_the_only_signer_is_the_reservation_manager():
    assert roles.SIGNER_ROLES == ("reservation_manager",)


def test_contact_registry_writers_are_owner_or_gm():
    assert CONTACT_REGISTRY_ROLES == ("owner", "gm")


def test_the_reader_set_is_the_union_of_registry_and_signers():
    assert CONTACT_READER_ROLES == ("gm", "owner", "reservation_manager")


def test_the_current_mfa_gap_is_explicit_not_accidental():
    # This is the exact observed conflict: a reservation_manager can read a
    # supplier contact only if _resolve_principal sees mfa_satisfied_at, but the
    # role is not in MFA_REQUIRED_ROLES, so open_session never requires a code.
    assert "reservation_manager" in roles.SIGNER_ROLES
    assert "reservation_manager" not in roles.MFA_REQUIRED_ROLES


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Owner decision required: Option A adds reservation_manager to "
        "MFA_REQUIRED_ROLES; do not activate it in PACK-005C"
    ),
)
def test_owner_approved_option_a_would_close_the_gap():
    assert "reservation_manager" in roles.MFA_REQUIRED_ROLES


def test_no_owner_token_bypass_is_part_of_the_design():
    # The proposed corrective design must not make the Owner a signing proxy for
    # a reservation manager. The signer set stays a single human role.
    assert "owner" not in roles.SIGNER_ROLES
