# ADR-0035 — The human organisation: seven holders, three deferred, two deleted for the AI maker, and a new Greeting Service

- Status: Accepted
- Date: 2026-09-28 (owner decision) · written 2026-10-01
- Deciders: owner
- Amends: ADR-0017 and ADR-0018 (both carried "the role vocabulary needs an owner decision" as an open item)
- Spec: Master Spec §18; AI Workforce Architecture V1.1 §2, §2A.1, §21

## Context

D3 in `docs/GO_LIVE_DECISIONS.md` asked two things: which roles exist, and **who fills them**.
The owner answered the second on 2026-09-28 and named the holders. Four consequences follow from
the naming that are not obvious, so this ADR records them next to the decision rather than leaving
them to be discovered later.

## Decision

### 1. The seven holders

Role ids follow Master Spec §18 where a §18 role exists, so spec traceability keeps working; the
display label follows the owner's org chart where the two differ.

| Role id | §18 role | Holder | Control that rides on it |
|---|---|---|---|
| `owner` | Owner/Director | **Mr. Owner** | receives the periodic reports; must **not** be the maker of a payment it approves |
| `gm` | *(not in §18)* | **Mr. CEO** | may be the same person as Owner; **today they are two** |
| `commercial_manager` | *(not in §18)* | **Mr. CMM** | none yet — no module |
| `partnership_manager` | Villa/Contract Admin | **Mr. PM** | §21: HUMAN, no agent |
| `reservation_manager` | Reservation Manager + Operations Manager | **Ms. RM** | approves out-of-policy discounts; **must not** approve its own |
| `finance_manager` | Finance Manager | **Ms. FAM** | the checker — **must not** be the maker |
| `system_admin` | System Admin (label: Technology / Security Manager) | **Mr. TS** | **zero** business or financial authority |
| `greeting_service` | *(replaces Guest Service)* | **Ms. Greeting** | **new role** — see §4 |

### 2. Three deferred

| Role id | Why | What actually happens meanwhile |
|---|---|---|
| `revenue_manager` | not needed yet | Revenue & BI reports go to the GM — the architecture document itself allows this (§2: *"Revenue Manager / GM if lean"*) |
| `auditor` | not needed yet | **the audit chain keeps running by itself**; what is missing is only the human who reads it. The role stays in the vocabulary and is simply unheld |
| `legal_compliance` | probably **external counsel** | a legal approval must be recorded **on behalf of external counsel**, carrying the letter or contract as its evidence. The platform cannot give an external party a login, so the *recorder* is Owner/GM and the *actor* is named separately in the record |

### 3. Two reserved for AI — and why "replaced" must not become "deleted"

The owner decided that **Finance Staff** and **Reservation Agent** are fully replaced by AI agents.
Verified fact: **those agents do not exist yet** — 0 of the 6 are built.

So this ADR records both roles as **reserved for the AI service identity, not deleted**:

- `finance_staff` → reserved for **Finance Admin AI** (component #5)
- `reservation_agent` → reserved for **AI Reservation Service** (component #3)

**Why deleting them would be a mistake.** The approval engine's maker-checker needs a *maker* and a
*checker* who are **different actors**. Delete `finance_staff` and only the checker is left:
`approvals.py` would have nothing to separate, and the rule "whoever prepares a payment cannot
approve it" would quietly stop meaning anything — while still *looking* like it was enforced. The
same is true of `reservation_agent` against `reservation_manager` on out-of-policy discounts.

**What it means until the agents exist:** nobody holds either role, so the platform **refuses**
those actions instead of granting them (fail-closed). That is safe — and it is also a **go-live
blocker**: with no maker, a payment cannot be prepared. It resolves itself the day components #3
and #5 are built.

### 4. Guest Service is deleted and replaced by a Greeting Service Team

The spec's `guest_service` role is removed and **`greeting_service`** takes its place, held by
**Ms. Greeting**, with a purpose §18 never described:

> visiting guests **in the villas** to gather insight from them, and building the relationship with
> villa management and villa crew.

That is not the same job as §18's guest service — which answered guest needs *during* the stay. Two
consequences:

- **It is a field role, not a desk role.** It needs read access to stay data (who is staying where,
  and when) so visits can be planned, and the ability to record visit notes and insight. It does
  **not** need booking, payment, or approval authority. Least privilege, written down before the
  code exists.
- **It has no AI counterpart, and should not get one silently.** The AI Workforce Architecture
  defines no Greeting agent, and §20.1 forbids creating autonomous agents without an ADR. This role
  stays human until the owner decides otherwise through an ADR.

### 5. What this ADR does *not* decide

- **The RBAC implementation.** The code still carries three disagreeing lists
  (`dashboard.ROLES` = 5, `approvals.ROLE_RANK` = 4, `identity.MFA_REQUIRED_ROLES` = 2), plus
  `accountant` and `admin` which appear in no document. Reconciling them changes approval
  behaviour, so it is its own commit with the per-role matrix test §18 requires.
- **Whether Owner and GM stay two people.** The owner stated they *may* be one person; today two
  names are given, and the platform should not assume either way.

## Consequences

- **D3 becomes DECIDED.** The vocabulary and the holders are both named. The only part still
  genuinely open is *when* the AI replacement takes effect — and that cannot be before components
  #3 and #5 exist.
- **A deviation from §18 is now on record:** `guest_service` is replaced by `greeting_service`, and
  four roles exist that §18 does not name (`gm`, `commercial_manager`, `partnership_manager`,
  `greeting_service`). Recorded here so a later reader does not mistake the code for being "wrong".
- **`preflight` will keep reporting D3 as OPEN** until the row in `docs/GO_LIVE_DECISIONS.md` is
  updated in the same commit as this ADR.
- **Test to add with the RBAC work** (§18's requirement): a per-role matrix asserting that
  `system_admin` holds no finance or bank authority; `finance_manager` cannot be the maker of what
  it approves; `reservation_manager` cannot approve its own discount; `auditor` is read-only; and
  `greeting_service` can read stays and write notes and nothing else.

---

## Amendment — 2026-09-28: the two human roles are deleted, and the maker function moves to the AI

The owner reviewed §3 and confirmed the decision with one correction: `finance_staff` and
`reservation_agent` are **not** merely reserved — **the human roles are deleted**, because the
AI agents will replace them.

That is adopted. The *effect* of §3 does not change, but the *mechanism* is now sharper, and the
difference is the whole point:

> **A role and a function are not the same thing.** The human role is deleted. The **maker
> function** is not, and must not be — a control with no maker is not a control, it is an open
> door.

### Four real cases, kept small

| A light real case | Who **makes** it | Who **approves** it | What that approval is for | If one actor did both |
|---|---|---|---|---|
| Guest cancels; the 50% deposit (Rp 7,500,000) must go back | **Finance Admin AI** prepares it: computes per the cancellation policy, attaches the transfer evidence | **Ms. FAM** (human) | money leaving the agency's account | Rp 7.5 M can be sent to any account under the word "refund"; it surfaces at the monthly reconciliation, by which time the money is gone |
| An email arrives: *"our villa's bank account has changed, transfer to the new number"* | **AI** records it and attaches the email | **Ms. FAM** (human) **plus a phone call to the OLD number** | changing where money is sent | the guest's deposit lands in a fraudster's account — the single most common loss in travel and villa agencies |
| Guest asks for 25% off; the AI's ceiling is 10% | **AI Reservation Service** builds the quote with its reason (14 nights, low season) | **Ms. RM** (human) | pricing below the policy floor | villa rates get pressed down at will; the monthly margin breaks and nobody can say where |
| Paying the villa after the guest checks out | **AI Finance** computes *booking value − agency commission − PBJT*, matched against the contract | **Ms. FAM** (human) | paying the villa the correct amount, once | a villa is overpaid or paid twice; the loss only shows months later |

### Who makes from now on

The **AI service principal** (`ai_service`, ADR-0018) — an actor the platform already has,
driven by Finance Admin AI and AI Reservation Service once components #5 and #3 are built.

| | Human role | Maker function |
|---|---|---|
| `finance_staff` | **DELETED** | held by the AI service principal |
| `reservation_agent` | **DELETED** | held by the AI service principal |

### Who stays human, permanently

The **approvers**: `finance_manager`, `reservation_manager` and `owner`. Not a preference —
AI Workforce Architecture §11 makes it an **absolute deny** for an AI to approve or execute a
payment, and §2A.3 routes *"outgoing money / refund approval / bank-wallet change"* to **human**
Finance Authority. So: **the AI does the work, the human approves it.**

**Who approves what, in one line:** AI prepares → **Ms. FAM** approves → money going out
(refunds, villa payments, bank changes). AI prepares → **Ms. RM** approves → price below the
floor. The **Owner approves no daily transaction** — per the owner's own answer on catalogue key
#3, the Owner receives periodic reports (daily / weekly / monthly) and corrects anomalies.

### The consequence the deletion creates

Until components #3 and #5 are built, nothing drives the AI maker — so **no payment request and
no quotation can be created at all**, and the platform refuses rather than falling back to a
human. That is correct fail-closed behaviour, and it means plainly: **the payment side cannot go
live before Finance Admin AI does.**

### One honest benefit that comes with it

Replacing the human maker with an AI maker removes a real attack path: a human maker can be
telephoned — *"it's the boss, transfer it now, I'll sign later"* — and that is one of the most
common ways villa and travel agencies lose money. An AI maker cannot be hurried, flattered, or
pressured. The flip side is just as real: an AI has no judgement about a case that *looks*
correct but is not, which is exactly why the **human checker matters more here, not less.**

---

## Amendment — 2026-10-02: the three lists are reconciled, and `gm` joins the ladder

§5 recorded that the code held **three disagreeing vocabularies** and left the reconciliation to
its own commit, with the per-role matrix test §18 requires. This amendment records that commit
(`WAVE 5-A`), because a reader who finds `reservation_agent` in a row written in October and
`agent` in one written in September will otherwise conclude the schema drifted.

**One home.** `src/roles.py` is now the only place the human role vocabulary is written down.
`approvals.ROLE_RANK` **is** `roles.ROLE_RANK` (the same object, not a copy — a second copy is
free to drift), and the dashboard maps to the canonical ids.

**The ladder** — ADR-0036's table, with only one new tier:

| Rank | Canonical id (what new writes say) | Was | Who |
|---|---|---|---|
| 1 | `reservation_agent` | `agent` | the maker tier, **deleted as a human role** (§3) — reserved for `ai_service` |
| 2 | `reservation_manager` | `manager` | Ms. RM — approves out-of-policy pricing |
| 3 | `finance_manager` | `accountant` | Ms. FAM — approves money out |
| 4 | **`gm`** | *(new)* | Mr. CEO — ADR-0036 tier 2 |
| 5 | `owner` | `owner` | reports and anomalies; top of the ladder |

**A rename is not an authority change**, and that is checkable rather than promised: every
legacy name resolves to the integer it already had (`agent` 1, `manager` 2, `accountant` 3), so
this commit changes which *names* the code writes without changing one decision it makes.

**The legacy names are kept forever, not for a transition period.** Approval rows are append-only:
somewhere in the history there is a row whose `required_role` is literally `accountant`. If the
alias is ever removed the engine cannot rank that row and the request becomes undecidable — an
audit trail that cannot be re-decided is not an audit trail. `roles.ROLE_ALIASES` is therefore a
fact about history, not a grace period.

**Executable matrix.** `roles.ROLE_CAPABILITIES` is the §18/ADR-0035 authority table in code, and
`tests/test_roles.py` asserts it: `system_admin` holds no finance or bank authority;
`finance_manager` and `owner` cannot be the maker of what they approve; `reservation_manager`
cannot approve money out; `auditor` is read-only; `greeting_service` reads stays and writes notes
and nothing else; and **no AI identity can appear on the ladder or as an approver** — at the
vocabulary and at the engine (`tests/test_roles.py` exercises both).

**A drift this found and closed.** `finance.approval.payment_thresholds_minor` was published
(ADR-0036) naming the ladder `["finance_manager", "gm"]` while **neither name existed** in
`approvals.ROLE_RANK`. Nothing read the value yet, so nothing failed — the first real payment
would have been refused with a cause that named no cause. `roles.approval_ladder()` now refuses
such a value loudly, and a test reads the live store to confirm the published ladder names roles
the engine knows.

**Still open, named rather than implied:**

1. **`gm` has no session yet.** The GM is on the approval ladder, but `dashboard.ROLES` — the
   five *page* roles — does not include it, so today only a service call can name `gm`. The page
   vocabulary is its own slice, and it must land **together with** `security.mfa.required_roles`:
   the GM decides money out, so the GM must owe a second factor before that slice ships.
2. **`security.mfa.required_roles` still says `["owner", "finance"]`** — a page word, not a
   canonical id. Reconciling it changes which sessions must hold a second factor, so it needs its
   own published revision rather than riding along in a rename.
3. **`documents` and `gates` keep their own words** (`manager`, `admin`, `finance`). They carry
   their own vocabularies for their own decisions and were deliberately left alone here.

**Two more copies of the same check were found by running the gate, not by reading it.** The full
suite went red in 81 places the moment `ROLE_RANK` became canonical: `messages._require_human`
validated `created_by_role` with its own `role_key not in ROLE_RANK`, so every draft prepared under
an `agent`-named role was refused as an unknown actor. `src/messages.py` now asks `roles` the same
question `approvals` asks (`roles.role_rank(...) is None`), and the import gate in
`tests/test_messages.py` — a whitelist of modules that cannot transport a message — grew by exactly
one name, `roles`, which is a vocabulary and not a transport. `preflight` gained
`check_published_ladder_roles`, so a published payment ladder that names a role the engine cannot
rank now fails the go-live gate instead of the first real payment.

**A gate that silently skips tests is not a gate.** Four refusal tests in
`tests/test_preflight.py` sat inside `class DemoDataTests` — a plain class, which pytest collects
only when the name starts with `Test` — so they had never executed, and one had gone stale in the
meantime (a foreign key now blocks the rename it performs). The classes were renamed, the stale
setup was repaired, and the rule was written into the module docstring where the next author reads
it.
