"""Workforce — jalur tulis data kerja (roster, absensi, timesheet, upah).

Paket ini mengikuti ADR-0071: *kerja* adalah data operasional yang boleh dibaca
supervisor, *upah* adalah ledger terpisah yang tidak pernah keluar dari
payroll/finance. Setiap penulis di sini memakai satu transaksi, satu aturan
idempotensi, dan menolak lebih awal daripada menulis baris yang salah.
"""

from __future__ import annotations

__all__ = ["repository", "roster"]
