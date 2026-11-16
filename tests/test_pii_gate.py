"""Gate PII (G4): uji apakah pemindainya benar-benar menangkap, dan menolak memberi PASS.

Gerbang G4 menuntut "PII gate lulus". Uji ini menjawab pertanyaan yang lebih keras:
apakah gate itu **bisa gagal**? Kalau tidak, "lulus" tidak berarti apa-apa.

Nilai sensitif di sini dibangun saat runtime dan tidak pernah ditulis sebagai literal,
supaya berkas uji ini sendiri tidak menjadi temuan bagi gate yang sedang diuji.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCAN = REPO / "scripts" / "pii_scan.py"


# Dibangun saat runtime, bukan ditulis sebagai literal: gate ini memindai pohon repo,
# dan versi literal dari nilai-nilai ini membuat gate memblokir berkasnya sendiri.
PLANTED_EMAIL = "rahasia.pribadi" + "@" + "gmail.com"
PLANTED_PHONE = "0812" + " " + "9876" + " " + "5432"


def run_gate(root: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCAN), str(root)],
        cwd=str(REPO), capture_output=True, text=True, timeout=180,
    )


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for char in reversed(digits):
        value = int(char)
        if alt:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        alt = not alt
    return total % 10 == 0


def luhn_valid_number(prefix: str = "400000000000") -> str:
    """Nomor 16 digit yang lolos Luhn dan **tidak** ada di allowlist placeholder gate.

    Nomor uji kanonik (4111…, 4242…) ada di allowlist dengan alasan tertulis, jadi
    memakainya sebagai kontrol positif hanya akan membuktikan allowlist bekerja.
    """
    allowlisted = {"4111111111111111", "4242424242424242", "5500000000000004"}
    for filler in range(1000):
        candidate = prefix + f"{filler:03d}"
        for check in "0123456789":
            number = candidate + check
            if luhn_ok(number) and number not in allowlisted:
                return number
    raise AssertionError("tidak menemukan nomor uji yang lolos Luhn")


def test_the_gate_refuses_a_root_it_cannot_read(tmp_path: Path) -> None:
    """Root yang tidak ada, atau berupa berkas: harus FAIL, bukan PASS atas nol berkas."""
    missing = run_gate(tmp_path / "tidak-ada")
    assert missing.returncode == 1, missing.stdout
    assert "PII-SCAN: PASS" not in missing.stdout, missing.stdout
    assert "never read" in missing.stdout or "not a directory" in missing.stdout

    a_file = tmp_path / "bukan-direktori.txt"
    a_file.write_text("bukan pohon\n", encoding="utf-8")
    assert run_gate(a_file).returncode == 1


def test_the_gate_refuses_a_tree_with_nothing_readable(tmp_path: Path) -> None:
    """Pohon yang tidak punya satu pun berkas teks: 0 temuan ≠ bersih."""
    (tmp_path / "gambar.bin").write_bytes(b"\x00\x01\x02\x03binary only\x00")
    result = run_gate(tmp_path)
    assert result.returncode == 1, result.stdout
    assert "PII-SCAN: PASS" not in result.stdout, result.stdout
    assert "0 text files scanned" in result.stdout


def test_the_gate_catches_planted_secret_shapes(tmp_path: Path) -> None:
    """Kontrol positif: bentuk yang harus tertangkap, benar-benar tertangkap."""
    email = PLANTED_EMAIL
    phone = PLANTED_PHONE
    card = luhn_valid_number()
    grouped = " ".join(card[i : i + 4] for i in range(0, 16, 4))
    bare = luhn_valid_number("400000000001")

    (tmp_path / "bocor.txt").write_text(
        f"kontak: {email}\nseluler: {phone}\nkartu: {grouped}\nnik: {bare}\n",
        encoding="utf-8",
    )
    result = run_gate(tmp_path)
    assert result.returncode == 1, result.stdout
    for kind in ("e-mail", "phone", "card number", "16-digit id"):
        assert kind in result.stdout, f"{kind} tidak dilaporkan:\n{result.stdout}"


def test_a_tree_with_only_synthetic_values_still_passes(tmp_path: Path) -> None:
    """Kontrol arah sebaliknya: nilai sintetis yang memang boleh ada tidak memblokir rilis."""
    (tmp_path / "dokumen.md").write_text(
        "Contoh surat: budi@villa.id\nNomor contoh: 081234567890\n",
        encoding="utf-8",
    )
    result = run_gate(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "PASS (0 findings, 1 text files scanned)" in result.stdout


@pytest.mark.parametrize("kind", ["e-mail", "phone", "card number"])
def test_each_shape_is_reported_on_its_own(tmp_path: Path, kind: str) -> None:
    """Satu jenis per berkas: laporan harus menyebut jenisnya, bukan sekadar 'ada temuan'."""
    planted = {
        "e-mail": PLANTED_EMAIL,
        "phone": PLANTED_PHONE,
        "card number": " ".join(
            luhn_valid_number()[i : i + 4] for i in range(0, 16, 4)
        ),
    }
    (tmp_path / "satu.txt").write_text(f"nilai: {planted[kind]}\n", encoding="utf-8")
    result = run_gate(tmp_path)
    assert result.returncode == 1, result.stdout
    assert kind in result.stdout, result.stdout


def test_the_repository_itself_is_clean_and_the_count_is_reported() -> None:
    """Gate pada pohon repo ini: lulus, dan menyebut berapa berkas yang benar-benar dibaca."""
    result = subprocess.run(
        [sys.executable, str(SCAN)], cwd=str(REPO), capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stdout
    assert "PASS (0 findings," in result.stdout
    read = int(result.stdout.split("0 findings, ")[1].split(" text files")[0])
    assert read > 300, f"hanya {read} berkas dibaca — gate tidak benar-benar memindai"
