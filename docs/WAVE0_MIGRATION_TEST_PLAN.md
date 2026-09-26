# Wave 0 Migration Test Plan — Before Any Refactor

- Status: **Required before coding/migration**
- Source of truth: Master Specification HTML in `/home/anashadi/Downloads/`
- Purpose: define tests that must exist before changing architecture.

## Rule

No migration/refactor may start until the relevant regression/negative tests exist. The goal is to protect working invariants while upgrading to the Master Spec.

## Baseline command set

```bash
make check
python3 -m pytest tests/ -q --tb=short
python3 scripts/generate_pg_twin.py --check
bash scripts/secret_scan.sh
bash scripts/unsafe_config_scan.sh
```

## Test categories required by Master Spec

| Category | Must prove | Priority |
|---|---|---:|
| Tenant scoping | user/agency cannot read or mutate another agency's records | P0 |
| Supplier route | reservation cannot confirm without explicit villa-supplier relationship | P0 |
| Parallel states | commercial/availability/hold/booking/client payment/supplier payment/ops states move independently | P0 |
| Booking gates | no BOOKING_CONFIRMED without commercial + inventory + financial gates | P0 |
| Hold lifecycle | REQUESTED ≠ CONFIRMED; release requested ≠ released; expired requires verification | P0 |
| Bank profile | bank changes require out-of-band verification + human approval + version activation | P0 |
| Payment request | payout target is fingerprint-bound; changed target invalidates approval | P0 |
| Supplier invoice | mismatch blocks payment request | P0 |
| Outbox | state mutation and event insert are atomic; duplicate publish is idempotent | P0 |
| Auth/MFA | Owner/Finance/System Admin require MFA; high-risk action requires step-up | P0 |
| AI deny | AI cannot approve/execute payment/refund/bank/permission/audit/secret/passport actions | P0 |
| Privacy | L4 data never reaches logs/browser storage/AI context | P0 |
| API | semantic commands reject generic status patching and stale writes | P1 |
| Public preview | no cost/margin/internal notes in client/public response | P1 |
| Document quarantine | unscanned external file cannot become trusted evidence | P1 |
| Restore drill | backup restore succeeds and critical workflows run | P1 |

## Regression tests to freeze existing good behavior

| Existing invariant | Current module | Test requirement before refactor |
|---|---|---|
| No double booking | `reservation_kernel.py`, schema indexes | Keep current overlap tests green; add supplier-route variant. |
| Cancellation review holds slot | `booking_states.py`, schema, kernel | Keep ADR-0002 tests; map to new parallel state. |
| Money uses integer minor units | `primitives.py`, finance modules | Add migration tests rejecting float/Decimal storage drift. |
| Audit append-only hash chain | `audit.py` | Verify tenant/correlation additions preserve chain. |
| Webhook replay safety | `payments.py` | Duplicate provider event creates one logical result. |
| Signature rejection moves no money | `payments.py` | Invalid webhook recorded but unapplied. |
| Approval dual control | `approvals.py` | Self-approval and one-approver payout/bank_change blocked. |
| Untrusted messages are data | `connectors.py` | Prompt-injection text cannot mutate status/price/booking. |
| Grounded messages | `messages.py` | Ungrounded/unapproved draft cannot send. |

## Migration-specific test plan

### 1. `agency_id` migration tests

- Create two agencies.
- Seed same human-readable reference under different agencies.
- Assert scoped query returns only own agency.
- Assert cross-agency mutation returns FORBIDDEN.
- Assert all primary business records get non-null `agency_id` after backfill.

### 2. Supplier route tests

- A villa with two suppliers must produce two possible routes.
- A reservation must store `villa_supplier_relationship_id`.
- A supplier route change requires reprice + revalidation + new snapshot.
- Quote cannot use expired/unverified rate as guaranteed truth.

### 3. Parallel state migration tests

- Map every old `bookings.status` to a valid state tuple.
- Reject ambiguous legacy rows into manual review, not silent conversion.
- Verify payment can be FUNDS_RECEIVED while hold is EXPIRED_PENDING_VERIFICATION without confirming booking.
- Verify client accepted quote after inventory change results in INVENTORY_RECONFIRMATION_REQUIRED.

### 4. Bank/payment target tests

- Pending bank change does not affect existing payment request.
- Active bank profile version is immutable historically.
- Approval bound to fingerprint becomes INVALIDATED after amount/currency/beneficiary changes.
- AI/system actor cannot approve payment request.

### 5. Transactional outbox tests

- Domain mutation and outbox insert commit together.
- Inject failure after state write before commit; assert neither state nor event persists.
- Publishing same event twice is idempotent.
- Notification failure does not rollback booking confirmation.

### 6. AI Gateway tests

- AI context excludes raw passport/ID/bank details by default.
- Prompt asking to approve payment returns DENIED at tool/API level.
- Prompt asking to reveal secret returns DENIED and audited.
- Safe tool output is validated by domain command before any write.

## Required evidence format

For every critical requirement:

```text
REQUIREMENT → FAILURE/THREAT MODEL → CONTROL → TEST → OBSERVABLE EVIDENCE
```

Example:

```text
AI cannot approve payment
→ prompt injection / agent overreach
→ no approve tool + API denies AI principal
→ adversarial test calls payment.approve as ai_service
→ DENIED + audit event observed
```

## Exit criteria for Wave 0

- ADRs accepted or explicitly revised by owner.
- Traceability matrix reviewed.
- Migration test plan accepted.
- No source-code refactor started before approval of the first implementation wave.

---

## V1.1 additions (2026-09-26)

New test categories the migration plan must carry, from `docs/MASTER_SPEC_V1_1_DELTA.md`.
Each entry names the failure it catches, not just the happy path.

### 1. Language standard (ADR-0011)
- Ratchet guard: no production surface (`templates/*.html`, `src/` string literals) may gain new
  Indonesian text; the legacy set can only shrink.
- Error `code` values are unchanged by translation (the machine contract must not move).

### 2. Payment rails (ADR-0012, §61)
- Duplicate card webhook and duplicate PayPal webhook → exactly one financial entry.
- Unallocated bank receipt; partial payment; overpayment is not automatic revenue.
- Provider/browser success without verified settlement → **no** `FUNDS_RECEIVED`.
- Rail outage → payment pending/failed while the booking business transaction survives
  (`SOURCE_UNAVAILABLE` must not become `PAYMENT_FAILED` or `NOT_AVAILABLE`).
- Card data through any write path → refused, nothing persisted, logs clean.
- Adapter isolation: no provider SDK import inside `payments.py` or any domain module.

### 3. Stablecoin (ADR-0013, §60)
- Wrong network / wrong token / wrong amount → exception row, no settlement.
- Destination change after approval → approval invalidated, no silent redirect.
- Duplicate on-chain transaction → one financial entry.
- Screenshot as evidence → not accepted.
- AI sign/transfer/withdraw capability → absent (adversarial suite), not merely refused.

### 4. AI autonomy and knowledge layer (ADR-0009 amendment, §56/§57)
- Conversation instructions cannot raise privilege.
- Human takeover pauses conflicting autonomous outbound actions and preserves case context.
- Unverified villa fact → agent clarifies/verifies instead of asserting.
- AI outage → booking, payment recording and approval keep working.

### 5. Identifiers, references, concurrency (ADR-0014)
- Reference format per entity (`INQ-/QT-/RSV-/INV-/PR-/INC-/APR-YYYY-NNNNN`), unique per agency
  per year, independent sequences between agencies.
- Stale write → `409 CONCURRENT_MODIFICATION`, stored row unchanged.
- Money round-trip decimal string ↔ integer minor units, lossless, multi-currency.

### 6. Foundations v1.1 promotes to blocking (§16, §25, §39)
- Policy version pinned into every decision; historical approval keeps its `policy_version_id`.
- Outbox event written in the same transaction as the business state change; consumer replay is
  idempotent.
- L4 fields (bank details, identity documents) unreadable without authorization and absent from
  logs, URLs and analytics payloads.

### Evidence format (unchanged)

Every one of these must be demonstrated by a test that fails when the control is removed —
`REQUIREMENT → THREAT/FAILURE → CONTROL → TEST → OBSERVABLE EVIDENCE` (§47).
