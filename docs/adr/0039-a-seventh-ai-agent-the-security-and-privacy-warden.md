# ADR-0039 — A seventh AI agent: the Security & Privacy Warden

- Status: Accepted
- Date: 2026-10-02 (owner decision) · written 2026-10-02
- Deciders: owner
- Amends: ADR-0035 (the AI organisation becomes seven components), ADR-0018 (a new AI principal)
- Spec: Master Spec §18, §19, §49, §56; AI Workforce Architecture V1.1 §2, §11, §21

## Context

The owner defined a seventh AI component on 2026-10-02, in their own words:

> "AI Agent Divisi Security & Privacy — tugasnya melakukan perlindungan sistem dari
> malware/virus dan kerusakan lainnya dari luas sistem, menjaga sistem agar terbebas dari bug
> & error, juga membuat sistem selalu terjaga terstruktur baik dan rapi, dan juga melakukan
> perbaikan signifikan secara otonom otomatis jika ada virus, malware, bug & error, menjaga
> data sensitif agar tidak dicuri oleh pihak luar, melaporkan ke GM/CEO & Owner jika ada
> kegawatan virus, malware, error & bug besar."

Four duties: **protect** (malware, damage), **keep clean** (no bug/error, structure and order),
**repair autonomously** (significant fixes on detection), **shield data**, and **escalate**
(report a serious emergency to the GM and the Owner).

The first, second and fourth are unremarkable. The third — *autonomous significant repair* — is
the one that has to be written carefully, because this agent would be repairing the same system
that constrains it. Every control in this platform (the deny registry, the approval engine, the
policy store, the audit chain) is *code*, and code can be edited. The question this ADR answers
is not "should the warden act?" — the owner said yes — but **which acts must never be the
warden's alone**, on the principle the platform already lives by: *an actor may not remove the
control that bounds it, and may not approve its own work.*

## Decision

**The warden diagnoses and repairs autonomously; it never changes the controls that bound it,
holds a raw secret, or approves its own change.**

### May do alone (bounded, reversible, each one recorded)

| Act | Why it is safe without asking first |
|---|---|
| Scan, probe, and integrity-check the tree, the database, the audit chain and the anchors | Reading. A read is never a risk |
| Detect and classify: malware, corrupted file, failing test, drifted schema, anomalous access | Produces a finding, not a change |
| **Isolate** a suspicious process, session, or outbound connection | Reversible, denies rather than grants; the fail-closed direction |
| **Rotate** a session/user credential it believes is compromised | Denies existing access; the old path stops working |
| Restore the service to a **known-good snapshot** after an incident | Returns to a state already proven good |
| Open an incident, attach evidence, page the GM/Owner on a serious one | Communication with a record, never a silent act |
| Draft the fix: patch, test, and a full report with the root cause | The change is proposed and proven, not applied to production |

### Needs a human before it happens (never the warden alone)

| Act | Who must approve | Why |
|---|---|---|
| Deploying code to production, including the warden's own repair | GM/CEO (per the normal deploy gate: tests green + preflight) | The owner's "autonomous significant repair" is honoured as *autonomous diagnosis and verified fix*, deployed with a human's yes. A production change nobody can reverse is not repair, it is risk |
| Changing a **control**: deny registry, approval thresholds, MFA role set, permission/role definitions, AI allowance | **Owner** (these are owner policy values, §16) | The warden's own rails live here; self-service would be the fix that removes the fence |
| Deleting or rewriting data (including after the 7-year retention window, D9) | GM/CEO **and** Owner | Already decided in D9; the warden executes, humans authorise |
| Reading a raw secret/key (`.env`, signing key, `key.read_raw`) | Never — for any AI | Already in the deny registry (v2): `secret.read`, `key.read_raw`, `passport.read_raw` |
| Approving anything | Never — for any AI | §11's absolute deny; the warden's report is evidence for a human approver, not an approval |
| Its own graduation from shadow mode | GM/CEO, by measurement (ADR-0038) | The agent that watches everything must not also certify itself |

**The plain-language rule, which is the whole ADR:** *the warden may fix the house; it may not
change the locks — and it may not sign off on its own work.*

### Escalation (the owner's fourth duty, made concrete)

| Severity | What it means | Who is told | How fast |
|---|---|---|---|
| **S1** | Sensitive data exposed or exfiltrated; signing key or credential leak; audit chain broken | GM/CEO **and** Owner, plus a written incident report | immediately, before any repair |
| **S2** | Malware or unauthorised access contained; service degraded | GM/CEO | within 1 hour |
| **S3** | Bug or error with guest impact but no data exposure | GM/CEO (report), Owner in the daily summary | same day |
| **S4** | Hygiene: drift, lint finding, structure issue | recorded in the daily summary | weekly |

## Consequences

- **A new AI principal** (`ai_security_warden`) joins the identity model (ADR-0018), with the
  same deny registry, the same audit trail, and no special tier that bypasses either.
- **Guardrail entries are published, not assumed.** The owner approved the four additions on
  2026-10-02, and `security.ai.denied_actions` **v3** (27 entries, effective 2026-10-02) now
  refuses them: `policy.publish`, `security.controls.change`, `ai.allowance.change`,
  `deploy.production`. The registry they protect is the one that holds them — the warden cannot
  reach it, and the earlier revision keeps answering for decisions dated before that day (the
  suite proves both: 27 entries today, 23 before, and neither drifts). They name *capabilities*
  rather than registered tools, so the file that guards the registry records them as the owner's
  extensions and notes the drift rule: when a tool for one of them is ever registered in
  `ai_tools`, the entry must name the **tool**, because enforcement compares tool names.
- **The warden is the sixth and seventh component's problem, in one order:** build the outbox
  first (§14: agents talk in typed, recorded letters), then the warden — because an agent that
  reports incidents needs a channel that cannot lose a report, and that channel does not exist
  yet.
- **Structure is a feature, not a preference.** "Terstruktur baik dan rapi" is checkable here:
  lint, schema parity with the PostgreSQL twin, ADR status readability, and the pre-deploy gate
  are the warden's daily hygiene report — the numbers already exist, so the warden reports them
  instead of inventing a new standard.
