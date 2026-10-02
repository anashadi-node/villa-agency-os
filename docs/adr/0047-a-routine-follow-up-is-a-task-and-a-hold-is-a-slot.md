# ADR-0047 — A routine follow-up is a task, and a hold is a slot

- Status: Accepted
- Date: 2026-10-02 · written 2026-10-02
- Deciders: platform (what each worker may do, and where the work lands). The MEDIUM allowance
  below is the owner's to publish, and until they do RES-W07 refuses like RES-W03 and RES-W05.
- Related: ADR-0045 (the three steps), ADR-0046 (pricing is an answer), ADR-0035 (the §9 employee),
  ADR-0018 (AI identity and the tool envelope)

## Context

§9's job list does not stop at the offer. The next two jobs are **Conversation & Follow-up**
(RES-W06) and **Tentative/Full Hold** (RES-W07), and §56 names exactly one capability for each:
`followup.routine` (LOW) and `hold.request_tentative` (MEDIUM, owner allow-listed). Everything
else — who does what, and where the work lands — is this platform's decision.

The first job looked like it should be a message, and the estate already says why it cannot be.
`messages.MessageService.draft` requires a **human** author: `_require_human` refuses any actor
whose role is not on the human ladder (`ai_service` is deliberately not), with the reason spelled
out — *"approval and sending are human acts"*. So an AI that "follows up" by writing text to the
guest would be writing under a person's name, and the platform would refuse it at run time.

The second job took a real capability out of the estate's hands: a tentative hold removes a unit
from sale. Until it expires, every other guest's search must not offer that unit, and the
supplier's confirmation is the authority for the hold (§6's matrix, §7's lifecycle).

## Decision

**RES-W06 files an internal task and contacts nobody.** `followup.routine` is LOW because it
never leaves the building: the worker delegates to `ops_tasks` (`kind = GUEST_FOLLOWUP`) and the
follow-up lands on the agency's own queue, where a person picks it up. The architecture says the
same thing in the autonomy list — *"internal task/reminder, normal follow-up"*.

Three properties are the caller's, and the worker refuses to invent any of them:

1. **The slot.** `slot` names the piece of work the follow-up belongs to (the version the guest
   holds, the hold that is running out, `no-answer-yet`). The idempotency key derives from
   `(conversation, slot)`, never from the clock, so a retry after a restart files nothing and two
   workers waking to the same silence produce one task. The same slot with a **different** deadline
   is refused by the queue, not silently rescheduled: a deadline someone already agreed to is not
   the worker's to move.
2. **The owner.** `assignee` is required. The queue's first invariant is that every task has an
   owner, and choosing who chases a guest is a decision — not a default this module may fill in.
3. **The deadline.** No follow-up SLA is published anywhere in the platform yet. The worker takes
   `due_at` from its caller rather than inventing a policy number, and the queue normalises the
   instant to UTC (the caller's `+07:00` is the same moment).

**RES-W07 derives everything from records, and is gated twice.** The window comes from the
inquiry's confirmed stay (§6), the guest reference from the case row, and the policy version from
`matching.policy_for` — the same lookup the ranking and the price use, so a hold cannot be taken
under rules no other step agreed to. A stay no policy covers is refused (`RES_WORKER_HOLD_NO_POLICY`),
never held under an absent basis. The kernel re-checks the actor before it writes (`request_hold`
takes `actor`/`actor_role`/`principal_id` and calls `require_tool_authority`), so the audit carries
**two** rows for one act: the door's, which names the worker, and the owning module's own check.
That is deliberate — the door decides whether the AI may act at all; the kernel decides whether
*this* hold is legal.

**The hold's key also derives from the question** — `(conversation, unit, window)` — so a retry
after a timeout and a duplicate from a second worker are one hold, while the same unit on other
dates is a different request.

## Consequences

- **A refused follow-up writes nothing, on any table.** The guard, the door and the queue's
  preconditions all run before the first row, so "refused" and "never attempted" stay
  indistinguishable — and the test that pins it also asserts `message_drafts` remains empty:
  §56's LOW tier buys the AI a reminder on our desk, not a voice in the guest's inbox.
- **The kill switch stays precise.** `followup.routine` is WRITE, so a §17 mode that neither
  writes nor acts files no task; a held thread (§13) files none either, because the guard runs
  before anything else. Nothing in this ADR widens the AI's reach.
- **The owner's list grows by one entry.** `security.ai.medium_tool_allowance` must now name
  `availability.request` (RES-W03), `quotation.send` (RES-W05) and `hold.request_tentative`
  (RES-W07) before those three workers can do anything at all. Unpublished still means *nothing is
  allowed*.
- **Honest limits, carried forward.** The hold is not yet attached to the conversation: §9's
  conversation minimum lists `quote_reference` and `hold_reference`, `ReservationService.attach_reference`
  exists, and binding a created record to the thread is the next increment — it is not a worker
  step, because attaching is not an act §56 names. The worker also does not advance the case to
  `HOLD_PENDING`: that is a `cases` transition, and the orchestration that sequences it (hold →
  case status → conversation reference) belongs to a later job, not to a worker that must stay
  inside its three steps.
- **What is still unanswered is *when* to chase.** The worker can file a follow-up the moment it
  is told the slot; deciding that an inquiry has gone quiet long enough — and how often it may be
  chased — is a policy question. Until the platform publishes that policy, the caller names the
  slot and the deadline, and the AI asks nobody twice for the same slot.
