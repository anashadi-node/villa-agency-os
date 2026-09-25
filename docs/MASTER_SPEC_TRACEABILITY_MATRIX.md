# Master Spec Traceability Matrix — Brownfield Audit Baseline

- Status: **Wave 0 baseline**
- Master source of truth: `/home/anashadi/Downloads/villa-agency-os-hermes-master-specification.html`
- Scope: compare existing `/home/anashadi/villa-agency-os` against Master Specification V1 Production Core
- Rule: this file is **audit/migration control**, not implementation.
- Coding/refactor/database migration: **NOT STARTED**.

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
