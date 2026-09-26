# ADR-0024 — The §56 enforcement point for presenting an option set, and one gate for every gated path

- **Status**: ACCEPTED (recorded with WAVE 4-B-4, 2026-09-26)
- **Spec references**: §56 (AI reservation agent autonomy — medium-risk, policy-controlled tools), §7 (CLIENT INQUIRY → CHECK AVAILABILITY → CLIENT DECISION), §18 (AI service identity), §16 (policy engine), §5/§34 (option sets)
- **Amends**: ADR-0016 (§7) and ADR-0018, which both recorded `MACHINE_ACTOR_REFUSED` as an explicit *placeholder* to be replaced once the policy engine exists. **Supersedes**: nothing.
- **Test evidence**: `tests/test_wave4b4_option_present_authority.py` (15 tests), the two rewritten tests in `tests/test_wave3b_option_sets.py`, and `tests/test_no_dead_imports.py` (the static guard this wave added)

## Context

ADR-0016 §7 wrote down the rule and the reason it could not be enforced: *"Presenting an option set is human authority (§56). Sending options to a guest is a client communication, which §56 classes as medium risk and reserves for policy-controlled tools. No policy engine exists yet, so `present()` fails closed for machine identities (`MACHINE_ACTOR_REFUSED`) instead of pretending to gate them."* Its consequence section listed that engine as open work.

ADR-0018 then named the follow-up exactly: *"`option_sets.present()`'s fail-closed `MACHINE_ACTOR_REFUSED` can now be replaced by a real policy decision."* Four measured facts decide how.

1. **The policy engine and the allowance mechanism now exist** (ADR-0017, ADR-0023). The stated precondition for replacing the placeholder is met, so leaving it is no longer "waiting for the engine" — it is a rule the platform has outgrown.
2. **The §56 catalogue already names the tool.** `comm.client_selected` is `MEDIUM` with policy key `security.ai.medium_tool_allowance`, described as "Selected client communications". `option_sets.py`'s own docstring had already classified presenting as a client communication, so the enforcement point needs no new capability: it needs the one the catalogue already carries.
3. **The gate existed in exactly one place, as a method.** `reservation_kernel._require_tool_authority` held the only implementation, with its docstring stating the requirement: *"One implementation for every §56 gate: a second gated path cannot inherit a weaker copy of this rule, and cannot forget the principal requirement."* A second gated path could only be added by copying it — and the copy is where a rule drifts.
4. **`make check` runs no static analysis.** The refactor in this ADR left three dead imports in `reservation_kernel` (`AiService`, `PolicyResolver`, `is_machine_actor`). The gate passed anyway, because it runs `syntax`, `test`, `secret-scan`, `unsafe-scan` and `schema-parity` — nothing that reads imports. `make lint` exists but needs `pyflakes`, which is not installed here, so it is not part of the gate.

## Decision

1. **The gate moves to `ai_service.require_tool_authority(...)`**, beside the authority it enforces. `reservation_kernel._require_tool_authority` now delegates to it with no behaviour change, and `option_sets.present()` calls the same function. A test asserts *object identity* across `ai_service`, `reservation_kernel` and `option_sets`, so a future second copy fails the suite rather than drifting quietly.
2. **`option_sets.present()` gets its enforcement point for `comm.client_selected`**, named as the constant `PRESENTATION_TOOL` with the catalogue row cited in the comment. Nothing is invented: the tool is the catalogue's own entry for the action the module had already classified as a client communication.
3. **`present()` takes `principal_id`**, the machine identity that acts. A human caller leaves it empty. A machine caller that omits it is refused `MACHINE_PRINCIPAL_REQUIRED` — the same rule as the hold path, because the shared gate is the same function.
4. **`MachineActorRefused` is deleted, not deprecated.** It encoded a placeholder rule, so nothing can raise it any more; a catchable code that can never be raised is a trap for the next reader. The class and its `__all__` entry are gone.
5. **Deny-first is preserved, and is now audited.** With the allowance unpublished, a machine actor is refused `AI_MEDIUM_TOOL_NOT_ALLOWED`, the AI service writes its own `AI_TOOL_REFUSED` row, and no presentation row is written. The old hard refusal left no trace at all, so the refusal is strictly more accountable than what it replaces.
6. **The human path is unchanged.** §56 reserves *policy control* for machine actions; requiring a published policy from a human would be a different rule than the one the specification states. A human presents exactly as before.
7. **The gate runs before the `ALREADY_PRESENTED` lookup**, following the ADR-0022 precedent: the permission is about *showing it*, so a replay is gated too.
8. **Opening it is a policy act, not a code change.** The owner adds `comm.client_selected` to the allowance through `scripts/publish_ai_allowance.py`; no code is touched. That is the whole point of §56, and it is why the gate is wired while the allowance still does not name the tool.
9. **`make check`'s blind spot is closed with the standard library.** `tests/test_no_dead_imports.py` fails the suite when a module imports a name it never uses, with `# noqa: F401` as the explicit, greppable escape hatch for a genuine re-export. The check is deliberately conservative — a name counts as used when it appears as an identifier (including annotations), in `__all__`, or as a string literal that is exactly an identifier — so it catches real dead imports without inventing failures.
10. **The guard was applied to the whole tree, not just to the module being changed.** It found five more dead imports in modules this wave did not otherwise touch: `json` and `secrets` in `evidence.py`, `json` and `Sequence` in `identity.py`, and `date` in `supplier_route.py`. Each was verified by hand (the `date` import was dead because the module uses `parse_iso_date`/`to_iso_date` from `primitives`) and removed. A dead import is not harmless: it keeps a dependency edge alive that the code no longer has.

## Consequences

**What this opens**

- Presenting options to a guest becomes exercisable by a machine under a real, attributable, time-boxed permission, and the refusal is recorded with the principal, the tool and the governing policy revision.
- §56's rule now has one home. The next gated path adds a call, not a copy.
- The suite fails on a dead import, so this class of finding no longer depends on someone reading the diff.

**What this does not do (honest gaps)**

1. **The allowance still does not name `comm.client_selected`, so a machine is refused today.** That is the owner's decision (ADR-0023 named one tool deliberately), not a code gap — and it is the state the tests pin.
2. **Four of the six §56 MEDIUM tools still have no enforcement point** (`quotation.send`, `request.coordinate_special`, `comm.supplier_selected`, and `hold.request_tentative` — the last is gated but not allowed). §56 coverage remains partial by construction.
3. **No rate limit** on presentations or requests: §7 names no number, and inventing one would be policy by guesswork. Measured earlier: 50/50 admitted from a single principal.
4. **`make check` still runs no static analysis** beyond this one guard. `make lint` needs `pyflakes`, which is not installed, so dead code that is *not* an import (unused private helpers, unreachable branches) is still found by reading, not by the gate.
5. **The gate creates an `AiService` per call.** That is the same shape as the hold path and costs one object; if a hot path ever needs it, the right fix is an injected service, not a second copy of the rule.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Keep the hard refusal and add an `if` for the allowance | Two rules for one gate. The copy drifts, and the principal requirement is the first thing a copy forgets. |
| Invent a catalogue tool `option_set.present` | §56's catalogue is the authority. The closest tool already exists and the module had already called presenting a client communication, so a new row would assert a capability the specification does not name. |
| Give `OptionSetService` its own `AiService` wiring | The gate would then depend on caller wiring: a caller that forgot to wire it would silently get a weaker path, and the fail-closed default would move from the platform to the call site. |
| Deprecate `MachineActorRefused` but keep it importable | A code that can never be raised still reads as a rule. Deleting it is the honest record that the placeholder is gone; the ADRs keep the history. |
| Make `principal_id` optional for machine actors | A gate a caller can skip by omitting an argument is not a gate (ADR-0022). |
| Wire the gate only after the owner publishes the tool | The wiring *is* what makes publishing meaningful. Until it exists, publishing `comm.client_selected` would change nothing while looking like a decision — and the refusal is audited, which the placeholder's was not. |
| Fix the three dead imports by hand and stop there | The next refactor repeats it. The guard is the difference between fixing a finding and closing a class of findings. |

## Verification

- `tests/test_wave4b4_option_present_authority.py` — 15 tests: the human path is unaffected; a machine actor is refused while the allowance is unpublished and writes nothing; a machine must name its principal; a machine-prefixed actor is gated rather than hard-refused; an unknown principal is refused; a suspended principal is refused even with the allowance; publishing `comm.client_selected` admits the machine and records `presented_by`; publishing a *different* tool alone does not open presenting; an expired allowance closes the path again; the gate runs before the already-presented lookup; a refusal leaves `AI_TOOL_REFUSED` and no presentation row; an allowed presentation is audited against the principal; and the three modules share one gate object.
- `tests/test_wave3b_option_sets.py` — the two tests that encoded the placeholder (`MACHINE_ACTOR_REFUSED`) were rewritten to the real rule (`MACHINE_PRINCIPAL_REQUIRED` with nothing written), keeping the file's scope on option sets.
- `tests/test_no_dead_imports.py` — the static guard; it fails on the three imports this refactor left behind and on the five found elsewhere.
- Full suite and `make check` → PASS (see the WAVE 4-B-4 report for the measured counts).
- No schema change: the §56 catalogue already carried `comm.client_selected`, so 58 tables / 50 triggers and the PostgreSQL twin are untouched.
