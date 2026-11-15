"""Lapisan "Bale Kendali" (CSS di templates/base.html) diuji, bukan diklaim.

Kenapa test ini ada
-------------------
Lapisan ini murni CSS di atas markup yang sudah ada. Risikonya senyap: nama KPI
berganti di Python tetapi CSS masih menunjuk nama lama (ubin kehilangan warna
status), halaman baru tidak punya rona, atau seseorang menyelipkan ``<script>``
/ aset eksternal yang akan ditolak CSP ``default-src 'none'``. Tidak ada test
visual di sini; yang dikunci adalah KONTRAK antara CSS, templat, dan kode.
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

import pytest

import dashboard as D

from test_ui_contrast import AA_NORMAL, contrast

ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))

# Latar nyata: kanvas dan kartu, mode gelap dan terang (token di base.html).
SURFACES = {"dark": ("#090d0c", "#101917"), "light": ("#f4f1e8", "#ffffff")}


def _hsl_hex(h: int, s: int, l: int) -> str:  # noqa: E741 - notasi HSL baku
    r, g, b = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
    return "#%02x%02x%02x" % tuple(round(x * 255) for x in (r, g, b))


def _hues() -> dict[str, int]:
    """``--h`` per halaman, dibaca dari aturan ``body:has([data-page="x"])``."""
    found = dict(
        (page, int(h))
        for page, h in re.findall(r'body:has\(\[data-page="([a-z_]+)"\]\)[^{]*\{\s*--h:\s*(\d+)', BASE)
    )
    assert found, "tidak ada rona per halaman terbaca dari base.html"
    return found


def _pages_in_templates() -> set[str]:
    pages: set[str] = set()
    for path in TEMPLATES:
        pages.update(re.findall(r'data-page="([a-z_]+)"', path.read_text(encoding="utf-8")))
    return pages - {"error"}


def test_every_page_has_its_own_hue_and_kicker() -> None:
    pages = _pages_in_templates()
    hues = _hues()
    # case_detail berbagi rona dengan "cases" lewat selektor ganda; sisanya wajib punya rona sendiri.
    missing = sorted(p for p in pages if p not in hues and p != "case_detail")
    assert not missing, f"halaman tanpa rona: {missing}"
    kickers = set(re.findall(r'section\[data-page="([a-z_]+)"\]::before\s*\{\s*content:', BASE))
    assert not sorted(pages - kickers), f"halaman tanpa kicker: {sorted(pages - kickers)}"
    # Setiap rona harus berbeda: dua halaman beda tetapi sama warnanya = kembali ke "template seragam".
    own = {p: h for p, h in hues.items() if p != "case_detail"}
    assert len(set(own.values())) == len(own), f"rona kembar: {own}"


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_page_hues_meet_wcag_aa_on_canvas_and_card(scheme: str) -> None:
    """Warna aksen (judul kecil, tautan, tag) lolos 4.5:1 di latar kanvas MAUPUN kartu."""
    failures = []
    for page, hue in _hues().items():
        accent = _hsl_hex(hue, 58, 66) if scheme == "dark" else _hsl_hex(hue, 64, 27)
        for surface in SURFACES[scheme]:
            ratio = contrast(accent, surface)
            if ratio < AA_NORMAL:
                failures.append(f"{page} h={hue} {accent} on {surface} = {ratio:.2f}:1")
    assert not failures, "\n".join(failures)


def test_formula_matches_css() -> None:
    """Kontrol: rumus aksen di test sama dengan di CSS, supaya test tidak mengukur warna lain."""
    assert "--acc:      hsl(var(--h) 58% 66%)" in BASE
    assert "body, nav.nav a { --acc: hsl(var(--h) 64% 27%); }" in BASE


def test_state_tones_meet_aa_on_tiles() -> None:
    """Teks tag KPI (aman/pantau/tindak) terbaca di ubin; kontrol negatif: warna salah gagal."""
    tones = {
        "dark": {"ok": "#5cc98f", "warn": "#e6b95f", "bad": "#f0807a"},
        "light": {"ok": "#1e7a4c", "warn": "#7a5210", "bad": "#a3322d"},
    }
    for scheme, palette in tones.items():
        for name, colour in palette.items():
            assert f"--tone-{name}: {colour}" in BASE, f"token {name} {scheme} berubah — ukur ulang"
            for surface in SURFACES[scheme]:
                assert contrast(colour, surface) >= AA_NORMAL, (scheme, name, colour, surface)
    # kontrol negatif: kuning terang di atas putih memang HARUS gagal, jadi ukurannya hidup
    assert contrast("#e6b95f", "#ffffff") < AA_NORMAL


def test_css_kpi_names_exist_in_python() -> None:
    """Setiap ``data-kpi="x"`` di CSS adalah KPI yang benar-benar dirender dashboard."""
    used = set(re.findall(r'data-kpi="([a-z_]+)"', BASE))
    assert used, "CSS tidak merujuk KPI apa pun"
    unknown = used - set(D.KPI_KEYS)
    assert not unknown, f"CSS menunjuk KPI yang tidak ada di KPI_KEYS: {sorted(unknown)}"


def test_hidden_kpi_is_neutral_not_alarm() -> None:
    """KPI yang disembunyikan untuk peran tertentu tidak boleh berwarna "tindak sekarang"."""
    row = str(D.kpi_row("unmatched_payments", "Payment belum tercocokkan", 5, available=False))
    assert 'data-hidden="1"' in row and 'data-value=""' in row
    for rule in re.findall(r'(table\[data-table="kpi"\] tr:is\([^{]*\))\s*\{', BASE):
        assert ":not([data-hidden])" in rule, f"aturan alarm tanpa pengecualian hidden: {rule[:70]}"


def test_audit_chain_tag_follows_status_not_count() -> None:
    """``data-value`` audit_chain = jumlah peristiwa; status kesehatan ada di ``data-chain``."""
    assert 'tr[data-kpi="audit_chain"]:not([data-chain="OK"])' in BASE
    row = str(
        D.kpi_row("audit_chain", "Peristiwa rantai audit", 0, note="OK", attrs=' data-chain="OK"')
    )
    assert 'data-chain="OK"' in row and 'data-value="0"' in row
    assert "chain intact" not in BASE, "klaim 'intact' berlebihan untuk rantai kosong; pakai 'verified OK'"
    assert "verified OK" in BASE


def test_layer_adds_no_script_or_external_asset() -> None:
    """CSP dashboard = ``default-src 'none'``: tanpa script, font web, gambar, atau @import eksternal."""
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        assert "<script" not in text.lower(), path.name
        assert "@import" not in text, path.name
        assert not re.search(r"url\(\s*['\"]?https?:", text), path.name
        assert not re.search(r"""(?:src|href)=["']https?://""", text), path.name


def test_rail_keeps_identity_markers_and_every_nav_link() -> None:
    """Rail kiri hanya menata ulang: penanda yang dipakai tes lain tetap ada."""
    for marker in ('data-actor="1"', 'data-role="1"', "{{ actor_role_label }}", "{{ nav }}", "{{ read_only_banner }}"):
        assert marker in BASE, marker
    assert BASE.count('class="pk"') == 4
