#!/usr/bin/env python3
"""Gerombolan Workforce Ops tidak boleh memancarkan teks Indonesia (ADR-0011 §55).

Kenapa skrip ini ada, padahal sudah ada ``tests/test_language_standard.py``:
ratchet global itu bekerja dengan daftar kata penanda yang **terbatas** dan
jumlah yang dipin per berkas legacy. Dua akibatnya nyata:

1. kata Indonesia yang tidak ada di daftar penanda tidak pernah terdeteksi —
   mis. ``"issue() hanya menerima PayrollCalculation"`` hidup di produksi tanpa
   satu pun tes gagal;
2. berkas baru (seperti ``src/workforce/*.py``) tidak punya pin, jadi aturannya
   "berkas tanpa entri harus nol" hanya berlaku untuk kata yang terdaftar.

Skrip ini menutup kedua celah itu **tanpa menyentuh pin legacy**: cakupannya
dibatasi ke ``src/workforce/``, dan penandanya sengaja lebih luas.

Batas yang jujur: ini bukan detektor bahasa. Ia menandai kata Indonesia yang
lazim muncul di pesan galat. Kata yang tidak ada di daftar tetap lolos — karena
itu keluaran "0 temuan" hanya bermakna bila kontrol negatif di
``tests/test_workforce_language.py`` membuktikan detektor ini benar-benar
menandai contoh Indonesia yang nyata.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "src" / "workforce"

#: Kata Indonesia yang lazim di pesan galat. Sengaja menghindari homograf
#: Inggris ("total", "jam", "ada" sebagai nama bahasa, "dan" sebagai nama orang)
#: supaya temuan di bawah ini bisa dipercaya tanpa penyaringan manual.
MARKERS = (
    "wajib",
    "tidak",
    "belum",
    "hanya",
    "harus",
    "sudah",
    "yang",
    "dengan",
    "untuk",
    "dari",
    "pada",
    "bila",
    "maka",
    "juga",
    "boleh",
    "tersedia",
    "kosong",
    "salah",
    "gagal",
    "berhasil",
    "periode",
    "terbalik",
    "menit",
    "hari",
    "kerja",
    "upah",
    "gaji",
    "potongan",
    "tarif",
    "periksa",
    "jumlah",
    "kurang",
    "lebih",
    "semua",
    "karena",
    "oleh",
    "telah",
    "punya",
    "milik",
    "ditolak",
    "diizinkan",
    "dibatalkan",
    "diminta",
    "tanpa",
    "tiap",
    "setiap",
    "bulan",
    "tanggal",
    "orang",
    "nama",
    "biaya",
    "nilai",
    "batas",
    "saat",
    "ketika",
    "tercatat",
    "melebihi",
    "pendapatan",
)

#: Pencocokan **kata utuh**, bukan substring: ``Asia/Makassar`` memuat "maka",
#: ``Makassar`` bukan pelanggaran bahasa. Tanpa batas kata, detektor ini akan
#: melaporkan zona waktu sebagai temuan dan kepercayaan pada keluarannya hilang.
_WORDS = re.compile(r"\b(" + "|".join(MARKERS) + r")\b", re.IGNORECASE)


def _docstring_ids(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def indonesian_literals(source: str) -> list[tuple[int, str, str]]:
    """(baris, kata, potongan teks) untuk setiap literal yang memancarkan Indonesia.

    Docstring dilewati — sejalan dengan ratchet global: catatan untuk developer
    adalah tingkat yang lebih rendah daripada kalimat yang bisa dibaca tamu.
    """
    tree = ast.parse(source)
    skip = _docstring_ids(tree)
    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        match = _WORDS.search(node.value)
        if match is not None:
            snippet = " ".join(node.value.split())[:90]
            hits.append((node.lineno, match.group(1).lower(), snippet))
    return sorted(hits)


#: Katalog label layar (T2.6) -> jumlah kata Indonesia terukur.
#:
#: Berkasnya sengaja di luar jangkauan ratchet global (yang hanya membaca ``src/**/*.py``,
#: ``templates/*.html`` dan ``templates/messages/*.json``). Karena itu jumlahnya dipaku **di
#: sini**: tanpa pin, copy Indonesia di layar staf bisa tumbuh tanpa tercatat siapa pun —
#: persis kelemahan yang membuat ratchet berhenti jadi ratchet.
CATALOGUE = REPO_ROOT / "src" / "workforce" / "screen_labels.json"
RECORDED_CATALOGUE = {"screen_labels.json": 9}


def catalogue_hits() -> int:
    """Kata Indonesia di **nilai** katalog. Nama grup dan kunci tidak dihitung.

    Mengembalikan -1 kalau berkasnya tidak terbaca atau tidak bisa di-parse: katalog yang
    rusak dihitung sebagai temuan, bukan sebagai bersih.
    """
    try:
        raw = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return -1
    if not isinstance(raw, dict):
        return -1
    hits = 0
    for values in raw.values():
        if isinstance(values, dict):
            hits += sum(len(_WORDS.findall(str(text))) for text in values.values())
    return hits


def scan() -> list[str]:
    findings: list[str] = []
    for path in sorted(TARGET.glob("*.py")):
        for line, word, snippet in indonesian_literals(path.read_text(encoding="utf-8")):
            findings.append(f"{path.relative_to(REPO_ROOT)}:{line}: [{word}] {snippet}")
    return findings


def main() -> int:
    findings = scan()
    measured = catalogue_hits()
    allowed = RECORDED_CATALOGUE[CATALOGUE.name]
    if measured < 0:
        findings.append(f"{CATALOGUE.name}: unreadable or not a JSON object of label groups")
    elif measured > allowed:
        findings.append(
            f"{CATALOGUE.name}: {measured} > {allowed} — a label added on purpose is fine, but"
            " raise the pin in this file in the same commit so the decision is recorded"
        )
    elif measured < allowed:
        findings.append(
            f"{CATALOGUE.name}: {measured} < {allowed} — lower the pin so the progress stays visible"
        )
    if findings:
        print("FINDINGS: Indonesian text in the workforce cluster (ADR-0011 §55):")
        for item in findings:
            print("  " + item)
        print(f"\n{len(findings)} finding(s); the allowance for this cluster is 0.")
        return 1
    files = len(list(TARGET.glob("*.py")))
    print(f"OK: 0 Indonesian strings across {files} files in src/workforce/*.py")
    print(f"OK: {CATALOGUE.name} holds {allowed} Indonesian strings, exactly as recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
