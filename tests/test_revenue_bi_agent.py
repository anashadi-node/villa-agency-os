"""Agent 6 — the Revenue & BI AI Agent: revenue telemetry with zero write paths.

``src/revenue_bi_agent.py`` answers §10 of the AI Workforce Architecture V1.1 and
the worker catalogue frozen in ADR-0064. Slice 1 owns M1 (occupancy), M2 (ADR),
M3 (RevPAR), M6 (ALOS), M8 (cancellations) and M9 (net billed revenue, per
currency). The claims this file pins down:

1. every number is read from recorded rows — ``bookings``, ``invoices``,
   ``invoice_lines``, ``payments``, ``units``, ``availability_evidence`` — and
   nothing is invented between them;
2. occupancy has exactly one official denominator (ADR-0064 part 2):
   unit-nights of ``units.status = 'ACTIVE'`` inside the window, clipped by
   ``units.created_at``; nights sold come from ``booking_states.OCCUPYING_STATES``
   and are clipped to the window so a stay across a boundary is counted once;
3. excluded supply is reported, never dropped: maintenance/suspended unit-nights
   appear beside the ratio, and the current-status-has-no-history limitation is
   stated in the caveats;
4. the evidence-based companion occupancy stays ``None`` with a measured reason
   until ``availability_evidence.occupancy_ranges`` is actually populated —
   a fabricated number here is worse than a missing one;
5. money is per currency, always: no metric ever sums IDR and USD together, and
   ADR/RevPAR are computed against their own currency's nights;
6. empty denominators give ``None``, never a fabricated 0%;
7. the collector writes nothing and is idempotent: calling it twice at one
   instant returns equal bundles and leaves every row count untouched;
8. a missing table is refused loudly (``RevenueClientError``), not swallowed into
   an empty report;
9. no P2+ field can leak: the collector selects no ``guest_ref``, and every
   produced value is a number, a rate, a label or an ISO instant;
10. the twelve worker names are the ADR-0064 table, and the gateway — not this
    module's good intentions — keeps write verbs out of reach.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import primitives as P
import revenue_bi_agent as RBI
from schema_loader import connect_sqlite

START = "2026-09-21T08:00:00Z"
NOW = P.parse_iso(START)


def _iso(days: float) -> str:
    return P.to_iso(NOW + timedelta(days=days))


class Harness(unittest.TestCase):
    """Real storage, deterministic clock — the harness the other agents use."""

    def setUp(self) -> None:
        self.conn = connect_sqlite()
        self.clock = P.FrozenClock(NOW)
        self.agency = "agency_default"
        self._chain()
        self.mark()

    def mark(self) -> None:
        """Re-baseline for :meth:`assertUntouched` — call after adding fixtures."""
        self._tables = self._row_counts()

    def _row_counts(self) -> dict[str, int]:
        counts = {}
        for t in (
            "bookings", "invoices", "invoice_lines", "payments", "units",
            "availability_evidence", "audit_events", "outbox_events", "ai_tools",
        ):
            try:
                counts[t] = self.conn.execute(
                    f"SELECT COUNT(*) FROM {t}"
                ).fetchone()[0]
            except Exception:
                counts[t] = -1
        return counts

    def assertUntouched(self) -> None:
        self.assertEqual(self._row_counts(), self._tables)

    # -- builders -----------------------------------------------------------

    def _chain(self) -> None:
        for stmt in (
            "INSERT OR IGNORE INTO suppliers (supplier_id, agency_id, name,"
            " status, created_at, updated_at) VALUES"
            " ('supplier_rbi', 'agency_default', 'RBI Test Supplier', 'ACTIVE',"
            " ?, ?)",
            "INSERT OR IGNORE INTO properties (property_id, agency_id, name,"
            " property_type, region, province, timezone, status,"
            " contract_pack_status, created_at, updated_at) VALUES"
            " ('prop_rbi', 'agency_default', 'Villa RBI Test', 'villa',"
            " 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE', 'ACTIVATED', ?, ?)",
            "INSERT OR IGNORE INTO policy_versions (policy_version_id,"
            " agency_id, property_id, unit_scope, version, effective_from,"
            " effective_to, currency, deposit_pct, service_fee_pct,"
            " tax_pbjt_pct, max_discount_pct, quote_valid_hours,"
            " hold_ttl_minutes, max_hold_extensions, payload, fingerprint,"
            " source_doc_ref, approved_by, created_at) VALUES"
            " ('policy_rbi', 'agency_default', 'prop_rbi', 'property', 1,"
            " '2026-01-01', '2026-12-31', 'IDR', '0.3', '0.05', '0.1', '0.1',"
            " 48, 60, 1, '{}', ?, 'SOP-rbi', 'rbi_backup_user', ?)",
            "INSERT OR IGNORE INTO villa_supplier_relationships"
            " (villa_supplier_relationship_id, agency_id, supplier_id,"
            " property_id, unit_id, status, created_at, updated_at) VALUES"
            " ('route_rbi', 'agency_default', 'supplier_rbi', 'prop_rbi',"
            " NULL, 'ACTIVE', ?, ?)",
        ):
            if stmt.startswith("INSERT OR IGNORE INTO policy_versions"):
                self.conn.execute(stmt, ("a" * 64, _iso(-90.0)))
            else:
                self.conn.execute(stmt, (_iso(-90.0), _iso(-90.0)))

    def add_unit(self, unit_id: str, *, status: str = "ACTIVE",
                 created_days: float = -90.0) -> None:
        self.conn.execute(
            "INSERT INTO units (unit_id, agency_id, property_id, name,"
            " unit_type, bedrooms, bathrooms, base_capacity, max_guests,"
            " status, created_at, updated_at)"
            " VALUES (?, 'agency_default', 'prop_rbi', ?, 'entire_villa', 3, 3,"
            " 2, 6, ?, ?, ?)",
            (unit_id, f"Unit {unit_id}", status, _iso(created_days),
             _iso(created_days)),
        )

    def add_booking(self, booking_id: str, *, unit_id: str = "unit_rbi_a",
                    status: str = "CONFIRMED", in_days: float = -10.0,
                    nights: int = 7, currency: str = "IDR",
                    cancelled_at_days: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO bookings (booking_id, agency_id,"
            " villa_supplier_relationship_id, property_id, unit_id, check_in,"
            " check_out, nights, status, guest_ref, adults, children, infants,"
            " currency, policy_version_id, policy_fingerprint, total_minor,"
            " amount_paid_minor, security_deposit_minor, payment_status,"
            " idempotency_key, version_lock, created_at, updated_at,"
            " cancelled_at) VALUES (?, 'agency_default', 'route_rbi',"
            " 'prop_rbi', ?, ?, ?, ?, ?, ?, 2, 0, 0, ?, 'policy_rbi', ?,"
            " 3000000, 0, 0, 'UNPAID', ?, 1, ?, ?, ?)",
            (
                booking_id, unit_id, _iso(in_days),
                _iso(in_days + nights), nights, status, f"g-{booking_id}",
                currency, "a" * 64, f"idem-{booking_id}", _iso(in_days - 5.0),
                _iso(in_days - 5.0),
                None if cancelled_at_days is None else _iso(cancelled_at_days),
            ),
        )

    def add_invoice(self, invoice_id: str, *, booking_id: str = "book_rbi_1",
                    status: str = "ISSUED", issued_days: float = -8.0,
                    currency: str = "IDR", subtotal: int = 2_100_000,
                    tax: int = 210_000, total: int = 2_310_000) -> None:
        voided = None if status == "ISSUED" else _iso(-6.0)
        self.conn.execute(
            "INSERT INTO invoices (invoice_id, agency_id, invoice_number,"
            " booking_id, policy_fingerprint, currency, subtotal_minor,"
            " tax_minor, service_minor, total_minor, amount_paid_minor, status,"
            " payment_status, issued_at, due_at, voided_at, void_reason,"
            " created_at) VALUES (?, 'agency_default', ?, ?, ?, ?, ?, ?, 0, ?,"
            " 0, ?, 'UNPAID', ?, ?, ?, ?, ?)",
            (
                invoice_id, f"INV-{invoice_id}", booking_id, "a" * 64, currency,
                subtotal, tax, total, status, _iso(issued_days),
                _iso(issued_days + 7.0), voided,
                None if voided is None else "harness void", _iso(issued_days),
            ),
        )

    def add_line(self, line_id: str, invoice_id: str, *, kind: str = "NIGHTLY",
                 quantity: int = 7, unit_amount: int = 300_000) -> None:
        self.conn.execute(
            "INSERT INTO invoice_lines (line_id, invoice_id, kind, description,"
            " quantity, unit_amount_minor, amount_minor, rate_card_id, nights,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                line_id, invoice_id, kind, f"harness {kind} line", quantity,
                unit_amount, quantity * unit_amount, quantity if kind == "NIGHTLY"
                else None, _iso(-8.0),
            ),
        )

    def add_payment(self, payment_id: str, *, invoice_id: str = "inv_rbi_1",
                    status: str = "REFUNDED", amount: int = 500_000,
                    currency: str = "IDR", settled_days: float = -4.0) -> None:
        self.conn.execute(
            "INSERT INTO payments (payment_id, agency_id, invoice_id, provider,"
            " provider_intent_id, method, currency, amount_minor, status,"
            " created_at, expires_at, settled_at, provider_status) VALUES"
            " (?, 'agency_default', ?, 'harnesspay', ?, 'BANK_TRANSFER', ?, ?,"
            " ?, ?, NULL, ?, 'harness')",
            (
                payment_id, invoice_id, f"pi_{payment_id}_0001", currency,
                amount, status, _iso(settled_days - 1.0), _iso(settled_days),
            ),
        )

    def add_evidence(self, evidence_id: str, *, unit_id: str = "unit_rbi_a",
                     ranges: str = "[]") -> None:
        self.conn.execute(
            "INSERT INTO availability_evidence (evidence_id, agency_id, unit_id,"
            " evidence_level, source_type, source_ref, captured_at, valid_until,"
            " result, restrictions, occupancy_ranges, can_quote, can_hold,"
            " can_confirm, notes, created_at) VALUES (?, 'agency_default', ?,"
            " 'B', 'manual_ops', ?, ?, ?, 'AVAILABLE', NULL, ?, 1, 1, 0, NULL,"
            " ?)",
            (
                evidence_id, unit_id, f"ref-{evidence_id}", _iso(-1.0),
                _iso(1.0), ranges, _iso(-1.0),
            ),
        )

    def collect(self, **over):
        kwargs = {"clock": self.clock, "agency_id": self.agency}
        kwargs.update(over)
        return RBI.collect(self.conn, **kwargs)

    # -- shared wiring: the workforce's identity + gateway, as production builds it
    def _wired(self):
        import ai_gateway as AG
        import ai_service as AIS
        import audit as AUD
        import policy_engine as PE

        policy = PE.PolicyResolver(
            self.conn, clock=self.clock, agency_id=self.agency
        )
        ai = AIS.AiService(self.conn, policy=policy, clock=self.clock)
        ids = P.IdFactory(True, "rbi")
        audit = AUD.AuditLog(self.conn, clock=self.clock, ids=ids)
        gateway = AG.AiGateway(ai, audit=audit, clock=self.clock, ids=ids)
        return ai, gateway


# --------------------------------------------------------------------------- #
# 1. empty estate — honest zeros and Nones
# --------------------------------------------------------------------------- #
class TestEmptyEstate(Harness):

    def test_empty_estate_reports_zero_and_no_rates(self):
        snap = self.collect()
        self.assertEqual(snap.occupancy.booked_nights, 0)
        self.assertEqual(snap.occupancy.managed_unit_nights, 0)
        self.assertIsNone(snap.occupancy.occupancy_managed)
        self.assertIsNone(snap.occupancy.occupancy_sellable)
        self.assertIsNone(snap.alos_nights)
        self.assertIsNone(snap.cancellation_rate)
        self.assertEqual(snap.money, {})

    def test_collector_writes_nothing(self):
        self.collect()
        self.assertUntouched()

    def test_missing_table_is_refused_loudly(self):
        self.conn.execute("DROP TABLE invoice_lines")
        with self.assertRaises(RBI.RevenueClientError):
            self.collect()


# --------------------------------------------------------------------------- #
# 2. the core metrics, hand-computed
# --------------------------------------------------------------------------- #
class TestCoreMetrics(Harness):

    def setUp(self) -> None:
        super().setUp()
        self.add_unit("unit_rbi_a", created_days=-90.0)
        self.add_booking("book_rbi_1", in_days=-10.0, nights=7)
        self.add_invoice("inv_rbi_1")
        self.add_line("line_rbi_1", "inv_rbi_1", kind="NIGHTLY", quantity=7,
                      unit_amount=300_000)

    def test_occupancy_uses_the_official_managed_denominator(self):
        """ADR-0064 part 2: 7 sold nights over 1 active unit × 30 window nights."""
        occ = self.collect().occupancy
        self.assertEqual(occ.booked_nights, 7)
        self.assertEqual(occ.managed_unit_nights, 30)
        self.assertAlmostEqual(occ.occupancy_managed, 7 / 30, places=9)
        self.assertIn("ACTIVE", occ.denominator_label)

    def test_adr_revpar_and_alos_are_hand_computable(self):
        snap = self.collect()
        line = snap.money["IDR"]
        self.assertAlmostEqual(line.adr_minor, 300_000.0, places=6)
        self.assertAlmostEqual(line.revpar_minor, 2_100_000 / 30, places=6)
        self.assertAlmostEqual(snap.alos_nights, 7.0, places=6)
        self.assertEqual(line.nights, 7)

    def test_net_billed_revenue_is_the_invoice_total(self):
        snap = self.collect()
        self.assertEqual(snap.money["IDR"].invoiced_minor, 2_310_000)
        self.assertEqual(snap.money["IDR"].refunded_minor, 0)
        self.assertEqual(snap.money["IDR"].revenue_net_minor, 2_310_000)

    def test_nights_of_a_stay_across_the_window_edge_are_clipped_once(self):
        """A 7-night stay that started before the window counts only its overlap."""
        self.add_booking("book_rbi_edge", in_days=-33.0, nights=7)
        occ = self.collect().occupancy
        self.assertEqual(occ.booked_nights, 7 + 4)   # 4 nights fall inside 30 days

    def test_maintenance_unit_nights_are_reported_not_dropped(self):
        self.add_unit("unit_rbi_off", status="MAINTENANCE")
        occ = self.collect().occupancy
        self.assertEqual(occ.managed_unit_nights, 30)
        self.assertEqual(occ.unavailable_unit_nights, 30)
        self.assertAlmostEqual(occ.occupancy_managed, 7 / 30, places=9)

    def test_unit_activated_inside_the_window_counts_partial_supply(self):
        self.add_unit("unit_rbi_new", created_days=-10.0)
        occ = self.collect().occupancy
        self.assertEqual(occ.managed_unit_nights, 30 + 10)


# --------------------------------------------------------------------------- #
# 3. void and refund honesty
# --------------------------------------------------------------------------- #
class TestVoidAndRefund(Harness):

    def setUp(self) -> None:
        super().setUp()
        self.add_unit("unit_rbi_a")
        self.add_booking("book_rbi_1", in_days=-10.0, nights=7)
        self.add_invoice("inv_rbi_1")
        self.add_invoice("inv_rbi_void", status="VOID", subtotal=9_000_000,
                         tax=900_000, total=9_900_000)

    def test_void_invoice_is_excluded(self):
        self.assertEqual(self.collect().money["IDR"].invoiced_minor, 2_310_000)

    def test_refunded_payment_reduces_net_revenue(self):
        self.add_payment("pay_rbi_1", status="REFUNDED", amount=500_000)
        line = self.collect().money["IDR"]
        self.assertEqual(line.refunded_minor, 500_000)
        self.assertEqual(line.revenue_net_minor, 2_310_000 - 500_000)

    def test_pending_payment_is_not_revenue(self):
        self.add_payment("pay_rbi_pending", status="PENDING", amount=2_310_000)
        line = self.collect().money["IDR"]
        self.assertEqual(line.refunded_minor, 0)
        self.assertEqual(line.revenue_net_minor, 2_310_000)


# --------------------------------------------------------------------------- #
# 4. two currencies are two rows, never one sum
# --------------------------------------------------------------------------- #
class TestCurrencies(Harness):

    def setUp(self) -> None:
        super().setUp()
        self.add_unit("unit_rbi_a")
        self.add_booking("book_rbi_idr", in_days=-10.0, nights=7, currency="IDR")
        self.add_booking("book_rbi_usd", in_days=-9.0, nights=3, currency="USD")
        self.add_invoice("inv_rbi_idr", booking_id="book_rbi_idr")
        self.add_invoice("inv_rbi_usd", booking_id="book_rbi_usd",
                         currency="USD", subtotal=900, tax=90, total=990)
        self.add_line("line_rbi_idr", "inv_rbi_idr", quantity=7,
                      unit_amount=300_000)
        self.add_line("line_rbi_usd", "inv_rbi_usd", quantity=3, unit_amount=300)

    def test_each_currency_gets_its_own_line(self):
        snap = self.collect()
        self.assertEqual(sorted(snap.money), ["IDR", "USD"])
        self.assertEqual(snap.money["IDR"].revenue_net_minor, 2_310_000)
        self.assertEqual(snap.money["USD"].revenue_net_minor, 990)

    def test_adr_is_computed_against_its_own_currency_nights(self):
        snap = self.collect()
        self.assertAlmostEqual(snap.money["USD"].adr_minor, 300.0, places=6)
        self.assertAlmostEqual(snap.money["IDR"].adr_minor, 300_000.0, places=6)

    def test_no_cross_currency_total_exists(self):
        snap = self.collect()
        for name in ("total_minor", "revenue_total_minor", "invoiced_total"):
            self.assertFalse(hasattr(snap, name))
        self.assertEqual(sorted(snap.money), ["IDR", "USD"])


# --------------------------------------------------------------------------- #
# 5. cancellation rate on the arrival cohort
# --------------------------------------------------------------------------- #
class TestCancellation(Harness):

    def test_cancelled_arrival_is_counted_against_the_cohort(self):
        self.add_unit("unit_rbi_a")
        self.add_booking("book_ok", in_days=-10.0, nights=7)
        self.add_booking("book_cx", in_days=-5.0, nights=4, status="CANCELLED",
                         cancelled_at_days=-7.0)
        snap = self.collect()
        self.assertEqual(snap.arrivals_in_window, 2)
        self.assertAlmostEqual(snap.cancellation_rate, 0.5, places=9)
        self.assertEqual(snap.occupancy.booked_nights, 7)   # cancelled is not sold


# --------------------------------------------------------------------------- #
# 6. read-only, idempotent, and P2-free
# --------------------------------------------------------------------------- #
class TestDiscipline(Harness):

    def setUp(self) -> None:
        super().setUp()
        self.add_unit("unit_rbi_a")
        self.add_booking("book_rbi_1", in_days=-10.0, nights=7)
        self.add_invoice("inv_rbi_1")
        self.mark()

    def test_collect_is_idempotent_and_writes_nothing(self):
        first = self.collect()
        second = self.collect()
        self.assertEqual(first, second)
        self.assertUntouched()

    def test_no_guest_reference_leaks_into_the_bundle(self):
        snap = self.collect()
        blob = repr(snap)
        self.assertNotIn("g-book_rbi_1", blob)
        for key in ("guest_ref", "guest_id", "channel_ref"):
            self.assertNotIn(key, blob)

    def test_every_metric_carries_lineage(self):
        snap = self.collect()
        for metric in ("M1", "M2", "M3", "M6", "M8", "M9"):
            self.assertIn(metric, snap.lineage)
            entry = snap.lineage[metric]
            self.assertTrue(entry["tables"])
            self.assertEqual(entry["window_days"], 30)


# --------------------------------------------------------------------------- #
# 7. the evidence companion stays honest while it cannot be computed
# --------------------------------------------------------------------------- #
class TestEvidenceCompanion(Harness):

    def test_without_evidence_the_companion_is_none_with_a_reason(self):
        self.add_unit("unit_rbi_a")
        occ = self.collect().occupancy
        self.assertIsNone(occ.occupancy_sellable)
        self.assertIsNotNone(occ.sellable_unavailable_reason)
        self.assertEqual(occ.evidence_rows_with_ranges, 0)

    def test_evidence_rows_are_counted_even_when_they_carry_no_ranges(self):
        self.add_unit("unit_rbi_a")
        self.add_evidence("ev_rbi_1")
        occ = self.collect().occupancy
        self.assertEqual(occ.evidence_rows, 1)
        self.assertEqual(occ.evidence_rows_with_ranges, 0)
        self.assertIsNone(occ.occupancy_sellable)


# --------------------------------------------------------------------------- #
# 8. authority — declarations, refusals, kill switch (§22)
# --------------------------------------------------------------------------- #
class TestAuthority(Harness):

    def test_declared_tools_are_read_only_and_write_nothing(self):
        for name, tier, scope, _intent in RBI.REVENUE_TOOLS:
            self.assertEqual(tier, "LOW", name)
            self.assertIsNone(scope, name)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM ai_tools WHERE tool LIKE 'revenue.%'"
        ).fetchone()[0]
        self.assertEqual(rows, 0)

    def test_handoff_target_is_wired_in_the_router(self):
        import control_tower as CT

        self.assertEqual(CT.ROUTES["REVENUE_BI"], RBI.REVENUE_AGENT_NAME)

    def test_write_and_unknown_verbs_are_refused(self):
        ai, gateway = self._wired()
        principal = RBI.register_principal(
            ai, secret_ref="VILLA_RBI_SECRET", owner_actor="owner_anas"
        )
        pid = principal.principal_id
        with self.assertRaises(Exception) as denied:
            gateway.request(pid, "payment.approve", intent="rbi: must be refused")
        self.assertEqual(getattr(denied.exception, "code", ""), "AI_DENIED_ACTION")
        with self.assertRaises(Exception) as unknown:
            gateway.request(pid, "ops_tasks.create", intent="rbi: must be refused")
        self.assertEqual(getattr(unknown.exception, "code", ""), "AI_UNKNOWN_TOOL")

    def test_suspend_stops_this_agent_and_reinstate_returns_it(self):
        ai, gateway = self._wired()
        principal = RBI.register_principal(
            ai, secret_ref="VILLA_RBI_SECRET", owner_actor="owner_anas"
        )
        pid = principal.principal_id
        gateway.request(pid, "villa.search", intent="rbi: read reference")
        ai.suspend(pid, actor="owner_anas", reason="§40 drill")
        with self.assertRaises(Exception) as ctx:
            gateway.request(pid, "villa.search", intent="rbi: read reference")
        self.assertIn("SUSPEND", str(ctx.exception).upper()
                      + str(getattr(ctx.exception, "code", "")))
        ai.reinstate(pid, actor="owner_anas", reason="drill over")
        gateway.request(pid, "villa.search", intent="rbi: read reference")
        self.assertTrue(ai.events(pid))


# --------------------------------------------------------------------------- #
# 9. the worker catalogue and the pre-code sign check
# --------------------------------------------------------------------------- #
class TestCanonicalCatalogue(Harness):

    def test_twelve_workers_with_stable_codes(self):
        self.assertEqual(len(RBI.REVENUE_BI_WORKERS), 12)
        codes = [row[0] for row in RBI.REVENUE_BI_WORKERS]
        self.assertEqual(codes, [f"RBI-W{i:02d}" for i in range(1, 13)])
        self.assertEqual(len({row[1] for row in RBI.REVENUE_BI_WORKERS}), 12)

    def test_the_catalogue_matches_the_accepted_adr(self):
        import pathlib

        adr = pathlib.Path(__file__).resolve().parents[1] / "docs" / "adr"
        text = next(adr.glob("0064-*.md")).read_text(encoding="utf-8")
        for code, worker_id, display in RBI.REVENUE_BI_WORKERS:
            self.assertIn(code, text)
            self.assertIn(worker_id, text)
            self.assertIn(display, text)


class TestDiscountSignIsVerified(Harness):
    """Pre-code step #1 of the Agent 6 research: the DISCOUNT sign, pinned."""

    def test_discount_lines_are_negative_and_reduce_the_total(self):
        import invoicing as INV

        self.add_unit("unit_rbi_a")
        self.add_booking("book_rbi_1", in_days=-10.0, nights=7)
        self.add_invoice("inv_rbi_1", subtotal=2_100_000, tax=210_000,
                         total=2_310_000)
        self.add_line("line_rbi_n", "inv_rbi_1", kind="NIGHTLY", quantity=7,
                      unit_amount=300_000)
        self.add_line("line_rbi_d", "inv_rbi_1", kind="DISCOUNT", quantity=1,
                      unit_amount=-200_000)
        rows = self.conn.execute(
            "SELECT kind, amount_minor FROM invoice_lines"
            " WHERE invoice_id = 'inv_rbi_1' ORDER BY kind"
        ).fetchall()
        kinds = {str(r[0]): int(r[1]) for r in rows}
        self.assertLess(kinds["DISCOUNT"], 0)
        from types import SimpleNamespace

        lines = [
            SimpleNamespace(kind="NIGHTLY", amount_minor=2_100_000),
            SimpleNamespace(kind="DISCOUNT", amount_minor=-200_000),
        ]
        # derive_totals keeps the discount inside the subtotal: net, not gross.
        self.assertEqual(INV.derive_totals(lines), (1_900_000, 0, 0, 1_900_000))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
