"""G4: dokumen sprint tidak boleh menyebut angka uji yang tidak lagi benar.

Kenapa ini ada: baris T2.6 menulis "14 uji pada tests/test_workforce_views.py" dan tetap
begitu setelah T2.6b menaikkannya ke 20. Angka dokumen yang basi bukan salah tulis kecil —
ia membuat laporan gate tidak bisa dipercaya. Uji ini mengukur ulang setiap berkas yang
disebut dokumen (satu pemanggilan pytest, jumlah per berkas dibaca dari keluaran) dan
menuntut dua hal:

1. setiap angka "N uji" di dokumen sama dengan jumlah uji yang benar-benar terkumpul;
2. setiap angka total "N passed" menyebut commit tempat ia diukur, dan commit itu ada.

Yang tidak diklaim uji ini: ia tidak menjalankan suite penuh untuk membuktikan totalnya.
Total hanya diaudit atribusinya, bukan kejujurannya.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "SPRINT_WORKFORCE_OPS_2_WEEKS.md"

# "tests/test_x.py (17 uji)" dan "20 uji pada tests/test_x.py" — dua arah yang dipakai dokumen.
CLAIM_PATTERNS = (
    re.compile(r"tests/(test_[A-Za-z0-9_]+\.py)[^0-9\n]{0,60}?(\d+)\s*uji"),
    re.compile(r"(\d+)\s*uji[^0-9\n]{0,60}?tests/(test_[A-Za-z0-9_]+\.py)"),
)
TOTAL = re.compile(r"(\d{3,5})\s+passed")


def claims_in_doc() -> list[tuple[int, str, int]]:
    found: list[tuple[int, str, int]] = []
    for lineno, line in enumerate(DOC.read_text(encoding="utf-8").splitlines(), 1):
        for index, pattern in enumerate(CLAIM_PATTERNS):
            for match in pattern.finditer(line):
                name = match.group(1) if index == 0 else match.group(2)
                count = int(match.group(2) if index == 0 else match.group(1))
                found.append((lineno, name, count))
    return found


@pytest.fixture(scope="module")
def collected() -> dict[str, int]:
    """Jumlah uji per berkas, satu pemanggilan collection untuk semua berkas yang disebut."""
    names = sorted({name for _, name, _ in claims_in_doc()})
    missing = [n for n in names if not (REPO / "tests" / n).exists()]
    assert not missing, f"dokumen menyebut berkas uji yang tidak ada: {missing}"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *[f"tests/{n}" for n in names],
         "--collect-only", "-q", "-p", "no:randomly"],
        cwd=str(REPO), capture_output=True, text=True, timeout=300,
    )
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^tests/(test_[A-Za-z0-9_]+\.py):\s*(\d+)\s*$", line.strip())
        if match:
            counts[match.group(1)] = int(match.group(2))
    assert counts, f"tidak bisa membaca jumlah uji dari collection:\n{result.stdout[-600:]}"
    return counts


def test_every_claimed_test_count_matches_reality(collected: dict[str, int]) -> None:
    found = claims_in_doc()
    assert len(found) >= 8, f"hanya {len(found)} klaim angka ditemukan — pola parser basi?"
    wrong = [
        f"baris {lineno}: dokumen bilang {count} uji pada {name}, nyatanya {collected[name]}"
        for lineno, name, count in found
        if name in collected and count != collected[name]
    ]
    assert not wrong, "angka uji di dokumen tidak sama dengan kenyataan:\n" + "\n".join(wrong)


def test_every_claimed_total_names_the_commit_it_was_measured_at() -> None:
    """Total "N passed" tanpa commit adalah angka yang tidak bisa diperiksa siapa pun."""
    lines = DOC.read_text(encoding="utf-8").splitlines()
    totals = [(n, line) for n, line in enumerate(lines, 1) if TOTAL.search(line)]
    assert totals, "tidak ada klaim total 'N passed' di dokumen — pola berubah?"
    unattributed = [n for n, line in totals if not re.search(r"`[0-9a-f]{7,40}`", line)]
    assert not unattributed, (
        "klaim total tanpa commit pengukuran di baris: "
        f"{unattributed} — tulis 'diukur pada `<sha>`'"
    )
    for lineno, line in totals:
        sha = re.search(r"`([0-9a-f]{7,40})`", line)
        assert sha, lineno
        exists = subprocess.run(
            ["git", "-C", str(REPO), "cat-file", "-e", f"{sha.group(1)}^{{commit}}"],
            capture_output=True,
        )
        assert exists.returncode == 0, f"baris {lineno}: commit {sha.group(1)} tidak ada di riwayat"
