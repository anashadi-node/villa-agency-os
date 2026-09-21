"""Tests for the shared kernel primitives (``src/primitives.py``).

These are the invariants every other module relies on: money never becomes a
float, time is always UTC ISO-8601, ids and fingerprints are deterministic, and
personal data is redacted before it can reach a log line.
"""

from __future__ import annotations

import decimal
import unittest
from datetime import date, datetime, timezone

import primitives as P


class TestModuleSurface(unittest.TestCase):
    """The declared public surface must exist — no silent attribute typos."""

    def test_all_exports_resolve(self):
        missing = [name for name in P.__all__ if not hasattr(P, name)]
        self.assertEqual(missing, [], f"__all__ names without a definition: {missing}")

    def test_no_float_anywhere_in_money_path(self):
        """Money must refuse floats outright: 0.1 + 0.2 style bugs are silent."""
        with self.assertRaises(P.InvalidMoney):
            P.Money.from_major(0.1, "IDR")
        with self.assertRaises(P.InvalidMoney):
            P.Money.from_minor(100, "IDR") * 0.5


class TestMoney(unittest.TestCase):
    def test_idr_has_zero_minor_units_and_usd_two(self):
        self.assertEqual(P.Money.from_major("1500000", "IDR").minor_units, 1_500_000)
        self.assertEqual(P.Money.from_major("10.50", "USD").minor_units, 1_050)
        self.assertEqual(P.Money.from_minor(1_050, "USD").to_major_str(), "10.50")

    def test_addition_and_subtraction_preserve_currency(self):
        a = P.Money.from_major("100.00", "USD")
        b = P.Money.from_major("25.75", "USD")
        self.assertEqual((a + b).to_major_str(), "125.75")
        self.assertEqual((a - b).to_major_str(), "74.25")

    def test_cross_currency_arithmetic_is_rejected(self):
        with self.assertRaises(P.CurrencyMismatch):
            P.Money.from_major("1", "USD") + P.Money.from_major("1", "IDR")

    def test_negative_result_is_allowed_but_flagged(self):
        """Refunds and adjustments legitimately go negative; callers must see it."""
        negative = P.Money.from_major("10", "USD") - P.Money.from_major("30", "USD")
        self.assertTrue(negative.is_negative)
        self.assertEqual(negative.to_major_str(), "-20.00")

    def test_scale_rounds_half_up_deterministically(self):
        base = P.Money.from_minor(1_005, "USD")  # 10.05
        self.assertEqual(base.scale("0.10").minor_units, 101)  # 1.005 -> 1.01

    def test_pct_uses_half_up_and_never_banker_rounding(self):
        amount = P.Money.from_major("100", "USD")
        self.assertEqual(amount.pct("2.5").to_major_str(), "2.50")
        self.assertEqual(P.Money.from_major("0.05", "USD").pct("50").to_major_str(), "0.03")

    def test_split_sums_back_to_the_original(self):
        """A deposit split must never lose or invent a cent."""
        total = P.Money.from_minor(1_000_003, "IDR")
        deposit, balance = total.split("30")
        self.assertEqual((deposit + balance).minor_units, total.minor_units)

    def test_zero_and_truthiness(self):
        self.assertFalse(bool(P.Money.zero("IDR")))
        self.assertTrue(bool(P.Money.from_minor(1, "IDR")))

    def test_unknown_currency_is_rejected(self):
        with self.assertRaises(P.InvalidMoney):
            P.Money.from_minor(1, "ZZZ")

    def test_money_json_round_trip_is_exact(self):
        import json

        original = P.Money.from_minor(123_456, "IDR")
        payload = json.dumps(original, cls=P._MoneyEncoder) if hasattr(P, "_MoneyEncoder") else json.dumps(original.to_dict())
        restored = json.loads(payload)
        self.assertEqual(restored["amount_minor"], 123_456)
        self.assertEqual(restored["currency"], "IDR")


class TestDecimalHelpers(unittest.TestCase):
    def test_decimal_of_accepts_str_int_decimal_only(self):
        self.assertEqual(P.decimal_of("1.50"), decimal.Decimal("1.50"))
        self.assertEqual(P.decimal_of(3), decimal.Decimal("3"))
        with self.assertRaises(P.DomainError):
            P.decimal_of(1.5)
        with self.assertRaises(P.DomainError):
            P.decimal_of(True)

    def test_decimal_to_str_is_plain_and_stable(self):
        self.assertEqual(P.decimal_to_str(decimal.Decimal("10.5000"), 2), "10.50")
        self.assertEqual(P.decimal_to_str(decimal.Decimal("10.5")), "10.5")


class TestTime(unittest.TestCase):
    def test_iso_round_trip_is_utc_and_stable(self):
        when = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)
        text = P.to_iso(when)
        self.assertEqual(P.parse_iso(text), when)
        self.assertTrue(text.endswith("+00:00"))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(P.DomainError):
            P.to_iso(datetime(2026, 9, 21, 4, 30))

    def test_dates_round_trip(self):
        self.assertEqual(P.parse_iso_date(P.to_iso_date(date(2026, 12, 31))), date(2026, 12, 31))

    def test_nights_between_is_checkout_minus_checkin(self):
        self.assertEqual(P.nights_between(date(2026, 10, 1), date(2026, 10, 3)), 2)

    def test_zero_or_negative_stay_is_rejected(self):
        with self.assertRaises(P.DomainError):
            P.nights_between(date(2026, 10, 3), date(2026, 10, 3))
        with self.assertRaises(P.DomainError):
            P.nights_between(date(2026, 10, 3), date(2026, 10, 1))

    def test_local_date_uses_the_property_timezone(self):
        """Bali is UTC+8: a 23:30 UTC instant is already the next day locally."""
        when = datetime(2026, 9, 21, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(P.local_date(when, "Asia/Makassar"), date(2026, 9, 22))
        self.assertEqual(P.local_date(when, "UTC"), date(2026, 9, 21))

    def test_frozen_clock_is_deterministic_and_test_only(self):
        clock = P.FrozenClock.at("2026-09-21T00:00:00+00:00")
        self.assertEqual(clock.now(), datetime(2026, 9, 21, tzinfo=timezone.utc))
        clock.advance(hours=2)
        self.assertEqual(clock.today(), date(2026, 9, 21))

    def test_system_clock_returns_utc(self):
        now = P.SystemClock().now()
        self.assertIs(now.tzinfo, timezone.utc)


class TestIdentityAndFingerprints(unittest.TestCase):
    def test_require_ref_accepts_prefixed_ids(self):
        self.assertEqual(P.require_ref("hold_ABC123", "hold_id"), "hold_ABC123")

    def test_require_ref_rejects_whitespace_padding_and_empty(self):
        for bad in ("", "   ", "hold ABC", "hold\tabc"):
            with self.subTest(bad=bad):
                with self.assertRaises(P.DomainError):
                    P.require_ref(bad, "hold_id")

    def test_require_text_rejects_control_characters(self):
        with self.assertRaises(P.DomainError):
            P.require_text("bad\u0000text", "note")

    def test_id_factory_is_deterministic_when_seeded(self):
        first = P.IdFactory(deterministic=True, seed="a").new("booking")
        second = P.IdFactory(deterministic=True, seed="a").new("booking")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("booking_"))

    def test_id_factory_uses_the_requested_prefix(self):
        self.assertTrue(P.IdFactory(deterministic=True, seed="x").new("hold").startswith("hold_"))

    def test_fingerprint_is_stable_and_sensitive_to_input(self):
        """The fingerprint guards policy/quote immutability: it must be exact."""
        a = P.fingerprint({"b": 2, "a": 1}, domain="policy")
        b = P.fingerprint({"a": 1, "b": 2}, domain="policy")
        c = P.fingerprint({"a": 1, "b": 3}, domain="policy")
        self.assertEqual(a, b, "key order must not change the fingerprint")
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 64)
        self.assertEqual(a, a.lower())

    def test_canonical_json_orders_keys_and_serialises_money(self):
        text = P.canonical_json({"z": 1, "a": P.Money.from_minor(100, "IDR")})
        self.assertTrue(text.startswith('{"a":'), text)

    def test_sha256_hex_separates_parts_so_concatenation_is_unambiguous(self):
        import hashlib

        self.assertEqual(P.sha256_hex("x", "y"), hashlib.sha256(b"x\x1fy\x1f").hexdigest())
        self.assertNotEqual(P.sha256_hex("ab", "c"), P.sha256_hex("a", "bc"))


class TestPrivacyRedaction(unittest.TestCase):
    """Data classification says PII never reaches a log line — enforce it here."""

    def test_email_phone_and_identity_numbers_are_masked(self):
        text = "guest a@b.com / +628123456789 / 3273010101900001"
        cleaned = P.redact(text)
        self.assertNotIn("a@b.com", cleaned)
        self.assertNotIn("628123456789", cleaned)
        self.assertNotIn("3273010101900001", cleaned)

    def test_non_pii_text_survives_untouched(self):
        self.assertEqual(P.redact("villa canggu checkin 2026-10-01"), "villa canggu checkin 2026-10-01")

    def test_pseudonymise_is_stable_per_salt_and_not_reversible(self):
        a = P.pseudonymize("guest@example.com", salt="k1")
        b = P.pseudonymize("guest@example.com", salt="k1")
        c = P.pseudonymize("guest@example.com", salt="k2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotIn("guest@example.com", a)


class TestDomainErrors(unittest.TestCase):
    def test_error_carries_a_machine_readable_code(self):
        err = P.DomainError("boom", code="X_1", detail=2)
        payload = err.as_dict()
        self.assertEqual(payload["code"], "X_1")
        self.assertEqual(payload["details"]["detail"], 2)

    def test_error_code_defaults_are_derived_from_the_class(self):
        self.assertTrue(P.EntityNotFound("missing").code)
        self.assertTrue(P.CurrencyMismatch("mismatch").code)
        self.assertEqual(P.IDEMPOTENT_REPLAY, P.IdempotentReplay("replay").code)


class TestLimits(unittest.TestCase):
    def test_money_max_minor_is_enforced(self):
        P.Money.from_minor(P.MONEY_MAX_MINOR, "IDR")
        with self.assertRaises(P.InvalidMoney):
            P.Money.from_minor(P.MONEY_MAX_MINOR + 1, "IDR")

    def test_minor_units_table_covers_the_supported_currencies(self):
        for currency in ("IDR", "USD", "EUR", "AUD", "SGD"):
            self.assertIn(currency, P.MINOR_UNITS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
