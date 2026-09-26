# ADR-0017 — The governance policy engine (§16)

- Status: **ACCEPTED** (recorded with WAVE 1-H, 2026-09-26)
- Date: 2026-09-26
- Spec: Master Spec v1.1 §5, §16, §18, §49, §50 (WAVE 1), §56

## Context

§50 places a *policy engine* in WAVE 1 and §16 says why: policy "wajib menjadi
first-class backend component, bukan angka tersebar di source code", versioned with
`effective_from`/`effective_to`, and historical approvals and transactions must store the
policy version that governed them.

The brownfield audit (`docs/MASTER_SPEC_TRACEABILITY_MATRIX.md`) marked the policy engine
`Strong partial / KEEP/REFACTOR` with the required action *"expand into first-class
cross-domain policy registry"*, and the module it pointed at (`src/policy_engine.py`) is a
real but **narrow** kernel: a versioned registry of *priced* policy (deposit, service fee,
tax, discount ceiling, quote validity, hold TTL) scoped to a property or unit, with
`source_doc_ref` and `approved_by` already mandatory.

The gap was measured, not assumed. Grepping the source for the §16 catalogue showed the
governance numbers still living as module constants:

| §16 example | where the number lived |
|---|---|
| step-up authentication | `identity.STEP_UP_WINDOW`, `identity.HIGH_RISK_SUBJECT_TYPES` |
| session policy | `identity.SESSION_IDLE_TIMEOUT`, `identity.SESSION_ABSOLUTE_TIMEOUT` |
| MFA requirement | `identity.MFA_REQUIRED_ROLES` |
| dual-approval thresholds | `approvals.DUAL_CONTROL_SUBJECTS`, `DUAL_CONTROL_MIN_APPROVALS`, `MAX_REQUIRED_APPROVALS` |
| payment approval windows | `approvals.EXPIRY_MINUTES` |
| reconfirmation freshness | `gates.LEVEL_FRESHNESS_SECONDS`, `LEVEL_MAX_FRESHNESS_SECONDS` |

`policy_versions` (the priced kernel) could not absorb them: its scope is a property/unit
and its columns are percentages of a rate, so a platform-wide security window has no honest
place in it.

## Decision

1. **The vocabulary is a table, the values are a table.** `policy_defs` declares each key
   (domain, value type, risk level, owner role, §-reference, description); `policy_revisions`
   holds the values (agency, version, value, window, `source_doc_ref`, `approved_by`,
   fingerprint). Both are append-only with unconditional triggers, so history is protected by
   the database and the PostgreSQL twin stays identical (a conditional trigger would diverge —
   see ADR-0016).

2. **Resolution returns the revision, not a bare number.** `PolicyResolver.resolve(key, at)`
   yields the covering revision with the highest version, carrying `policy_revision_id`,
   `version` and `fingerprint`. A decision that stores those can be replayed and audited, which
   is what §16 asks for when it says historical decisions store the policy version that applied.

3. **Supersession is "highest version covering the instant", and publishing may not backdate.**
   This deliberately differs from `PolicyRegistry.active()`, which refuses an overlapping
   window because two prices for one night make a quote unreproducible. A governance value *is*
   superseded by a later revision that starts while the previous one is still nominally open,
   and because the table is append-only an old row can never be closed after the fact. So the
   rule here is deterministic (highest version wins), non-retroactive (`publish()` refuses an
   `effective_from` earlier than the newest revision's start with
   `POLICY_BACKDATED_REVISION`), and reproducible (the chosen revision is returned). The priced
   kernel keeps its stricter rule unchanged.

4. **Resolution fails closed, in four ways.** Unknown key (`POLICY_UNKNOWN_KEY`), a key with no
   covering revision (`POLICY_VALUE_NOT_PUBLISHED`), a value that does not match its declared
   type or range (`POLICY_VALUE_INVALID`), and an inverted window (`POLICY_WINDOW_INVALID`).
   There is no default value and no "closest" revision: a number that was never published is
   never invented.

5. **Eight catalogue keys are deliberately left unpublished.** `retention.records.retention_days_by_class`
   (the spec itself leaves retention periods OPEN in §49), plus the discount authority, margin
   floor, payment thresholds, booking financial gates, hold reminders, reconfirmation freshness
   and incident escalation. The catalogue names them so they can be decided, and resolution
   refuses until they are — an honest refusal instead of a plausible-looking constant.

6. **One policy, one home.** Client deposit %, quote validity, hold TTL and the discount
   ceiling stay in `policy_versions`, where they are already versioned, cited and approved.
   The governance store does not duplicate them; duplicating a value is how two sources of
   truth start.

7. **The baseline revisions cite their documents.** Every published value names where it came
   from (ADR-0005 for the MFA/session/step-up set, `src/approvals.py` and `src/gates.py` for the
   windows being migrated) and is recorded as approved by `owner_anas`, matching the existing
   `policy_versions` seed convention. **No value changed**: this wave moves numbers, it does not
   re-decide them. The owner can supersede any of them by publishing a new revision.

8. **One consumer is wired, and the constants are pinned to the store.**
   `SessionService.from_policy()` builds a service whose idle/absolute timeout, step-up window,
   MFA role set and high-risk subject set come from the resolver, and the methods that use them
   (`open_session`, `authorize`, `require_step_up`, `Session.needs_mfa`) read the service's own
   attributes. The module constants remain the documented baseline for callers that do not wire
   a resolver — and a drift-guard test fails the suite if a constant and its published revision
   ever disagree. The bridge is therefore proven by a test that publishes a 15-minute step-up
   window and gets it **without touching code**.

## Consequences

- §56's medium-risk class ("selected client communications") now has the policy-controlled
  tool it requires, which unblocks the machine-actor refusal in `src/option_sets.py` (WAVE 1-I).
- WAVE 4 finance and availability work reads thresholds through `PolicyResolver` instead of
  adding constants.
- Open: the role vocabulary is inconsistent and needs an owner decision. §18 lists twelve roles;
  `approvals.ROLE_RANK` ranks `agent`/`supervisor`/`accountant`/`owner`, and `identity` speaks of
  `owner`/`finance`. Reconciling them changes approval behaviour, so it is not done here.
- Open: the transactional outbox and the full twelve-role RBAC remain WAVE 1 foundations not yet
  built.
- Open: the eight unpublished keys need owner decisions before the finance and availability
  gates that depend on them can be enforced rather than refused.
- Open: only one consumer is wired. The remaining constants (`approvals`, `gates`) are pinned by
  the drift guard but still read from code until their callers are migrated.
