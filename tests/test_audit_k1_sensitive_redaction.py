"""Audit 2026-11-13, finding K1: P3/P4 values typed into free text never reach storage.

Before this fix a website inquiry carrying ``card 4111 1111 1111 1111 cvv 123``,
``passport A1234567`` and ``+62 812-3456-7890`` was stored verbatim in
``case_messages.body_redacted``; only the email address was masked. The P4 tier
(Data Classification §1) says card data is *never stored*, and D12 is the owner's
own decision. These tests are the negative control: each one plants a value the
redactor must catch, and one plants a value it must leave alone.
"""

from __future__ import annotations


import pytest

import dashboard
import marketing_site as M
from form_token_support import signed_token
from primitives import IdFactory, mask_sensitive_numbers, redact, to_iso
from schema_loader import connect_sqlite

SENSITIVE = [
    ("card 4111 1111 1111 1111", "4111 1111 1111 1111", "[CARD]"),
    ("kartu 5500-0000-0000-0004", "5500-0000-0000-0004", "[CARD]"),
    ("card 4111111111111111", "4111111111111111", None),  # caught by the digit-run masks
    ("amex 3782 822463 10005", "3782 822463 10005", "[CARD]"),
    ("cvv 123", "123", "[CVV]"),
    ("CVC: 4321", "4321", "[CVV]"),
    ("kode keamanan 987", "987", "[CVV]"),
    ("passport A1234567", "A1234567", "[PASSPORT]"),
    ("Paspor no. B9876543", "B9876543", "[PASSPORT]"),
    ("call me +62 812-3456-7890", "812-3456-7890", "[PHONE]"),
    ("office (0361) 761 234", "761 234", "[PHONE]"),
    ("whatsapp +61 412 345 678", "412 345 678", "[PHONE]"),
    ("NIK 3201010101010001", "3201010101010001", None),
]

HARMLESS = [
    "Dates 20-24 Nov, 6 guests, 3 bedrooms",
    "total IDR 5,775,000 for 4 nights",
    "IDR 5 775 000",
    "arrive 2026-11-20, leave 2026-11-24",
    "invoice INV-202610-00001 at 2026-11-12T23:44:22+00:00",
    "passport please, and a cot for the baby",
    "a" * 3 + "b" * 61,  # 64-char hex digest survives
]


@pytest.mark.parametrize("text, secret, marker", SENSITIVE)
def test_redact_masks_every_sensitive_value(text, secret, marker):
    out = redact(text)
    assert secret not in out
    if marker is not None:
        assert marker in out


@pytest.mark.parametrize("text, secret, marker", SENSITIVE)
def test_dashboard_redaction_masks_the_same_values(text, secret, marker):
    assert secret not in dashboard.redact_pii(text)


@pytest.mark.parametrize("text", HARMLESS)
def test_harmless_numbers_survive(text):
    assert mask_sensitive_numbers(text) == text
    assert redact(text) == text


def test_luhn_invalid_grouped_digits_are_not_called_a_card():
    assert "[CARD]" not in mask_sensitive_numbers("ref 1234 5678 9012 3456")


def test_a_website_inquiry_never_stores_p3_or_p4(clock):
    conn = connect_sqlite(":memory:")
    stamp = to_iso(clock.now())
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at) VALUES"
        " ('prop-1','agency_default','Villa','villa','Ubud','Bali','Asia/Makassar','SELLABLE','ACTIVATED',?,?)",
        (stamp, stamp),
    )
    site = M.MarketingSite(conn, clock=clock, ids=IdFactory(seed="k1"))
    message = (
        "Call me +62 812-3456-7890, passport A1234567, card 4111 1111 1111 1111 cvv 123. "
        "Dates 20-24 Nov, 6 guests."
    )
    site.intake.submit(
        form={
            "name": "Sari Dewi",
            "email": "sari@example.com",
            "message": message,
            "form_token": signed_token(),
            M.HONEYPOT_FIELD: "",
            "property_ref": "",
        },
        remote_addr="203.0.113.50",
        rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
        expected_origin_host=None,
        origin_header=None,
    )
    stored = " ".join(
        str(value)
        for row in conn.execute("SELECT * FROM case_messages").fetchall()
        for value in tuple(row)
    )
    for secret in ("4111 1111 1111 1111", "cvv 123", "A1234567", "812-3456-7890"):
        assert secret not in stored, secret
    assert "Dates 20-24 Nov, 6 guests." in stored  # the useful part of the message survives


def test_stored_pii_scanner_counts_without_printing_values(tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "scan_stored_pii", Path(__file__).resolve().parent.parent / "scripts" / "scan_stored_pii.py"
    )
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)

    db = tmp_path / "scan.db"
    conn = connect_sqlite(db)
    conn.close()
    assert scanner.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "STORED-PII-SCAN: PASS" in out
    assert scanner.main(["--db", str(tmp_path / "missing.db")]) == 2
