"""Kontrol untuk gerbang PII: pemindai harus BISA gagal.

Sebuah pemindai yang selalu PASS tidak membuktikan apa pun. Berkas ini menanam
nilai sintetis ke direktori sementara dan menuntut pemindai menemukannya
(kontrol positif), lalu menuntut pemindai tetap diam pada bentuk yang sah
(kontrol negatif): placeholder terdokumentasi, koordinat SVG yang kebetulan
lolos Luhn, dan potongan hex di dalam digest.

Nilai yang ditanam di sini sintetis dan tidak boleh berupa data nyata siapa pun.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("pii_scan", ROOT / "scripts" / "pii_scan.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pii_scan = _load()


def _scan_tree(tmp_path: Path) -> dict[str, list[str]]:
    findings, _ = pii_scan.scan(tmp_path)
    findings.pop("_scanned", None)
    return {k: v for k, v in findings.items() if v}


class TestPositiveControls:
    def test_unmasked_email_is_found(self, tmp_path):
        (tmp_path / "guest.md").write_text("kontak: rahasia.warga@gmail.com\n", encoding="utf-8")
        found = _scan_tree(tmp_path)
        assert "e-mail" in found
        assert "guest.md:1" in found["e-mail"][0]

    def test_unmasked_phone_is_found(self, tmp_path):
        (tmp_path / "guest.md").write_text("wa: +62 857 1234 5678\n", encoding="utf-8")
        found = _scan_tree(tmp_path)
        assert "phone" in found

    def test_identifier_number_is_found(self, tmp_path):
        (tmp_path / "guest.md").write_text("nik 3201234567890123\n", encoding="utf-8")
        found = _scan_tree(tmp_path)
        assert "16-digit id" in found

    def test_luhn_valid_card_is_found(self, tmp_path):
        (tmp_path / "guest.md").write_text("kartu 4012 8888 8888 1881\n", encoding="utf-8")
        found = _scan_tree(tmp_path)
        assert "card number" in found

    def test_cli_exit_code_is_one_on_findings(self, tmp_path, capsys):
        (tmp_path / "guest.md").write_text("wa: +62 857 1234 5678\n", encoding="utf-8")
        assert pii_scan.main(["pii_scan.py", str(tmp_path)]) == 1
        assert "PII-SCAN: FAIL" in capsys.readouterr().out


class TestNegativeControls:
    def test_documented_placeholders_are_not_findings(self, tmp_path):
        (tmp_path / "doc.md").write_text(
            "wa: +62 812 3456 7890\n"
            "dua tenant: +628111111111 dan +628222222222\n"
            "kartu uji: 4111 1111 1111 1111 / 4242 4242 4242 4242\n"
            "nik contoh: 3273010101900001\n",
            encoding="utf-8",
        )
        assert _scan_tree(tmp_path) == {}

    def test_reserved_email_domains_are_not_findings(self, tmp_path):
        (tmp_path / "doc.md").write_text(
            "a@example.com b@example.org c@villa.example d@localhost\n", encoding="utf-8"
        )
        assert _scan_tree(tmp_path) == {}

    def test_svg_path_coordinates_are_not_card_numbers(self, tmp_path):
        # Bentuk ini pernah menghasilkan false positive "card number" di review.
        (tmp_path / "poster.html").write_text(
            '<path d="M0 365 C160 290 270 390 430 310 C560 245 650 330 800 260 V500 H0Z"/>\n',
            encoding="utf-8",
        )
        assert "card number" not in _scan_tree(tmp_path)

    def test_twelve_digit_run_is_not_an_identifier(self, tmp_path):
        (tmp_path / "doc.md").write_text("order 123456789012 diproses\n", encoding="utf-8")
        assert "16-digit id" not in _scan_tree(tmp_path)

    def test_digit_run_inside_a_digest_is_not_a_phone(self, tmp_path):
        (tmp_path / "doc.md").write_text(
            "digest fe372293ac6fc8767d248278e9ceacbb0813933935d1332\n", encoding="utf-8"
        )
        assert "phone" not in _scan_tree(tmp_path)


class TestRepositoryItself:
    def test_this_repository_is_clean(self):
        """Gerbang yang tidak pernah dijalankan pada repo sendiri bukan gerbang."""
        findings, scanned = pii_scan.scan(ROOT)
        findings.pop("_scanned", None)
        assert scanned > 100, "pemindai tidak membaca berkas apa pun — gerbang hampa"
        assert {k: v for k, v in findings.items() if v} == {}

    def test_binary_files_are_skipped_and_that_is_documented(self):
        # PDF/gambar tidak dipindai; batas ini harus tetap dinyatakan di docstring.
        doc = (ROOT / "scripts" / "pii_scan.py").read_text(encoding="utf-8")
        assert "binaries are skipped" in doc
