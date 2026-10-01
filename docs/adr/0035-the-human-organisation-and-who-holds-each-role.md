# ADR-0035 — The human organisation: seven holders, three deferred, two reserved for AI, and a new Greeting Service

- Status: Accepted
- Date: 2026-09-28
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
