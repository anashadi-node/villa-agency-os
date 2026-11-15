"""Tabel lebar harus menggulir dirinya sendiri, bukan menyeret halaman.

Kenapa test ini ada
-------------------
Halaman Keuangan punya tabel 10 kolom dan Operasi 13 kolom. Di layar selebar
390px (ponsel) tabel-tabel itu sebelumnya memperlebar ``document.documentElement``
menjadi 954-1166px: seluruh halaman — header, sidebar, tombol tema — ikut bergeser
ke samping dan teks jadi tidak terbaca tanpa menggeser layar bolak-balik.

Perbaikannya satu aturan CSS: tabel data menjadi wadah gulir miliknya sendiri
(``display: block`` + ``overflow-x: auto``), sedangkan tabel KPI dikecualikan
karena ia grid kartu, bukan tabel data.

Yang dijaga di sini:
1. Aturannya ada dan tetap **scoped** — kalau selektornya melebar menjadi
   ``table``, grid KPI akan rusak diam-diam.
2. Properti yang membuat gulir benar-benar bekerja tidak boleh hilang
   (``display: block`` + ``overflow-x: auto`` + ``max-width: 100%``).

Efeknya diukur terpisah di browser sungguhan (390px dan 1440px); test ini
menjaga sumbernya agar tidak diam-diam kembali ke keadaan lama.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "templates" / "base.html"
CSS = BASE.read_text(encoding="utf-8")

SELECTOR = 'table:not([data-table="kpi"])'


def _block_after(selector: str) -> str:
    """Ambil isi ``{ ... }`` pertama setelah ``selector`` muncul di CSS."""
    at = CSS.index(selector)
    open_at = CSS.index("{", at)
    close_at = CSS.index("}", open_at)
    return CSS[open_at + 1 : close_at]


def test_wide_table_becomes_its_own_scroll_container():
    block = _block_after(SELECTOR)
    assert "display: block" in block
    assert "overflow-x: auto" in block
    assert "max-width: 100%" in block


def test_kpi_grid_is_excluded_from_the_scroll_rule():
    # Tabel KPI memakai tbody{display:grid}; memaksakan display:block di tabelnya
    # akan merusak grid kartu. Selektornya harus tetap :not([data-table="kpi"]).
    assert '[data-table="kpi"]' in SELECTOR
    # Aturan tingkat TABEL untuk KPI tidak boleh ikut menjadi wadah gulir;
    # aturan tingkat sel (td) boleh saja memakai display:block.
    table_level = [
        line
        for line in CSS.splitlines()
        if line.strip().startswith('table[data-table="kpi"] {')
    ]
    assert table_level, "aturan grid KPI hilang — periksa templates/base.html"
    for line in table_level:
        assert "display: block" not in line


def test_scroll_rule_does_not_leak_to_every_table():
    # Tidak boleh ada aturan tanpa kualifikasi yang membuat semua tabel menggulir,
    # karena itu menimpa grid KPI.
    assert "\ntable {\n  display: block" not in CSS
