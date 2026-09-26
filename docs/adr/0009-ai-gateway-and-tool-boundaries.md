# ADR-0009 — AI Gateway and Tool Boundaries

- Status: **PROPOSED — pending owner review**
- Date: 2026-09-22

## Context

Master Spec says AI is copilot/workflow assistant, not source of truth and not financial authority. Existing repo already has safe building blocks: untrusted inbound handling, grounded message drafts, approval queue, privacy gates, and deterministic matching.

## Decision

Build AI Gateway as a controlled wrapper around narrow safe tools. Do not let AI directly access SQL/database, payment execution, bank mutation, audit modification, secrets, or raw passport/ID.

## AI request flow

```text
USER
→ AUTHORIZATION
→ AI POLICY GATEWAY
→ MINIMUM CONTEXT BUILDER
→ REDACTION
→ SAFE TOOL ALLOWLIST
→ LLM
→ STRUCTURED OUTPUT / PROPOSAL
→ DOMAIN VALIDATION
→ HUMAN APPROVAL IF REQUIRED
→ DOMAIN COMMAND
```

## Explicit deny registry

- `payment.approve`
- `payment.execute`
- `refund.approve`
- `refund.execute`
- `bank.change`
- `bank.approve`
- `discount.approve`
- `margin_override.approve`
- `contract.activate`
- `rate.verify`
- `rate.activate`
- `permission.change`
- `audit.modify/delete`
- `secret.read`
- `passport.read_raw` by default

## Reusable current assets

- `connectors.py`: prompt-injection/untrusted content boundary.
- `messages.py`: grounded drafts + approval.
- `gates.py`: privacy/classification redaction.
- `approvals.py`: human decision controls.
- `matching.py`: explainable matching.
- `intake.py`: rule-first extraction/proposals.

## Tests required

- AI cannot approve or execute forbidden action at tool/API level.
- Prompt injection is treated as data and audited.
- AI context is minimized and redacted.
- AI output cannot mutate domain state without domain validation and required human approval.

---

## Amendment — 2026-09-26: autonomy tiers, knowledge layer, extended deny registry (v1.1 §56, §57, §60, §21)

v1.1 turns the AI boundary from "a gateway with tools" into a **risk-tiered reservation agent**.
Recorded decisions:

1. **Three autonomy tiers** (the tool allowlist is the enforcement, not the prompt):

   | Tier | Examples | Rule |
   |---|---|---|
   | Low-risk, verified, policy-permitted | inquiry intake, clarification, deterministic villa search, verified villa Q&A, suitable alternatives, routine follow-up, booking-status information, verified pre-arrival info | may act autonomously |
   | Medium-risk | availability requests, tentative-hold requests, quotation sending, special-request coordination, selected supplier/client communications | policy-controlled explicit tools |
   | High-risk | financial approval/execution, bank changes, refunds, out-of-policy discounts/commercial overrides, contract/rate verification/activation, serious incident compensation, legal disputes | **human authority only** |

2. **Conversation instructions never increase privilege.** A user (or a document, or a web page)
   asserting new authority changes nothing: authorization comes from the identity, not the text.

3. **Human takeover preserves case context and pauses conflicting autonomous outbound actions** —
   a handoff that leaves queued autonomous messages running is a defect.

4. **Professional Reservation Knowledge Layer is advisory and versioned.** It covers etiquette,
   qualification, villa terminology, availability/holds/quotations/deposits, amendments,
   cancellations, arrivals, transfers, concierge, special requests, complaints, supplier
   communication, policy-compliant upsell. It **never** substitutes for verified villa facts,
   rates, policies, availability, settlement, or supplier confirmation. Unknown fact →
   clarify/verify, never fabricate.

5. **Extended deny registry** — in addition to the finance/secret list, these are denied and, more
   importantly, **not implemented as tools at all**: `blockchain.sign`,
   `crypto.transfer`, `withdrawal.approve`, `treasury.control`, `secret.read`,
   `key.read_raw`, `seed_phrase.read`, `passport.read_raw` (deny by default).

6. **Fallback**: the core system must keep functioning when the AI or its provider is
   unavailable (§1). AI outage must not block booking, payment recording, or approval.
