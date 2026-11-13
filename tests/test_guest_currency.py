"""Guest currency default, inferred from the visitor's apparent country (ADR-0060).

The agency shows a guest exactly one of two display currencies — IDR for a guest whose
apparent country is Indonesia, USD for everyone else. What this deliberately is *not*:

* **not pricing** — no price on the marketing site, and no quote, changes with this value;
  a human confirms and sends every quote, and `quote_calculator`'s `policy.currency` remains
  the only authority for what a quote is priced in;
* **not trusted geo-location** — the hint is a request header (``CF-IPCountry`` today);
  anything it says is cosmetic and must fail *open* to USD rather than reject an inquiry,
  because a guessed country must never cost the agency a lead.

Pinned here:

* the pure mapping (``fx.infer_guest_currency``): exactly ``"ID"`` → ``"IDR"``, everything
  else — other countries, ``None``, blank, whitespace, lower-case, junk — → ``"USD"``;
* the schema: the column exists, defaults to USD, and refuses anything outside IDR/USD;
* the intake wiring: ``country_hint`` lands in the new guest's ``preferred_currency`` and is
  ignored for a guest who already exists (an upsert must not rewrite a person's record);
* the dashboard: staff see the guest's preferred currency on the case detail page.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import cases as cases_mod  # noqa: E402
import crm as CRM  # noqa: E402
import dashboard as D  # noqa: E402
import fx as FX  # noqa: E402
import marketing_site as M
from form_token_support import signed_token  # noqa: E402
from primitives import FrozenClock, IdFactory  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

CLOCK = FrozenClock.at("2026-09-21T08:00:00+00:00")


class TestInferGuestCurrency(unittest.TestCase):
    """The pure mapping: exactly ID → IDR, everything else → USD, and it never raises."""

    def test_indonesia_maps_to_idr_in_any_case_or_padding(self):
        for hint in ("ID", "id", "Id", "  ID  "):
            with self.subTest(hint=hint):
                self.assertEqual("IDR", FX.infer_guest_currency(hint))

    def test_every_other_country_maps_to_usd(self):
        for hint in ("AU", "US", "SG", "ZZ", "XX"):
            with self.subTest(hint=hint):
                self.assertEqual("USD", FX.infer_guest_currency(hint))

    def test_missing_blank_or_malformed_hints_fail_open_to_usd(self):
        for hint in (None, "", "   ", "123", "I D", "ID; DROP TABLE guests"):
            with self.subTest(hint=hint):
                self.assertEqual("USD", FX.infer_guest_currency(hint))

    def test_the_currency_vocabulary_is_exactly_two_currencies(self):
        self.assertEqual(("IDR", "USD"), FX.GUEST_CURRENCIES)


class TestGuestCurrencySchema(unittest.TestCase):
    """The column: defaults to USD, accepts only the two currencies."""

    def setUp(self):
        self.conn = connect_sqlite(":memory:")
        self.conn.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES ('agency_default', 'Default', 'ACTIVE', '2026-09-21T08:00:00+00:00',"
            " '2026-09-21T08:00:00+00:00') ON CONFLICT(agency_id) DO UPDATE SET"
            " updated_at = excluded.updated_at"
        )

    def tearDown(self):
        self.conn.close()

    def _register(self, **kwargs):
        svc = CRM.CrmService(
            self.conn, clock=CLOCK, ids=IdFactory(seed="cur"), agency_id="agency_default"
        )
        return svc.register_guest("Ada Lovelace", email="ada@example.com", **kwargs)

    def test_a_new_guest_defaults_to_usd_without_a_hint(self):
        guest_id = self._register()
        row = self.conn.execute(
            "SELECT preferred_currency FROM guests WHERE guest_id = ?", (guest_id,)
        ).fetchone()
        self.assertEqual("USD", row["preferred_currency"])

    def test_idr_is_accepted_and_stored(self):
        guest_id = self._register(preferred_currency="IDR")
        row = self.conn.execute(
            "SELECT preferred_currency FROM guests WHERE guest_id = ?", (guest_id,)
        ).fetchone()
        self.assertEqual("IDR", row["preferred_currency"])

    def test_a_currency_outside_idr_usd_is_rejected_by_the_service(self):
        with self.assertRaises(CRM.CrmError):
            self._register(preferred_currency="EUR")

    def test_the_check_constraint_rejects_eur_even_bypassing_the_service(self):
        with self.assertRaises(Exception):
            self.conn.execute(
                "INSERT INTO guests (guest_id, agency_id, full_name, email, locale,"
                " preferred_currency, status, created_at, updated_at)"
                " VALUES ('g-x', 'agency_default', 'Bad Row', 'bad@example.com', 'en-US',"
                " 'EUR', 'ACTIVE', '2026-09-21T08:00:00+00:00', '2026-09-21T08:00:00+00:00')"
            )


def _make_site(clock=CLOCK):
    """The marketing-site test rig, in miniature (same shape as tests/test_marketing_site.py)."""
    conn = connect_sqlite(":memory:")
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_default', 'Default', 'ACTIVE', '2026-09-21T08:00:00+00:00',"
        " '2026-09-21T08:00:00+00:00') ON CONFLICT(agency_id) DO UPDATE SET"
        " updated_at = excluded.updated_at"
    )
    site = M.MarketingSite(conn, clock=clock, ids=IdFactory(seed="cur-mkt"))
    return conn, site


def _valid_fields(email="budi@example.com"):
    return {
        "name": "Budi Santoso",
        "email": email,
        "message": "We are two families looking for a villa near Ubud in July.",
        "form_token": signed_token(),
        M.HONEYPOT_FIELD: "",
        "property_ref": "",
    }


def _submit(site, country_hint):
    return site.intake.submit(
        form=_valid_fields(),
        remote_addr="203.0.113.10",
        rate_limiter=M.SlidingWindowRateLimiter(CLOCK, max_events=5, window_seconds=600),
        expected_origin_host=None,
        origin_header=None,
        country_hint=country_hint,
    )


class TestIntakeCarriesTheCountryHint(unittest.TestCase):
    """submit() turns the apparent country into the new guest's display default."""

    def test_an_indonesian_visitor_becomes_an_idr_guest(self):
        conn, site = _make_site()
        _submit(site, country_hint="ID")
        row = conn.execute(
            "SELECT preferred_currency FROM guests WHERE email = 'budi@example.com'"
        ).fetchone()
        self.assertEqual("IDR", row["preferred_currency"])

    def test_any_other_visitor_becomes_a_usd_guest(self):
        conn, site = _make_site()
        _submit(site, country_hint="AU")
        row = conn.execute(
            "SELECT preferred_currency FROM guests WHERE email = 'budi@example.com'"
        ).fetchone()
        self.assertEqual("USD", row["preferred_currency"])

    def test_a_missing_header_still_admits_the_inquiry_as_usd(self):
        conn, site = _make_site()
        result = _submit(site, country_hint=None)
        self.assertIn("reference", result)  # the inquiry was not blocked
        row = conn.execute(
            "SELECT preferred_currency FROM guests WHERE email = 'budi@example.com'"
        ).fetchone()
        self.assertEqual("USD", row["preferred_currency"])

    def test_the_hint_never_touches_an_existing_guest(self):
        conn, site = _make_site()
        _submit(site, country_hint="AU")  # first inquiry: USD guest
        _submit(site, country_hint="ID")  # same person writes again from Indonesia
        rows = conn.execute(
            "SELECT preferred_currency FROM guests WHERE email = 'budi@example.com'"
        ).fetchall()
        self.assertEqual(1, len(rows))  # still one guest, not a duplicate
        self.assertEqual("USD", rows[0]["preferred_currency"])  # record unchanged


TOKENS = {"tok-owner": ("agent-owner-1", "owner")}


class TestDashboardShowsThePreferredCurrency(unittest.TestCase):
    """Staff must see which currency the guest expects, without opening the CRM."""

    def setUp(self):
        self.conn = connect_sqlite(":memory:")
        self.conn.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES ('agency_default', 'Default', 'ACTIVE', '2026-09-21T08:00:00+00:00',"
            " '2026-09-21T08:00:00+00:00') ON CONFLICT(agency_id) DO UPDATE SET"
            " updated_at = excluded.updated_at"
        )
        self.app = D.Dashboard(
            self.conn,
            clock=CLOCK,
            actor="owner-sakti-1",
            actor_role="owner",
            tokens=TOKENS,
        )
        self.owner = self.app.tokens["tok-owner"]

    def tearDown(self):
        self.conn.close()

    def _open_case_for(self, email: str, preferred_currency: str) -> str:
        ids = IdFactory(seed="cur-dash")
        cases = cases_mod.CaseService(self.conn, CLOCK, ids, agency_id="agency_default")
        svc = CRM.CrmService(
            self.conn,
            clock=CLOCK,
            ids=ids,
            agency_id="agency_default",
            case_service=cases,
        )
        guest_id = svc.register_guest(
            "Ada Lovelace", email=email, preferred_currency=preferred_currency
        )
        result = svc.open_inquiry(
            guest_id=guest_id,
            channel="website",
            body="Looking for a villa in Ubud.",
            idempotency_key=f"inq-{email}",
            content_class="P2",
        )
        return result["case_id"]

    def test_an_idr_guest_is_labelled_idr_on_the_case_detail(self):
        case_id = self._open_case_for("idr-guest@example.com", "IDR")
        detail = self.app.view_case_detail(case_id, self.owner)
        header = str(detail["case_header"])
        self.assertIn("Guest&#x27;s preferred currency", header)  # template-escaped label
        self.assertIn("<dd>IDR</dd>", header)

    def test_a_usd_guest_is_labelled_usd_on_the_case_detail(self):
        case_id = self._open_case_for("usd-guest@example.com", "USD")
        detail = self.app.view_case_detail(case_id, self.owner)
        header = str(detail["case_header"])
        self.assertIn("Guest&#x27;s preferred currency", header)  # template-escaped label
        self.assertIn("<dd>USD</dd>", header)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
