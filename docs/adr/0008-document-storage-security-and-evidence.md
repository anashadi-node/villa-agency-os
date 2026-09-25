# ADR-0008 — Document Storage, Quarantine, and Evidence Security

- Status: **PROPOSED — pending owner review**
- Date: 2026-09-22

## Context

Existing `documents.py` produces deterministic, identifier-minimized HTML documents. Master Spec additionally requires quarantine, file validation, malware scan, classification, encrypted object storage, signed/authorized download, and evidence provenance.

## Decision

Keep deterministic document generation, but add a separate secure document ingestion/storage pipeline before accepting real external supplier/guest documents.

## Required lifecycle

```text
UPLOAD
→ QUARANTINE
→ TYPE/SIZE VALIDATION
→ MALWARE SCAN
→ CLASSIFICATION
→ SAFE STORAGE
→ AUTHORIZED ACCESS
```

## Rules

- External documents are untrusted until validated/scanned/classified.
- Restricted files are never permanent public links.
- DB stores metadata/hash/classification/storage key/version/access policy, not raw large content.
- Passport/raw ID must not enter AI context.

## Tests required

- Unsafe file type rejected.
- Oversize file rejected.
- Unscanned file cannot become trusted evidence.
- Signed URL/access requires authorization.
- Hash mismatch detected.
