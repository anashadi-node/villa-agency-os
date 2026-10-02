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

``intent`` is a short operational sentence written by *us*. The gateway records it and never
consults it (§56: conversation text cannot raise privilege), so pasting what a guest wrote
into it would put guest text into the audit chain in exchange for nothing.

Scope: five of §9's jobs — inquiry (RES-W01), matching (RES-W02), availability (RES-W03),
pricing (RES-W04) and the offer (RES-W05). The rest of the job list arrives with the modules it
needs and follows the same three steps: guard, door, delegate.

Two of the five are worth reading together with §56. Pricing has no capability of its own — §56
names the *answer* (``villa.answer_verified``) — so RES-W04 writes nothing; the priced stay
becomes a row inside RES-W05, whose one act is the one §56 names for the offer
(``quotation.send``, MEDIUM, owner allow-listed). A kill switch that leaves the AI able to answer
a price therefore still leaves it unable to start an offer.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any, Mapping, Optional, Sequence

import crm as CRM
import intake as IN
import matching as M
import quotation as Q
import reservation_kernel as RK
from primitives import DomainError, sha256_hex
from reservation_service import Conversation, ReservationService


#: The §9 capability worker codes. The code is what an operator greps for when asking "who in
#: the AI workforce did this?" — a worker's name is part of its output, not a comment.
INQUIRY_WORKER = "RES-W01"
MATCHING_WORKER = "RES-W02"
AVAILABILITY_WORKER = "RES-W03"
PRICING_WORKER = "RES-W04"
QUOTATION_WORKER = "RES-W05"

#: The catalogue names, spelled once here and pinned against :data:`ai_service`'s ``ai_tools``
#: rows by ``test_the_tools_the_workers_hold_are_the_tools_the_catalogue_declares``. A typo must
#: fail the suite, not be discovered by the door at run time.
INTAKE_TOOL = "inquiry.intake"
SEARCH_TOOL = "villa.search"
#: Named through the module that enforces each of these, so a rename cannot leave one
#: file gating a tool the other no longer knows about.
AVAILABILITY_TOOL = RK.AVAILABILITY_REQUEST_TOOL
QUOTE_TOOL = Q.SEND_TOOL

#: Pricing is §56's *answer* capability, not a commercial act of its own: the AI tells a
#: verified figure about this villa's rates. §56 has no separate name for the arithmetic (or for
#: opening a version the guest cannot see), and the catalogue is the closed §56 list — so the
#: spelling here is the answer's, and what the step must not do is made true by the code instead:
#: RES-W04 writes nothing at all.
PRICING_TOOL = "villa.answer_verified"

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
