# ADR-0038 — An AI agent graduates from shadow mode by measurement, never by mood

- Status: Accepted
- Date: 2026-10-02 (owner decision) · written 2026-10-02
- Deciders: owner
- Spec: Master Spec §3, §17, §20.1; AI Workforce Architecture V1.1 §11, §16

## Context

D7 asked the owner for the number at which an AI agent may stop being a *shadow*: running
for real, watched by a human, but **forbidden to send, execute, or commit anything**. The
owner approved the proposed bar on 2026-10-02.

Shadow mode is not a delay, it is the only honest way to answer "does this agent make the
same decision a person would?" without paying for the mistakes. What was missing was the
number — and a number nobody wrote down becomes "it looked fine to me", which is how an
agent with a plausible tone gets permission it never earned.

## Decision

**One bar, measured on real cases, and the switch is a human act.**

| Measurement | Bar | Why this one |
|---|---|---|
| **Agreement rate** — the agent's decision matches the human's | **≥ 95%** | Below this, one guest in twenty is handled differently than the business intended; the human reviewer becomes a rubber stamp instead of a reviewer |
| **Unsupported claims** — sentences with no record behind them | **exactly 0** | One invented fact to a guest is a refund, a complaint, or a legal problem. It is not a rate; it is a hard zero |
| **Volume** — shadow cases observed | **≥ 100** | 20 cases cannot show a 5% disagreement rate; the sample has to be big enough for the bar to mean something |
| **Window** | rolling, per agent **and** per worker (`*-Wxx`), not per platform | A platform-wide average hides the one worker that keeps quoting the wrong rate |

**The ranking is deliberate:** unsupported claims are not traded against agreement. An agent
with 99% agreement and one fabricated claim has not met the bar.

## Who flips the switch

- The **measurement is recorded** by the platform: volume, agreement, unsupported count, the
  policy revision and the code revision that produced them.
- The **decision is human**: GM/CEO approves graduation, and the approval is an approval
  record (fingerprint-bound, ADR-0036's mechanism), not a setting someone toggles quietly.
- **A graduation is reversible by rule, not by argument:** if live agreement falls below
  **90% over a rolling 50 actions**, or any unsupported claim reaches a guest, the agent
  returns to shadow mode automatically and the incident is reported to GM/CEO and Owner.

## Consequences

- **No agent is "mostly ready".** The bar is met or it is not; there is no partial live mode
  and no per-sentence exception, because the failure this prevents is exactly one sentence.
- **The bar is machine-readable.** The numbers live as platform defaults (like the SLA
  ceiling, ADR-0037's pattern) so a change has to be a deliberate change with its reason,
  not a quiet edit. A tenant may set a *stricter* bar, never a looser one.
- **Graduation is not the same event as deployment.** Deploying an agent's code follows the
  normal gate; graduating an agent's authority is a second, separate human approval. One
  process cannot silently grant the other.
