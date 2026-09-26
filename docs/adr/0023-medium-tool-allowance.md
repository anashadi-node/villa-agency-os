# ADR-0023 — Publishing the §56 medium-tool allowance: one tool, a bounded window, and no permission in the schema

- **Status**: ACCEPTED (recorded with WAVE 4-B-3, 2026-09-26)
- **Spec references**: §56 (AI reservation agent autonomy — medium-risk, policy-controlled tools), §16 (policy engine), §7 (CHECK AVAILABILITY), §18 (AI service identity)
- **Amends**: ADR-0021, ADR-0022 (both left the allowance unpublished and named the publication as the owner's decision). **Supersedes**: nothing.
- **Test evidence**: `tests/test_wave4b3_ai_allowance_decision.py` (8 tests — the semantics of the published value) and `tests/test_ai_allowance_cli.py` (6 tests — the documented command)

## Context

ADR-0021 and ADR-0022 built the enforcement points. Both left `security.ai.medium_tool_allowance` unpublished, so the machine path refuses — deny-first by construction. What was still open was never *whether* the gate exists, but **how the opening is made and where it lives**. Four measured facts decided it.

1. **The schema already seeds 12 `policy_revisions`** — and some are permission-shaped (`security.mfa.required_roles`, `security.session.owner_timeout_minutes`, `finance.approval.dual_control_min_approvals`, `availability.evidence.freshness_hours`). Every one carries a `source_doc_ref` reading "… migrated to policy (WAVE 1-H)": they are **constants that used to live in code**, values the platform cannot function without, whose absence is not a valid state.
2. **Every test database is built from that same schema.** Seeding the allowance there would open the machine path in *every* environment including the suite: the 8 tests that assert `AI_MEDIUM_TOOL_NOT_ALLOWED` on a fresh database would fail, and deny-first would become a property of the seed file rather than of the platform.
3. **`PolicyResolver.publish()` computes the revision `fingerprint`** from the canonical window, and nothing verifies a stored fingerprint on read. A hand-written row in the DDL would therefore be an artefact nobody can check.
4. **`scripts/publish_anchor.py` already establishes the ops-script shape** for a recorded publish against a real database.

§56's own design intent agrees with the second fact: a medium-risk tool opens "by publishing a policy value rather than by changing code" (ADR-0021).

## Decision

1. **The schema seeds no permission.** The rule, stated so the next person can apply it: *the schema seeds the values the platform cannot run without; a permission's absence is a valid state, so permissions are published as recorded acts.* This is why the 12 baselines stay and the allowance does not join them.
2. **One tool: `availability.request`.** It is the only §56 MEDIUM tool with an enforcement point today (ADR-0021). The other five have no caller and no gate, so allowing them would assert coverage the platform cannot deliver. `hold.request_tentative` now *has* a gate (ADR-0022) and stays refused until the owner names it: a visible refusal is the honest state, an allowance without a gate is a claim.
3. **A bounded window, not a permanent opening.** `effective_from = 2026-09-27`, `effective_to = 2026-10-26`. Measured (ADR-0022 probe, T2 and F4): outside the window the resolver finds no effective revision, the machine is refused, and the refusal code chain is `POLICY_VALUE_NOT_PUBLISHED` → `AI_MEDIUM_TOOL_NOT_ALLOWED`. Exposure therefore ends without anyone remembering to end it — the failure mode of every standing permission.
4. **The opening is an ops act**: `scripts/publish_ai_allowance.py` with `status` / `publish` / `withdraw`. It requires `--approved-by`, writes through `PolicyResolver.publish()` so the fingerprint comes from the platform, and refuses to run against a missing database or without an approver. `withdraw` publishes an empty list: one revision, no code change, and the decision it replaced stays readable because the store is append-only. `withdraw` takes effect **the day it is issued** (the platform clock's today), never on the publish default: a withdrawal that silently waits for a start date is indistinguishable from one that does not close — the first version of this script had exactly that bug, and its own CLI test caught it.
5. **The decision is pinned in the suite, not only in a script.** `tests/test_wave4b3_ai_allowance_decision.py` states it as executable claims: a fresh database resolves nothing and refuses the machine; a human is unaffected; the named tool opens and the other five do not; a **shared database read after the window refuses again** (two clocks, one file — what "time passed" actually looks like); the day before the window is still closed; withdrawal closes it and both revisions remain; and `payment.execute` stays `AI_DENIED_ACTION` even under an allowance naming all six MEDIUM tools.
6. **The command is tested, not only the semantics.** `tests/test_ai_allowance_cli.py` runs the script as a subprocess against a real database file — the way an operator runs it: `status` on a fresh database, a `publish` with no approver refused with exit 2 and *nothing written*, `publish` → the effective value visible, a different tool list published with no code change, `withdraw` closing it in one revision, and a missing database reported as a configuration error (exit 2).

## Consequences

**What this opens**

- The machine path for `availability.request` becomes exercisable end-to-end with a real, attributable, time-boxed permission, so §56's governance envelope is proven *with a value* instead of with its absence.
- Changing the decision needs no code change: a different tool list or window is one recorded publish.

**What this does not do (honest gaps)**

1. Five §56 MEDIUM tools stay refused. Three of them (`request.coordinate_special`, `comm.client_selected`, `comm.supplier_selected`) still have **no caller at all**, so §56 coverage is partial by construction, not by policy.
2. `hold.request_tentative` is gated (ADR-0022) but not allowed — deliberately: it is the next decision, not a leftover.
3. **No rate limit** on requests: measured 50/50 admitted from a single principal. §7 names no number, so the number is the owner's to set, and inventing one would be policy by guesswork.
4. **Nothing schedules the next window.** After 2026-10-26 the machine path closes and stays closed until a human decides again. That is fail-closed and correct, but it is a human dependency: automating renewal would turn a bounded exposure into an unbounded one while looking identical in `status` output.
5. The allowance is **one value per agency**, not per property. A future per-property delegation needs a new policy key, not a longer list.

## Alternatives rejected

| Alternative | Why not |
| Seed the allowance in `001_initial.sql` | Every test database is built from that schema: the machine path would open in the suite and in every fresh environment, and deny-first would become a property of the seed. The 8 tests that assert the closed default on a fresh database would have to be rewritten to close what they were written to prove. |
| Publish all six MEDIUM tools | Five have no enforcement point: an allowance naming them would assert coverage the platform cannot deliver. |
| `effective_to = 9999-12-31` (the baselines' horizon) | A standing permission never expires; a bounded one fails closed on a date nobody has to remember. |
| Publish by editing code or a one-off `python -c` | A permission with no `approved_by`, no `source_doc_ref` and no repeatable tool is not a governance act, and the fingerprint must come from the platform. |
| Build the publish into `seed_demo.py` | The demo seeder creates data; opening a permission is an owner act with an approver. Keeping them apart keeps "what the demo contains" separate from "what the machine may do". |
| Automate renewal | It converts a bounded exposure into an unbounded one while looking identical in the status output. |

## Verification

- `tests/test_wave4b3_ai_allowance_decision.py` — 8 tests (listed in Decision 5). The window test uses one file database read by two clocks, so it exercises the real rule rather than a backdated revision.
- `tests/test_ai_allowance_cli.py` — 9 tests running the documented command as a subprocess against a real file.
- The script was exercised for real on a database holding the demo dataset: `status` (CLOSED, never published) → `publish` with no approver (exit 2, nothing written) → `publish --approved-by owner_anas` (version 1, `["availability.request"]`, 2026-09-26 → 2026-10-26) → `status` (effective) → `withdraw` (version 2, `[]`, effective the same day) → `status` (refused again) → both revisions still present. Raw output is in the WAVE 4-B-3 report.
- **Two traps the CLI tests caught in this script, recorded rather than quietly fixed**: (a) `withdraw` initially fell back to the *publish* default start date, so a withdrawal would have waited until 2026-09-27 while the permission stayed open — it now defaults to the platform clock's today; (b) `PolicyValue.value` is the stored JSON **text**, so `if value.value:` is true for the string `'[]'` — emptiness now goes through the platform's own `as_json()`, the accessor `ai_service` uses.
- A third finding came out of the same evidence run: the demo seeder (`scripts/seed_demo.py`) had been broken since WAVE 4-B and **nothing ran it**. It is fixed and covered by `tests/test_seed_demo_cli.py`; the fix has its own commit and its own tests.
- Full suite and `make check` → PASS.
- No schema change: 58 tables / 50 triggers unchanged, PG twin `--check` clean.

## Amendment — the granting act names its database (2026-09-26)

Two rules added after running the documented commands outside the test suite:

- **`publish` refuses unless `VILLA_DB` is set explicitly** (exit 2, nothing written). Every
  other CLI in this repository falls back to `data/villa.db`, which is the right default for a
  read or a seed; for an authority grant it is not a default anyone chose. Measured, not
  theorised: the README's own publish example, run without `VILLA_DB`, wrote a revision into
  this repository's demo database. Reads and seeds keep the default — only the grant is strict.
- **Both write paths print `target database: <path>`**, so an authority change can be pointed
  at rather than inferred.
- **The withdrawal date is the operator's to name.** A withdrawal defaults to the platform
  clock's today (Decision 4, unchanged), but the engine refuses a revision starting before the
  newest one (`POLICY_BACKDATED_REVISION`). So revoking a revision that begins in the future
  requires `--effective-from` — the README example now shows it, and
  `test_withdraw_can_name_a_date_for_a_revision_that_starts_in_the_future` pins both the
  refusal and the working form. This is the second time the *default date* of a withdrawal
  has been the trap in this script; the first is recorded in Verification above.
- `make allowance-status` exposes the read-only view. Publishing is deliberately not a make
  target: opening a §56 path should cost a typed decision, not a keystroke.
