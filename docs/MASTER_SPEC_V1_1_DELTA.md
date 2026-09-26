# Master Spec V1.1 — Delta Register (integration into Wave 0 / Traceability)

- **Authoritative document:** `~/Downloads/villa-agency-os-hermes-master-specification-v1.1.html`
- **Version string:** `1.1 Final Architecture Freeze — AI Reservation Autonomy + U.S. English + Multi-Rail Payments`
- **sha256:** `0751dfdc89968495c4707505a575ea077456fb72437cf1d0262d145339388da3` (57,059 bytes, 48 sections)
- **Supersedes:** `villa-agency-os-hermes-master-specification.html` (v1.0, sha256 `…` recompute before use; 50,234 bytes)
- **Status:** v1.1 **replaces every earlier version** as the source of truth (owner instruction, 2026-09-26).
- **Scope of this document:** only what v1.1 **adds or changes**. Requirements already covered by
  `MASTER_SPEC_TRACEABILITY_MATRIX.md` are not repeated here (V1.1 instruction: do not re-audit unchanged parts).

Format per requirement: **Requirement → Existing condition → Gap → Change required → Test/Evidence**.

---

## D-1 — Language standard is LOCKED to U.S. English (en-US) — §55

| | |
|---|---|
| **Existing** | Production-facing text is Indonesian: **484** Indonesian string literals across **10** modules (`dashboard.py` 158, `connectors.py` 90, `incidents.py` 74, `cases.py` 42, `intake.py` 40, `identity.py` 26, `matching.py` 26, `messages.py` 19, `documents.py` 7, `scheduler.py` 2) + **15** hits across **8** templates. *Measured with an AST sweep that excludes docstrings (developer-facing) and counts only literals a program can emit — an earlier regex estimate of 306 was too coarse and is superseded by this number, which is now pinned by `tests/test_language_standard.py`.* + Indonesian docs. Canonical identifiers are already English. |
| **Gap** | The locked rule covers *all* production system language: internal UI, client communications, invoices, quotations, confirmations, validation/help text, default AI replies, tests, user-facing docs. |
| **Change** | (a) ADR-0011 records the strategy; (b) translate UI/validation/error text in `templates/` + the 10 modules; (c) add a guard test that fails when new Indonesian text enters a production surface; (d) Indonesian remains allowed only in `docs/` governance notes where V1.1 itself uses it. |
| **Test** | Guard test over `templates/*.html` + string literals in `src/`; assertion that a known sample of user-visible strings is en-US. |
| **Wave** | WAVE 2 (before any new UI surface grows further); translation of approval/finance strings before WAVE 6. |

## D-2 — AI Reservation Agent with risk-based autonomy + human handoff — §56

| **Existing** | No AI runtime exists. ADR-0009 is a paper ADR (gateway/tool boundaries). |
|---|---|
| **Gap** | No AI service identity, no autonomy tiers (low/medium/high-risk), no policy-controlled tools, no human takeover that preserves case context and pauses conflicting autonomous outbound actions. |
| **Change** | ADR-0009 update: autonomy tier table, tool allowlist by tier, handoff contract (case context preserved, autonomous outbound paused). Blocked by "AI Service identity" from §18, which is still MISSING. |
| **Test** | Adversarial: conversation instruction cannot raise privilege; handoff pauses queued outbound drafts; low-risk action proceeds without human. |
| **Wave** | WAVE 1 (AI service identity) → WAVE 8 (agent runtime). |

## D-3 — Professional Reservation Knowledge Layer, separated from verified data — §57

| **Existing** | Nothing. |
|---|---|
| **Gap** | No knowledge artifact; no rule that knowledge never substitutes verified villa facts, rates, policies, availability, settlement, or supplier confirmation. |
| **Change** | Versioned knowledge artifact + ADR-0009 amendment: knowledge is advisory content, every commercial fact still resolves through `get_verified_villa_facts`-style tools; unknown fact → clarification, never fabrication. |
| **Test** | Guest asks an unverified villa fact → agent refuses to assert, asks/verifies; rate answer must cite the applicable verified rate version. |
| **Wave** | WAVE 8. |

## D-4 — Multi-rail, provider-neutral payment architecture — §58, §61

| **Existing** | `payments.py` + `payments` table carry a single `provider` + `provider_intent_id`; `method` vocabulary is `HOSTED_PAGE, BANK_TRANSFER, CASH, OTHER`. Columns: `payment_id, agency_id, invoice_id, provider, provider_intent_id, method, currency, amount_minor, status, created_at, expires_at, settled_at, provider_status`. |
|---|---|
| **Gap** | Missing rails: **CARD, PAYPAL, STABLECOIN**. Missing canonical fields: method category, asset, blockchain network, gross vs fee vs settlement amount/currency, verification source/status/timestamps. No adapter boundary — provider logic would leak into core. |
| **Change** | ADR-0012: adapter interface (`PaymentRailAdapter`), `method` vocabulary extension to `CARD, BANK_TRANSFER, PAYPAL, STABLECOIN` (existing values mapped, not dropped), added columns for fees/settlement/verification + `asset`/`network` (stablecoin), and a rule that core booking/finance models stay provider-neutral. Schema change = migration → **needs owner approval**. |
| **Test** | §61 canonical set: duplicate webhook per rail; unallocated bank receipt; partial/overpayment; browser-success without verified settlement; wrong network/token/amount; rail outage does not block core booking. |
| **Wave** | WAVE 6 (Finance), adapter skeleton can be drafted in WAVE 2 without provider commitment. |

## D-5 — Card minimum-PCI controls — §59

| **Existing** | `assert_no_card_data` in `payments.py` refuses card-like fields; webhook verification stores `signature_verified` and `applied`; unverified events open a reconciliation case. |
|---|---|
| **Gap** | No documented hosted-checkout/tokenization stance; no test proving raw PAN/CVV/PIN never reaches storage or logs; refunds-not-automated stance not pinned for cards. |
| **Change** | ADR-0012 section: hosted fields/tokenization only, never store sensitive authentication data; extend the existing guard to cover logs and error payloads. |
| **Test** | Attempt to store PAN/CVV via every write path → refused and nothing persisted; scan DB + logs for card patterns. |
| **Wave** | WAVE 6. |

## D-6 — USDC/USDT stablecoin controls — §60

| **Existing** | Nothing. |
|---|---|
| **Gap** | Missing asset **and** network allowlist (symbol alone insufficient), destination/reference/expiry instructions, finality/confirmation policy, crypto exception set (under/over/duplicate/wrong token/wrong network/late/reorg/outage/unmatched), wallet substitution treated as bank-substitution risk. |
| **Change** | ADR-0013: allowlist model, trusted verification sources (custody provider / chain data / controlled human verification), exception catalogue, wallet-change approval flow reusing the bank-profile versioning pattern. Private keys/seed/signing credentials must never enter LLM context, frontend, logs, source, or ordinary staff access. |
| **Test** | Wrong network/token/amount → exception + no settlement; destination change → pending verification, no silent redirect; AI attempt to sign/transfer → denied at capability level. |
| **Wave** | WAVE 6 + compliance gate (OPEN decisions: jurisdiction, AML/KYC, tax, custody). |

## D-7 — AI capability deny list extended — §21, §60

| **Existing** | No AI capabilities exist (nothing to deny yet); ADR-0009 lists the finance deny list. |
|---|---|
| **Gap** | Deny registry must additionally cover: signing blockchain transactions, transferring crypto, approving withdrawals, controlling treasury wallets, reading private keys/seed phrases/signing credentials. |
| **Change** | ADR-0009 amend + implement as **no such tool exists** (deny by construction, not by prompt). |
| **Test** | Adversarial prompt suite asserting the capability is absent, not merely refused. |
| **Wave** | WAVE 1 (identity/deny registry) → WAVE 8 (tools). |

## D-8 — Physical database requirements not yet met — §39, Appendix B, §23

| **Existing** | `agency_id` + FKs + money as integer minor units + tenant-scoped uniques ✅. Human-readable reference: only `INV-` exists. `version` columns: `contract_versions`, `rate_versions`, `policy_versions`, `rate_cards`, `documents` (+ `bookings.version_lock`). Ids are opaque prefixed tokens (e.g. `task_ab12`), **not** UUID/ULID. |
|---|---|
| **Gap** | (a) Missing reference patterns `INQ-`, `QT-`, `RSV-`, `PR-`, `INC-`, `APR-` §Appendix B; (b) optimistic-concurrency `version` columns missing on `bookings`/`cases`/`invoices`/`payments` etc.; (c) id format deviates from "UUID/ULID internal IDs". |
| **Change** | ADR needed (0014) recording either adoption or a justified deviation for id format; reference-pattern generator module; `version` column + 409 `CONCURRENT_MODIFICATION` semantics on mutable business records; API money as decimal string (§23) while storage stays integer minor units. |
| **Test** | Reference pattern format per entity; stale write → 409 + no overwrite; money round-trips as decimal string without precision loss. |
| **Wave** | WAVE 2 (references for supplier/contract entities) then WAVE 5/6. |

## D-9 — RBAC role model mismatch — §18

| **Existing** | Dashboard `ROLES` = 5: `reservation`, `finance`, `operations`, `owner`, `auditor`. |
|---|---|
| **Gap** | V1.1 defines **12** roles with explicit authority + critical exclusions: Owner/Director, Reservation Manager, Reservation Agent, Operations Manager, Guest Service, Finance Staff, Finance Manager, Villa/Contract Admin, System Admin, Auditor, AI Service, System Service. Notably our single `reservation` role merges Manager+Agent, and `finance` merges Staff+Manager (which breaks maker-checker separation in the role model). |
| **Change** | Role vocabulary expansion + separation-of-duties mapping; `system_admin` must not inherit finance/bank authority; AI Service and System Service as non-human identities. |
| **Test** | Per-role matrix test: each role's allowed/denied surfaces; finance staff cannot self-approve; system admin denied bank/payment authority. |
| **Wave** | WAVE 1 (finishes the security foundation). |

## D-10 — Policy Engine, Exception Engine, transactional outbox, jobs semantics — §16, §17, §25, §26

| **Existing** | Approval engine ✅ (`approvals.py`), audit chain ✅, scheduler-ish sweeps ✅ (`scheduler.py`), observability ✅. Policy Engine ❌, Exception Engine ❌, outbox ❌ (ADR-0007 paper only). |
|---|---|
| **Gap** | Thresholds/percentages still live in code constants; no outbox → business state and events can diverge; `SOURCE_UNAVAILABLE` semantics not modelled (must not become `NOT_AVAILABLE` or `PAYMENT_FAILED` without evidence). |
| **Change** | Policy Engine as first-class versioned component (deposit %, balance days, discount authority, margin floors, approval/dual thresholds, hold reminders, step-up, retention); Exception Engine (rule → task/alert/approval → role queue); outbox table + publisher; delivery semantics PENDING/SENT/DELIVERED/FAILED. |
| **Test** | Policy version pinned into each decision; historical approval keeps its `policy_version_id`; outbox event written in the same transaction as the state change; duplicate webhook → one financial entry. |
| **Wave** | WAVE 1 (Policy Engine + outbox) — required before WAVE 6 finance. |

## D-11 — Booking confirmation / closure gates and parallel states — §8, §9, §30

| **Existing** | `booking_states.py` parallel statuses partially model this; single `reservations`-style model is deliberately avoided already. |
|---|---|
| **Gap** | `BOOKING_CONFIRMED` gate list (commercial / inventory / financial with `required_financial_conditions`) and `BOOKING_CLOSED` gate list (checkout, open issues, financial + supplier reconciliation, deposit, incidents, documents) are not enforced as gates; blockers not exposed. |
| **Change** | Gate evaluation service + explicit blocker list; `POST_STAY_PENDING` instead of silent close. |
| **Test** | Close with unresolved refund/incident → blocked with blockers enumerated; deposit % and balance days read from policy, not hard-coded. |
| **Wave** | WAVE 5 (reservation) / WAVE 7 (operations). |

## D-12 — CI/CD and security-baseline gaps — §40, §42

| **Existing** | `make check` = compileall + pytest + secret scan + unsafe-config scan + PostgreSQL twin parity. Session rotation/revocation + idle/absolute timeouts ✅. Log redaction ✅. |
|---|---|
| **Gap** | Missing: lint, type check, API contract tests, DB migration tests, dependency vulnerability scan, E2E, visual regression, encrypted backups + **restore drill**, field-level encryption for L4 (bank details/passport), **Finance Lockdown** and **AI Kill Switch**, critical-change second approver (agents must not self-approve). |
| **Change** | Extend the quality gate; add L4 field encryption decision; implement Finance Lockdown + AI Kill Switch; restore-drill runbook. |
| **Test** | Restore drill verified; kill switch stops AI and outbound autonomy without stopping core booking; L4 fields unreadable without authorization. |
| **Wave** | WAVE 1 (lockdown/kill switch) → WAVE 9 (hardening drills). |

---

## New / updated ADRs required by v1.1

| ADR | Action | Subject |
|---|---|---|
| 0003 | **update** | record v1.1 supersession of v1.0 (controlled requirement change) |
| 0004 | **update** | API contract standard: §22 semantic commands, §23 money decimal string, Idempotency-Key, 409 concurrency, correlation id |
| 0009 | **update** | AI autonomy tiers (§56), knowledge layer boundary (§57), extended deny registry incl. crypto (§60) |
| **0011** | **new** | Language standard en-US (LOCKED) + migration strategy for existing Indonesian production text |
| **0012** | **new** | Provider-neutral multi-rail payment orchestration + canonical payment model + minimum-PCI stance |
| **0013** | **new** | Stablecoin (USDC/USDT) asset+network allowlist, verification sources, exception catalogue |
| **0014** | **new** | Identifier & reference format: UUID/ULID question, human-readable references (Appendix B), optimistic-concurrency `version` columns |

## Migration-plan ordering impact

1. **WAVE 1 is not finished**; v1.1 makes Policy Engine, AI Service identity, the 12-role RBAC model, and the transactional outbox *foundational*. Spec instruction: *do not proceed by assuming unfinished security/finance controls will be added later.* → Policy Engine + outbox + role model should be completed before WAVE 6 finance and are a hard prerequisite for the v1.1 payment work.
2. **Existing wave naming maps as:** our `Wave 1-*` ≈ spec WAVE 0/1; our planned `Wave 2-A supplier route` ≈ spec WAVE 2 *Villa Commercial Foundation* — and under v1.1 it must include the §12 provenance pipeline (SUPPLIER DOCUMENT → QUARANTINE → AI EXTRACTION → STRUCTURED DRAFT → PENDING VERIFICATION → HUMAN VERIFIED → APPROVED → ACTIVE, with **no AI_EXTRACTED → ACTIVE**), §27 document quarantine, and §44 Contract/Rate DoD (provenance, versioning, effective dates, conflict detection).
3. **Payment work moves later than the supplier route**, but its schema delta (D-4/D-6) should be designed now so the WAVE 2 contract/rate model does not assume a single provider.

## Critical blockers / new risks

1. **en-US is LOCKED but production text is Indonesian** (306 literals, 11 templates). Until translated, the system is non-conformant with a locked requirement.
2. **RBAC role model** (5 vs 12 roles) breaks the spec's separation of duties (Finance Staff ≠ Finance Manager) — a maker-checker requirement, not cosmetics.
3. **No Policy Engine** → thresholds remain in code, contradicting §16 and blocking §9/§11 gate work.
4. **No outbox** → §25 event reliability unproven; finance events could diverge from state.
5. **Stablecoin compliance is OPEN** (jurisdiction, AML/KYC, tax, custody) → capability may be built, but activation must stay gated.
6. **L4 data has no field-level encryption** (bank details, passport) → §19/§40 requirement not met.
7. **Id format deviation** (prefixed tokens vs UUID/ULID) → needs a recorded decision, not silent drift.

## Wave 0 status after integration

**CONDITIONAL GO** — unchanged in kind from the brownfield audit, with new conditions:

- Wave 0 deliverables (ADRs, traceability, migration test plan) now exist **and include the v1.1 delta**; no migration implementation has been executed.
- Condition 1: ADR-0011…0014 must be written (this integration) and accepted before the waves they gate.
- Condition 2: WAVE 1 items (Policy Engine, AI service identity, 12-role RBAC, outbox) complete before WAVE 6.
- Condition 3: any schema change for payments/stablecoin/references requires explicit owner approval (no autonomous destructive migration, §39).
- Condition 4 (blocker to a *sellable* pilot): the en-US language migration must be scheduled — a locked requirement that is currently unmet.

**No-GO** applies only to production go-live, unchanged: no auth wired into the HTTP surface, no policy engine, no outbox, no provider integrations, no restore drill.
