# ADR-0044 — The escalation vocabulary is closed, the threshold is deny-first, and a handoff needs a staffed role

- Status: Accepted
- Date: 2026-10-02 · written 2026-10-02
- Deciders: owner (the trigger list and where each one routes are the owner's; the enforcement is the platform's)
- Amends: ADR-0035 (the human organisation — the first module that reads *who is actually on duty*), ADR-0041 (the exception engine gains its first caller from the AI side)
- Spec: Master Spec §9 (Mandatory Escalation, escalation triggers, "AI uncertainty above configured threshold"), §13, §17 (policy catalogues are data); `docs/AI_WORKFORCE_TRACEABILITY.md` §9/§4

## Context

§9 lists twelve triggers that **mandatorily** hand a conversation to a human, and names an
escalation path ("Operations/Reservation Manager → GM"). Two questions fall out of that list, and
both were answered badly by default in every system that has ever had a support inbox:

1. **Who detects a trigger?** Some are facts the platform already holds: a requested discount
   above the published maximum, a unit with two conflicting availability records, money that
   arrived after its hold expired. Others are *what a person said*: a legal threat, a serious
   complaint, "I want to speak to a human". No rule in this repository can read intent, and a
   service that pretended to would be guessing in the one place guessing is expensive.
2. **Where does the conversation go?** The obvious implementation routes on a role *label*
   ("Operations Manager"). But a tenant renames its roles in its own language (ADR-0035
   amendment), and authority follows the blueprint, never the label. Worse, D3 decided every
   role starts **unstaffed** — so a handoff to a role nobody holds is a guest handed to a void.

## Decision

**Four rules, and the last one is the one that makes a handoff real.**

| Rule | Why it is the rule |
|---|---|
| **The trigger vocabulary is closed.** A code outside `ESCALATION_TRIGGERS` is refused (`RES_UNKNOWN_TRIGGER`) and *nothing is written*. | A free-text reason is not a trigger anybody can count, route, test or trend. "The guest sounded annoyed" filed as a reason is a signal that will never be found again |
| **The model classifies; the service enforces.** Language-derived triggers arrive as a declared code from the layer that read the message. Platform-detectable ones are rules here. | The honest split. A model may tell us *what* a message seems to be; it does not get to decide whether the platform escalates or how |
| **§9's confidence threshold is deny-first.** Unpublished → escalate. Below the published threshold → escalate. At or above → act. A value between 0 and 1 is refused as ambiguous. | Nobody has decided how sure is sure enough, so the service will not decide it quietly. "The model sounded sure" is not a value a person wrote down. And `0.9` meaning 90% against a threshold of 85 must never pass as "0.9%", so one spelling only |
| **An escalation that is also a failure opens an incident** — `AVAILABILITY_CONFLICT` → `VENUE_UNAVAILABLE`, `FUNDS_AFTER_HOLD_EXPIRY` → `PAYMENT_UNRECONCILED`, `SAFETY_OR_EMERGENCY` → `SAFETY_EMERGENCY`. No new playbook is invented. | A stuck unit or money against an expired hold is already a failure with a playbook (ADR-0041); routing it through the one failure engine means it is counted, deduped and reported the same way wherever it was noticed |
| **A handoff targets a blueprint, and only a staffed one may be taken.** `staffed_blueprints()` reads `agency_roles` + `agency_role_holders`; an unstaffed blueprint is refused (`RES_HANDOFF_ROLE_UNSTAFFED`). | ADR-0035/D3: the platform knows who holds what. Handing a guest to a role nobody holds is the failure that decision exists to prevent — and a guest who gets no answer is how you find out |

**The routing table is a reviewed constant, not a setting.** Changing where a trigger goes is a
policy act (the same rule `exception_engine.FAILURE_ROUTES` carries): reservation triggers go to
the reservation manager, the material ones (`LEGAL_THREAT`, `SAFETY_OR_EMERGENCY`,
`HIGH_VALUE_NEGOTIATION`) to the GM, money-after-hold to Finance, privacy/security to the
Technology–Security Manager. The owner may move an entry; nobody moves one by accident.

## The owner decision this creates

`security.ai.escalation_min_confidence` ships **declared but unpublished**. That is deliberate and
it is the *only* way this rule is honest: the number is a business decision ("how sure is sure
enough before the AI answers a rate question on its own"), and it belongs in the catalogue with a
source document and an approver, not in a constant in a merge request. Until it is published, the
language path escalates every time — which is the safe direction, and it makes the missing decision
visible instead of invisible.

## Consequences

- **An escalation is a fact with a shape.** Trigger, route, actor, reason, package digest and — for
  three of them — an incident, all in one append-only ladder.
- **The tenant's own records decide who can be handed a guest.** Staffing a role in the tenant's
  roles screen is what makes a handoff possible; disabling the holder closes it again, with no
  deploy.
- **§13's "only if explicitly safe" is a person, not a heuristic.** The platform has no rule that
  decides the AI is safe again; a human says so, and the reason is recorded.
