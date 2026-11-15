"""Sakelar tema diuji dari sumbernya, bukan diklaim.

Kenapa test ini ada
-------------------
Dashboard punya dua wajah — basalt gelap dan kertas terang — dan sakelarnya
dipasang tanpa JavaScript: tiga tautan fragmen (``#tema-terang``,
``#tema-gelap``, ``#tema-auto``) yang hanya mengubah ``color-scheme``.

Tiga hal mudah rusak tanpa suara, dan semuanya ditangkap di sini:

1. **Palet paksa berbeda dari palet sistem.** Blok ``html:has(#tema-gelap:target)``
   menyalin token palet gelap. Kalau suatu hari token di ``:root`` digelapkan
   dan salinannya tidak, tombol "Gelap" akan berbohong — persis jenis drift
   yang tidak terlihat di screenshot karena tampak "hampir sama".
2. **Sakelar berubah menjadi permintaan jaringan.** Kalau seseorang mengganti
   ``href="#tema-gelap"`` menjadi ``?theme=dark``, klik akan mengirim GET tanpa
   header token dan menghasilkan 401. Test menjaga sakelar tetap murni fragmen.
3. **Kembalinya animasi yang membuat tile "belum jadi".** Sempat terjadi: tile
   KPI dianimasikan bertahap, sehingga tangkapan layar (dan mata pengguna di
   perangkat lambat) melihat angka setengah transparan yang tampak seperti bug
   tata letak. Tile sekarang harus selesai dicat seketika.

Test ini membaca ``templates/base.html`` sebagai sumber kebenaran dan tidak
membutuhkan server maupun browser.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "templates" / "base.html"
CSS = BASE.read_text(encoding="utf-8")

#: Variabel yang *harus* berbeda antara dua palet. Kalau daftar ini bertambah,
#: tambahkan juga di kedua blok paksa — itu inti test ini.
PALETTE_VARS = (
    "--basalt",
    "--basalt-2",
    "--surface",
    "--surface-2",
    "--surface-3",
    "--line",
    "--line-strong",
    "--ink",
    "--ink-soft",
    "--muted",
    "--jade",
    "--gold",
    "--ok",
    "--warn",
    "--bad",
    "--acc-s",
    "--acc-l",
    "--header-bg",
    "--card-bg",
    "--table-bg",
    "--th-bg",
    "--chip-bg",
    "--btn-ink",
    "--tone-ok",
    "--tone-warn",
    "--tone-bad",
    "--tile-s",
    "--cell-fg",
)


def _brace_block(text: str, opener_index: int) -> str:
    """Ambil blok CSS mulai dari ``{`` pertama setelah ``opener_index``."""
    start = text.index("{", opener_index)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError("blok CSS tidak tertutup")


def _declarations(block: str) -> dict[str, str]:
    found = re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block, flags=re.I)
    return {name.lower(): re.sub(r"\s+", " ", value).strip() for name, value in found}


def _root_block() -> str:
    return _brace_block(CSS, CSS.index(":root {"))


def _light_block() -> str:
    media = CSS.index("@media (prefers-color-scheme: light)")
    return _brace_block(CSS, CSS.index(":root {", media))


def _forced_dark_block() -> str:
    return _brace_block(CSS, CSS.index("html:has(#tema-gelap:target)"))


def _forced_light_block() -> str:
    return _brace_block(CSS, CSS.index("html:has(#tema-terang:target)"))


@pytest.mark.parametrize(
    "scheme, reference, forced",
    [
        ("gelap", _root_block, _forced_dark_block),
        ("terang", _light_block, _forced_light_block),
    ],
)
def test_the_forced_palette_is_identical_to_the_system_palette(scheme, reference, forced):
    """Tombol ``#tema-<scheme>`` harus menghasilkan warna yang sama persis
    dengan mode sistem yang setara — kalau tidak, ia berbohong soal dirinya."""
    ref = _declarations(reference())
    got = _declarations(forced())

    missing = [name for name in PALETTE_VARS if name not in ref]
    assert missing == [], f"palet acuan tidak mendeklarasikan: {missing}"
    missing = [name for name in PALETTE_VARS if name not in got]
    assert missing == [], f"blok paksa '{scheme}' tidak mendeklarasikan: {missing}"

    drift = {name: (ref[name], got[name]) for name in PALETTE_VARS if ref[name] != got[name]}
    assert drift == {}, f"palet paksa '{scheme}' menyimpang dari sistem: {drift}"


def test_both_schemes_are_declared_to_the_browser():
    """``color-scheme`` menentukan kontrol bawaan (scrollbar, select) dan juga
    dipakai ``prefers-color-scheme``; tanpa deklarasi, mode terang memakai
    kontrol gelap bawaan."""
    assert "color-scheme: dark" in _declarations(_root_block()).get("color-scheme", "") or (
        "color-scheme: dark" in _root_block()
    )
    assert "color-scheme: light" in _light_block()
    assert 'name="color-scheme"' in CSS, "meta color-scheme hilang"


def test_the_switch_marks_exactly_one_state():
    """Tiga tautan, tiga target. ``:has(:target)`` hanya bekerja bila id-nya ada."""
    hrefs = re.findall(r'class="switcher".*?</div>', CSS, flags=re.S)
    assert len(hrefs) == 1, "blok .switcher tidak ditemukan"
    links = re.findall(r'href="(#[^"]+)"\s+data-theme-set="([a-z]+)"', hrefs[0])
    assert sorted(theme for _, theme in links) == ["auto", "dark", "light"]

    sinks = re.findall(r'<span id="(tema-[a-z]+)" class="themesink"', CSS)
    assert sorted(sinks) == ["tema-auto", "tema-gelap", "tema-terang"]
    for href, theme in links:
        assert href == f"#tema-{'gelap' if theme == 'dark' else 'terang' if theme == 'light' else 'auto'}"


def test_the_switch_never_sends_a_request():
    """Fragmen = nol permintaan. Query string = 401, karena tautan browser tidak
    membawa header token dan dashboard hanya menerima header."""
    assert "?theme=" not in CSS, "sakelar tema berubah menjadi permintaan jaringan"
    switch_region = re.search(r'class="switcher".*?</div>', CSS, flags=re.S)
    assert switch_region is not None
    assert "href=\"?" not in switch_region.group(0)


def test_the_dashboard_still_ships_without_javascript():
    """Sakelar berbasis fragmen hanya masuk akal bila tidak ada script sama sekali."""
    for path in sorted((REPO / "templates").glob("*.html")):
        html = path.read_text(encoding="utf-8")
        assert "<script" not in html.lower(), f"{path.name} menyertakan <script>"
        assert " onclick=" not in html.lower(), f"{path.name} memakai handler inline"


def test_kpi_tiles_paint_settled():
    """Regresi: tile KPI sempat dianimasikan bertahap sehingga tampak terpotong.

    Tangkapan layar menangkap opacity pertengahan dan mata membacanya sebagai
    "tata letak rusak". Tile tidak boleh lagi berpindah atau menunda diri.
    """
    assert "animation-delay" not in CSS, "tile KPI kembali memakai stagger"
    keyframes = re.search(r"@keyframes rise \{[^}]*\}[^}]*\}", CSS, flags=re.S)
    assert keyframes is not None, "@keyframes rise hilang"
    assert "translate" not in keyframes.group(0), "tile KPI kembali bergeser saat masuk"


def test_every_page_gets_its_own_hue():
    """Satu halaman, satu warna. Halaman yang lupa didaftarkan akan diam-diam
    memakai hue bawaan dan kehilangan identitasnya."""
    from src.dashboard import NAV  # noqa: PLC0415 — impor lokal agar test ringan

    hues = dict(re.findall(r'body:has\(\[data-page="([a-z_]+)"\]\)\s*\{\s*--h:\s*(\d+);', CSS))
    for page, _route in NAV:
        assert page in hues, f"halaman '{page}' belum punya --h"
    for extra in ("case_detail", "error"):
        assert extra in hues, f"halaman '{extra}' belum punya --h"
    values = {int(v) for v in hues.values()}
    assert all(0 <= v <= 360 for v in values), f"hue di luar rentang: {sorted(values)}"
