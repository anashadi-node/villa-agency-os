# W1 — Full-Stack Baseline Register (DRAFT, not approved)

**Status:** DRAFT for owner review. Nothing in this file is an approved requirement.
**Scope:** reconciliation only — it does not restate any BRD/PRD, ADR or the A–Z guide.
**HEAD at time of writing:** `b35412f` (local, unpushed) + the working-tree changes of W1-SAFETY.
**Why this file exists:** the recovery-derived matrix carried 42 IDs and was being read as if it
were the whole approved scope. It is one source among several, and a subset. This register keeps
the three views apart — DESIRED_BASELINE, AS_BUILT_STATE, RECONCILIATION_DELTA — so the
completion matrix can be used for full-stack delivery without inventing requirements.

## 1. Sources and their approval standing

| Source | What it establishes | Approval standing | How it was read |
|---|---|---|---|
| `docs/recovery-reference-v2.3/VILLA-AGENCY-RECOVERY-MASTER-EFFICIENCY-v2.3-2026-11-17.html` | 42 requirement IDs (24 FR, 6 NFR, 7 SEC, 5 UX) from the recovery programme | **RECOVERY-DERIVED, not the denominator** — owner 2026-11-18: "Jangan menganggap 42 merupakan jumlah final approved requirements" | parsed table rows |
| `docs/GO_LIVE_DECISIONS.md` | D1–D17 business decisions | D1–D14 + D17 **DECIDED** (owner 2026-10-02); **D15, D16 OPEN** | headings + decision rows |
| `docs/adr/` (72 files) | active architectural decisions, 0001–0072 | ADR = approved decision record; individual ADRs still need to be cited per capability | file index |
| `docs/PROJECT_VILLA_AGENCY_A-Z.md` | 26 narrative sections + appendices (72-ADR index, 60-module index) | **descriptive**, not an approval instrument; describes intent and current state | section headings |
| `docs/PROJECT_VILLA_AGENCY_AZ_2026-11-17.html` | newer A–Z rendering, **untracked** in the working tree | UNREVIEWED — must not be treated as approved scope | not read (protected file) |
| `docs/MASTER_SPEC_TRACEABILITY_MATRIX.md` | brownfield baseline, module matrix, v1.1 delta | audit baseline, carries no FR IDs | section headings |
| `docs/AI_WORKFORCE_TRACEABILITY.md` | AI workforce traceability | register | referenced |
| Repository + tests at `b35412f` | AS_BUILT_STATE | evidence, never approval | file and test inventory |

**No BRD or PRD exists in the repository.** Until one is supplied, every row below is grounded
in the sources above; where a capability has no numbered requirement it is an **OPEN item**, not
a new approved ID.

## 2. DESIRED_BASELINE × AS_BUILT_STATE

Evidence columns are file names actually present at `b35412f`. `tests` = number of test files
whose names match the capability (keyword match, **not** an acceptance-criteria audit).
`AS_BUILT_UNVERIFIED` means "code and tests exist, no AC-level verification has been performed".
No row is `VERIFIED_COMPLETE`.

| Group | Capability | AS_BUILT evidence (`src/`) | tests | State | Material gap | Proposed ID (OPEN) |
|---|---|---|---|---|---|---|
| Business | Villa supplier management | `supplier_route`, `supplier_settlements`, `villa_import`, `villa_showcase` | 4 | AS_BUILT_UNVERIFIED | supplier master data lifecycle (contract, rate card versioning) not audited | OPEN-W1-01 |
| Business | CRM & inquiry | `crm`, `cases`, `intake`, `matching`, `channels`, `messages` | 10 | AS_BUILT_UNVERIFIED | inquiry→booking conversion measured nowhere | OPEN-W1-02 |
| Business | Quotation & pricing | `quotation`, `quote_calculator`, `discounts`, `option_sets` | 4 | AS_BUILT_UNVERIFIED | pricing policy per season/channel not AC-verified | OPEN-W1-03 |
| Business | Availability | `availability`, `evidence`, `hold_states` | 2 | AS_BUILT_UNVERIFIED | truth-level ladder (PMS/owner/manual/iCal) not AC-verified | OPEN-W1-04 |
| Business | HOLD & booking | `reservation_kernel`, `reservation_service`, `hold_states`, `booking_states` | 7 | AS_BUILT_UNVERIFIED | SQLite proven; PG path skipped without `VILLA_PG_DSN` | OPEN-W1-05 |
| Business | Guest journey | `messages`, `crm`, `booking_states` | 2 | PARTIAL_IMPL | end-to-end guest journey (pre-arrival → post-stay) has no scenario test | OPEN-W1-06 |
| Business | Operations | `ops_tasks`, `operations_agent`, `control_tower`, `incidents` | 5 | AS_BUILT_UNVERIFIED | — | OPEN-W1-07 |
| Business | Booking closure | `booking_closure`, `deposit_resolutions`, `exception_engine` | 3 | AS_BUILT_UNVERIFIED | closure checklist AC not audited | OPEN-W1-08 |
| Application | Public website | `marketing_site`, `villa_showcase`, `geo` | 2 | AS_BUILT_UNVERIFIED | a11y/SEO/perf budgets unproven | OPEN-W1-09 |
| Application | Frontend UI/UX | `dashboard`, `marketing_site`, `control_tower` | 12 | AS_BUILT_UNVERIFIED | no a11y (contrast/keyboard) or visual-regression gate | OPEN-W1-10 |
| Application | Operator dashboard | `dashboard`, `control_tower` | 8 | AS_BUILT_UNVERIFIED | runs as `--role owner` in the live unit (see §4) | OPEN-W1-11 |
| Application | Reservation workspace | `reservation_service`, `hold_states`, `booking_closure`, `dashboard` | 7 | AS_BUILT_UNVERIFIED | — | OPEN-W1-12 |
| Application | Finance workspace | `finance_admin`, `invoicing`, `payments`, `deposit_resolutions` | 3 | AS_BUILT_UNVERIFIED | — | OPEN-W1-13 |
| Application | Tenant settings | `settings`, `agency_roles`, `villa_import` | 7 | AS_BUILT_UNVERIFIED | tenant readiness screen (D17) still listed as not built | OPEN-W1-14 |
| Application | API contracts | `dashboard`, `marketing_site`, `connectors` | 1 | PARTIAL_IMPL | no contract/schema test for the HTTP surface | OPEN-W1-15 |
| Application | Auth & RBAC | `identity`, `roles`, `agency_roles`, `approvals`, `totp`, `gates` | 7 | AS_BUILT_UNVERIFIED | — | OPEN-W1-16 |
| Finance | Invoice | `invoicing`, `money_display` | 1 | AS_BUILT_UNVERIFIED | numbering/tax fields AC not audited | OPEN-W1-17 |
| Finance | Payment verification | `payments`, `finance_admin` | 3 | AS_BUILT_UNVERIFIED | provider webhook replay/idempotency at AC level | OPEN-W1-18 |
| Finance | Supplier settlement | `supplier_settlements`, `supplier_route` | 1 | AS_BUILT_UNVERIFIED | settlement statement reconciliation with owner not AC-verified | OPEN-W1-19 |
| Finance | Security deposit | `deposit_resolutions`, `payments` | 1 | AS_BUILT_UNVERIFIED | — | OPEN-W1-20 |
| Finance | Reconciliation | `finance_admin`, `payments`, `connectors` | 2 | AS_BUILT_UNVERIFIED | ≥99% reconciliation target never measured | OPEN-W1-21 |
| Finance | Financial approval | `approvals`, `gates` | 2 | AS_BUILT_UNVERIFIED | — | OPEN-W1-22 |
| Finance | Bank-change approval | `approvals`, `settings` | 1 | PARTIAL_IMPL | dual-control on bank detail change has no dedicated test | OPEN-W1-23 |
| Finance | Accounting | `finance_admin`, `invoicing`, `fx` | 7 | PARTIAL_IMPL | ledger posting rules never audited against an accountant's chart | OPEN-W1-24 |
| AI Workforce | Orchestrator | `ai_service`, `ai_gateway`, `control_tower`, `gates` | 4 | AS_BUILT_UNVERIFIED | — | OPEN-W1-25 |
| AI Workforce | Commercial agent | `crm`, `quotation`, `discounts`, `matching` | 2 | PARTIAL_IMPL | shadow-mode agreement rate (D7) never measured on ≥100 cases | OPEN-W1-26 |
| AI Workforce | Reservation agent | `reservation_service`, `availability`, `hold_states` | 14 | AS_BUILT_UNVERIFIED | — | OPEN-W1-27 |
| AI Workforce | Operations agent | `operations_agent`, `ops_tasks` | 3 | AS_BUILT_UNVERIFIED | — | OPEN-W1-28 |
| AI Workforce | Finance administration agent | `finance_admin`, `payments`, `supplier_settlements` | 10 | AS_BUILT_UNVERIFIED | no AI-may-not-pay test at the transfer boundary | OPEN-W1-29 |
| AI Workforce | Revenue / BI | `revenue_bi_agent`, `reservation_kpi`, `observability` | 6 | AS_BUILT_UNVERIFIED | forecast accuracy never evaluated | OPEN-W1-30 |
| AI Workforce | Security-agent capabilities | `audit`, `anchors`, `shadow_eval`, `incidents` | 13 | AS_BUILT_UNVERIFIED | — | OPEN-W1-31 |
| Infrastructure | SQLite | `schema_loader`, `reservation_kernel` | 4 | AS_BUILT_UNVERIFIED | WAL-reset exposure, see §4 | OPEN-W1-32 |
| Infrastructure | PostgreSQL | `schema_loader`, `reservation_kernel` | 1 | PARTIAL_IMPL | **AUD-04**: `connect_pg(apply=True)` applies `001_initial.pg.sql` only; migrations 002+ have no PG parity | OPEN-W1-33 |
| Infrastructure | Database migrations | `schema_loader` | 2 | AS_BUILT_UNVERIFIED | SQLite-only runner; PG migration path missing | OPEN-W1-34 |
| Infrastructure | CI/CD | `.github/workflows/check.yml` | 0 | PARTIAL_IMPL | one workflow; PG job lives off `master`; last verified run on `28a2bcd`, not on HEAD | OPEN-W1-35 |
| Infrastructure | Tenant isolation | `agency_roles`, `identity`, `settings` | 6 | AS_BUILT_UNVERIFIED | cross-tenant leakage not fuzz-tested | OPEN-W1-36 |
| Infrastructure | Security | `gates`, `identity`, `totp`, `audit` | 7 | AS_BUILT_UNVERIFIED | — | OPEN-W1-37 |
| Infrastructure | Privacy / DLP | `documents`, `messages`, `outbox` | 3 | AS_BUILT_UNVERIFIED | retention deletion job (D9, 7 years) not built | OPEN-W1-38 |
| Infrastructure | Backup / restore | `restore_proof` | 1 | PARTIAL_IMPL | RPO 24h vs `BACKUP_MAX_AGE_HOURS=26`; restore cadence 3-monthly unproven | OPEN-W1-39 |
| Infrastructure | Monitoring | `observability`, `control_tower`, `scheduler` | 4 | AS_BUILT_UNVERIFIED | alert→on-call routing (D13) unproven | OPEN-W1-40 |
| Infrastructure | Disaster recovery | `restore_proof`, `observability` | 4 | PARTIAL_IMPL | no DR drill record; no RTO measurement | OPEN-W1-41 |

## 3. RECONCILIATION_DELTA (material only)

1. **Denominator.** 42 recovery IDs ≠ approved full-stack scope. Rows §2 are the scope the owner
   listed; most have no numbered requirement. They stay OPEN until the owner either approves a
   proposed ID or supplies the BRD/PRD.
2. **PostgreSQL.** The kernel and schema support PG, but the schema applier is `001`-only, so a
   fresh PG deployment cannot be built from the SQLite migration chain. This is a delivery
   blocker for any PG production claim, not a test problem.
3. **Tenant readiness (D17).** DECIDED, and its artifact (a readiness screen + preflight check)
   is still not built.
4. **Open decisions.** D15 (mixed-source evidence) and D16 (unit count and rate card) are OPEN;
   they gate real inventory, not code.
5. **Coverage shape.** Test counts above are keyword matches. They show presence, not acceptance.
   The cheapest material gain is one end-to-end scenario per business group, not more unit tests.
6. **CI.** The only workflow is `check.yml`; the PG run that passed was on `feat/fr904-pg-ci`
   (`28a2bcd`). Neither `0a2836e` nor `b35412f` has ever run in CI.

## 4. Operational findings (read-only, 2026-11-18)

| Item | Observed | Standing |
|---|---|---|
| `villa-dashboard.service` | `/usr/bin/python3 src/dashboard.py --db …/data/villa.db --port 8085 --role owner --token-file ~/.config/villa/dashboard.token`, `WorkingDirectory` = repo, started 06:25:59 | **privilege concentration**: the staff dashboard runs with the highest role instead of the operator's own role; bind is loopback (not exposed) |
| `villa-marketing.service` | `/usr/bin/python3 src/marketing_site.py --db …/data/villa.db --port 8090`, loopback | public-site process holds the operational DB |
| Runtime | Python 3.14.4 / SQLite 3.46.1 (`make interpreter`) | Makefile's comment still claims 3.12.3 / 3.45.1 — stale |
| WAL-reset bug | fixed in SQLite **3.51.3** (2026-03-13, sqlite.org/releaselog/3_51_3.html); runtime is 3.46.1 | **exposure exists in principle**; sqlite.org calls it rare ("less than or equal to SSD malfunction rates") and requires a checkpoint/commit race. No upgrade performed — needs a separate approved change window |
| Token file mode | not inspected | UNKNOWN |
| Network exposure beyond loopback | none observed (`127.0.0.1` in both descriptions) | loopback only, as of the unit files |

## 5. What this register does not claim

- It does not approve any requirement, and it does not close any gap.
- It does not certify AC-level correctness; every `AS_BUILT_UNVERIFIED` row needs its own
  evidence before a release gate can cite it.
- It does not assert that no undiscovered defect exists.

## 6. Stage B vertical slice — Reservation Workspace (2026-11-18)

One coherent frontend→backend slice inside the approved surface (`src/dashboard.py`,
`templates/inventory.html`, `tests/test_dashboard.py`): the operator sees inventory, picks a
unit and dates, names the availability evidence, and the **existing** `ReservationKernel`
decides. No second kernel, no new business rule, no automatic payment confirmation, and no
schema, permission, financial-authorization or banking change.

| Acceptance | Test (`tests/test_dashboard.py`) | Status |
|---|---|---|
| A. Authorized operator completes a valid HOLD | `test_authorized_operator_requests_a_hold` | PASS |
| B. Unauthorized role is denied | `test_hold_is_denied_for_a_read_only_role` | PASS |
| C. Cross-tenant operation is denied | `test_hold_refuses_a_unit_from_another_tenant` | PASS |
| D. Expired session is denied | `test_expired_session_cannot_reach_the_hold_route` | PASS |
| E. Missing/invalid form token is denied | `test_hold_requires_a_form_token_from_this_process_and_this_principal` | PASS |
| F. Invalid dates rejected | `test_hold_rejects_invalid_dates` | PASS |
| G. Stale availability evidence rejected | `test_hold_rejects_stale_evidence` | PASS |
| H. Overlapping reservation rejected | `test_hold_rejects_an_overlapping_booking` | PASS |
| I. Duplicate submission is idempotent | `test_hold_retry_is_idempotent` | PASS |
| J. Failed operation creates no partial state | `test_refused_hold_writes_nothing_anywhere` | PASS |
| K. UI shows the states it can show | `test_hold_form_is_rendered_for_a_writer_and_names_no_unsupported_action` | PASS — form, success banner (`role="status"`), refusal banner (`role="alert"`); no client script exists, so there is no "loading" state to fake (CSP `default-src 'none'`) |
| L. Every write carries audit evidence | asserted inside `test_authorized_operator_requests_a_hold` (`audit_events` row `hold.requested`, actor = principal identity) | PASS |
| M. Reservation Kernel tests remain valid | `tests/test_reservation_kernel.py` (73 tests) unchanged and green | PASS |
| N. Dashboard regression passes | `tests/test_dashboard.py`, `tests/test_dashboard_tenant_scope.py`, `tests/test_dashboard_session.py` | PASS |

Evidence for the run: 378 tests (incl. subtests), **0 failed, 0 errors, 0 skipped**, 125.5 s,
`RC=0`, `--basetemp` inside the approved scratch dir, re-run after the banner was moved to a
human label (`_time_label`) with an assertion guarding it. PG-live paths were not exercised
here (no `VILLA_PG_DSN`); the PostgreSQL evidence for this commit is the Stage A CI run.

What this does **not** claim: no requirement in §2's register is promoted to
`VERIFIED_COMPLETE` by this slice; the candidate OPEN items stay OPEN until their own evidence
exists, and the strict PG HOLD-vs-BOOKING XFAIL (`tests/test_pg_overlap_concurrency.py:756`)
remains unresolved.

## 6.1 Stage C1 — Booking Conversion Workspace (HOLD → PAYMENT_PENDING)

Scope: convert an eligible live HOLD into a booking whose status is `PAYMENT_PENDING`, through
the application UI, by an authorized reservation operator. The slice ends there: payment
verification (`mark_paid`) and final confirmation (`confirm_booking`) are separate controlled
actions and are **not** executed by this code. No new state machine, no direct
`HOLD → CONFIRMED` path, no change to the Reservation Kernel, payment services, financial
approval policy, or database schema.

| # | Acceptance | Test | Result |
|---|---|---|---|
| A. An authorized operator converts a live hold | `test_authorized_operator_converts_a_live_hold` | PASS — `data-banner="booking-reserved"`, status `PAYMENT_PENDING` |
| B. The result is not CONFIRMED and no money moved | asserted in A (`payment_status = UNPAID`, `amount_paid_minor = 0`, `confirmed_at IS NULL`) | PASS |
| C. The hold keeps blocking the slot | asserted in A (hold row still `CONFIRMED`) | PASS |
| D. Read-only auditor cannot convert | `test_read_only_role_cannot_convert_a_hold` | PASS — `403` |
| E. Another agency's hold is not convertible | `test_a_hold_from_another_agency_is_not_convertible` | PASS — kernel `HOLD_NOT_FOUND` |
| F. Missing cookie is unauthenticated | `test_conversion_requires_a_session` | PASS — `401` |
| G. Wrong form token is refused | `test_conversion_requires_the_page_form_token` | PASS — `403 FORM_TOKEN_INVALID` |
| H. A released hold is not convertible | `test_a_released_hold_cannot_be_converted` | PASS — `HOLD_NOT_ACTIVE` |
| I. An expired hold is not convertible | `test_an_expired_hold_cannot_be_converted` | PASS — `HOLD_ALREADY_EXPIRED` |
| J. A quote that was never accepted is refused | `test_a_quote_that_was_not_accepted_is_refused` | PASS — `CONFIRM_NOT_ACCEPTED` |
| K. A quote for another unit is refused | `test_a_quote_for_another_unit_is_refused` | PASS — `QUOTE_UNIT_MISMATCH` |
| L. A missing quote is refused, not invented | `test_a_booking_without_a_quote_is_refused` | PASS — `INVALID_INPUT` |
| M. Duplicate submission is idempotent | `test_conversion_retry_is_idempotent` | PASS — one booking row, `data-replayed="1"` |
| N. Refusal writes nothing anywhere | asserted across `bookings`/`invoices`/`payments`/`holds` | PASS |

Language contract: every new user-facing literal in this slice is en-US (ADR-0011 §55);
`dashboard.py` markers **159 → 156** and `inventory.html` **1 → 0**, with the ratchet pins
lowered to the measured values rather than the numbers raised.

Evidence: 389 tests (incl. subtests), **0 failed, 0 errors, 0 skipped**, 136.2 s, `RC=0`; the
language gate `tests/test_language_standard.py` → 7 passed; `compileall` and `pyflakes` clean.
PG-live paths were not exercised locally (no `VILLA_PG_DSN`); the PostgreSQL evidence for this
commit is the CI run on branch `feat/villa-fullstack-integration`.

What this does **not** claim: nothing here promotes an OPEN register item to `VERIFIED_COMPLETE`;
the strict PG HOLD-vs-BOOKING XFAIL (`tests/test_pg_overlap_concurrency.py:756`) remains an
unresolved production risk, and FR-904 is not production complete.
