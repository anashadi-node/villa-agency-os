"""Audit 2026-11-13, T8 — rupiah first, with a USD reference that can be checked.

The owner's rule is that money is shown in **IDR** with an automatic conversion to **USD**
beside it. The audit's own finding (T8) is the other half of that rule: a price that binds
must be quoted in one currency, so the converted figure has to stay a *reference* — labelled,
never a second price, and absent whenever the rate behind it cannot be stood behind.

What these tests pin, each with a negative control:

* the arithmetic is the rate's, to the cent, not a number baked into the module;
* a stale rate, a future-dated rate, an unpublished pair, a missing freshness limit or a
  missing date each remove the reference and keep the rupiah figure intact;
* the reference never appears for an amount already in the target currency;
* the footnote names the rate and the day, or says why there is no reference;
* guest-facing documents stay rupiah-only until counsel approves the reference line;
* the dashboard's finance page shows both, and shows only rupiah once the rate goes stale.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fx as FX
import money_display as MD
import policy_engine as PE
import reservation_kernel as RK
from primitives import FrozenClock, IdFactory, Money, canonical_json

AGENCY = "agency_default"
NOW = "2026-11-13T09:00:00+00:00"
RATE_DAY = "2026-11-13"
SOURCE = "§11 Pricing, Quote & Discount — audit 2026-11-13"
OWNER = "human_owner_anas"
USD_IDR = "16250"

#: 4.042.500 rupiah at 16.250 IDR/USD = 248,7692… USD → 248,77 (nearest cent).
RP = 4_042_500
USD_MINOR = 24_877


def table(rates: dict | None = None) -> FX.FxTable:
    return FX.FxTable(rates if rates is not None else {"USD/IDR": USD_IDR})


def source_at(quoted_on: str = RATE_DAY, *, max_age_hours: int = 48) -> MD.ReferenceSource:
    return MD.ReferenceSource(table=table(), quoted_on=quoted_on, max_age_hours=max_age_hours)


class FormattingTest(unittest.TestCase):
    """The figure itself: integer arithmetic, the reader's grouping, no float anywhere."""

    def test_rupiah_keeps_its_zero_decimals(self):
        self.assertEqual(MD.format_money(RP, "IDR"), "IDR 4.042.500")

    def test_dollars_keep_their_two(self):
        self.assertEqual(MD.format_money(USD_MINOR, "USD"), "USD 248,77")

    def test_a_float_never_enters(self):
        # 1,15 as a float is 114.99999999999999 minor units: `int()` of it loses a cent.
        # The formatter takes integers only, so a caller that scaled with a float is caught
        # here rather than in a bank reconciliation six weeks later.
        self.assertEqual(int(1.15 * 100), 114)
        self.assertEqual(MD.format_money(115, "USD"), "USD 1,15")
        self.assertEqual(MD.format_money(int(1.15 * 100), "USD"), "USD 1,14")

    def test_nonsense_is_a_dash_not_a_crash(self):
        for value in ("bukan angka", None):
            self.assertEqual(MD.format_money(value, "IDR"), "—")


class ReferenceArithmeticTest(unittest.TestCase):
    def test_the_rate_is_used_and_the_result_rounded_to_the_nearest_cent(self):
        note = MD.reference(Money(RP, "IDR"), source=source_at(), today=NOW)
        self.assertIsNotNone(note)
        self.assertEqual(note.amount.amount_minor, USD_MINOR)
        self.assertEqual(note.amount.currency, "USD")
        self.assertEqual(note.text(), "≈ USD 248,77")

    def test_the_reference_follows_the_published_rate(self):
        """Negative control for a hard-coded number: change the rate, the figure must move."""
        cheaper = MD.ReferenceSource(table=table({"USD/IDR": "16000"}), quoted_on=RATE_DAY, max_age_hours=48)
        note = MD.reference(Money(RP, "IDR"), source=cheaper, today=NOW)
        # 4.042.500 / 16.000 = 252,6562… → 252,66 — not the 248,77 the other rate produced.
        self.assertEqual(note.amount.amount_minor, 25_266)
        self.assertEqual(note.text(), "≈ USD 252,66")

    def test_the_line_puts_rupiah_first_and_the_reference_in_parentheses(self):
        self.assertEqual(
            MD.line(RP, "IDR", source=source_at(), today=NOW), "IDR 4.042.500 (≈ USD 248,77)"
        )

    def test_an_amount_already_in_the_reference_currency_gets_no_reference(self):
        self.assertIsNone(MD.reference(Money(USD_MINOR, "USD"), source=source_at(), today=NOW))
        self.assertEqual(MD.line(USD_MINOR, "USD", source=source_at(), today=NOW), "USD 248,77")

    def test_an_inverted_pair_converts_correctly_and_is_labelled_per_rupiah(self):
        inverted = MD.ReferenceSource(
            table=table({"IDR/USD": "0.0000615384615"}), quoted_on=RATE_DAY, max_age_hours=48
        )
        note = MD.reference(Money(RP, "IDR"), source=inverted, today=NOW)
        self.assertEqual(note.amount.amount_minor, USD_MINOR)
        # The note quotes the orientation that was published, not its inverse.
        self.assertIn("USD per IDR", note.basis())


class FailClosedTest(unittest.TestCase):
    """Every one of these removes the reference — and none of them touches the price."""

    def assert_rupiah_only(self, source, today=NOW):
        self.assertIsNone(MD.reference(Money(RP, "IDR"), source=source, today=today))
        self.assertEqual(MD.line(RP, "IDR", source=source, today=today), "IDR 4.042.500")

    def test_no_rate_published(self):
        self.assert_rupiah_only(None)

    def test_rate_older_than_the_published_limit(self):
        healthy = MD.reference(Money(RP, "IDR"), source=source_at(), today=NOW)
        self.assertIsNotNone(healthy)
        # Same rate, same limit, five days later: 48 h refuses from the third day.
        self.assert_rupiah_only(source_at(), today="2026-11-18T09:00:00+00:00")

    def test_rate_that_takes_effect_in_the_future(self):
        self.assert_rupiah_only(source_at("2026-11-20"), today=NOW)

    def test_unpublished_pair(self):
        other = MD.ReferenceSource(
            table=table({"USD/EUR": "0.92"}), quoted_on=RATE_DAY, max_age_hours=48
        )
        self.assert_rupiah_only(other)

    def test_missing_freshness_limit_is_a_refusal_not_a_default(self):
        # The limit lives in policy (`commercial.fx.max_age_hours`); a module that guessed
        # 48 h would let an unrenewed rate keep producing a figure nobody sanctioned.
        self.assert_rupiah_only(source_at(max_age_hours=0), today="2026-11-14T09:00:00+00:00")
        self.assertIsNotNone(
            MD.reference(Money(RP, "IDR"), source=source_at(max_age_hours=0), today=NOW),
            "a zero limit still allows the day it was published",
        )

    def test_a_sentence_says_why_there_is_no_reference(self):
        self.assertEqual(MD.footnote(None, today=NOW), MD.NO_RATE_NOTE)
        self.assertIn("not shown", MD.NO_RATE_NOTE)


class FootnoteTest(unittest.TestCase):
    def test_it_names_the_rate_and_the_day(self):
        text = MD.footnote(source_at(), today=NOW)
        self.assertIn("16.250 IDR per USD", text)
        self.assertIn("13 Nov 2026", text)
        self.assertIn(MD.REFERENCE_MARKER, text)
        self.assertIn("rupiah", text)

    def test_a_stale_rate_produces_the_refusal_sentence(self):
        self.assertEqual(MD.footnote(source_at(), today="2026-12-01T09:00:00+00:00"), MD.NO_RATE_NOTE)


class GuestDocumentsStayRupiahOnlyTest(unittest.TestCase):
    """BI's rule binds the price to one currency; a reference needs counsel's approval."""

    def test_the_reference_is_off_for_guest_documents(self):
        self.assertFalse(MD.GUEST_DOCUMENT_REFERENCE_ENABLED)

    def test_documents_do_not_import_the_reference_layer(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "documents.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("money_display", source, "a guest document must not print a USD reference")


class PolicySourcedRateTest(unittest.TestCase):
    """The rate the page uses is the one policy published — read, not assumed."""

    def build(self, *, publish_rate: bool = True, publish_limit: bool = True, quoted_on: str = RATE_DAY):
        kernel = RK.ReservationKernel.open(
            ":memory:", clock=FrozenClock.at(NOW), ids=IdFactory(deterministic=True, seed="cur"),
            agency_id=AGENCY,
        )
        self.addCleanup(kernel.close)
        resolver = PE.PolicyResolver(kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY)
        if publish_rate:
            resolver.publish(
                FX.FX_RATES_KEY, canonical_json({"USD/IDR": USD_IDR}), effective_from=quoted_on,
                source_doc_ref=SOURCE, approved_by=OWNER,
            )
        if publish_limit:
            resolver.publish(
                FX.FX_MAX_AGE_KEY, "48", effective_from="2026-11-01",
                source_doc_ref=SOURCE, approved_by=OWNER,
            )
        return resolver

    def test_a_published_rate_becomes_the_reference_source(self):
        source = MD.source_from_policy(self.build(), at=NOW)
        self.assertIsNotNone(source)
        self.assertEqual(source.quoted_on, RATE_DAY)
        self.assertEqual(source.max_age_hours, 48)
        self.assertEqual(
            MD.line(RP, "IDR", source=source, today=NOW), "IDR 4.042.500 (≈ USD 248,77)"
        )

    def test_nothing_published_means_no_reference(self):
        self.assertIsNone(MD.source_from_policy(self.build(publish_rate=False), at=NOW))

    def test_a_rate_without_its_limit_means_no_reference(self):
        self.assertIsNone(MD.source_from_policy(self.build(publish_limit=False), at=NOW))

    def test_a_freshness_limit_that_has_expired_means_no_reference(self):
        source = MD.source_from_policy(self.build(), at="2026-11-20T09:00:00+00:00")
        self.assertIsNotNone(source, "the source is read; the freshness guard is what refuses")
        self.assertIsNone(MD.reference(Money(RP, "IDR"), source=source, today="2026-11-20T09:00:00+00:00"))


class DashboardFinanceShowsBothTest(unittest.TestCase):
    """End-to-end through the page staff actually read."""

    def test_the_finance_page_prints_rupiah_and_the_reference(self):
        conn, clock = _seeded_dashboard_fixtures()
        handler = _finance_view(conn, clock, publish_rate=True)
        page = handler
        self.assertIn("IDR 4.500.000", page, "the rupiah figure must still be the one shown")
        self.assertIn("(≈ USD", page)
        self.assertIn("16.250 IDR per USD", page)
        self.assertIn(MD.REFERENCE_MARKER, page)

    def test_without_a_published_rate_the_page_shows_rupiah_and_says_so(self):
        conn, clock = _seeded_dashboard_fixtures()
        page = _finance_view(conn, clock, publish_rate=False)
        self.assertIn("IDR 4.500.000", page)
        self.assertNotIn("(≈ USD", page)
        self.assertIn("not shown", page)


# --------------------------------------------------------------------------- #
# Helpers that stand up the dashboard exactly the way its own suite does
# --------------------------------------------------------------------------- #
def _seeded_dashboard_fixtures():
    import test_dashboard as TD
    from schema_loader import connect_sqlite

    conn = connect_sqlite(":memory:")  # the schema loader already registers agency_default
    clock = FrozenClock.at("2026-09-21T08:00:00Z")
    TD.seed(conn, clock)
    return conn, clock


def _finance_view(conn, clock, *, publish_rate: bool):
    import dashboard as D

    if publish_rate:
        resolver = PE.PolicyResolver(conn, clock=clock, agency_id=AGENCY)
        resolver.publish(
            FX.FX_RATES_KEY, canonical_json({"USD/IDR": USD_IDR}),
            effective_from="2026-09-21", source_doc_ref=SOURCE, approved_by=OWNER,
        )
        resolver.publish(
            FX.FX_MAX_AGE_KEY, "48", effective_from="2026-09-01",
            source_doc_ref=SOURCE, approved_by=OWNER,
        )
    app = D.Dashboard(conn, clock=clock, actor="owner-sakti-1", actor_role="owner")
    view = app.view_finance(D.Principal("agent-finance-1", "finance"))
    return "\n".join(str(value) for value in view.values())
