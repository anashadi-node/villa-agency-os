"""Kontras situs marketing publik diuji, bukan diklaim.

Kenapa test ini ada
-------------------
``templates/marketing/`` punya design system sendiri (``:root`` berisi --sand,
--paper, --teak, --coral, --clay, ...) dan dipakai semua halaman tamu: beranda,
koleksi villa, profil villa, destinasi, halaman pemilik, formulir inkuiri. Situs
inilah yang dilihat calon tamu dan calon pemilik villa, jadi "premium" di sini
berarti terbaca — bukan sekadar cantik di layar pengembang.

``tests/test_ui_contrast.py`` hanya membaca ``templates/base.html`` (dashboard
internal), sehingga seluruh token marketing lolos tanpa diperiksa. Saat diukur
pertama kali, tiga pasangan gagal AA:

* ``.eyebrow`` --coral #c9683f di atas --paper 3.65:1 dan di atas --sand 3.24:1
* ``.card-region`` --clay #a95d38 di atas --sand 4.16:1
* ``.hero-band .eyebrow`` #e7c27a pada kasus terburuk hero (dua lapis glow
  radial di belakang teks) 3.79:1

Token sudah digelapkan/diterangkan sampai lulus dengan margin, bukan ambangnya
yang diturunkan.

Aturan yang dikunci test ini
----------------------------
1. Token dibaca dari berkasnya sendiri; tidak ada palet kedua di dalam test.
2. Setiap pasangan teks/latar yang benar-benar dipakai CSS dinilai.
3. Latar hero dihitung sebagai komposit warna nyata (gradien + dua radial
   overlay), bukan warna rata — kasus terburuk yang membuktikan klaim.
4. Warna aksen harus tetap bisa dibedakan dari --ink dan --leaf (ΔE >= 20),
   supaya "lulus kontras" tidak dicapai dengan mengubah aksen jadi abu-abu.
5. Kontrol positif: fungsi kontras wajib memberi 21:1 untuk hitam/putih dan
   < 4.5:1 untuk pasangan yang memang buruk — kalau tidak, "lulus" itu artefak.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "templates" / "marketing" / "base.html"

AA_NORMAL = 4.5
AA_LARGE = 3.0
MARGIN = 0.05  # pasangan yang dinilai wajib lulus dengan sedikit ruang, bukan pas-pasan


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        raise ValueError(f"bukan warna hex 6 digit: {hex_colour!r}")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _luminance(hex_colour: str) -> float:
    channels = [c / 255 for c in _rgb(hex_colour)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    """Rasio kontras WCAG 2.1 antara dua warna (selalu >= 1.0)."""
    a, b = _luminance(foreground), _luminance(background)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _lab(hex_colour: str) -> tuple[float, float, float]:
    r, g, b = (c / 255 for c in _rgb(hex_colour))
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda v: v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116  # noqa: E731
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(lhs: str, rhs: str) -> float:
    """Jarak warna CIE76 — dipakai untuk aksen, karena rasio kontras buta hue."""
    return sum((a - b) ** 2 for a, b in zip(_lab(lhs), _lab(rhs))) ** 0.5


def _overlay(foreground: str, alpha: float, background: str) -> str:
    """Warna hasil komposit RGBA di atas latar opak (yang dilihat mata)."""
    blended = [
        round(f * alpha + b * (1 - alpha))
        for f, b in zip(_rgb(foreground), _rgb(background))
    ]
    return "#%02x%02x%02x" % tuple(blended)


@pytest.fixture(scope="module")
def css() -> str:
    return BASE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tokens(css: str) -> dict[str, str]:
    root = re.search(r":root\{(.*?)\}", css, re.S)
    assert root is not None, "templates/marketing/base.html tidak punya blok :root"
    found = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{3,8})", root.group(1)))
    assert len(found) >= 10, f"token terlalu sedikit ({len(found)}), blok :root berubah bentuk"
    return found


def _rule(css: str, selector: str) -> str:
    match = re.search(r"(?<![\w.-])" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match is not None, f"rule {selector!r} hilang dari templates/marketing/base.html"
    return match.group(1)


def _resolve(value: str, tokens: dict[str, str]) -> str:
    value = value.strip()
    var = re.fullmatch(r"var\(--([\w-]+)\)", value)
    if var:
        return tokens[var.group(1)]
    assert re.fullmatch(r"#[0-9a-fA-F]{3,8}", value), f"nilai warna tak dikenal: {value!r}"
    return value


def _prop(css: str, selector: str, prop: str, tokens: dict[str, str]) -> str:
    body = _rule(css, selector)
    match = re.search(rf"(?:^|;)\s*{prop}:\s*([^;]+)", body)
    assert match is not None, f"{selector} tidak mendeklarasikan {prop}"
    return _resolve(match.group(1), tokens)


def test_the_contrast_metric_itself_is_sound():
    """Kontrol positif: metrik wajib membedakan pasangan bagus dan buruk."""
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.03)
    assert contrast("#c9683f", "#fffaf1") < AA_NORMAL, "metrik tidak menangkap pasangan gagal"
    assert delta_e("#000000", "#ffffff") > 100


def test_body_and_link_text_clear_aa(css, tokens):
    """Teks utama dan tautan adalah 90% halaman: wajib jauh di atas ambang."""
    paper = tokens["paper"]
    ink = _prop(css, "body", "color", tokens)
    link = _prop(css, "a", "color", tokens)
    assert ink == tokens["ink"]
    assert contrast(ink, paper) >= AA_NORMAL + MARGIN * 10  # 15:1 di tema ini
    assert contrast(link, paper) >= AA_NORMAL + MARGIN
    assert contrast(_prop(css, ".lede", "color", tokens), paper) >= AA_NORMAL
    assert contrast(tokens["muted"], paper) >= AA_NORMAL


def test_eyebrow_label_clears_aa_on_both_page_backgrounds(css, tokens):
    """Label kecil (.eyebrow) muncul di atas kertas maupun pita pasir."""
    colour = _prop(css, ".eyebrow", "color", tokens)
    for name in ("paper", "sand"):
        ratio = contrast(colour, tokens[name])
        assert ratio >= AA_NORMAL + MARGIN, f".eyebrow di atas --{name} hanya {ratio:.2f}:1"


def test_card_region_label_clears_aa_on_both_page_backgrounds(css, tokens):
    colour = _prop(css, ".card-region", "color", tokens)
    for name in ("paper", "sand"):
        ratio = contrast(colour, tokens[name])
        assert ratio >= AA_NORMAL + MARGIN, f".card-region di atas --{name} hanya {ratio:.2f}:1"


def test_accent_tokens_stay_distinguishable_from_each_other(css, tokens):
    """Lulus kontras tidak boleh dicapai dengan menjadikan semua aksen sama."""
    ink, leaf = tokens["ink"], tokens["leaf"]
    for token in ("coral", "clay", "gold", "muted"):
        assert delta_e(tokens[token], ink) >= 20, f"--{token} terlalu dekat ke --ink"
    assert delta_e(tokens["coral"], tokens["clay"]) >= 5, "--coral dan --clay nyaris sama"
    assert delta_e(tokens["coral"], leaf) >= 20


def test_table_header_and_buttons_clear_aa(css, tokens):
    """th berlatar pasir, tombol berlatar daun; keduanya teks normal."""
    th_bg = _prop(css, "th", "background", tokens)
    th_fg = _prop(css, "th", "color", tokens)
    assert contrast(th_fg, th_bg) >= AA_NORMAL + MARGIN

    cta_bg = _prop(css, ".cta", "background", tokens)
    cta_fg = _prop(css, ".cta", "color", tokens)
    assert contrast(cta_fg, cta_bg) >= AA_NORMAL + MARGIN

    gold_bg = _prop(css, ".cta.gold", "background", tokens)
    gold_fg = _prop(css, ".cta.gold", "color", tokens)
    assert contrast(gold_fg, gold_bg) >= AA_NORMAL + MARGIN


def test_navigation_links_clear_aa_on_the_forest_header(css, tokens):
    nav_fg = _prop(css, "header.site nav a", "color", tokens)
    assert contrast(nav_fg, tokens["teak"]) >= AA_NORMAL + MARGIN

    wa_bg = _prop(css, "header.site nav a.whatsapp", "background", tokens)
    wa_fg = _prop(css, "header.site nav a.whatsapp", "color", tokens)
    assert contrast(wa_fg, wa_bg) >= AA_NORMAL


def test_hero_text_clears_aa_over_the_composited_hero_background(css, tokens):
    """Hero bukan warna rata: gradien + dua glow radial. Ukur kasus terburuk.

    Glow emas/koral menerangi latar di belakang teks, jadi pasangan yang lulus
    di atas --teak bisa gagal tepat di tengah glow. Karena itu latar hero
    dikomposit persis seperti CSS-nya, lalu dipakai sebagai latar penilaian.
    """
    stops = re.findall(r"linear-gradient\([^)]*?(#[0-9a-fA-F]{6})[^)]*\)", css)
    assert len(stops) >= 2, "gradien hero tidak ditemukan"

    radials = re.findall(
        r"radial-gradient\([^,]+,\s*rgba\((\d+),(\d+),(\d+),([\d.]+)\)", css
    )
    assert len(radials) >= 2, "glow radial hero tidak ditemukan"

    darkest = min(stops, key=_luminance)
    composite = darkest
    for r, g, b, alpha in radials:
        composite = _overlay("#%02x%02x%02x" % (int(r), int(g), int(b)), float(alpha), composite)

    for label, selector, threshold in (
        ("h1", ".hero-band h1", AA_LARGE + MARGIN),
        ("lede", ".hero-band .lede", AA_NORMAL + MARGIN),
        ("eyebrow", ".hero-band .eyebrow", AA_NORMAL + MARGIN),
    ):
        colour = _prop(css, selector, "color", tokens)
        ratio = contrast(colour, composite)
        assert ratio >= threshold, f"hero {label} hanya {ratio:.2f}:1 di atas {composite}"


def test_footer_text_clears_aa_on_the_dark_band(css, tokens):
    dark = _prop(css, "footer.site", "background", tokens)
    legal = _prop(css, "footer.site .legal", "color", tokens)
    assert contrast(legal, dark) >= AA_NORMAL
