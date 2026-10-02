# ADR-0043 — Escalation silences the AI *before* the human arrives

- Status: Accepted
- Date: 2026-10-02 · written 2026-10-02
- Deciders: owner (that the ladder has eight rungs is the spec's; that the middle rungs are silent is the platform's reading)
- Amends: ADR-0038 (a widening of authority is a human act), ADR-0040 (security, safety and privacy come first)
- Spec: Master Spec §13 (Human Handoff State Machine), §9 (Mandatory Escalation, "must not claim a requested hold is confirmed"); `docs/AI_WORKFORCE_TRACEABILITY.md` §13

## Context

§13 draws eight states and one sentence that decides how to read the whole ladder:

> Once a human takes control of a customer conversation/case, conflicting AI outbound messages
> must stop.

The narrow reading says the AI goes quiet at `HUMAN_ASSIGNED` — the moment named in the sentence.
The problem is what happens *between* the trigger firing and a person actually being there. In a
villa agency that gap is minutes to hours: the GM is asleep, the reservation manager is on another
call, the trigger is a legal threat at 02:00 local time. During that gap the AI is still
`AI_RUNNING` under the narrow reading, and the most damaging messages are exactly the ones it
would send then — "your hold is confirmed", "yes, we can do that rate", "I have cancelled it".
Every one of those is a commitment made *because someone else was about to decide*.

Two more pieces point the same way. §9's escalation list is *mandatory*: the trigger firing is a
decision the service has already made, not a piece of context for a later decision. And ADR-0040
puts safety above throughput: the failure mode of being quiet for two hours is a guest who waits;
the failure mode of talking is a commitment the agency did not make.

## Decision

**Only `AI_RUNNING` lets the AI write. Every other rung refuses.**

| Rule | Why it is the rule |
|---|---|
| **The write rule is one predicate over the eight states** (`writes_allowed = state == "AI_RUNNING"`), and every worker opens with `require_write`. | A rule that each worker re-derives is a rule the ninth worker will get wrong. One predicate, one place to read it, one place to test it |
| **Escalation is the moment the AI stops**, not the moment a human appears. `ESCALATION_TRIGGERED` is already silent. | The gap between the trigger and the person is when an unguarded message does the most damage, and it is the one window a narrow reading leaves open |
| **The rungs are walked in order and cannot be skipped.** `LADDER` is a closed table of `kind → (from, to)`; a rung whose predecessor is not the current state is refused (`RES_RUNG_REFUSED`) and writes nothing. | "Jump to HUMAN_HANDLING" is how a guest ends up unwatched. A rejected rung must leave no event and no audit row, so a refused move is not a half-move |
| **`take_control` records three rungs in one transaction.** `HUMAN_TAKEN` → `WRITES_SUSPENDED` → `HANDLING_STARTED`. | They are three facts (somebody owns it; the AI has stopped; the person is working it) and one gesture. Recording them together is honest about what a human actually did; recording them *skippably* would not be |
| **Widening authority is a human act.** `allow_resume` refuses a machine principal; `resume` only accepts the principal the conversation belongs to. | ADR-0038's rule — the switch is a human act — applies to widening exactly as it does to graduating an agent out of shadow mode. And two identities must never put words in one guest's thread |
| **Resolution cannot be refused silently.** The package's pending actions say so at `RESOLUTION`: deciding nothing keeps the AI silent. | A human who forgets to allow a resume has left a guest with no assistant; that is a state the platform should name, not hide |

## Consequences

- **The safe failure is silence, and it is visible.** An escalated conversation that nobody picks
  up does not produce wrong commitments; it produces an incident the SLA clocks and the outbox
  already track (`ai.conversation.escalated`, then `ai.conversation.handoff_requested`).
- **The AI's quiet is unambiguous.** There is no "the AI may still send internal notes" carve-out,
  because a second write path is a second thing to reason about; if the platform later wants
  drafting during a handoff, that is a new rung with its own test, not a quiet exception here.
- **`RESUME_ALLOWED` and `RESUMED` are two rungs, not one.** The human says "the AI may take this
  back"; the AI then takes it back as the principal that owns it. Merging them would let the
  decision and the act happen in one machine call, which is precisely what §13's last rung avoids.
- **The audit answers "why did the AI stop talking".** Every rung carries its actor, its reason
  and — for `HANDOFF_REQUESTED` — the digest of the brief the human was handed.
