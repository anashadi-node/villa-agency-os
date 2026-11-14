"""Kontras warna dashboard diuji, bukan diklaim.

Kenapa test ini ada
-------------------
Tampilan "premium" gampang rusak tanpa disadari: satu token warna digeser
sedikit, chip nav jadi tidak terbaca di mode terang, dan tidak ada test yang
menangkapnya. Test ini membaca token langsung dari ``templates/base.html``
(sumber kebenaran), menghitung rasio kontras WCAG 2.1, lalu menolak build bila
ada pasangan teks/latar di bawah ambang AA.

Ambang yang dipakai
-------------------
* 4.5:1 — teks normal (WCAG 2.1 SC 1.4.3, level AA).
* 3.0:1 — teks besar (>= 18.66px bold / 24px) dan batas komponen antarmuka
  (SC 1.4.11). Kami hanya memakai 4.5:1 untuk keamanan, kecuali pasangan yang
  memang dekoratif dan diberi label di bawah.

Kalau test gagal, jangan naikkan ambangnya — gelapkan/terangkan tokennya.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "templates" / "base.html"

AA_NORMAL = 4.5
AA_LARGE = 3.0


def _relative_luminance(hex_colour: str) -> float:
    """Luminans relatif menurut WCAG 2.1 (gamma 2.4, koefisien 0.2126/0.7152/0.0722)."""
    value = hex_colour.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        raise ValueError(f"bukan warna hex 6 digit: {hex_colour!r}")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    """Rasio kontras WCAG antara dua warna (selalu >= 1.0)."""
    a, b = _relative_luminance(foreground), _relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    value = hex_colour.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _lab(hex_colour: str) -> tuple[float, float, float]:
    """Konversi sRGB -> CIE L*a*b* (D65), jalur standar: linear -> XYZ -> Lab."""
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in _rgb(hex_colour))
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    # putih acuan D65
    xn, yn, zn = 0.95047, 1.00000, 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x / xn), f(y / yn), f(z / zn)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e_cie76(a: str, b: str) -> float:
    """Jarak warna perseptual CIE76: akar dari selisih L*, a*, b*.

    Dipakai untuk menguji *keterbedaan* status (ok/warn/bad). Rasio kontras
    tidak bisa dipakai di sini: dua warna terang berbeda hue punya rasio
    luminans ~1.0 walau mata jelas membedakannya.
    """
    la, aa, ba = _lab(a)
    lb, ab, bb = _lab(b)
    return ((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2) ** 0.5


def _block(css: str, start: int) -> str:
    """Ambil isi blok ``{ ... }`` mulai dari indeks ``start`` (kurung seimbang)."""
    open_at = css.index("{", start)
    depth = 0
    for i in range(open_at, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[open_at + 1 : i]
    raise AssertionError("blok CSS tidak seimbang")


def tokens(scheme: str) -> dict[str, str]:
    """Token ``--nama: #hex`` untuk mode gelap atau terang, dibaca dari base.html."""
    css = BASE.read_text(encoding="utf-8")
    if scheme == "dark":
        scope = _block(css, css.index(":root {"))
    elif scheme == "light":
        media = css.index("@media (prefers-color-scheme: light)")
        scope = _block(css, css.index(":root {", media))
    else:  # pragma: no cover - guard
        raise ValueError(scheme)
    found = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", scope))
    assert found, f"tidak ada token warna terbaca untuk mode {scheme}"
    return found


# Pasangan (nama di laporan, token teks, latar). Latar memakai token --basalt
# karena seluruh teks duduk di atas kanvas, bukan di atas kartu.
PAIRS = [
    ("teks utama", "--ink"),
    ("teks sekunder", "--ink-soft"),
    ("teks redup / meta", "--muted"),
    ("tautan & aksen jade", "--jade"),
    ("aksen emas etnik", "--gold"),
    ("status ok", "--ok"),
    ("status warn", "--warn"),
    ("status bad", "--bad"),
]


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_dashboard_text_meets_wcag_aa(scheme: str) -> None:
    """Setiap token teks yang dipakai dashboard lolos WCAG AA (4.5:1)."""
    t = tokens(scheme)
    background = t["--basalt"]
    report = []
    failures = []
    for label, token in PAIRS:
        assert token in t, f"token {token} hilang dari mode {scheme}"
        value = contrast(t[token], background)
        report.append(f"{label:22s} {t[token]} on {background} = {value:5.2f}:1")
        if value < AA_NORMAL:
            failures.append(report[-1])
    assert not failures, (
        f"kontras di bawah AA 4.5:1 pada mode {scheme}:\n  " + "\n  ".join(failures)
        + "\n\nlaporan lengkap:\n  " + "\n  ".join(report)
    )


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_badge_colours_are_distinguishable(scheme: str) -> None:
    """Tiga status (ok/warn/bad) tidak boleh saling tertukar di mata.

    Diukur dengan jarak warna perseptual CIE76 (ΔE) di ruang Lab, bukan rasio
    kontras: ``--ok`` dan ``--warn`` sama-sama terang sehingga rasio luminans
    keduanya cuma 1.03:1 padahal hijaunya jelas berbeda dari kuningnya. Ambang
    ΔE >= 20 dipakai karena di bawah itu dua warna mulai terbaca "sama" pada
    layar buruk; ΔE >= 20 adalah *kontrol positif*: kalau seseorang menyetel
    ketiganya ke nilai yang sama, rasio terhadap latar tetap lolos tetapi badge
    berhenti memberi informasi — test ini yang menangkapnya.
    """
    t = tokens(scheme)
    for left, right in (("--ok", "--warn"), ("--warn", "--bad"), ("--ok", "--bad")):
        delta = delta_e_cie76(t[left], t[right])
        assert delta >= 20.0, (
            f"{left} ({t[left]}) vs {right} ({t[right]}) hanya ΔE={delta:.1f} — "
            "badge sulit dibedakan"
        )


def test_contrast_maths_matches_wcag_reference() -> None:
    """Kontrol: rumus kami cocok dengan angka acuan WCAG (hitam/putih = 21:1)."""
    assert contrast("#000000", "#ffffff") == 21.0
    assert contrast("#ffffff", "#ffffff") == 1.0
    # abu-abu #767676 di atas putih adalah contoh klasik batas AA (4.54:1)
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.02)
