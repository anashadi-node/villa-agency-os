"""Tests for ``src/policy_engine.py`` — the single home for business numbers.

Focus: a quote must be reproducible, so the rule set must be immutable,
fingerprint-stable, bounded exactly like the database, and unambiguous in time.
"""

from __future__ import annotations

import copy
import json
import unittest

import policy_engine as PE
from primitives import DomainError


def payload_template(**overrides):
    """A plain (mutable) payload template — the frozen one cannot be edited."""
    base = {
        "extra_guest_fee_minor": 250_000,
        "cleaning_fee_minor": 500_000,
        "security_deposit_minor": 2_000_000,
        "min_stay_nights": 2,
        "cancellation_ladder": [
            {"min_days_before": 30, "refund_pct": "100"},
            {"min_days_before": 14, "refund_pct": "50"},
            {"min_days_before": 0, "refund_pct": "0"},
        ],
    }
    base.update(overrides)
    return base


def rules(**overrides):
    base = dict(
        currency="IDR",
        deposit_pct="30",
        service_fee_pct="5",
        tax_pbjt_pct="10",
        max_discount_pct="15",
        quote_valid_hours=48,
        hold_ttl_minutes=720,
        max_hold_extensions=2,
        payload=payload_template(),
    )
    base.update(overrides)
    return PE.PolicyRules(**base)


def version(**overrides):
    base = dict(
        policy_version_id="policy_v1",
        property_id="prop_canggu_1",
        unit_scope="property",
        version=1,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        rules=rules(),
        created_at="2026-09-21T00:00:00+00:00",
        source_doc_ref="policy/2026-Q1-villa-canggu.pdf",
        approved_by="user:owner-1",
    )
    base.update(overrides)
    return PE.PolicyVersion(**base)


class TestPayloadValidation(unittest.TestCase):
    """An unknown payload key is a typo that would silently price a stay at zero."""

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            rules(payload=payload_template(extra_guest_fee=250_000))  # missing the _minor suffix
        self.assertEqual(ctx.exception.code, "POLICY_PAYLOAD_UNKNOWN_KEY")

    def test_missing_required_key_is_rejected(self):
        payload = payload_template()
        del payload["min_stay_nights"]
        with self.assertRaises(DomainError) as ctx:
            rules(payload=payload)
        self.assertEqual(ctx.exception.code, "POLICY_PAYLOAD_INCOMPLETE")

    def test_money_keys_must_be_integer_minor_units(self):
        for bad in ("250000", 250_000.0, True, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError) as ctx:
                    rules(payload=payload_template(extra_guest_fee_minor=bad))
                self.assertEqual(ctx.exception.code, "POLICY_PAYLOAD_INVALID")

    def test_cancellation_ladder_must_be_complete_and_unique(self):
        with self.assertRaises(DomainError) as ctx:
            rules(payload=payload_template(cancellation_ladder=[{"min_days_before": 7}]))
        self.assertEqual(ctx.exception.code, "POLICY_PAYLOAD_INVALID")

        duplicate = [
            {"min_days_before": 7, "refund_pct": "50"},
            {"min_days_before": 7, "refund_pct": "10"},
        ]
        with self.assertRaises(DomainError):
            rules(payload=payload_template(cancellation_ladder=duplicate))

    def test_empty_ladder_is_rejected(self):
        with self.assertRaises(DomainError):
            rules(payload=payload_template(cancellation_ladder=[]))

    def test_payload_is_normalised_to_sorted_keys(self):
        payload = rules().payload
        self.assertEqual(list(payload), sorted(payload))


class TestPercentParsing(unittest.TestCase):
    def test_exact_decimals_are_kept(self):
        self.assertEqual(str(PE.parse_pct("7.5", field_name="p")), "7.5")
        self.assertEqual(str(PE.parse_pct("30", field_name="p")), "30")

    def test_floats_and_scientific_notation_are_refused(self):
        for bad in (7.5, "7.5e1", "1e2", True, None, "abc", "-5", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError) as ctx:
                    PE.parse_pct(bad, field_name="p")
                self.assertEqual(ctx.exception.code, "POLICY_INVALID_PERCENT")

    def test_bounds_are_enforced_with_a_specific_code(self):
        with self.assertRaises(DomainError) as ctx:
            PE.parse_pct("11", field_name="tax_pbjt_pct", bounds=("0", "10"))
        self.assertEqual(ctx.exception.code, "POLICY_PERCENT_OUT_OF_RANGE")

    def test_rules_mirror_the_database_check_bounds(self):
        """These bounds are the DB CHECKs; drift here means a write that fails late."""
        for field, bad in (
            ("deposit_pct", "101"),
            ("service_fee_pct", "25.5"),
            ("tax_pbjt_pct", "11"),
            ("max_discount_pct", "50.5"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(DomainError):
                    rules(**{field: bad})


class TestPolicyRules(unittest.TestCase):
    def test_ratios_are_exact(self):
        r = rules(deposit_pct="33.33")
        self.assertEqual(str(r.deposit_ratio), "0.3333")
        self.assertEqual(r.min_stay_nights, 2)
        self.assertEqual(r.extra_guest_fee.minor_units, 250_000)
        self.assertEqual(r.cleaning_fee.minor_units, 500_000)
        self.assertEqual(r.security_deposit.minor_units, 2_000_000)

    def test_limits_must_be_integers_inside_the_schema_bounds(self):
        for field in ("quote_valid_hours", "hold_ttl_minutes", "max_hold_extensions"):
            for bad in (True, "2", None):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(DomainError) as ctx:
                        rules(**{field: bad})
                    self.assertEqual(ctx.exception.code, "POLICY_INVALID_LIMIT")

    def test_limits_outside_the_schema_bounds_get_their_own_code(self):
        for field, bad in (
            ("quote_valid_hours", 0),
            ("quote_valid_hours", 169),
            ("hold_ttl_minutes", 14),
            ("hold_ttl_minutes", 1441),
            ("max_hold_extensions", -1),
            ("max_hold_extensions", 4),
        ):
            with self.subTest(field=field, bad=bad):
                with self.assertRaises(DomainError) as ctx:
                    rules(**{field: bad})
                self.assertEqual(ctx.exception.code, "POLICY_LIMIT_OUT_OF_RANGE")

    def test_zero_extensions_is_legal(self):
        self.assertEqual(rules(max_hold_extensions=0).max_hold_extensions, 0)

    def test_limit_bounds_mirror_the_schema_exactly(self):
        """The code bounds and the DB CHECKs must not drift apart."""
        import schema_loader

        model = schema_loader.parse_schema_model("sqlite")
        exprs = [expr for _name, expr in model.tables["policy_versions"].checks]
        for field, (low, high) in PE._LIMIT_BOUNDS.items():  # type: ignore[attr-defined]
            expr = next(expr for expr in exprs if expr.startswith(f"{field} between"))
            self.assertEqual(expr, f"{field} between {low} and {high}")

    def test_unknown_currency_fails_at_construction(self):
        with self.assertRaises(DomainError):
            rules(currency="ZZZ")

    def test_fingerprint_is_stable_and_rule_sensitive(self):
        a, b = rules(), rules()
        self.assertEqual(a.fingerprint(), b.fingerprint())
        self.assertNotEqual(a.fingerprint(), rules(deposit_pct="29").fingerprint())
        self.assertNotEqual(a.fingerprint(), rules(currency="USD").fingerprint())
        self.assertNotEqual(
            a.fingerprint(),
            rules(payload=payload_template(cleaning_fee_minor=501_000)).fingerprint(),
        )

    def test_fingerprint_ignores_dict_key_order(self):
        reordered = payload_template()
        self.assertEqual(
            sorted(reordered),
            sorted(payload_template().keys()),
        )
        self.assertEqual(
            rules().fingerprint(),
            rules(payload=dict(reversed(list(payload_template().items())))).fingerprint(),
        )


class TestPolicyVersion(unittest.TestCase):
    def test_valid_version_exposes_currency_and_fingerprint(self):
        v = version()
        self.assertEqual(v.currency, "IDR")
        self.assertEqual(v.fingerprint, v.rules.fingerprint())
        self.assertEqual(len(v.fingerprint), 64)

    def test_scope_is_limited_to_the_two_schema_values(self):
        for bad in ("villa", "PROPERTY", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError) as ctx:
                    version(unit_scope=bad)
                self.assertEqual(ctx.exception.code, "POLICY_INVALID_SCOPE")

    def test_window_must_be_ordered(self):
        with self.assertRaises(DomainError) as ctx:
            version(effective_from="2026-12-31", effective_to="2026-01-01")
        self.assertEqual(ctx.exception.code, "POLICY_WINDOW_INVALID")

    def test_single_day_window_is_allowed(self):
        v = version(effective_from="2026-03-01", effective_to="2026-03-01")
        self.assertTrue(v.covers("2026-03-01"))
        self.assertFalse(v.covers("2026-03-02"))

    def test_version_must_be_a_positive_int(self):
        for bad in (0, -1, True, 1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError):
                    version(version=bad)

    def test_created_at_must_be_a_timezone_aware_iso_timestamp(self):
        for bad in ("2026-09-21", "2026-09-21T00:00:00", "not-a-date"):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError):
                    version(created_at=bad)

    def test_covers_is_inclusive_on_both_ends(self):
        v = version()
        self.assertTrue(v.covers("2026-01-01"))
        self.assertTrue(v.covers("2026-12-31"))
        self.assertFalse(v.covers("2025-12-31"))
        self.assertFalse(v.covers("2027-01-01"))

    def test_quote_expiry_uses_the_policy_validity_hours(self):
        self.assertEqual(version().quote_expiry("2026-09-21T00:00:00+00:00"), "2026-09-23T00:00:00+00:00")

    def test_as_row_serialises_percentages_as_text_and_payload_as_json(self):
        row = version().as_row()
        self.assertEqual(row["deposit_pct"], "30")
        self.assertIsInstance(row["payload"], str)
        self.assertEqual(json.loads(row["payload"])["min_stay_nights"], 2)
        self.assertEqual(set(row), {
            "policy_version_id", "property_id", "unit_scope", "version", "effective_from",
            "effective_to", "currency", "deposit_pct", "service_fee_pct", "tax_pbjt_pct",
            "max_discount_pct", "quote_valid_hours", "hold_ttl_minutes",
            "max_hold_extensions", "payload", "fingerprint", "source_doc_ref",
            "approved_by", "created_at",
        })

    def test_provenance_does_not_change_the_fingerprint(self):
        """Re-approving identical rules must keep the same fingerprint."""
        self.assertEqual(
            version(approved_by="user:owner-2").fingerprint,
            version(approved_by="user:owner-1").fingerprint,
        )


class TestPolicyRegistry(unittest.TestCase):
    def test_active_returns_the_covering_version(self):
        registry = PE.PolicyRegistry([version()])
        self.assertIs(registry.active("prop_canggu_1", "property", "2026-06-01"), registry.get("policy_v1"))

    def test_missing_window_raises_not_found(self):
        registry = PE.PolicyRegistry([version()])
        with self.assertRaises(DomainError) as ctx:
            registry.active("prop_canggu_1", "property", "2027-01-01")
        self.assertEqual(ctx.exception.code, "POLICY_NOT_FOUND")

    def test_overlapping_windows_refuse_to_guess(self):
        registry = PE.PolicyRegistry([
            version(),
            version(policy_version_id="policy_v2", version=2, effective_from="2026-06-01", effective_to="2026-07-01"),
        ])
        with self.assertRaises(DomainError) as ctx:
            registry.active("prop_canggu_1", "property", "2026-06-15")
        self.assertEqual(ctx.exception.code, "POLICY_WINDOW_OVERLAP")
        self.assertEqual(sorted(ctx.exception.details["versions"]), ["policy_v1", "policy_v2"])

    def test_adjacent_windows_do_not_overlap(self):
        registry = PE.PolicyRegistry([
            version(effective_to="2026-06-30"),
            version(policy_version_id="policy_v2", version=2, effective_from="2026-07-01"),
        ])
        self.assertEqual(
            registry.active("prop_canggu_1", "property", "2026-07-01").policy_version_id, "policy_v2"
        )

    def test_duplicate_version_number_is_rejected(self):
        registry = PE.PolicyRegistry([version()])
        with self.assertRaises(DomainError) as ctx:
            registry.add(version(policy_version_id="policy_other"))
        self.assertEqual(ctx.exception.code, "POLICY_VERSION_DUPLICATE")

    def test_unit_scope_wins_over_property_scope(self):
        registry = PE.PolicyRegistry([
            version(),
            version(policy_version_id="policy_unit", unit_scope="unit", version=1),
        ])
        resolved = registry.resolve_for_unit("prop_canggu_1", "unit_1", "2026-06-01")
        self.assertEqual(resolved.unit_scope, "unit")

    def test_resolution_falls_back_to_property_scope(self):
        registry = PE.PolicyRegistry([version()])
        resolved = registry.resolve_for_unit("prop_canggu_1", "unit_1", "2026-06-01")
        self.assertEqual(resolved.unit_scope, "property")

    def test_unknown_property_is_not_found(self):
        registry = PE.PolicyRegistry([version()])
        with self.assertRaises(DomainError):
            registry.resolve_for_unit("prop_other", "unit_1", "2026-06-01")

    def test_versions_are_returned_in_a_stable_order(self):
        registry = PE.PolicyRegistry([
            version(policy_version_id="policy_v3", version=3, effective_from="2026-10-01", effective_to="2026-10-31"),
            version(),
        ])
        self.assertEqual([v.version for v in registry.versions()], [1, 3])


class TestImmutability(unittest.TestCase):
    def test_rules_are_frozen(self):
        with self.assertRaises(Exception):
            rules().deposit_pct = "99"  # type: ignore[misc]

    def test_payload_is_not_mutable_through_the_rule_object(self):
        r = rules()
        with self.assertRaises(TypeError):
            r.payload["min_stay_nights"] = 5  # type: ignore[index]

    def test_as_dict_returns_a_copy_so_callers_cannot_corrupt_rules(self):
        r = rules()
        snapshot = r.as_dict()
        copy.deepcopy(snapshot)
        snapshot["payload"]["min_stay_nights"] = 99
        self.assertEqual(r.min_stay_nights, 2)


class TestStoredPolicyVersion(unittest.TestCase):
    """``as_row`` -> ``from_row`` must be identity, because a price is pinned to a stored row.

    The reader is the missing inverse of the writer: without it, pricing could only be handed a
    version object a test built, never one a tenant actually has in force.
    """

    def test_a_stored_version_reads_back_as_the_version_that_was_written(self):
        written = version()
        read = PE.PolicyVersion.from_row(written.as_row())

        self.assertEqual(read.policy_version_id, written.policy_version_id)
        self.assertEqual(read.property_id, written.property_id)
        self.assertEqual(read.unit_scope, written.unit_scope)
        self.assertEqual(read.version, written.version)
        self.assertEqual(read.effective_from, written.effective_from)
        self.assertEqual(read.effective_to, written.effective_to)
        self.assertEqual(read.created_at, written.created_at)
        self.assertEqual(read.source_doc_ref, written.source_doc_ref)
        self.assertEqual(read.approved_by, written.approved_by)
        self.assertEqual(read.rules.as_dict(), written.rules.as_dict())
        self.assertEqual(read.fingerprint, written.fingerprint)
        self.assertEqual(read.currency, written.currency)

    def test_the_reader_does_not_trust_a_stored_fingerprint_column(self):
        """A drifted fingerprint column must not become the fingerprint of a live version."""

        row = version().as_row()
        row["fingerprint"] = "0" * 64

        read = PE.PolicyVersion.from_row(row)

        self.assertEqual(read.fingerprint, version().fingerprint)
        self.assertNotEqual(read.fingerprint, row["fingerprint"])

    def test_a_row_whose_payload_is_not_readable_json_is_refused(self):
        row = version().as_row()
        row["payload"] = "{not json"

        with self.assertRaises(DomainError) as ctx:
            PE.PolicyVersion.from_row(row)

        self.assertEqual(ctx.exception.code, "POLICY_PAYLOAD_UNREADABLE")

    def test_a_row_whose_rules_break_the_bounds_is_refused_not_coerced(self):
        """The same validation the writer passed runs on the way in, so drift cannot be priced."""

        row = version().as_row()
        row["deposit_pct"] = "900"

        with self.assertRaises(DomainError) as ctx:
            PE.PolicyVersion.from_row(row)

        self.assertEqual(ctx.exception.code, "POLICY_PERCENT_OUT_OF_RANGE")

    def test_a_payload_that_is_not_an_object_is_refused(self):
        row = version().as_row()
        row["payload"] = "[1, 2]"

        with self.assertRaises(DomainError) as ctx:
            PE.PolicyVersion.from_row(row)

        self.assertEqual(ctx.exception.code, "POLICY_PAYLOAD_INVALID")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
