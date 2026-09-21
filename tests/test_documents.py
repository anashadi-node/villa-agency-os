"""Tests for deterministic, identifier-free document issuance.

The suite pins the contract of ``src/documents.py``:

* the file/row pair written by one ``generate`` (kind, version, storage_ref,
  body_sha256, content_class, status) and the safety of the path it writes to;
* byte determinism (same inputs + same instant -> same sha256) and the fact that
  the instant lives in the body;
* reconciliation against the real ``invoices``/``invoice_lines`` and ``bookings``
  rows — a document that disagrees with its source is refused, not written;
* the data-class rule: every kind is free of direct identifiers, including when
  the subject carries a guest email/phone;
* simple HTML (no ``<script``, no ``javascript:``, no inline handler) plus a
  plain-text export;
* ``verify`` re-hashing the file and reporting a missing/changed one;
* listing/latest/status transitions (GENERATED -> SUPERSEDED/VOID).

Invoices are seeded with SQL rather than through ``invoicing.InvoiceService``:
that service is currently unusable for *any* caller (``Invoice.as_dict`` calls
``Money.as_dict``, but the attribute is ``Money.to_dict``), and this suite must not
edit another module. The rows it writes are the same rows the invoice module
would have written, and ``documents`` reconciles against them with
``invoicing.derive_totals`` — the point of the reconciliation tests.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import cases as C
import documents as DOC
import gates as G
import primitives as P
from audit import contains_direct_identifiers
from schema_loader import connect_sqlite

REPO_ROOT = Path(__file__).resolve().parents[1]
START = "2026-09-21T08:00:00Z"
NOW_ISO = P.to_iso(P.parse_iso(START))
FINGERPRINT = "0" * 64
EMAIL = "budi@example.com"
PHONE = "081234567890"
CARD_LIKE = "4111 1111 1111 1111"
LONG_DIGITS = "123456789012"

INVOICE_COLUMNS = (
    "invoice_id",
    "invoice_number",
    "booking_id",
    "currency",
    "subtotal_minor",
    "tax_minor",
    "service_minor",
    "total_minor",
    "amount_paid_minor",
    "status",
    "payment_status",
    "issued_at",
    "due_at",
)


# --------------------------------------------------------------------------- #
# Seeding helpers (the parent chain a bookings row needs, then the rows
# documents reconcile against)
# --------------------------------------------------------------------------- #
def seed_property_chain(conn) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO properties (property_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('prop_1', 'Villa Satu', 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE',"
        " 'ACTIVATED', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
        " base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES ('unit_1', 'prop_1', 'Unit Satu', 'entire_villa', 2, 2, 2, 4, 'ACTIVE',"
        " '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO policy_versions (policy_version_id, property_id, unit_scope,"
        " version, effective_from, effective_to, currency, deposit_pct, service_fee_pct,"
        " tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,"
        " max_hold_extensions, payload, fingerprint, source_doc_ref, approved_by, created_at)"
        " VALUES ('policy_v1', 'prop_1', 'property', 1, '2026-01-01', '2027-01-01', 'IDR', '30',"
        " '10', '5', '10', 48, 60, 1, '{}', ?, 'doc.pdf', 'ops_1',"
        " '2026-09-01T00:00:00+00:00')",
        (FINGERPRINT,),
    )


def seed_booking(
    conn,
    booking_id: str,
    *,
    status: str = "CONFIRMED",
    check_in: str = "2026-10-01",
    check_out: str = "2026-10-03",
    nights: int = 2,
    adults: int = 2,
    children: int = 1,
    infants: int = 0,
    total_minor: int = 1500000,
) -> str:
    conn.execute(
        "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
        " status, guest_ref, adults, children, infants, currency, policy_version_id,"
        " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
        " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
        " updated_at, confirmed_at, cancelled_at)"
        " VALUES (?, 'prop_1', 'unit_1', ?, ?, ?, ?, 'guest_0001', ?, ?, ?, 'IDR',"
        " 'policy_v1', ?, 'quote_1', 'hold_1', ?, 0, 0, 'UNPAID', ?, 1, ?, ?, ?, NULL)",
        (
            booking_id,
            check_in,
            check_out,
            nights,
            status,
            adults,
            children,
            infants,
            FINGERPRINT,
            total_minor,
            "idem_" + booking_id,
            START,
            START,
            START,
        ),
    )
    return booking_id


def seed_invoice(
    conn,
    invoice_id: str = "inv_1",
    *,
    invoice_number: str = "INV-202609-00001",
    booking_id: str = "book_1",
    subtotal_minor: int = 1500000,
    tax_minor: int = 75000,
    service_minor: int = 150000,
    amount_paid_minor: int = 0,
    status: str = "ISSUED",
    payment_status: str = "UNPAID",
) -> str:
    total_minor = subtotal_minor + tax_minor + service_minor
    voided_at = START if status == "VOID" else None
    void_reason = "dibatalkan" if status == "VOID" else None
    conn.execute(
        "INSERT INTO invoices (invoice_id, invoice_number, booking_id, policy_fingerprint,"
        " currency, subtotal_minor, tax_minor, service_minor, total_minor, amount_paid_minor,"
        " status, payment_status, issued_at, due_at, voided_at, void_reason, created_at)"
        " VALUES (?, ?, ?, ?, 'IDR', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            invoice_id,
            invoice_number,
            booking_id,
            FINGERPRINT,
            subtotal_minor,
            tax_minor,
            service_minor,
            total_minor,
            amount_paid_minor,
            status,
            payment_status,
            START,
            "2026-09-28T08:00:00+00:00",
            voided_at,
            void_reason,
            START,
        ),
    )
    lines = [
        ("line_1", "NIGHTLY", "Villa dua malam", 2, subtotal_minor // 2, subtotal_minor, 2),
        ("line_2", "TAX", "PBJT lima persen", 1, tax_minor, tax_minor, None),
        ("line_3", "SERVICE", "Service sepuluh persen", 1, service_minor, service_minor, None),
    ]
    for line_id, kind, description, quantity, unit_minor, amount_minor, nights in lines:
        conn.execute(
            "INSERT INTO invoice_lines (line_id, invoice_id, kind, description, quantity,"
            " unit_amount_minor, amount_minor, rate_card_id, nights, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                line_id + "_" + invoice_id,
                invoice_id,
                kind,
                description,
                quantity,
                unit_minor,
                amount_minor,
                nights,
                START,
            ),
        )
    return invoice_id


def digits(text: str) -> str:
    """Digits of a rendered amount: ``IDR 1.725.000`` -> ``1725000``."""
    return re.sub(r"\D", "", text)


# --------------------------------------------------------------------------- #
# Shared rig
# --------------------------------------------------------------------------- #
class DocumentTestBase(unittest.TestCase):
    """In-memory SQLite + frozen clock + deterministic ids + a throwaway root."""

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.clock = P.FrozenClock(P.parse_iso(START))
        self.ids = P.IdFactory(deterministic=True, seed="documents")
        self.tmp = Path(tempfile.mkdtemp(prefix="villa-documents-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "documents"
        self.svc = DOC.DocumentService(self.conn, clock=self.clock, ids=self.ids, root=self.root)
        seed_property_chain(self.conn)
        seed_booking(self.conn, "book_1")
        seed_booking(
            self.conn, "book_2", status="INQUIRY", check_in="2026-11-01", check_out="2026-11-03"
        )
        seed_invoice(self.conn)

    # -- helpers ---------------------------------------------------------
    def generate(self, **overrides):
        kwargs = {
            "kind": "CONFIRMATION",
            "document_key": "doc.default_1",
            "subject": {"booking_id": "book_1"},
            "generated_by": "agent_01",
            "actor_role": "agent",
        }
        kwargs.update(overrides)
        return self.svc.generate(**kwargs)

    def refuse(self, code: str, func, *args, **kwargs) -> P.DomainError:
        with self.assertRaises(P.DomainError) as caught:
            func(*args, **kwargs)
        exc = caught.exception
        self.assertEqual(exc.code, code, f"kode error salah: {exc.code} ({exc})")
        return exc

    def body_of(self, document) -> str:
        return (self.root / document.storage_ref).read_text(encoding="utf-8")

    def files(self) -> tuple[str, ...]:
        if not self.root.exists():
            return ()
        return tuple(
            sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file())
        )

    def trace(self) -> tuple[int, tuple[str, ...]]:
        """Rows + files: everything a write could have changed."""
        rows = int(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        return (rows, self.files())

    def assert_no_trace(self, before, message: str = "") -> None:
        self.assertEqual(self.trace(), before, message or "a refused call wrote something")
        self.assertFalse((self.tmp / "escape").exists(), "a file escaped the document root")

    def dd(self, body: str, label: str) -> str:
        """Read one ``<dt>label</dt><dd>value</dd>`` value out of a body."""
        found = re.findall(rf"<dt>{re.escape(label)}</dt>\n<dd>([^<]*)</dd>", body)
        self.assertTrue(found, f"label {label!r} not found in the body")
        return found[0]

    def open_case(self) -> str:
        service = C.CaseService(
            self.conn, clock=self.clock, ids=P.IdFactory(deterministic=True, seed="cases")
        )
        case, _message = service.open_case(
            channel="website",
            guest_ref="guest_0001",
            content_class="P1",
            body="Halo, apakah ada villa kosong akhir bulan?",
            idempotency_key="idem_case_doc_0001",
        )
        return case.case_id

    def invoice_row(self, invoice_id: str = "inv_1"):
        return self.conn.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()


# --------------------------------------------------------------------------- #
# Contract surface
# --------------------------------------------------------------------------- #
class TestContractSurface(DocumentTestBase):
    def test_kind_vocabulary_matches_the_schema_check(self):
        self.assertEqual(
            DOC.DOCUMENT_KINDS,
            ("CONFIRMATION", "RECEIPT", "INVOICE", "CONTRACT", "NOTICE", "REPORT"),
        )
        schema = (REPO_ROOT / "src" / "schema" / "001_initial.sql").read_text(encoding="utf-8")
        for kind in DOC.DOCUMENT_KINDS:
            self.assertIn(f"'{kind}'", schema, f"{kind} is not a schema document kind")

    def test_status_and_format_vocabulary_matches_the_schema(self):
        self.assertEqual(DOC.DOCUMENT_STATUSES, ("GENERATED", "SUPERSEDED", "VOID"))
        self.assertEqual(DOC.DOCUMENT_FORMATS, ("HTML", "TEXT", "MARKDOWN"))
        self.assertEqual(DOC.DOCUMENT_FORMAT, "HTML")

    def test_document_columns_match_the_table(self):
        actual = tuple(row[1] for row in self.conn.execute("PRAGMA table_info(documents)"))
        self.assertEqual(DOC.DOCUMENT_COLUMNS, actual)

    def test_content_class_is_p1_and_allowed_to_leave(self):
        self.assertEqual(DOC.DOCUMENT_CONTENT_CLASS, "P1")
        self.assertIn(DOC.DOCUMENT_CONTENT_CLASS, G.CLASSIFICATIONS)
        document = self.generate()
        body = self.body_of(document)
        for destination in (G.DESTINATION_EXPORT, G.DESTINATION_EMAIL, G.DESTINATION_LOG):
            decision = G.gate_outbound(
                [G.ClassifiedField(name="body", classification=DOC.DOCUMENT_CONTENT_CLASS, value=body)],
                destination=destination,
                actor_role="agent",
            )
            self.assertTrue(decision.allowed, f"{destination} refused a P1 document body")

    def test_role_policy_covers_every_kind(self):
        self.assertEqual(set(DOC.DOCUMENT_ROLE_POLICY), set(DOC.DOCUMENT_KINDS))
        for kind, roles in DOC.DOCUMENT_ROLE_POLICY.items():
            self.assertTrue(roles, f"{kind} has no allowed role")
            self.assertIn("system", roles, f"{kind} cannot be issued by the kernel")

    def test_default_root_is_the_repo_documents_directory(self):
        self.assertEqual(DOC.default_root(), REPO_ROOT / DOC.DOCUMENT_ROOT_NAME)
        self.assertEqual(Path(DOC.DocumentService(self.conn).root), REPO_ROOT / "documents")
        self.assertFalse(
            (REPO_ROOT / "documents").exists(),
            "a test wrote into the repository instead of its own root",
        )

    def test_service_exposes_injected_collaborators(self):
        self.assertIs(self.svc.conn, self.conn)
        self.assertIs(self.svc.clock, self.clock)
        self.assertIs(self.svc.ids, self.ids)
        self.assertEqual(self.svc.root, self.root)


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #
class TestKeySafety(DocumentTestBase):
    def test_traversal_key_is_refused_before_anything_is_written(self):
        before = self.trace()
        for key in ("../escape", "a/../b", "confirm.book_1/../../escape"):
            self.refuse(
                DOC.UNSAFE_DOCUMENT_KEY,
                self.generate,
                document_key=key,
            )
        self.assert_no_trace(before)

    def test_absolute_key_is_refused(self):
        before = self.trace()
        for key in ("/etc/passwd", "/tmp/documents/escape"):
            self.refuse(DOC.UNSAFE_DOCUMENT_KEY, self.generate, document_key=key)
        self.assert_no_trace(before)

    def test_separator_key_is_refused(self):
        before = self.trace()
        for key in ("confirm/book_1", "confirm\\book_1"):
            self.refuse(DOC.UNSAFE_DOCUMENT_KEY, self.generate, document_key=key)
        self.assert_no_trace(before)

    def test_dot_dot_inside_a_token_is_refused(self):
        before = self.trace()
        self.refuse(DOC.UNSAFE_DOCUMENT_KEY, self.generate, document_key="booking..1")
        self.assert_no_trace(before)

    def test_short_and_malformed_keys_are_refused(self):
        before = self.trace()
        for key in ("short", "Bad Key!", "Doc:1", "Upper.Case", "doc@1"):
            self.refuse(DOC.INVALID_DOCUMENT_REQUEST, self.generate, document_key=key)
        self.assert_no_trace(before)

    def test_a_blank_or_non_string_key_is_refused(self):
        for key in (None, 12, "      "):
            with self.assertRaises(P.DomainError):
                self.generate(document_key=key)
        self.refuse("MISSING_FIELD", self.generate, document_key="")

    def test_no_file_lands_outside_the_root(self):
        before = self.trace()
        self.refuse(DOC.UNSAFE_DOCUMENT_KEY, self.generate, document_key="../escape")
        self.refuse(
            DOC.UNSAFE_DOCUMENT_KEY,
            self.generate,
            kind="RECEIPT",
            document_key="/etc/passwd",
            subject={"invoice_id": "inv_1"},
            generated_by="finance_01",
            actor_role="finance",
        )
        self.assert_no_trace(before)
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), [])

    def test_resolve_storage_path_refuses_every_escape(self):
        for ref in ("../x", "/etc/passwd", "a/../../b", "a//b", "a/./b", "", "C:/x", "a\\b"):
            with self.assertRaises(P.DomainError) as caught:
                DOC.resolve_storage_path(self.root, ref)
            self.assertIn(
                caught.exception.code,
                (DOC.DOCUMENT_STORAGE_INVALID, "MISSING_FIELD"),
                f"{ref!r} resolved to {caught.exception}",
            )

    def test_resolve_storage_path_accepts_the_paths_it_builds(self):
        document = self.generate()
        resolved = DOC.resolve_storage_path(self.root, document.storage_ref)
        self.assertEqual(resolved, self.root / document.storage_ref)
        self.assertTrue(str(resolved).startswith(str(self.root)))

    def test_lookup_validates_the_key_shape(self):
        self.refuse(DOC.UNSAFE_DOCUMENT_KEY, self.svc.latest, "../escape")
        self.refuse(DOC.INVALID_DOCUMENT_REQUEST, self.svc.latest, "short")


# --------------------------------------------------------------------------- #
# Versioning and supersede
# --------------------------------------------------------------------------- #
class TestVersioning(DocumentTestBase):
    def test_first_issue_is_version_one_and_generated(self):
        document = self.generate()
        self.assertEqual(document.version, 1)
        self.assertEqual(document.status, "GENERATED")
        self.assertEqual(document.kind, "CONFIRMATION")
        self.assertEqual(document.format, "HTML")
        self.assertEqual(document.redaction_profile, DOC.REDACTION_PROFILE)
        self.assertEqual(len(document.body_sha256), 64)
        self.assertEqual(document.booking_id, "book_1")
        self.assertEqual(document.generated_by, "agent_01")
        self.assertTrue(document.is_active)

    def test_second_issue_supersedes_the_first(self):
        first = self.generate()
        second = self.generate()
        self.assertEqual(second.version, 2)
        self.assertEqual(second.status, "GENERATED")
        self.assertEqual(self.svc.get(first.document_id).status, "SUPERSEDED")
        self.assertNotEqual(first.document_id, second.document_id)
        self.assertNotEqual(first.storage_ref, second.storage_ref)

    def test_third_issue_piles_up_and_keeps_every_file(self):
        first = self.generate()
        second = self.generate()
        third = self.generate()
        self.assertEqual([first.version, second.version, third.version], [1, 2, 3])
        self.assertEqual(self.svc.get(first.document_id).status, "SUPERSEDED")
        self.assertEqual(self.svc.get(second.document_id).status, "SUPERSEDED")
        self.assertEqual(self.document_kinds(), 3)
        self.assertEqual(len(self.files()), 3)

    def document_kinds(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def test_versions_are_unique_per_key(self):
        for _ in range(3):
            self.generate()
        rows = [
            row[0]
            for row in self.conn.execute(
                "SELECT COUNT(*) FROM documents GROUP BY document_key, version"
            ).fetchall()
        ]
        self.assertEqual(rows, [1, 1, 1])

    def test_the_same_key_keeps_one_active_version_only(self):
        self.generate()
        self.generate()
        active = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE document_key = 'doc.default_1'"
            " AND status = 'GENERATED'"
        ).fetchone()[0]
        self.assertEqual(int(active), 1)

    def test_latest_returns_the_newest_version(self):
        self.generate()
        newest = self.generate()
        self.assertEqual(self.svc.latest("doc.default_1"), newest)

    def test_latest_refuses_an_unknown_key(self):
        self.refuse(DOC.DOCUMENT_NOT_FOUND, self.svc.latest, "booking.unknown_1")

    def test_a_new_key_starts_again_at_version_one(self):
        self.generate()
        other = self.generate(document_key="doc.other_1")
        self.assertEqual(other.version, 1)

    def test_files_are_never_overwritten(self):
        first = self.generate(subject={"booking_id": "book_1", "notes": "versi pertama"})
        payload = (self.root / first.storage_ref).read_bytes()
        second = self.generate(subject={"booking_id": "book_1", "notes": "versi kedua"})
        self.assertEqual((self.root / first.storage_ref).read_bytes(), payload)
        self.assertIn("versi pertama", self.body_of(first))
        self.assertIn("versi kedua", self.body_of(second))


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
class TestDeterminism(DocumentTestBase):
    def test_same_inputs_and_instant_produce_identical_bytes(self):
        subject = {"booking_id": "book_1", "notes": "catatan sama"}
        first = self.generate(subject=subject)
        second = self.generate(subject=subject)
        self.assertEqual(first.body_sha256, second.body_sha256)
        self.assertEqual(self.body_of(first), self.body_of(second))
        self.assertNotEqual(first.version, second.version)

    def test_bytes_on_disk_match_the_stored_hash(self):
        document = self.generate()
        payload = (self.root / document.storage_ref).read_bytes()
        self.assertEqual(document.body_sha256, P.sha256_hex(payload.decode("utf-8")))

    def test_a_different_instant_changes_the_bytes(self):
        first = self.generate(subject={"booking_id": "book_1"})
        self.clock.advance(minutes=5)
        second = self.generate(subject={"booking_id": "book_1"})
        self.assertNotEqual(self.body_of(first), self.body_of(second))
        self.assertNotEqual(first.body_sha256, second.body_sha256)

    def test_the_timestamp_lives_in_the_body(self):
        document = self.generate()
        body = self.body_of(document)
        self.assertIn(NOW_ISO, body)
        self.assertEqual(document.generated_at, NOW_ISO)
        self.assertEqual(self.dd(body, "Stempel waktu"), NOW_ISO)

    def test_an_explicit_instant_overrides_the_clock(self):
        document = self.generate(at="2026-12-24T10:00:00Z")
        self.assertEqual(document.generated_at, "2026-12-24T10:00:00+00:00")
        self.assertIn("2026-12-24T10:00:00+00:00", self.body_of(document))

    def test_the_body_does_not_embed_the_document_id_or_version(self):
        first = self.generate()
        body = self.body_of(first)
        self.assertNotIn(first.document_id, body)
        self.assertNotIn("v0001", body)
        self.assertNotIn("v0002", body)

    def test_the_clock_drives_the_default_instant(self):
        self.clock.advance(hours=2)
        document = self.generate()
        self.assertEqual(document.generated_at, P.to_iso(self.clock.now()))

    def test_a_naive_instant_is_refused(self):
        before = self.trace()
        self.refuse(
            "NAIVE_TIMESTAMP",
            self.generate,
            at="2026-12-24T10:00:00",
        )
        self.assert_no_trace(before)


# --------------------------------------------------------------------------- #
# Financial reconciliation
# --------------------------------------------------------------------------- #
class TestFinanceReconciliation(DocumentTestBase):
    def receipt(self, **overrides):
        kwargs = {
            "kind": "RECEIPT",
            "document_key": "receipt.inv_1",
            "subject": {"invoice_id": "inv_1"},
            "generated_by": "finance_01",
            "actor_role": "finance",
        }
        kwargs.update(overrides)
        return self.generate(**kwargs)

    def test_receipt_carries_the_stored_number_currency_and_total(self):
        row = self.invoice_row()
        body = self.body_of(self.receipt())
        self.assertEqual(self.dd(body, "Nomor invoice"), row["invoice_number"])
        self.assertEqual(self.dd(body, "Mata uang"), row["currency"])
        self.assertIn(row["invoice_number"], body)
        self.assertEqual(digits(self.dd(body, "Total")), str(row["total_minor"]))
        self.assertEqual(self.dd(body, "Total"), DOC.format_amount(
            P.Money(int(row["total_minor"]), "IDR"), "id-ID"
        ))

    def test_receipt_carries_the_paid_and_outstanding_amounts(self):
        row = self.invoice_row()
        body = self.body_of(self.receipt())
        self.assertEqual(digits(self.dd(body, "Sudah dibayar")), str(row["amount_paid_minor"]))
        expected = int(row["total_minor"]) - int(row["amount_paid_minor"])
        self.assertEqual(digits(self.dd(body, "Sisa tagihan")), str(expected))
        self.assertEqual(self.dd(body, "Status pembayaran"), row["payment_status"])

    def test_invoice_document_matches_the_invoice_row(self):
        row = self.invoice_row()
        document = self.generate(
            kind="INVOICE",
            document_key="invoice.inv_1",
            subject={"invoice_id": "inv_1"},
            generated_by="finance_01",
            actor_role="finance",
        )
        body = self.body_of(document)
        self.assertEqual(document.invoice_id, "inv_1")
        self.assertEqual(document.booking_id, "book_1")
        self.assertEqual(self.dd(body, "Nomor invoice"), row["invoice_number"])
        for label, column in (
            ("Subtotal", "subtotal_minor"),
            ("Pajak", "tax_minor"),
            ("Service", "service_minor"),
            ("Total", "total_minor"),
        ):
            self.assertEqual(digits(self.dd(body, label)), str(row[column]))

    def test_every_invoice_line_is_rendered_from_the_table(self):
        lines = self.conn.execute(
            "SELECT kind, amount_minor FROM invoice_lines WHERE invoice_id = 'inv_1'"
            " ORDER BY line_id"
        ).fetchall()
        body = self.body_of(self.receipt())
        rendered = re.findall(r"<td>([^<]*)</td>", body)
        for line in lines:
            self.assertIn(
                DOC.format_amount(P.Money(int(line["amount_minor"]), "IDR"), "id-ID"),
                rendered,
                f"line {line['kind']} missing from the rendered table",
            )
        self.assertEqual(len(lines), 3)

    def test_a_total_claim_that_disagrees_with_the_invoice_is_refused(self):
        before = self.trace()
        self.refuse(
            DOC.INVOICE_SOURCE_MISMATCH,
            self.receipt,
            subject={"invoice_id": "inv_1", "total_minor": 999},
        )
        self.assert_no_trace(before)

    def test_a_currency_claim_that_disagrees_with_the_invoice_is_refused(self):
        before = self.trace()
        self.refuse(
            DOC.INVOICE_SOURCE_MISMATCH,
            self.receipt,
            subject={"invoice_id": "inv_1", "currency": "USD"},
        )
        self.assert_no_trace(before)

    def test_a_number_claim_that_disagrees_with_the_invoice_is_refused(self):
        before = self.trace()
        self.refuse(
            DOC.INVOICE_SOURCE_MISMATCH,
            self.receipt,
            subject={"invoice_id": "inv_1", "invoice_number": "INV-202609-09999"},
        )
        self.assert_no_trace(before)

    def test_matching_claims_are_accepted(self):
        row = self.invoice_row()
        document = self.receipt(
            subject={
                "invoice_id": "inv_1",
                "total_minor": int(row["total_minor"]),
                "currency": row["currency"],
                "invoice_number": row["invoice_number"],
            }
        )
        self.assertEqual(digits(self.dd(self.body_of(document), "Total")), str(row["total_minor"]))

    def test_lines_that_do_not_reconcile_stop_the_document(self):
        self.conn.execute(
            "INSERT INTO invoice_lines (line_id, invoice_id, kind, description, quantity,"
            " unit_amount_minor, amount_minor, rate_card_id, nights, created_at)"
            " VALUES ('line_4_inv_1', 'inv_1', 'OTHER', 'Extra line', 1, 1000, 1000, NULL, NULL, ?)",
            (START,),
        )
        before = self.trace()
        exc = self.refuse(DOC.INVOICE_SOURCE_MISMATCH, self.receipt)
        self.assertEqual(exc.details["derived"]["total_minor"], 1726000)
        self.assertEqual(exc.details["stored"]["total_minor"], 1725000)
        self.assert_no_trace(before)

    def test_totals_that_disagree_with_their_own_lines_are_refused(self):
        # A discount row flipped positive is a pricing bug the source module
        # refuses; documents must not paper over it.
        self.conn.execute(
            "UPDATE invoice_lines SET amount_minor = -500000, unit_amount_minor = -250000"
            " WHERE line_id = 'line_1_inv_1'"
        )
        before = self.trace()
        self.refuse(DOC.INVOICE_SOURCE_MISMATCH, self.receipt)
        self.assert_no_trace(before)

    def test_an_unknown_invoice_is_refused(self):
        before = self.trace()
        self.refuse(
            DOC.DOCUMENT_SOURCE_MISSING,
            self.receipt,
            subject={"invoice_id": "inv_missing"},
        )
        self.assert_no_trace(before)

    def test_a_void_invoice_is_refused(self):
        seed_invoice(self.conn, "inv_void", invoice_number="INV-202609-00042", status="VOID")
        before = self.trace()
        self.refuse(
            DOC.DOCUMENT_SOURCE_INVALID,
            self.receipt,
            document_key="receipt.inv_void",
            subject={"invoice_id": "inv_void"},
        )
        self.assert_no_trace(before)

    def test_a_booking_claim_that_disagrees_with_the_invoice_is_refused(self):
        seed_booking(
            self.conn, "book_9", check_in="2026-12-01", check_out="2026-12-03"
        )
        before = self.trace()
        self.refuse(
            DOC.INVOICE_SOURCE_MISMATCH,
            self.receipt,
            subject={"invoice_id": "inv_1", "booking_id": "book_9"},
        )
        self.assert_no_trace(before)

    def test_a_financial_document_needs_an_invoice_id(self):
        before = self.trace()
        for kind, key in (("RECEIPT", "receipt.none_1"), ("INVOICE", "invoice.none_1")):
            self.refuse(
                DOC.INVALID_DOCUMENT_REQUEST,
                self.generate,
                kind=kind,
                document_key=key,
                subject={"booking_id": "book_1"},
                generated_by="finance_01",
                actor_role="finance",
            )
        self.assert_no_trace(before)

    def test_a_claim_without_a_source_is_refused(self):
        before = self.trace()
        self.refuse(
            DOC.INVALID_DOCUMENT_REQUEST,
            self.generate,
            kind="NOTICE",
            document_key="notice.book_1",
            subject={"booking_id": "book_1", "total_minor": 1000},
            generated_by="manager_01",
            actor_role="manager",
        )
        self.assert_no_trace(before)

    def test_a_notice_cannot_reference_an_invoice(self):
        before = self.trace()
        self.refuse(
            DOC.INVALID_DOCUMENT_REQUEST,
            self.generate,
            kind="NOTICE",
            document_key="notice.book_1",
            subject={"booking_id": "book_1", "invoice_id": "inv_1"},
            generated_by="manager_01",
            actor_role="manager",
        )
        self.assert_no_trace(before)


# --------------------------------------------------------------------------- #
# Booking reconciliation
# --------------------------------------------------------------------------- #
class TestBookingReconciliation(DocumentTestBase):
    def test_confirmation_carries_the_stay_and_the_guest_count(self):
        row = self.conn.execute("SELECT * FROM bookings WHERE booking_id = 'book_1'").fetchone()
        body = self.body_of(self.generate())
        self.assertEqual(self.dd(body, "Check-in"), row["check_in"])
        self.assertEqual(self.dd(body, "Check-out"), row["check_out"])
        guests = int(row["adults"]) + int(row["children"]) + int(row["infants"])
        self.assertEqual(self.dd(body, "Jumlah tamu"), str(guests))
        self.assertEqual(self.dd(body, "Malam"), str(row["nights"]))
        self.assertIn(row["check_in"], body)
        self.assertIn(row["check_out"], body)
        self.assertEqual(digits(self.dd(body, "Total")), str(row["total_minor"]))

    def test_the_guest_count_sums_the_three_columns(self):
        seed_booking(
            self.conn,
            "book_3",
            check_in="2026-12-05",
            check_out="2026-12-07",
            adults=2,
            children=1,
            infants=1,
        )
        body = self.body_of(
            self.generate(document_key="confirm.book_3", subject={"booking_id": "book_3"})
        )
        self.assertEqual(self.dd(body, "Jumlah tamu"), "4")

    def test_the_booking_row_is_read_at_issue_time_not_cached(self):
        self.conn.execute(
            "UPDATE bookings SET total_minor = 2000000 WHERE booking_id = 'book_1'"
        )
        body = self.body_of(self.generate())
        self.assertEqual(digits(self.dd(body, "Total")), "2000000")

    def test_a_booking_total_claim_that_disagrees_is_refused(self):
        before = self.trace()
        self.refuse(
            DOC.DOCUMENT_SOURCE_MISMATCH,
            self.generate,
            subject={"booking_id": "book_1", "total_minor": 1},
        )
        self.assert_no_trace(before)

    def test_a_matching_booking_claim_is_accepted(self):
        document = self.generate(
            subject={"booking_id": "book_1", "total_minor": 1500000, "currency": "IDR"}
        )
        self.assertEqual(digits(self.dd(self.body_of(document), "Total")), "1500000")

    def test_a_booking_that_cannot_be_confirmed_is_refused(self):
        before = self.trace()
        self.refuse(
            DOC.DOCUMENT_SOURCE_INVALID,
            self.generate,
            document_key="confirm.book_2",
            subject={"booking_id": "book_2"},
        )
        self.assert_no_trace(before)

    def test_an_unknown_booking_is_refused(self):
        before = self.trace()
        self.refuse(
            DOC.DOCUMENT_SOURCE_MISSING,
            self.generate,
            document_key="confirm.book_missing",
            subject={"booking_id": "book_missing"},
        )
        self.assert_no_trace(before)

    def test_a_confirmation_needs_a_booking_id(self):
        before = self.trace()
        self.refuse(
            DOC.INVALID_DOCUMENT_REQUEST, self.generate, subject={"notes": "tanpa booking"}
        )
        self.assert_no_trace(before)

    def test_an_offer_letter_carries_the_booking_total_and_validity(self):
        document = self.generate(
            kind="CONTRACT",
            document_key="offer.book_1",
            subject={"booking_id": "book_1"},
            generated_by="manager_01",
            actor_role="manager",
        )
        body = self.body_of(document)
        self.assertIn("Surat Penawaran", body)
        self.assertIn(str(DOC.OFFER_VALIDITY_HOURS), body)
        self.assertEqual(digits(self.dd(body, "Penawaran total")), "1500000")

    def test_an_offer_letter_refuses_a_dead_booking(self):
        seed_booking(
            self.conn,
            "book_dead",
            status="CANCELLED",
            check_in="2026-12-20",
            check_out="2026-12-22",
        )
        before = self.trace()
        self.refuse(
            DOC.DOCUMENT_SOURCE_INVALID,
            self.generate,
            kind="CONTRACT",
            document_key="offer.book_dead",
            subject={"booking_id": "book_dead"},
            generated_by="manager_01",
            actor_role="manager",
        )
        self.assert_no_trace(before)

    def test_a_notice_needs_a_booking_or_a_case(self):
        before = self.trace()
        self.refuse(
            DOC.INVALID_DOCUMENT_REQUEST,
            self.generate,
            kind="NOTICE",
            document_key="notice.none_1",
            subject={"notes": "tanpa rujukan"},
            generated_by="manager_01",
            actor_role="manager",
        )
        self.assert_no_trace(before)

    def test_a_notice_references_a_case_and_an_unknown_case_is_refused(self):
        case_id = self.open_case()
        document = self.generate(
            kind="NOTICE",
            document_key="notice.case_1",
            subject={"case_id": case_id},
            generated_by="manager_01",
            actor_role="manager",
        )
        self.assertEqual(document.case_id, case_id)
        self.assertEqual(self.dd(self.body_of(document), "Kode kasus"), case_id)
        before = self.trace()
        self.refuse(
            DOC.DOCUMENT_SOURCE_MISSING,
            self.generate,
            kind="NOTICE",
            document_key="notice.case_2",
            subject={"case_id": "case_missing"},
            generated_by="manager_01",
            actor_role="manager",
        )
        self.assert_no_trace(before)


# --------------------------------------------------------------------------- #
# Data class
# --------------------------------------------------------------------------- #
class TestDataClass(DocumentTestBase):
    def issue_all_kinds(self) -> dict:
        case_id = self.open_case()
        documents = {
            "CONFIRMATION": self.generate(document_key="all.confirmation_1"),
            "RECEIPT": self.generate(
                kind="RECEIPT",
                document_key="all.receipt_1",
                subject={"invoice_id": "inv_1", "contact_email": EMAIL, "contact_phone": PHONE},
                generated_by="finance_01",
                actor_role="finance",
            ),
            "INVOICE": self.generate(
                kind="INVOICE",
                document_key="all.invoice_1",
                subject={"invoice_id": "inv_1"},
                generated_by="finance_01",
                actor_role="finance",
            ),
            "CONTRACT": self.generate(
                kind="CONTRACT",
                document_key="all.contract_1",
                subject={"booking_id": "book_1"},
                generated_by="manager_01",
                actor_role="manager",
            ),
            "NOTICE": self.generate(
                kind="NOTICE",
                document_key="all.notice_1",
                subject={"case_id": case_id, "contact_email": EMAIL},
                generated_by="manager_01",
                actor_role="manager",
            ),
            "REPORT": self.generate(
                kind="REPORT",
                document_key="all.report_1",
                subject={"booking_id": "book_1"},
                generated_by="manager_01",
                actor_role="manager",
            ),
        }
        return documents

    def test_every_kind_is_free_of_direct_identifiers(self):
        for kind, document in self.issue_all_kinds().items():
            body = self.body_of(document)
            self.assertFalse(
                contains_direct_identifiers(body), f"{kind} body carries an identifier"
            )
            self.assertEqual(P.redact(body), body, f"{kind} body would be redacted further")
            self.assertNotIn(EMAIL, body)
            self.assertNotIn(PHONE, body)

    def test_the_stored_rows_are_declared_p1(self):
        for _kind, document in self.issue_all_kinds().items():
            self.assertEqual(document.content_class, "P1")
        rows = self.conn.execute("SELECT DISTINCT content_class FROM documents").fetchall()
        self.assertEqual([row[0] for row in rows], ["P1"])

    def test_contact_identifiers_are_redacted_not_stored(self):
        document = self.generate(
            subject={"booking_id": "book_1", "contact_email": EMAIL, "contact_phone": PHONE}
        )
        body = self.body_of(document)
        self.assertIn("[EMAIL]", body)
        self.assertIn("[PHONE]", body)
        self.assertNotIn(EMAIL, body)
        self.assertNotIn(PHONE, body)
        self.assertFalse(contains_direct_identifiers(body))

    def test_an_identifier_in_notes_is_redacted(self):
        document = self.generate(
            subject={"booking_id": "book_1", "notes": f"hubungi {EMAIL} atau {PHONE}"}
        )
        body = self.body_of(document)
        self.assertIn("[EMAIL]", body)
        self.assertIn("[PHONE]", body)
        self.assertFalse(contains_direct_identifiers(body))

    def test_an_unvetted_subject_field_is_refused(self):
        before = self.trace()
        for field in ("guest_name", "guest_full_name", "ktp_number"):
            self.refuse(
                DOC.UNKNOWN_SUBJECT_FIELD,
                self.generate,
                subject={"booking_id": "book_1", field: "apa pun"},
            )
        self.assert_no_trace(before)

    def test_an_unknown_field_name_does_not_echo_its_value(self):
        before = self.trace()
        exc = self.refuse(
            DOC.UNKNOWN_SUBJECT_FIELD,
            self.generate,
            subject={"booking_id": "book_1", "guest_email": EMAIL},
        )
        self.assertNotIn(EMAIL, str(exc))
        self.assert_no_trace(before)

    def test_an_unknown_field_name_that_is_itself_an_identifier_is_redacted(self):
        before = self.trace()
        exc = self.refuse(
            DOC.UNKNOWN_SUBJECT_FIELD,
            self.generate,
            subject={"booking_id": "book_1", EMAIL: "x"},
        )
        self.assertNotIn(EMAIL, str(exc))
        self.assert_no_trace(before)

    def test_a_source_id_that_looks_like_an_identifier_stops_the_document(self):
        # Nothing cleans a booking_id on the way out (it is a ref by contract);
        # the body guard is what catches a row whose id is a raw identifier.
        before = self.trace()
        seed_booking(
            self.conn,
            LONG_DIGITS,
            check_in="2027-01-05",
            check_out="2027-01-07",
        )
        self.refuse(
            DOC.DOCUMENT_IDENTIFIER_LEAK,
            self.generate,
            document_key="confirm.long_digits",
            subject={"booking_id": LONG_DIGITS},
        )
        self.assert_no_trace(before)

    def test_a_card_like_run_redact_misses_is_still_refused(self):
        self.assertEqual(P.redact(CARD_LIKE), CARD_LIKE)  # redact alone lets this through
        before = self.trace()
        self.refuse(
            DOC.DOCUMENT_IDENTIFIER_LEAK,
            self.generate,
            subject={"booking_id": "book_1", "notes": CARD_LIKE},
        )
        self.assert_no_trace(before)

    def test_a_contact_field_that_is_not_an_identifier_is_refused(self):
        before = self.trace()
        self.refuse(
            DOC.INVALID_DOCUMENT_REQUEST,
            self.generate,
            subject={"booking_id": "book_1", "contact_email": "budi.example.com"},
        )
        self.assert_no_trace(before)

    def test_an_already_masked_contact_value_is_accepted(self):
        document = self.generate(
            subject={"booking_id": "book_1", "contact_phone": "[PHONE]"}
        )
        self.assertIn("[PHONE]", self.body_of(document))

    def test_assert_identifier_free_names_the_field(self):
        with self.assertRaises(P.DomainError) as caught:
            DOC.assert_identifier_free(f"x {EMAIL}", field="title")
        exc = caught.exception
        self.assertEqual(exc.code, DOC.DOCUMENT_IDENTIFIER_LEAK)
        self.assertEqual(exc.details["field"], "title")
        self.assertNotIn(EMAIL, str(exc))


# --------------------------------------------------------------------------- #
# Format
# --------------------------------------------------------------------------- #
class TestFormat(DocumentTestBase):
    def test_bodies_carry_no_script_token_or_scheme(self):
        case_id = self.open_case()
        bodies = [
            self.body_of(self.generate(document_key="fmt.confirmation_1")),
            self.body_of(
                self.generate(
                    kind="RECEIPT",
                    document_key="fmt.receipt_1",
                    subject={"invoice_id": "inv_1"},
                    generated_by="finance_01",
                    actor_role="finance",
                )
            ),
            self.body_of(
                self.generate(
                    kind="NOTICE",
                    document_key="fmt.notice_1",
                    subject={"case_id": case_id},
                    generated_by="manager_01",
                    actor_role="manager",
                )
            ),
        ]
        for body in bodies:
            self.assertNotIn("<script", body)
            self.assertNotIn("javascript:", body)
            self.assertNotIn("<iframe", body)
            self.assertNotIn("<style", body)
            self.assertNotRegex(body, r"<[^>]+\son[a-z]+\s*=")

    def test_a_script_payload_in_notes_is_escaped(self):
        document = self.generate(
            subject={"booking_id": "book_1", "notes": "<script>alert(1)</script>"}
        )
        body = self.body_of(document)
        self.assertNotIn("<script", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertFalse(contains_direct_identifiers(body))

    def test_a_javascript_url_in_the_title_is_neutralised(self):
        document = self.generate(
            subject={"booking_id": "book_1", "title": "javascript:alert(1)"}
        )
        body = self.body_of(document)
        self.assertNotIn("javascript:", body)
        self.assertIn("alert(1)", body)

    def test_an_onerror_attribute_in_a_value_cannot_become_an_attribute(self):
        document = self.generate(
            subject={
                "booking_id": "book_1",
                "notes": '" onerror="alert(1)" x="',
            }
        )
        body = self.body_of(document)
        self.assertNotRegex(body, r"<[^>]+\son[a-z]+\s*=")
        self.assertIn("&quot;", body)

    def test_the_document_is_well_formed_html(self):
        body = self.body_of(self.generate())
        self.assertTrue(body.startswith("<!DOCTYPE html>\n"))
        self.assertTrue(body.endswith("</html>\n"))
        self.assertEqual(body.count("<html"), 1)
        self.assertEqual(body.count("<article"), 1)
        self.assertIn('data-kind="CONFIRMATION"', body)
        self.assertIn('lang="id-ID"', body)

    def test_the_locale_changes_the_language_and_the_number_format(self):
        indonesian = self.body_of(self.generate(document_key="loc.id_1"))
        english = self.body_of(
            self.generate(document_key="loc.en_1", locale="en-US")
        )
        self.assertIn("01 Okt 2026", indonesian)
        self.assertIn("IDR 1.500.000", indonesian)
        self.assertIn("01 Oct 2026", english)
        self.assertIn("IDR 1,500,000", english)
        self.assertIn('lang="en-US"', english)

    def test_an_unsupported_locale_is_refused(self):
        before = self.trace()
        self.refuse(DOC.UNSUPPORTED_LOCALE, self.generate, locale="fr-FR")
        self.assert_no_trace(before)

    def test_the_text_export_is_plain_and_keeps_the_facts(self):
        row = self.invoice_row()
        document = self.generate(
            kind="RECEIPT",
            document_key="txt.receipt_1",
            subject={"invoice_id": "inv_1"},
            generated_by="finance_01",
            actor_role="finance",
        )
        text = self.svc.text(document.document_id)
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        self.assertIn(row["invoice_number"], text)
        self.assertIn(DOC.format_amount(P.Money(int(row["total_minor"]), "IDR")), text)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text, DOC.plain_text(self.body_of(document)))

    def test_the_text_export_carries_no_script_or_identifier(self):
        document = self.generate(
            subject={
                "booking_id": "book_1",
                "notes": "<script>alert(1)</script> javascript:x",
                "contact_email": EMAIL,
            }
        )
        text = self.svc.text(document.document_id)
        self.assertNotIn("<script", text)
        self.assertNotIn("javascript:", text)
        self.assertNotIn(EMAIL, text)
        self.assertFalse(contains_direct_identifiers(text))

    def test_the_text_export_needs_a_storage_ref(self):
        document = self.generate()
        self.conn.execute(
            "UPDATE documents SET status = 'SUPERSEDED', storage_ref = NULL WHERE document_id = ?",
            (document.document_id,),
        )
        self.refuse(DOC.DOCUMENT_STORAGE_INVALID, self.svc.text, document.document_id)

    def test_the_text_export_reports_a_missing_file(self):
        document = self.generate()
        (self.root / document.storage_ref).unlink()
        self.refuse(DOC.DOCUMENT_STORAGE_INVALID, self.svc.text, document.document_id)


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #
class TestVerify(DocumentTestBase):
    def test_a_fresh_document_verifies(self):
        document = self.generate()
        report = self.svc.verify(document.document_id)
        self.assertTrue(report.ok)
        self.assertEqual(report.issues, ())
        self.assertEqual(report.expected_sha256, document.body_sha256)
        self.assertEqual(report.actual_sha256, document.body_sha256)
        self.assertEqual(Path(report.path), self.root / document.storage_ref)
        self.assertEqual(report.status, "GENERATED")
        self.assertEqual(report.content_class, "P1")
        self.assertEqual(report.version, 1)

    def test_a_missing_file_is_reported(self):
        document = self.generate()
        (self.root / document.storage_ref).unlink()
        report = self.svc.verify(document.document_id)
        self.assertFalse(report.ok)
        self.assertEqual(report.issues, (DOC.ISSUE_FILE_MISSING,))
        self.assertIsNone(report.actual_sha256)

    def test_a_changed_file_is_reported(self):
        document = self.generate()
        path = self.root / document.storage_ref
        path.write_text(path.read_text() + "tampered\n", encoding="utf-8")
        report = self.svc.verify(document.document_id)
        self.assertFalse(report.ok)
        self.assertEqual(report.issues, (DOC.ISSUE_HASH_MISMATCH,))
        self.assertNotEqual(report.actual_sha256, report.expected_sha256)

    def test_an_unknown_document_is_refused(self):
        self.refuse(DOC.DOCUMENT_NOT_FOUND, self.svc.verify, "doc_missing_1")

    def test_a_superseded_version_is_not_verified_as_active(self):
        first = self.generate()
        self.generate()
        exc = self.refuse(DOC.DOCUMENT_NOT_ACTIVE, self.svc.verify, first.document_id)
        self.assertEqual(exc.details["status"], "SUPERSEDED")

    def test_a_void_version_is_not_verified_as_active(self):
        document = self.generate()
        self.svc.void(
            document.document_id, reason="salah alamat", actor="manager_01", actor_role="manager"
        )
        self.refuse(DOC.DOCUMENT_NOT_ACTIVE, self.svc.verify, document.document_id)

    def test_an_inactive_version_can_be_audited_on_request(self):
        first = self.generate()
        self.generate()
        report = self.svc.verify(first.document_id, active_only=False)
        self.assertTrue(report.ok)
        self.assertEqual(report.status, "SUPERSEDED")

    def test_a_deleted_superseded_file_is_still_reported(self):
        first = self.generate()
        self.generate()
        (self.root / first.storage_ref).unlink()
        report = self.svc.verify(first.document_id, active_only=False)
        self.assertFalse(report.ok)
        self.assertEqual(report.issues, (DOC.ISSUE_FILE_MISSING,))

    def test_a_tampered_storage_ref_is_reported_not_followed(self):
        document = self.generate()
        self.conn.execute(
            "UPDATE documents SET storage_ref = '../../etc/passwd' WHERE document_id = ?",
            (document.document_id,),
        )
        report = self.svc.verify(document.document_id)
        self.assertFalse(report.ok)
        self.assertEqual(report.issues, (DOC.ISSUE_UNSAFE_STORAGE_REF,))
        self.assertIsNone(report.path)

    def test_a_missing_storage_ref_is_reported(self):
        document = self.generate()
        self.conn.execute(
            "UPDATE documents SET status = 'SUPERSEDED', storage_ref = NULL WHERE document_id = ?",
            (document.document_id,),
        )
        report = self.svc.verify(document.document_id, active_only=False)
        self.assertFalse(report.ok)
        self.assertEqual(report.issues, (DOC.ISSUE_MISSING_STORAGE_REF,))

    def test_the_report_serialises(self):
        document = self.generate()
        payload = self.svc.verify(document.document_id).as_dict()
        self.assertEqual(payload["document_id"], document.document_id)
        self.assertEqual(payload["issues"], [])
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["expected_sha256"], document.body_sha256)

    def test_verify_uses_the_root_it_was_given(self):
        document = self.generate()
        other_root = self.tmp / "elsewhere"
        other = DOC.DocumentService(self.conn, clock=self.clock, ids=self.ids, root=other_root)
        report = other.verify(document.document_id)
        self.assertFalse(report.ok)
        self.assertEqual(report.issues, (DOC.ISSUE_FILE_MISSING,))


# --------------------------------------------------------------------------- #
# Listing, latest, status
# --------------------------------------------------------------------------- #
class TestListing(DocumentTestBase):
    def three_documents(self) -> None:
        self.generate(document_key="list.a_1")
        self.generate(
            kind="RECEIPT",
            document_key="list.b_1",
            subject={"invoice_id": "inv_1"},
            generated_by="finance_01",
            actor_role="finance",
        )
        self.generate(document_key="list.a_1")  # version 2 of the first

    def test_listing_returns_everything_ordered(self):
        self.three_documents()
        listed = self.svc.list_documents()
        self.assertEqual(len(listed), 3)
        self.assertEqual(
            [(document.document_key, document.version) for document in listed],
            [("list.a_1", 1), ("list.a_1", 2), ("list.b_1", 1)],
        )

    def test_filtering_by_kind(self):
        self.three_documents()
        receipts = self.svc.list_documents(kind="RECEIPT")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].kind, "RECEIPT")
        self.assertEqual(len(self.svc.list_documents(kind="CONFIRMATION")), 2)

    def test_filtering_by_status(self):
        self.three_documents()
        active = self.svc.list_documents(status="GENERATED")
        superseded = self.svc.list_documents(status="SUPERSEDED")
        self.assertEqual([document.version for document in active], [2, 1])
        self.assertEqual([document.version for document in superseded], [1])

    def test_filtering_by_a_subject_ref(self):
        self.three_documents()
        by_booking = self.svc.list_documents(subject_ref="book_1")
        self.assertEqual(len(by_booking), 3)
        by_invoice = self.svc.list_documents(subject_ref="inv_1")
        self.assertEqual(len(by_invoice), 1)
        self.assertEqual(by_invoice[0].invoice_id, "inv_1")
        self.assertEqual(self.svc.list_documents(subject_ref="book_2"), ())

    def test_filtering_by_a_case_ref(self):
        case_id = self.open_case()
        self.generate(
            kind="NOTICE",
            document_key="list.case_1",
            subject={"case_id": case_id},
            generated_by="manager_01",
            actor_role="manager",
        )
        listed = self.svc.list_documents(subject_ref=case_id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].case_id, case_id)

    def test_filters_combine(self):
        self.three_documents()
        listed = self.svc.list_documents(kind="CONFIRMATION", status="GENERATED")
        self.assertEqual([document.version for document in listed], [2])

    def test_an_unknown_filter_is_refused(self):
        self.refuse(DOC.UNKNOWN_DOCUMENT_KIND, self.svc.list_documents, kind="LETTER")
        self.refuse(DOC.UNKNOWN_DOCUMENT_STATUS, self.svc.list_documents, status="DRAFT")
        # A malformed ref is the shared helper's call to refuse (INVALID_FIELD).
        self.refuse("INVALID_FIELD", self.svc.list_documents, subject_ref="bad ref")

    def test_a_document_row_serialises_and_exposes_its_subject(self):
        document = self.generate()
        payload = document.as_dict()
        self.assertEqual(payload["document_id"], document.document_id)
        self.assertEqual(set(payload), set(DOC.DOCUMENT_COLUMNS))
        self.assertEqual(document.subject_ref, "book_1")


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
class TestRoles(DocumentTestBase):
    def test_a_finance_role_is_needed_for_an_invoice(self):
        before = self.trace()
        self.refuse(
            DOC.DOCUMENT_ROLE_FORBIDDEN,
            self.generate,
            kind="INVOICE",
            document_key="role.invoice_1",
            subject={"invoice_id": "inv_1"},
            generated_by="agent_01",
            actor_role="agent",
        )
        self.assert_no_trace(before)
        document = self.generate(
            kind="INVOICE",
            document_key="role.invoice_1",
            subject={"invoice_id": "inv_1"},
            generated_by="finance_01",
            actor_role="finance",
        )
        self.assertEqual(document.status, "GENERATED")

    def test_an_agent_may_confirm_but_not_offer(self):
        self.assertEqual(self.generate().status, "GENERATED")
        self.refuse(
            DOC.DOCUMENT_ROLE_FORBIDDEN,
            self.generate,
            kind="CONTRACT",
            document_key="role.offer_1",
            subject={"booking_id": "book_1"},
            generated_by="agent_01",
            actor_role="agent",
        )

    def test_an_unknown_role_is_refused_for_every_kind(self):
        before = self.trace()
        for index, kind in enumerate(DOC.DOCUMENT_KINDS):
            self.refuse(
                DOC.DOCUMENT_ROLE_FORBIDDEN,
                self.generate,
                kind=kind,
                document_key=f"role.none_{index}",
                subject={"booking_id": "book_1"},
                generated_by="guest_1",
                actor_role="guest",
            )
        self.assert_no_trace(before)

    def test_a_blank_actor_is_refused(self):
        before = self.trace()
        self.refuse("MISSING_FIELD", self.generate, generated_by="")
        self.refuse("MISSING_FIELD", self.generate, actor_role="")
        self.assert_no_trace(before)

    def test_the_actor_is_stored_and_rendered(self):
        document = self.generate(generated_by="agent_07")
        self.assertEqual(document.generated_by, "agent_07")
        self.assertIn("agent_07", self.body_of(document))


# --------------------------------------------------------------------------- #
# Voiding
# --------------------------------------------------------------------------- #
class TestVoid(DocumentTestBase):
    def void(self, document, **overrides):
        kwargs = {
            "reason": "dokumen salah",
            "actor": "manager_01",
            "actor_role": "manager",
        }
        kwargs.update(overrides)
        return self.svc.void(document.document_id, **kwargs)

    def test_voiding_retires_a_generated_version_and_keeps_the_file(self):
        document = self.generate()
        voided = self.void(document)
        self.assertEqual(voided.status, "VOID")
        self.assertEqual(voided.storage_ref, document.storage_ref)
        self.assertTrue((self.root / document.storage_ref).exists())
        self.assertEqual(self.svc.list_documents(status="VOID"), (voided,))
        self.assertEqual(self.svc.latest("doc.default_1").status, "VOID")

    def test_voiding_a_superseded_version_is_refused(self):
        first = self.generate()
        self.generate()
        self.refuse(DOC.DOCUMENT_NOT_ACTIVE, self.void, first)

    def test_voiding_twice_is_refused(self):
        document = self.generate()
        self.void(document)
        self.refuse(DOC.DOCUMENT_NOT_ACTIVE, self.void, document)

    def test_voiding_needs_a_reason(self):
        document = self.generate()
        self.refuse(DOC.INVALID_DOCUMENT_REQUEST, self.void, document, reason="ok")
        self.refuse("MISSING_FIELD", self.void, document, reason="")

    def test_voiding_needs_a_manager_or_admin(self):
        document = self.generate()
        self.refuse(DOC.DOCUMENT_ROLE_FORBIDDEN, self.void, document, actor_role="agent")
        self.refuse(DOC.DOCUMENT_ROLE_FORBIDDEN, self.void, document, actor_role="finance")

    def test_voiding_an_unknown_document_is_refused(self):
        self.refuse(DOC.DOCUMENT_NOT_FOUND, self.svc.void, "doc_missing_1", reason="apa",
                    actor="manager_01", actor_role="manager")

    def test_a_voided_document_cannot_be_resurrected_by_a_new_version_of_the_key(self):
        document = self.generate()
        self.void(document)
        newer = self.generate()
        self.assertEqual(newer.version, 2)
        self.assertEqual(self.svc.get(document.document_id).status, "VOID")


# --------------------------------------------------------------------------- #
# Source hygiene
# --------------------------------------------------------------------------- #
class TestSourceHygiene(unittest.TestCase):
    """The module may not reach for wall-clock time, randomness, or a template
    engine — determinism is the point, and the repo is stdlib-only."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (REPO_ROOT / "src" / "documents.py").read_text(encoding="utf-8")

    def test_the_module_uses_stdlib_and_siblings_only(self):
        allowed = set(sys.stdlib_module_names) | {"primitives", "gates", "invoicing"}
        imported = set()
        for line in self.source.splitlines():
            match = re.match(r"(?:from|import)\s+([A-Za-z_][\w.]*)", line.strip())
            if match:
                imported.add(match.group(1).split(".")[0])
        self.assertTrue(imported)
        self.assertLessEqual(imported, allowed, f"unexpected import(s): {imported - allowed}")

    def test_the_module_does_not_reach_for_wall_clock_time_or_randomness(self):
        for banned in (
            "datetime.now",
            "utcnow",
            "time.time",
            "import random",
            "import requests",
            "subprocess",
            "float(",
            "eval(",
            "exec(",
            "jinja",
            "reportlab",
            "weasyprint",
            "sqlite3.connect",
        ):
            self.assertNotIn(banned, self.source, f"{banned} is banned in documents.py")

    def test_the_module_declares_its_public_surface(self):
        for name in (
            "DocumentService",
            "Document",
            "VerificationReport",
            "plain_text",
            "resolve_storage_path",
            "storage_ref_for",
            "assert_identifier_free",
            "format_amount",
            "format_date",
            "default_root",
            "DOCUMENT_KINDS",
            "DOCUMENT_COLUMNS",
            "DOCUMENT_ROLE_POLICY",
            "DOCUMENT_CONTENT_CLASS",
            "DOCUMENT_IDENTIFIER_LEAK",
            "DOCUMENT_NOT_ACTIVE",
            "DOCUMENT_SOURCE_MISMATCH",
            "INVOICE_SOURCE_MISMATCH",
            "UNSAFE_DOCUMENT_KEY",
        ):
            self.assertTrue(hasattr(DOC, name), f"{name} is missing from documents.py")
            self.assertIn(name, DOC.__all__, f"{name} is not exported")
        for method in ("generate", "verify", "void", "list_documents", "latest", "text"):
            self.assertTrue(
                callable(getattr(DOC.DocumentService, method, None)),
                f"DocumentService.{method} is missing",
            )

    def test_no_blanket_except(self):
        self.assertNotIn("except:", self.source)
        self.assertNotIn("except Exception:\n            pass", self.source)


if __name__ == "__main__":
    unittest.main()
