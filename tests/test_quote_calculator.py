"""Tests for ``src/quote_calculator.py`` — the money path.

Every expected number below is derived by hand from the documented order of
operations, so a silent change in the pricing sequence fails loudly.
"""

from __future__ import annotations

import json
import unittest

import policy_engine as PE
import quote_calculator as QC
from primitives import DomainError

ISSUED = "2026-09-21T00:00:00+00:00"


def payload(**overrides):
    base = {
        "extra_guest_fee_minor": 250_000,
        "cleaning_fee_minor": 500_000,
        "security_deposit_minor": 2_000_000,
        "min_stay_nights": 2,
        "cancellation_ladder": [{"min_days_before": 0, "refund_pct": "100"}],
    }
    base.update(overrides)
    return base


def policy(**overrides):
    base = dict(
        policy_version_id="policy_v1",
        property_id="prop_1",
        unit_scope="property",
        version=1,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        rules=PE.PolicyRules(
            currency=overrides.pop("currency", "IDR"),
            deposit_pct=overrides.pop("deposit_pct", "30"),
            service_fee_pct=overrides.pop("service_fee_pct", "5"),
            tax_pbjt_pct=overrides.pop("tax_pbjt_pct", "10"),
            max_discount_pct=overrides.pop("max_discount_pct", "15"),
            quote_valid_hours=48,
            hold_ttl_minutes=720,
            max_hold_extensions=2,
            payload=overrides.pop("payload", payload()),
        ),
        created_at=ISSUED,
        source_doc_ref=overrides.pop("source_doc_ref", "policy/2026-public.pdf"),
        approved_by=overrides.pop("approved_by", "user:owner-1"),
    )
    base.update(overrides)
    return PE.PolicyVersion(**base)


def season(season_id="season_regular", start="2026-01-01", end="2026-12-31",
           rate=1_500_000, min_stay=1, priority=0, name="Regular"):
    return QC.Season(
        season_id=season_id,
        name=name,
        start_date=start,
        end_date=end,
        nightly_rate_minor=rate,
        min_stay_nights=min_stay,
        priority=priority,
    )


def rate_card(**overrides):
    base = dict(
        rate_card_id="card_v1",
        property_id="prop_1",
        unit_scope="property",
        name="Public 2026",
        version=1,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        currency=overrides.pop("currency", "IDR"),
        weekly_discount_pct=overrides.pop("weekly_discount_pct", "0"),
        monthly_discount_pct=overrides.pop("monthly_discount_pct", "0"),
        seasons=overrides.pop("seasons", (season(),)),
        source_doc_ref=overrides.pop("source_doc_ref", "rates/2026-public.pdf"),
        approved_by=overrides.pop("approved_by", "user:owner-1"),
    )
    base.update(overrides)
    return QC.RateCard(**base)


def unit(**overrides):
    base = dict(unit_id="unit_1", property_id="prop_1", base_capacity=2, max_guests=6)
    base.update(overrides)
    return QC.UnitFacts(**base)


def request(**overrides):
    base = dict(
        quote_date="2026-09-21",
        check_in="2026-10-01",
        check_out="2026-10-03",
        adults=2,
        children=0,
        infants=0,
        extras=(),
        discount_minor=0,
    )
    base.update(overrides)
    return QC.QuoteRequest(**base)


def quote(req=None, pol=None, card=None, u=None, **kwargs):
    return QC.calculate_quote(
        req or request(),
        pol or policy(),
        card or rate_card(),
        u or unit(),
        quote_id=kwargs.pop("quote_id", "quote_t1"),
        issued_at=kwargs.pop("issued_at", ISSUED),
    )


class TestStayAndNights(unittest.TestCase):
    def test_nights_and_night_dates_are_half_open(self):
        r = request(check_in="2026-10-01", check_out="2026-10-04")
        self.assertEqual(r.nights, 3)
        self.assertEqual(r.night_dates(), ("2026-10-01", "2026-10-02", "2026-10-03"))

    def test_night_dates_cross_a_month_boundary(self):
        r = request(check_in="2026-10-30", check_out="2026-11-02")
        self.assertEqual(r.night_dates(), ("2026-10-30", "2026-10-31", "2026-11-01"))

    def test_non_positive_stay_is_rejected(self):
        with self.assertRaises(DomainError):
            request(check_in="2026-10-03", check_out="2026-10-03")

    def test_infants_are_free_and_excluded_from_paying_guests(self):
        self.assertEqual(request(adults=2, children=1, infants=2).paying_guests, 3)


class TestCanonicalPricing(unittest.TestCase):
    """The reference quote, derived by hand from the documented order of operations."""

    def test_two_night_idr_quote_arithmetic(self):
        q = quote()
        self.assertEqual(q.accommodation.minor_units, 3_000_000)   # 2 x 1,500,000
        self.assertEqual(q.extra_guest.minor_units, 0)
        self.assertEqual(q.cleaning.minor_units, 500_000)
        self.assertEqual(q.discount.minor_units, 0)
        self.assertEqual(q.subtotal.minor_units, 3_500_000)
        self.assertEqual(q.service_fee.minor_units, 175_000)       # 5% of 3,500,000
        self.assertEqual(q.tax.minor_units, 367_500)               # 10% of 3,675,000
        self.assertEqual(q.total.minor_units, 4_042_500)
        self.assertEqual(q.security_deposit.minor_units, 2_000_000)

    def test_deposit_split_is_exact(self):
        q = quote()
        self.assertEqual(q.deposit_due.minor_units, 1_212_750)     # 30% of 4,042,500
        self.assertEqual(q.balance_due.minor_units, 2_829_750)
        self.assertEqual(
            (q.deposit_due + q.balance_due).minor_units, q.total.minor_units
        )

    def test_deposit_split_never_loses_a_unit_on_indivisible_totals(self):
        """33.33% of an odd total must still split exactly."""
        q = quote(pol=policy(deposit_pct="33.33", payload=payload(cleaning_fee_minor=1)))
        self.assertEqual(
            (q.deposit_due + q.balance_due).minor_units, q.total.minor_units
        )

    def test_security_deposit_is_not_part_of_the_total(self):
        q = quote()
        self.assertNotIn(q.security_deposit.minor_units, (q.total.minor_units,))
        self.assertEqual(
            q.total.minor_units,
            q.subtotal.minor_units + q.service_fee.minor_units + q.tax.minor_units,
        )

    def test_usd_cents_round_half_up(self):
        card = rate_card(currency="USD", seasons=(season(rate=25_050),))  # 250.50
        q = quote(
            req=request(check_in="2026-10-01", check_out="2026-10-02", adults=2),
            pol=policy(
                currency="USD",
                payload=payload(min_stay_nights=1, cleaning_fee_minor=0, security_deposit_minor=0),
            ),
            card=card,
        )
        self.assertEqual(q.accommodation.to_major_str(), "250.50")
        # 250.50 + 5% (12.53, rounded half up from 12.525) = 263.03; +10% = 289.33
        self.assertEqual(q.service_fee.minor_units, 1_253)
        self.assertEqual(q.total.to_major_str(), "289.33")

    def test_extra_guest_fee_is_per_guest_per_night(self):
        q = quote(req=request(adults=4), u=unit(base_capacity=2, max_guests=6))
        self.assertEqual(q.extra_guest.minor_units, 1_000_000)  # 250,000 x 2 guests x 2 nights
        # 3,000,000 + 1,000,000 + 500,000 = 4,500,000; +5% = 225,000; +10% of 4,725,000 = 472,500
        self.assertEqual(q.total.minor_units, 5_197_500)

    def test_extras_are_added_and_ordered_by_code(self):
        q = quote(req=request(extras=(
            {"code": "airport_transfer", "amount_minor": 400_000},
            {"code": "breakfast", "amount_minor": 150_000},
        )))
        self.assertEqual(q.extras.minor_units, 550_000)
        # 3,000,000 + 0 + 500,000 + 550,000 = 4,050,000; +5% = 202,500; +10% = 425,250
        self.assertEqual(q.total.minor_units, 4_677_750)
        codes = [line["code"] for line in q.lines]
        self.assertIn("extra:airport_transfer", codes)
        self.assertLess(codes.index("extra:airport_transfer"), codes.index("extra:breakfast"))

    def test_line_items_reconcile_to_the_subtotal(self):
        """Sum of the charged lines (nights, fees, extras, discounts) == subtotal."""
        q = quote(req=request(adults=3, extras=({"code": "spa", "amount_minor": 200_000},)))
        charged = [
            line["amount_minor"]
            for line in q.lines
            if line["code"].startswith(("night:", "extra", "cleaning", "discount:"))
        ]
        self.assertEqual(sum(charged), q.subtotal.minor_units)

    def test_zero_fee_policy_is_allowed(self):
        q = quote(pol=policy(service_fee_pct="0", tax_pbjt_pct="0", deposit_pct="0"))
        self.assertEqual(q.service_fee.minor_units, 0)
        self.assertEqual(q.tax.minor_units, 0)
        self.assertEqual(q.deposit_due.minor_units, 0)
        self.assertEqual(q.balance_due.minor_units, q.total.minor_units)

    def test_fractional_percentages_are_exact(self):
        q = quote(pol=policy(service_fee_pct="7.5", tax_pbjt_pct="10"))
        self.assertEqual(q.service_fee.minor_units, 262_500)  # 7.5% of 3,500,000
        self.assertEqual(q.tax.minor_units, 376_250)          # 10% of 3,762,500


class TestSeasonsAndRates(unittest.TestCase):
    def test_higher_priority_season_wins(self):
        card = rate_card(seasons=(
            season(season_id="season_low", priority=0, rate=1_000_000),
            season(season_id="season_peak", priority=10, rate=2_000_000, start="2026-10-01", end="2026-10-02"),
        ))
        q = quote(card=card, req=request(check_in="2026-10-01", check_out="2026-10-03"))
        # Both nights fall inside season_peak (1-2 Oct inclusive at 2,000,000 each).
        self.assertEqual(q.accommodation.minor_units, 4_000_000)

    def test_equal_priority_overlap_is_ambiguous(self):
        card = rate_card(seasons=(
            season(season_id="season_a", priority=5, start="2026-10-01", end="2026-10-31"),
            season(season_id="season_b", priority=5, start="2026-10-01", end="2026-10-31"),
        ))
        with self.assertRaises(DomainError) as ctx:
            quote(card=card)
        self.assertEqual(ctx.exception.code, "RATE_AMBIGUOUS")
        self.assertEqual(ctx.exception.details["season_ids"], ["season_a", "season_b"])

    def test_missing_season_fails_closed(self):
        card = rate_card(seasons=(season(start="2026-11-01", end="2026-11-30"),))
        with self.assertRaises(DomainError) as ctx:
            quote(card=card)
        self.assertEqual(ctx.exception.code, "RATE_NOT_FOUND")

    def test_stay_spanning_two_seasons_warns(self):
        card = rate_card(seasons=(
            season(season_id="season_regular", priority=0, rate=1_000_000),
            season(season_id="season_peak", priority=10, rate=2_000_000,
                   start="2026-10-02", end="2026-10-02"),
        ))
        q = quote(card=card)
        self.assertIn("STAY_SPANS_SEASONS:season_peak,season_regular", q.warnings)
        # night 1 Oct -> season_regular (1,000,000); night 2 Oct -> season_peak (2,000,000)
        self.assertEqual(q.accommodation.minor_units, 3_000_000)

    def test_season_outside_card_window_is_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            rate_card(seasons=(season(start="2025-01-01", end="2025-12-31"),))
        self.assertEqual(ctx.exception.code, "RATE_CARD_INVALID")

    def test_season_discount_bounds_mirror_the_database(self):
        with self.assertRaises(DomainError):
            rate_card(weekly_discount_pct="51")
        with self.assertRaises(DomainError):
            rate_card(monthly_discount_pct="61")

    def test_inverted_season_window_is_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            season(start="2026-12-01", end="2026-01-01")
        self.assertEqual(ctx.exception.code, "SEASON_WINDOW_INVALID")


class TestGatesBeforePricing(unittest.TestCase):
    def test_capacity_exceeded_is_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            quote(req=request(adults=7), u=unit(base_capacity=2, max_guests=6))
        self.assertEqual(ctx.exception.code, "CAPACITY_EXCEEDED")

    def test_policy_minimum_stay_is_enforced(self):
        with self.assertRaises(DomainError) as ctx:
            quote(req=request(check_in="2026-10-01", check_out="2026-10-02"))
        self.assertEqual(ctx.exception.code, "STAY_TOO_SHORT")
        self.assertEqual(ctx.exception.details["min_stay_nights"], 2)

    def test_season_minimum_stay_can_be_stricter_than_the_policy(self):
        card = rate_card(seasons=(season(min_stay=5),))
        with self.assertRaises(DomainError) as ctx:
            quote(card=card)
        self.assertEqual(ctx.exception.details["min_stay_nights"], 5)

    def test_minimum_stay_is_satisfied_exactly_at_the_boundary(self):
        q = quote(req=request(check_in="2026-10-01", check_out="2026-10-03"))
        self.assertEqual(q.nights, 2)

    def test_currency_mismatch_is_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            quote(card=rate_card(currency="USD"))
        self.assertEqual(ctx.exception.code, "QUOTE_CURRENCY_MISMATCH")

    def test_rate_card_from_another_property_is_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            quote(card=rate_card(property_id="prop_other"))
        self.assertEqual(ctx.exception.code, "RATE_CARD_MISMATCH")

    def test_naive_inputs_are_validated(self):
        with self.assertRaises(DomainError):
            request(adults=0)
        with self.assertRaises(DomainError):
            request(discount_minor=-1)
        with self.assertRaises(DomainError):
            unit(base_capacity=4, max_guests=2)


class TestDiscounts(unittest.TestCase):
    def test_weekly_discount_applies_from_seven_nights(self):
        card = rate_card(weekly_discount_pct="10", seasons=(season(rate=1_000_000),))
        q = quote(
            card=card,
            req=request(check_in="2026-10-01", check_out="2026-10-08"),
        )
        self.assertEqual(q.nights, 7)
        self.assertEqual(q.accommodation.minor_units, 7_000_000)
        self.assertEqual(q.discount.minor_units, 700_000)          # 10% of room revenue
        self.assertEqual(q.subtotal.minor_units, 6_800_000)        # cleaning keeps its full value
        self.assertEqual(q.total.minor_units, 7_854_000)

    def test_monthly_discount_replaces_the_weekly_one(self):
        card = rate_card(weekly_discount_pct="10", monthly_discount_pct="25",
                         seasons=(season(rate=1_000_000),))
        q = quote(card=card, req=request(check_in="2026-10-01", check_out="2026-10-29"))
        self.assertEqual(q.nights, 28)
        self.assertEqual(q.discount.minor_units, 7_000_000)  # 25% of 28,000,000

    def test_six_nights_get_no_length_of_stay_discount(self):
        card = rate_card(weekly_discount_pct="10", seasons=(season(rate=1_000_000),))
        q = quote(card=card, req=request(check_in="2026-10-01", check_out="2026-10-07"))
        self.assertEqual(q.nights, 6)
        self.assertEqual(q.discount.minor_units, 0)

    def test_requested_discount_is_capped_by_policy(self):
        with self.assertRaises(DomainError) as ctx:
            quote(req=request(discount_minor=600_000))  # 15% of 3,500,000 = 525,000
        self.assertEqual(ctx.exception.code, "DISCOUNT_EXCEEDS_POLICY")
        self.assertEqual(ctx.exception.details["ceiling_minor"], 525_000)

    def test_requested_discount_at_the_ceiling_is_allowed(self):
        q = quote(req=request(discount_minor=525_000, discount_reason="repeat guest"))
        self.assertEqual(q.discount.minor_units, 525_000)
        self.assertEqual(q.subtotal.minor_units, 2_975_000)

    def test_discount_without_a_reason_is_warned(self):
        q = quote(req=request(discount_minor=100_000))
        self.assertIn("DISCOUNT_WITHOUT_REASON", q.warnings)

    def test_discount_ceiling_uses_the_post_los_subtotal(self):
        """After a 10% LOS discount the 15% ceiling shrinks with it."""
        card = rate_card(weekly_discount_pct="10", seasons=(season(rate=1_000_000),))
        with self.assertRaises(DomainError) as ctx:
            quote(card=card, req=request(check_in="2026-10-01", check_out="2026-10-08", discount_minor=1_020_001))
        # after_los = 7,000,000 - 700,000 + 500,000 = 6,800,000 -> 15% = 1,020,000
        self.assertEqual(ctx.exception.details["ceiling_minor"], 1_020_000)


class TestExtrasValidation(unittest.TestCase):
    def test_duplicate_extra_codes_are_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            request(extras=(
                {"code": "spa", "amount_minor": 1},
                {"code": "spa", "amount_minor": 2},
            ))
        self.assertEqual(ctx.exception.code, "EXTRAS_DUPLICATE")

    def test_extra_shape_and_amount_are_validated(self):
        for bad in (
            {"code": "spa"},
            {"code": "spa", "amount_minor": -1},
            {"code": "spa", "amount_minor": "1000"},
            {"code": "spa", "amount_minor": 1.0},
            {"code": "spa", "amount_minor": True},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError) as ctx:
                    request(extras=(bad,))
                self.assertEqual(ctx.exception.code, "EXTRAS_INVALID")


class TestReproducibility(unittest.TestCase):
    def test_same_inputs_produce_identical_totals_and_fingerprint(self):
        first, second = quote(), quote()
        self.assertEqual(first.total, second.total)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.as_row(), second.as_row())

    def test_different_unit_changes_the_fingerprint(self):
        self.assertNotEqual(
            quote(u=unit(unit_id="unit_1")).fingerprint,
            quote(u=unit(unit_id="unit_2")).fingerprint,
        )

    def test_policy_change_changes_the_fingerprint(self):
        self.assertNotEqual(
            quote().fingerprint,
            quote(pol=policy(deposit_pct="20")).fingerprint,
        )

    def test_rate_card_change_changes_the_fingerprint(self):
        self.assertNotEqual(
            quote().fingerprint,
            quote(card=rate_card(seasons=(season(rate=1_600_000),))).fingerprint,
        )

    def test_discount_reason_does_not_change_the_price_fingerprint(self):
        """The reason is bookkeeping: it must not alter the price fingerprint."""
        a = quote(req=request(discount_minor=100_000, discount_reason="repeat"))
        b = quote(req=request(discount_minor=100_000, discount_reason="owner friend"))
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_quote_id_participates_in_the_fingerprint(self):
        self.assertNotEqual(
            quote(quote_id="quote_t1").fingerprint,
            quote(quote_id="quote_t2").fingerprint,
        )

    def test_expiry_comes_from_the_policy(self):
        self.assertEqual(quote().expires_at, "2026-09-23T00:00:00+00:00")


class TestRowShape(unittest.TestCase):
    """``as_row`` must line up with the ``quotes`` table columns."""

    def test_row_has_exactly_the_quotes_columns(self):
        self.assertEqual(set(quote().as_row()), {
            "quote_id", "property_id", "unit_id", "check_in", "check_out", "nights",
            "adults", "children", "infants", "currency", "policy_version_id",
            "policy_fingerprint", "rate_card_id", "accommodation_minor", "extra_guest_minor",
            "cleaning_minor", "extras_minor", "discount_minor", "subtotal_minor",
            "service_fee_minor", "tax_minor", "total_minor", "security_deposit_minor",
            "lines", "fingerprint", "status", "warnings", "created_at", "expires_at",
        })

    def test_json_columns_are_canonical_json(self):
        row = quote().as_row()
        lines = json.loads(row["lines"])
        warnings = json.loads(row["warnings"])
        self.assertIsInstance(lines, list)
        self.assertIsInstance(warnings, list)
        self.assertEqual(row["lines"], json.dumps(lines, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    def test_all_money_columns_are_integers(self):
        row = quote().as_row()
        for key, value in row.items():
            if key.endswith("_minor"):
                self.assertIsInstance(value, int, key)
                self.assertNotIsInstance(value, bool, key)

    def test_draft_status_is_the_only_status_the_calculator_emits(self):
        self.assertEqual(quote().as_row()["status"], "DRAFT")

    def test_to_dict_adds_the_deposit_split_without_touching_the_row(self):
        q = quote()
        extra = q.to_dict()
        self.assertEqual(extra["deposit_due_minor"], q.deposit_due.minor_units)
        self.assertEqual(extra["balance_due_minor"], q.balance_due.minor_units)
        self.assertEqual(set(extra) - set(q.as_row()), {"deposit_due_minor", "balance_due_minor", "rate_card_fingerprint"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
