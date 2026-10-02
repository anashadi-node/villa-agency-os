# ADR-0051 — The pre-arrival answer assembles what a person sends, and the prep task stays §26's job

- Status: Accepted
- Date: 2026-10-02
- Step: §9's tenth capability worker (RES-W10, pre-arrival communication), after ADR-0048/0049/0050

## Context

§9's tenth step is pre-arrival communication. §28 places it in the guest journey after
`BOOKING_CONFIRMED` (… → PRE-ARRIVAL → VILLA RECONFIRMATION → READINESS → ARRIVAL → CHECK-IN …)
and settles who owns what: "Agency owns guest journey; Villa Management owns on-property execution
according to contract. Requested special service ≠ confirmed service. Villa READY memerlukan
supplier/authorized evidence, bukan AI assumption."

§56 gives the AI exactly one arrival capability, `arrival.inform_verified`, tiered LOW. This
estate's gateway classes it **WRITE** — "tells the guest something: a message leaves the system" —
and the catalogue is closed, so the worker below holds that one tool and nothing else.

Two facts about the estate shape the decision:

1. **A machine may not write to a guest.** `messages.MessageService.draft` requires a human author
   (the AI principal is absent from `roles.ROLE_RANK`), so "the AI informed the guest" cannot mean
   "the AI wrote the message".
2. **The arrival window is tenant data.** `checkin_time` and `checkout_time` are allowed keys of the
   policy payload (§11's pack), written by the tenant — and until this ADR nothing in the codebase
   read them.

## Decision

**1. RES-W10 answers; it does not send.** `ReservationWorkers.arrival_info()` assembles the
verified pre-arrival information for the thread's confirmed booking and returns it as data. What
actually reaches a guest is a message, and a message needs a person — the same reasoning that makes
RES-W06's follow-up an internal task rather than a draft under someone's name.

**2. The window comes from the policy version the *booking* names.** The answer reads
`bookings.policy_version_id` — the version the stay was priced, accepted and confirmed under — and
not `matching.policy_for`, which resolves the version in force *today*. This is RES-W09's lesson
(ADR-0049) applied to a second fact: a guest-facing statement belongs to the version that governed
the stay, else a tenant's later edit silently rewrites what a guest was told.

**3. An unfilled window is absent, never defaulted.** `checkin_time`/`checkout_time` carry no format
validation in `policy_engine` (they are tenant free text), so the answer relays them verbatim and
marks their provenance; when the tenant has authored neither, the answer says *no recorded arrival
window* and `None`, rather than inventing `15:00`.

**4. Pre-arrival facts this schema cannot hold are named.** `ARRIVAL_NOT_STORED` (address,
directions, access instructions, contact) is reported in the answer, because there is no column for
them and a caller must not read silence as "checked". Filling them is the tenant's own data entry —
this platform sells the blueprint and the rules, not the tenant's content (ADR-0037).

**5. The refusal boundaries are §28's order made checkable.** A case with no booking has no arrival
to describe (`MissingRequirements`, `missing=["booking_id"]`); a booking whose status is not in
`booking_states.OCCUPYING_STATES` (e.g. `CANCELLED`) has no arrival ahead of it. Both refuse before
the door and write nothing.

**6. The check-in prep task is §26's background job, not an AI capability.** §26 lists "pre-arrival
tasks" among scheduled background jobs, and §56 contains no tool for filing them, so RES-W10 files
nothing. The queue is already prepared for that job — `ops_tasks.KIND_CHECKIN_PREP` exists with the
subject rule `("booking_id",)`, i.e. the job must name the booking it prepares. **Recorded gap:** that
job is not implemented; when it is, it writes under its own authority (a platform job), not under a
§56 tool.

## Consequences

**Positive.** §56's arrival capability has its first reader, and the two payload keys the template
always allowed become readable facts (the fourth "catalogue name with no reader" closed this way).
The answer cannot claim readiness: §28's "READY needs the villa's evidence" is carried in the
answer's own note, and `not_recorded` makes missing tenant data visible instead of implied. The
worker writes nothing — a test counts every table it could have touched.

**Negative.** Because §56 (and this estate's gateway) classes informing a guest as an outbound
WRITE, `arrival_info` is refused under §17 READ_ONLY even though it changes no row: the act it
serves is outbound, so switching outbound acts off must switch it off. That is deliberate and tested.

**Ticket.** Arrival address/directions/access instructions have no home yet; they belong in tenant
settings (platform ships the fields, the tenant fills them). Until then the answer names them as
not recorded, which is the honest reading of the schema rather than a gap in the answer.

## Tests

`tests/test_reservation_workers.py` (claim 14, 54 → 59): the recorded-facts answer, the absent
window, no-booking and cancelled-booking refusals, and the READ_ONLY refusal with no door row. The
tools pin gains `(RES-W10, arrival.inform_verified)`.
