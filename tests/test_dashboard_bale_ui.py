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
    """Kontrol: rumus aksen di test sama dengan di CSS, supaya test tidak mengukur warna lain.

    Aksen kini dirakit dari satu rumus (``--acc-s``/``--acc-l``) supaya rona
    halaman tetap satu sumber dan mode gelap/terang hanya mengganti dua angka.
    Test ini memastikan angka yang dipakai perhitungan kontras di atas memang
    angka yang dipakai CSS — bukan salinan yang kebetulan mirip.
    """
    assert "--acc:      hsl(var(--h) var(--acc-s) var(--acc-l))" in BASE
    assert "--acc-s: 58%; --acc-l: 66%;" in BASE, "skalar aksen mode gelap berubah — ukur ulang kontras"
    assert "--acc-s: 64%; --acc-l: 27%;" in BASE, "skalar aksen mode terang berubah — ukur ulang kontras"


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


# --------------------------------------------------------------------------- #
# Butir T2.7 yang tadinya hanya ada di CSS: gerakan, motif, dan tipografi.
# Tiga hal ini mudah "hilang tanpa suara" — animasi yang lupa dimatikan, motif
# yang berubah jadi berkas eksternal (ditolak CSP), angka yang tidak lagi rata.
# --------------------------------------------------------------------------- #
REDUCE = "@media (prefers-reduced-motion: reduce)"


def _block(css: str, header: str) -> str:
    """Isi satu blok ``@media`` sampai kurung penutupnya, tanpa parser CSS."""
    start = css.index(header)
    depth = 0
    for index in range(css.index("{", start), len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start : index + 1]
    raise AssertionError(f"blok {header} tidak tertutup")


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"aturan {selector} tidak ada di base.html"
    return match.group(1)


def test_motion_stops_when_the_reader_asks_for_no_motion() -> None:
    """Kriteria T2.7: animasi mati saat ``prefers-reduced-motion`` — diuji, bukan diklaim."""
    assert REDUCE in BASE, "blok reduce hilang: gerakan berjalan untuk semua orang"
    block = _block(BASE, REDUCE)
    # Penutup universal: gerakan yang ditambahkan nanti ikut mati tanpa disentuh lagi.
    assert "*, *::before, *::after" in block
    for killed in (
        "animation: none !important",
        "transition: none !important",
        "scroll-behavior: auto !important",
    ):
        assert killed in block, killed
    assert "transform: none" in block, "gerakan hover masih hidup saat reduce"
    # Kontrol: lembar ini memang punya gerakan untuk dimatikan — kalau tidak, ujinya hampa.
    assert re.search(r"@keyframes\s+[a-z-]+", BASE), "tidak ada @keyframes"
    assert re.search(r"(?<!-)\banimation:\s*(?!none)\w", BASE), "tidak ada animasi berjalan"
    assert re.search(r"\btransition:\s*(?!none)[\w.]+", BASE), "tidak ada transisi berjalan"
    # Kontrol negatif: gerakan tidak dimatikan untuk SEMUA orang di layar. Kalau ``animation: none``
    # juga muncul di lembar layar, kriteria ini lulus hanya karena tidak ada gerakan sejak awal.
    # Dikecualikan hanya blok cetak: kertas bukan pembaca yang bergerak, dan ``@media print``
    # memang wajib beku (dibuktikan di bawah, bukan diabaikan).
    print_block = _block(BASE, "@media print")
    assert "animation: none" in print_block, "cetakan boleh beranimasi? seharusnya beku"
    screen = BASE.replace(block, "").replace(print_block, "")
    assert "animation: none" not in screen, "gerakan dimatikan di lembar layar"
    assert "transition: none" not in screen
    assert "scroll-behavior: auto" not in screen


def _declarations(css: str, token: str) -> list[tuple[str, str]]:
    """Setiap deklarasi ``token`` beserta pemilih dan isi lengkapnya (sampai titik koma)."""
    lines = css.splitlines()
    found: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        if not line.strip().startswith(f"{token}:"):
            continue
        body = [line.strip()]
        while not body[-1].endswith(";"):
            index += 1
            body.append(lines[index].strip())
        context = next(
            (
                lines[j].strip()
                for j in range(index - len(body), -1, -1)
                if re.match(r"^\s*(:root|html|body|\[data-|@media)", lines[j])
            ),
            "?",
        )
        found.append((context, " ".join(body)))
    return found


def test_the_motif_is_woven_in_css_with_no_file_behind_it() -> None:
    """Motif etnik-alam (tenun + butir) digambar CSS dan data-URI: nol berkas, nol font web.

    CSP dashboard ``default-src 'none'`` menolak aset jaringan, jadi motif yang berubah menjadi
    berkas ``.png`` atau font web akan lenyap di layar tanpa satu pun uji lain gagal.
    """
    assert "@font-face" not in BASE, "font web = aset eksternal, ditolak CSP"
    assert not re.search(r"url\(\s*['\"]?(?:https?:)?//", BASE), "ada URL jaringan di CSS"
    woven = _declarations(BASE, "--weave")
    # Empat deklarasi = dua mode (gelap/terang) × dua cara memilih mode: ikut mesin
    # (``prefers-color-scheme``) atau sakelar di halaman (``:target``). Kalau satu jalur hilang,
    # separuh pembaca kehilangan motifnya — dan tidak ada uji lain yang akan menyadarinya.
    assert len(woven) == 4, [c for c, _ in woven]
    assert BASE.count("prefers-color-scheme") == 1, "mode ikut mesin hilang"
    for switch in ("html:has(#tema-gelap:target)", "html:has(#tema-terang:target)"):
        assert switch in BASE, f"sakelar tema {switch} hilang"
    for _, body in woven:
        assert body.count("repeating-linear-gradient") >= 2, "tenun butuh dua arah benang"
        assert "45deg" in body and "-45deg" in body
    # Benang terang untuk mode gelap, benang gelap untuk mode terang — masing-masing dua kali.
    threads = [
        match.group(0)
        for _, body in woven
        if (match := re.search(r"rgba\(\s*\d+,\s*\d+,\s*\d+", body))
    ]
    assert len(threads) == 4, threads
    assert threads.count("rgba(160,196,178") == 2, threads
    assert threads.count("rgba(30,60,48") == 2, threads
    assert "data:image/svg+xml" in BASE, "butir harus data-URI, bukan berkas"
    assert BASE.count("--grain-opacity: .30") == 2 and BASE.count("--grain-opacity: .16") == 2, (
        "butir tidak lagi disetel per mode dan per jalur: satu mode akan terlalu ramai"
    )
    # Halaman benar-benar memakai motifnya, bukan hanya menyimpan token.
    assert BASE.count("var(--weave)") >= 4, "token tenun tidak dipakai"
    assert "var(--grain-image)" in BASE, "token butir tidak dipakai"


def test_figures_line_up_because_the_type_is_local_and_tabular() -> None:
    """Tipografi: dua tumpukan lokal (serif judul, mono angka) dan angka tabular di kolom uang."""
    assert "ui-serif" in BASE and "ui-monospace" in BASE, "tumpukan font lokal hilang"
    assert "@font-face" not in BASE, "font web dilarang: tidak ada dijamin ada di mesin mana pun"
    for selector in ("th.num", "td.num", "dd", 'table[data-table="kpi"] td.num'):
        assert "tabular-nums" in _rule(BASE, selector), f"{selector} kehilangan angka tabular"
    assert "text-align: right" in _rule(BASE, "td.num"), "kolom angka tidak rata kanan"
    # Kontrol negatif: tabular hanya untuk angka. Kalau seluruh dokumen memakainya, uji ini tidak
    # membuktikan apa pun tentang kolom uang — ia hanya membuktikan satu baris CSS ada.
    assert "tabular-nums" not in _rule(BASE, "body")
