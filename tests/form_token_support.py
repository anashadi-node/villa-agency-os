"""Signed inquiry-form tokens for tests (audit 2026-11-13, S1).

The public form now refuses tokens it did not sign, tokens younger than three
seconds and tokens older than two hours. Tests run on a frozen clock at
2026-09-21T08:00:00Z, so the helper signs a token "issued" one minute earlier with
the same test key that ``conftest.py`` puts in ``VILLA_FORM_TOKEN_KEY``.
"""

from __future__ import annotations

from datetime import timedelta

import marketing_site as M
from primitives import FrozenClock

#: Test-only key; never used outside the test process.
TEST_FORM_TOKEN_KEY = "test-only-form-token-key-not-a-real-secret"
FROZEN_NOW = "2026-09-21T08:00:00Z"


def signed_token(*, seconds_before_now: float = 60, now: str = FROZEN_NOW) -> str:
    clock = FrozenClock.at(now)
    tokens = M.FormTokens(clock, key=TEST_FORM_TOKEN_KEY)
    return tokens.issue(issued_at=clock.now() - timedelta(seconds=seconds_before_now))
