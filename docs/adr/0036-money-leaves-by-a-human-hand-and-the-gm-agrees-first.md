# ADR-0036 — Money leaves by a human hand, the account changes by a human hand, and the GM agrees first

- Status: Accepted
- Date: 2026-09-28 (owner decision) · written 2026-10-01
- Deciders: owner
- Amends: ADR-0035 (the approval ladder gains a GM tier), ADR-0033 (the money flow gains an explicit executor step)
- Spec: Master Spec §13, §24, Appendix A; AI Workforce Architecture V1.1 §2A.3, §11, §16

## Context

The owner added three rules on 2026-09-28:

1. **Real money leaving must be transferred by a human** — the AI Agent may not do it. The AI
   provides the report of what must be paid and its details, nothing more.
2. **Changing a destination account number must be done by a human** — the AI Agent may not.
3. **Every activity above requires the General Manager / CEO's approval first, in tiers.**

Two of the three turn out to be **already enforced**. The third is new, and it changes the
approval ladder rather than the deny list.

## What is already enforced — verified, not promised

`security.ai.denied_actions` (published value, version 2) already carries 23 entries, among them:

```
payment.approve   payment.execute   refund.approve   refund.execute
bank.change       bank.approve      crypto.transfer  treasury.control
blockchain.sign   withdrawal.approve                  key.read_raw
```

So **rules 1 and 2 need no new code.** An AI identity in this platform cannot execute a payment,
cannot approve one, cannot change a bank or wallet destination, and cannot sign a chain
transaction. The owner's rules make explicit what the deny list already says — which is the right
way round: the control existed before the instruction.

## What is new — rule 3

The GM tier does not exist yet, and this is what has to change:

| Where | Today | Needed |
|---|---|---|
| `approvals.ROLE_RANK` | `agent 1, manager 2, accountant 3, owner 4` | a rank for `gm`, above `finance_manager` |
| `finance.approval.payment_thresholds_minor` | v1 names `["finance","manager"]` | v2 names the ladder `["finance_manager","gm"]` |
| `finance.approval.dual_control_min_approvals` | `2` — already requires two approvers on `refund`, `payout`, `bank_change` | unchanged; the ladder satisfies it |

Publishing the policy value is part of this ADR. Changing `ROLE_RANK` is **not** — it changes
approval behaviour, so it belongs to the RBAC slice with the per-role matrix test.

## The ladder

| Step | Who | What it produces |
|---|---|---|
| 1 | **AI** (Finance Admin AI, component #5, when built) | the report: what must be paid, to whom, how much, against which contract or invoice |
| 2 | **Ms. FAM** — Finance & Accounting Manager | tier-1 approval |
| 3 | **Mr. CEO** — General Manager | tier-2 approval |
| 4 | **a human** — Ms. FAM | the transfer actually executed, by hand |

## The question rule 1 opens, and its answer

Rule 1 says a human executes the transfer. But `finance_staff` was deleted (ADR-0035), so the
obvious question is: **who is the "maker" now?**

The answer is that the **human who executes is the maker** for the purposes of the control — and
the control still holds, for a reason that is verifiable in code rather than assumed:

> **The approval is bound to the request fingerprint.** `src/approvals.py:1020` refuses a
> replayed approval whose request fingerprint has moved:
> `if self._row_fingerprint(replayed) != fingerprint:` …
> So §16's invariant *"Approval becomes invalid when its critical target/fingerprint changes"* is
> enforced, not merely written down. Change one digit of the amount, or one digit of the account
> number, and the approval **dies** and must be granted again.

That is what makes "Ms. FAM approves and then executes" acceptable here: she cannot execute
something the GM approved at a *different* amount, because the fingerprint would no longer match.
The authority to release money stays with the GM; the hand that moves it is hers; and the request
cannot drift between the two.

## The cost of rule 3, named rather than hidden

The GM now sits in **every** money-out path. `finance.approval.expiry_minutes_by_risk` gives a
HIGH-risk request a **240-minute** window, so a GM who is asleep, flying, or in a meeting
**stalls payments** — and an expired request must be raised again from the start.

That is a deliberate trade of throughput for control, and it is the owner's to make. The standard
mitigation, if it ever bites in practice, is a **threshold**: routine villa payouts below an
agreed amount on the Finance Manager's approval alone, with the GM tier above it. **Not applied
here** — recorded so the option stays visible instead of being rediscovered in a crisis.

## Consequences

- **Two of the three rules required no code**; the platform already refuses AI execution, AI
  approval and AI bank changes.
- **The RBAC slice must add `gm`** to the role vocabulary and the approval ladder, and its
  per-role matrix test must assert that **no AI identity can ever appear as an approver** on a
  money-out request.
- **The executor step is now explicit** in the money flow: it ends with a human hand, not a
  button the platform presses.
