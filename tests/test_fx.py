"""§11's absolute margin floor needs a currency — these tests pin the conversion that gives it one.

`commercial.margin_floor_minor` is a single number, and a single number carries no currency: it
is denominated in `commercial.margin_floor_currency`, and a booking in any other currency has to
convert it through `commercial.fx.rates` before the §11 guard is applied. Bali sells in USD,
Yogyakarta may contract in IDR, so both directions are real.

What these tests pin, and why:

* an amount moves through **major units** when the currencies differ — IDR has exponent 0 and USD
  has exponent 2, so scaling minor units by a rate directly is the classic cross-currency bug;
* a **minimum is never rounded down** — the default rounding is ceiling, so a converted floor can
  only ever be equal to or stricter than the exact value;
* **no rate is ever assumed** — an unpublished pair refuses rather than reading rupiah as dollars,
  which is the failure mode that *passes* an absolute floor instead of failing it;
* **no float is ever accepted** as a rate, because a binary approximation must not reach money;
* an inverted pair is answered, and says that it was inverted.
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

import fx as FX
from primitives import InvalidMoney

RATE = "16250"
FRACTIONAL_RATE = "16250.5"


class TestPairsAndRatesAreReadExactly(unittest.TestCase):
    """A rate is a published fact: it is read, validated, and never guessed at."""

    def test_a_pair_key_normalises_case_and_separates_the_two_codes(self):
        self.assertEqual("USD/IDR", FX.pair_key("usd", "idr"))

    def test_a_pair_key_refuses_a_currency_the_platform_does_not_hold(self):
        with self.assertRaises(InvalidMoney):
            FX.pair_key("USD", "XYZ")

    def test_a_rate_may_be_written_as_a_string_an_int_or_a_decimal(self):
        for written in (RATE, 16250, Decimal(RATE)):
            with self.subTest(written=written):
                self.assertEqual(Decimal(RATE), FX.parse_rate(written, pair="USD/IDR"))

    def test_a_float_rate_refuses_because_a_binary_approximation_must_not_reach_money(self):
        with self.assertRaises(FX.FxRateInvalid) as caught:
            FX.parse_rate(0.1, pair="USD/IDR")
        self.assertEqual("FX_RATE_INVALID", caught.exception.code)

    def test_a_zero_or_negative_rate_refuses(self):
        for written in ("0", "-16250"):
            with self.subTest(written=written):
                with self.assertRaises(FX.FxRateInvalid):
                    FX.parse_rate(written, pair="USD/IDR")

    def test_a_non_numeric_rate_refuses(self):
        with self.assertRaises(FX.FxRateInvalid):
            FX.parse_rate("about sixteen thousand", pair="USD/IDR")

    def test_a_rate_that_names_the_same_currency_twice_refuses(self):
        with self.assertRaises(FX.FxRateInvalid):
            FX.FxRate("USD", "USD", Decimal(RATE))

    def test_the_published_payload_is_read_into_a_normalised_table(self):
        table = FX.FxTable.from_policy({"usd/idr": RATE})
        self.assertEqual(("USD/IDR",), table.pairs())
        self.assertEqual({"rates": {"USD/IDR": RATE}}, table.to_dict())

    def test_a_payload_that_is_not_an_object_of_pairs_refuses(self):
        for payload in (None, {}, [], "USD/IDR", {"USD": RATE}, {"USD/IDR/JPY": RATE}):
            with self.subTest(payload=payload):
                with self.assertRaises(FX.FxError):
                    FX.FxTable.from_policy(payload)

    def test_a_payload_naming_an_unsupported_currency_refuses_in_this_modules_vocabulary(self):
        with self.assertRaises(FX.FxRateInvalid):
            FX.FxTable.from_policy({"USD/XYZ": RATE})

    def test_two_keys_that_normalise_to_the_same_pair_refuse(self):
        with self.assertRaises(FX.FxRateInvalid):
            FX.FxTable.from_policy({"USD/IDR": RATE, "usd/idr": RATE})


class TestConversionCrossesCurrenciesCorrectly(unittest.TestCase):
    """The exponents differ, so the conversion has to go through major units to be right."""

    def setUp(self):
        self.table = FX.FxTable.from_policy({"USD/IDR": RATE})

    def test_a_floor_in_minor_units_becomes_the_other_currencys_minor_units(self):
        # USD 25.00 (2500 minor, exponent 2) at 16,250 IDR per USD is IDR 406,250 (exponent 0).
        self.assertEqual(406_250, self.table.convert_minor(
            2500, from_currency="USD", to_currency="IDR"))

    def test_converting_a_currency_into_itself_is_the_identity_and_needs_no_rate(self):
        table = FX.FxTable.from_policy({"USD/IDR": RATE})
        self.assertEqual(999, table.convert_minor(999, from_currency="idr", to_currency="IDR"))

    def test_a_minimum_is_rounded_up_never_down(self):
        # 25.01 USD at 16,250.5 is exactly 406,425.005 IDR — a floor of 406,425 would be weaker
        # than the published one, so the ceiling applies.
        table = FX.FxTable.from_policy({"USD/IDR": FRACTIONAL_RATE})
        self.assertEqual(406_426, table.convert_minor(
            2501, from_currency="USD", to_currency="IDR"))
        self.assertEqual(406_425, table.convert_minor(
            2501, from_currency="USD", to_currency="IDR", rounding=FX.ROUND_NEAREST))

    def test_the_converted_amount_comes_back_validated_not_as_a_bare_number(self):
        table = FX.FxTable.from_policy({"USD/IDR": RATE})
        with self.assertRaises(InvalidMoney):
            table.convert_minor(2500, from_currency="USD", to_currency="XYZ")

    def test_an_unsupported_rounding_refuses_instead_of_defaulting(self):
        with self.assertRaises(FX.FxRateInvalid):
            self.table.convert_minor(2500, from_currency="USD", to_currency="IDR",
                                     rounding="ROUND_DOWN")


class TestTheOtherDirectionIsDerivedAndSaysSo(unittest.TestCase):
    """Publishing one direction answers both — and the derived one is marked, not disguised."""

    def setUp(self):
        self.table = FX.FxTable.from_policy({"USD/IDR": RATE})

    def test_the_published_direction_is_used_verbatim(self):
        rate = self.table.rate("USD", "IDR")
        self.assertEqual(Decimal(RATE), rate.rate)
        self.assertFalse(rate.derived)
        self.assertEqual({"base": "USD", "quote": "IDR", "rate": RATE, "derived": False},
                         rate.to_dict())

    def test_the_opposite_direction_is_inverted_and_marked_derived(self):
        rate = self.table.rate("IDR", "USD")
        self.assertTrue(rate.derived)
        self.assertEqual("IDR/USD", rate.key)
        self.assertEqual(Decimal(1) / Decimal(RATE), rate.rate)

    def test_an_inverted_pair_converts_back_to_where_it_started(self):
        self.assertEqual(100, self.table.convert_minor(
            16_250, from_currency="IDR", to_currency="USD"))

    def test_has_answers_for_both_directions_and_refuses_a_pair_it_cannot(self):
        self.assertTrue(self.table.has("USD", "IDR"))
        self.assertTrue(self.table.has("IDR", "USD"))
        self.assertFalse(self.table.has("USD", "EUR"))

    def test_an_unpublished_pair_refuses_and_names_what_was_published(self):
        with self.assertRaises(FX.FxRateNotPublished) as caught:
            self.table.rate("USD", "EUR")
        self.assertEqual("FX_RATE_NOT_PUBLISHED", caught.exception.code)
        self.assertEqual(("USD/IDR",), tuple(caught.exception.details["published"]))


class TestNothingHereAssumesParity(unittest.TestCase):
    """The whole point: a missing rate must stop the comparison, not make it pass."""

    def test_a_table_built_from_an_empty_payload_refuses(self):
        with self.assertRaises(FX.FxError):
            FX.FxTable.from_policy({})

    def test_parity_is_never_substituted_for_a_missing_rate(self):
        table = FX.FxTable.from_policy({"USD/IDR": RATE})
        with self.assertRaises(FX.FxRateNotPublished):
            table.convert_minor(2500, from_currency="USD", to_currency="EUR")

    def test_a_rate_is_not_a_float_even_when_the_json_holds_one(self):
        payload = json.loads('{"USD/IDR": 0.1}')
        self.assertEqual(0.1, payload["USD/IDR"])  # the float survives JSON, not this module
        with self.assertRaises(FX.FxRateInvalid):
            FX.FxTable.from_policy(payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
