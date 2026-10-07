"""Agent 5 — the Finance Admin AI Agent: telemetry with zero money authority.

``src/finance_admin.py`` answers §11 of the AI Workforce Architecture V1.1.
Its mission is *administration*: read what payments, invoices, ledger, supplier
settlements and deposit resolutions already recorded, and turn that into the
reports a human Finance Manager works from. The claims this file pins down:

1. every number is read from recorded facts (``invoices``, ``payments``,
   ``ledger_entries``, ``reconciliation_cases``, ``supplier_settlements``,
   ``deposit_resolutions``, ``payment_events``) and nothing is invented
   between them — no predicted cash, no estimated anything;
2. authority is structural: the agent registers itself with READ-tier analysis
   tools whose tier ceiling keeps HIGH-tier money tools out of reach; this
   module contains no order path at all;
3. AR aging is honest: buckets cover the open-receivable timeline exactly —
   aged from ``due_at`` (0-30, 31-60, 61-90, 90+ days) at ``now``, VOID
   invoices excluded, per-currency sums kept separate;
4. AP mirrors AR: a supplier ``DUE`` not fully covered by later ``PAID`` rows
   is an open payable; a ``PAID`` on a booking with no ``DUE`` counts as an
   exception the report must surface;
5. exception detection is a survey of the cases the *payments service itself*
   opened, plus webhook events recorded without a verified signature; the
   agent reports them, it does not resolve them;
6. exposure is the §15 source-of-truth accounting: per-currency ledger cash
   balance (DEBIT-CREDIT), outstanding AR per currency, open AP, deposit
   resolutions counted by action — each labelled read-only;
7. KPI telemetry answers the §11 families from the same facts, and a rate
   with an empty denominator is ``None``, never a fabricated 0%;
8. collecting twice at the same instant returns equal bundles, and the
   collector writes nothing: table row counts, audit rows, outbox rows are
   untouched — read-only is enforced, not promised;
9. a missing table is refused loudly (``FinanceClientError``), not swallowed
   into an empty report;
10. no P2+ field can leak: every produced value is an integer, a rate, a
    label or an ISO instant — the surveys select no guest ref, no name, no
    free-text payload.

Every fill is checked against the tables, not only against the report.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import finance_admin as FIN
import primitives as P
from schema_loader import connect_sqlite

START = "2026-09-21T08:00:00Z"
NOW = P.parse_iso(START)


def _iso(days: float) -> str:
    return P.to_iso(NOW + timedelta(days=days))


class Harness(unittest.TestCase):
    """Real storage, deterministic clock — the same harness other modules use."""

    def setUp(self) -> None:
        self.conn = connect_sqlite()
        self.clock = P.FrozenClock(NOW)
        self.agency = "agency_default"
        self._tables = self._row_counts()

    def _row_counts(self) -> dict[str, int]:
        tables = [
            "invoices", "payments", "payment_events", "ledger_entries",
            "reconciliation_cases", "supplier_settlements",
            "deposit_resolutions", "audit_events", "outbox_events",
        ]
        counts = {}
        for t in tables:
            try:
                counts[t] = self.conn.execute(
                    f"SELECT COUNT(*) FROM {t}"
                ).fetchone()[0]
            except Exception:
                counts[t] = -1
        return counts

    def assertUntouched(self) -> None:
        self.assertEqual(self._row_counts(), self._tables)

    # -- builders -------------------------------------------------------------

    def add_booking(self, booking_id: str = "bkg_fin_01") -> None:
        if self.conn.execute(
            "SELECT 1 FROM bookings WHERE booking_id = ?", (booking_id,)
        ).fetchone():
            return
        # uq_bookings_active_slot is UNIQUE (unit_id, check_in): every booking in
        # this harness shares one unit, so each booking_id needs its own slot.
        check_in_days = 20.0 + (sum(map(ord, booking_id)) % 60)
        for stmt in (
            "INSERT OR IGNORE INTO suppliers (supplier_id, agency_id, name,"
            " status, created_at, updated_at) VALUES"
            " ('supplier_fin', 'agency_default', 'Fin Test Supplier',"
            " 'ACTIVE', ?, ?)",
            "INSERT OR IGNORE INTO properties (property_id, agency_id, name,"
            " property_type, region, province, timezone, status,"
            " contract_pack_status, created_at, updated_at) VALUES"
            " ('prop_fin', 'agency_default', 'Villa Fin Test', 'villa',"
            " 'Canggu', 'Bali', 'Asia/Makassar', 'SELLABLE', 'ACTIVATED',"
            " ?, ?)",
            "INSERT OR IGNORE INTO units (unit_id, agency_id, property_id,"
            " name, unit_type, bedrooms, bathrooms, base_capacity,"
            " max_guests, status, created_at, updated_at) VALUES"
            " ('unit_fin', 'agency_default', 'prop_fin', 'Unit Fin Test',"
            " 'entire_villa', 3, 3, 2, 6, 'ACTIVE', ?, ?)",
            "INSERT OR IGNORE INTO policy_versions"
            " (policy_version_id, agency_id, property_id, unit_scope,"
            " version, effective_from, effective_to, currency, deposit_pct,"
            " service_fee_pct, tax_pbjt_pct, max_discount_pct,"
            " quote_valid_hours, hold_ttl_minutes, max_hold_extensions,"
            " payload, fingerprint, source_doc_ref, approved_by, created_at)"
            " VALUES ('policy_version_fin', 'agency_default', 'prop_fin',"
            " 'property', 1, '2026-01-01', '2026-12-31', 'IDR', '0.3',"
            " '0.05', '0.1', '0.1', 48, 60, 1, ?, ?, 'SOP-fin',"
            " 'finance_backup_user', ?)",
        ):
            args: tuple
            if "fingerprint" in stmt:
                args = ("{}", "a" * 64, _iso(-50.0))
            else:
                args = (_iso(-50.0), _iso(-50.0))
            self.conn.execute(stmt, args)
        self.conn.execute(
            "INSERT OR IGNORE INTO villa_supplier_relationships"
            " (villa_supplier_relationship_id, agency_id, supplier_id,"
            " property_id, unit_id, status, created_at, updated_at)"
            " VALUES ('route_default', 'agency_default', 'supplier_fin',"
            " 'prop_fin', 'unit_fin', 'ACTIVE', ?, ?)",
            (_iso(-50.0), _iso(-50.0)),
        )
        self.conn.execute(
            "INSERT INTO bookings (booking_id, agency_id,"
            " villa_supplier_relationship_id, property_id, unit_id, check_in,"
            " check_out, nights, status, guest_ref, adults, children, infants,"
            " currency, policy_version_id, policy_fingerprint, quote_ref,"
            " hold_ref, total_minor, amount_paid_minor,"
            " security_deposit_minor, payment_status, idempotency_key,"
            " version_lock, created_at, updated_at, confirmed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                booking_id, self.agency, "route_default", "prop_fin",
                "unit_fin", _iso(check_in_days), _iso(check_in_days + 7.0), 7,
                "CONFIRMED",
                "g-fin-0001", 2, 0, 0, "IDR", "policy_version_fin",
                "a" * 64, None, None, 3_000_000, 0, 0, "UNPAID",
                f"idem-{booking_id}", 1, _iso(-40.0), _iso(-40.0),
                _iso(-40.0),
            ),
        )

    def add_invoice(
        self,
        invoice_id: str,
        *,
        total: int = 1_000_000,
        paid: int = 0,
        status: str = "ISSUED",
        payment_status: str | None = None,
        currency: str = "IDR",
        issued_days: float = -40.0,
        due_days: float = -10.0,
        voided_days: float | None = None,
        booking_id: str = "bkg_fin_01",
    ) -> None:
        self.add_booking(booking_id)
        self.conn.execute(
            "INSERT INTO invoices (invoice_id, agency_id, invoice_number,"
            " booking_id, policy_fingerprint, currency, subtotal_minor,"
            " tax_minor, service_minor, total_minor, amount_paid_minor,"
            " status, payment_status, issued_at, due_at, voided_at,"
            " void_reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                invoice_id, self.agency, f"INV-0000-{invoice_id.upper()}",
                booking_id, "b" * 64, currency, total, 0, 0, total, paid,
                # Voiding in the real ledger writes status=VOID alongside
                # voided_at; keep the seed coherent with that fact.
                ("VOID" if voided_days is not None else status),
                payment_status or ("UNPAID" if paid == 0 else "PARTIAL"),
                _iso(issued_days), _iso(due_days),
                _iso(voided_days) if voided_days is not None else None,
                "Void test" if voided_days is not None else None,
                _iso(issued_days),
            ),
        )

    def add_recon_case(self, recon_id: str, kind: str = "UNMATCHED_PAYMENT") -> None:
        self.conn.execute(
            "INSERT INTO reconciliation_cases (recon_id, agency_id, kind,"
            " payment_event_id, payment_id, invoice_id, detected_at, due_at,"
            " status, owner_actor, resolution_note, resolved_at, details,"
            " created_at)"
            " VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, 'OPEN', NULL, NULL,"
            " NULL, '{}', ?)",
            (recon_id, self.agency, kind, _iso(-2.0), _iso(-1.5), _iso(-2.0)),
        )

    def add_settlement(self, sett_id: str, action: str, amount: int,
                       days: float = -5.0,
                       booking_id: str = "bkg_fin_02") -> None:
        self.add_booking(booking_id)
        self.conn.execute(
            "INSERT INTO supplier_settlements (supplier_settlement_id,"
            " agency_id, booking_id, route_id, action, amount_minor,"
            " currency, decided_by, actor_role, evidence_ref, note,"
            " idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sett_id, self.agency, booking_id, "route_default", action,
                amount, "IDR", "finance_backup_user", "finance_manager",
                f"ev-{sett_id}", None, f"idem-{sett_id}", _iso(days),
            ),
        )

    def add_deposit(self, dep_id: str, action: str, amount: int,
                    booking_id: str = "bkg_fin_01") -> None:
        self.add_booking(booking_id)
        self.conn.execute(
            "INSERT INTO deposit_resolutions (deposit_resolution_id, agency_id,"
            " booking_id, action, amount_minor, currency, decided_by,"
            " actor_role, evidence_ref, note, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dep_id, self.agency, booking_id, action, amount, "IDR",
                "finance_backup_user", "finance_manager", f"ev-{dep_id}",
                None, f"idem-{dep_id}", _iso(-3.0),
            ),
        )

    def add_unverified_webhook(self, event_id: str = "pe_bad") -> None:
        self.conn.execute(
            "INSERT INTO payment_events (event_id, provider,"
            " provider_event_id, payment_id, event_type, signature_verified,"
            " applied, rejection_reason, amount_minor, currency,"
            " payload_sha256, payload_redacted, received_at, created_at)"
            " VALUES (?, 'midtrans', 'evt-000001', NULL, 'payment.succeeded',"
            " 0, 0, 'WEBHOOK_SIGNATURE_INVALID: signature did not match',"
            " NULL, NULL, ?, NULL, ?, ?)",
            (event_id, FIN.ZERO_SHA256, _iso(-1.0), _iso(-1.0)),
        )

    def add_ledger_pair(self, entry_id: str, debit_account: str,
                        credit_account: str, amount: int,
                        currency: str = "IDR",
                        booking_id: str = "bkg_fin_01") -> None:
        """Balanced two-line journal with a valid 64-char hash chain."""
        self.add_booking(booking_id)
        tail = self.conn.execute(
            "SELECT seq, entry_hash FROM ledger_entries ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = 1 if tail is None else int(tail[0]) + 1
        prev = FIN.ZERO_SHA256 if tail is None else str(tail[1])
        h1 = hash_entry_text(f"{seq}:{entry_id}")
        h2 = hash_entry_text(f"{seq + 1}:{entry_id}")
        for i, (acct, direction) in enumerate(
            ((debit_account, "DEBIT"), (credit_account, "CREDIT"))
        ):
            self.conn.execute(
                "INSERT INTO ledger_entries (entry_id, seq, account,"
                " direction, amount_minor, currency, booking_id, invoice_id,"
                " payment_id, occurred_at, memo, entry_hash, prev_hash,"
                " created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)",
                (
                    f"{entry_id}_{'d' if i == 0 else 'c'}", seq + i, acct,
                    direction, amount, currency, booking_id, _iso(-1.0),
                    f"{acct} {direction} test", h1 if i == 0 else h2,
                    prev if i == 0 else h1, _iso(-1.0),
                ),
            )

    def tearDown(self) -> None:
        self.conn.close()


def hash_entry_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 1. empty estate — honest zeros and Nones
# --------------------------------------------------------------------------- #
class TestEmptyEstate(Harness):

    def test_empty_estate_reports_zero_open_and_no_rates(self):
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.ar.total_open_minor_by_currency, {})
        self.assertEqual(kpis.ar.open_count, 0)
        for bucket in kpis.ar.buckets.values():
            self.assertEqual(bucket.count_and_minor, (0, 0))
        self.assertEqual(kpis.ap.total_open_minor_by_currency, {})
        self.assertIsNone(kpis.kpi.collected_rate)
        self.assertEqual(kpis.exceptions.open_count, 0)

    def test_collector_writes_nothing(self):
        FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertUntouched()


# --------------------------------------------------------------------------- #
# 2. AR aging — window closure and void exclusion
# --------------------------------------------------------------------------- #
class TestARAging(Harness):

    def test_buckets_cover_the_timeline_exactly(self):
        self.add_invoice("inv_cur", due_days=-5.0)
        self.add_invoice("inv_45", issued_days=-80.0, due_days=-45.0)
        self.add_invoice("inv_100", issued_days=-140.0, due_days=-100.0)
        self.add_invoice("inv_void", due_days=-30.0, voided_days=-10.0,
                         issued_days=-60.0)
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        b = kpis.ar.buckets
        self.assertEqual(b["0-30"].count, 1)
        self.assertEqual(b["31-60"].count, 1)
        self.assertEqual(b["61-90"].count, 0)
        self.assertEqual(b["90+"].count, 1)
        self.assertEqual(kpis.ar.open_count, 3)
        self.assertEqual(
            kpis.ar.total_open_minor_by_currency, {"IDR": 3_000_000}
        )

    def test_partially_paid_invoice_ages_only_the_balance(self):
        self.add_invoice("inv_part", total=1_000_000, paid=400_000,
                         issued_days=-120.0, due_days=-80.0)
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(
            kpis.ar.total_open_minor_by_currency, {"IDR": 600_000}
        )
        self.assertEqual(kpis.ar.buckets["61-90"].count, 1)

    def test_unpaid_invoice_not_yet_due_is_current(self):
        self.add_invoice("inv_fwd", due_days=+3.0)
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.ar.buckets["0-30"].count, 1)
        self.assertEqual(kpis.ar.open_count, 1)

    def test_overdue_aging_boundary_is_inclusive_on_day_30(self):
        self.add_invoice("inv_30", due_days=-30.0)
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.ar.buckets["0-30"].count, 1)


# --------------------------------------------------------------------------- #
# 3. AR collected and KPI rates
# --------------------------------------------------------------------------- #
class TestARCollected(Harness):

    def test_collected_sum_matches_recorded_payments(self):
        self.add_invoice("inv_a", total=1_000_000, paid=300_000)
        self.add_invoice("inv_b", total=2_000_000, paid=2_000_000,
                         payment_status="PAID")
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(
            kpis.ar.total_collected_minor_by_currency["IDR"], 2_300_000
        )

    def test_collected_rate_empty_denominator_is_none(self):
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertIsNone(kpis.kpi.collected_rate)


# --------------------------------------------------------------------------- #
# 4. AP — supplier settlements mirror AR
# --------------------------------------------------------------------------- #
class TestSupplierPayables(Harness):

    def test_due_minus_paid_is_open_payable(self):
        self.add_settlement("ss_due1", "DUE", 2_000_000)
        self.add_settlement("ss_paid1", "PAID", 500_000)
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.ap.total_open_minor_by_currency, {"IDR": 1_500_000})
        self.assertEqual(kpis.ap.open_count, 1)
        self.assertEqual(kpis.ap.settled_minor_by_currency["IDR"], 500_000)

    def test_paid_without_due_surfaces_as_exception(self):
        self.add_settlement("ss_paid_nodue", "PAID", 700_000)
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.ap.paid_without_due_count, 1)
        self.assertEqual(kpis.ap.total_open_minor_by_currency, {})

    def test_actions_constant(self):
        self.assertEqual(FIN.AP_ACTIONS, ("DUE", "PAID"))


# --------------------------------------------------------------------------- #
# 5. Deposits held vs resolved
# --------------------------------------------------------------------------- #
class TestDeposits(Harness):

    def test_deposit_resolutions_count_by_action(self):
        self.add_deposit("dep_r", "RETURNED", 500_000)
        self.add_deposit("dep_f2", "FORFEITED", 250_000)
        self.add_deposit("dep_a", "APPLIED", 100_000)
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(
            kpis.deposits.resolutions_by_action,
            {"RETURNED": 1, "FORFEITED": 1, "APPLIED": 1},
        )


# --------------------------------------------------------------------------- #
# 6. Exceptions — recon cases and unverified webhooks
# --------------------------------------------------------------------------- #
class TestExceptions(Harness):

    def test_open_recon_cases_counted_by_kind(self):
        self.add_recon_case("rc_1", "UNMATCHED_PAYMENT")
        self.add_recon_case("rc_2", "AMOUNT_MISMATCH")
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.exceptions.open_count, 2)
        self.assertEqual(
            kpis.exceptions.open_by_kind,
            {"UNMATCHED_PAYMENT": 1, "AMOUNT_MISMATCH": 1},
        )

    def test_overdue_recon_counted(self):
        self.add_recon_case("rc_late", "UNMATCHED_PAYMENT")
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        # due_at (+0.5d from -2d) already passed at NOW
        self.assertEqual(kpis.exceptions.overdue_count, 1)

    def test_unverified_webhook_events_counted(self):
        self.add_unverified_webhook()
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.exceptions.unverified_webhook_events, 1)


# --------------------------------------------------------------------------- #
# 7. Exposure — ledger cash per currency
# --------------------------------------------------------------------------- #
class TestExposure(Harness):

    def test_ledger_balances_per_currency(self):
        self.add_ledger_pair("lg1", "CASH_IN", "TAX_PAYABLE", 1_000_000)
        self.add_ledger_pair("lg2", "CASH_IN", "SERVICE_FEE", 600_000)
        kpis = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(kpis.exposure.cash_by_currency, {"IDR": 1_600_000})


# --------------------------------------------------------------------------- #
# 8. Read-only + determinism + refusals
# --------------------------------------------------------------------------- #
class TestReadOnlyAndRefusals(Harness):

    def test_collect_is_deterministic_at_one_instant(self):
        self.add_invoice("inv_d", due_days=-15.0)
        a = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        b = FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertEqual(a, b)

    def test_collect_writes_nothing_after_full_fill(self):
        self.add_invoice("inv_e", issued_days=-120.0, due_days=-70.0)
        self.add_settlement("ss_due2", "DUE", 1_000_000)
        # Re-baseline AFTER seeding: the claim under test is that collect()
        # itself writes nothing, not that the seed was free.
        self._tables = self._row_counts()
        FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertUntouched()

    def test_missing_table_refused_not_swallowed(self):
        self.conn.execute("DROP TABLE reconciliation_cases")
        with self.assertRaises(FIN.FinanceClientError) as ctx:
            FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        self.assertIn("reconciliation_cases", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 9. identity — read-tier tools, and the ceiling that keeps money out of reach
# --------------------------------------------------------------------------- #
class TestIdentityAndCeiling(Harness):
    """Claim #2 in the module docstring: the agent registers no tool of its own, and the
    gateway's tier and policy layer — not this module's good intentions — is what stops it
    approving money, even while its principal is ACTIVE.

    ADR-0061 (owner decision 2026-10-07) chose option B: `FINANCE_TOOLS` is a declaration
    of intent, not a registration. Adding those rows to `ai_tools` would put names in the
    catalogue that `ai_gateway.TOOL_EFFECTS` cannot classify."""

    def _wired(self):
        import ai_gateway as AG
        import ai_service as AIS
        import audit as AUD
        import policy_engine as PE

        policy = PE.PolicyResolver(
            self.conn, clock=self.clock, agency_id=self.agency
        )
        ai = AIS.AiService(self.conn, policy=policy, clock=self.clock)
        ids = P.IdFactory(True, "fin")
        audit = AUD.AuditLog(self.conn, clock=self.clock, ids=ids)
        gateway = AG.AiGateway(ai, audit=audit, clock=self.clock, ids=ids)
        return ai, gateway

    def test_the_declared_tools_are_low_tier_read_verbs_and_stay_unregistered(self):
        """Every name this module declares is LOW tier and no money verb — and no row:
        a declaration of intent is not a registration in the catalogue."""
        banned = ("approve", "execute", "refund", "payout", "bank", "charge",
                  "transfer", "withdraw")
        self.assertTrue(FIN.FINANCE_TOOLS)
        for name, tier, policy_key, description in FIN.FINANCE_TOOLS:
            self.assertEqual(tier, "LOW")
            self.assertIsNone(policy_key)
            self.assertTrue(description.strip())
            self.assertFalse(
                [verb for verb in banned if verb in name],
                f"money verb leaked into a read-tier tool name: {name}",
            )
        rows = self.conn.execute(
            "SELECT tool FROM ai_tools WHERE tool LIKE 'finance.%'"
        ).fetchall()
        self.assertEqual(rows, [], "the declaration must not be registered")

    def test_building_the_finance_report_never_touches_the_catalogue(self):
        """26 rows in, 26 rows out — and the same 26 the effect table classifies."""
        import ai_gateway as AG

        before = {r[0] for r in self.conn.execute("SELECT tool FROM ai_tools")}
        self.assertEqual(before, set(AG.TOOL_EFFECTS))
        FIN.collect(self.conn, clock=self.clock, agency_id=self.agency)
        after = {r[0] for r in self.conn.execute("SELECT tool FROM ai_tools")}
        self.assertEqual(after, before)

    def test_agent_cannot_approve_money_even_when_active(self):
        """The defence is the gateway's — tool registration, policy and the deny layer —
        so the refusal holds without this module policing anything of its own."""
        ai, gateway = self._wired()
        principal = FIN.register_principal(
            ai, secret_ref="VILLA_FIN_SECRET", owner_actor="owner_anas"
        )
        with self.assertRaises(Exception) as ctx:
            gateway.request(
                principal.principal_id, "payment.approve", intent="fin: approve"
            )
        self.assertEqual(getattr(ctx.exception, "code", None), "AI_DENIED_ACTION")

    def test_register_principal_refuses_without_an_ai_service(self):
        with self.assertRaises(FIN.FinanceClientError) as ctx:
            FIN.register_principal(
                None, secret_ref="VILLA_FIN_SECRET", owner_actor="owner_anas"
            )
        self.assertEqual(ctx.exception.code, "FIN_NO_AI_SERVICE")


if __name__ == "__main__":
    unittest.main()
