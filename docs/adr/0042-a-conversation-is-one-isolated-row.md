# ADR-0042 — A conversation is one isolated row, and it carries references rather than copies

- Status: Accepted
- Date: 2026-10-02 · written 2026-10-02
- Deciders: platform (isolation and provenance are not preferences); the tenant fills the settings (ADR-0037)
- Amends: ADR-0016 (the AI service identity and the single door) — the door gains its first caller in a workflow
- Spec: Master Spec §5 (workflow durability), §9 (Agent 3, Conversation Isolation Minimum), §15 (source of truth), §16 (only a reviewed field is a fact); `docs/AI_WORKFORCE_TRACEABILITY.md` §9/§2A

## Context

The AI Reservation Service is the first §9 employee to be built, and the first thing it needs is
not a prompt or a model: it is **somewhere for a conversation to live**. Three things in the
repository already say what that place has to be, and none of them had an implementation yet:

- §5 requires the workflow state to live outside worker memory. A worker that crashes mid-quote
  must not lose a guest's reservation, and §9 scales the service by *concurrent workers* — so two
  workers must be able to pick up two conversations without either one knowing the other exists.
- §9's *Conversation Isolation Minimum* names the context that may never be shared between
  guests: tenant, customer, inquiry, channel, reservation, state, requirements, candidates,
  quote reference, hold reference, payment state, consent, human owner, risk state.
- §15 says a fact has one owner. Requirements belong to intake, candidates to matching, prices to
  quotation, payment state to payments. §16 is sharper still: **only a reviewed field is a fact.**

The tempting shortcut is one wide table per conversation holding all fourteen fields. It would
duplicate five other modules' state, and the copies would drift the first time a price changed.

## Decision

**A conversation is one row in `ai_conversations`, and it holds identity, scope and references —
never another module's values.**

| Rule | Why it is the rule |
|---|---|
| **One conversation per (tenant, inquiry, channel).** A `UNIQUE` constraint, not a caller's check. | Two AI threads answering one guest is the §13 "conflicting AI outbound messages" failure. The schema refuses the second one; a rule that lives in a caller's `if` is a rule the next caller does not have |
| **The row holds references, not copies.** `quote_ref`, `hold_ref`, `reservation_ref` point at the owning module's record; requirements, candidates and payment state are resolved *by reference* when a handoff package is built. | A copy is a second answer to a question that already has one. A conversation holding its own copy of a price is a price that can be wrong in a way nobody can see |
| **The guest is `guest_ref`, never a name.** No column for a name, phone number, email or identity document exists. | §9 forbids raw identity documents in model context. The cheapest way to keep a promise about data is not to store the data |
| **A reference is written after the record exists.** `attach_reference` stores a pointer; the foreign key refuses a pointer to nothing. | A conversation that referenced a quote before the quote existed would force every reader to treat the link as a *maybe* |
| **The current rung is the newest event, not a column.** `ai_conversation_events` is append-only; `ai_conversations` keeps no `state` field. | The same reason `ai_principal_events` works this way: the log is what proves who moved the conversation, when and why. A mutable status column is a fact with no history |
| **The current rung is not stored twice.** The row's `updated_at` moves; its state is derived. | Two sources for one fact drift. Here the drift would be a guest handed to nobody, or an AI that thinks it is still in charge |

**Consent is part of the row and is a person's to write.** A machine principal cannot record
consent (`RES_HUMAN_REQUIRED`), and the field stays writable while the AI is suspended: a
withdrawal must be honourable the moment it arrives, even mid-handoff. Consent in this system
means the guest agreed to be contacted again — the AI may not manufacture it for itself.

## Consequences

- **Worker concurrency is now safe by construction.** Two workers on two conversations share no
  state; the same conversation is one row, and a second conversation on the same thread is
  refused by the database.
- **A handoff package cannot disagree with its sources.** It is assembled at read time from
  intake (reviewed facts only), matching (the newest run, with the candidate's own reasons),
  cases (the SLA clocks) and the conversation's own references — and it keeps a digest, so the
  audit proves exactly what the human was handed.
- **Message bodies are deliberately not copied into the package.** They live in `case_messages`
  behind the retention and content-class rules of §8; a package that duplicated them would be a
  second place those rules would have to be enforced, and the second place is the one that leaks.
  The package names the case; the human reads it under the access rules the case already has.
- **This is the first caller of the gateway.** The door (ADR-0016) existed with no workflow behind
  it. From here on, an AI act arrives at the domain through the door and is refused when the mode,
  the tier or the §56 allowance says no.
