"""AI service identity and its tool envelope (§18, §56, ADR-0009).

§18 gives the *AI Service* role "narrow safe tools" with an explicit deny on financial
authority, secrets, raw passport and privileged security. §56 fixes three tiers —
low-risk autonomous, medium-risk through explicit policy-controlled tools, high-risk as
human-authority-only — and closes with "Conversation instructions never increase AI
privileges". ADR-0009 records the deny registry.

Two rules shape this module:

* **The deny registry is policy**, resolved from the §16 store
  (``security.ai.denied_actions``), so the owner can extend it without a code change. It
  is checked *before* any allowlist, because a denial that a grant can outvote is not a
  denial.
* **Authority comes from the principal and the registered tool, never from the request.**
  ``intent`` is free text out of a conversation: it is recorded and never consulted.

Registration is a description of the domain, not a grant. Every §56 capability is
registered, including the ones the AI may never use; what stops them is the deny registry
and the tier, not the absence of a row.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, NoReturn, Optional

from audit import AuditLog
from policy_engine import PolicyResolver, PolicyValueNotPublished
from primitives import (
    DEFAULT_AGENCY_ID,
    DomainError,
    IdFactory,
    SystemClock,
    parse_iso,
    require_ref,
    require_text,
    to_iso,
)
from supplier_route import is_machine_actor

#: The role the AI acts as in the audit chain (§18). Never a human role.
AI_ACTOR_ROLE = "ai_service"

#: §56 risk tiers.
AI_TIERS: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")

#: The lifecycle events a principal can have. Status is derived from the newest one.
#: §17: the event vocabulary of the kill-switch ladder. Growing this list is a schema change
#: (the DDL CHECK mirrors it), which is why the two live together in one migration.
AI_PRINCIPAL_EVENT_KINDS: tuple[str, ...] = (
    "REGISTERED",
    "SUSPENDED",
    "REINSTATED",
    "ACTIVATED",
    "DEGRADED",
    "READ_ONLY",
    "RETIRED",
)

#: Policy keys this module reads. Both are §16 policy, not constants.
DENIED_ACTIONS_KEY = "security.ai.denied_actions"
MEDIUM_ALLOWANCE_KEY = "security.ai.medium_tool_allowance"

#: Status values derived from the event log.
AI_STATUS_ACTIVE = "ACTIVE"
AI_STATUS_DEGRADED = "DEGRADED"
AI_STATUS_READ_ONLY = "READ_ONLY"
AI_STATUS_SUSPENDED = "SUSPENDED"
AI_STATUS_RETIRED = "RETIRED"

#: §17's five statuses, from most capable to terminal. The ladder is a *narrowing*: each step down
#: removes capability and never adds any, and RETIRED is the only step that does not come back.
AI_STATUS_LADDER: tuple[str, ...] = (
    AI_STATUS_ACTIVE,
    AI_STATUS_DEGRADED,
    AI_STATUS_READ_ONLY,
    AI_STATUS_SUSPENDED,
    AI_STATUS_RETIRED,
)

_ACTIVE_KINDS = ("REGISTERED", "REINSTATED", "ACTIVATED")


class AiServiceError(DomainError):
    """Base class for AI service refusals."""

    code = "AI_SERVICE_ERROR"


class AiLadderRefused(AiServiceError):
    """A §17 transition that would *widen* capability through the wrong door.

    Narrowing is free; widening has exactly one door per state. Without this rule, ``degrade``
    called on a suspended principal would quietly hand capability back.
    """

    code = "AI_LADDER_ORDER"


class AiPrincipalRetired(AiServiceError):
    """Retirement is terminal: the log is the proof of why it stays that way."""

    code = "AI_PRINCIPAL_RETIRED"


#: The §17 state a lifecycle event arrives at. Two kinds reach ACTIVE because two verbs do:
#: ``REINSTATED`` is the way up from SUSPENDED, ``ACTIVATED`` the way up from a narrowed rung.
_KIND_MODES: Mapping[str, str] = {
    "REINSTATED": "ACTIVE",
    "ACTIVATED": "ACTIVE",
}

#: Which current statuses each lifecycle event may be applied to. Repeats are allowed (an operator
#: pressing the same button twice is not an incident); anything that adds capability must use the
#: one verb that adds it.
_LADDER_ALLOWED: dict[str, tuple[str, ...]] = {
    "SUSPENDED": (AI_STATUS_ACTIVE, AI_STATUS_DEGRADED, AI_STATUS_READ_ONLY, AI_STATUS_SUSPENDED),
    AI_STATUS_DEGRADED: (AI_STATUS_ACTIVE, AI_STATUS_DEGRADED),
    AI_STATUS_READ_ONLY: (AI_STATUS_ACTIVE, AI_STATUS_DEGRADED, AI_STATUS_READ_ONLY),
    "ACTIVATED": (AI_STATUS_ACTIVE, AI_STATUS_DEGRADED, AI_STATUS_READ_ONLY),
    "REINSTATED": (AI_STATUS_SUSPENDED, AI_STATUS_ACTIVE),
    "RETIRED": (AI_STATUS_ACTIVE, AI_STATUS_DEGRADED, AI_STATUS_READ_ONLY, AI_STATUS_SUSPENDED),
}


class UnknownAiPrincipal(AiServiceError):
    """No such principal in this agency."""

    code = "AI_UNKNOWN_PRINCIPAL"


class AiPrincipalNotActive(AiServiceError):
    """The principal is suspended or retired: §40's kill switch."""

    code = "AI_PRINCIPAL_NOT_ACTIVE"


class UnknownAiTool(AiServiceError):
    """The tool is not registered. An allowlist, not a blocklist."""

    code = "AI_UNKNOWN_TOOL"


class AiDeniedAction(AiServiceError):
    """The action is in the deny registry (§18, ADR-0009)."""

    code = "AI_DENIED_ACTION"


class AiMediumToolNotAllowed(AiServiceError):
    """A §56 medium-risk tool without a published controlling policy."""

    code = "AI_MEDIUM_TOOL_NOT_ALLOWED"


class AiHumanAuthorityRequired(AiServiceError):
    """A §56 high-risk action: human authority only, whatever the tool is called."""

    code = "AI_HUMAN_AUTHORITY_ONLY"


@dataclass(frozen=True)
class AiPrincipal:
    """A machine identity. ``secret_ref`` names a secret; it is never the secret."""

    principal_id: str
    agency_id: str
    name: str
    secret_ref: str
    status: str
    created_at: str

    @property
    def is_active(self) -> bool:
        return self.status == AI_STATUS_ACTIVE

    @property
    def may_act(self) -> bool:
        """§17: the *acting band* is ACTIVE, DEGRADED and READ_ONLY.

        Two layers, two questions. This one is "may this identity act at all"; how far it may go
        is the mode ceiling, which the gateway enforces. Collapsing them would make DEGRADED and
        READ_ONLY indistinguishable from SUSPENDED.
        """
        return self.status in (AI_STATUS_ACTIVE, AI_STATUS_DEGRADED, AI_STATUS_READ_ONLY)


@dataclass(frozen=True)
class AiTool:
    """One §56 capability: what tier it is, and which policy controls it."""

    tool: str
    tier: str
    policy_key: Optional[str]
    spec_ref: str
    description: str


@dataclass(frozen=True)
class AiAuthorisation:
    """The record of a granted tool call, citing the policy revision that allowed it."""

    principal_id: str
    tool: str
    tier: str
    allowed: bool
    policy_revision_id: Optional[str]
    intent: Optional[str]
    decided_at: str


class AiService:
    """Registers AI principals, holds the kill switch, and authorises tool calls."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        policy: PolicyResolver,
        clock: Optional[Any] = None,
        ids: Optional[IdFactory] = None,
        audit: Optional[AuditLog] = None,
        agency_id: str = DEFAULT_AGENCY_ID,
    ) -> None:
        # ``policy`` has no default on purpose: without the §16 store there is no deny
        # registry, and an AI with no deny registry must not start at all.
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.policy = policy
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id")
        self.audit = audit or AuditLog(conn, clock=self.clock, ids=self.ids)

    # -- time --------------------------------------------------------------- #

    def _moment(self, at: Optional[Any] = None) -> datetime:
        if at is None:
            return self.clock.now()
        if isinstance(at, datetime):
            if at.tzinfo is None:
                raise DomainError("at must be timezone-aware", code="INVALID_FIELD", field="at")
            return at.astimezone(UTC)
        return parse_iso(at)

    def _stamp(self, at: Optional[Any] = None) -> str:
        return to_iso(self._moment(at))

    # -- the deny registry and the medium-risk allowance (both §16 policy) --- #

    def _deny_policy(self, at: Optional[Any] = None) -> Any:
        return self.policy.resolve(DENIED_ACTIONS_KEY, at=at)

    def denied_actions(self) -> tuple[str, ...]:
        """The capabilities the AI may never hold, as published by the owner."""
        return tuple(self._deny_policy().as_json())

    def _allowance(self, at: Optional[Any] = None) -> Optional[Any]:
        try:
            return self.policy.resolve(MEDIUM_ALLOWANCE_KEY, at=at)
        except PolicyValueNotPublished:
            # Not published is not "allow everything": it is "nothing is allowed".
            return None

    def allowed_medium_tools(self, *, at: Optional[Any] = None) -> tuple[str, ...]:
        value = self._allowance(at=at)
        return () if value is None else tuple(value.as_json())

    # -- principals --------------------------------------------------------- #

    def register_principal(
        self,
        name: str,
        *,
        secret_ref: str,
        actor: str,
        agency_id: Optional[str] = None,
        at: Optional[Any] = None,
    ) -> AiPrincipal:
        """Register a machine identity. ``actor`` is the human answerable for it."""
        clean_name = require_text(name, "name", max_len=80)
        clean_ref = require_ref(secret_ref, "secret_ref")
        clean_actor = require_ref(actor, "actor")
        scope = self.agency_id if agency_id is None else require_ref(agency_id, "agency_id")
        principal_id = self.ids.new("aip")
        self.conn.execute(
            "INSERT INTO ai_principals (principal_id, agency_id, name, secret_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (principal_id, scope, clean_name, clean_ref, self._stamp(at)),
        )
        self._record_event(principal_id, "REGISTERED", actor=clean_actor, reason=None, at=at)
        return self.principal(principal_id, agency_id=scope)

    def principal(self, principal_id: str, *, agency_id: Optional[str] = None) -> AiPrincipal:
        pid = require_ref(principal_id, "principal_id")
        scope = self.agency_id if agency_id is None else require_ref(agency_id, "agency_id")
        row = self.conn.execute(
            "SELECT * FROM ai_principals WHERE principal_id = ? AND agency_id = ?",
            (pid, scope),
        ).fetchone()
        if row is None:
            raise UnknownAiPrincipal(
                "no such AI principal in this agency",
                code="AI_UNKNOWN_PRINCIPAL",
                principal_id=pid,
                agency_id=scope,
            )
        return AiPrincipal(
            principal_id=row["principal_id"],
            agency_id=row["agency_id"],
            name=row["name"],
            secret_ref=row["secret_ref"],
            status=self._status(pid),
            created_at=row["created_at"],
        )

    def reachable_modes(self, principal_id: str) -> tuple[str, ...]:
        """The §17 rungs this principal may move to right now — what the operator may press next.

        Derived from the same table the guard uses, so the hint an operator reads can never
        disagree with the refusal they get. A retired principal reaches nothing: the ladder ends.
        """
        status = self.principal(principal_id).status
        if status == AI_STATUS_RETIRED:
            return ()
        reachable: list[str] = []
        for kind, allowed in _LADDER_ALLOWED.items():
            if status not in allowed:
                continue
            mode = _KIND_MODES.get(kind, kind)
            if mode not in reachable:
                reachable.append(mode)
        return tuple(reachable)

    @contextmanager
    def _one_write(self) -> Iterator[sqlite3.Connection]:
        """The event row and its audit row are one write, or neither happened.

        ``connect_sqlite`` opens SQLite in autocommit mode on purpose, so without this a failure
        between the two statements leaves a mode change with no audit row — a pair an operator must
        never have to guess about (found by driving the kill-switch CLI twice, 2026-10-02). Nested
        use joins the outer write rather than opening a second ``BEGIN``, which SQLite refuses
        mid-transaction.
        """
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def principals(self) -> tuple[AiPrincipal, ...]:
        """Every principal of this agency, by name — what an operator reads before pressing anything.

        The status is derived per row (the newest event wins), so this can never disagree with
        ``principal()`` about a single principal.
        """
        rows = self.conn.execute(
            "SELECT principal_id FROM ai_principals WHERE agency_id = ? ORDER BY name",
            (self.agency_id,),
        ).fetchall()
        return tuple(self.principal(row["principal_id"]) for row in rows)

    def events(self, principal_id: str) -> tuple[sqlite3.Row, ...]:
        """One principal's lifecycle log, oldest first — the §40 ``when`` and ``who``.

        Read-only on purpose: the log is append-only, and history is never the thing an operator
        edits. ``principal_id`` is validated by the same call the writer uses, so a typo is a
        refusal rather than an empty list that looks like "nothing happened".
        """
        principal = self.principal(principal_id)
        return tuple(
            self.conn.execute(
                "SELECT seq, kind, actor, reason, occurred_at FROM ai_principal_events"
                " WHERE principal_id = ? ORDER BY seq",
                (principal.principal_id,),
            ).fetchall()
        )

    def suspend(
        self, principal_id: str, *, actor: str, actor_role: Optional[str] = None,
        reason: Optional[str] = None, at: Optional[Any] = None
    ) -> AiPrincipal:
        """§40 kill switch: stop this principal acting, and say who stopped it."""
        return self._lifecycle(
            principal_id, "SUSPENDED", actor=actor, actor_role=actor_role,
            reason=reason, at=at
        )

    def reinstate(
        self, principal_id: str, *, actor: str, actor_role: Optional[str] = None,
        reason: Optional[str] = None, at: Optional[Any] = None
    ) -> AiPrincipal:
        return self._lifecycle(
            principal_id, "REINSTATED", actor=actor, actor_role=actor_role,
            reason=reason, at=at
        )

    def degrade(
        self, principal_id: str, *, actor: str, actor_role: Optional[str] = None,
        reason: Optional[str] = None, at: Optional[Any] = None
    ) -> AiPrincipal:
        """§17: narrow this principal to low-tier tools until a human widens it again."""
        return self._lifecycle(
            principal_id, AI_STATUS_DEGRADED, actor=actor, actor_role=actor_role,
            reason=reason, at=at
        )

    def read_only(
        self, principal_id: str, *, actor: str, actor_role: Optional[str] = None,
        reason: Optional[str] = None, at: Optional[Any] = None
    ) -> AiPrincipal:
        """§17: this principal may look and report, and may change nothing at all."""
        return self._lifecycle(
            principal_id, AI_STATUS_READ_ONLY, actor=actor, actor_role=actor_role,
            reason=reason, at=at
        )

    def activate(
        self, principal_id: str, *, actor: str, actor_role: Optional[str] = None,
        reason: Optional[str] = None, at: Optional[Any] = None
    ) -> AiPrincipal:
        """Widen a degraded or read-only principal back to ACTIVE. Retired is terminal."""
        return self._lifecycle(
            principal_id, "ACTIVATED", actor=actor, actor_role=actor_role,
            reason=reason, at=at
        )

    def retire(
        self, principal_id: str, *, actor: str, actor_role: Optional[str] = None,
        reason: Optional[str] = None, at: Optional[Any] = None
    ) -> AiPrincipal:
        return self._lifecycle(
            principal_id, "RETIRED", actor=actor, actor_role=actor_role,
            reason=reason, at=at
        )

    def _lifecycle(
        self,
        principal_id: str,
        kind: str,
        *,
        actor: str,
        actor_role: Optional[str] = None,
        reason: Optional[str],
        at: Optional[Any],
    ) -> AiPrincipal:
        principal = self.principal(principal_id)
        clean_actor = require_ref(actor, "actor")
        clean_role = None if actor_role is None else require_ref(actor_role, "actor_role")
        if principal.status == AI_STATUS_RETIRED:
            raise AiPrincipalRetired(
                "a retired AI principal is not brought back",
                code="AI_PRINCIPAL_RETIRED",
                principal_id=principal.principal_id,
            )
        allowed = _LADDER_ALLOWED.get(kind, ())
        if principal.status not in allowed:
            raise AiLadderRefused(
                f"{kind} is not a step from {principal.status}: capability widens only through "
                f"the verb that widens it",
                code="AI_LADDER_ORDER",
                principal_id=principal.principal_id,
                from_status=principal.status,
                event=kind,
            )
        with self._one_write():
            self._record_event(principal.principal_id, kind, actor=clean_actor, reason=reason, at=at)
            self.audit.append(
                action=f"AI_PRINCIPAL_{kind}",
                entity_type="ai_principal",
                entity_id=principal.principal_id,
                actor=clean_actor,
                # The actor's own role when the caller knows it (the gateway verifies it before the
                # call gets here); the legacy heuristic only covers callers that pass a bare reference.
                actor_role=clean_role
                or ("owner" if clean_actor.startswith("owner") else "system"),
                reason=reason or f"AI principal {kind.lower()}",
            )
        return self.principal(principal.principal_id)

    def _record_event(
        self, principal_id: str, kind: str, *, actor: str, reason: Optional[str], at: Optional[Any]
    ) -> None:
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM ai_principal_events WHERE principal_id = ?",
            (principal_id,),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO ai_principal_events (ai_principal_event_id, principal_id, seq, kind,"
            " actor, reason, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.ids.new("aipe"),
                principal_id,
                int(seq),
                kind,
                actor,
                reason,
                self._stamp(at),
            ),
        )

    def _status(self, principal_id: str) -> str:
        """The newest event decides. Retirement is not undone by an earlier reinstatement."""
        row = self.conn.execute(
            "SELECT kind FROM ai_principal_events WHERE principal_id = ? ORDER BY seq DESC LIMIT 1",
            (principal_id,),
        ).fetchone()
        if row is None:
            return AI_STATUS_ACTIVE
        kind = row["kind"]
        if kind in _ACTIVE_KINDS:
            return AI_STATUS_ACTIVE
        if kind in (AI_STATUS_DEGRADED, AI_STATUS_READ_ONLY, AI_STATUS_SUSPENDED):
            return kind
        return AI_STATUS_RETIRED

    # -- tools -------------------------------------------------------------- #

    def tools(self) -> tuple[AiTool, ...]:
        rows = self.conn.execute("SELECT * FROM ai_tools ORDER BY tier, tool").fetchall()
        return tuple(
            AiTool(
                tool=row["tool"],
                tier=row["tier"],
                policy_key=row["policy_key"],
                spec_ref=row["spec_ref"],
                description=row["description"],
            )
            for row in rows
        )

    def tool(self, tool: str) -> AiTool:
        name = require_ref(tool, "tool", max_len=80)
        row = self.conn.execute("SELECT * FROM ai_tools WHERE tool = ?", (name,)).fetchone()
        if row is None:
            raise UnknownAiTool(
                "the AI service holds no such tool",
                code="AI_UNKNOWN_TOOL",
                tool=name,
            )
        return AiTool(
            tool=row["tool"],
            tier=row["tier"],
            policy_key=row["policy_key"],
            spec_ref=row["spec_ref"],
            description=row["description"],
        )

    # -- authorisation ------------------------------------------------------ #

    def authorize(
        self,
        principal_id: str,
        tool: str,
        *,
        intent: Optional[str] = None,
        at: Optional[Any] = None,
    ) -> AiAuthorisation:
        """Decide whether this principal may use this tool right now.

        Refusals raise *and* leave an audit row: a refusal that nobody can read later is
        indistinguishable from an attempt that never happened. ``intent`` is recorded and
        never consulted — §56 is explicit that conversation text cannot raise privilege.
        """
        principal = self.principal(principal_id)
        definition = self.tool(tool)
        deny = self._deny_policy(at=at)
        denied = tuple(deny.as_json())
        intent_text = None if intent is None else require_text(intent, "intent", max_len=500)

        if definition.tool in denied:
            self._refuse(
                principal,
                definition,
                reason=f"deny registry: {definition.tool}",
                policy_revision_id=deny.policy_revision_id,
                error=AiDeniedAction(
                    "the deny registry forbids this action (§18, ADR-0009)",
                    code="AI_DENIED_ACTION",
                    tool=definition.tool,
                    principal_id=principal.principal_id,
                ),
            )

        if not principal.may_act:
            self._refuse(
                principal,
                definition,
                reason=f"principal {principal.status.lower()}",
                policy_revision_id=None,
                error=AiPrincipalNotActive(
                    "this AI principal is not active",
                    code="AI_PRINCIPAL_NOT_ACTIVE",
                    tool=definition.tool,
                    principal_id=principal.principal_id,
                    status=principal.status,
                ),
            )

        if definition.tier == "HIGH":
            self._refuse(
                principal,
                definition,
                reason="high-risk: human authority only",
                policy_revision_id=None,
                error=AiHumanAuthorityRequired(
                    "§56 reserves this action to human authority",
                    code="AI_HUMAN_AUTHORITY_ONLY",
                    tool=definition.tool,
                    tier=definition.tier,
                    principal_id=principal.principal_id,
                ),
            )

        if definition.tier == "MEDIUM":
            allowance = self._allowance(at=at)
            allowed_tools = () if allowance is None else tuple(allowance.as_json())
            if definition.tool not in allowed_tools:
                self._refuse(
                    principal,
                    definition,
                    reason=f"medium-risk tool not on the allowance: {definition.policy_key}",
                    policy_revision_id=None if allowance is None else allowance.policy_revision_id,
                    error=AiMediumToolNotAllowed(
                        "§56 requires a published policy to control this tool",
                        code="AI_MEDIUM_TOOL_NOT_ALLOWED",
                        tool=definition.tool,
                        tier=definition.tier,
                        policy_key=definition.policy_key,
                        principal_id=principal.principal_id,
                    ),
                )
            governing = allowance.policy_revision_id
        else:
            governing = deny.policy_revision_id

        decided_at = self._stamp(at)
        self._write_audit(
            "AI_TOOL_ALLOWED",
            principal,
            definition,
            reason=intent_text or f"tier {definition.tier}",
            policy_revision_id=governing,
        )
        return AiAuthorisation(
            principal_id=principal.principal_id,
            tool=definition.tool,
            tier=definition.tier,
            allowed=True,
            policy_revision_id=governing,
            intent=intent_text,
            decided_at=decided_at,
        )

    def _refuse(
        self,
        principal: AiPrincipal,
        definition: AiTool,
        *,
        reason: str,
        policy_revision_id: Optional[str],
        error: DomainError,
    ) -> NoReturn:
        self._write_audit(
            "AI_TOOL_REFUSED",
            principal,
            definition,
            reason=reason,
            policy_revision_id=policy_revision_id,
        )
        raise error

    def _write_audit(
        self,
        action: str,
        principal: AiPrincipal,
        definition: AiTool,
        *,
        reason: str,
        policy_revision_id: Optional[str],
    ) -> None:
        self.audit.append(
            action=action,
            entity_type="ai_tool",
            entity_id=definition.tool,
            actor=principal.principal_id,
            actor_role=AI_ACTOR_ROLE,
            reason=reason,
            policy_version_id=policy_revision_id,
            tool=definition.tool,
        )


# --------------------------------------------------------------------------- #
# the §56 gate every policy-controlled path shares
# --------------------------------------------------------------------------- #


def require_tool_authority(
    conn: sqlite3.Connection,
    actor: Any,
    actor_role: Any,
    principal_id: Optional[str],
    *,
    tool: str,
    clock: Optional[Any] = None,
    ids: Optional[IdFactory] = None,
    agency_id: str = DEFAULT_AGENCY_ID,
) -> None:
    """§56: a machine actor may use a medium-risk tool only under policy control.

    A human actor is admitted directly — §56 reserves *policy control* for machine
    actions, and requiring a published policy from a human would be a different rule
    than the one the specification states. A machine actor must name the principal it
    acts as and clear ``tool`` through the AI service, which is deny-first: until the
    owner publishes that tool on ``security.ai.medium_tool_allowance`` the answer is
    no. That refusal is the intended behaviour, not a stub — §56 classes these actions
    as policy-controlled, so they open by publishing a policy value rather than by
    changing code.

    It lives here, beside the authority it enforces, because every gated path must
    share one implementation: a second path cannot inherit a weaker copy of this rule
    and cannot forget the principal requirement.
    """
    if not is_machine_actor(actor, actor_role):
        return
    if principal_id is None:
        raise DomainError(
            "a machine actor must name the principal it acts as",
            code="MACHINE_PRINCIPAL_REQUIRED",
            actor=actor,
            actor_role=actor_role,
        )
    AiService(
        conn,
        policy=PolicyResolver(conn, clock=clock, agency_id=agency_id),
        clock=clock,
        ids=ids,
        agency_id=agency_id,
    ).authorize(require_ref(principal_id, "principal_id"), tool)
