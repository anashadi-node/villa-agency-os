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
