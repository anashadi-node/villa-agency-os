# VILLA CANONICAL DECISION REGISTER — DRAFT

**Status:** `DRAFT — NO AUTHORITY`. This file neither approves nor replaces any
existing decision. It reconciles business topics so the Owner can make one
explicit decision per topic. `D1`, `D2`, … in
[`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) and
[`GO_LIVE_DECISIONS.md`](GO_LIVE_DECISIONS.md) are **not** semantically identical;
matching numbers below is forbidden.

Prepared: 2026-11-23 (PACK-005C). Do not mark ADR-0074 ACCEPTED, do not enable
G2-B, and do not authorize real customer payments by editing this draft.

## Summary

| Decision ID | Business topic | Charter source | GO_LIVE source | ADR | Status |
|---|---|---|---|---|---|
| DEC-VILLA-001 | Merchant of record; incoming guest funds | D1, D3 (OPEN) | D1 (DECIDED) | 0033 | CONFLICT_OWNER_DECISION_REQUIRED |
| DEC-VILLA-002 | Invoice issuer | D2 (OPEN) | — | 0033, 0037 | OWNER_LEGAL_TAX_REQUIRED |
| DEC-VILLA-003 | Outgoing funds and custody | D3 (OPEN) | D3, D11 (DECIDED) | 0036 | CONFLICT_OWNER_DECISION_REQUIRED |
| DEC-VILLA-004 | Refund and chargeback responsibility | D4 (OPEN) | D11 (DECIDED) | 0036, 0037 | CONFLICT_OWNER_LEGAL_REQUIRED |
| DEC-VILLA-005 | Supplier confirmation / block proof | — | D1 (DECIDED) | 0033, 0069, 0073, 0074 | IMPLEMENTATION_GATED |
| DEC-VILLA-006 | Supplier contact verification | — | D1, D17 (DECIDED) | 0074 (PROPOSED) | IMPLEMENTATION_GATED |
| DEC-VILLA-007 | ADR-0074 approval status | — | D1, D17 | 0074 (PROPOSED) | PROPOSED_NOT_ACCEPTED |
| DEC-VILLA-008 | Tenant readiness gate | — | D17 (DECIDED) | 0037 | ACCEPTED_WITH_UI_GAP |
| DEC-VILLA-009 | PostgreSQL cutover | — | — | 0006 | BLOCKED |
| DEC-VILLA-010 | Master specification baseline | — | — | 0003 | EVIDENCE_INCOMPLETE |
| DEC-VILLA-011 | Pilot property count and business model | D5 (OPEN) | D2 (DECIDED) | 0037 | CONFLICT_OWNER_DECISION_REQUIRED |
| DEC-VILLA-012 | PBJT/PND tax source and approval | D6 (OPEN) | D5 (DECIDED) | 0037 | ACCEPTED_DATA_GAP |
| DEC-VILLA-013 | WhatsApp provider and templates | D7 (OPEN) | D6 (DECIDED) | 0037 | ACCEPTED_DATA_GAP |
| DEC-VILLA-014 | Shadow-mode graduation thresholds | D8 (OPEN) | D7 (DECIDED) | 0038 | ACCEPTED |
| DEC-VILLA-015 | Data retention and deletion | — | D9 (DECIDED) | 0040 | ACCEPTED_IMPLEMENTATION_GAP |
| DEC-VILLA-016 | Backup, RPO/RTO, restore testing | — | D10 (DECIDED) | — | IMPLEMENTATION_GATED |

## Decision records

### DEC-VILLA-001 — Merchant of record and incoming guest funds

- **Business domain:** money, PSP, incoming funds, legal entity.
- **Charter source:** D1 “Merchant of record & pemilik akun PSP” and D3 “Alur dana: custody vs split langsung” — both **OPEN**; D1–D4 block production guest money.
- **GO_LIVE source:** D1 — **DECIDED** 2026-09-28: merchant of record = AGENCY; guest money enters the agency; PSP is filled by each tenant; evidence `docs/adr/0033-…`.
- **ADR reference:** ADR-0033 (ACCEPTED); ADR-0012 payment rails (context only).
- **Existing approval evidence:** Owner entry in `GO_LIVE_DECISIONS.md` D1, dated 2026-09-28, pointing to ADR-0033. No legal opinion, PSP contract, or entity document is present in the repository.
- **Conflict:** Charter still calls D1/D3 OPEN, while GO_LIVE D1 is DECIDED. The numbers overlap but the questions differ: Charter D1 names owner+legal as decision owner; GO_LIVE D1 both chooses the agency model and delegates PSP choice to each tenant.
- **Financial/security impact:** BLOCKING. Receiving real guest money without a resolved merchant/entity/PSP arrangement creates custody, refund, chargeback, tax, and AML exposure.
- **Proposed authoritative wording:** “The agency is merchant of record and guest funds enter the agency. The payment provider and account are configured per tenant; the platform does not select or operate a PSP. No production guest funds are accepted until Open Decisions D1–D4 and the legal/PSP evidence are closed.”
- **Required Owner decision:** Ratify or amend GO_LIVE D1; close or restate Charter D1/D3; name the legal entity and PSP evidence.
- **Status:** `CONFLICT_OWNER_DECISION_REQUIRED`.

### DEC-VILLA-002 — Invoice issuer

- **Business domain:** tax, invoice, accounting, guest-facing document.
- **Charter source:** D2 “Penerbit invoice (entitas agency vs owner)” — **OPEN**.
- **GO_LIVE source:** No dedicated invoice-issuer row. D1 (money flow) and D5 (PBJT) are adjacent but do not decide the issuer.
- **ADR reference:** ADR-0033 (money path), ADR-0037 (tenant settings).
- **Existing approval evidence:** None found for the issuer question itself.
- **Conflict:** Charter D2 is OPEN; GO_LIVE has no equivalent row, so a reader cannot infer that the question was closed. A D-number match would be wrong.
- **Financial/security impact:** HIGH. An invoice from the wrong entity can invalidate tax treatment and misstate who owes the guest.
- **Proposed authoritative wording:** “The invoice issuer is a tenant/legal-entity setting, reviewed by the tenant’s tax advisor. Invoices and settlement instructions are in Rupiah; the platform stores the approved issuer per tenant and never hard-codes one entity for all villas.”
- **Required Owner decision:** Owner + legal/tax advisor choose the issuer model and record the evidence; do not infer it from D2 of another register.
- **Status:** `OWNER_LEGAL_TAX_REQUIRED`.

### DEC-VILLA-003 — Outgoing funds and custody

- **Business domain:** owner settlement, payout authority, bank changes.
- **Charter source:** D3 “Alur dana: custody vs split langsung ke owner” — **OPEN**.
- **GO_LIVE source:** D3 — **DECIDED** 2026-09-28: real transfers and bank changes are human-only, with GM/CEO approval; evidence ADR-0035/0036. D11 covers refund approval.
- **ADR reference:** ADR-0036 (ACCEPTED) — money leaves by a human hand and the GM agrees first.
- **Existing approval evidence:** Owner entry in GO_LIVE D3 dated 2026-09-28; three money rules in the D3 value.
- **Conflict:** GO_LIVE D3 settles **execution authority** but not the **custody model** (agency holds funds vs split at source). Charter D3 still labels the custody question OPEN.
- **Financial/security impact:** BLOCKING/HIGH. Ambiguous custody can create commingling, payout disputes, and unauthorized bank changes.
- **Proposed authoritative wording:** “Guest money is received by the agency and held under the approved custody model. No automated payout or bank-detail change exists. Every real transfer is executed by a human, approved Finance Manager → GM/CEO, and recorded; the platform only prepares evidence.”
- **Required Owner decision:** Choose custody vs direct split, ratify the human-only execution rule, and record bank-change/approval evidence.
- **Status:** `CONFLICT_OWNER_DECISION_REQUIRED`.

### DEC-VILLA-004 — Refund and chargeback responsibility

- **Business domain:** refunds, chargebacks, compensation, guest money.
- **Charter source:** D4 “Tanggung jawab refund & chargeback” — **OPEN**.
- **GO_LIVE source:** D11 — **DECIDED** 2026-10-02: contract/rate-card terms; approval ladder Finance Manager → GM/CEO; above-threshold goes to GM/CEO.
- **ADR reference:** ADR-0036 (human money authority), ADR-0037 (tenant settings).
- **Existing approval evidence:** Owner entry in GO_LIVE D11 dated 2026-10-02.
- **Conflict:** Charter D4 remains OPEN while GO_LIVE D11 is DECIDED. The topics overlap substantially, but the Charter assigns legal responsibility while GO_LIVE describes the operational approval path.
- **Financial/security impact:** HIGH. An unsupported refund or chargeback can lose money and create a guest/owner dispute.
- **Proposed authoritative wording:** “Refund and chargeback responsibility follows each villa’s contract/rate card. No refund is automated; approval is Finance Manager → GM/CEO, with any above-contract amount escalated. The legal responsibility clause remains subject to counsel.”
- **Required Owner decision:** Ratify GO_LIVE D11 as the operational rule and obtain/record legal review for responsibility allocation; then close Charter D4 explicitly.
- **Status:** `CONFLICT_OWNER_LEGAL_REQUIRED`.

### DEC-VILLA-005 — Supplier confirmation and block proof

- **Business domain:** reservation kernel, supplier proof, booking confirmation.
- **Charter source:** No dedicated row; related to production readiness.
- **GO_LIVE source:** D1 — **DECIDED** 2026-09-28: villa must send written/email unit-block evidence before booking CONFIRMED.
- **ADR reference:** ADR-0033 (ACCEPTED), ADR-0069 (ACCEPTED), ADR-0073 (ACCEPTED), ADR-0074 (PROPOSED).
- **Existing approval evidence:** ADR-0073 accepted by Owner 2026-11-18; GO_LIVE D1 owner entry.
- **Conflict:** The block-proof mechanism is accepted (ADR-0073), but the supplier-contact authenticity rules needed to trust the source are still PROPOSED (ADR-0074); G2-B and supplier enforcement remain OFF.
- **Financial/security impact:** BLOCKING for real bookings. Confirming without a verified source is the double-booking/guest-relocation path.
- **Proposed authoritative wording:** “A booking becomes CONFIRMED only after verified payment and a booking-scoped supplier block proof whose contact was verified under the approved supplier-authenticity policy. A block proof without a verified contact is not sufficient.”
- **Required Owner decision:** Decide ADR-0074 and its contact-verification policy before enabling production confirmations; keep G2-B OFF until then.
- **Status:** `IMPLEMENTATION_GATED`.

### DEC-VILLA-006 — Supplier contact verification

- **Business domain:** supplier identity, contact registry, maker/checker.
- **Charter source:** No dedicated row.
- **GO_LIVE source:** D1 (supplier confirmation flow), D17 (tenant readiness).
- **ADR reference:** ADR-0074 (PROPOSED); implementation `src/schema/020_supplier_contacts.sql`, `src/supplier_assertion.py`.
- **Existing approval evidence:** ADR-0074 §10 records an Owner-approved **development-baseline** deviation on 2026-11-20; it explicitly retains PROPOSED status. It is not an approval of live contact triggers.
- **Conflict:** Migration `020` contains contact triggers, while the governing ADR remains PROPOSED. The current signer can be blocked at read time by `MFA_REQUIRED` (C6 gap).
- **Financial/security impact:** HIGH. A false supplier contact can authorize a fake block proof; an over-restricted contact can prevent a legitimate confirmation.
- **Proposed authoritative wording:** “Only a verified phone contact, registered by an authenticated owner/GM session with maker≠checker, may be used for a callback. Reads are tenant-scoped and audited. The signer role is MFA-required and must step up for signing. Activation requires ADR-0074 acceptance.”
- **Required Owner decision:** Accept/amend ADR-0074, approve the reservation_manager MFA policy change, and authorize `020`/G2-B separately.
- **Status:** `IMPLEMENTATION_GATED`.

### DEC-VILLA-007 — ADR-0074 approval status

- **Business domain:** governance, supplier authenticity, human signer.
- **Charter source:** —.
- **GO_LIVE source:** D1 and D17 references, not an approval of ADR-0074.
- **ADR reference:** ADR-0074, Status **PROPOSED** (`E5_LOCAL_COMMITTED_REVIEW_PENDING`; §10 retains PROPOSED).
- **Existing approval evidence:** Owner approved four development-baseline deviations on 2026-11-20; no acceptance of the ADR as a whole.
- **Conflict:** Implementation and tests exist locally, but the decision remains proposed; treating “code exists” as “policy approved” is the governance error this register prevents.
- **Financial/security impact:** HIGH if live activation is inferred without approval.
- **Proposed authoritative wording:** “ADR-0074 remains PROPOSED. It may be accepted only by an explicit Owner decision naming the accepted scope, the contact-read policy, and the activation gate. No live migration or enforcement follows from local code.”
- **Required Owner decision:** Accept, reject, or amend ADR-0074; record the decision reference and scope.
- **Status:** `PROPOSED_NOT_ACCEPTED`.

### DEC-VILLA-008 — Tenant readiness gate

- **Business domain:** tenant onboarding, go-live gate.
- **Charter source:** —.
- **GO_LIVE source:** D17 — **DECIDED** 2026-10-02: each agency must pass its own readiness check; required settings + real role holders.
- **ADR reference:** ADR-0037 (ACCEPTED).
- **Existing approval evidence:** Owner entry in GO_LIVE D17; `src/settings.py` readiness and `scripts/preflight.py::check_tenant_readiness` exist.
- **Conflict:** D17 says the readiness screen is “belum dibangun”, while the checker exists; the missing UI is an implementation gap, not a policy gap.
- **Financial/security impact:** HIGH. An unprepared tenant can accept guests/money without a payment provider, tax rate, rate card, or real role holders.
- **Proposed authoritative wording:** “No agency goes live until its own readiness check passes: required settings are filled and every role has a real holder. The platform may be ready earlier; tenant readiness is a separate blocking gate.”
- **Required Owner decision:** Confirm the gate remains blocking; authorize or defer the readiness UI as a separate implementation slice.
- **Status:** `ACCEPTED_WITH_UI_GAP`.

### DEC-VILLA-009 — PostgreSQL cutover

- **Business domain:** storage, migration, operational continuity.
- **Charter source:** —.
- **GO_LIVE source:** —.
- **ADR reference:** ADR-0006 (ACCEPTED) — additive migration and parallel-state strategy.
- **Existing approval evidence:** ADR-0006 accepted 2026-09-28; PACK-005B recorded the PG migration runner and cutover as blocked.
- **Conflict:** SQLite is the live operational store in this pack; PG has migration tooling but no approved cutover evidence. “PG code exists” is not “PG is authoritative”.
- **Financial/security impact:** HIGH. A premature cutover risks lost reservations/payments and split-brain state.
- **Proposed authoritative wording:** “SQLite remains authoritative until the PostgreSQL parallel-state mapping, migration dry-run, ledger reconciliation, and restore evidence pass and the Owner approves a cutover window. No cutover occurs in PACK-005C.”
- **Required Owner decision:** Approve a future PG cutover only after the named evidence exists; keep SQLite for this pack.
- **Status:** `BLOCKED`.

### DEC-VILLA-010 — Master specification baseline

- **Business domain:** requirements authority, governance.
- **Charter source:** —.
- **GO_LIVE source:** —.
- **ADR reference:** ADR-0003 (ACCEPTED, v1.0 baseline); `docs/MASTER_SPEC_V1_1_DELTA.md` claims v1.1 authority from the Owner’s 2026-09-26 instruction.
- **Existing approval evidence:** Owner instruction recorded in the delta; the referenced `~/Downloads/villa-agency-os-hermes-master-specification-v1.1.html` is **not present** (PACK-005B/005A evidence). The delta records a SHA-256 but the original artifact could not be verified.
- **Conflict:** A delta claims to replace v1.1 requirements while the authoritative artifact is missing; teams cannot verify a hash without the file.
- **Financial/security impact:** MEDIUM–HIGH. Requirements can be silently changed by citing an unavailable document.
- **Proposed authoritative wording:** “v1.1 is authoritative only when the original artifact is restored and its recorded SHA-256 is verified. Until then, v1.0 plus the delta is the working baseline; no new requirement is treated as approved from the missing file.”
- **Required Owner decision:** Restore/provide the v1.1 artifact, verify its hash, and confirm whether the delta is the complete change set.
- **Status:** `EVIDENCE_INCOMPLETE`.

### DEC-VILLA-011 — Pilot property count and business model

- **Business domain:** commercial scope, tenant configuration.
- **Charter source:** D5 “Jumlah properti pilot final (10–20?)” — **OPEN**.
- **GO_LIVE source:** D2 — **DECIDED** 2026-10-02: each tenant fills its own property list and per-villa model (commission/full management); import tooling exists.
- **ADR reference:** ADR-0037 (ACCEPTED).
- **Existing approval evidence:** Owner entry in GO_LIVE D2; `src/villa_import.py` and `scripts/import_villas.py`.
- **Conflict:** Charter asks for one platform pilot number; GO_LIVE delegates the list and model to each tenant. “10–20” is a working recommendation, not a final decision.
- **Financial/security impact:** MEDIUM. A platform-wide assumption can misstate capacity, commissions, and onboarding work.
- **Proposed authoritative wording:** “Pilot size and per-villa business model are tenant data, not a platform constant. The platform supports commission, full management, or both per property; the initial 10–20 figure remains a planning assumption until the Owner fixes a number.”
- **Required Owner decision:** Confirm whether a platform-level pilot cap exists; otherwise approve tenant-defined lists.
- **Status:** `CONFLICT_OWNER_DECISION_REQUIRED`.

### DEC-VILLA-012 — PBJT/PND tax source and approval

- **Business domain:** tax, invoice, property policy.
- **Charter source:** D6 “Struktur PBJT per kabupaten/kota + PND” — **OPEN**.
- **GO_LIVE source:** D5 — **DECIDED** 2026-10-02: rates are tenant-filled per kabupaten/kota and per villa; Finance Manager approves, GM/CEO approves; `policy_versions.tax_pbjt_pct`; no hard-code.
- **ADR reference:** ADR-0037.
- **Existing approval evidence:** Owner entry in GO_LIVE D5.
- **Conflict:** Charter D6 remains OPEN while GO_LIVE D5 is DECIDED; the tax advisor’s formal approval is not in the repository.
- **Financial/security impact:** HIGH. Wrong tax creates under/over-collection and compliance exposure.
- **Proposed authoritative wording:** “Tax is a per-property policy value sourced from the tenant’s approved kabupaten/kota schedule. No rate is hard-coded. Finance Manager prepares; GM/CEO approves; the tax advisor’s written source is retained.”
- **Required Owner decision:** Ratify GO_LIVE D5 and record the tax-advisor evidence; do not close Charter D6 without it.
- **Status:** `ACCEPTED_DATA_GAP`.

### DEC-VILLA-013 — WhatsApp provider and templates

- **Business domain:** guest communication, consent, channel.
- **Charter source:** D7 — **OPEN**.
- **GO_LIVE source:** D6 — **DECIDED** 2026-10-02: tenant fills the WhatsApp number; system provides 12 templates; no template requests identity documents in chat.
- **ADR reference:** ADR-0037.
- **Existing approval evidence:** Owner entry in GO_LIVE D6; `docs/MESSAGE_TEMPLATES.md`.
- **Conflict:** Charter D7 remains OPEN; GO_LIVE D6 settles content and tenancy but the provider/consent integration is still a tenant setup task.
- **Financial/security impact:** MEDIUM. Wrong channel/consent can leak guest data or fail to deliver a booking notice.
- **Proposed authoritative wording:** “The tenant configures the official WhatsApp Business number and consent; the platform provides approved Indonesian templates. No identity documents are requested in chat.”
- **Required Owner decision:** Confirm provider/consent ownership and close Charter D7 with evidence.
- **Status:** `ACCEPTED_DATA_GAP`.

### DEC-VILLA-014 — Shadow-mode graduation thresholds

- **Business domain:** AI autonomy, quality gate.
- **Charter source:** D8 — **OPEN**.
- **GO_LIVE source:** D7 — **DECIDED** 2026-10-02: agreement rate ≥95%, unsupported claims = 0, minimum 100 cases.
- **ADR reference:** ADR-0038 (ACCEPTED).
- **Existing approval evidence:** Owner entry in GO_LIVE D7.
- **Conflict:** Charter D8 is OPEN while GO_LIVE D7 is DECIDED; the measurement implementation exists separately.
- **Financial/security impact:** MEDIUM. Premature autonomy can send unsupported claims or execute actions.
- **Proposed authoritative wording:** “AI remains in shadow mode until ≥95% agreement, zero unsupported material claims, and at least 100 measured cases; graduation is a recorded decision, not a code switch.”
- **Required Owner decision:** Ratify GO_LIVE D7 and close Charter D8.
- **Status:** `ACCEPTED`.

### DEC-VILLA-015 — Data retention and deletion

- **Business domain:** privacy, audit, deletion.
- **Charter source:** Section 6 PDP principles; no separate decision row.
- **GO_LIVE source:** D9 — **DECIDED** 2026-10-02: delete after 7 years; AI executes only with GM/CEO + Owner approval; audit recorded.
- **ADR reference:** ADR-0040 (security/privacy first) as context.
- **Existing approval evidence:** Owner entry in GO_LIVE D9.
- **Conflict:** The policy is decided, but no scheduled deletion implementation/retention job is authorized or verified in this pack.
- **Financial/security impact:** HIGH. Premature deletion loses financial/audit evidence; indefinite retention violates PDP minimisation.
- **Proposed authoritative wording:** “Retention is 7 years for the covered classes; deletion is a human-approved, audited job. No automatic deletion is added until a tested retention policy and backup recoverability gate exist.”
- **Required Owner decision:** Approve the retention schedule/classes and the deletion operator; keep automatic deletion OFF until tested.
- **Status:** `ACCEPTED_IMPLEMENTATION_GAP`.

### DEC-VILLA-016 — Backup, RPO/RTO, restore testing

- **Business domain:** continuity, recovery evidence.
- **Charter source:** Section 6/8 (risk mitigation); no separate decision row.
- **GO_LIVE source:** D10 — **DECIDED** 2026-10-02: RPO 24 h, RTO 4 h, off-host copy, restore test every 3 months by Mr. TS.
- **ADR reference:** No dedicated backup ADR found; PACK-005C implements C1–C4 locally.
- **Existing approval evidence:** Owner entry in GO_LIVE D10; PACK-005B recorded the newest `data/` backup as 28 Sep 2026, so RPO is currently unmet.
- **Conflict:** The approved target exists, but there is no enabled timer, no verified off-host copy, and no current restore cadence. A local script alone does not meet D10.
- **Financial/security impact:** HIGH. Lost reservations/payments or an unverified restore can stop the business.
- **Proposed authoritative wording:** “RPO 24 h/RTO 4 h are targets, not achieved facts. A backup is usable only after an independent restore drill. Off-host storage and the 3-month test cadence require separate approval; no retention deletion is enabled.”
- **Required Owner decision:** Approve timer activation, off-host destination, retention, and the named restore tester; record the first verified drill.
- **Status:** `IMPLEMENTATION_GATED`.

## Guardrails for the next revision

- Do not replace a DECIDED value merely because a different register has the same
  number.
- Do not turn an ADR `PROPOSED` status into `ACCEPTED` by editing this draft.
- Keep `G2_B: OFF`, `supplier_enforcement: OFF`, and real payment ingress OFF
  until the Owner records the corresponding decisions.
- Every future status change needs an Owner reference, date, and evidence path.
