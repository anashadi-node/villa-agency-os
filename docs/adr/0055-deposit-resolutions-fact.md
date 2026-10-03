# ADR-0055 — Resolusi deposit keamanan jadi fakta berpunya: `deposit_resolutions`

- Status: Accepted
- Date: 2026-10-03
- Deciders: platform + Mas Anas (menutup blocker §30 kedua dari tiga)
Bagian dari: dokumen 30 (gerbang penutupan booking), §18 (otoritas manusia), ADR-0052

## Konteks

ADR-0052 membuat gerbang §30 fail-closed pada tiga fakta yang belum berpunya.
Setelah ADR-0054 menutup daftar dokumen wajib, tersisa dua. Yang kedua:
`security_deposit_resolved` — booking menyimpan *jumlah yang ditahan*
(`security_deposit_minor`) tetapi tidak ada modul yang mencatat **apa yang
terjadi** pada uang itu: dikembalikan, hangus, atau dipakai.

## Keputusan

1. **Tabel satu penulis**: `deposit_resolutions` — append-only (trigger menolak
   UPDATE/DELETE), FK ke `bookings`, unik `(agency_id, idempotency_key)`.
   Satu resolusi = satu bagian dari hasil: `action ∈ {RETURNED, FORFEITED,
   APPLIED}`, `amount_minor > 0`, `currency`, `decided_by`, `actor_role`,
   `evidence_ref` (token opak — kontrak rantai audit), `note`.
2. **Otoritas manusia.** Aktor mesin ditolak di penulis sebelum ada baris —
   predikat `supplier_route.is_machine_actor` dipakai bersama, bukan disalin
   (§18/§21/§56: hasil deposit adalah keputusan uang atas nama tenant).
3. **Satu mata uang, satu booking, tanpa over-resolution.** Currency wajib
   sama dengan booking; jumlah kumulatif resolusi tidak boleh melebihi jumlah
   yang ditahan — penulis menolak dengan angka (`DEPOSIT_OVER_RESOLVED`).
4. **Gate membaca ulang aritmetika yang sama** (`booking_closure._deposit`):
   sisa yang belum terselesaikan tetap blocker dan menyebut angkanya;
   resolusi melebihi tahanan = blocker (kontradiksi bukan kelulusan);
   bagian yang berjumlah tepat = lolos, dirinci per aksi; booking tanpa
   deposit tidak diblokir check ini.
5. **Idempoten.** `idempotency_key` yang sama = keputusan yang sama: replay
   mengembalikan baris terekam tanpa menulis kedua — retry setelah timeout
   tidak pernah menjadi fakta kembar.

## Alternatif yang ditolak

| Alternatif | Kenapa tidak |
|---|---|
| Kolom `deposit_resolved` boolean di `bookings` | Menghapus jawaban "apa yang terjadi": RETURNED/APPLIED/FORFEITED bukan boolean, dan tiap perubahan berarti UPDATE atas fakta uang. |
| Menunggu modul settlement supplier, satu tabel untuk keduanya | Dua fakta, dua pemilik arah berbeda (tamu↔agency vs agency↔supplier); menumpuknya menunda fakta yang sudah punya bentuk jelas. |
| Mengizinkan aktor mesin dengan approval manusia belakangan | Uang keluar atas nama tenant adalah HIGH band §56; tidak ada pintu mesin untuk keputusan ini. |
| Mengubah/menarik baris resolusi bila keliru | Append-only: koreksi adalah resolusi lanjutan dengan bukti sendiri, bukan penyuntingan sejarah. |

## Konsekuensi

- Blocker §30 tersisa **satu**: `supplier_reconciliation` (settlement agency↔
  supplier) — masih fail-closed sampai pemiliknya dibangun.
- Penutupan booking yang menahan deposit kini bisa terselesaikan secara
  sah: bagian per bagian, dengan bukti, oleh orang, dan teraudit.

## Verifikasi

- `tests/test_deposit_resolutions.py` — 12 tes: penulisan oleh manusia,
  penolakan mesin/aksi asing/over-resolution/mata uang asing, idempotensi,
  rantai audit ber-evidence, append-only, dan empat jawaban gate (lolos
  berbagian, sisa terbuka bernama, tanpa deposit tidak diblokir, tanpa
  fakta tetap fail-closed dengan pesan lama).
- Migrasi `013_deposit_resolutions.sql` (idempoten) + tabel di
  `001_initial.sql`, twin PG diregenerasi (`generate_pg_twin.py --check`).
- Suite penuh + `make check` hijau sebelum commit.