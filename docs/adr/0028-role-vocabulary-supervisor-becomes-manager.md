# ADR-0028 — The approval role `supervisor` is renamed `manager`

- Status: Accepted
- Date: 2026-09-27
- Deciders: owner

## Context

The agency names the role that oversees front-line work *Manajer*. The platform called it
`supervisor`: `approvals.ROLE_RANK` ranked `agent`/`supervisor`/`accountant`/`owner`,
`dashboard.APPROVAL_ROLE_BY_ROLE` mapped the session role `operations` onto it, and
`messages` defaulted `required_role` to it. Two names for one authority is not cosmetic: a
policy value is compared against `actor_role` **verbatim**, so a role set written in the
owner's words would match nobody, and the permission would silently do nothing.

This is **not** the §18 role-model work. V1.1 §18 defines twelve roles with a maker-checker
separation (Reservation Manager ≠ Reservation Agent, Finance Manager ≠ Finance Staff); delta
D-9 tracks that gap and it stays open. A rename neither closes nor widens it — the kernel
still ranks four approval roles, and this ADR adds, merges and re-ranks none of them.

## Decision

`supervisor` becomes `manager` everywhere the approval vocabulary is written: `ROLE_RANK`,
the dashboard mapping, the `messages` default, and the tests that name them. The retired name
is refused like any other unknown role — a stale caller is never quietly accepted.

## Consequences

- `actor_role` values are `agent`, `manager`, `accountant`, `owner`. Anything that publishes a
  role set (`commercial.discount.authority_roles`) must use exactly these names.
- ADR-0017 and ADR-0018 describe `ROLE_RANK` as it stood when they were written; their
  mentions of `supervisor` are historical.
- D-9 remains open. When the twelve-role model lands, `manager` is where Operations Manager
  attaches; the per-function managers (Reservation, Finance) are still to be separated.
- A guard test pins `dashboard.APPROVAL_ROLE_BY_ROLE` values to `ROLE_RANK`, so a half-done
  rename fails loudly instead of silencing a role.
