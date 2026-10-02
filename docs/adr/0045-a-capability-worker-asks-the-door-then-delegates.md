# ADR-0045 — A capability worker guards the thread, asks the door, then delegates

- Status: Accepted
- Date: 2026-10-02 · written 2026-10-02
- Deciders: platform (the order and the delegation rule). The MEDIUM allowance below is the
  owner's to publish, and until they do the affected worker refuses.
- Related: ADR-0035 (the §9 employee), ADR-0038 (shadow mode), ADR-0043 (escalation silences the
  AI), ADR-0044 (the escalation vocabulary)

## Context

§9 describes an employee with thirteen jobs; §7 gives each job tools; the platform already has a
door (`ai_gateway`), an identity layer (`ai_service`) and the domain modules that own the rules
(`intake`, `matching`, `reservation_kernel`, …). What did not exist was the part in between: the
code that *acts* — the worker that reads a guest's message into a proposal, ranks the inventory,
or asks a supplier whether a unit is free.

That code is where an AI employee becomes dangerous in three specific, boring ways:

1. **It re-implements a rule that already has one.** A worker that files its own proposal row can
   skip review; one that ranks units itself can ignore a confirmed fact; one that writes its own
   availability row can bypass overlap and idempotency. None of these are attacks — they are
   ordinary shortcuts taken by code that has access to a connection.
2. **It acts after the door.** If the audit row is written once the work is done, the audit is a
   log of the past, not a control: a tool the principal's §17 mode does not reach would still
   have run, and the only trace would be a row saying it did.
3. **It keeps talking after a human took over.** §13's ladder ends the AI's turn; a worker that
   checks only "may I write?" still reads, searches and asks inside a conversation a person now
   owns.

## Decision

**Every capability worker runs the same three steps, in this order, and nothing else:**

1. **Guard the thread.** `ReservationService.require_thread(conversation_id)` — the conversation
   must be at `AI_RUNNING`, and this holds for *reads as well as writes*. A thread a human owns
   is not the AI's to search, ask about or answer; a conversation it was resumed into is not
   either until `resume()` moves it back to `AI_RUNNING`.
2. **Ask the door.** `AiGateway.request(principal, tool, intent=…, correlation_id=…)` — which
   consults the identity layer, applies the §17 mode ceiling and the tool's effect class, and
   records the attempt. **The door is asked before the domain module is called, always.**
3. **Delegate.** The module that owns the rule performs the work: `intake.propose` for proposals,
   `matching.match` for ranking, `reservation_kernel.request_availability_check` for asking.

**A worker owns no table and no rule of its own.** It returns what the domain module returned —
no wrapper, no re-shaping — so there is exactly one answer to "what did this action produce?".

**`intent` carries an operational sentence written by us, never guest text.** The gateway records
it and never consults it (§56: conversation text cannot raise privilege), so passing a guest's
words into it would put guest text into the audit chain — a §8 retention surface — in exchange
for nothing.

**A refused attempt leaves nothing behind.** The guard, the door and the module's own
preconditions all run before the first row is written, so "refused" and "never attempted" are
indistinguishable — the same promise `matching` and `reservation_kernel` already make.

**The MEDIUM allowance is a publication, not a ship-with default.** `availability.request` is
MEDIUM, and §56 requires a published `security.ai.medium_tool_allowance` before a machine actor
may hold it. The key ships with no value: an unpublished allowance means *nothing is allowed*,
not *everything is*. The platform therefore ships a reservation employee that can record and
rank, and that cannot ask a supplier anything until the owner publishes the list.

## Consequences

- The order is testable: a worker refused by the door has written no row, on any table.
- An operator reading the audit sees the worker code (`RES-W01`) and the tool, so "which part of
  the AI workforce did this?" is answered by grep.
- §13 is enforced once, by the guard every worker opens with — a worker cannot forget it without
  failing a test, because the tests drive real conversations through the ladder.
- Two things must be done by a human before the reservation employee is fully useful: publish
  `security.ai.medium_tool_allowance` (which MEDIUM tools the AI may hold) and
  `security.ai.escalation_min_confidence` (when a language-derived judgement must escalate).
  Both are owner acts; the platform refuses rather than defaulting.
- A worker that needs a rule the platform does not have yet must add it to the owning module —
  not to itself. That is the cost of the rule, and it is the point: the rule lands where it is
  reviewed and tested with the rest of that module's rules.
