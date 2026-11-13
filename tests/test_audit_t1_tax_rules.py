"""Audit 2026-11-13, finding T1: the POLICY_MATRIX tax rules exist in code.

``tax_applies_to``, ``tax_included_in_rate`` and ``blackout_dates`` were documented
(POLICY_MATRIX §Pajak & fee, §Pembatalan) but appeared nowhere in ``src/``; the
calculator always charged PBJT on subtotal + service fee. A villa whose published
rate already included PBJT would therefore have been taxed twice.

Every expected number is derived by hand from the reference quote of
``test_quote_calculator`` (2 nights x 1,500,000 + cleaning 500,000 = 3,500,000
subtotal, service fee 5 %, PBJT 10 %).
"""

from __future__ import annotations

import pytest

import policy_engine as PE
from primitives import DomainError
from test_quote_calculator import payload, policy, quote, request


def _q(**payload_overrides):
    return quote(pol=policy(payload=payload(**payload_overrides)))


def test_default_is_unchanged_tax_on_subtotal_plus_service():
    q = _q()
    assert (q.subtotal.minor_units, q.service_fee.minor_units, q.tax.minor_units, q.total.minor_units) == (
        3_500_000, 175_000, 367_500, 4_042_500,
    )


def test_explicit_default_keeps_the_same_numbers_and_the_old_fingerprint_unchanged():
    assert _q(tax_applies_to="subtotal_plus_service").total.minor_units == 4_042_500
    # A policy that does not name the new keys keeps its published fingerprint.
    assert "tax_applies_to" not in policy().rules.as_dict()["payload"]


def test_tax_on_subtotal_only():
    q = _q(tax_applies_to="subtotal")
    assert q.service_fee.minor_units == 175_000
    assert q.tax.minor_units == 350_000            # 10 % of 3,500,000
    assert q.total.minor_units == 4_025_000


def test_tax_included_in_rate_is_backed_out_not_charged_twice():
    q = _q(tax_included_in_rate=True)
    # net = 3,500,000 / 1.1 = 3,181,818.18 -> 3,181,818; included PBJT = 318,182
    # service fee on net 5 % = 159,090.9 -> 159,091; PBJT on service fee = 15,909
    assert q.service_fee.minor_units == 159_091
    assert q.tax.minor_units == 318_182 + 15_909
    assert q.total.minor_units == 3_500_000 + 159_091 + 15_909
    assert "TAX_INCLUDED_IN_RATE" in q.warnings
    tax_line = next(line for line in q.lines if line["code"] == "tax_pbjt")
    assert tax_line.get("included") is True


def test_tax_included_with_subtotal_base_adds_no_tax_on_top():
    q = _q(tax_included_in_rate=True, tax_applies_to="subtotal")
    assert q.tax.minor_units == 318_182
    assert q.total.minor_units == 3_500_000 + 159_091


def test_deposit_split_stays_exact_with_included_tax():
    q = _q(tax_included_in_rate=True)
    assert q.deposit_due.minor_units + q.balance_due.minor_units == q.total.minor_units


def test_blackout_night_is_refused_but_checkout_day_is_not():
    with pytest.raises(DomainError) as exc:
        _q(blackout_dates=["2026-10-02"])
    assert exc.value.code == "BLACKOUT_DATE"
    assert _q(blackout_dates=["2026-10-03"]).total.minor_units == 4_042_500


@pytest.mark.parametrize(
    "overrides",
    [
        {"tax_applies_to": "total"},
        {"tax_included_in_rate": "yes"},
        {"blackout_dates": "2026-10-02"},
        {"blackout_dates": ["02/10/2026"]},
        {"blackout_dates": ["2026-02-30"]},
    ],
)
def test_malformed_tax_keys_fail_closed(overrides):
    with pytest.raises(DomainError) as exc:
        PE.PolicyRules(
            currency="IDR", deposit_pct="30", service_fee_pct="5", tax_pbjt_pct="10",
            max_discount_pct="15", quote_valid_hours=48, hold_ttl_minutes=720,
            max_hold_extensions=2, payload=payload(**overrides),
        )
    assert exc.value.code == "POLICY_PAYLOAD_INVALID"


def test_request_helper_still_sells_two_nights():
    assert request().nights == 2
