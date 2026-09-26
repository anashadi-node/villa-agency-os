# ADR-0015 — Inquiry, guest and evidence: domain mapping onto the existing kernel

- Status: **ACCEPTED** (recorded with WAVE 2-B and WAVE 3-A, 2026-09-26)
- Date: 2026-09-26
- Spec: Master Spec v1.1 §5, §6, §12, §19, §27, §50 (WAVE 2 and WAVE 3), Appendix B

## Context

The spec names domain entities at the level of meaning (`INQUIRY`, `GUEST`, `DOCUMENT`,
`EVIDENCE`). The existing kernel already contains working, tested structures for two of them,
under different names. §0 forbids inventing requirements and §3 forbids cross-module
duplication, so the mapping has to be decided once, on the record, rather than re-litigated
per wave — and it must never fork a second writer for one lifecycle.

## Decision

1. **An inquiry IS the existing case spine.** `cases` already carries channel, guest ref,
   status, SLA, messages, events and intake. WAVE 3 does not create an `inquiries` table; it
   adds `cases.reference` (`INQ-YYYY-NNNNN`) and `cases.guest_id`. `CaseService.open_case`
   remains the only function that creates a case, and `CrmService` composes it and then
   attaches the reference and the guest link.

2. **`intake_fields` are proposals; `inquiry_requirements` are confirmed truth.** They are two
   layers, not duplicates. The extraction pipeline writes the first (§12: AI_EXTRACTED ≠
   VERIFIED); the second is what matching and pricing may rely on. A requirement whose source
   is `ACCEPTED_INTAKE` must cite an intake field that is genuinely `ACCEPTED` *and* belongs to
   the same case, so extraction cannot promote itself.

3. **`guests` is the Guest Master (§6).** Raw identity documents (passport, ID scans) are
   deliberately *not* columns here: §19 classifies them L4 and they belong to evidence objects
   under §27, where classification, hashing and access grants apply.

4. **Evidence objects are their own registry.** `evidence_objects` (uploaded, quarantined,
   scanned) is distinct from `documents` (generated outbound paperwork) and from
   `availability_evidence` (availability observations). Collapsing them would give the
   platform one name for three different trust levels.

5. **`require_ref` guards opaque identifiers, not path-like keys.** A storage key
   (`evidence/<agency>/<id>`) legitimately contains `/`; applying the opaque-token rule to it
   is a bug, not a control. Storage keys get path confinement instead: relative, no `..`, and
   the resolved path must stay inside the storage root.

6. **`content_class` (P0–P4) is a data classification, never defaulted.** It decides whether a
   message body is stored, stored redacted, or not stored at all, so `open_inquiry` requires it
   rather than picking a value a CRM helper would have no authority to pick (§19).

## Consequences

- WAVE 3's remaining slices (conversations, tasks, matching, option sets) extend the case
  spine and the requirement layer; none of them needs a new inquiry table.
- `reference` is a label (§Appendix B): every lookup by reference stays agency-scoped, and
  `tests/test_wave3a_crm_inquiry.py` pins that with a cross-agency read.
- WAVE 4 pricing reads `inquiry_requirements`, so the provenance rule in (2) is a dependency of
  pricing correctness, not only of CRM tidiness.
