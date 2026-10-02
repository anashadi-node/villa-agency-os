# ADR-0041 — A failure is routed, counted, and reported: the exception engine

- Status: Accepted
- Date: 2026-10-02 · written 2026-10-02
- Deciders: owner (the escalation ladder and the playbooks are the owner's; the routing is the platform's)
- Amends: ADR-0007 (the outbox gains its first real producer), ADR-0039 (the warden's reports have a destination)
- Spec: Master Spec §17, §19; `docs/RETENTION_AND_INCIDENT_RESPONSE.md` §2 (playbooks P1–P6, severities S1–S4)

## Context

The repository refuses loudly *before* harm: policy gates, deny registries, approval ladders.
What was missing is the other half — what happens **after** something went wrong. A job throws,
a provider refuses, a detector finds a hole. The obligations that follow (who is told, how
fast, what the regulator's clock is) already existed in `incidents.py`; what did not exist was
the piece that turns a failure into those obligations **without a human deciding each time,
and without a hundred incidents for one repeating bug.**

## Decision

**Three rules, and each one closes a specific way this goes wrong in practice.**

| Rule | Why it is the rule |
|---|---|
| **Severity is looked up, never guessed.** The caller brings a *category*; the platform maps it to a playbook and a severity. An unknown category is refused. | "We opened it as a warning because nothing else fit" is exactly how a personal-data breach ends up with a 24-hour response time. A failure nobody classified is a decision, not a default |
| **A repeating failure is one incident with a count.** The fingerprint of (agency, source, category, code, aggregate) is stored with a **rolling** window measured from the last arrival. | The obligation is the *failure*, not each arrival. A loop failing 400 times a minute must not wake the GM 400 times; and a failure that returns after a quiet spell *is* news, so the window rolls forward rather than counting from the first sighting |
| **The report commits with the fact.** Capturing writes an incident *and* an outbox event (ADR-0007) in one story. | A notification that "may have fired" is not evidence. The engine **writes**; a publisher sends. The report survives a crash because it is a row, not a callback |

**Personal data can be raised by a caller but never lowered.** A route that says personal data
is involved cannot be talked out of it by a hurried caller (`personal_data_involved=False` is
ignored where the route says otherwise), and `SEV1`/`SEV2` start the 72-hour regulator clock in
`incidents.py` regardless. Fail-closed on the side that matters.

## The categories, and what they are not

Seven categories route to the six playbooks that already exist: `DATA_EXPOSURE`,
`BOOKING_CONFLICT`, `SAFETY_EMERGENCY`, `PAYMENT_UNRECONCILED`, `VENUE_UNAVAILABLE`,
`PROVIDER_OUTAGE`, `JOB_FAILURE`. **No playbook is invented here.** The Security & Privacy
Warden (ADR-0039) will need its own playbook — a malware finding is not a data breach — and a
category is deliberately *not* added for it yet: a route pointing at the wrong playbook is worse
than no route, because it looks handled. Until that playbook exists, a security finding is
routed to `DATA_EXPOSURE` only when personal data is actually involved, which is a true
statement, rather than being filed under a heading that means something else.

## Consequences

- **A failure's whole life is answerable.** "How often did this happen, was it one incident or
  thirty, when did the regulator clock start" is a query, not a memory: `exception_fingerprints`
  holds the count and the incident id, `incidents.py` holds the playbook and the append-only
  step history, and `outbox_events` holds every report with its correlation.
- **The engine is a writer, not a sender.** No socket, no page, no retry: sending is the
  outbox publisher's job. That is what keeps this module testable and what keeps a tenant's
  transport (ADR-0037) outside the platform's control flow.
- **A new failure category is a policy act.** Adding one means naming the playbook and the
  severity, in a document, with a reason — not choosing the closest match in a hurry.
