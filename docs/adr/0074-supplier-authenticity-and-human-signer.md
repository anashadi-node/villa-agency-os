# 0074: Supplier authenticity and the human signer, contract-first

- Status: PROPOSED (implementation checkpoint E5_LOCAL_COMMITTED_REVIEW_PENDING —
  owner approved the reconciliation in §10 for the development baseline; independent
  review and final approval are still outstanding. Not accepted, not production-ready.)
- Date: 2026-11-20
- Builds on: ADR-0073 (proof is booking-scoped, human-verified, audited, idempotent),
  ADR-0035/0036 (maker-checker, approvals), ADR-0072 (session cookie carrier),
  master spec §7 (CHECK AVAILABILITY), §12 (provenance), §18 (approvals)
- Requirement anchors: extends the PROPOSED FR-905 / DATA-214 / SEC-131 / TEST-402
  family from ADR-0073. No approved requirement is renumbered; new IDs are also
  PROPOSED and listed in §7.

## Problem

ADR-0073 made the supplier block proof booking-scoped, human-verified, audited and
idempotent (E3). Two gaps remain, both found in E3 and left open on purpose:

1. **Nothing ties an answer to a supplier.** `availability_evidence` records
   `agency_id`, `unit_id`, level, `source_type` and a free-text `source_ref` — no
   supplier, no relationship. "This answer came from *this* supplier" cannot be
   established from the data, so a generic AVAILABLE answer and a supplier selection
   can be combined into an apparently authoritative block.
2. **Any named human role can sign.** `roles.is_named_human_role` (the E3 kernel gate)
   accepts every canonical human role, including `auditor` and `finance_manager`. And
   `reservation_manager` is not in `identity.MFA_REQUIRED_ROLES`
   ("owner", "finance_manager", "gm"), so the signer role the owner proposes can log
   in with a single factor today.

This ADR proposes the smallest contract that closes both, for owner approval. It is a
design document: no schema, permission, or MFA change is authorized by it.

## §1 Supplier authenticity contract

The trusted chain, end to end:

```
REGISTERED SUPPLIER (suppliers, per agency)
→ REGISTERED CONTACT (new: supplier_contacts; one verified channel per supplier)
→ EXPLICIT BOOKING-SPECIFIC BLOCK CONFIRMATION
   (identifies supplier reservation ref or booking ref + unit + stay dates
    + an explicit commitment to block)
→ SOURCE EVIDENCE (availability_evidence, attributed)
→ SUPPLIER RELATIONSHIP (villa_supplier_relationships, ACTIVE, same unit)
→ BOOKING (supplier_block_proofs, one live proof per booking — ADR-0073)
→ AUTHORIZED HUMAN VERIFICATION (reservation_manager, MFA session)
```

Rules that make the chain real:

- **R1 — One verified contact per supplier.** A supplier has contacts; exactly one may
  carry `verified`. Verification is an out-of-band callback to the *stored* number —
  never a number supplied inside the incoming message. The stored contact is the
  trust anchor; everything downstream inherits it.
- **R2 — Booking-specific assertion.** A usable confirmation must contain (a) the
  supplier's reservation reference or our booking reference, (b) the unit, (c) the
  stay dates, and (d) an explicit statement that the supplier blocks the unit for
  those dates. Two of four is not a confirmation.
- **R3 — Attribution is captured at recording time.** `record_evidence` gains
  attribution parameters; whoever records an attributed answer names supplier,
  relationship, and source contact in the same write. There is no post-hoc
  attribution: an old row cannot be "claimed" later, because there is no path that
  mutates evidence (rows are immutable except nothing — evidence has no revoke
  column; supersession is expressed by recording a new row).
- **R4 — The structured block fields are the proof, not `result` and not free text.**
  `NOT_AVAILABLE` means "occupied or unsellable", not "held for us" — a truthful answer
  about somebody else's stay would satisfy it. What the kernel matches is the
  structured block assertion carried on the attributed evidence (§2): the supplier's
  `block_ref`, `block_start`/`block_end`, and `supplier_confirmed_at`, compared to the
  booking's unit and stay. `result` only gates out generic AVAILABLE answers
  (`BLOCK_PROOF_EVIDENCE_NOT_A_BLOCK`); "matching restrictions" as free text is never
  compared. An attributed row must additionally have `source_type` in
  (`manager_written`, `manual_ops`, `property_api`) and `evidence_level` in (A,B) —
  `memory`/`ical` answers and level C/D cannot carry a block claim, and a row carrying
  block fields must not claim `can_confirm = 1` (a block is not a sellable answer).
- **R5 — Freshness.** A block confirmation decays. The evidence's existing
  `valid_until` applies as today; in addition the *proof* already carries `expires_at`
  (E3). Proposed default: a confirmation is only usable while
  `captured_at <= now <= valid_until` and the recorded block confirmation is at most
  `max_block_confirmation_age_hours` (default 168 h, per-relationship overridable
  later) old. Exact default is an owner decision (§9).
- **R6 — Failure states.** Unregistered source → the evidence may be recorded as
  unattributed (readable, never authorizing); supplier/source mismatch,
  relationship mismatch, suspended relationship, and expired confirmation are all
  *refusals at proof time* with distinct kernel codes (§7), not silent rejections.
- **R7 — Privacy.** Contact values are personal data (UU PDP): `supplier_contacts`
  stores the minimal value needed to call back, marks it `sensitive`, is excluded from
  exports by the existing data-classification policy, and is only readable by
  `partnership.write` holders. No contact data in logs or audit payloads — audit rows
  reference `contact_id`, never the number.
- **R8 — Pilot channel.** Written confirmation from the verified contact **plus** one
  independent callback to the stored contact details, recorded by a human. The pilot
  makes both steps explicit fields in the evidence notes template and in the
  recording UI copy ("verified by callback to registered contact on …").

Long-term (not pilot): authenticated supplier portal, then authenticated supplier
API. In both, the *channel* is the authentication and R2 still applies; the contract
above is written so `source_kind` grows from `CALLBACK_VERIFIED` to `PORTAL` / `API`
without schema redesign.

## §2 Attribution schema design (additive, minimum)

> **As built (E5, 2026-11-20):** the attribution contract is carried by a separate
> table `evidence_supplier_attributions` rather than by nullable columns on
> `availability_evidence`. The contract below still holds — all-or-none, immutable,
> no post-hoc attribution, legacy rows untouched — but the mechanism differs. §10
> records the deviation and the owner decision that approved it. Read §10 before
> reading this section as a description of the current schema.

Three changes, all additive and nullable. Two foreign keys alone do **not** establish
authenticity (the brief is right); the third column plus the recording-time rule R3
are what bind a source to a supplier.

1. **New table `supplier_contacts`** (SQLite + PG twin):
   `contact_id PK`, `agency_id FK`, `supplier_id FK`, `contact_name`, `channel`
   (CHECK in `phone_wa`,`phone_voice`,`email`,`portal`), `contact_value` (NOT NULL,
   sensitive), `verified_at` nullable, `verified_by` nullable (human actor),
   `status` (`ACTIVE`/`RETIRED`), `created_at`, `updated_at`.
   Partial unique index: one `verified` ACTIVE contact per `(agency_id, supplier_id)`.
2. **`availability_evidence` + 4 nullable columns:** `supplier_id`,
   `villa_supplier_relationship_id`, `supplier_contact_id` (all FK), and
   `source_kind` (CHECK in `CALLBACK_VERIFIED` only for now, nullable), and the
   **structured block assertion** — `block_ref`, `block_start`, `block_end`,
   `supplier_confirmed_at` (when the supplier gave the commitment — distinct from
   `captured_at`, which is when *we* wrote the row), `callback_done_at`,
   `callback_done_by` — all nullable, all-or-none with the attribution triple, and
   `contact_verified_at` **snapshotted** onto the row at INSERT.
   CHECK: either all attribution fields are NULL (legacy/unattributed) or all NOT NULL.
2b. **Immutability is enforced by the database, not by hoping.** BEFORE UPDATE
   triggers (SQLite + PG) reject any change to the attribution columns, the block
   fields, and `created_at` on `availability_evidence`; on `supplier_contacts`,
   `contact_value` and `supplier_id` are immutable — a new number means retire the old
   contact and insert a new one — and contact *verification history* is an append-only
   child table rather than an overwritten column. Without triggers, "no post-hoc
   attribution" is only a code-path promise: a nullable column and a plain UPDATE
   would relabel legacy evidence. The proof-time check also requires the snapshot to
   hold: `contact_verified_at` on the row must be non-null and the contact must still
   be ACTIVE, so verifying a contact *after* the fact cannot retroactively bless
   evidence.
2c. **Agency-safe joins.** `suppliers` gains `UNIQUE (agency_id, supplier_id)`;
   relationships and contacts gain the same shape; the evidence foreign keys become
   composite so a single-column FK cannot pair a row with another agency's supplier.
   The kernel additionally requires `contact.supplier_id = relationship.supplier_id =
   evidence.supplier_id`, and a relationship with NULL `unit_id` (property-wide)
   **never** authorizes a block — only an exact unit match does.
3. **No writer change to old rows.** There is no backfill. A row written yesterday
   stays exactly as it is: readable, visible in the workspace, and — under the new
   kernel rule — unable to start a *new* proof. Nothing is relabeled as verified.

Authoritative writer: the kernel remains the only writer; attribution is captured
through `record_evidence` parameters. Contact registration is `partnership.write`
(the Partnership Manager owns supplier relationships and registered contacts, per the
owner's direction); proof signing is `reservation_manager` only (§3).

Tenant constraints: every new column/table carries `agency_id` and joins through the
agency-scoped parents, same as every existing table.

Revocation: a contact is `RETIRED` (never deleted — audit chain intact); an
attributed evidence row is immutable, so "revoking" a claim means recording a
contradicting row and revoking the proof (E3 path). Freshness is R5.

Migration & parity: SQLite migration `020_supplier_contacts.sql`; PostgreSQL twin in
`001_initial.pg.sql` for fresh DBs plus an additive PG migration for existing ones —
the PG migration runner itself is a standing blocker (§9). Rollback/backout: all
changes are nullable and additive; the backout is "stop setting attribution", with no
data loss and no destructive DDL. No operational DB is touched by design work.

Legacy compatibility, restated as the owner's rule: historical evidence without
verified provenance **may remain readable but must not authorize NEW booking
confirmations** — and that includes *existing proofs* recorded under the E3 contract
on unattributed evidence: from a dated cutover (a policy constant, proposed), the
confirmation gate refuses them with `BLOCK_PROOF_EVIDENCE_UNATTRIBUTED` until a
re-verification records an attributed replacement. No historical row is silently
relabeled; the alternative "keep old proofs valid until natural expiry" was withdrawn
after review because it contradicts the owner's rule.

## §3 Human signer contract

- Normal signing role: **`reservation_manager`** (canonical id; page word
  `operations`). Owner retains oversight but exceptional signing needs a separate
  explicit decision — so the allowlist is exactly `{reservation_manager}`; owner is
  *not* in it by default.
- No signing authority: `reservation_agent`, `finance_manager`, `auditor`,
  `system_admin`, AI/service principals, `partnership_manager`, any other human role.
  Partnership Manager manages suppliers and contacts; it does not sign confirmations.
- **Identity-to-role:** the session must carry the canonical role bound to a
  per-person identity (`human:<identity>` provenance, E3); a caller-supplied
  `actor`/`actor_role`/`verified_by` string is not identity (E3 already binds these
  to the kernel principal; the allowlist is checked *on top*).
- **Authentication strength — verified conflict, and what "smallest" really is:**
  `reservation_manager` is NOT in `MFA_REQUIRED_ROLES` (confirmed: the tuple is
  `("owner","finance_manager","gm")`). TOTP enrollment storage and the login flow
  already exist (`mfa_factors`, per identity), and the session already carries
  `mfa_satisfied_at`. But there are **two** enforcement vectors, not one: the tuple
  *and* the runtime policy `security.mfa.required_roles` that `SessionService` reads
  when opening a session (`src/identity.py`). The smallest secure improvement is
  therefore: add `reservation_manager` to the tuple **and** set the runtime policy —
  one data change plus one policy value, both reusing the existing per-role machinery.
  A TOTP-only login is not a complete MFA design by itself; MFA at login alone also
  does not bind the signing *act* to a fresh factor. So step-up is **required, not
  optional**: the kernel refuses signing when the session's `mfa_satisfied_at` is
  missing or older than a stated window (proposed 15 minutes) — one existing claim,
  one comparison, testable at the kernel boundary (`BLOCK_PROOF_MFA_STALE`), and a
  direct service call with a non-MFA session fails the same way.
- **Kernel enforcement:** `record_supplier_block_proof` / `revoke` resolve the role
  through `_require_human_verifier_principal` (E3) and then additionally require
  `role ∈ SIGNER_ROLES` — new constant `SIGNER_ROLES = ("reservation_manager",)` in
  `roles.py`, data not prompt. Direct service invocation is covered because the
  allowlist is enforced at the trusted service boundary, not only at the dashboard.
- **Role spoofing:** E3 refuses machine identities/roles and third-party actors on
  bound kernels; the allowlist closes the remaining "any human role" hole.
- **Revocation of signer authority:** suspending the person's role assignment (or the
  relationship/session) ends signing at the next session check; contact retirement
  (R1) kills the *source*; proof revocation (E3) kills the *artifact*. Three distinct
  kill switches, each audited — and for the source/supplier switches to be real, the
  **confirmation path re-checks** what the proof was built on: contact ACTIVE and
  verified, supplier ACTIVE, relationship ACTIVE, evidence attributed. ADR-0073
  already reads the proof row at confirmation, so these are four more reads on the
  same row set, with distinct codes; without them "retire the contact" revokes
  nothing that matters.
- **Audit trail:** E3 already writes `supplier_block.recorded` / `.revoked` with
  `verifier_role`; with the allowlist in force, `verifier_role` is always
  `reservation_manager` for new rows — a cheap integrity signal worth asserting in
  tests.

## §4 Acceptance matrix (TEST-402 extensions, PROPOSED)

Every refusal case asserts a named code **and** zero rows in the proof, evidence and
audit tables (refusals are *not* audited — consistent with ADR-0073's rule that audit
is atomic with a state change, and a refusal writes nothing). Cases 2, 3, 4, 7 and 11
each run a positive control that differs in exactly one field, so the refusal is
attributable to that field and not to the scaffolding.

| # | Case | Expected |
|---|------|----------|
| 1 | Verified-contact source, matching supplier/relationship/unit/dates, phone channel | proof accepted, audited once |
| 2 | Source not a registered contact | `BLOCK_PROOF_EVIDENCE_UNATTRIBUTED` (attribution refused at recording) |
| 3 | Attributed supplier ≠ contact's supplier | `BLOCK_PROOF_EVIDENCE_NOT_FROM_SUPPLIER` |
| 4 | Evidence relationship ≠ proof relationship | `BLOCK_PROOF_EVIDENCE_NOT_FROM_SUPPLIER` |
| 5 | Evidence for another booking's stay | `BLOCK_PROOF_BOOKING_MISMATCH` (ADR-0073 scoping) |
| 6 | Wrong unit or dates (`block_*` fields vs booking) | `BLOCK_PROOF_BOOKING_MISMATCH` |
| 7 | Generic `AVAILABLE` row, otherwise a valid chain (control included) | `BLOCK_PROOF_EVIDENCE_NOT_A_BLOCK` |
| 8 | No callback record (`callback_done_at` NULL) | refused at recording, **no** audit row |
| 9 | Historical unattributed evidence, and an old E3 proof on it, after cutover | readable; new proof refused; **confirmation** refused `BLOCK_PROOF_EVIDENCE_UNATTRIBUTED` |
| 10 | Expired / superseded evidence, or revoked proof | existing E3 codes |
| 11 | Wrong human role (`auditor`, `finance_manager`, `reservation_agent`, …) | `BLOCK_PROOF_SIGNER_ROLE_NOT_AUTHORIZED` |
| 12 | Machine or shared-token signer | `BLOCK_PROOF_MACHINE_VERIFIER` / `HUMAN_SESSION_REQUIRED` (E3) |
| 13 | Spoofed actor/role on a bound kernel | `BLOCK_PROOF_ACTOR_NOT_BOUND` (E3) |
| 14a | `reservation_manager` session without MFA | session refused at login |
| 14b | MFA'd session, direct kernel call, `mfa_satisfied_at` older than the window | `BLOCK_PROOF_MFA_STALE` |
| 15 | Confirmation + audit atomicity | unchanged, tested (E3) |
| 16 | Cross-tenant access, incl. cross-agency supplier/contact/relationship combos | denied, no write (existing + composite FKs) |
| 17 | Payment/booking invariants | unchanged, band regression |
| 18 | Failed operations leave no partial writes | unchanged, tested |
| 19 | Contact retired between proof and confirmation | confirmation refused `BLOCK_PROOF_SOURCE_RETIRED` |
| 20 | Supplier SUSPENDED between proof and confirmation | confirmation refused |
| 21 | Relationship SUSPENDED between proof and confirmation | confirmation refused |
| 22 | Two contacts racing to become the one verified (partial unique index) | second INSERT rejected by the DB |
| 23 | Idempotency replay after attribution rules change | `IDEMPOTENCY_CONFLICT` — the request hash includes the attribution fields, the block fields and the signer role |
| 24 | Confirmation age > window but evidence `valid_until` still ahead | refused (R5) |
| 25 | `supplier_confirmed_at` in the future | refused at recording |

## §6 Sonnet review (2026-11-20, one bounded call, Claude Sonnet 5.5)

Eight findings on the first draft; all accepted and folded into this revision:
attribution immutability is now trigger-enforced with a verification snapshot (1);
the callback assertion became structured fields with `source_kind` limited to
`CALLBACK_VERIFIED` and voice-only channels (2); the block proof is matched on
structured fields rather than `NOT_AVAILABLE` (3); the confirmation path re-checks
attribution and statuses, and the legacy "keep valid" option was withdrawn (4); the
trust anchor's registration moved to MFA-carrying roles with maker-checker and a
narrow audited contact-read (5); composite FKs and the NULL-unit rule were added (6);
the MFA claim now states both enforcement vectors and a *required* `mfa_satisfied_at`
freshness check, verified against `src/identity.py` (7); the matrix gained named
codes, zero-write assertions, negative controls and rows 19–25 (8).

## §7 New PROPOSED IDs (extends ADR-0073's family; nothing renumbered)

- FR-906 supplier attribution on authoritative evidence (R2–R4)
- FR-907 reservation_manager is the signing role (§3)
- FR-908 registered supplier contacts with one verified channel (R1, R7)
- DATA-215 `supplier_contacts` + attribution columns on `availability_evidence`
- SEC-132 signer allowlist enforced at the kernel boundary
- SEC-133 MFA required for `reservation_manager`
- TEST-403 acceptance matrix §4

## §9 Open owner decisions

1. Default `max_block_confirmation_age_hours` (168 h proposed).
2. Confirm the pilot channel script (R8) and who may register contacts until a
   Partnership UI exists (page role for `partnership_manager` is absent today —
   `PAGE_ROLE_BY_CANONICAL` has no mapping; interim: kernel-side registration with
   audit, or defer contact registration to a small UI slice).
3. Step-up re-auth at signing: required or optional for pilot.
4. (withdrawn after review — see §6)
5. PG migration runner remains a standing blocker for the PG side of 020.
6. The cutover date after which unattributed E3 proofs stop authorizing confirmations
   (a policy constant; until then they are tracked as a reported, not silent, legacy).

## §10 E5 design reconciliation (owner decision 2026-11-20, pack VILLA-E5-LOCAL-CLOSEOUT)

The owner approved four deviations from §2 and §3 **for the development baseline
only**. They are recorded here rather than applied silently to the text above: the
design sections stay as written so the difference between *specified* and *built*
remains auditable.

| # | §2/§3 said | As built (E5) | Why it is not weaker |
|---|---|---|---|
| A | Attribution = 4 nullable columns + all-or-none CHECK on `availability_evidence` | Table `evidence_supplier_attributions`, one row per attributed evidence, every attribution column `NOT NULL`, composite FKs `(agency_id, …)`, INSERT-only triggers | `NOT NULL` on a separate row enforces all-or-none harder than a CHECK with a nullable branch; no code path updates `availability_evidence`, and the attribution row is written only at `record_evidence` time, so a legacy answer has no row here and **no application path** can add one afterwards. G1 was the remaining hole — a direct writer could file an attribution against any evidence — and is now closed at the database boundary (§11.2, `63ac1ea`) |
| B | Contact registration = `partnership.write` (Partnership Manager) | `owner` + `gm` register and counter-check for the pilot (`CONTACT_REGISTRY_ROLES`), maker ≠ checker, both from authenticated sessions with MFA | The Partnership page role does not exist yet (§9.2). Narrowing the role set widens nothing; the maker/checker separation is enforced in the kernel (`CONTACT_CHECKER_IS_MAKER`) and in the database (`ck_sce_checker_is_not_maker`) |
| C | `status ACTIVE/RETIRED` + `verified_at`/`verified_by` columns | Append-only `supplier_contact_events` (`VERIFIED`/`SUSPENDED`/`REINSTATED`) + revoke-only `revoked_at`; standing derived from the latest event | The column form overwrites history. The event form keeps every verification and suspension with actor, reason and time; "one live `VERIFIED` event per contact" is a partial unique index (`ux_sce_one_verified`) and "one live number per supplier" another (`ux_sc_live_value`), while "no second verified contact" is the kernel's `CONTACT_ALREADY_VERIFIED` |
| D | `channel` ∈ `phone_wa`, `phone_voice`, `email`, `portal` | `PHONE` only | A closed set of one is a deliberate pilot limit, not an oversight; the CHECK can only widen later, and no code path assumes another channel exists |

Owner conditions that were **not** waived, and where each is enforced:

- **Booking-specific written supplier confirmation** — `supplier_assertion.py`
  requires `block_ref`, `block_start`, `block_end`, `supplier_confirmed_at` for the
  booking's own unit and dates; a generic `AVAILABLE` answer carries no attribution
  row and is refused.
- **Independent callback to a previously verified contact** — the assertion names a
  `contact_event_id`; the contact must still be `VERIFIED` at confirmation time, and
  the callback window is `SETTING_MAX_AGE_HOURS` (default 168 h).
- **Tenant isolation** — composite foreign keys plus `agency_id` filters; a contact,
  relationship or evidence row from another tenant cannot be joined.
- **Source authenticity** — attribution can only be written at `record_evidence`
  time from a trusted session, never by a later update (INSERT-only triggers).
- **Human signer authorization** — signing is restricted to `reservation_manager`
  bound to the session through `agency_role_holders`; machine principals are refused.
- **MFA and fresh step-up** — `MFA_REQUIRED_ROLES` plus `require_step_up` with the
  repository's 10-minute `STEP_UP_WINDOW`; a step-up made by one signer does not lift
  another signer's session, and an expired step-up is refused (`SIGNER_STEP_UP_STALE`).
- **Auditability** — every registration, verification, suspension, assertion and
  confirmation writes an audit row with actor, role, tenant and idempotency key.
- **Fail-closed confirmation** — `confirm_booking` re-runs the attribution and
  freshness checks itself. Enforcement is a per-tenant setting whose default is
  `false` (deliberate: unattributed E3 proofs stay authorisable until the cutover
  date in §9.6, reported rather than hidden); while it is `true`, a missing or stale
  attribution blocks the confirmation, and the screen mirrors the kernel's window
  instead of inventing one.

Status effect: **PROPOSED is retained.** This section records a decision to build,
not an accepted ADR. `§9` items 1 (default age), 3 (step-up enforcement), 5 (PG
migration runner) and 6 (legacy cutover date) remain open; the local commit is a
checkpoint (`E5_LOCAL_COMMITTED_REVIEW_PENDING`), not production readiness.

## §11 Independent implementation review (2026-11-20, one bounded call, Claude Sonnet 5.5)

Scope sent: migration 020 in full, the gate functions of `supplier_assertion.py`
(lines 150–340 and 560–660), and the 106 collected test names (61 assertion, 45
block proof). 10,829 prompt tokens, 5,145 completion tokens, one call, no repository
upload. Six HIGH findings returned; four do not survive inspection of the code, two
stand and are recorded here as residuals rather than fixed in the same commit.

### §11.1 Refuted findings (with the evidence that refutes them)

| Finding | Verdict | Evidence |
|---|---|---|
| PG twin may lack trigger functions, so append-only holds only on SQLite | **Refuted** | `001_initial.pg.sql:443` defines `villa_reject_mutation()` in plpgsql; `villa_revoke_only()` likewise; 74 `EXECUTE FUNCTION` references, 0 unresolved |
| Confirmation may not re-check booking, stay, supplier, contact standing, role binding | **Refuted** | `_require_attributed_evidence(at_confirmation=True)` compares `booking_id`, `unit_id`, `block_start/end` to the booking, then re-runs `_check_supplier_chain`, `_contact_standing` and the `agency_role_holders` binding of `callback_by` |
| A machine principal, wrong role or stale step-up might sign | **Refuted** | `_resolve_principal` refuses machine identity/role (`MACHINE_PRINCIPAL_REFUSED`), checks the allowed set after canonicalisation, requires an ACTIVE holder row, `mfa_satisfied_at`, and `require_step_up` inside `STEP_UP_WINDOW` |
| A case/whitespace variant of the maker's identity could self-check | **Refuted in practice** | Binding compares `session.identity` to `holder_ref` exactly, so an identity variant holds no role and cannot reach `CONTACT_CHECKER_IS_MAKER`; revisit only if identity storage ever becomes case-insensitive |

### §11.2 Residual status (G1 and G1-R closed locally, G2 open)

- **G1 — CLOSED by `63ac1ea` (fix committed locally, subject to independent
  verification).** The attribution is now pinned by the database: `(evidence_id,
  contact_id)` must match a parent `availability_evidence` row whose derived column
  `callback_verified_contact` equals that contact, the event reference carries a
  `NOT NULL` `contact_event_kind` CHECKed to `'VERIFIED'` into a composite FK against
  `supplier_contact_events (event_id, contact_id, kind)`, and `(agency_id, booking_id,
  unit_id, block_start, block_end)` must equal a real `bookings` stay. Parent keys
  `ux_evidence_callback_contact`, `uq_sce_event_contact_kind` and `uq_bk_agency_stay`
  make those FKs legal on SQLite and PostgreSQL alike; no trigger was needed. What the
  database still cannot assert, and what must not be claimed for it: that the evidence
  row was written in the same transaction as the attribution, and that a
  `source_ref` value is an authentic supplier attestation rather than a claim. Those
  remain service and operating concerns (§11.3).
- **G1-R — upgrade path for existing databases: CLOSED by this pack (local, uncommitted
  until the corrective commit).** The gap was real and is now measured end-to-end on a
  disposable database whose two affected objects were downgraded to the pre-`63ac1ea`
  shape (`cced242` DDL) while every other object stayed as it is: `020` alone failed with
  `no such column: callback_verified_contact` (18 statements applied, 1 failed, closed).
  `scripts/apply_migrations.py` now recognises three states before it writes anything —
  COMPLETE (no structural work), HISTORICAL (idempotent upgrade, then `020`), UNKNOWN
  (fail closed). The upgrade is two steps, chosen because each is provably safe on
  SQLite: `ALTER TABLE availability_evidence ADD COLUMN callback_verified_contact …
  VIRTUAL` (the only generated-column form SQLite can add in place), and a rebuild of
  `evidence_supplier_attributions` only — the one table that needs new columns *and* new
  foreign keys, and the one that nothing references — copying rows verbatim and taking
  `contact_event_kind` from the event the row already pointed at. No authority is
  invented: the copy refuses outright if any existing row's event is not `VERIFIED` or
  its evidence does not name that contact (`inventing authority`), and refuses if the
  ledger cannot be read at all. The whole upgrade plus `020` runs in one transaction
  with `foreign_keys` off outside it, and `foreign_key_check` (no *new* violations) and
  `integrity_check` run before `COMMIT`.
  What this leaves open, stated plainly: the upgraded database carries the derived
  column as VIRTUAL while a fresh one carries it STORED — same values, same parent key,
  same refusals (a nine-case matrix gives identical outcomes on both), but not
  byte-identical DDL; and there is still no migration runner for PostgreSQL, so the
  *existing*-schema path is unproven on PG even though the fresh-schema twin applies
  cleanly. Both are recorded as residual, not as tested.
- **G2 — OPEN — HIGH / PRODUCTION BLOCKER.** Enforcement defaults to `false`. Until an
  agency sets `supplier_assertion_enforced=true` (or the cutover date in §9.6 is
  chosen), an unattributed E3 proof can still authorise a confirmation. This is the
  deliberate legacy window, but it *is* a fail-open default and must be stated as one.
  Measured, not merely asserted: with enforcement off, a paid booking was taken to
  `CONFIRMED` with **no session at all** — evidence written by `record_evidence` (which
  takes no credential), a block proof signed by a self-declared
  `verifier_role="reservation_manager"`, and `confirm_booking` called with a made-up
  `actor`. Every one of those gates is keyed off the *declared* role string, so the
  only thing standing between a script and a confirmation today is the enforcement
  flag. The dashboard's HTTP routes are session-bound and cannot do this; a caller with
  in-process kernel access can.

### §11.3 Verification of the G1 fix (pack VILLA-E5-VERIFY-001, 2026-11-20)

Three different questions, kept apart because conflating them is how a database
constraint gets mistaken for a supplier's word:

- **A. Referential consistency — enforced, proven.** Composite FKs and their parent
  keys are present in the committed DDL on both engines; negative probes (foreign
  evidence, non-callback evidence, evidence naming another contact, non-VERIFIED event,
  cross-tenant ids, NULL composite parts, wrong stay) are refused by the database, and
  a valid attribution is accepted. PostgreSQL 16 applies the generated twin and
  enforces the same constraints — the probe there accepted the valid pair and refused
  non-callback, wrong-contact and missing-evidence references.
- **B. Authorized source attestation — not enforced by the database, and not claimed.**
  `record_evidence` accepts any caller and any `source_ref` string, so
  `callback_verified:<contact>` is a value a writer can type, not a fact the database
  can verify. The attribution row is only written by the authenticated assertion path,
  which is what makes the *row* trustworthy; the FK proves the row is consistent with
  its parents, nothing more.
- **C. Actual supplier confirmation — outside the database entirely.** Only the
  operating procedure (a human calls the number, reads it through
  `read_supplier_contact_for_callback`, and records the assertion with a fresh step-up)
  can assert that. No schema in this repository can.
- **D. Upgrade path — repaired, and proven on a disposable legacy database.** A database
  built from `63ac1ea^` and then given the current `020_supplier_contacts.sql` never
  could take the new constraints: 18 statements applied, 1 failed with
  `no such column: callback_verified_contact`, closed. The runner now upgrades such a
  database first (see G1-R in §11.2) and the same fixture, seeded with suppliers,
  availability evidence, a paid booking, an audit chain, a block proof and an existing
  attribution, then reaches the fresh shape with every row intact. What is *not* proven:
  an upgrade across a database large enough to make the single transaction impractical,
  and the PostgreSQL equivalent (no PG runner exists).


