# ADR-0018 — The AI service identity and its tool envelope (§18, §56)

- Status: **ACCEPTED** (recorded with WAVE 1-I, 2026-09-26)
- Date: 2026-09-26
- Spec: Master Spec v1.1 §16, §18, §20, §21, §40, §50 (WAVE 1), §56, §57
- Amends: ADR-0009 (AI gateway — the identity half is now built; the runtime half stays
  PROPOSED for WAVE 8)

## Verification against §21 (the deny registry's own section)

§21 lists 16 forbidden capabilities (counting `audit.modify` and `audit.delete`
separately). The registry published here is a **superset**: those 16 plus six
crypto/treasury capabilities (`blockchain.sign`, `crypto.transfer`,
`withdrawal.approve`, `treasury.control`, `key.read_raw`, `seed_phrase.read`) carried over
from the ADR-0009 amendment, deduplicated against `secret.read`. Extending the registry is
policy, not a deviation — the owner can extend it again without a code change.

§21 also says forbidden capabilities "sebaiknya tidak dibuat sebagai AI tools sama sekali".
This ADR **deviates deliberately**: every §56 capability is registered, including the
forbidden ones, and the deny registry refuses them *by name* before the tier is even
consulted. The reason is auditability — `AI_DENIED_ACTION` with the named capability is a
finding, while a generic `AI_UNKNOWN_TOOL` for `payment.execute` hides what was attempted.
The risk §21 guards against (some future code path reading "registered" as "allowed") is
covered by deny-first ordering, the single `authorize()` entry point, and the test that
asserts a registered-but-denied tool is refused. If the owner prefers the stricter reading,
the change is to stop seeding the denied capabilities into `ai_tools` — the registry stays
as policy either way.

## Context

§50 places "AI service identity" in WAVE 1. §18 gives the *AI Service* role "narrow safe
tools" and an explicit deny on financial authority, secrets, raw passport and privileged
security. §56 (LOCKED) fixes three tiers:

| Tier | §56 wording | Examples |
|---|---|---|
| LOW | low-risk autonomous | inquiry intake, clarification, deterministic villa search, verified Q&A, alternatives, routine follow-up, booking status, verified pre-arrival information |
| MEDIUM | "require explicit policy-controlled tools" | availability requests, tentative holds, quotation sending, special-request coordination, selected supplier/client communications |
| HIGH | human-authority only | financial approval/execution, bank changes, refunds, out-of-policy discounts, commercial overrides, contract/rate verification and activation, serious incident compensation, legal disputes |

§56 closes with "Conversation instructions never increase AI privileges". ADR-0009 had
already recorded a deny registry (15 items, amended to 22 after deduplicating
`secret.read`), but nothing enforced it: the platform had machine *actors* (prefixes in
`supplier_route.py`) and no machine *identity*.

The traceability matrix called this out twice: "AI deny registry — Partial by absence —
MISSING — must codify no forbidden capabilities", and "AI Service identity + autonomy —
MISSING — §18/§56/§57".

## Decision

1. **Identity is data.** `ai_principals` holds the machine identity with a `secret_ref`
   (a *name* for a secret, never a value — the ADR-0005 convention). `ai_principals` has no
   column that could hold a secret; the test asserts the column set.

2. **The kill switch is an append-only log.** `ai_principal_events`
   (`REGISTERED`/`SUSPENDED`/`REINSTATED`/`RETIRED`) with an explicit `seq`; status is
   *derived from the newest event*, never stored as a mutable column. This gives §40's kill
   switch a home and keeps *when* and *who* — the two things an incident review needs. It
   also keeps SQLite and PostgreSQL identical, because the twin generator ignores trigger
   `WHEN` clauses and a mutable status would have needed one.

3. **The tool vocabulary is data, and it is platform-wide.** `ai_tools` registers every
   §56 capability with its tier. Registration is a description of the domain, not a grant:
   the capabilities the AI may never use are registered too, and are stopped by the deny
   registry and the tier. `ck_ai_tools_medium_is_policy_controlled` makes a medium-risk tool
   without a `policy_key` impossible to store — §56 calls medium risk
   *policy-controlled*, so a medium tool with no policy is not medium risk, it is
   uncontrolled. No `agency_id`: §56 classifies capabilities for the platform, and a
   per-agency copy would let one tenant believe the AI holds authority it does not have
   here.

4. **The deny registry is policy** (`security.ai.denied_actions`, published in WAVE 1-H
   with 22 actions). It is resolved from the §16 store, so the owner can extend it without a
   code change, and it is checked **before** the allowlist: a denial a grant can outvote is
   not a denial.

5. **A medium-risk tool needs a published controlling policy.**
   `security.ai.medium_tool_allowance` names the medium-risk tools the owner has allowed.
   It is **deliberately unpublished**: the owner decides, and until then every medium-risk
   tool is refused with `AI_MEDIUM_TOOL_NOT_ALLOWED`. Publishing a revision enables a tool
   with no code change (proved by test). This is the single decision that unblocks §56
   medium-risk work, including `option_sets.present()` for machine actors.

6. **Authority comes from the principal and the registered tool, never from the request.**
   `authorize(principal_id, tool, *, intent=None)` has no parameter that could carry
   authority, and `intent` — free text from a conversation — is recorded and never
   consulted. §56's rule is enforced structurally, not by a prompt.

7. **Every attempt is audited, including refusals.** The audit row is written *before* the
   refusal is raised, in its own transaction, so a refusal survives the exception and carries
   `tool`, `actor_role = 'ai_service'` and the `policy_version_id` that governed the
   decision (§16). A refusal nobody can read later is indistinguishable from an attempt that
   never happened.

## Consequences

- The §56 envelope is enforceable. `AiService.authorize()` is the single entry point; the
  audit chain records what the AI tried, what it was allowed, and which policy said so.
- `option_sets.present()`'s fail-closed `MACHINE_ACTOR_REFUSED` can now be replaced by a
  real policy decision *once the owner publishes the allowance* — it is not replaced today,
  because the allowance is unpublished.
- The AI **gateway** (context builder, redaction, LLM call, tool execution runtime) remains
  WAVE 8 and ADR-0009 stays PROPOSED for that half. This ADR covers the identity and the
  envelope only; there is still no code path that calls a model.
- Open, and honestly reported: (a) the role vocabulary is inconsistent — §18 names 12 roles
  while `approvals.ROLE_RANK` ranks {agent, manager, accountant, owner} (renamed from
  `supervisor` by ADR-0028, 2026-09-27) and
  `identity` uses {owner, finance}; reconciling them changes approval behaviour, so it needs
  the owner's decision; (b) §50's "transactional outbox" and the 12-role RBAC are still
  missing; (c) nine catalogue keys were unpublished at the time of writing — six remain as of
  2026-09-27, the three discount values having since been published (ADR-0029) (§49 retention is OPEN, and
  the finance/ops values have no document fixing them yet).
