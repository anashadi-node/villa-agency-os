# ADR-0040 — Security, safety and privacy come first: the principle, and what enforces it

- Status: Accepted
- Date: 2026-10-02 (owner decision) · written 2026-10-02
- Deciders: owner
- Amends: every ADR that trades a control for throughput (ADR-0036's GM tier, ADR-0037's tenant settings, ADR-0039's warden)
- Spec: Master Spec §18, §19, §21, §49, §56

## Context

The owner stated the standing principle on 2026-10-02, while approving the four guardrail entries:

> "intinya Prinsip : **Keamanan, Security & Privacy adalah Hal yang Utama dan Pertama** dalam sistem."

A principle written down is worth exactly as much as what it overrules. This platform already
retreats from several things the principle outranks: throughput (the GM tier stalls a payment
when the GM is asleep), convenience (a role with no MFA must fetch a code), margin (a €9 fraud
screen beats a €9 fee), and speed of delivery (the gate runs 3.078 tests on every change). Those
were all decided case by case. This ADR makes them one rule with one name, so the *next* proposal
that trades a control for speed has a standing answer rather than an argument.

## The rule

**No feature, deadline, tenant request, or convenience may weaken a control. When safety and
speed conflict, safety wins, and the cost is named out loud rather than hidden.**

Four consequences, each already enforced by something that can fail:

| Consequence | What it means in practice | What enforces it today |
|---|---|---|
| **Deny by default** | An unknown role, an unscoped write, an unpublished policy value, an unrecognised data class: refused, not guessed | `roles.UnknownRole`, `approvals._role`, `PolicyValueNotPublished`, `gates` classification |
| **An AI may not hold the keys, the money, or the pen that approves** | No AI executes a payment, approves one, changes a bank destination, signs a chain transaction, or reads a raw secret — and the warden may not rewrite its own rails | `security.ai.denied_actions` v3 (27 entries), `ai_service.authorise()` refusals with an audit row |
| **A human authorises what a machine cannot** | Money out, bank change, data deletion, tenant-visible settings, role changes, production deploys | ADR-0036's ladder, ADR-0037's tenant settings, ADR-0039's warden matrix |
| **Privacy is a design constraint, not a setting** | Sensitive data is classified (P0–P4), redacted before it reaches a model, never stored for the classes that forbid it, and every P3 export needs a recorded reason | `docs/DATA_CLASSIFICATION_AND_ACCESS.md`, `gates.P2_AUTH_ROLES`/`P3_AUTH_ROLES`, `unsafe_config_scan.sh` |

## What this principle does *not* say

- **It is not "refuse everything".** A control that makes the business unusable is not safety; it
  is a different failure. Where the principle wins, the *cost is written down* — ADR-0036 named
  the 240-minute expiry as the price of the GM tier, and the threshold option was recorded, not
  applied.
- **It does not make the platform the owner of the tenant's data policy.** The tenant fills its
  own settings (ADR-0037); the platform's floor is the floor.
- **It does not freeze the design.** It sets the burden of proof: a proposal that relaxes a
  control needs evidence and an owner decision; a proposal that tightens one needs neither.

## Consequences

- **Tightening is cheap; loosening is expensive.** A new deny entry, a stricter SLA, a smaller
  MFA window: accepted on sight. Loosening any of them requires an owner decision recorded in an
  ADR, because that is where the cost lands.
- **Every control needs a test that fails when the control is removed.** The principle is only as
  strong as the suite: the deny registry has a guard test that fails on an unaccounted entry, the
  SLA has a ceiling test, the roles have a per-role matrix test, and the reconciliation of the
  vocabulary has a drift guard. A control without a failing test is a comment.
- **The warden inherits the rule.** The agent built to protect the system is bounded by it in the
  same sentence: it may repair the house and may not change the locks (ADR-0039).
