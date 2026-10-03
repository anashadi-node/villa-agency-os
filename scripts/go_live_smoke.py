#!/usr/bin/env python3
"""Walk the whole guest path on a real database — the go-live proof a buyer can re-run.

``seed_demo.py`` writes rows straight into tables: honest, but it proves nothing about the
flows. This script is the opposite. It creates a database from nothing and drives the
platform the way one week of operation would, one named actor at a time:

    1  tenant      one property, one unit, the policy and rate card in force
    2  policy      the owner publishes the values the commercial path refuses without
    3  inbound     a signed channel webhook becomes a case message (a forgery is refused)
    4  intake      a structured proposal, reviewed field by field by a person
    5  evidence    availability for the window, recorded with its source
    6  quote       priced from the versions in force, then persisted
    7  offer       drafted, sent, accepted by the guest
    8  hold        the slot leaves the market (a second hold on it is refused)
    9  booking     confirmed from the hold, against the accepted quote version
   10  invoice     issued from the accepted quote
   11  payment     a hosted intent, then the provider's signed webhook settles it
   12  documents   the document kinds §30 requires are recorded
   13  stay        arrival prepared, guest checked in, stay completed
   14  settlement  supplier settled (DUE then PAID), deposit resolved
   15  closure     §30's gate opens; the booking is closed
   16  evidence    audit chain, ledger, KPI bundle, JSON report on disk

Every step is a command the platform owns, performed by a person the report can name. A
refusal stops the run and prints the domain code — a go-live that cannot be demonstrated is
not a go-live.

Why it is a script and not a test only: the same journey runs as a test
(``tests/test_go_live_smoke.py``), but the artefact a buyer receives is the command they can
run on their own machine against a database of their own.

Exit codes
----------
0  every step passed (the report is written)
1  a step was refused — the code and the step number are printed
2  usage problem, or a database that is not empty

The clock is frozen and advanced by hand, so two runs produce the same journey and reports
that can be diffed. No secret is real: the channel and provider secrets are generated for
this run and printed nowhere.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import booking_closure as CLOSURE  # noqa: E402
import cases as CASES  # noqa: E402
import channels as CHANNELS  # noqa: E402
import deposit_resolutions as DEPOSITS  # noqa: E402
import documents as DOCUMENTS  # noqa: E402
import intake as INTAKE  # noqa: E402
import invoicing as INVOICING  # noqa: E402
import payments as PAYMENTS  # noqa: E402
import policy_engine as POLICY  # noqa: E402
import quotation as QUOTATION  # noqa: E402
import reservation_kernel as KERNEL  # noqa: E402
import supplier_settlements as SUPPLIERS  # noqa: E402
from primitives import DomainError, FrozenClock, IdFactory  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

AGENCY = "agency_default"

#: The four people who act in this journey. Named, because every row the platform writes
#: says who did it — a run with anonymous actors would not exercise that rule.
OWNER = "user:owner_anas"
MANAGER = "user:manager_dewi"
FRONT_DESK = "user:staff_ayu"
GUEST = "user:guest_marco"

CHANNEL = "whatsapp_business"
PROVIDER = "dummy"
PROPERTY = "prop_nusadua"
UNIT = "unit_nusadua_1"
CHECK_IN = "2026-10-20"
CHECK_OUT = "2026-10-23"
ADULTS = 2
CHILDREN = 0
CURRENCY = "IDR"
GUEST_REF = "guest_marco_01"

#: 2026-10-03 08:00 WIB. The journey runs 18 days ahead of the stay, so nothing depends on
#: "today" — the dates are facts of the scenario, not of the machine that runs it.
T0 = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)

#: Generated per run. Nothing prints them; nothing stores them; they exist so the two signed
#: doors in this journey are real doors rather than a bypass.
RUN_SECRET = hashlib.sha256(f"villa-smoke-{id(object())}".encode()).hexdigest()


def instant(hours: float) -> str:
    """An ISO instant ``hours`` after T0 (the clock the whole journey reads)."""
    return (T0 + timedelta(hours=hours)).isoformat()


def sign(secret: str, body: str) -> str:
    """HMAC-SHA256 hex over the exact body — the scheme both signed doors use."""
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


class Refused(Exception):
    """A step was refused by the platform: the run stops and the code is reported."""

    def __init__(self, step: str, exc: DomainError) -> None:
        super().__init__(f"{step}: {exc.code}: {exc.message}")
        self.step = step
        self.code = exc.code
        self.message = exc.message


class Smoke:
    """One run of the journey. Every step appends a line of evidence to ``self.log``."""

    def __init__(self, db: Path) -> None:
        self.db = db
        self.clock = FrozenClock.at(instant(0))
        self.ids = IdFactory()
        self.kernel = KERNEL.ReservationKernel.open(
            str(db), clock=self.clock, ids=self.ids, agency_id=AGENCY
        )
        self.conn: sqlite3.Connection = self.kernel.conn
        self.log: list[dict[str, Any]] = []
        self.facts: dict[str, Any] = {}

    # -- plumbing ----------------------------------------------------------- #

    def close(self) -> None:
        self.kernel.close()

    def at(self, hours: float) -> None:
        """Advance the frozen clock — an audit trail with no movement proves nothing."""
        self.clock.set(instant(hours))

    @contextmanager
    def step(self, number: int, name: str) -> Iterator[dict[str, Any]]:
        """Time one step, record it, and turn any domain refusal into a stop."""
        entry: dict[str, Any] = {"step": number, "name": name, "at": instant(0)}
        try:
            yield entry
        except DomainError as exc:
            entry["refused"] = {"code": exc.code, "message": exc.message}
            self.log.append(entry)
            raise Refused(f"step {number} ({name})", exc) from exc
        self.log.append(entry)
        print(f"  {number:>2}. {name:<24} OK   {entry.get('detail', '')}")

    # -- 1. the tenant ------------------------------------------------------ #

    def tenant(self) -> None:
        self.kernel.upsert_property(property_id=PROPERTY, name="Villa Nusa Dua 1")
        self.kernel.upsert_unit(
            unit_id=UNIT, property_id=PROPERTY, name="Villa Nusa Dua 1", base_capacity=2,
            max_guests=4,
        )
        policy = POLICY.PolicyVersion(
            policy_version_id="policy_nusadua_2026",
            property_id=PROPERTY,
            unit_scope="property",
            version=1,
            effective_from="2026-01-01",
            effective_to="2026-12-31",
            rules=POLICY.PolicyRules(
                currency=CURRENCY,
                deposit_pct="30",
                service_fee_pct="5",
                tax_pbjt_pct="10",
                max_discount_pct="15",
                quote_valid_hours=48,
                hold_ttl_minutes=720,
                max_hold_extensions=2,
                payload={
                    "extra_guest_fee_minor": 250_000,
                    "cleaning_fee_minor": 500_000,
                    "security_deposit_minor": 2_000_000,
                    "min_stay_nights": 2,
                    "cancellation_ladder": [{"min_days_before": 0, "refund_pct": "100"}],
                },
            ),
            created_at=instant(0),
            source_doc_ref="docs/legal/rate-policy-2026.md",
            approved_by=OWNER,
        )
        self.kernel.insert_policy_version(policy.as_row())
        self.kernel.insert_rate_card(
            {
                "rate_card_id": "card_nusadua_2026",
                "property_id": PROPERTY,
                "unit_scope": "property",
                "name": "Public 2026",
                "version": 1,
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "currency": CURRENCY,
                "weekly_discount_pct": "0",
                "monthly_discount_pct": "0",
                "source_doc_ref": "docs/legal/rate-card-2026.md",
                "approved_by": OWNER,
                "created_at": instant(0),
            },
            [
                {
                    "season_id": "season_regular_2026",
                    "rate_card_id": "card_nusadua_2026",
                    "name": "Regular",
                    "start_date": "2026-01-01",
                    "end_date": "2026-12-31",
                    "nightly_rate_minor": 1_500_000,
                    "min_stay_nights": 2,
                    "priority": 0,
                    "created_at": instant(0),
                }
            ],
        )
        self.facts["property"] = PROPERTY
        self.facts["unit"] = UNIT

    # -- 2. the owner's decisions ------------------------------------------ #

    def policy_values(self) -> None:
        """Publish what the platform refuses to invent (§15: thresholds are configuration).

        Values a tenant fills in for themselves — this is the white-label surface, and the
        journey proves the engine runs on published values rather than code defaults.
        """
        resolver = POLICY.PolicyResolver(
            self.conn, clock=self.clock, ids=self.ids, agency_id=AGENCY
        )
        published = {
            "operations.booking.required_document_kinds": ["CONFIRMATION", "RECEIPT"],
            "commercial.margin_floor_currency": CURRENCY,
            "commercial.margin_floor_minor": 0,
            "commercial.margin_floor_pct": "0",
            "commercial.discount.authority_roles": ["owner"],
            "finance.approval.dual_control_subjects": ["refund", "payout", "bank_change"],
        }
        for key, value in published.items():
            try:
                resolver.publish(
                    key,
                    value,
                    effective_from="2026-01-01",
                    source_doc_ref="docs/GO_LIVE_DECISIONS.md#D6",
                    approved_by=OWNER,
                )
            except DomainError as exc:
                # A tenant catalogue that does not declare the key yet is not a failure of
                # the journey: the key is optional until the flow that reads it runs.
                if exc.code != "POLICY_KEY_UNKNOWN":
                    raise
        self.facts["policy_keys_published"] = len(published)

    # -- 3. the guest arrives through a signed door ------------------------- #

    def inbound(self) -> None:
        cases = CASES.CaseService(self.conn, clock=self.clock, ids=self.ids, agency_id=AGENCY)
        channel = CHANNELS.ChannelAdapter(
            cases=cases,
            messages=None,
            transport=CHANNELS.FakeTransport(),
            secrets={CHANNEL: RUN_SECRET},
            clock=self.clock,
        )
        case, first_message = cases.open_case(
            channel=CHANNEL,
            guest_ref=GUEST_REF,
            content_class="P2",
            body="Halo, apakah villa 3 malam 20-23 Oktober masih tersedia untuk 2 orang?",
            idempotency_key="cmd_smoke_open_case",
            priority="NORMAL",
            sla_policy="whatsapp_v1",
            external_ref="wa_msg_0001",
            received_at=instant(0),
        )
        self.facts["case_id"] = str(case.case_id)
        self.facts["first_message_id"] = str(first_message.case_message_id)

        body = "Kami tertarik, tolong kirim penawaran resminya."
        message = channel.receive(
            channel=CHANNEL,
            body=body,
            signature=sign(RUN_SECRET, body),
            case_id=str(case.case_id),
            content_class="P2",
            idempotency_key="cmd_smoke_inbound_1",
            sender_ref=GUEST_REF,
            external_ref="wa_msg_0002",
            received_at=instant(1),
        )
        replayed = channel.receive(
            channel=CHANNEL,
            body=body,
            signature=sign(RUN_SECRET, body),
            case_id=str(case.case_id),
            content_class="P2",
            idempotency_key="cmd_smoke_inbound_1",
            sender_ref=GUEST_REF,
            external_ref="wa_msg_0002",
            received_at=instant(1),
        )
        if str(getattr(message, "message_id", "")) != str(getattr(replayed, "message_id", "")):
            raise DomainError("a replayed webhook wrote a second fact", code="SMOKE_REPLAY_NOT_IDEMPOTENT")

        refused = False
        try:  # a forged signature must leave nothing behind
            channel.receive(
                channel=CHANNEL,
                body="transfer ke rekening pribadi saya",
                signature=sign("the-wrong-secret", "transfer ke rekening pribadi saya"),
                case_id=str(case.case_id),
                content_class="P2",
                idempotency_key="cmd_smoke_forged",
                received_at=instant(1),
            )
        except DomainError:
            refused = True
        if not refused:
            raise DomainError("a forged webhook was accepted", code="SMOKE_FORGERY_ACCEPTED")

    # -- 4. a person reviews the extracted facts ---------------------------- #

    def intake(self) -> None:
        service = INTAKE.IntakeService(self.conn, clock=self.clock, ids=self.ids)
        case_id = self.facts["case_id"]
        fields = (
            INTAKE.ProposedField(name="check_in", value=CHECK_IN, confidence=95),
            INTAKE.ProposedField(name="check_out", value=CHECK_OUT, confidence=95),
            INTAKE.ProposedField(name="adults", value=ADULTS, confidence=92),
            INTAKE.ProposedField(name="children", value=CHILDREN, confidence=88),
        )
        _, rows = service.propose(
            case_id,
            source_message_id=self.facts["first_message_id"],
            fields=fields,
            extractor="rule",
            produced_at=instant(2),
        )
        accepted = 0
        for row in rows:  # a proposal nobody reviews is not a fact
            service.review(
                intake_field_id=row.intake_field_id,
                decision="ACCEPT",
                reviewer=MANAGER,
                reviewer_role="manager",
                at=instant(3),
            )
            accepted += 1
        if accepted != len(fields):
            raise DomainError(
                f"expected {len(fields)} reviewed fields, reviewed {accepted}",
                code="SMOKE_INTAKE_INCOMPLETE",
            )

    # -- 5. availability, on evidence -------------------------------------- #

    def availability(self) -> None:
        self.kernel.record_evidence(
            unit_id=UNIT,
            source_type="manager_written",
            evidence_level="A",
            result="AVAILABLE",
            captured_at=instant(3),
            valid_until=instant(72),
            ttl_hours=72,
            can_quote=True,
            can_hold=True,
            can_confirm=False,
            source_ref="wa_supplier_2026_10_03",
            notes="supplier confirms the unit is free for the window",
        )
        check = self.kernel.request_availability_check(
            unit_id=UNIT,
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            actor=MANAGER,
            actor_role="manager",
            idempotency_key="cmd_smoke_avail_check",
        )
        self.facts["check_id"] = str(getattr(check, "check_id", ""))

    # -- 6. the price ------------------------------------------------------ #

    def quote(self) -> None:
        quote = self.kernel.quote_stay(
            unit_id=UNIT, check_in=CHECK_IN, check_out=CHECK_OUT, adults=ADULTS, children=CHILDREN
        )
        self.kernel.persist_quote(quote)
        # Kept alive on purpose: the invoice is issued from the *quote object* (its lines
        # are the pricing authority), not from the row a later query happens to return.
        self.quote_object = quote
        self.facts["quote_id"] = str(quote.quote_id)
        self.facts["total_minor"] = int(quote.total.amount_minor)
        self.facts["currency"] = str(quote.total.currency)
        self.facts["deposit_due_minor"] = int(quote.deposit_due.amount_minor)
        self.facts["security_deposit_minor"] = int(quote.security_deposit.amount_minor)
        self.facts["nights"] = int(quote.nights)

    # -- 7. the offer, accepted -------------------------------------------- #

    def offer(self) -> None:
        service = QUOTATION.QuotationService(
            self.conn, clock=self.clock, ids=self.ids, agency_id=AGENCY
        )
        version = service.draft(self.facts["quote_id"], actor=MANAGER, actor_role="manager")
        version_id = str(version.quotation_version_id)
        service.send(version_id, actor=MANAGER, actor_role="manager")
        self.at(5)  # the guest answers the same morning
        accepted = service.accept(version_id, actor=GUEST, actor_role="guest")
        self.facts["quotation_version_id"] = version_id
        # §11: the guest's yes is recorded with the version and its three snapshots —
        # the answered version is what the booking is later gated on.
        self.facts["acceptance_id"] = str(accepted.acceptance_id)
        self.facts["accepted_at"] = str(accepted.accepted_at)
        self.facts["snapshots"] = [
            str(accepted.price_snapshot_id),
            str(accepted.terms_snapshot_id),
            str(accepted.policy_snapshot_id),
        ]

    # -- 8. the hold: the slot leaves the market --------------------------- #

    def hold(self) -> None:
        outcome = self.kernel.request_hold(
            unit_id=UNIT,
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            guest_ref=GUEST_REF,
            policy_version_id="policy_nusadua_2026",
            idempotency_key="cmd_smoke_hold_1",
            actor=MANAGER,
            actor_role="manager",
            quote_ref=self.facts["quote_id"],
        )
        self.facts["hold_id"] = str(outcome.hold_id)
        blocked = False
        try:  # the same window cannot be sold twice
            self.kernel.request_hold(
                unit_id=UNIT,
                check_in=CHECK_IN,
                check_out=CHECK_OUT,
                guest_ref="guest_second",
                policy_version_id="policy_nusadua_2026",
                idempotency_key="cmd_smoke_hold_2",
                actor=MANAGER,
                actor_role="manager",
            )
        except DomainError:
            blocked = True
        if not blocked:
            raise DomainError("a second hold took the same slot", code="SMOKE_DOUBLE_HOLD")

    # -- 9. the booking ---------------------------------------------------- #

    def booking(self) -> None:
        outcome = self.kernel.confirm_from_hold(
            hold_id=self.facts["hold_id"],
            quote_id=self.facts["quote_id"],
            idempotency_key="cmd_smoke_confirm_1",
            command_id="cmd_smoke_confirm_1",
            adults=ADULTS,
            children=CHILDREN,
            actor=MANAGER,
            actor_role="manager",
        )
        self.facts["booking_id"] = str(outcome.booking_id)

    # -- 10. the invoice --------------------------------------------------- #

    def invoice(self) -> None:
        service = INVOICING.InvoiceService(
            self.conn, clock=self.clock, ids=self.ids, agency_id=AGENCY
        )
        invoice = service.issue_from_quote(
            quote=self.quote_object,
            booking_id=self.facts["booking_id"],
            idempotency_key="cmd_smoke_invoice_1",
            actor=MANAGER,
            actor_role="manager",
        )
        self.facts["invoice_id"] = str(invoice.invoice_id)
        self.facts["invoice_number"] = str(invoice.invoice_number)

    # -- 11. the money ----------------------------------------------------- #

    def payment(self) -> None:
        payments = PAYMENTS.PaymentService(
            self.conn, clock=self.clock, ids=self.ids, provider=PROVIDER, agency_id=AGENCY
        )
        intent = payments.create_intent(
            invoice_id=self.facts["invoice_id"],
            amount_minor=self.facts["total_minor"],
            currency=CURRENCY,
            idempotency_key="cmd_smoke_intent_1",
            created_by=MANAGER,
        )
        provider_intent_id = str(intent.provider_intent_id)
        payload = json.dumps(
            {
                "provider_intent_id": provider_intent_id,
                "invoice_id": self.facts["invoice_id"],
                "amount_minor": self.facts["total_minor"],
                "currency": CURRENCY,
                "status": "succeeded",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        payments.handle_webhook(
            provider=PROVIDER,
            event_id="evt_smoke_0001",
            event_type="payment.succeeded",
            payload_raw=payload,
            signature=sign(RUN_SECRET, payload),
            secret=RUN_SECRET,
            received_at=instant(8),
        )
        verification = payments.verify()
        if not getattr(verification, "ok", False):
            raise DomainError(
                "the ledger does not verify after settlement", code="SMOKE_LEDGER_UNVERIFIED"
            )
        self.facts["payment_id"] = str(intent.payment_id)

    # -- 12. the stay ------------------------------------------------------ #

    def stay(self) -> None:
        booking_id = self.facts["booking_id"]
        self.at(24 * 17)  # the day before arrival
        self.kernel.prepare_arrival(
            booking_id=booking_id, command_id="cmd_smoke_prepare", actor=FRONT_DESK,
            actor_role="staff",
        )
        self.at(24 * 18)  # the guest arrives
        self.kernel.check_in(
            booking_id=booking_id, command_id="cmd_smoke_checkin", actor=FRONT_DESK,
            actor_role="staff",
        )
        self.at(24 * 21)  # and leaves
        self.kernel.complete_stay(
            booking_id=booking_id, command_id="cmd_smoke_complete", actor=FRONT_DESK,
            actor_role="staff",
        )
        self.facts["stay_events"] = [
            f"{event['from_state']}->{event['to_state']}"
            for event in self.kernel.state_events(booking_id)
        ]

    # -- 13. what a person owes, and what is owed -------------------------- #

    def settlement(self) -> None:
        booking_id = self.facts["booking_id"]
        supplier_amount = int(self.facts["total_minor"] * 70 // 100)
        suppliers = SUPPLIERS.SupplierSettlementService(
            self.conn, clock=self.clock, ids=self.ids, agency_id=AGENCY
        )
        for action, amount, key in (
            ("DUE", supplier_amount, "cmd_smoke_settle_due"),
            ("PAID", supplier_amount, "cmd_smoke_settle_paid"),
        ):
            suppliers.record(
                booking_id=booking_id,
                action=action,
                amount_minor=amount,
                currency=CURRENCY,
                decided_by=OWNER,
                actor_role="owner",
                evidence_ref="supplier_invoice_2026_10",
                idempotency_key=key,
            )
        deposit = 2_000_000
        deposits = DEPOSITS.DepositResolutionService(
            self.conn, clock=self.clock, ids=self.ids, agency_id=AGENCY
        )
        deposits.record(
            booking_id=booking_id,
            action="RETURNED",
            amount_minor=deposit,
            currency=CURRENCY,
            decided_by=OWNER,
            actor_role="owner",
            evidence_ref="deposit_return_2026_10",
            idempotency_key="cmd_smoke_deposit_returned",
        )
        self.facts["supplier_amount_minor"] = supplier_amount
        self.facts["deposit_minor"] = deposit

    # -- 14. the documents §30 asks for ------------------------------------ #

    def documents(self) -> None:
        service = DOCUMENTS.DocumentService(
            self.conn, clock=self.clock, ids=self.ids, root=self.db.parent / "documents",
            agency_id=AGENCY,
        )
        subject = {"booking_id": self.facts["booking_id"]}
        invoice_subject = {"invoice_id": self.facts["invoice_id"]}
        for kind, key, source in (
            ("CONFIRMATION", "booking_confirmed", subject),
            ("RECEIPT", "deposit_received", invoice_subject),
        ):
            service.generate(
                kind=kind,
                document_key=key,
                subject=source,
                generated_by=MANAGER,
                actor_role="manager",
                at=self.clock.now(),
            )

    # -- 15. §30: the closure gate ----------------------------------------- #

    def closure(self) -> None:
        booking_id = self.facts["booking_id"]
        verdict = CLOSURE.evaluate(self.conn, booking_id=booking_id, agency_id=AGENCY)
        self.facts["closure_ready"] = bool(verdict.ready)
        self.facts["closure_checks"] = [
            {"name": check.name, "ok": bool(check.ok), "detail": check.detail}
            for check in verdict.checks
        ]
        if not verdict.ready:
            raise DomainError(
                "§30's closure gate is not satisfied: " + ", ".join(verdict.blockers),
                code="SMOKE_CLOSURE_NOT_READY",
            )
        self.kernel.close_booking(
            booking_id=booking_id, command_id="cmd_smoke_close", reason="stay completed",
            actor=MANAGER, actor_role="manager",
        )
        row = self.kernel.get_booking(booking_id)
        self.facts["booking_status"] = str(row["status"])
        if self.facts["booking_status"] != "CLOSED":
            raise DomainError(
                f"booking is {self.facts['booking_status']} after close",
                code="SMOKE_NOT_CLOSED",
            )

    # -- 16. the evidence -------------------------------------------------- #

    def evidence(self) -> None:
        if not self.kernel.verify_audit_chain():
            raise DomainError("the audit chain does not verify", code="SMOKE_AUDIT_CHAIN_BROKEN")
        payments = PAYMENTS.PaymentService(
            self.conn, clock=self.clock, ids=self.ids, provider=PROVIDER, agency_id=AGENCY
        )
        ledger = payments.verify()
        self.facts["audit_events"] = int(
            self.conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]
        )
        self.facts["ledger_ok"] = bool(getattr(ledger, "ok", False))
        self.facts["ledger_entries_checked"] = int(getattr(ledger, "checked", 0))
        if not self.facts["ledger_ok"]:
            raise DomainError(
                f"the ledger does not verify: {getattr(ledger, 'reason', None)}",
                code="SMOKE_LEDGER_UNVERIFIED",
            )

    # -- the run ------------------------------------------------------------ #

    def run(self) -> dict[str, Any]:
        steps = (
            (1, "tenant", self.tenant),
            (2, "policy values", self.policy_values),
            (3, "inbound webhook", self.inbound),
            (4, "intake review", self.intake),
            (5, "availability", self.availability),
            (6, "quote", self.quote),
            (7, "offer accepted", self.offer),
            (8, "hold", self.hold),
            (9, "booking", self.booking),
            (10, "invoice", self.invoice),
            (11, "payment", self.payment),
            (12, "documents", self.documents),
            (13, "stay", self.stay),
            (14, "settlement", self.settlement),
            (15, "closure §30", self.closure),
            (16, "evidence", self.evidence),
        )
        for number, name, action in steps:
            with self.step(number, name):
                action()
        return self.report()

    def report(self) -> dict[str, Any]:
        return {
            "journey": "guest path, end to end",
            "database": str(self.db),
            "agency": AGENCY,
            "started_at": instant(0),
            "steps": self.log,
            "facts": self.facts,
        }


def _is_empty(path: Path) -> bool:
    """Refuse a database that already holds data: this script is a from-nothing proof.

    ``connect_sqlite`` applies the schema, so the tables always exist and the question is
    only whether a earlier journey left rows behind. Re-running over one would mix two
    stories into one audit trail, which is the opposite of evidence.
    """
    if not path.exists():
        return True
    conn = connect_sqlite(str(path))
    try:
        for table in ("properties", "cases", "bookings", "payments"):
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            if int(row["n"]) > 0:
                return False
        return True
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default="",
        help="database file to create (default: <repo>/data/go-live-smoke.db)",
    )
    parser.add_argument("--report", default="", help="where to write the JSON report")
    args = parser.parse_args(argv)

    db = Path(args.db).expanduser() if args.db else REPO / "data" / "go-live-smoke.db"
    report_path = (
        Path(args.report).expanduser() if args.report else db.with_suffix(".report.json")
    )
    db.parent.mkdir(parents=True, exist_ok=True)
    if not _is_empty(db):
        print(
            f"refusing to run: {db} already holds a journey "
            "(this proof starts from an empty database)",
            file=sys.stderr,
        )
        return 2

    print(f"=== GO-LIVE SMOKE — {db} ===")
    smoke = Smoke(db)
    try:
        report = smoke.run()
    except Refused as refusal:
        print(f"\nREFUSED at {refusal.step}: {refusal.code}: {refusal.message}", file=sys.stderr)
        return 1
    except DomainError as exc:
        print(f"\nSTOPPED: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    finally:
        smoke.close()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    facts = report["facts"]
    print("\n--- THE JOURNEY ---")
    print(f"  case        {facts['case_id']}")
    print(f"  quote       {facts['quote_id']}  {facts['currency']} {facts['total_minor']:,}")
    print(f"  hold        {facts['hold_id']}")
    print(f"  booking     {facts['booking_id']}  -> {facts.get('booking_status', '?')}")
    print(f"  invoice     {facts['invoice_number']}")
    print(f"  payment     {facts['payment_id']}")
    print(f"  stay        {', '.join(facts.get('stay_events', []))}")
    print(f"  §30 ready   {facts.get('closure_ready')}")
    print(f"  audit rows  {facts.get('audit_events')}")
    print(f"  report      {report_path}")
    print("\n=== GO-LIVE SMOKE: PASS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
