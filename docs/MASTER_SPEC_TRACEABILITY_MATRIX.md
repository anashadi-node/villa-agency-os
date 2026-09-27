# Master Spec Traceability Matrix — Brownfield Audit Baseline

- Status: **Wave 0 baseline**
- Master source of truth: `/home/anashadi/Downloads/villa-agency-os-hermes-master-specification.html`
- Scope: compare existing `/home/anashadi/villa-agency-os` against Master Specification V1 Production Core
- Rule: this file is **audit/migration control**, not implementation.
- Coding/refactor/database migration: **NOT STARTED** *(true of this Wave 0 baseline snapshot; see
  the progress note below — the rows are deliberately left as audited, not rewritten).*

## Progress since this baseline

The classifications above are the Wave 0 audit of the code as it was found. Work has since
proceeded wave by wave, each recorded in an ADR rather than by editing these rows:

| Wave | Scope | Record |
|---|---|---|
| WAVE 1 | tenant scoping, integrity, observability | ADR-0003 … ADR-0014 |
| WAVE 2-A | supplier route (contract/rate versions) | `docs/adr/0013-*`, `src/supplier_route.py` |
| WAVE 2-B | document quarantine + evidence provenance (§27) | `docs/adr/0015-*`, `src/evidence.py` |
| WAVE 3-A | Appendix B references, guest master, inquiry, requirements | `docs/adr/0015-*`, `src/crm.py`, `src/references.py` |
| WAVE 3-B | matching on confirmed requirements; option sets | `docs/adr/0016-*`, `src/matching.py`, `src/option_sets.py` |
| WAVE 1-H | governance policy engine (§16): catalogue + versioned revisions, fail-closed resolution | `docs/adr/0017-*`, `src/policy_engine.py` |
| WAVE 1-I | AI service identity (§18/§56): principal + kill switch, §56 tool tiers, deny registry as policy | `docs/adr/0018-*`, `src/ai_service.py` |
| WAVE 4-A | availability state vocabulary (§7/§11, Appendix A): canonical results, derived state, explicit restricted state | `docs/adr/0019-*`, `src/availability.py` |
| WAVE 4-B | hold lifecycle (§7): one vocabulary + one transition writer, timezone on the expiry, two-step extension, expiry as `EXPIRED_PENDING_VERIFICATION` that keeps occupying until an authoritative source verifies it | `docs/adr/0020-*`, `src/hold_states.py`, `src/reservation_kernel.py` |
| WAVE 4-A-2 | check-availability request (§7): the request half of `CHECK AVAILABILITY` — append-only request + event log, one transition writer, `ANSWERED` kept out of the availability vocabulary, subject state precedence, and the `availability.request` §56 MEDIUM gate | `docs/adr/0021-*`, `src/availability.py`, `src/reservation_kernel.py` |
| WAVE 4-B-2 | hold-request authority (§7/§56): `request_hold()` takes a required identity, the `hold.request_tentative` MEDIUM tool gets its enforcement point, one shared `_require_tool_authority` for every §56 gate, and the `hold.requested` event names the caller | `docs/adr/0022-*`, `src/reservation_kernel.py` |
| WAVE 4-B-3 | §56 allowance decision (§56/§16): the owner's decision published as a recorded act — one tool (`availability.request`), a bounded window, `withdraw` effective the day it is issued, and the rule that the schema seeds no permission | `docs/adr/0023-*`, `scripts/publish_ai_allowance.py`, `tests/test_wave4b3_ai_allowance_decision.py`, `tests/test_ai_allowance_cli.py` |
| WAVE 4-B fix | demo seeder repaired — broken since WAVE 4-B (NOT NULL `expires_timezone`, replaced hold vocabulary, non-idempotent re-run, missing `data/`) and covered by `tests/test_seed_demo_cli.py`: the seeder had **no test at all**, which is why it broke silently | `scripts/seed_demo.py`, `tests/test_seed_demo_cli.py` |
| WAVE 4-B-4 | option-present authority (§56/§7): the `comm.client_selected` enforcement point on `present()`, one shared `require_tool_authority` for every §56 gate (pinned by an object-identity test), `MachineActorRefused` deleted rather than deprecated, and `present()` takes a required-for-machines `principal_id` | `docs/adr/0024-*`, `src/ai_service.py`, `src/option_sets.py`, `tests/test_wave4b4_option_present_authority.py` |
| WAVE 4-B-4 guard | a dead import now fails the suite (§10 quality gates): a standard-library `ast` check over `src/` and `scripts/` — it found the 3 imports this refactor left behind plus 5 more in modules the wave did not touch (`evidence.py`, `identity.py`, `supplier_route.py`) | `tests/test_no_dead_imports.py` |
| WAVE 4-C | quotation versioning + acceptance (§11): an immutable V1/V2/V3 chain over the stay, three snapshots (price/terms/policy) written at *send*, an acceptance naming §11's five references, the freeze and the append-only rules enforced by triggers, and the `quotation.send` §56 gate | `docs/adr/0025-*`, `src/quotation.py`, `src/schema/001_initial.sql`, `tests/test_wave4c_quotation_versions.py` |
| WAVE 4-D | discount engine (§11/§16/§15/§21): supplier-funded and agency-funded kept apart by *whose money moves*, authority and **both** margin floors read from published policy (percentage plus a new `commercial.margin_floor_minor` absolute key), a breach that moves nothing and escalates to a human approval, and an engine that exposes no way to approve anything | `docs/adr/0026-*`, `src/discounts.py`, `src/schema/001_initial.sql`, `tests/test_wave4d_discount_engine.py` |
| WAVE 4-D guard | a §21 DENY that matched nothing (§21): the registry named `discount.approve` while the catalogue registers `discount.approve_out_of_policy`, so `ai_service`'s verbatim tool-name comparison denied no tool at all — amended as revision 2 of `security.ai.denied_actions` and locked by a guard that maps every §21 capability to the tool carrying it and rejects any entry that denies nothing | `tests/test_deny_registry_covers_spec_denials.py`, `src/schema/001_initial.sql` |
| Integrity guard | a redactor that corrupted integrity values (§16/§21): `_PHONE_RE` carries no boundary, so about 1 sha256 in 250 was stored in the chain as `[PHONE]` — `publish_anchor` then refused to extend a healthy series and the tamper detector reported an untampered chain. Digests are stashed around redaction now, and the flakiness that exposed it (`make check` failing ~0.5% of runs, on a different anchor test each time) is gone | `docs/adr/0027-*`, `src/primitives.py`, `tests/test_primitives.py`, `tests/test_audit.py` |
| Owner act | the discount values are the owner's to decide, so they must be settable **by the owner** without a code change (§15 "thresholds are configuration", §16 lists `discount authority` and `margin floors`). A generic CLI reads the key's declared type from the catalogue, coerces the typed value, and demands the document it came from, the approver, and an explicit database; the engine keeps refusing until a value exists — it never defaults one | `scripts/publish_policy_value.py`, `tests/test_publish_policy_value_cli.py`, `README.md`, `Makefile` |
| WAVE 4-E | the absolute floor carries a currency (§11): one count cannot say what it is worth, so `commercial.margin_floor_minor` is denominated in a new `commercial.margin_floor_currency`, and a booking in any other currency has the floor converted through a new `commercial.fx.rates` table — published, never assumed. The conversion rounds a *minimum* up, refuses a float, an unknown ISO-4217 code, and a missing pair, and the decision records both the published number and the currency it became | `docs/adr/0030-*`, `src/fx.py`, `src/discounts.py`, `src/schema/004_currency_aware_margin_floor.sql`, `tests/test_fx.py`, `tests/test_wave4d_discount_engine.py` |
| WAVE 4-F | the rate is a day's price and has a birthday (§11): `scripts/refresh_fx_rate.py` fetches the day's real rate from a public provider and publishes it with the provider's own quote time as its provenance, refusing a payload with no quote time, a provider-reported failure, a foreign base, an inexact or non-positive rate, a quote the provider itself dates past the feed limit, a future-dated quote, and a move larger than the band. A new `commercial.fx.max_age_hours` (owner: 48) is the reader's limit: `src/fx.require_fresh` refuses a conversion on a rate older than that, applied in whole days with the tighter day winning, and an unpublished limit refuses every conversion | `docs/adr/0031-*`, `scripts/refresh_fx_rate.py`, `src/fx.py`, `src/discounts.py`, `src/schema/005_fx_rate_max_age_hours.sql`, `tests/test_refresh_fx_rate.py`, `tests/test_wave4d_discount_engine.py` |

Read a row's classification as "what the baseline found", and the ADRs as "what was decided".

## Executive assessment

Existing project is a strong deterministic reservation/finance/audit kernel, but it does **not** yet satisfy Master Spec V1 Production Core. Recommendation remains **CONDITIONAL GO for brownfield migration**, not production GO and not rebuild.

## Classification legend

| Class | Meaning |
|---|---|
| KEEP | Already aligned enough; preserve and wrap/extend safely. |
| REFACTOR | Useful implementation exists but must be reshaped for Master Spec. |
| REPLACE | Current implementation conflicts with Master Spec for production use. |
| REMOVE | Demo/obsolete/unsafe for production authority. |
| MISSING | Required by Master Spec but not present. |

## Module/component matrix

| Existing component | Master domain/requirement | Classification | Evidence | Required action |
|---|---|---:|---|---|
| `src/primitives.py` | Money fixed precision, refs, redaction | KEEP | README: integer minor units + ISO-4217; no float principle | Keep. Reuse for all new finance/supplier modules. |
| `src/schema_loader.py` | DB schema application/parity | KEEP / REFACTOR | Existing twin SQLite⇄PostgreSQL parity gate | Keep parity discipline; extend for future migrations. |
| `src/schema/001_initial.sql` | Physical DB baseline | REFACTOR | 34 tables, no `agency_id`, no supplier route, no outbox | Do not rewrite. Add migration sequence after tests. |
| `src/schema/002_pg_range_exclusions.pg.sql` | PostgreSQL concurrency/range control | KEEP | PG EXCLUDE constraint for overlap | Keep; extend predicate after parallel state migration. |
| `src/reservation_kernel.py` | Availability/Hold/Reservation integrity | KEEP / REFACTOR | Single inventory writer; overlap checks; evidence-required hold | Preserve anti-double-booking mechanics; refactor vocabulary/state model. |
| `src/booking_states.py` | State machine | REFACTOR / PARTIAL REPLACE | Single booking status graph; 32 statuses | Replace authority model with parallel states while preserving valid transition tests. |
| `src/policy_engine.py` | Policy Engine | KEEP / REFACTOR | Versioned policy object, exact percentages, windows | Keep; expand scope beyond property/unit to agency/supplier/contract policies. |
| `src/quote_calculator.py` | Pricing/quotation | REFACTOR | Current property/unit policy pricing | Add supplier-route, contract/rate version, snapshots, FX/tax policy hooks. |
| `src/gates.py` | Privacy/outbound gates | KEEP | Fail-closed data gates and classification | Keep; integrate into AI Privacy Gateway and API layer. |
| `src/audit.py` | Auditability | KEEP / REFACTOR | Hash chain, append-only, PII guard | Keep; add tenant scope, correlation IDs, API action/event links. |
| `src/anchors.py` | External audit anchoring | KEEP | Existing anchor publish/verify docs | Keep; formalize production WORM/secondary target. |
| `src/approvals.py` | Approval Engine | KEEP / REFACTOR | Dual control, self-approval blocked, derived status | Add policy_version_id, target_fingerprint, invalidation/revalidation semantics. |
| `src/invoicing.py` | Client invoices | KEEP / REFACTOR | Invoice + lines + policy totals | Keep for AR; add void/credit/replacement flow if missing. |
| `src/payments.py` | Client payment, webhook, ledger | KEEP / REFACTOR | HMAC webhook, ledger, reconciliation, refund approval check | Keep for client side; do not reuse as supplier payout engine without new AP controls. |
| `src/cases.py` | CRM/case management | KEEP / REFACTOR | Case records and SLA | Keep; map to Inquiry/CRM domain and public website intake. |
| `src/intake.py` | Rule-first extraction | KEEP / REFACTOR | Deterministic extraction/proposal pattern | Keep as non-AI baseline; wrap with AI proposal validator later. |
| `src/matching.py` | Villa matching | KEEP / REFACTOR | Explainable matching | Keep; add supplier route, verified fact provenance, restrictions. |
| `src/messages.py` | Safe outbound drafts | KEEP | Grounded, human-approved drafts | Keep; make official AI-safe messaging tool behind gateway. |
| `src/connectors.py` | Integrations/inbound/outbound | REFACTOR | HMAC WhatsApp, untrusted inbound; file outbox command | Keep inbound safety. Replace outbound reliability with transactional outbox. |
| `src/documents.py` | Document issuance | REFACTOR | Deterministic docs, path safety, P1 output | Add quarantine, malware scan, object storage metadata, signed access. |
| `src/ops_tasks.py` | Guest operations | KEEP / REFACTOR | Ops tasks exist | Extend to Master operations lifecycle and closure gate. |
| `src/incidents.py` | Incident management | KEEP / REFACTOR | Incident module exists | Add compensation workflow link, severity policy, closure blockers. |
| `src/scheduler.py` | Background jobs | REFACTOR | Idempotent heartbeat | Integrate with Exception Engine and outbox/dead-letter. |
| `src/observability.py` | Health/metrics | KEEP / REFACTOR | Metrics/health module exists | Expand to production observability, audit, restore drill signals. |
| `src/dashboard.py` | Internal UI | KEEP for internal/demo; REPLACE auth for production | Static roles + bearer token; read-only GET | Preserve UI discipline; put behind production auth/API. |
| `templates/*.html` | Internal web IA | KEEP / REFACTOR | 11 dashboard templates | Reuse for design language; migrate to production frontend later. |
| `demo/*` | Sales/demo artifacts | KEEP as demo; REMOVE from production authority | Mock app/video | Keep for sales only. Must not be live source of truth. |
| Public website | Required consumer interface | MISSING | No production public booking website found | Build after API/invariants. |
| Android/iOS native app | Optional later | MISSING / DEFER | No native app found | Start with responsive website/PWA; native after traction. |

## Master requirement coverage

| Master section/topic | Current coverage | Class | Notes |
|---|---|---:|---|
| Modular monolith | Strong | KEEP | Existing stdlib modular monolith fits. |
| PostgreSQL production | Partial | REFACTOR | PG schema twin exists, but production runtime adapter/deploy choices still ADR. |
| `agency_id` scoping | None | MISSING | Must be introduced before multi-agency/SaaS/client data. |
| Supplier / villa relationship | None | MISSING | Critical source-of-truth gap. |
| Contract/rate provenance | Partial | REFACTOR | Policy/rate exists, supplier-doc provenance incomplete. |
| Parallel reservation states | Partial/none | REPLACE model | Current single `bookings.status` conflicts with Master Spec. |
| Booking confirmation gates | Partial | REFACTOR | Need commercial + inventory + financial gates explicitly. |
| Race controls | Strong for inventory; partial globally | REFACTOR | Add quote/payment/bank/approval race tests. |
| Client finance | Strong partial | KEEP / REFACTOR | Current payment module is client-side oriented. |
| Supplier finance/AP | None | MISSING | Required before real supplier payouts. |
| Bank security/versioning | None | MISSING | Approval subject exists but not full model. |
| Approval engine | Strong partial | KEEP / REFACTOR | Add policy version, fingerprint, invalidation. |
| Policy engine | Strong partial | KEEP / REFACTOR | Expand into first-class cross-domain policy registry. |
| Exception engine | Partial via scheduler/SLA | MISSING / REFACTOR | Need deterministic exception engine. |
| RBAC/auth/MFA | Demo/internal only | REPLACE for production | Current bearer token dashboard not enough. |
| Data classification/privacy | Strong partial | KEEP / REFACTOR | Extend to API/AI/documents. |
| AI Gateway | None | MISSING | Must be built before AI tools. |
| AI deny registry | Partial by absence | MISSING | Must codify no forbidden capabilities. |
| API `/api/v1` | None | MISSING | Required before public website/mobile. |
| Transactional outbox | None | MISSING | Critical reliability gap. |
| Webhook/integration inbox | Partial | REFACTOR | Payment/connectors have idempotency but not unified inbox. |
| Document quarantine/storage | Partial | REFACTOR | Deterministic issue exists; quarantine/object storage absent. |
| Control Tower | Partial | REFACTOR | Dashboard exists but not full Master IA. |
| CI/CD quality gates | Partial | REFACTOR | `make check` strong; add contract/security/e2e/visual restore gates. |

## Critical findings to preserve during migration

1. **Do not flatten states.** Master explicitly forbids a generic one-status reservation model.
2. **Do not drop existing anti-double-booking controls.** Existing kernel is one of the strongest assets.
3. **Do not expose current dashboard auth to public internet.** It is an internal/demo control, not production MFA.
4. **Do not add AI as direct DB/tool executor.** AI must enter through Policy Gateway and narrow tools.
5. **Do not implement supplier payout on current client payment module.** Build AP controls separately.
6. **Do not migrate destructively.** Additive migrations + backfill + verification first.

## Reuse-first migration principle

Reuse current modules when they enforce a real invariant. Refactor only where the Master Spec requires a stronger domain model, state split, tenant scope, or production security boundary.

---

## V1.1 delta integration (2026-09-26)

**Authoritative document changed.** `villa-agency-os-hermes-master-specification-v1.1.html`
(sha256 `0751dfdc89968495c4707505a575ea077456fb72437cf1d0262d145339388da3`) now supersedes v1.0
for every decision in this repository (see ADR-0003 amendment).

The 12 findings v1.1 adds or changes are registered in **`docs/MASTER_SPEC_V1_1_DELTA.md`** with
the required format (requirement → existing condition → gap → change → test). This section only
records the *matrix consequence*; it deliberately does not repeat the audit already done.

### Classification changes caused by v1.1

| Area | Was (v1.0 audit) | Now (v1.1) | Why |
|---|---|---|---|
| Identity / MFA / sessions | MISSING | **KEEP** (built, ADR-0005 accepted) | WAVE 1 work landed: TOTP, dual-deadline sessions, step-up, append-only auth trail |
| Approval engine + audit chain | KEEP | KEEP | unchanged by v1.1 |
| RBAC role model | PARTIAL | **REFACTOR** | v1.1 §18 defines 12 roles; repo has 5 (`reservation`/`finance` merge Manager+Staff, which breaks maker-checker) |
| Production UI/validation language | not assessed | **REPLACE** | v1.1 §55 LOCKED en-US; 484 Indonesian literals in 10 modules + 15 template hits to translate (ADR-0011 ratchet, `tests/test_language_standard.py`) |
| Payment model | PARTIAL | **REFACTOR** | v1.1 §58/§61 add CARD/PAYPAL/STABLECOIN + provider-neutral adapters + canonical fields (ADR-0012) |
| Card handling | KEEP (`assert_no_card_data`) | KEEP + extend | §59 minimum PCI; extend guard to logs/error payloads |
| Stablecoin | MISSING | **MISSING (gated)** | §60 asset+network allowlist, verification sources, exception catalogue (ADR-0013); activation gated on compliance |
| Policy Engine | MISSING | **MISSING (now foundational)** | §16 + §50: WAVE 1 prerequisite for finance |
| Exception Engine | PARTIAL (`scheduler.py` sweeps are not it) | MISSING | §17: rules → task/alert/approval → role queue |
| Transactional outbox | MISSING | **MISSING (now foundational)** | §25 + §50 |
| AI Service identity + autonomy | MISSING | MISSING | §18/§56/§57; ADR-0009 amended with tiers, knowledge layer, extended deny registry |
| Reference patterns | PARTIAL (`INV-` only) | **REFACTOR** | Appendix B: INQ/QT/RSV/PR/INC/APR missing (ADR-0014) |
| Optimistic concurrency | PARTIAL | PARTIAL | §39/§23: `version` columns only on 5 tables + `bookings.version_lock`; 409 semantics absent |
| Internal id format | KEEP | KEEP with **recorded deviation** | prefixed tokens, not UUID/ULID (ADR-0014 §1) — cheap to reverse now, expensive later |
| Booking confirm/closure gates | PARTIAL | PARTIAL | §9/§30 gates + blocker lists not enforced |
| CI/CD quality gates | PARTIAL | PARTIAL | §42: lint/type/API-contract/migration/vuln/E2E/visual + restore drill missing |
| L4 field encryption | MISSING | MISSING | §19/§40 bank details/passport |
| Finance Lockdown + AI Kill Switch | MISSING | MISSING | §40 |

### Wave-order consequence

v1.1 §50 + the closing instruction ("security, audit, policy, authorization are foundational —
do not assume they will be added later") means: **Policy Engine, AI Service identity, the 12-role
RBAC model and the transactional outbox are prerequisites for WAVE 6 (Finance)**, and the
planned supplier-route work (WAVE 2) must incorporate the §12 provenance pipeline and §27
document quarantine rather than only the relationship/contract/rate skeleton.

### Wave 0 status

**CONDITIONAL GO** (unchanged in kind). Conditions: ADR-0011…0014 accepted; WAVE 1 foundational
items complete before WAVE 6; explicit owner approval for any payment/stablecoin/reference schema
change; and the en-US migration scheduled (a locked requirement currently unmet).
