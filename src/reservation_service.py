"""§9 Agent 3 — the AI Reservation Service: the conversation ledger and the §13 ladder.

This module is the *state* an AI employee has, and nothing else. It owns exactly two things:

* **A conversation** — one guest thread, isolated by tenant, by inquiry and by guest. §5 of
  the AI Workforce Architecture says the workflow must live outside worker memory (a crash
  must not lose a reservation) and §9's *Conversation Isolation Minimum* names the context
  that may never be shared. A row in ``ai_conversations`` is that boundary. A dictionary in a
  worker would be neither durable nor isolated from the next conversation the worker picks up.
* **The §13 human-handoff ladder** — ``AI_RUNNING`` → ``ESCALATION_TRIGGERED`` →
  ``HANDOFF_PENDING`` → ``HUMAN_ASSIGNED`` → ``AI_WRITE_SUSPENDED`` → ``HUMAN_HANDLING`` →
  ``RESOLUTION`` → ``AI_RESUME_ALLOWED``, walked one rung at a time, each rung an append-only
  event that names its actor and its reason.

What it deliberately does **not** do:

* It does not copy requirements, candidates, prices or payment state into itself. Each of
  those has one owner (intake, matching, quotation, payments) and a copy here would be a
  second answer to the same question — §15's failure. :meth:`ReservationService.handoff_package`
  resolves them *by reference* at the moment a human needs them.
* It does not decide whether the AI may act at all. That is the gateway's answer
  (:mod:`ai_gateway`), and the §56 tool allowance's answer (:mod:`ai_service`). This module
  asks, and refuses to continue when the answer is no.
* It does not store guest identity. ``guest_ref`` is the opaque reference the agency already
  uses; §9 forbids raw identity documents in model context, and a column that does not exist
  cannot leak into one.

Why the ladder is walked rather than skipped
--------------------------------------------
The rungs are not ceremony. ``ESCALATION_TRIGGERED`` is the moment the AI stops speaking to
the guest; ``HANDOFF_PENDING`` is the moment a *named* human is asked to take it;
``HUMAN_ASSIGNED`` is the moment somebody owns it; ``AI_WRITE_SUSPENDED`` is the moment the AI
stops touching it at all. A caller that could jump from ``AI_RUNNING`` to ``HUMAN_HANDLING``
would leave the guest unwatched in between — which is exactly the window where an unguarded
message does the most damage. So the transition table below is closed, and
:meth:`ReservationService._move` refuses a rung whose predecessor is not the current one.

The write rule, stated once
---------------------------
**Only ``AI_RUNNING`` lets the AI write.** Every other rung — including the ones a human is
still walking towards — refuses a write. This is the narrow reading of §13's sentence ("once a
human takes control, conflicting AI outbound messages must stop"): between the trigger firing
and the human arriving, the AI is already silent, so no message can cross a pending handoff.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

from primitives import (
    DomainError,
    IdFactory,
    SystemClock,
    fingerprint,
    require_ref,
    require_text,
    to_iso,
)
from policy_engine import PolicyValueNotPublished
from supplier_route import is_machine_actor

#: The §9 service acts as a named machine principal. The name is the *service*, never the
#: individual worker: §9 scales by concurrent workers, not by minting identities.
DEFAULT_ACTOR = "ai:reservation-service"
DEFAULT_ACTOR_ROLE = "ai_service"

DEFAULT_AGENCY_ID = "agency_default"

#: The eight §13 states, in ladder order. ``AI_RUNNING`` is the first rung and the last one:
#: a conversation that was resumed stands where it started.
CONVERSATION_STATES: tuple[str, ...] = (
    "AI_RUNNING",
    "ESCALATION_TRIGGERED",
    "HANDOFF_PENDING",
    "HUMAN_ASSIGNED",
    "AI_WRITE_SUSPENDED",
    "HUMAN_HANDLING",
    "RESOLUTION",
    "AI_RESUME_ALLOWED",
)

#: The only rung at which the AI may write anything at all (see the module docstring).
WRITE_STATE = "AI_RUNNING"

#: ``kind`` -> ``(from_state, to_state)``. ``None`` means "no predecessor": a conversation
#: starts on its first event. The table is the ladder; :meth:`_move` reads it and nothing else,
#: so there is one place to look when asking "how can this conversation get there?".
LADDER: Mapping[str, tuple[Optional[str], str]] = MappingProxyType(
    {
        "OPENED": (None, "AI_RUNNING"),
        "ESCALATED": ("AI_RUNNING", "ESCALATION_TRIGGERED"),
        "HANDOFF_REQUESTED": ("ESCALATION_TRIGGERED", "HANDOFF_PENDING"),
        "HUMAN_TAKEN": ("HANDOFF_PENDING", "HUMAN_ASSIGNED"),
        "WRITES_SUSPENDED": ("HUMAN_ASSIGNED", "AI_WRITE_SUSPENDED"),
        "HANDLING_STARTED": ("AI_WRITE_SUSPENDED", "HUMAN_HANDLING"),
        "RESOLVED": ("HUMAN_HANDLING", "RESOLUTION"),
        "RESUME_ALLOWED": ("RESOLUTION", "AI_RESUME_ALLOWED"),
        "RESUMED": ("AI_RESUME_ALLOWED", "AI_RUNNING"),
    }
)

#: The §9 *Mandatory Escalation* list, as a closed vocabulary. A caller may not invent a code:
#: an unknown trigger is refused rather than stored, because a free-text reason is not a
#: trigger anyone can count, route or test.
#:
#: Six of these are detected here, by a deterministic rule (§9's own wording: "availability
#: conflict", "funds after hold expiry", "out-of-policy discount", "AI uncertainty above
#: configured threshold", plus the two the platform already has playbooks for). The rest
#: describe what a *person* said — a legal threat, a complaint, a request for a human — and no
#: rule in this repository can read a guest's intent. Those are declared by the layer that
#: read the message (the channel adapter, with the model's classification) and validated
#: here; the honest split is "the model classifies, the service enforces".
ESCALATION_TRIGGERS: tuple[str, ...] = (
    "OUT_OF_POLICY_DISCOUNT",
    "HIGH_VALUE_NEGOTIATION",
    "AMBIGUOUS_CONTRACT_OR_SUPPLIER_DATA",
    "AVAILABILITY_CONFLICT",
    "FUNDS_AFTER_HOLD_EXPIRY",
    "SERIOUS_COMPLAINT",
    "SAFETY_OR_EMERGENCY",
    "LEGAL_THREAT",
    "UNUSUAL_CANCELLATION",
    "PRIVACY_OR_SECURITY_MATTER",
    "CUSTOMER_REQUESTS_HUMAN",
    "AI_UNCERTAINTY",
)

#: Which tenant role each trigger hands the conversation to, named as a **blueprint** from
#: :mod:`agency_roles` rather than as a role id: a tenant renames its roles in its own language
#: (ADR-0035 amendment) and authority follows ``based_on``, never a label. Reserving triggers
#: go to the Operations/Reservation Manager; the three that carry material, legal or security
#: weight go to the GM; money that arrived after its hold expired goes to Finance, which is
#: where the question "what do we do with this payment?" is actually answered.
#:
#: Changing an entry is a policy act, not a code edit — the same rule
#: :data:`exception_engine.FAILURE_ROUTES` carries.
BLUEPRINT_OF_TRIGGER: Mapping[str, str] = MappingProxyType(
    {
        "OUT_OF_POLICY_DISCOUNT": "reservation_manager",
        "HIGH_VALUE_NEGOTIATION": "gm",
        "AMBIGUOUS_CONTRACT_OR_SUPPLIER_DATA": "reservation_manager",
        "AVAILABILITY_CONFLICT": "reservation_manager",
        "FUNDS_AFTER_HOLD_EXPIRY": "finance_manager",
        "SERIOUS_COMPLAINT": "reservation_manager",
        "SAFETY_OR_EMERGENCY": "gm",
        "LEGAL_THREAT": "gm",
        "UNUSUAL_CANCELLATION": "reservation_manager",
        "PRIVACY_OR_SECURITY_MATTER": "system_admin",
        "CUSTOMER_REQUESTS_HUMAN": "reservation_manager",
        "AI_UNCERTAINTY": "reservation_manager",
    }
)

#: The triggers that are *failures* as well as escalations: the ones with a playbook in
#: :data:`exception_engine.FAILURE_ROUTES`. An escalation opens an incident through the one
#: engine every other failure already goes through, so a stuck unit or money that arrived
#: against an expired hold is counted and reported the same way wherever it was noticed.
INCIDENT_OF_TRIGGER: Mapping[str, str] = MappingProxyType(
    {
        "AVAILABILITY_CONFLICT": "VENUE_UNAVAILABLE",
        "FUNDS_AFTER_HOLD_EXPIRY": "PAYMENT_UNRECONCILED",
        "SAFETY_OR_EMERGENCY": "SAFETY_EMERGENCY",
    }
)

#: §9: "AI uncertainty above configured threshold". Published, never hard-coded — and ships
#: unpublished, so until an owner writes the number down the service refuses to act on any
#: judgement that came from language. "The model sounded sure" is not a value anybody decided.
MIN_CONFIDENCE_KEY = "security.ai.escalation_min_confidence"

#: Outbox event types. Only the two rungs that require a human to *act* are announced:
#: elsewhere in the estate an event per rung would be noise, and noise is how a real alert
#: gets missed.
ESCALATED_EVENT = "ai.conversation.escalated"
HANDOFF_REQUESTED_EVENT = "ai.conversation.handoff_requested"

#: How many ranked candidates a handoff package carries. A human reads a shortlist; the full
#: run stays in ``match_candidates`` where matching wrote it.
PACKAGE_CANDIDATE_LIMIT = 10

#: Domains for the fingerprints this module derives, so a digest can never be replayed as
#: evidence of a different kind of record (§27: a redaction must not corrupt a digest either).
PACKAGE_DOMAIN = "villa-agency-os.reservation.handoff"


class ReservationServiceError(DomainError):
    """Base class for every refusal this service makes."""

    code = "RESERVATION_SERVICE_ERROR"


class ConversationNotFound(ReservationServiceError):
    """No conversation with that id inside this tenant. Also the answer to a cross-tenant id."""

    code = "RES_CONVERSATION_NOT_FOUND"


class ConversationExists(ReservationServiceError):
    """This inquiry already has an AI conversation on this channel (the §13 duplicate-thread failure)."""

    code = "RES_CONVERSATION_EXISTS"


class UnknownChannel(ReservationServiceError):
    code = "RES_UNKNOWN_CHANNEL"


class UnknownConsentState(ReservationServiceError):
    code = "RES_UNKNOWN_CONSENT_STATE"


class UnknownTrigger(ReservationServiceError):
    """A trigger code outside the §9 vocabulary: refused, never stored as free text."""

    code = "RES_UNKNOWN_TRIGGER"


class UnknownReference(ReservationServiceError):
    """A reference kind this service does not carry."""

    code = "RES_UNKNOWN_REFERENCE"


class RungRefused(ReservationServiceError):
    """The conversation is not on the rung the caller assumed, so the move is refused."""

    code = "RES_RUNG_REFUSED"


class WritesSuspended(ReservationServiceError):
    """The AI may not write: this conversation is with a human (or on its way to one)."""

    code = "RES_WRITES_SUSPENDED"


class HumanRequired(ReservationServiceError):
    """This rung is a human act. A machine principal cannot take control or allow a resume."""

    code = "RES_HUMAN_REQUIRED"


class HandoffRoleUnstaffed(ReservationServiceError):
    """Nobody holds the role this handoff targets: fail closed rather than hand a guest to a void."""

    code = "RES_HANDOFF_ROLE_UNSTAFFED"


class ConfidenceNotPublished(ReservationServiceError):
    """§9's threshold has no published value, so the service may not act on language at all."""

    code = "RES_CONFIDENCE_NOT_PUBLISHED"


class ConfidenceTooLow(ReservationServiceError):
    """The reported confidence is below the published threshold: a human reads it instead."""

    code = "RES_CONFIDENCE_TOO_LOW"


class EscalationRaised(ReservationServiceError):
    """Raised *after* a conversation was escalated, so the caller cannot continue as if it passed."""

    code = "RES_ESCALATED"


@dataclass(frozen=True)
class Conversation:
    """One guest thread, as it stands. Immutable: a caller cannot hold a stale mutable row."""

    conversation_id: str
    agency_id: str
    principal_id: str
    case_id: str
    guest_ref: str
    channel: str
    consent_state: str
    quote_ref: Optional[str]
    hold_ref: Optional[str]
    reservation_ref: Optional[str]
    opened_at: str
    updated_at: str
    state: str

    @property
    def writes_allowed(self) -> bool:
        return self.state == WRITE_STATE

    @property
    def with_human(self) -> bool:
        """True from the moment the AI stopped speaking: a human owns (or is owed) this thread."""

        return self.state != WRITE_STATE


@dataclass(frozen=True)
class ConversationEvent:
    """One rung, as written. The log is the proof of who moved the conversation."""

    event_id: str
    conversation_id: str
    seq: int
    kind: str
    state: str
    trigger_code: Optional[str]
    handoff_blueprint: Optional[str]
    human_ref: Optional[str]
    package_fingerprint: Optional[str]
    actor: str
    actor_role: str
    reason: Optional[str]
    occurred_at: str


@dataclass(frozen=True)
class HandoffPackage:
    """§13's brief for the human who takes over: context, verified facts, states, reason.

    Carry **references**, not copies. ``requirements`` are the facts intake marked ``ACCEPTED``
    (§16: only a reviewed field is a fact), ``candidates`` are the newest matching run's ranked
    rows with their own reasons, and every other record is named by id so the human opens it in
    the system that owns it — under the access rules that record already has. The message
    bodies are not copied here on purpose: they live in ``case_messages`` behind the retention
    and content-class rules of §8/§9, and a package that duplicated them would be a second
    place those rules would have to be enforced.
    """

    conversation_id: str
    case_id: str
    case_reference: Optional[str]
    state: str
    trigger_code: Optional[str]
    reason: str
    handoff_blueprint: str
    guest_ref: str
    channel: str
    consent_state: str
    requirements: Mapping[str, Any]
    unknown_required: tuple[str, ...]
    candidates: tuple[Mapping[str, Any], ...]
    match_run_id: Optional[str]
    quote_ref: Optional[str]
    hold_ref: Optional[str]
    reservation_ref: Optional[str]
    payment_state: Optional[str]
    sla: Mapping[str, str]
    pending_actions: tuple[str, ...]
    fingerprint: str

    def as_payload(self) -> dict[str, Any]:
        """The dict the fingerprint is taken over: stable order, no set, no float."""

        return {
            "conversation_id": self.conversation_id,
            "case_id": self.case_id,
            "state": self.state,
            "trigger_code": self.trigger_code,
            "reason": self.reason,
            "handoff_blueprint": self.handoff_blueprint,
            "guest_ref": self.guest_ref,
            "channel": self.channel,
            "consent_state": self.consent_state,
            "requirements": dict(self.requirements),
            "unknown_required": list(self.unknown_required),
            "candidates": [dict(row) for row in self.candidates],
            "match_run_id": self.match_run_id,
            "quote_ref": self.quote_ref,
            "hold_ref": self.hold_ref,
            "reservation_ref": self.reservation_ref,
            "payment_state": self.payment_state,
            "sla": dict(self.sla),
            "pending_actions": list(self.pending_actions),
        }


class ReservationService:
    """The §9 conversation ledger and the §13 ladder, for one tenant.

    Everything this class writes goes through :meth:`_move`, so a transition always lands as
    *one* append-only event next to *one* audit row, in one transaction. Callers arrive from
    the capability workers (a later step of this stage) and from the operator CLI; neither gets
    its own path to the tables.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        gateway: Any,
        policy: Any,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        audit: Any = None,
        outbox: Any = None,
        exceptions: Any = None,
        agency_id: str = DEFAULT_AGENCY_ID,
        principal_id: Optional[str] = None,
        actor: str = DEFAULT_ACTOR,
        actor_role: str = DEFAULT_ACTOR_ROLE,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.gateway = gateway
        self.policy = policy
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.audit = audit
        self.outbox = outbox
        self.exceptions = exceptions
        self.agency_id = require_ref(agency_id, "agency_id")
        self.principal_id = principal_id
        self.actor = require_ref(actor, "actor")
        self.actor_role = require_text(actor_role, "actor_role", max_len=64)
        self._channels = self._channel_vocabulary()

    # -- plumbing ----------------------------------------------------------- #

    def _channel_vocabulary(self) -> tuple[str, ...]:
        """The channel values the schema accepts, read from the schema rather than restated.

        A second hard-coded list would be a second answer to "which channels exist", and the
        schema is the one that actually refuses a bad value.
        """

        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ai_conversations'"
        ).fetchone()
        if row is None:  # pragma: no cover - the schema is applied before any service exists
            raise ReservationServiceError(
                "ai_conversations is missing: apply the schema before opening a conversation",
                code="RES_SCHEMA_MISSING",
                table="ai_conversations",
            )
        import re

        match = re.search(r"channel\s+IN\s*\(([^)]*)\)", str(row["sql"]))
        if match is None:  # pragma: no cover - defence against a schema edit without a test
            raise ReservationServiceError(
                "ai_conversations.channel has no CHECK vocabulary to follow",
                code="RES_SCHEMA_MISSING",
                table="ai_conversations",
            )
        return tuple(part.strip().strip("'") for part in match.group(1).split(","))

    def _now(self, at: Any = None) -> str:
        return to_iso(at if at is not None else self.clock.now())

    def _transaction(self) -> Any:
        return _transaction(self.conn)

    def _audit(
        self,
        *,
        action: str,
        conversation_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        reason: Optional[str],
        actor: str,
        actor_role: str,
    ) -> None:
        if self.audit is None:
            return
        self.audit.append(
            action=action,
            entity_type="ai_conversation",
            entity_id=conversation_id,
            actor=actor,
            actor_role=actor_role,
            before=dict(before),
            after=dict(after),
            reason=reason,
        )

    # -- reads -------------------------------------------------------------- #

    def get(self, conversation_id: str) -> Conversation:
        """One conversation, scoped to this tenant. A foreign id is *not found*, not forbidden."""

        conversation_id = require_ref(conversation_id, "conversation_id")
        row = self.conn.execute(
            """
            SELECT c.*, (
                SELECT e.state FROM ai_conversation_events e
                WHERE e.conversation_id = c.conversation_id
                ORDER BY e.seq DESC LIMIT 1
            ) AS state
            FROM ai_conversations c
            WHERE c.conversation_id = ? AND c.agency_id = ?
            """,
            (conversation_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise ConversationNotFound(
                "no such conversation in this agency",
                conversation_id=conversation_id,
                agency_id=self.agency_id,
            )
        return _conversation(row)

    def state(self, conversation_id: str) -> str:
        return self.get(conversation_id).state

    def for_case(self, case_id: str) -> tuple[Conversation, ...]:
        """Every conversation this tenant has on one inquiry, oldest first."""

        case_id = require_ref(case_id, "case_id")
        rows = self.conn.execute(
            """
            SELECT c.*, (
                SELECT e.state FROM ai_conversation_events e
                WHERE e.conversation_id = c.conversation_id
                ORDER BY e.seq DESC LIMIT 1
            ) AS state
            FROM ai_conversations c
            WHERE c.case_id = ? AND c.agency_id = ?
            ORDER BY c.opened_at, c.conversation_id
            """,
            (case_id, self.agency_id),
        ).fetchall()
        return tuple(_conversation(row) for row in rows)

    def events(self, conversation_id: str) -> tuple[ConversationEvent, ...]:
        """The whole ladder, oldest first — what an incident review reads."""

        conversation = self.get(conversation_id)
        rows = self.conn.execute(
            "SELECT * FROM ai_conversation_events WHERE conversation_id = ? ORDER BY seq",
            (conversation.conversation_id,),
        ).fetchall()
        return tuple(_event(row) for row in rows)

    def may_write(self, conversation_id: str) -> bool:
        """§13's write rule, as one call, so no worker has to remember the comparison."""

        return self.get(conversation_id).writes_allowed

    def require_write(self, conversation_id: str, *, what: str) -> Conversation:
        """The guard the write path opens with: refuse to act on a handed-off thread."""

        return self.require_thread(conversation_id, what=what)

    def require_thread(self, conversation_id: str, *, what: str) -> Conversation:
        """The guard every capability worker opens with: the AI must hold this thread to act.

        The rule is one rung wide on purpose. ``AI_RUNNING`` is the only state in which the AI
        says anything *or* looks anything up for a conversation: a thread a human owns (or is
        owed) is not the AI's to search, ask about or answer, and a capability worker that
        checked only the writes would still be a second voice in a conversation the §13 ladder
        had already taken away from it. Reads are refused with the same error as writes, so an
        operator reading the audit sees the same reason whichever worker strayed.
        """

        conversation = self.get(conversation_id)
        if not conversation.writes_allowed:
            raise WritesSuspended(
                f"{what} refused: this conversation is not the AI's to touch",
                conversation_id=conversation.conversation_id,
                state=conversation.state,
            )
        return conversation

    # -- opening ------------------------------------------------------------ #

    def open_conversation(
        self,
        *,
        case_id: str,
        guest_ref: str,
        channel: str,
        consent_state: str = "UNKNOWN",
        at: Any = None,
    ) -> Conversation:
        """Open the AI's thread on an inquiry. One per (inquiry, channel), enforced by the schema.

        The inquiry must exist **in this tenant**: a conversation that pointed at another
        agency's case would be a cross-tenant read waiting to happen, so the lookup is scoped
        and a foreign case is refused as unknown.
        """

        case_id = require_ref(case_id, "case_id")
        guest_ref = require_text(guest_ref, "guest_ref", max_len=128)
        channel = require_text(channel, "channel", max_len=32).lower()
        consent_state = require_text(consent_state, "consent_state", max_len=16).upper()
        if channel not in self._channels:
            raise UnknownChannel(
                "unknown channel", channel=channel, known=list(self._channels)
            )
        if consent_state not in ("UNKNOWN", "GRANTED", "WITHDRAWN"):
            raise UnknownConsentState("unknown consent state", consent_state=consent_state)

        case = self.conn.execute(
            "SELECT case_id, guest_ref FROM cases WHERE case_id = ? AND agency_id = ?",
            (case_id, self.agency_id),
        ).fetchone()
        if case is None:
            raise ConversationNotFound(
                "no such inquiry in this agency",
                case_id=case_id,
                agency_id=self.agency_id,
            )

        existing = self.conn.execute(
            "SELECT conversation_id FROM ai_conversations WHERE agency_id = ? AND case_id = ? AND channel = ?",
            (self.agency_id, case_id, channel),
        ).fetchone()
        if existing is not None:
            raise ConversationExists(
                "this inquiry already has an AI conversation on this channel",
                case_id=case_id,
                channel=channel,
                conversation_id=str(existing["conversation_id"]),
            )

        principal_id = self._principal()
        conversation_id = self.ids.new("conv")
        moment = self._now(at)
        with self._transaction():
            self.conn.execute(
                """
                INSERT INTO ai_conversations (
                    conversation_id, agency_id, principal_id, case_id, guest_ref, channel,
                    consent_state, quote_ref, hold_ref, reservation_ref, opened_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    conversation_id,
                    self.agency_id,
                    principal_id,
                    case_id,
                    guest_ref,
                    channel,
                    consent_state,
                    moment,
                    moment,
                ),
            )
            self._append_event(
                conversation_id=conversation_id,
                kind="OPENED",
                state="AI_RUNNING",
                actor=self.actor,
                actor_role=self.actor_role,
                reason=None,
                at=moment,
            )
            self._audit(
                action="reservation.conversation.opened",
                conversation_id=conversation_id,
                before={},
                after={
                    "state": "AI_RUNNING",
                    "case_id": case_id,
                    "channel": channel,
                    "principal_id": principal_id,
                },
                reason=None,
                actor=self.actor,
                actor_role=self.actor_role,
            )
        return self.get(conversation_id)

    def _principal(self) -> str:
        """The principal this service acts as. §56: a machine actor must name one."""

        if self.principal_id is None:
            raise ReservationServiceError(
                "this service has no AI principal: a machine actor must name the principal it acts as",
                code="RES_PRINCIPAL_REQUIRED",
            )
        return require_ref(self.principal_id, "principal_id")

    def principal(self) -> str:
        """The same name, for callers outside this class: a capability worker named it.

        The workers ask the gateway on behalf of the *service's* principal, never a per-worker
        identity of their own (§9 scales by concurrent workers, not by minting one identity per
        worker), so they need one public way to read the name the conversation already carries.
        """

        return self._principal()

    # -- consent ------------------------------------------------------------ #

    def set_consent(
        self,
        conversation_id: str,
        *,
        consent_state: str,
        reason: str,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> Conversation:
        """Record what the guest agreed to.

        A person writes it down, with a reason: a machine that could mark its own consent could
        talk a guest back into a thread they had withdrawn from. It stays writable while the AI
        is suspended, because consent is the guest's, not the model's, and a human must be able
        to honour a withdrawal at the moment it arrives.
        """

        conversation_id = require_ref(conversation_id, "conversation_id")
        consent_state = require_text(consent_state, "consent_state", max_len=16).upper()
        if consent_state not in ("UNKNOWN", "GRANTED", "WITHDRAWN"):
            raise UnknownConsentState("unknown consent state", consent_state=consent_state)
        reason = require_text(reason, "reason", max_len=200)
        who, role = self._who(actor, actor_role, human=True)
        if is_machine_actor(who, role):
            raise HumanRequired(
                "a machine principal cannot record consent: consent is the guest's, not the model's",
                conversation_id=conversation_id,
            )
        conversation = self.get(conversation_id)
        moment = self._now()
        with self._transaction():
            self.conn.execute(
                "UPDATE ai_conversations SET consent_state = ?, updated_at = ? WHERE conversation_id = ?",
                (consent_state, moment, conversation_id),
            )
            self._audit(
                action="reservation.conversation.consent",
                conversation_id=conversation_id,
                before={"consent_state": conversation.consent_state},
                after={"consent_state": consent_state},
                reason=reason,
                actor=who,
                actor_role=role,
            )
        return self.get(conversation_id)

    # -- references --------------------------------------------------------- #

    def attach_reference(
        self,
        conversation_id: str,
        *,
        kind: str,
        ref: str,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Conversation:
        """Point the conversation at the record that now governs it (quote, hold, booking).

        The reference is written *after* the owning module created the record, never before: a
        conversation that pointed at a quote that did not exist yet would be a link the rest of
        the platform has to treat as a maybe. This method stores the pointer; it never invents
        the record, and it does not re-decide whether the quote was acceptable.
        """

        column = {"quote": "quote_ref", "hold": "hold_ref", "reservation": "reservation_ref"}
        conversation_id = require_ref(conversation_id, "conversation_id")
        kind = require_text(kind, "kind", max_len=16).lower()
        if kind not in column:
            raise UnknownReference(
                "unknown reference kind", kind=kind, known=sorted(column)
            )
        ref = require_ref(ref, "ref", max_len=128)
        who, role = self._who(actor, actor_role)
        conversation = self.get(conversation_id)
        moment = self._now()
        with self._transaction():
            self.conn.execute(
                f"UPDATE ai_conversations SET {column[kind]} = ?, updated_at = ? WHERE conversation_id = ?",
                (ref, moment, conversation_id),
            )
            self._audit(
                action=f"reservation.conversation.{kind}_attached",
                conversation_id=conversation_id,
                before={column[kind]: getattr(conversation, column[kind])},
                after={column[kind]: ref},
                reason=reason,
                actor=who,
                actor_role=role,
            )
        return self.get(conversation_id)

    # -- the ladder --------------------------------------------------------- #

    def escalate(
        self,
        conversation_id: str,
        *,
        trigger: str,
        reason: str,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
        at: Any = None,
    ) -> Conversation:
        """Stop the AI and put the conversation on its way to a human (§9 Mandatory Escalation).

        The trigger must be a code from :data:`ESCALATION_TRIGGERS`. A trigger with an incident
        route opens an incident through :mod:`exception_engine` — after the rung is recorded, so
        a failing incident engine cannot leave a guest with nobody told.
        """

        conversation_id = require_ref(conversation_id, "conversation_id")
        trigger = self._trigger(trigger)
        reason = require_text(reason, "reason", max_len=300)
        who, role = self._who(actor, actor_role)
        conversation = self._move(
            conversation_id,
            "ESCALATED",
            actor=who,
            actor_role=role,
            reason=reason,
            trigger_code=trigger,
            at=at,
        )
        if self.outbox is not None:
            self.outbox.emit(
                event_type=ESCALATED_EVENT,
                aggregate_type="ai_conversation",
                aggregate_id=conversation.conversation_id,
                payload={
                    "conversation_id": conversation.conversation_id,
                    "case_id": conversation.case_id,
                    "trigger_code": trigger,
                    "handoff_blueprint": BLUEPRINT_OF_TRIGGER[trigger],
                    "state": conversation.state,
                },
                actor=who,
                idempotency_key=f"escalate:{conversation.conversation_id}:{trigger}",
            )
        self._capture_incident(conversation, trigger=trigger, reason=reason)
        return conversation

    def hand_off(
        self,
        conversation_id: str,
        *,
        reason: str,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
        at: Any = None,
    ) -> HandoffPackage:
        """Build the §13 package and post the conversation to the role that owns the trigger.

        Returns the package *as handed over*, digest included, because the human receives a
        brief and the audit keeps the digest of exactly what that brief said.
        """

        conversation_id = require_ref(conversation_id, "conversation_id")
        reason = require_text(reason, "reason", max_len=300)
        who, role = self._who(actor, actor_role)
        trigger = self._latest_trigger(conversation_id)
        blueprint = BLUEPRINT_OF_TRIGGER[trigger]
        # The brief describes the conversation as the human receives it, not as it was one
        # rung earlier: "what do I have to do next" is read off the rung they are handed.
        package = self._package(
            conversation_id, reason=reason, state=LADDER["HANDOFF_REQUESTED"][1]
        )
        conversation = self._move(
            conversation_id,
            "HANDOFF_REQUESTED",
            actor=who,
            actor_role=role,
            reason=reason,
            trigger_code=trigger,
            handoff_blueprint=blueprint,
            package_fingerprint=package.fingerprint,
            at=at,
        )
        if self.outbox is not None:
            self.outbox.emit(
                event_type=HANDOFF_REQUESTED_EVENT,
                aggregate_type="ai_conversation",
                aggregate_id=conversation.conversation_id,
                payload={
                    "conversation_id": conversation.conversation_id,
                    "case_id": conversation.case_id,
                    "trigger_code": trigger,
                    "handoff_blueprint": blueprint,
                    "package_fingerprint": package.fingerprint,
                },
                actor=who,
                idempotency_key=f"handoff:{conversation.conversation_id}",
            )
        return package

    def take_control(
        self,
        conversation_id: str,
        *,
        human_ref: str,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
        reason: Optional[str] = None,
        at: Any = None,
    ) -> Conversation:
        """A named human owns the conversation: three rungs, one gesture, one transaction.

        ``HUMAN_TAKEN`` → ``WRITES_SUSPENDED`` → ``HANDLING_STARTED``. They are recorded as three
        events because they are three facts (somebody owns it; the AI has stopped; the person is
        working it), and they are *performed* together because a person clicking "take over"
        performs exactly those three at once. Each rung still refuses out of order: calling
        :meth:`begin_handling` first is a :class:`RungRefused`.

        The target role must be **staffed** in this tenant. Handing a guest to a role nobody
        holds is the failure the D3 decision (roles start ``UNASSIGNED``) exists to prevent, so
        it is refused here rather than discovered by a guest who gets no answer.
        """

        conversation_id = require_ref(conversation_id, "conversation_id")
        human_ref = require_text(human_ref, "human_ref", max_len=128)
        who, role = self._who(actor, actor_role, human=True)
        if is_machine_actor(who, role):
            raise HumanRequired(
                "a machine principal cannot take control of a conversation",
                conversation_id=conversation_id,
            )
        blueprint = self._latest_blueprint(conversation_id)
        if blueprint not in self.staffed_blueprints():
            raise HandoffRoleUnstaffed(
                "nobody holds the role this conversation was handed to",
                conversation_id=conversation_id,
                handoff_blueprint=blueprint,
            )
        moment = self._now(at)
        with self._transaction():
            conversation = self._append_rung(
                conversation_id,
                "HUMAN_TAKEN",
                actor=who,
                actor_role=role,
                reason=reason,
                human_ref=human_ref,
                moment=moment,
            )
            conversation = self._append_rung(
                conversation_id,
                "WRITES_SUSPENDED",
                actor=who,
                actor_role=role,
                reason=reason,
                moment=moment,
            )
            conversation = self._append_rung(
                conversation_id,
                "HANDLING_STARTED",
                actor=who,
                actor_role=role,
                reason=reason,
                moment=moment,
            )
        return conversation

    def suspend_writes(
        self,
        conversation_id: str,
        *,
        reason: str,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
        at: Any = None,
    ) -> Conversation:
        """The explicit rung: the AI stops touching this conversation entirely."""

        who, role = self._who(actor, actor_role)
        return self._move(
            conversation_id,
            "WRITES_SUSPENDED",
            actor=who,
            actor_role=role,
            reason=require_text(reason, "reason", max_len=300),
            at=at,
        )

    def begin_handling(
        self,
        conversation_id: str,
        *,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
        at: Any = None,
    ) -> Conversation:
        """The human has started work: the rung that follows the AI going quiet."""

        who, role = self._who(actor, actor_role)
        return self._move(
            conversation_id,
            "HANDLING_STARTED",
            actor=who,
            actor_role=role,
            reason=reason,
            at=at,
        )

    def resolve(
        self,
        conversation_id: str,
        *,
        reason: str,
        actor: Optional[str] = None,
        actor_role: Optional[str] = None,
        at: Any = None,
    ) -> Conversation:
        """The human is done with it. Nothing the AI may do on its own: this rung is theirs."""

        who, role = self._who(actor, actor_role, human=True)
        if is_machine_actor(who, role):
            raise HumanRequired(
                "a machine principal cannot resolve a handoff",
                conversation_id=conversation_id,
            )
        return self._move(
            conversation_id,
            "RESOLVED",
            actor=who,
            actor_role=role,
            reason=require_text(reason, "reason", max_len=300),
            at=at,
        )

    def allow_resume(
        self,
        conversation_id: str,
        *,
        reason: str,
        actor: str,
        actor_role: str,
        at: Any = None,
    ) -> Conversation:
        """§13's "only if explicitly safe": a human decides the AI may take it back.

        The actor is mandatory and must be a person: this is the rung that widens authority
        again, and ADR-0038's rule ("the switch is a human act") applies to widening here
        exactly as it does to graduating an agent out of shadow mode.
        """

        conversation_id = require_ref(conversation_id, "conversation_id")
        who, role = self._who(actor, actor_role)
        if is_machine_actor(who, role):
            raise HumanRequired(
                "a machine principal cannot allow a resume",
                conversation_id=conversation_id,
            )
        return self._move(
            conversation_id,
            "RESUME_ALLOWED",
            actor=who,
            actor_role=role,
            reason=require_text(reason, "reason", max_len=300),
            human_ref=who,
            at=at,
        )

    def resume(self, conversation_id: str, *, reason: Optional[str] = None, at: Any = None) -> Conversation:
        """The AI picks the conversation up again — as the principal that owns it.

        A different principal may not resume somebody else's thread: the conversation names the
        principal it belongs to, and taking it over would put two identities' words in one
        guest's thread.
        """

        conversation_id = require_ref(conversation_id, "conversation_id")
        conversation = self.get(conversation_id)
        if self.principal_id is not None and conversation.principal_id != self.principal_id:
            raise ReservationServiceError(
                "this conversation belongs to a different AI principal",
                code="RES_WRONG_PRINCIPAL",
                conversation_id=conversation_id,
                principal_id=conversation.principal_id,
            )
        return self._move(
            conversation_id,
            "RESUMED",
            actor=self.actor,
            actor_role=self.actor_role,
            reason=reason,
            at=at,
        )

    # -- §9 uncertainty ----------------------------------------------------- #

    def check_confidence(
        self,
        conversation_id: str,
        *,
        confidence_pct: Any,
        what: str,
        reason: Optional[str] = None,
        at: Any = None,
    ) -> None:
        """May the AI act on a judgement that came from language? §9's escalation rule.

        Three outcomes, and two of them are escalations:

        * the threshold is **unpublished** → escalate. Nobody has decided how sure is sure
          enough, and inventing the number here would be the quietest way to widen authority;
        * ``confidence_pct`` is **below** the published threshold → escalate;
        * otherwise return, so the caller may act.

        Both escalations raise :class:`EscalationRaised` *after* recording the rung, so no
        caller can record the escalation and then carry on as if it had not happened.
        """

        conversation_id = require_ref(conversation_id, "conversation_id")
        confidence = _percentage(confidence_pct)
        conversation = self.get(conversation_id)
        if not conversation.writes_allowed:
            raise WritesSuspended(
                f"{what} refused: the conversation is already with a human",
                conversation_id=conversation_id,
                state=conversation.state,
            )
        try:
            threshold = _percentage(
                Decimal(self.policy.resolve(MIN_CONFIDENCE_KEY, at=at).as_decimal())
            )
        except PolicyValueNotPublished as exc:
            self.escalate(
                conversation_id,
                trigger="AI_UNCERTAINTY",
                reason=(
                    f"{MIN_CONFIDENCE_KEY} has no published value, so {what} may not act on a "
                    "judgement that came from language"
                )[:300],
                at=at,
            )
            raise EscalationRaised(
                "the confidence threshold is unpublished: escalated instead of guessing",
                conversation_id=conversation_id,
                policy_key=MIN_CONFIDENCE_KEY,
            ) from exc
        if confidence < threshold:
            self.escalate(
                conversation_id,
                trigger="AI_UNCERTAINTY",
                reason=(
                    f"reported confidence {confidence}% is below the published threshold "
                    f"{threshold}%: {reason or what}"
                )[:300],
                at=at,
            )
            raise EscalationRaised(
                "reported confidence is below the published threshold: escalated to a human",
                conversation_id=conversation_id,
                confidence_pct=str(confidence),
                threshold_pct=str(threshold),
            )

    # -- the handoff package ------------------------------------------------ #

    def handoff_package(self, conversation_id: str, *, reason: str = "") -> HandoffPackage:
        """§13's brief for the conversation's **current** rung.

        Read-only, and allowed on *any* rung — a suspended conversation is exactly when somebody
        needs to see what the AI knew. Every value is fetched by reference, so a package can
        never disagree with the record it came from. A rebuild with the same reason and the same
        rung returns the same digest, which is what makes the digest on the rung evidence rather
        than a curiosity.
        """

        state = self.get(conversation_id).state
        return self._package(conversation_id, reason=reason, state=state)

    def _package(self, conversation_id: str, *, reason: str, state: str) -> HandoffPackage:
        """The brief, written for ``state``. :meth:`hand_off` passes the rung it is posting to."""

        conversation = self.get(conversation_id)
        case = self.conn.execute(
            """
            SELECT case_id, reference, status, guest_ref, channel, sla_policy,
                   first_response_due_at, quote_due_at, first_responded_at, quoted_at, booking_id
            FROM cases WHERE case_id = ? AND agency_id = ?
            """,
            (conversation.case_id, self.agency_id),
        ).fetchone()
        if case is None:  # pragma: no cover - the FK guarantees the case exists in the tenant
            raise ConversationNotFound(
                "the inquiry this conversation belongs to is gone",
                conversation_id=conversation.conversation_id,
                case_id=conversation.case_id,
            )

        trigger = self._latest_trigger(conversation.conversation_id)
        requirements, unknown = self._requirements(conversation.case_id)
        candidates, run_id = self._candidates(conversation.case_id)
        payment_state = self._payment_state(conversation.reservation_ref)
        payload = {
            "conversation_id": conversation.conversation_id,
            "case_id": conversation.case_id,
            "state": state,
            "trigger_code": trigger,
            "reason": reason,
            "handoff_blueprint": BLUEPRINT_OF_TRIGGER[trigger],
            "guest_ref": conversation.guest_ref,
            "channel": conversation.channel,
            "consent_state": conversation.consent_state,
            "requirements": requirements,
            "unknown_required": list(unknown),
            "candidates": [dict(row) for row in candidates],
            "match_run_id": run_id,
            "quote_ref": conversation.quote_ref,
            "hold_ref": conversation.hold_ref,
            "reservation_ref": conversation.reservation_ref,
            "payment_state": payment_state,
            "sla": {
                "policy": str(case["sla_policy"]),
                "first_response_due_at": str(case["first_response_due_at"]),
                "quote_due_at": str(case["quote_due_at"]),
                "first_responded_at": _optional(case["first_responded_at"]),
                "quoted_at": _optional(case["quoted_at"]),
            },
            "pending_actions": list(
                _pending_actions(
                    state=state,
                    trigger=trigger,
                    unknown=unknown,
                    first_responded_at=case["first_responded_at"],
                    reservation_ref=conversation.reservation_ref,
                )
            ),
        }
        return HandoffPackage(
            conversation_id=conversation.conversation_id,
            case_id=conversation.case_id,
            case_reference=_optional(case["reference"]),
            state=state,
            trigger_code=trigger,
            reason=reason,
            handoff_blueprint=BLUEPRINT_OF_TRIGGER[trigger],
            guest_ref=conversation.guest_ref,
            channel=conversation.channel,
            consent_state=conversation.consent_state,
            requirements=requirements,
            unknown_required=unknown,
            candidates=candidates,
            match_run_id=run_id,
            quote_ref=conversation.quote_ref,
            hold_ref=conversation.hold_ref,
            reservation_ref=conversation.reservation_ref,
            payment_state=payment_state,
            sla=payload["sla"],
            pending_actions=tuple(payload["pending_actions"]),
            fingerprint=fingerprint(payload, domain=PACKAGE_DOMAIN),
        )

    def _requirements(self, case_id: str) -> tuple[Mapping[str, Any], tuple[str, ...]]:
        """The facts intake marked ``ACCEPTED``, and the required ones still unknown.

        Read through :class:`intake.IntakeService` rather than a query of my own: what counts
        as a fact (an ``ACCEPTED`` review) and what counts as still missing are intake's rules,
        and a second copy of them here would be exactly the `missing == empty` bug that module
        already had to fix once.
        """

        intake = self._intake()
        return dict(intake.accepted_facts(case_id)), tuple(intake.unknown_required(case_id))

    def _intake(self) -> Any:
        """A read handle on intake's published views. Constructed, not injected: this is a read."""

        from intake import IntakeService

        return IntakeService(self.conn, clock=self.clock, ids=self.ids)

    def _candidates(self, case_id: str) -> tuple[tuple[Mapping[str, Any], ...], Optional[str]]:
        """The newest matching run for this inquiry: ranked rows with their own reasons."""

        run = self.conn.execute(
            "SELECT match_run_id, created_at FROM match_runs WHERE case_id = ? ORDER BY created_at DESC, match_run_id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if run is None:
            return (), None
        rows = self.conn.execute(
            """
            SELECT rank, unit_id, eligible, score_centi, reasons, deny_reasons
            FROM match_candidates WHERE match_run_id = ? ORDER BY rank LIMIT ?
            """,
            (str(run["match_run_id"]), PACKAGE_CANDIDATE_LIMIT),
        ).fetchall()
        return tuple(dict(row) for row in rows), str(run["match_run_id"])

    def _payment_state(self, reservation_ref: Optional[str]) -> Optional[str]:
        """The booking's own payment status — never what a guest said they paid."""

        if not reservation_ref:
            return None
        row = self.conn.execute(
            "SELECT payment_status FROM bookings WHERE booking_id = ? AND agency_id = ?",
            (reservation_ref, self.agency_id),
        ).fetchone()
        return None if row is None else str(row["payment_status"])

    # -- writing the ladder -------------------------------------------------- #

    def _move(
        self,
        conversation_id: str,
        kind: str,
        *,
        actor: str,
        actor_role: str,
        reason: Optional[str] = None,
        trigger_code: Optional[str] = None,
        handoff_blueprint: Optional[str] = None,
        human_ref: Optional[str] = None,
        package_fingerprint: Optional[str] = None,
        at: Any = None,
    ) -> Conversation:
        """One rung, one transaction, one audit row. Every other write path goes through here."""

        conversation_id = require_ref(conversation_id, "conversation_id")
        moment = self._now(at)
        with self._transaction():
            conversation = self._append_rung(
                conversation_id,
                kind,
                actor=actor,
                actor_role=actor_role,
                reason=reason,
                trigger_code=trigger_code,
                handoff_blueprint=handoff_blueprint,
                human_ref=human_ref,
                package_fingerprint=package_fingerprint,
                moment=moment,
            )
        return conversation

    def _append_rung(
        self,
        conversation_id: str,
        kind: str,
        *,
        actor: str,
        actor_role: str,
        reason: Optional[str] = None,
        trigger_code: Optional[str] = None,
        handoff_blueprint: Optional[str] = None,
        human_ref: Optional[str] = None,
        package_fingerprint: Optional[str] = None,
        moment: Optional[str] = None,
    ) -> Conversation:
        """Append one event after checking the rung against :data:`LADDER`. Caller owns the tx.

        ``moment`` arrives already resolved: a rung and the audit row that explains it carry the
        same instant, and the caller that opened the transaction owns that instant.
        """

        if kind not in LADDER:
            raise RungRefused("not a rung on the §13 ladder", kind=kind)
        expected_from, to_state = LADDER[kind]
        current = self.get(conversation_id).state
        if current != expected_from:
            raise RungRefused(
                "this rung does not follow the conversation's current state",
                conversation_id=conversation_id,
                kind=kind,
                state=current,
                expected_state=expected_from,
            )
        self._append_event(
            conversation_id=conversation_id,
            kind=kind,
            state=to_state,
            actor=actor,
            actor_role=actor_role,
            reason=reason,
            trigger_code=trigger_code,
            handoff_blueprint=handoff_blueprint,
            human_ref=human_ref,
            package_fingerprint=package_fingerprint,
            at=moment,
        )
        self.conn.execute(
            "UPDATE ai_conversations SET updated_at = ? WHERE conversation_id = ?",
            (moment, conversation_id),
        )
        self._audit(
            action=f"reservation.conversation.{kind.lower()}",
            conversation_id=conversation_id,
            before={"state": current},
            after={"state": to_state, "kind": kind},
            reason=reason,
            actor=actor,
            actor_role=actor_role,
        )
        return self.get(conversation_id)

    def _append_event(
        self,
        *,
        conversation_id: str,
        kind: str,
        state: str,
        actor: str,
        actor_role: str,
        reason: Optional[str],
        trigger_code: Optional[str] = None,
        handoff_blueprint: Optional[str] = None,
        human_ref: Optional[str] = None,
        package_fingerprint: Optional[str] = None,
        at: str,
    ) -> None:
        seq_row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM ai_conversation_events WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        self.conn.execute(
            """
            INSERT INTO ai_conversation_events (
                ai_conversation_event_id, conversation_id, seq, kind, state, trigger_code,
                handoff_blueprint, human_ref, package_fingerprint, actor, actor_role, reason,
                occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.ids.new("convev"),
                conversation_id,
                int(seq_row["next"]),
                kind,
                state,
                trigger_code,
                handoff_blueprint,
                human_ref,
                package_fingerprint,
                actor,
                actor_role,
                reason,
                at,
            ),
        )

    # -- helpers ------------------------------------------------------------ #

    def _who(self, actor: Optional[str], actor_role: Optional[str], *, human: bool = False) -> tuple[str, str]:
        """The identity for this act: the caller's, or this service's own.

        ``human=True`` requires the caller to name itself: an act that only a person may perform
        must not silently default to the AI's identity.
        """

        if actor is None or actor_role is None:
            if human:
                raise HumanRequired(
                    "this rung must name the person performing it",
                    actor=actor,
                    actor_role=actor_role,
                )
            return self.actor, self.actor_role
        return require_ref(actor, "actor"), require_text(actor_role, "actor_role", max_len=64)

    def _trigger(self, trigger: Any) -> str:
        code = require_text(trigger, "trigger", max_len=64).upper()
        if code not in ESCALATION_TRIGGERS:
            raise UnknownTrigger(
                "this trigger is not in the §9 escalation vocabulary",
                trigger=code,
                known=list(ESCALATION_TRIGGERS),
            )
        return code

    def _latest_trigger(self, conversation_id: str) -> str:
        row = self.conn.execute(
            """
            SELECT trigger_code FROM ai_conversation_events
            WHERE conversation_id = ? AND kind = 'ESCALATED'
            ORDER BY seq DESC LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if row is None or row["trigger_code"] is None:
            raise RungRefused(
                "this conversation was never escalated, so there is nothing to hand over",
                conversation_id=conversation_id,
            )
        return str(row["trigger_code"])

    def _latest_blueprint(self, conversation_id: str) -> str:
        row = self.conn.execute(
            """
            SELECT handoff_blueprint FROM ai_conversation_events
            WHERE conversation_id = ? AND kind = 'HANDOFF_REQUESTED'
            ORDER BY seq DESC LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if row is None or row["handoff_blueprint"] is None:
            raise RungRefused(
                "this conversation was never handed to a role, so there is nobody to take it",
                conversation_id=conversation_id,
            )
        return str(row["handoff_blueprint"])

    def staffed_blueprints(self) -> tuple[str, ...]:
        """Which tenant roles actually have an active holder right now (ADR-0035/D3, steps 1–2).

        Read from :mod:`agency_roles` rather than from a setting, because "who is on duty" is the
        tenant's own record and it changes without a deploy.
        """

        rows = self.conn.execute(
            """
            SELECT DISTINCT r.based_on AS based_on
            FROM agency_roles r
            JOIN agency_role_holders h
              ON h.agency_id = r.agency_id AND h.role_id = r.role_id
            WHERE r.agency_id = ? AND r.status = 'ACTIVE' AND h.status = 'ACTIVE'
            ORDER BY r.based_on
            """,
            (self.agency_id,),
        ).fetchall()
        return tuple(str(row["based_on"]) for row in rows)

    def _capture_incident(self, conversation: Conversation, *, trigger: str, reason: str) -> None:
        """A trigger that is also a failure opens an incident, through the one engine for failures."""

        category = INCIDENT_OF_TRIGGER.get(trigger)
        if self.exceptions is None or category is None:
            return
        self.exceptions.capture(
            category=category,
            code=f"RES_{trigger}",
            message=f"AI reservation escalation: {reason}"[:500],
            source="reservation_service",
            aggregate_id=conversation.case_id,
            details={"conversation_id": conversation.conversation_id, "trigger_code": trigger},
        )


def _conversation(row: Mapping[str, Any]) -> Conversation:
    return Conversation(
        conversation_id=str(row["conversation_id"]),
        agency_id=str(row["agency_id"]),
        principal_id=str(row["principal_id"]),
        case_id=str(row["case_id"]),
        guest_ref=str(row["guest_ref"]),
        channel=str(row["channel"]),
        consent_state=str(row["consent_state"]),
        quote_ref=_optional(row["quote_ref"]),
        hold_ref=_optional(row["hold_ref"]),
        reservation_ref=_optional(row["reservation_ref"]),
        opened_at=str(row["opened_at"]),
        updated_at=str(row["updated_at"]),
        state=str(row["state"]),
    )


def _event(row: Mapping[str, Any]) -> ConversationEvent:
    return ConversationEvent(
        event_id=str(row["ai_conversation_event_id"]),
        conversation_id=str(row["conversation_id"]),
        seq=int(row["seq"]),
        kind=str(row["kind"]),
        state=str(row["state"]),
        trigger_code=_optional(row["trigger_code"]),
        handoff_blueprint=_optional(row["handoff_blueprint"]),
        human_ref=_optional(row["human_ref"]),
        package_fingerprint=_optional(row["package_fingerprint"]),
        actor=str(row["actor"]),
        actor_role=str(row["actor_role"]),
        reason=_optional(row["reason"]),
        occurred_at=str(row["occurred_at"]),
    )


def _optional(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _percentage(value: Any) -> Decimal:
    """A 0–100 percentage, as a ``Decimal``. Refuses a 0–1 fraction instead of guessing.

    The ambiguity is the point: a caller passing ``0.9`` meaning "90%" would clear a threshold
    of 85 and look correct, and the same caller passing ``0.9`` meaning "0.9%" would be a
    catastrophic under-confidence. One spelling only, and it is the one the catalogue's ``PCT``
    type already uses.
    """

    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - the message matters more than the class
        raise DomainError(
            "a percentage must be a number", code="RES_INVALID_PERCENTAGE", value=repr(value)
        ) from exc
    if number < 0 or number > 100:
        raise DomainError(
            "a percentage must be between 0 and 100",
            code="RES_INVALID_PERCENTAGE",
            value=str(number),
        )
    if Decimal(0) < number < Decimal(1):
        raise DomainError(
            "a value between 0 and 1 is ambiguous between a fraction and a percentage: "
            "write the percentage (90, not 0.9)",
            code="RES_INVALID_PERCENTAGE",
            value=str(number),
        )
    return number


def _pending_actions(
    *,
    state: str,
    trigger: str,
    unknown: Sequence[str],
    first_responded_at: Any,
    reservation_ref: Optional[str],
) -> tuple[str, ...]:
    """What a human still has to do, derived from the rung and the case — not from a prompt."""

    actions: list[str] = []
    if state == "HANDOFF_PENDING":
        actions.append("take this conversation: it is waiting for a named human")
    if state == "HUMAN_ASSIGNED":
        actions.append("acknowledge: the AI is still holding writes on this thread")
    if state == "AI_WRITE_SUSPENDED":
        actions.append("start handling: the AI has stopped, the guest is waiting")
    if state == "RESOLUTION":
        actions.append("allow or refuse an AI resume — deciding nothing keeps the AI silent")
    if first_responded_at is None:
        actions.append("answer the guest: the first-response clock has not been marked")
    for field_name in unknown:
        actions.append(f"clarify a missing requirement: {field_name}")
    if trigger == "FUNDS_AFTER_HOLD_EXPIRY" and reservation_ref is None:
        actions.append("decide what happens to money that arrived with no live hold")
    return tuple(actions)


def _transaction(conn: sqlite3.Connection) -> Any:
    """A savepoint when the caller already opened a transaction, a transaction otherwise.

    Matching, the kernel and the case service all nest transactions this way, so a step that
    runs inside a host transaction (the scheduler, a test) still commits exactly once, at the
    outer boundary, and a step that runs alone commits its own rung atomically.
    """

    class _Txn:
        def __enter__(self) -> sqlite3.Connection:
            self._outer = not conn.in_transaction
            if self._outer:
                conn.execute("BEGIN")
            else:
                conn.execute("SAVEPOINT reservation_service")
            return conn

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            if self._outer:
                conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
            else:
                conn.execute("RELEASE reservation_service" if exc_type is None else "ROLLBACK TO reservation_service")
            return False

    return _Txn()
