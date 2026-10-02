# ADR-0037 — The platform is sold per agency: every setting a tenant owns is a setting the tenant fills

- Status: Accepted
- Date: 2026-10-02 (owner decision) · written 2026-10-02
- Deciders: owner
- Amends: ADR-0035 (the role vocabulary becomes a blueprint a tenant may edit), ADR-0031 (a rate is a day's price — per tenant), ADR-0033 (the money flow is per tenant)
- Spec: Master Spec §5, §16, §18, §22; AI Workforce Architecture V1.1 §2, §21

## Context

The owner stated on 2026-10-02 what the product *is*:

> "sistem ekosistem Villa agency ini akan saya jual atau di buat berlangganan sistem nya, jadi
> yang akan memakainya nanti yang membeli nya … setting seperti provider nomer WA, nama
> villa-villa yang membeli/yang berlangganan ekosistem villa agency yang mengisi nya sendiri
> bukan saya"

That single sentence settles a row of decisions that were sitting in `docs/GO_LIVE_DECISIONS.md`
as *owner* decisions: D1 (payment provider), D2 (property list), D5 (tax rates), D6 (WhatsApp),
D11 (refund/compensation), D9 (retention execution). They are not the owner's to fill for every
future customer — they are **the tenant's**, and the platform's job is to give the tenant a place
to fill them.

The system is already multi-tenant in the hard places: every table carries `agency_id`, the read
paths filter on it (WAVE 1-D: fourteen queries, twelve sites), `_agency_scope()` refuses an
unscoped write, and the policy store resolves per agency. What is missing is not isolation — it is
**the tenant's own controls**: nowhere to type a provider, a WhatsApp number, a villa list, a tax
rate, or a role.

## Decision

**Everything a tenant owns is a setting, not a constant, and not a decision the platform maker
takes on the tenant's behalf.**

| What the tenant owns | Where it must live | Today |
|---|---|---|
| Payment provider & rail (D1) | per-agency setting, chosen by the tenant | not built |
| Villa/unit list + business model per villa (D2) | manual entry **or bulk file import** | not built |
| Tax rate per kabupaten/kota, per villa (D5) | `policy_versions.tax_pbjt_pct` per property, validated by Ms. FAM, approved by the GM | store exists, no screen |
| WhatsApp number (D6) | per-agency channel setting; **message templates authored by the platform** and editable by the tenant | templates exist in `messages.py`, no settings screen |
| Refund / compensation values (D11) | per-villa contract & rate card, approved Finance Manager → GM | policy exists, no screen |
| Data-retention execution (D9) | AI executes, GM/CEO **and** Owner approve, after 7 years | engine not built |
| Roles and holders (D3) | the ADR-0035 vocabulary is the **blueprint**; a tenant may add, rename, or deactivate its own roles | vocabulary is code, not data |

## Consequences

- **A new blocking decision replaces the ones it answers.** D1, D2, D5, D6, D9, D11 and D12 stop
  being "the owner must name a value" and become **D17 — tenant readiness**: before any agency uses
  the system with real money, its own required settings must be filled and a check must prove it.
  The platform may be ready while a tenant is not; that is exactly the split preflight already
  draws between "software ready" and "business ready", one level down.
- **Roles stop being a Python constant for the tenant's own use.** ADR-0035's vocabulary stays the
  blueprint and the enforcement vocabulary (a tenant may not invent a rank above the owner, and may
  not edit what the platform refuses). Editing roles is a *settings* feature with an audit trail,
  and an edited role must still be resolvable by the approval engine — the alias mechanism in
  `src/roles.py` is what makes that possible without rewriting history.
- **The platform maker's job is the template, not the value.** Message templates, the default SLA
  numbers, the default retention periods: these are the platform's to author, because a tenant
  cannot be expected to invent a privacy notice or a WhatsApp tone. The *values* that differ per
  business (provider, number, villa names, rates) are the tenant's.
- **Onboarding is a feature, not a script.** "Bulk file import" for the villa list and the rate
  card is the difference between a product a tenant can adopt and a product that needs the maker
  on the phone. It is recorded here as a first-class deliverable.
- **Nothing here loosens a control.** A tenant fills settings; it does not fill *permissions*. The
  deny registry, the human-approval rule for money out, and the AI deny list are the platform's,
  identical for every tenant.

## What this ADR does not decide

- **How tenants are billed** (subscription tiers, per-villa, per-booking). Not needed to build the
  settings, and it is a commercial decision with its own document.
- **Whether a tenant may run the platform themselves** (self-hosted) or only as a service. Both
  are compatible with this ADR; the answer changes deployment, not the design above.
