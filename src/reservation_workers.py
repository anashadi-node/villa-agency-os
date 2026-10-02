"""§9's capability workers — the AI employee's hands, and the only ones it has.

§9 lists what the reservation employee does; the capability catalogue lists the tools each job
needs. This module is where the two meet, and it holds one rule:

    **Every worker asks the door first, then hands the work to the module that owns it.**

Three things follow, and each one is a claim pinned in
:file:`tests/test_reservation_workers.py`:

1. **No worker touches a table.** The proposal, the ranking and the availability request are
   created by :mod:`intake`, :mod:`matching` and :mod:`reservation_kernel` — the modules whose
   rules (review, §6 facts authority, overlap, idempotency) this file must not restate. A
   worker that wrote its own row would be a second implementation of a rule that already has
   one, and the second implementation is the one that drifts.
2. **The door is asked before the work, never after.** ``AiGateway.request`` is what refuses a
   tool the principal's §17 mode does not reach and what records the attempt; an audit row
   written *after* the domain call would be a log, not a control.
3. **A refusal leaves nothing behind.** The §13 guard, the door and the domain module's own
   preconditions all run before the first row is written, so a refused attempt is
   indistinguishable from one that never happened — the same promise the estate's other
   fail-closed paths make.

RES-W06 and RES-W07 are the two steps where "which module owns this?" is worth reading before
the code. A routine follow-up is **not** a message: §56 tiers ``followup.routine`` LOW, and the
estate's own rule is that chasing is done "by opening a task for a human, never by contact" —
``messages.MessageService.draft`` refuses a machine author, because approval and sending are
human acts. So the AI's follow-up is a row on the agency's own task queue. A tentative hold, by
contrast, is an act with an outside effect: it takes a slot out of sale, which is why §56 tiers
``hold.request_tentative`` MEDIUM and why the owner has to allow-list it before the AI may ask.

``intent`` is a short operational sentence written by *us*. The gateway records it and never
consults it (§56: conversation text cannot raise privilege), so pasting what a guest wrote
into it would put guest text into the audit chain in exchange for nothing.

Scope: eight of §9's jobs — inquiry (RES-W01), matching (RES-W02), availability (RES-W03),
pricing (RES-W04), the offer (RES-W05), the routine follow-up (RES-W06), the tentative hold
(RES-W07) and the booking status (RES-W08). The rest of the job list arrives with the modules it
needs and follows the same three steps: guard, door, delegate.

Three of the eight are worth reading together with §56. Pricing has no capability of its own — §56
names the *answer* (``villa.answer_verified``) — so RES-W04 writes nothing; the priced stay
becomes a row inside RES-W05, whose one act is the one §56 names for the offer
(``quotation.send``, MEDIUM, owner allow-listed). A kill switch that leaves the AI able to answer
a price therefore still leaves it unable to start an offer.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import booking_states as BS
import cases as C
import crm as CRM
import intake as IN
import matching as M
import ops_tasks as OT
import policy_engine as PE
import quotation as Q
import reservation_kernel as RK
from primitives import DomainError, require_ref, require_text, sha256_hex
from reservation_service import Conversation, ReservationService


#: The §9 capability worker codes. The code is what an operator greps for when asking "who in
#: the AI workforce did this?" — a worker's name is part of its output, not a comment.
INQUIRY_WORKER = "RES-W01"
MATCHING_WORKER = "RES-W02"
AVAILABILITY_WORKER = "RES-W03"
PRICING_WORKER = "RES-W04"
QUOTATION_WORKER = "RES-W05"
FOLLOWUP_WORKER = "RES-W06"
HOLD_WORKER = "RES-W07"

#: The catalogue names, spelled once here and pinned against :data:`ai_service`'s ``ai_tools``
#: rows by ``test_the_tools_the_workers_hold_are_the_tools_the_catalogue_declares``. A typo must
#: fail the suite, not be discovered by the door at run time.
INTAKE_TOOL = "inquiry.intake"
SEARCH_TOOL = "villa.search"
#: Named through the module that enforces each of these, so a rename cannot leave one
#: file gating a tool the other no longer knows about.
AVAILABILITY_TOOL = RK.AVAILABILITY_REQUEST_TOOL
HOLD_TOOL = RK.HOLD_REQUEST_TOOL
QUOTE_TOOL = Q.SEND_TOOL
#: ``followup.routine`` has no module of its own to be named through: the estate never put the
#: name in code, only in the §56 seeds (``ai_tools``) and the gateway's effect table. Spelled
#: here once, and pinned against the catalogue by the suite like the rest.
FOLLOWUP_TOOL = "followup.routine"
BOOKING_WORKER = "RES-W08"
BOOKING_TOOL = "booking.status"

#: RES-W09 — Amend / Cancel Assist. Two acts, neither of them a decision: answer what the terms
#: the guest *accepted* say about cancelling, and route the change request to a person.
SPECIAL_REQUEST_WORKER = "RES-W09"

#: §56's MEDIUM coordination name — "special-request coordination". Its first caller in the
#: estate (ADR-0023/0024 recorded it as a §56 tool with no enforcement point at all).
SPECIAL_REQUEST_TOOL = "request.coordinate_special"

#: The two changes a guest can raise through it. Closed: an unknown kind is refused before the
#: door, because a request nobody can name is a request nobody can work.
SPECIAL_REQUEST_KINDS: tuple[str, ...] = ("CANCELLATION", "AMENDMENT")

#: Why the answer carries no money, in the answer's own words.
TERMS_NOTE_ACCEPTED = (
    "the ladder is the one inside the TERMS snapshot of the version the guest accepted (§11); "
    "any refund, penalty or settlement is the finance module's fact and a person's decision — "
    "this answer reads the binding terms, it does not compute a payout"
)

#: The honest edge: a notice inside every threshold has *no* applicable step.
TERMS_NOTE_NO_STEP = (
    "the notice is shorter than every step of the ladder: no step applies, which is not the same "
    "as a zero refund — whether anything is owed at all is a person's decision on the finance "
    "module's facts"
)

#: And the edge that matters most: a stay with no accepted version has no binding terms at all.
TERMS_NOTE_NO_VERSION = (
    "no accepted version is on record for this stay, so there are no binding cancellation terms "
    "to read: answering from the policy in force today would quote a ladder the guest never "
    "agreed to (§11)"
)

#: The task a routed request becomes, and the field that has to carry its intent. ``ops_tasks``
#: has no narrative column and its ``kind`` vocabulary cannot name a cancellation yet, so the
#: runbook is the only place the request can be spelled out — and a request with an empty runbook
#: would reach a person as an unreadable interruption. Hence: at least one step, required.
SPECIAL_REQUEST_MIN_STEPS = 1

#: A version that carries no ladder at all: nothing to read, nothing to infer.
TERMS_NOTE_NO_LADDER = (
    "the accepted version records no cancellation ladder, so there is nothing to read here — and "
    "nothing may be inferred from its absence"
)

#: §9's three gates, under the spec's own names, so the answer and the tests speak it.
GATE_COMMERCIAL = "commercial"
GATE_INVENTORY = "inventory"
GATE_FINANCIAL = "financial"

#: What the platform can and cannot see about §9's financial gate. §9 wants
#: ``required_client_funds_verified == true`` plus whatever supplier-side conditions the policy or
#: contract adds; this estate records a human's confirmation and the amount paid. Until the
#: verification fact has a home of its own, the answer says so instead of implying it.
FINANCIAL_GATE_NOTE = (
    "no verified-funds fact is recorded yet: the platform stores the amount paid and the human "
    "who confirmed it, not §9's required_client_funds_verified, nor policy-specific "
    "supplier prerequisites"
)

#: Pricing is §56's *answer* capability, not a commercial act of its own: the AI tells a
#: verified figure about this villa's rates. §56 has no separate name for the arithmetic (or for
#: opening a version the guest cannot see), and the catalogue is the closed §56 list — so the
#: spelling here is the answer's, and what the step must not do is made true by the code instead:
#: RES-W04 writes nothing at all.
PRICING_TOOL = "villa.answer_verified"

#: §56's LOW/READ answer name, shared with RES-W04: one catalogue name, two questions (a price,
#: a cancellation ladder). Named through the pricing constant so the two cannot drift apart.
TERMS_TOOL = PRICING_TOOL

#: How a model-derived proposal is labelled in ``intake_proposals.method``. §16 is the reason it
#: is not ``"rule"``: these fields are extracted from a guest's words and are *proposals* until a
#: person accepts them, so the ledger says which kind of extractor produced them.
DEFAULT_METHOD = "llm_shadow"


class WorkerError(DomainError):
    """Base class for refusals this module makes."""

    code = "RES_WORKER_ERROR"


class MissingRequirements(WorkerError):
    """A worker needed the inquiry's confirmed stay and the inquiry does not have one yet."""

    code = "RES_WORKER_MISSING_REQUIREMENTS"


class NoPolicyInForce(WorkerError):
    """A hold was asked for under a stay no policy version covers. Refused, never invented."""


@dataclass(frozen=True)
class BookingStatusAnswer:
    """Where a reservation stands, and which of §9's gates are still open (RES-W08).

    Read-only by construction. ``status`` is the row's own word, ``booking_id`` stays empty
    until a booking exists, and nothing in this object is a confirmation: the only writer of a
    booking is :meth:`reservation_kernel.ReservationKernel.confirm_from_hold`, called by a human.
    """

    case_id: str
    booking_id: Optional[str]
    status: Optional[str]
    occupies_slot: bool
    unit_id: Optional[str]
    check_in: Optional[str]
    check_out: Optional[str]
    commercial: Mapping[str, Any]
    inventory: Mapping[str, Any]
    financial: Mapping[str, Any]
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """The answer as plain data, ready for a prompt or a log line."""

        return {
            "case_id": self.case_id,
            "booking_id": self.booking_id,
            "status": self.status,
            "occupies_slot": self.occupies_slot,
            "unit_id": self.unit_id,
            "check_in": self.check_in,
            "check_out": self.check_out,
            "commercial": dict(self.commercial),
            "inventory": dict(self.inventory),
            "financial": dict(self.financial),
            "missing": list(self.missing),
        }

    code = "RES_WORKER_HOLD_NO_POLICY"


@dataclass(frozen=True)
class CancellationTerms:
    """What the accepted terms say about cancelling, and nothing more (RES-W09).

    The platform's truth for "what cancelling costs" is neither this answer nor the policy in
    force today: it is the ladder inside the TERMS snapshot of the version the guest said yes to
    (§11). This dataclass carries that ladder, where the sitting stay falls in it, and the version
    it came from — and deliberately carries **no money**. Whether a refund is due, how much, and
    by when are the finance module's facts and a person's decision (§9 forbids the AI from
    claiming money moved, and a number invented here would be a commercial term).
    """

    worker: str
    conversation_id: str
    binding: Optional[Mapping[str, Any]]
    notice_days: Optional[int]
    ladder: Optional[tuple[Mapping[str, Any], ...]]
    applicable: Optional[Mapping[str, Any]]
    note: str

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready view: the door's audit and a UI need the same shape."""

        return {
            "worker": self.worker,
            "conversation_id": self.conversation_id,
            "binding": None if self.binding is None else dict(self.binding),
            "notice_days": self.notice_days,
            "ladder": None if self.ladder is None else [dict(step) for step in self.ladder],
            "applicable": None if self.applicable is None else dict(self.applicable),
            "note": self.note,
        }


class InvalidDate(WorkerError):
    """A date reached a worker in a shape nobody can parse. Refused, never coerced."""

    code = "RES_WORKER_INVALID_DATE"


def _iso_date(value: Any, *, field: str) -> str:
    """A date-ish value as ``YYYY-MM-DD``. The *format* is normalised here; the date rules are
    not — whether a date parses, moves forward and is not in the past belongs to
    :mod:`reservation_kernel`, which already refuses a bad window by name.
    """

    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if not isinstance(value, str):
        raise InvalidDate(
            "a worker was handed a date that is not a date or an ISO string",
            field=field,
            got=type(value).__name__,
        )
    text = value.strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    if not text:
        raise InvalidDate("a worker was handed an empty date", field=field)
    return text


class ReservationWorkers:
    """The §9 worker set for one tenant: guard the thread, ask the door, delegate.

    The object holds a :class:`reservation_service.ReservationService` and nothing else of its
    own: the connection, the clock, the id factory, the agency and the AI principal all belong
    to the service, so there is one answer to "which tenant is this and who is acting". The
    kernel, the CRM reader and the intake writer are constructed through the service's own
    dependencies and can be injected by a test that wants to watch them.
    """

    def __init__(
        self,
        service: ReservationService,
        *,
        kernel: Optional[Any] = None,
        crm: Optional[Any] = None,
        intake: Optional[Any] = None,
        quotation: Optional[Any] = None,
        ops: Optional[Any] = None,
        cases: Optional[Any] = None,
    ) -> None:
        self.service = service
        self.conn: sqlite3.Connection = service.conn
        self.gateway = service.gateway
        self.clock = service.clock
        self.ids = service.ids
        self.agency_id = service.agency_id
        self._kernel = kernel
        self._crm = crm
        self._intake = intake
        self._quotation = quotation
        self._ops = ops
        self._cases = cases

    # -- the collaborators, constructed on first use ------------------------- #

    def kernel(self) -> Any:
        """The one module allowed to answer "is this unit free?" (§7 / ``reservation_kernel``)."""

        if self._kernel is None:
            self._kernel = RK.ReservationKernel(
                self.conn, clock=self.clock, ids=self.ids, agency_id=self.agency_id
            )
        return self._kernel

    def crm(self) -> Any:
        """The §6 authority for a stay's requirements. Read here, written nowhere in this file."""

        if self._crm is None:
            self._crm = CRM.CrmService(
                self.conn, clock=self.clock, ids=self.ids, agency_id=self.agency_id
            )
        return self._crm

    def intake(self) -> Any:
        """The module that turns a guest's words into *proposed* fields awaiting review."""

        if self._intake is None:
            self._intake = IN.IntakeService(self.conn, clock=self.clock, ids=self.ids)
        return self._intake

    def quotation(self) -> Any:
        """The module that owns the version chain of an offer (``quotation``)."""

        if self._quotation is None:
            self._quotation = Q.QuotationService(
                self.conn, clock=self.clock, ids=self.ids, agency_id=self.agency_id
            )
        return self._quotation

    def ops(self) -> Any:
        """The module that owns the task queue (``ops_tasks``) — where internal work lives."""

        if self._ops is None:
            self._ops = OT.OpsTaskService(
                self.conn, clock=self.clock, ids=self.ids, agency_id=self.agency_id
            )
        return self._ops

    def cases(self) -> Any:
        """The module that owns ``cases`` — read here for the guest and the owner it names."""

        if self._cases is None:
            self._cases = C.CaseService(
                self.conn, self.clock, self.ids, agency_id=self.agency_id
            )
        return self._cases

    # -- the three steps, in one place -------------------------------------- #

    def _door(
        self,
        conversation: Conversation,
        tool: str,
        *,
        worker: str,
        what: str,
        at: Any = None,
    ) -> Any:
        """Ask the §17 gate for one tool, on behalf of the service's principal.

        The intent is built from the worker code and the action — never from anything a guest
        wrote. It is recorded and never consulted, so it exists to make the audit readable by a
        person, not to influence the decision.
        """

        return self.gateway.request(
            self.service.principal(),
            tool,
            intent=f"{worker}: {what}",
            correlation_id=conversation.conversation_id,
            at=at,
        )

    def _case(self, case_id: str) -> Any:
        """The case row this conversation carries, read through the module that owns it.

        The guest reference and the case's owner are facts about the inquiry, not about the
        message that arrived last: a worker that took either from the caller's arguments could be
        handed a different guest's reference and no rule here would notice.
        """

        return self.cases().get(case_id)

    def _policy_in_force(self, unit_id: str, check_in: str, check_out: str) -> str:
        """The policy version covering this stay, from the rule that already chooses it.

        :func:`matching.policy_for` is the same lookup the ranking and the price used, so a hold
        cannot be taken under a version no other step agreed to. Which version covers a stay is
        not re-decided here; only the refusal to hold without one is this module's.
        """

        unit = self.conn.execute(
            "SELECT * FROM units WHERE unit_id = ? AND agency_id = ?",
            (unit_id, self.agency_id),
        ).fetchone()
        if unit is None:
            raise DomainError(f"unknown unit: {unit_id}", code="UNIT_NOT_FOUND", unit_id=unit_id)
        row = M.policy_for(self.conn, unit, check_in, check_out)
        if row is None:
            raise NoPolicyInForce(
                "no policy version covers this stay, so there is no hold to take under it",
                unit_id=unit_id,
                check_in=check_in,
                check_out=check_out,
            )
        return str(row["policy_version_id"])

    def _requirements(self, case_id: str) -> Mapping[str, Any]:
        """The inquiry's current confirmed stay, straight from the §6 authority."""

        return dict(self.crm().confirmed_facts(case_id))

    # -- RES-W01: record what the guest asked for --------------------------- #

    def record_inquiry(
        self,
        conversation_id: str,
        *,
        source_message_id: str,
        fields: Sequence[Any],
        method: str = DEFAULT_METHOD,
        at: Any = None,
    ) -> tuple[str, list[Any]]:
        """Record the guest's words as *proposed* fields on the inquiry (RES-W01).

        Returns ``(proposal_id, rows)`` exactly as :meth:`intake.IntakeService.propose` does —
        the worker adds the door and the guard, never a second opinion about the fields.

        Every field stays ``PROPOSED`` until a person reviews it (§16): the AI may propose what
        it read out of a message, and only a human may turn that into a fact the rest of the
        platform reasons on. Nothing here decides whether the guest's words are true.
        """

        conversation = self.service.require_thread(
            conversation_id, what=f"{INQUIRY_WORKER} recording the inquiry"
        )
        if not fields:
            raise WorkerError(
                "a proposal with no fields would record nothing; refusing rather than "
                "writing an empty extraction",
                code="RES_WORKER_EMPTY_PROPOSAL",
                conversation_id=conversation_id,
                worker=INQUIRY_WORKER,
            )
        self._door(
            conversation,
            INTAKE_TOOL,
            worker=INQUIRY_WORKER,
            what="recording the guest's inquiry as proposed fields",
            at=at,
        )
        return self.intake().propose(
            conversation.case_id,
            source_message_id=source_message_id,
            fields=list(fields),
            extractor=self.service.actor,
            method=method,
            produced_at=at,
        )

    # -- RES-W02: search the inventory -------------------------------------- #

    def search_villas(
        self,
        conversation_id: str,
        *,
        facts: Optional[Mapping[str, Any]] = None,
        at: Any = None,
    ) -> Any:
        """Rank the tenant's inventory for this inquiry (RES-W02).

        ``facts=None`` reads the inquiry's **confirmed** requirements, which Master Spec v1.1 §6
        makes the authority for a stay; a caller may pass its own criteria, and the run then
        records that it ranked against a caller's claim rather than a confirmed fact — the
        ledger must never upgrade one into the other.

        Searching is a read: it ranks and appends its own run, and it changes nothing a guest
        can see. That is why the tool is ``villa.search`` (READ) while every tool that tells
        somebody something is not.
        """

        conversation = self.service.require_thread(
            conversation_id, what=f"{MATCHING_WORKER} searching the inventory"
        )
        self._door(
            conversation,
            SEARCH_TOOL,
            worker=MATCHING_WORKER,
            what="ranking the inventory for this inquiry",
            at=at,
        )
        return M.match(
            self.conn,
            case_id=conversation.case_id,
            facts=facts,
            kernel=self.kernel(),
            now=at if at is not None else self.clock.now(),
            clock=self.clock,
            ids=self.ids,
            agency_id=self.agency_id,
        )

    # -- RES-W03: ask a source whether a unit is free ----------------------- #

    def request_availability(
        self,
        conversation_id: str,
        *,
        unit_id: str,
        check_in: Any = None,
        check_out: Any = None,
        notes: Optional[str] = None,
        at: Any = None,
    ) -> Any:
        """Ask a source whether one unit is free for the inquiry's window (RES-W03).

        The window defaults to the inquiry's **confirmed** stay and is never invented: a worker
        that guessed dates would ask a supplier about a trip nobody agreed to, and the guest
        would be quoted against it. Asking is how the platform finds out, not a reservation —
        no evidence is required to ask, and nothing here decides that the answer is true.

        The idempotency key is derived from the question (conversation, unit, window), so
        re-asking the *same* question replays the existing request instead of opening a second
        one. A different window is a different question and gets its own key.
        """

        conversation = self.service.require_thread(
            conversation_id, what=f"{AVAILABILITY_WORKER} asking about availability"
        )
        window_in, window_out = self._window(
            conversation, check_in=check_in, check_out=check_out, worker=AVAILABILITY_WORKER
        )
        self._door(
            conversation,
            AVAILABILITY_TOOL,
            worker=AVAILABILITY_WORKER,
            what=f"asking whether {unit_id} is free for {window_in}..{window_out}",
            at=at,
        )
        return self.kernel().request_availability_check(
            unit_id=unit_id,
            check_in=window_in,
            check_out=window_out,
            actor=self.service.actor,
            actor_role=self.service.actor_role,
            principal_id=self.service.principal(),
            idempotency_key=self.availability_key(
                conversation_id=conversation_id, unit_id=unit_id, check_in=window_in, check_out=window_out
            ),
            notes=notes,
        )

    # -- RES-W04: price the stay from the versions in force ----------------- #

    def price_stay(
        self,
        conversation_id: str,
        *,
        unit_id: str,
        check_in: Any = None,
        check_out: Any = None,
        adults: Any = None,
        children: Any = None,
        infants: Any = None,
        extras: Optional[Sequence[Mapping[str, Any]]] = None,
        discount_minor: int = 0,
        discount_reason: Optional[str] = None,
        at: Any = None,
    ) -> Any:
        """Answer what the inquiry's stay costs, from the versions in force (RES-W04).

        The window is the inquiry's **confirmed** stay unless the caller names another, and the
        guest counts default to the confirmed ones; a count the caller passes is handed through
        unchanged, so the request object is what refuses a bad value rather than this worker
        repairing it into a valid one. No guest count at all is refused here — a stay nobody
        agreed to is not priced.

        The answer is §56's ``villa.answer_verified``: the AI may tell a verified figure about
        this villa's own rates, and that is all this step is. It **records nothing** — the priced
        stay becomes a row inside RES-W05, after that step's door — so a kill switch that leaves
        the AI able to answer a price still leaves it unable to start an offer. No arithmetic
        happens in this file either: ``kernel.quote_stay`` chooses the policy version and the rate
        card through the rules the ranking already used and computes with ``quote_calculator``
        (§9: the commercial truth of a price comes from verified rate/contract versions, never
        from a model's memory).
        """

        conversation = self.service.require_thread(
            conversation_id, what=f"{PRICING_WORKER} pricing the stay"
        )
        window_in, window_out = self._window(
            conversation, check_in=check_in, check_out=check_out, worker=PRICING_WORKER
        )
        facts = self._requirements(conversation.case_id)
        guests = {
            "adults": adults if adults is not None else facts.get("adults"),
            "children": children if children is not None else facts.get("children", 0),
            "infants": infants if infants is not None else facts.get("infants", 0),
        }
        if guests["adults"] is None:
            raise MissingRequirements(
                "this inquiry has no confirmed guest count yet, so there is no stay to price; "
                "the worker will not assume how many people are coming",
                conversation_id=conversation.conversation_id,
                case_id=conversation.case_id,
                worker=PRICING_WORKER,
                missing=["adults"],
            )
        self._door(
            conversation,
            PRICING_TOOL,
            worker=PRICING_WORKER,
            what=f"pricing {unit_id} for {window_in}..{window_out}",
            at=at,
        )
        return self.kernel().quote_stay(
            unit_id=unit_id,
            check_in=window_in,
            check_out=window_out,
            adults=guests["adults"],
            children=guests["children"],
            infants=guests["infants"],
            extras=tuple(extras or ()),
            discount_minor=discount_minor,
            discount_reason=discount_reason,
        )

    # -- RES-W05: make the offer stand -------------------------------------- #

    def send_quote(
        self,
        conversation_id: str,
        *,
        unit_id: str,
        check_in: Any = None,
        check_out: Any = None,
        adults: Any = None,
        children: Any = None,
        infants: Any = None,
        extras: Optional[Sequence[Mapping[str, Any]]] = None,
        discount_minor: int = 0,
        discount_reason: Optional[str] = None,
        at: Any = None,
    ) -> Any:
        """Price, record, open and send the offer — §9's quotation step (RES-W05).

        One step, because §56 names one act: ``quotation.send``, MEDIUM, owner allow-listed. The
        four things it does are not four capabilities — pricing is the answer RES-W04 gives (and
        this step gives it again from the same basis, so the recorded figure and the answer
        cannot disagree), recording the priced stay writes the agency's own row, opening the
        version writes one the guest cannot see, and sending is what the guest sees. Splitting
        "draft but do not send" off would name an act the catalogue does not name.

        The order is the guarantee: the door is asked **before** anything is written, so a
        refused send leaves no quote row, no version and nothing a guest could ever be shown.
        A later send opens a new version and supersedes the one the guest held — which is why the
        previous version is untouched until the new one actually goes out.
        """

        conversation = self.service.require_thread(
            conversation_id, what=f"{QUOTATION_WORKER} sending the offer"
        )
        window_in, window_out = self._window(
            conversation, check_in=check_in, check_out=check_out, worker=QUOTATION_WORKER
        )
        facts = self._requirements(conversation.case_id)
        guests = {
            "adults": adults if adults is not None else facts.get("adults"),
            "children": children if children is not None else facts.get("children", 0),
            "infants": infants if infants is not None else facts.get("infants", 0),
        }
        if guests["adults"] is None:
            raise MissingRequirements(
                "this inquiry has no confirmed guest count yet, so there is no offer to make; "
                "the worker will not assume how many people are coming",
                conversation_id=conversation.conversation_id,
                case_id=conversation.case_id,
                worker=QUOTATION_WORKER,
                missing=["adults"],
            )
        self._door(
            conversation,
            QUOTE_TOOL,
            worker=QUOTATION_WORKER,
            what=f"sending the offer for {unit_id} {window_in}..{window_out}",
            at=at,
        )

        kernel = self.kernel()
        quote = kernel.quote_stay(
            unit_id=unit_id,
            check_in=window_in,
            check_out=window_out,
            adults=guests["adults"],
            children=guests["children"],
            infants=guests["infants"],
            extras=tuple(extras or ()),
            discount_minor=discount_minor,
            discount_reason=discount_reason,
        )
        quote_id = kernel.persist_quote(quote)
        version = self.quotation().draft(
            quote_id, actor=self.service.actor, actor_role=self.service.actor_role
        )
        return self.quotation().send(
            version.quotation_version_id,
            actor=self.service.actor,
            actor_role=self.service.actor_role,
            principal_id=self.service.principal(),
        )

    # -- RES-W08: the booking status --------------------------------------- #

    def booking_status(
        self,
        conversation_id: str,
        *,
        hold_id: Optional[str] = None,
        at: Any = None,
    ) -> BookingStatusAnswer:
        """Report where the reservation stands, and what §9 still requires (RES-W08).

        §9 is the *booking confirmation invariant*: ``BOOKING_CONFIRMED`` is valid only when the
        client accepted the applicable commercial version, the inventory is secured by an explicit
        supplier/villa confirmation, and the required client funds are verified. §56 gives the AI
        exactly one booking capability — ``booking.status``, LOW and read-only — and none of those
        three gates is an act this service may perform on its own behalf. So the worker reports: the
        row's own status when a booking exists, and the state of the three gates either way.

        The architecture's "Must Not" list is why the *shape* matters: the AI may not claim a
        requested hold is confirmed, nor that a reported payment is a received one. This answer makes
        that claim impossible to construct — ``booking_id`` is empty until a booking exists, and
        ``inventory["secures_slot"]`` is true only for a hold the supplier actually confirmed
        (§6's source of truth), never for one we merely asked for.

        ``hold_id`` names the hold the conversation is waiting on, because the estate does not yet
        bind holds to conversations (§9's ``hold_reference`` is still unwritten). The worker refuses
        rather than guess which hold a guest means.
        """

        conversation = self.service.require_thread(
            conversation_id, what=f"{BOOKING_WORKER} answering where the booking stands"
        )
        case = self._case(conversation.case_id)
        kernel = self.kernel()
        booking = kernel.get_booking(case.booking_id) if case.booking_id else None
        hold = kernel.get_hold(hold_id) if hold_id else None
        if booking is None and hold is None:
            raise MissingRequirements(
                "this case has no booking yet and no hold was named, so there is nothing to report; "
                "name the hold the guest is waiting on",
                conversation_id=conversation.conversation_id,
                case_id=case.case_id,
                worker=BOOKING_WORKER,
                missing=["hold_id"],
            )
        self._door(
            conversation,
            BOOKING_TOOL,
            worker=BOOKING_WORKER,
            what=f"the booking status of case {case.case_id}",
            at=at,
        )
        if booking is not None:
            unit_id: Any = booking["unit_id"]
            check_in: Any = booking["check_in"]
            check_out: Any = booking["check_out"]
        else:
            unit_id = hold["unit_id"]
            check_in = hold["check_in"]
            check_out = hold["check_out"]
        commercial = self._commercial_gate(unit_id, check_in, check_out)
        inventory = self._inventory_gate(booking=booking, hold=hold)
        financial = self._financial_gate(booking)
        missing = tuple(
            name
            for name, satisfied in (
                (GATE_COMMERCIAL, bool(commercial["accepted"])),
                (GATE_INVENTORY, bool(inventory["secures_slot"])),
                (GATE_FINANCIAL, bool(financial["recorded"])),
            )
            if not satisfied
        )
        status = None if booking is None else str(booking["status"])
        return BookingStatusAnswer(
            case_id=case.case_id,
            booking_id=None if booking is None else str(booking["booking_id"]),
            status=status,
            occupies_slot=bool(status is not None and status in BS.OCCUPYING_STATES),
            unit_id=unit_id,
            check_in=check_in,
            check_out=check_out,
            commercial=commercial,
            inventory=inventory,
            financial=financial,
            missing=missing,
        )

    def _commercial_gate(self, unit_id: str, check_in: str, check_out: str) -> Mapping[str, Any]:
        """§9's commercial gate, read from the module that owns the versions (§11)."""

        service = self.quotation()
        current = service.current(unit_id=unit_id, check_in=check_in, check_out=check_out)
        if current is None:
            return {"version_id": None, "status": None, "accepted": False, "accepted_at": None}
        acceptance = service.acceptance(current.quotation_version_id)
        return {
            "version_id": current.quotation_version_id,
            "status": current.status,
            "accepted": acceptance is not None,
            "accepted_at": None if acceptance is None else acceptance.accepted_at,
        }

    def _inventory_gate(self, *, booking: Any, hold: Any) -> Mapping[str, Any]:
        """§9's inventory gate: the supplier's confirmation, never our own request.

        A booking row already carries the hold it converted, so the slot is secured while the row
        occupies it. Before that, only a hold the supplier confirmed secures the slot —
        ``REQUESTED``/``AWAITING_SUPPLIER`` are our side of the conversation (§7).
        """

        if booking is not None:
            return {
                "source": "booking",
                "hold_ref": None if booking["hold_ref"] is None else str(booking["hold_ref"]),
                "status": str(booking["status"]),
                "secures_slot": str(booking["status"]) in BS.OCCUPYING_STATES,
            }
        status = str(hold["status"])
        return {
            "source": "hold",
            "hold_id": str(hold["hold_id"]),
            "status": status,
            "expires_at": str(hold["expires_at"]),
            "secures_slot": status == RK.HOLD_CONFIRMED,
        }

    def _financial_gate(self, booking: Any) -> Mapping[str, Any]:
        """§9's financial gate, as far as this platform can see it — see the note.

        ``recorded`` is deliberately narrow: it is true when the booking carries an amount a human
        confirmed, which is the closest fact the estate holds. It is *not* a claim that §9's
        ``required_client_funds_verified`` was checked, and the answer carries the note that says so.
        """

        return {
            "recorded": bool(booking is not None and int(booking["amount_paid_minor"]) > 0),
            "amount_paid_minor": None if booking is None else int(booking["amount_paid_minor"]),
            "currency": None if booking is None else str(booking["currency"]),
            "payment_status": None if booking is None else str(booking["payment_status"]),
            "note": FINANCIAL_GATE_NOTE,
        }

    # -- RES-W06: the routine follow-up ------------------------------------ #



    @staticmethod
    def _accepted_for_stay(quotation: Any, hold: Any) -> Any:
        """The accepted version for the stay a hold covers, or ``None``.

        A hold's own ``quote_id`` is a *derived* quote — the kernel prices the hold's window into a
        row of its own — so the offer the guest actually said yes to is found by walking the stay's
        chain through the module that owns it, not by following the hold's reference. ``versions``
        is oldest first and a re-negotiated stay can carry two acceptances: the last one governs.
        """

        accepted = [
            version
            for version in quotation.versions(
                unit_id=hold["unit_id"],
                check_in=hold["check_in"],
                check_out=hold["check_out"],
            )
            if version.status == "ACCEPTED"
        ]
        return accepted[-1] if accepted else None

    def cancellation_terms(
        self,
        conversation_id: str,
        *,
        hold_id: Optional[str] = None,
        at: Any = None,
    ) -> CancellationTerms:
        """Answer what the terms the guest accepted say about cancelling (RES-W09).

        §9's "amend/cancel assist" has a part that is a *question* and a part that is a *decision*.
        This is the question: a guest who asks what cancelling would cost is owed the ladder they
        actually agreed to. The binding source is the TERMS snapshot of the accepted version (§11)
        — never the policy in force today, which is how a guest ends up quoted a term nobody
        signed. With no accepted version the answer says so instead of inventing terms; with a
        notice inside every threshold it says *no step applies* instead of rounding a missing rule
        down to a zero refund.

        Read-only, through §56's LOW ``villa.answer_verified`` (the catalogue name RES-W04 answers
        a price with: one name, two questions), so this answer survives the §17 kill switch in
        READ_ONLY and writes nothing anywhere.

        ``hold_id`` names the hold when the stay has no booking yet; the booking's own ``quote_ref``
        wins when it exists, because that is the version the guest's yes was recorded against.
        """

        conversation = self.service.require_thread(
            conversation_id,
            what=f"{SPECIAL_REQUEST_WORKER} reading the cancellation terms",
        )
        case = self._case(conversation.case_id)
        kernel = self.kernel()
        booking = kernel.get_booking(case.booking_id) if case.booking_id else None
        hold = kernel.get_hold(hold_id) if hold_id else None
        self._door(
            conversation,
            TERMS_TOOL,
            worker=SPECIAL_REQUEST_WORKER,
            what="reading the cancellation terms the guest accepted",
            at=at,
        )
        quotation = self.quotation()
        version = None
        source = "none"
        if booking is not None and booking["quote_ref"]:
            version = quotation.accepted(str(booking["quote_ref"]))
            source = "booking"
        elif hold is not None:
            version = self._accepted_for_stay(quotation, hold)
            source = "hold"
        if version is None:
            return CancellationTerms(
                worker=SPECIAL_REQUEST_WORKER,
                conversation_id=conversation.conversation_id,
                binding=None,
                notice_days=None,
                ladder=None,
                applicable=None,
                note=TERMS_NOTE_NO_VERSION,
            )
        snapshot = quotation.snapshot(version.quotation_version_id, "TERMS")
        payload = json.loads(snapshot.payload) if snapshot is not None else {}
        ladder = payload.get("cancellation_ladder") or None
        acceptance = quotation.acceptance(version.quotation_version_id)
        check_in = _dt.date.fromisoformat(_iso_date(version.check_in, field="check_in"))
        today = _dt.date.fromisoformat(_iso_date(self.service._now(at), field="today"))
        notice_days = (check_in - today).days
        applicable = PE.cancellation_step(ladder, notice_days=notice_days) if ladder else None
        if ladder is None:
            note = TERMS_NOTE_NO_LADDER
        elif applicable is None:
            note = TERMS_NOTE_NO_STEP
        else:
            note = TERMS_NOTE_ACCEPTED
        return CancellationTerms(
            worker=SPECIAL_REQUEST_WORKER,
            conversation_id=conversation.conversation_id,
            binding={
                "quotation_version_id": version.quotation_version_id,
                "quote_id": version.quote_id,
                "version": version.version,
                "status": version.status,
                "check_in": str(version.check_in),
                "source": source,
                "accepted_at": None if acceptance is None else acceptance.accepted_at,
                "terms_fingerprint": None if snapshot is None else snapshot.fingerprint,
            },
            notice_days=notice_days,
            ladder=None if ladder is None else tuple(dict(step) for step in ladder),
            applicable=None if applicable is None else dict(applicable),
            note=note,
        )

    def special_request(
        self,
        conversation_id: str,
        *,
        request_kind: str,
        slot: str,
        assignee: str,
        due_at: Any,
        checklist: Sequence[Any],
        priority: Any = OT.DEFAULT_PRIORITY,
        at: Any = None,
    ) -> tuple[Any, bool]:
        """Put the guest's amendment/cancellation request on a person's queue (RES-W09).

        §9's MEDIUM band is **coordination**, not decision: a cancellation or a date change moves
        money (the ladder, a refund, a penalty) and re-prices an offer, and the estate refuses the
        machine both acts twice over — every money tool is HIGH and in the §21 deny registry, and
        :mod:`approvals` will not take a machine principal at all ("an approval is a human act";
        :mod:`roles` gives ``ai_service`` no rank to request under). What it may do is what the
        collections sweep already does with an overdue invoice: file the work where a person
        cannot miss it, and let a person raise the approval and move the booking.

        ``checklist`` is required because the queue's closed ``kind`` vocabulary cannot name a
        cancellation: a routed change is a ``GUEST_FOLLOWUP`` task on the booking (or on the case,
        before a booking exists) and the runbook is the only field that can say what was asked —
        an empty one would reach a person as an unreadable interruption.

        Idempotent on ``(conversation, request_kind, slot)``: a retry after a restart files
        nothing, while a cancellation and an amendment on the same slot stay two pieces of work.
        The deadline and the owner are the caller's; this module publishes no SLA and invents no
        assignee.
        """

        conversation = self.service.require_thread(
            conversation_id,
            what=f"{SPECIAL_REQUEST_WORKER} routing a guest request",
        )
        wanted = require_text(request_kind, "request_kind", max_len=32).strip().upper()
        if wanted not in SPECIAL_REQUEST_KINDS:
            raise MissingRequirements(
                f"{SPECIAL_REQUEST_WORKER} may route "
                f"{list(SPECIAL_REQUEST_KINDS)} — not {wanted!r}",
                code="RES_WORKER_UNKNOWN_REQUEST_KIND",
                kind=wanted,
                known=list(SPECIAL_REQUEST_KINDS),
            )
        slot = require_ref(slot, "slot", max_len=64)
        require_ref(assignee, "assignee", max_len=64)
        steps = tuple(checklist)
        if len(steps) < SPECIAL_REQUEST_MIN_STEPS:
            raise MissingRequirements(
                f"{SPECIAL_REQUEST_WORKER} must file this under a runbook that names it: the "
                "queue's kind cannot say what the guest asked for",
                code="RES_WORKER_SPECIAL_REQUEST_NO_STEPS",
                minimum=SPECIAL_REQUEST_MIN_STEPS,
            )
        case = self._case(conversation.case_id)
        self._door(
            conversation,
            SPECIAL_REQUEST_TOOL,
            worker=SPECIAL_REQUEST_WORKER,
            what=f"routing the guest's {wanted.lower()} request on {slot}",
            at=at,
        )
        task, created = self.ops().ensure_task(
            kind=OT.KIND_GUEST_FOLLOWUP,
            assignee=assignee,
            due_at=due_at,
            idempotency_key=self.special_key(
                conversation_id=conversation.conversation_id,
                request_kind=wanted,
                slot=slot,
            ),
            priority=priority,
            booking_id=case.booking_id or None,
            case_id=None if case.booking_id else case.case_id,
            checklist=steps,
        )
        return task, created

    def follow_up(
        self,
        conversation_id: str,
        *,
        slot: str,
        due_at: Any,
        assignee: str,
        priority: str = OT.DEFAULT_PRIORITY,
        checklist: Sequence[str] = (),
        at: Any = None,
    ) -> tuple[Any, bool]:
        """File the follow-up this inquiry is owed as *our* work (RES-W06).

        §56 names this act ``followup.routine`` and tiers it LOW, and the estate has already
        decided what a routine follow-up is: an **internal task/reminder**, not a message. That is
        enforced one layer down — :meth:`messages.MessageService.draft` requires a human author,
        because approval and sending are human acts — so a machine that "followed up" by drafting
        words would be writing under a person's name. What the AI may do is put the work on the
        agency's own queue and let a person pick it up, which is also what the collections sweep
        does with an overdue invoice ("by opening a task for a human, never by contact").

        ``slot`` is the piece of work this follow-up belongs to (the version the guest holds, the
        hold that is running out, ``no-answer-yet``). It is an argument rather than a timestamp so
        that the *same* follow-up is the same task: a retry, or a second worker, files nothing.
        The deadline is the caller's — this module publishes no follow-up SLA and will not invent
        one — and the queue will not let it be moved by a replay: same slot with a different
        ``due_at`` is refused rather than silently rescheduled.

        ``assignee`` is required for the same reason the queue requires it: a task without a named
        owner is an unassigned promise, and choosing who chases a guest is a decision, not a
        default this module may fill in.

        Returns ``(task, created)`` exactly as :meth:`ops_tasks.OpsTaskService.ensure_task` does.
        """

        conversation = self.service.require_thread(
            conversation_id, what=f"{FOLLOWUP_WORKER} filing the routine follow-up"
        )
        slot = require_ref(slot, "slot", max_len=64)
        require_ref(assignee, "assignee", max_len=64)
        self._door(
            conversation,
            FOLLOWUP_TOOL,
            worker=FOLLOWUP_WORKER,
            what=f"filing the routine follow-up on {slot}",
            at=at,
        )
        task, created = self.ops().ensure_task(
            kind=OT.KIND_GUEST_FOLLOWUP,
            assignee=assignee,
            due_at=due_at,
            idempotency_key=self.followup_key(
                conversation_id=conversation.conversation_id, slot=slot
            ),
            priority=priority,
            case_id=conversation.case_id,
            checklist=tuple(checklist),
        )
        return task, created

    # -- RES-W07: the tentative hold --------------------------------------- #

    def request_hold(
        self,
        conversation_id: str,
        *,
        unit_id: str,
        check_in: Any = None,
        check_out: Any = None,
        quote_ref: Optional[str] = None,
        evidence_id: Optional[str] = None,
        ttl_minutes: Optional[int] = None,
        at: Any = None,
    ) -> Any:
        """Ask the kernel for a tentative hold on one unit (RES-W07).

        This is §9's second MEDIUM act — ``hold.request_tentative``, owner allow-listed — and the
        first one that takes something **out of sale**: while the hold runs, other guests' checks
        must treat the unit as taken. That outside effect is why the catalogue tiers it above the
        AI's autonomous band.

        Everything the kernel needs is derived from the versions in force instead of accepted from
        the caller: the stay from the inquiry's confirmed facts, the guest from the case row, the
        policy version from :func:`matching.policy_for` (the lookup the ranking and the price
        already used). The idempotency key comes from the question — this conversation, this unit,
        this window — so a retry asks once, while the same unit on other dates is another
        question.

        The kernel re-checks the actor, so this act passes two gates: this module's door, which
        decides whether the AI may act at all under the §17 mode, and the owning module, which
        decides whether *this* hold is legal. Neither is a second opinion about the other.
        """

        conversation = self.service.require_thread(
            conversation_id, what=f"{HOLD_WORKER} asking for a tentative hold"
        )
        window_in, window_out = self._window(
            conversation, check_in=check_in, check_out=check_out, worker=HOLD_WORKER
        )
        guest_ref = self._case(conversation.case_id).guest_ref
        self._door(
            conversation,
            HOLD_TOOL,
            worker=HOLD_WORKER,
            what=f"a tentative hold on {unit_id} {window_in}..{window_out}",
            at=at,
        )
        return self.kernel().request_hold(
            unit_id=unit_id,
            check_in=window_in,
            check_out=window_out,
            guest_ref=guest_ref,
            policy_version_id=self._policy_in_force(unit_id, window_in, window_out),
            idempotency_key=self.hold_key(
                conversation_id=conversation.conversation_id,
                unit_id=unit_id,
                check_in=window_in,
                check_out=window_out,
            ),
            actor=self.service.actor,
            actor_role=self.service.actor_role,
            principal_id=self.service.principal(),
            quote_ref=quote_ref,
            evidence_id=evidence_id,
            ttl_minutes=ttl_minutes,
        )

    def _window(
        self, conversation: Conversation, *, check_in: Any, check_out: Any, worker: str
    ) -> tuple[str, str]:
        """The window to ask about: what the caller passed, else the inquiry's confirmed stay.

        ``worker`` is the code of the caller, because this rule is shared: a refusal has to
        name the worker that actually asked, or an operator reading the incident would chase
        the wrong part of the workforce.
        """

        if check_in is not None and check_out is not None:
            return _iso_date(check_in, field="check_in"), _iso_date(check_out, field="check_out")
        facts = self._requirements(conversation.case_id)
        resolved_in = check_in if check_in is not None else facts.get("check_in")
        resolved_out = check_out if check_out is not None else facts.get("check_out")
        if resolved_in is None or resolved_out is None:
            raise MissingRequirements(
                "this inquiry has no confirmed stay yet, so there is no window to ask about; "
                "the worker will not guess the dates",
                conversation_id=conversation.conversation_id,
                case_id=conversation.case_id,
                worker=worker,
                missing=[
                    name
                    for name, value in (("check_in", resolved_in), ("check_out", resolved_out))
                    if value is None
                ],
            )
        return _iso_date(resolved_in, field="check_in"), _iso_date(resolved_out, field="check_out")

    @staticmethod
    def availability_key(
        *, conversation_id: str, unit_id: str, check_in: str, check_out: str
    ) -> str:
        """The idempotency key for one availability question, derived rather than random.

        Derived, because the question is what must not be asked twice: a retry after a timeout
        and a duplicate from a second worker are the same question, while the same unit on
        other dates is not.
        """

        digest = sha256_hex(
            "ai-availability-request", conversation_id, unit_id, check_in, check_out
        )
        return f"ai-avail-{digest[:32]}"

    @staticmethod
    def followup_key(*, conversation_id: str, slot: str) -> str:
        """The idempotency key for one follow-up, derived from the work slot it belongs to.

        Derived, because "have we already chased this?" is a question about the work, not about
        when someone asked. A retry after a restart and a second worker waking up to the same
        silence are the same follow-up; a chased version and a chased hold are not.
        """

        digest = sha256_hex("ai-followup", conversation_id, slot)
        return f"ai-follow-{digest[:32]}"

    @staticmethod
    def special_key(*, conversation_id: str, request_kind: str, slot: str) -> str:
        """The idempotency key for one routed request: the kind of change plus its work slot.

        Derived, because "did we already route this?" is a question about the work, not about when
        someone asked. A cancellation and an amendment on the same slot are two different pieces of
        work and must not collide on one key; a retry of either is the same piece and must.
        """

        digest = sha256_hex("ai-special-request", conversation_id, request_kind, slot)
        return f"ai-special-{digest[:32]}"

    @staticmethod
    def hold_key(*, conversation_id: str, unit_id: str, check_in: str, check_out: str) -> str:
        """The idempotency key for one hold question, derived rather than random.

        Derived, because the question is what must not be asked twice: a retry after a timeout
        and a duplicate from a second worker are the same request, while the same unit on other
        dates is a different one.
        """

        digest = sha256_hex("ai-hold-request", conversation_id, unit_id, check_in, check_out)
        return f"ai-hold-{digest[:32]}"
