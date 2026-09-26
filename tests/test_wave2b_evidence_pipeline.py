"""WAVE 2-B tests: the evidence quarantine pipeline (Master Spec v1.1 §27, §12, §19, §39).

The requirement is a fixed order — UPLOAD → QUARANTINE → TYPE/SIZE VALIDATION →
MALWARE SCAN → CLASSIFICATION → SAFE STORAGE → AUTHORIZED ACCESS — and these tests
exist to prove the order cannot be shortened, in the service *and* in the database.

Two tests carry the most weight:

* `test_storage_cannot_be_reached_without_a_clean_scan` — the row cannot exist without a
  named scanner and a CLEAN verdict, so skipping quarantine is not representable;
* `test_a_deployment_without_a_scanner_promotes_nothing` — the fail-closed default. A
  document pipeline that silently "works" without scanning is worse than one that is
  visibly unavailable.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

import evidence as EV
from primitives import DomainError, FrozenClock, IdFactory
from schema_loader import connect_sqlite

START = "2026-09-26T08:00:00+00:00"
PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
TOKEN_KEY = "k" * 48


class FixedScanner:
    """A deterministic scanner double. The real engine is a deployment decision."""

    name = "test-scanner"

    def __init__(self, verdict: str = "CLEAN") -> None:
        self.verdict = verdict
        self.calls = 0

    def scan(self, *, content: bytes, content_type: str, filename: str) -> EV.ScanResult:
        self.calls += 1
        return EV.ScanResult(verdict=self.verdict, provider=self.name, detail="double")


def _unit(conn, agency_id: str = "agency_bali", suffix: str = "bali") -> str:
    """A unit to hang a supplier route on, created once per agency."""
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE', 'ACTIVATED', ?, ?)"
        " ON CONFLICT DO NOTHING",
        (f"prop_{suffix}", agency_id, f"Villa {suffix}", START, START),
    )
    conn.execute(
        "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms, bathrooms,"
        " base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'entire_villa', 2, 2, 2, 4, 'ACTIVE', ?, ?) ON CONFLICT DO NOTHING",
        (f"unit_{suffix}", agency_id, f"prop_{suffix}", f"Unit {suffix}", START, START),
    )
    return f"unit_{suffix}"


@pytest.fixture()
def conn():
    connection = connect_sqlite(":memory:")
    for agency in ("agency_bali", "agency_jogja"):
        connection.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'ACTIVE', ?, ?) ON CONFLICT DO NOTHING",
            (agency, agency, START, START),
        )
    yield connection
    connection.close()


def service(conn, tmp_path: Path, *, agency_id: str = "agency_bali",
            scanner=None, signer=True, **kwargs) -> EV.EvidenceService:
    return EV.EvidenceService(
        conn,
        clock=FrozenClock.at(START),
        ids=IdFactory(deterministic=True, seed="wave2b"),
        agency_id=agency_id,
        quarantine=EV.LocalStorageBackend(tmp_path / "quarantine", name="quarantine"),
        storage=EV.LocalStorageBackend(tmp_path / "storage", name="object-store"),
        scanner=FixedScanner() if scanner is None else scanner,
        signer=EV.AccessTokenSigner(TOKEN_KEY) if signer else None,
        **kwargs,
    )


def uploaded(svc: EV.EvidenceService, content: bytes = PDF, filename: str = "contract.pdf",
             content_type: str = "application/pdf", kind: str = "PDF") -> str:
    return svc.upload(content=content, filename=filename, content_type=content_type,
                      kind=kind, uploaded_by="ops_uploader")


def stored(svc: EV.EvidenceService, *, classification: str = "L3") -> str:
    evidence_id = uploaded(svc)
    svc.validate(evidence_id)
    svc.scan(evidence_id)
    svc.classify(evidence_id, classification=classification)
    svc.store(evidence_id)
    return evidence_id


# --------------------------------------------------------------------------- #
# 1. The pipeline cannot be shortened
# --------------------------------------------------------------------------- #
def test_an_upload_lands_in_quarantine_and_nothing_else(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = uploaded(svc)
    row = svc.get(evidence_id)
    assert row["status"] == "QUARANTINED"
    assert row["storage_key"] is None, "a quarantined file must not have a safe-storage key"
    assert row["classification"] is None
    assert (tmp_path / "quarantine" / f"quarantine/{evidence_id}").read_bytes() == PDF
    assert not (tmp_path / "storage").exists() or not any((tmp_path / "storage").rglob("*"))


def test_storage_cannot_be_reached_without_a_clean_scan(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = uploaded(svc)
    with pytest.raises(Exception) as caught:
        conn.execute(
            "INSERT INTO evidence_objects (evidence_id, agency_id, kind, status, classification,"
            " access_policy, original_filename, content_type, byte_size, sha256, uploaded_by,"
            " quarantine_ref, storage_backend, storage_key, created_at, updated_at)"
            " VALUES ('evid_ghost', 'agency_bali', 'PDF', 'STORED', 'L3', 'AGENCY_SCOPED',"
            " 'x.pdf', 'application/pdf', 10, ?, 'ops_uploader', 'quarantine/x', 'object-store',"
            " 'evidence/x', ?, ?)",
            ("a" * 64, START, START),
        )
    assert "ck_evidence_stored_requires_clean_scan" in str(caught.value), (
        f"the refusal must name the quarantine guard: {caught.value}"
    )
    assert svc.get(evidence_id)["status"] == "QUARANTINED"


def test_each_step_refuses_to_run_out_of_order(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = uploaded(svc)
    with pytest.raises(EV.InvalidEvidenceTransition) as scan_first:
        svc.scan(evidence_id)
    assert scan_first.value.code == "STATE_TRANSITION_NOT_ALLOWED"
    with pytest.raises(EV.InvalidEvidenceTransition):
        svc.store(evidence_id)
    svc.validate(evidence_id)
    with pytest.raises(EV.InvalidEvidenceTransition):
        svc.classify(evidence_id, classification="L3")
    with pytest.raises(EV.InvalidEvidenceTransition):
        svc.store(evidence_id)


def test_the_whole_pipeline_produces_a_stored_object(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    row = svc.get(evidence_id)
    assert row["status"] == "STORED"
    assert row["scan_provider"] == "test-scanner"
    assert row["scan_verdict"] == "CLEAN"
    assert row["storage_backend"] == "object-store"
    assert row["storage_key"] == f"evidence/agency_bali/{evidence_id}"
    assert (tmp_path / "storage" / row["storage_key"]).read_bytes() == PDF
    assert row["sha256"] == hashlib.sha256(PDF).hexdigest()


# --------------------------------------------------------------------------- #
# 2. Fail-closed defaults
# --------------------------------------------------------------------------- #
def test_a_deployment_without_a_scanner_promotes_nothing(conn, tmp_path):
    svc = EV.EvidenceService(
        conn, clock=FrozenClock.at(START), ids=IdFactory(deterministic=True, seed="noscanner"),
        agency_id="agency_bali",
        quarantine=EV.LocalStorageBackend(tmp_path / "quarantine"),
        storage=EV.LocalStorageBackend(tmp_path / "storage"),
        signer=EV.AccessTokenSigner(TOKEN_KEY),
    )
    evidence_id = uploaded(svc)
    svc.validate(evidence_id)
    with pytest.raises(EV.ScannerUnavailable) as caught:
        svc.scan(evidence_id)
    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert svc.get(evidence_id)["status"] == "SCAN_PENDING"
    with pytest.raises(EV.InvalidEvidenceTransition):
        svc.store(evidence_id)


def test_a_deployment_without_a_token_key_issues_no_grants(conn, tmp_path):
    with pytest.raises(EV.StorageUnavailable) as caught:
        EV.AccessTokenSigner("")
    assert caught.value.code == "SOURCE_UNAVAILABLE"
    with pytest.raises(EV.StorageUnavailable):
        EV.AccessTokenSigner("too-short")


def test_grants_are_refused_when_no_signer_is_configured(conn, tmp_path):
    svc = service(conn, tmp_path, signer=False)
    evidence_id = stored(svc)
    with pytest.raises(EV.StorageUnavailable):
        svc.grant_access(evidence_id, granted_to="auditor_1", purpose="compliance review")


def test_an_infected_file_is_not_promoted(conn, tmp_path):
    svc = service(conn, tmp_path, scanner=FixedScanner("INFECTED"))
    evidence_id = uploaded(svc)
    svc.validate(evidence_id)
    with pytest.raises(EV.InfectedFile) as caught:
        svc.scan(evidence_id)
    assert caught.value.code == "FILE_INFECTED"
    row = svc.get(evidence_id)
    assert row["status"] == "SCAN_FAILED" and row["storage_key"] is None
    assert "SCAN_FAILED" in [event["kind"] for event in svc.events(evidence_id)]


def test_an_unknown_scan_verdict_is_not_clean(conn, tmp_path):
    svc = service(conn, tmp_path, scanner=FixedScanner("UNKNOWN"))
    evidence_id = uploaded(svc)
    svc.validate(evidence_id)
    with pytest.raises(EV.InfectedFile):
        svc.scan(evidence_id)
    assert svc.get(evidence_id)["status"] == "SCAN_FAILED"


# --------------------------------------------------------------------------- #
# 3. Type / size / filename validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "filename,content_type,content,reason_word",
    [
        ("contract.pdf", "application/zip", PDF, "content type"),
        ("contract.pdf", "application/pdf", b"not a pdf at all", "declared type"),
        ("../../../etc/passwd", "text/plain", b"root:x:0:0", "path"),
        ("empty.pdf", "application/pdf", b"", "empty"),
    ],
)
def test_validation_refuses_and_parks_the_file(conn, tmp_path, filename, content_type, content,
                                               reason_word):
    svc = service(conn, tmp_path)
    evidence_id = svc.upload(content=content, filename=filename, content_type=content_type,
                             kind="OTHER", uploaded_by="ops_uploader")
    with pytest.raises(EV.RejectedFile) as caught:
        svc.validate(evidence_id)
    assert reason_word in str(caught.value)
    row = svc.get(evidence_id)
    assert row["status"] == "REJECTED"
    assert row["storage_key"] is None
    assert row["rejection_reason"]


def test_an_oversized_file_is_refused(conn, tmp_path):
    svc = service(conn, tmp_path, max_bytes=64)
    evidence_id = svc.upload(content=PDF + b"x" * 200, filename="big.pdf",
                             content_type="application/pdf", kind="PDF",
                             uploaded_by="ops_uploader")
    with pytest.raises(EV.RejectedFile) as caught:
        svc.validate(evidence_id)
    assert "byte limit" in str(caught.value)


def test_a_mismatched_magic_byte_is_refused_even_with_the_right_extension(conn, tmp_path):
    """A .pdf that is not a PDF is how a polyglot file walks past an extension check."""
    svc = service(conn, tmp_path)
    evidence_id = svc.upload(content=b"<?php system($_GET['c']); ?>", filename="invoice.pdf",
                             content_type="application/pdf", kind="PDF",
                             uploaded_by="ops_uploader")
    with pytest.raises(EV.RejectedFile):
        svc.validate(evidence_id)


# --------------------------------------------------------------------------- #
# 4. Classification (§19)
# --------------------------------------------------------------------------- #
def test_l4_evidence_must_use_the_restricted_vault(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = uploaded(svc)
    svc.validate(evidence_id)
    svc.scan(evidence_id)
    with pytest.raises(EV.ValidationFailed):
        svc.classify(evidence_id, classification="L4", access_policy="AGENCY_SCOPED")
    svc.classify(evidence_id, classification="L4")
    row = svc.get(evidence_id)
    assert row["classification"] == "L4"
    assert row["access_policy"] == "RESTRICTED_VAULT"


def test_the_database_also_refuses_l4_outside_the_vault(conn, tmp_path):
    with pytest.raises(Exception) as caught:
        conn.execute(
            "INSERT INTO evidence_objects (evidence_id, agency_id, kind, status, classification,"
            " access_policy, original_filename, content_type, byte_size, sha256, uploaded_by,"
            " quarantine_ref, created_at, updated_at)"
            " VALUES ('evid_l4', 'agency_bali', 'PDF', 'QUARANTINED', 'L4', 'AGENCY_SCOPED',"
            " 'x.pdf', 'application/pdf', 1, ?, 'ops', 'quarantine/x', ?, ?)",
            ("a" * 64, START, START),
        )
    assert "ck_evidence_restricted_vault" in str(caught.value)


# --------------------------------------------------------------------------- #
# 5. Storage backend confinement
# --------------------------------------------------------------------------- #
def test_a_storage_key_cannot_escape_its_root(tmp_path):
    backend = EV.LocalStorageBackend(tmp_path / "storage")
    with pytest.raises(EV.StorageUnavailable):
        backend.put(key="../../etc/passwd", content=b"x")


def test_a_stored_file_is_owner_only(conn, tmp_path):
    svc = service(conn, tmp_path)
    row = svc.get(stored(svc))
    mode = stat.S_IMODE(os.stat(tmp_path / "storage" / row["storage_key"]).st_mode)
    assert mode == 0o600, f"stored evidence must not be world readable, got {oct(mode)}"


# --------------------------------------------------------------------------- #
# 6. Temporary authorized access (§27)
# --------------------------------------------------------------------------- #
def test_a_grant_is_single_use_by_default(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    token = svc.grant_access(evidence_id, granted_to="auditor_1", purpose="compliance review")
    assert svc.authorize(token) == evidence_id
    with pytest.raises(EV.AccessDenied) as caught:
        svc.authorize(token)
    assert caught.value.details.get("reason") == "grant already used"


def test_a_grant_expires(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    token = svc.grant_access(evidence_id, granted_to="auditor_1", purpose="review",
                             ttl_seconds=60)
    with pytest.raises(EV.AccessDenied) as caught:
        svc.authorize(token, at="2026-09-26T09:30:00+00:00")
    assert caught.value.details.get("reason") == "grant expired"


def test_a_tampered_token_is_refused(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    token = svc.grant_access(evidence_id, granted_to="auditor_1", purpose="review")
    grant_id, signature = token.split(".", 1)
    forged = f"{grant_id}.{'0' * len(signature)}"
    with pytest.raises(EV.AccessDenied) as caught:
        svc.authorize(forged)
    assert caught.value.details.get("reason") in ("token digest does not match",
                                                  "signature does not verify")


def test_a_revoked_grant_is_refused(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    token = svc.grant_access(evidence_id, granted_to="auditor_1", purpose="review")
    grant_id = token.split(".", 1)[0]
    svc.revoke_access(grant_id, reason="purpose withdrawn")
    with pytest.raises(EV.AccessDenied) as caught:
        svc.authorize(token)
    assert caught.value.details.get("reason") == "grant was revoked"


def test_a_grant_cannot_be_issued_for_quarantined_evidence(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = uploaded(svc)
    with pytest.raises(EV.AccessDenied):
        svc.grant_access(evidence_id, granted_to="auditor_1", purpose="review")


def test_the_raw_token_is_never_stored(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    token = svc.grant_access(evidence_id, granted_to="auditor_1", purpose="review")
    signature = token.split(".", 1)[1]
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        text_columns = [c for c in columns if c not in ("evidence_id", "grant_id")]
        if not text_columns:
            continue
        for row in conn.execute(f"SELECT * FROM {table}"):
            for column in text_columns:
                value = row[column]
                if isinstance(value, str):
                    assert signature not in value, f"{table}.{column} stored the raw token"


# --------------------------------------------------------------------------- #
# 7. History is append-only and deletion is a status (§39)
# --------------------------------------------------------------------------- #
def test_the_evidence_trail_is_append_only(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    event_id = svc.events(evidence_id)[0]["event_id"]
    with pytest.raises(Exception) as caught:
        conn.execute("UPDATE evidence_events SET kind = 'STORED' WHERE event_id = ?", (event_id,))
    assert "append-only" in str(caught.value)
    with pytest.raises(Exception):
        conn.execute("DELETE FROM evidence_events WHERE event_id = ?", (event_id,))


def test_deleting_evidence_keeps_the_row_and_its_history(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    svc.soft_delete(evidence_id, reason="retention review", actor="owner_1", actor_role="owner")
    assert svc.get(evidence_id)["status"] == "DELETED"
    kinds = [event["kind"] for event in svc.events(evidence_id)]
    assert kinds[-1] == "SOFT_DELETED"
    assert "STORED" in kinds


# --------------------------------------------------------------------------- #
# 8. Tenant scope
# --------------------------------------------------------------------------- #
def test_another_agency_cannot_see_or_touch_this_evidence(conn, tmp_path):
    bali = service(conn, tmp_path, agency_id="agency_bali")
    jogja = service(conn, tmp_path, agency_id="agency_jogja")
    evidence_id = stored(bali)
    with pytest.raises(EV.EvidenceNotFound):
        jogja.get(evidence_id)
    with pytest.raises(EV.EvidenceNotFound):
        jogja.validate(evidence_id)
    with pytest.raises(EV.EvidenceNotFound):
        jogja.grant_access(evidence_id, granted_to="auditor_1", purpose="review")
    with pytest.raises(EV.AccessDenied):
        jogja.authorize("grant_x.deadbeef")


# --------------------------------------------------------------------------- #
# 9. Provenance: only a stored object may be cited (§12)
# --------------------------------------------------------------------------- #
def test_quarantined_evidence_cannot_be_cited_as_a_source(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = uploaded(svc)
    with pytest.raises(EV.AccessDenied) as caught:
        svc.stored_evidence(evidence_id)
    assert caught.value.code == "VERIFICATION_REQUIRED"


def test_a_stored_object_can_be_cited(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    assert svc.stored_evidence(evidence_id)["status"] == "STORED"

def test_a_denial_with_no_valid_target_is_still_recorded(conn, tmp_path):
    """The signal an operator needs: a forged or unknown grant must leave a trace."""
    svc = service(conn, tmp_path)
    with pytest.raises(EV.AccessDenied):
        svc.authorize("grant_forged.deadbeef")
    denials = svc.denials()
    assert len(denials) == 1
    assert denials[0]["evidence_id"] is None
    assert denials[0]["kind"] == "ACCESS_DENIED"
    assert "unknown grant" in denials[0]["detail"]


def test_a_trail_row_without_a_target_is_only_allowed_for_a_denial(conn, tmp_path):
    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    with pytest.raises(Exception) as caught:
        conn.execute(
            "INSERT INTO evidence_events (event_id, agency_id, evidence_id, kind, actor,"
            " actor_role, detail, occurred_at) VALUES ('evt_bad', 'agency_bali', NULL,"
            " 'STORED', 'ops', 'agent', '{}', ?)",
            (START,),
        )
    assert "ck_evidence_event_target" in str(caught.value)
    assert svc.events(evidence_id)

# --------------------------------------------------------------------------- #
# 10. Provenance: a contract or rate cites a stored object (§12 chain)
# --------------------------------------------------------------------------- #
def test_a_contract_draft_can_cite_a_stored_evidence_object(conn, tmp_path):
    import supplier_route as SR

    svc = service(conn, tmp_path)
    evidence_id = stored(svc)
    route = SR.SupplierRouteService(conn, clock=FrozenClock.at(START),
                                    ids=IdFactory(deterministic=True, seed="link"),
                                    agency_id="agency_bali")
    supplier_id = route.register_supplier("Supplier Bali")
    route_id = route.link_villa(supplier_id, unit_id=_unit(conn))
    contract_id = route.draft_contract(
        route_id, source_doc_ref="contract-2026.pdf", source_reference="clause 4.2",
        effective_from="2026-10-01", effective_to="2026-12-31",
        source_evidence_id=evidence_id,
    )
    row = conn.execute(
        "SELECT source_evidence_id FROM contract_versions WHERE contract_version_id = ?",
        (contract_id,),
    ).fetchone()
    assert row["source_evidence_id"] == evidence_id


def test_quarantined_evidence_cannot_be_cited_by_a_contract(conn, tmp_path):
    import supplier_route as SR

    svc = service(conn, tmp_path)
    evidence_id = uploaded(svc)  # never validated, never scanned
    route = SR.SupplierRouteService(conn, clock=FrozenClock.at(START),
                                    ids=IdFactory(deterministic=True, seed="link2"),
                                    agency_id="agency_bali")
    supplier_id = route.register_supplier("Supplier Bali")
    route_id = route.link_villa(supplier_id, unit_id=_unit(conn))
    with pytest.raises(SR.VerificationRequired) as caught:
        route.draft_contract(
            route_id, source_doc_ref="c.pdf", source_reference="clause 1",
            effective_from="2026-10-01", effective_to="2026-12-31",
            source_evidence_id=evidence_id,
        )
    assert caught.value.code == "VERIFICATION_REQUIRED"
    assert conn.execute("SELECT COUNT(*) FROM contract_versions").fetchone()[0] == 0


def test_another_agencys_evidence_cannot_be_cited(conn, tmp_path):
    import supplier_route as SR

    bali = service(conn, tmp_path, agency_id="agency_bali")
    evidence_id = stored(bali)
    jogja = SR.SupplierRouteService(conn, clock=FrozenClock.at(START),
                                    ids=IdFactory(deterministic=True, seed="link3"),
                                    agency_id="agency_jogja")
    supplier_id = jogja.register_supplier("Supplier Jogja")
    route_id = jogja.link_villa(supplier_id, unit_id=_unit(conn, "agency_jogja", "jogja"))
    with pytest.raises(SR.SupplierRouteError) as caught:
        jogja.draft_contract(
            route_id, source_doc_ref="c.pdf", source_reference="clause 1",
            effective_from="2026-10-01", effective_to="2026-12-31",
            source_evidence_id=evidence_id,
        )
    assert caught.value.code == "RESOURCE_NOT_FOUND"
