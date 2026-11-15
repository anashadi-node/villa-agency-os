"""Satu transaksi, satu aturan idempotensi — dasar jalur tulis Workforce (T1.1).

Kenapa modul sekecil ini ada. Tiga penulis Workforce (roster, absensi, timesheet)
menghadapi masalah yang sama: perintah bisa tiba dua kali (retry jaringan, tombol
diklik ganda, impor dijalankan ulang), dan setiap tabel punya kunci yang berbeda
untuk mendeteksinya — absensi memakai ``idempotency_key``, roster memakai
``(staff_id, starts_at_utc)``. Kalau tiap modul menulis pemeriksaannya sendiri,
cepat atau lambat salah satunya lupa dan duplikat masuk. Jadi kuncinya
dipusatkan di sini, sekali, dan diuji sekali.

Kontrak modul:

1. **``insert_once()`` menulis paling banyak satu baris.** Kalau baris dengan
   kombinasi ``conflict_on`` yang sama sudah ada, INSERT dibatalkan oleh
   ``ON CONFLICT DO NOTHING`` dan fungsi mengembalikan ``False``. Pemanggil tidak
   pernah menerima pengecualian integritas mentah, dan tidak pernah menulis dua
   kali karena membaca-dulu-baru-menulis.
2. **Identitas kolom tidak pernah datang dari masukan bebas.** ``table``,
   ``columns`` dan ``conflict_on`` wajib berupa pengenal SQL polos; nama lain
   ditolak ``WORKFORCE_BAD_IDENTIFIER`` sebelum ada string yang disusun. Ini
   pertahanan terhadap SQL yang dirakit dari nama kolom kiriman pemanggil.
3. **``write_transaction()`` adalah satu-satunya cara menulis.** ``BEGIN
   IMMEDIATE`` lalu commit, atau rollback penuh. Perintah yang ditolak di tengah
   batch tidak meninggalkan baris separuh jadi.
4. **Kunci idempotensi punya panjang minimum.** ``MIN_IDEMPOTENCY_KEY_LENGTH``
   (8) berlaku untuk semua penulis: kunci pendek seperti ``"1"`` atau tanggal
   saja bukan kunci, dan menerimanya berarti dua peristiwa berbeda bisa
   bertabrakan menjadi satu.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from primitives import DomainError, require_text

MIN_IDEMPOTENCY_KEY_LENGTH = 8

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class WorkforceError(DomainError):
    """Kegagalan yang disengaja pada jalur tulis Workforce."""

    code = "WORKFORCE_ERROR"


class BadIdentifier(WorkforceError):
    code = "WORKFORCE_BAD_IDENTIFIER"


class InvalidIdempotencyKey(WorkforceError):
    code = "WORKFORCE_INVALID_IDEMPOTENCY_KEY"


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Satu perintah = satu transaksi: ``BEGIN IMMEDIATE``, commit, atau rollback.

    ``IMMEDIATE`` mengambil kunci tulis di awal, sehingga pemeriksaan bentrok
    jadwal di dalam blok ini tidak bisa disela penulis lain di tengah jalan.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def require_identifier(value: Any, field_name: str) -> str:
    """Terima hanya pengenal SQL polos (huruf, angka, garis bawah)."""
    text = require_text(value, field_name, max_len=64)
    if not _IDENTIFIER.match(text):
        raise BadIdentifier(
            f"{field_name} must be a plain SQL identifier",
            field=field_name,
            value=text,
        )
    return text


def require_idempotency_key(value: Any, field_name: str = "idempotency_key") -> str:
    """Kunci idempotensi wajib jelas: bukan kosong, bukan singkatan. 8–200 karakter."""
    text = require_text(value, field_name, max_len=200)
    if len(text) < MIN_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidIdempotencyKey(
            f"{field_name} must be at least {MIN_IDEMPOTENCY_KEY_LENGTH} characters",
            field=field_name,
            length=len(text),
        )
    return text


def insert_once(
    conn: sqlite3.Connection,
    *,
    table: Any,
    columns: Mapping[str, Any],
    conflict_on: Sequence[Any],
) -> bool:
    """INSERT satu baris; ``False`` bila baris dengan kunci yang sama sudah ada.

    ``conflict_on`` harus menunjuk kunci unik yang benar-benar ada di skema —
    kalau tidak, SQLite melempar ``OperationalError`` dan itu memang kesalahan
    programmer, bukan data. Nilai ``None`` tetap ditulis apa adanya (NULL),
    karena banyak kolom opsional memang berarti "belum diisi".
    """
    table_name = require_identifier(table, "table")
    if not isinstance(columns, Mapping) or not columns:
        raise BadIdentifier("columns must be a non-empty mapping", field="columns")
    names = [require_identifier(name, "columns") for name in columns]
    conflict_names = [require_identifier(name, "conflict_on") for name in conflict_on]
    if not conflict_names:
        raise BadIdentifier("conflict_on must name at least one column", field="conflict_on")
    missing = [name for name in conflict_names if name not in names]
    if missing:
        raise BadIdentifier(
            "conflict_on must be a subset of the inserted columns",
            field="conflict_on",
            missing=",".join(missing),
        )

    placeholders = ", ".join("?" * len(names))
    sql = (
        f"INSERT INTO {table_name} ({', '.join(names)}) VALUES ({placeholders})"
        f" ON CONFLICT ({', '.join(conflict_names)}) DO NOTHING"
    )
    cursor = conn.execute(sql, tuple(columns[name] for name in names))
    return cursor.rowcount == 1
