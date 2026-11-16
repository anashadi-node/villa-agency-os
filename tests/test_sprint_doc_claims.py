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

# Klaim harus MENEMPEL pada berkasnya — jarak maksimal 12 karakter, dua arah:
#   "`tests/test_x.py` (17 uji)"      dan   "35 uji di `tests/test_x.py`"
# Jendela lebar (60 karakter) pernah dipakai dan menghasilkan empat positif palsu: angka
# lama yang dikutip dalam kalimat ("T2.6 menulis 14 uji pada ...") terbaca sebagai klaim
# status hari ini, dan angka milik berkas lain terbaca sebagai milik berkas berikutnya.
# Konvensi ini yang membuat pemeriksa bisa dipercaya: kalau sebuah angka menyebut suatu
# berkas, ia harus berdiri tepat di sebelahnya.
CLAIM_PATTERNS = (
    re.compile(r"tests/(test_[A-Za-z0-9_]+\.py)`?\s*\(?\s*(\d+)\s*uji"),
    re.compile(r"(\d+)\s*uji[^0-9\n]{0,12}?tests/(test_[A-Za-z0-9_]+\.py)"),
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


def reachable_commits() -> set[str]:
    """Commit yang bisa diperiksa orang lain: yang terjangkau dari ref mana pun.

    `git cat-file -e` tidak cukup. Di clone yang pernah rebase/amend, commit lama masih ada sebagai
    objek tak terjangkau — jadi pemeriksaan berbasis objek LOLOS di mesin pengembang dan GAGAL di
    clone bersih: hijau lokal, merah di CI, dan rujukan menggantung lolos sampai ada yang mengkloning
    ulang. Persis itu yang terjadi pada `docs/AUDIT_2026-11-13_FASE0_STATUS.md`.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "rev-list", "--all"], capture_output=True, text=True
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def commit_is_present(sha: str) -> bool:
    """Ada DAN terjangkau? Clone dangkal tidak bisa menjawab, dan itu bukan "tidak ada".

    CI memakai `fetch-depth: 0` sejak T2.8, jadi di sana riwayatnya lengkap dan pertanyaan ini
    benar-benar terjawab. Di clone dangkal, menjawab "tidak ada" akan menuduh dokumen yang jujur
    (dan `git fetch origin <sha>` tidak bisa dipakai sebagai bukti: server boleh menolak sha yang
    valid sekalipun), jadi keadaannya dinyatakan apa adanya, bukan disimpulkan.
    """
    resolved = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
        capture_output=True, text=True,
    )
    if resolved.returncode == 0:
        # rev-parse menerima singkatan 7 huruf dan mengembalikan sha penuh; rev-list selalu penuh,
        # jadi keduanya harus disamakan dulu sebelum dibandingkan.
        return resolved.stdout.strip() in reachable_commits()
    shallow = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True,
    ).stdout.strip()
    if shallow == "true":
        pytest.skip(
            f"clone dangkal: {sha} tidak ada di objek lokal, dan 'tidak terlihat dari sini' "
            "bukan 'tidak ada di riwayat' — jalankan di clone beriwayat penuh untuk memeriksanya"
        )
    return False


def test_every_cited_commit_survives_a_squash_merge() -> None:
    """Repo ini hanya mengizinkan squash/rebase: sha yang hidup di cabang akan hilang.

    Setiap token heksadesimal yang dikutip dokumen harus bisa diperiksa orang lain di riwayat
    master, atau berupa id run CI (angka >= 9 digit) yang juga bertahan. Sha cabang yang akan
    dihapus squash merge adalah janji yang tidak bisa ditepati.
    """
    cited = set(re.findall(r"`([0-9a-f]{7,40})`", DOC.read_text(encoding="utf-8")))
    assert len(cited) >= 2, f"hanya {len(cited)} token dikutip: penguncinya kehilangan gigi"
    for token in sorted(cited):
        if token.isdigit() and len(token) >= 9:
            continue  # id run CI: provenance di luar riwayat git, dan itu disengaja
        assert commit_is_present(token), (
            f"dokumen mengutip `{token}` yang tidak ada di riwayat: squash merge akan "
            "menghapusnya, dan pembaca tidak bisa memeriksanya"
        )


def attribution(line: str):
    """Token provenance di baris ini: commit yang ada, atau id run CI (angka >= 9 digit)."""
    found = re.search(r"`([0-9a-f]{7,40})`", line)
    if not found:
        return None
    token = found.group(1)
    if token.isdigit() and len(token) >= 9:
        return ("run", token)
    return ("commit", token)


DOC_ROOTS = ("docs", "reports")


def test_no_documentation_cites_a_commit_that_does_not_exist() -> None:
    """Rujukan yang menggantung di dokumen mana pun adalah rujukan yang tidak bisa diperiksa.

    Berlaku untuk seluruh dokumentasi + laporan, bukan hanya dokumen sprint: sha cabang hilang
    saat squash merge, dan pembaca yang mencoba memeriksanya akan menemukan halaman kosong.
    """
    files = [Path("README.md")] if Path("README.md").exists() else []
    for root in DOC_ROOTS:
        files += sorted(Path(root).rglob("*.md"))
    assert files, "tidak ada berkas dokumentasi yang ditemukan — cek jalurnya"
    checked, bad = 0, []
    for f in files:
        for token in sorted(set(re.findall(r"`([0-9a-f]{7,40})`", f.read_text(encoding="utf-8")))):
            checked += 1
            if token.isdigit() and len(token) >= 9:
                continue  # id run CI: bertahan di luar riwayat git
            if not commit_is_present(token):
                bad.append(f"{f}: `{token}`")
    assert checked >= 20, f"hanya {checked} token diperiksa: penguncinya kehilangan gigi"
    assert not bad, f"rujukan menggantung (tidak ada di riwayat): {bad}"


def test_every_claimed_total_names_the_commit_it_was_measured_at() -> None:
    """Total "N passed" tanpa provenance adalah angka yang tidak bisa diperiksa siapa pun."""
    lines = DOC.read_text(encoding="utf-8").splitlines()
    totals = [(n, line) for n, line in enumerate(lines, 1) if TOTAL.search(line)]
    assert totals, "tidak ada klaim total 'N passed' di dokumen — pola berubah?"
    unattributed = [n for n, line in totals if attribution(line) is None]
    assert not unattributed, (
        "klaim total tanpa provenance di baris: "
        f"{unattributed} — tulis 'diukur pada `<sha>`' atau run CI `<id>` di baris yang sama"
    )
    for lineno, line in totals:
        kind, token = attribution(line)
        if kind == "run":
            continue  # run CI tidak bisa diperiksa luring; id-nya bertahan, sha cabang tidak
        assert commit_is_present(token), (
            f"baris {lineno}: commit {token} tidak ada di riwayat"
        )
