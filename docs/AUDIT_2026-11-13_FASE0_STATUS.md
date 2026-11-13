# Status Fase 0 — Audit 2026-11-13

Dokumen kerja: satu baris per butir Fase 0 pada "Audit & Rekomendasi Penyempurnaan Villa
Agency OS". Kolom **Bukti** hanya berisi hal yang benar-benar dijalankan di repo ini, bukan
klaim. Kolom **Status** hanya punya tiga nilai: `TERKIRIM` (kode + tes ada dan lulus),
`SEBAGIAN` (ada tetapi ada yang belum), `TERBUKA` (belum dikerjakan).

Basis: `23a0a0d` → tujuh commit patch (`f2abe79`…`c488222`) → commit kerja sesudahnya.

## Ringkasan

| Butir | Temuan | Status | Bukti |
| --- | --- | --- | --- |
| K1 | PII (kartu, CVV, paspor, telepon) diredaksi sebelum disimpan | TERKIRIM | `tests/test_audit_k1_sensitive_redaction.py`; `scripts/pii_scan.py`; `scripts/scan_stored_pii.py` (hanya-baca) |
| K2 | `CONFIRMED` hanya setelah pembayaran terverifikasi + bukti blok | TERKIRIM | `tests/test_audit_k2_paid_then_confirmed.py`; `tests/test_no_double_booking_concurrency.py`; ADR-0069 |
| K5 | Satu koneksi diam tidak melumpuhkan situs/dashboard; `/healthz`; `HEAD` | TERKIRIM | `tests/test_audit_k5_concurrent_reads.py` |
| T1 | `tax_applies_to`, `tax_included_in_rate`, `blackout_dates` di kalkulator | SEBAGIAN | `tests/test_audit_t1_tax_rules.py`; nilai per kabupaten masih menunggu keputusan owner |
| S1 | Token formulir HMAC berumur 3 detik–2 jam | TERKIRIM | `tests/test_audit_s1_form_tokens.py` |
| S2 | `guest.registered` + `case.opened` di rantai audit, satu transaksi | TERKIRIM | `tests/test_audit_s2_intake_audit_rows.py` |
| S4 | CI matriks Python 3.11/3.12/3.13 + langkah go-live smoke | TERKIRIM | `.github/workflows/check.yml` |
| S6 | Akun `owner_anas` di data seed | TERBUKA | ditunda bersama perubahan seed berikutnya (menggeser fingerprint policy) |
| T8 | Rupiah sebagai nilai, USD sebagai rujukan berlabel | SEBAGIAN | ADR-0070; `src/money_display.py`; `tests/test_audit_currency_reference.py`; dokumen tamu masih menunggu counsel |

## K2 — apa yang ditambahkan di luar paket patch

Patch membawa urutan baru (`reserve_from_hold` → `mark_paid` → `confirm_booking`). Satu celah
tersisa: `PAYMENT_PENDING` tidak menempati slot di tabel `bookings` (dan memang tidak boleh —
`OCCUPYING_BOOKING_STATES` harus tetap sepasang dengan indeks unik parsial di skema), sehingga
slot ditahan oleh hold. Tanpa penjagaan, `release_hold` yang berhasil akan mengembalikan slot
ke penjualan sementara booking yang belum dibayar tetap hidup.

Yang dikerjakan:

1. `src/reservation_kernel.py` — konstanta `BACKING_BOOKING_STATES` (empat status yang masih
   bersandar pada hold) dan penjagaan di `release_hold`: hold yang menopang booking hidup
   ditolak dengan `HOLD_BACKS_BOOKING`, beserta `booking_id` di `details`. Jalan keluarnya
   membatalkan booking.
2. `tests/test_no_double_booking_concurrency.py` — tes balapan disesuaikan ke alur K2 (jalur
   `booking_support.confirm_through_payment`), ditambah kelas baru yang mengunci tiga perilaku:
   pelepasan hold ditolak, penantang kedua ditolak `SLOT_ALREADY_HELD`, dan pembatalan booking
   memindahkan hold ke `RELEASE_REQUESTED` (slot masih ditempati sampai pelepasan diverifikasi).
3. ADR-0069 bagian Konsekuensi — alasan penjagaan ini dicatat, bukan hanya ditulis di kode.

## T8 — rupiah + rujukan USD

Permintaan owner: nilai dalam IDR, dengan konversi otomatis ke USD di sampingnya. Audit T8
mengingatkan sisi lain: harga yang mengikat hanya boleh satu mata uang.

Yang dikerjakan (ADR-0070):

- `src/money_display.py` — rupiah lebih dulu, rujukan USD berlabel `≈` + `indicative`, memakai
  kurs terbit `commercial.fx.rates` yang wajib lulus `fx.require_fresh` dengan
  `commercial.fx.max_age_hours`. Tidak ada kurs terbit / tidak ada tanggal / kurs basi /
  pasangan tidak terbit ⇒ rujukan tidak ditampilkan, dan halaman menyebut alasannya. Tidak ada
  kurs cadangan, tidak ada paritas, tidak ada konstanta.
- `src/dashboard.py` + `templates/finance.html` — halaman `/finance` mencetak
  `IDR 4.500.000 (≈ USD 276,92)` untuk baris invoice dan payment, plus satu baris keterangan
  kurs + tanggal. Angka berlabel `(minor)` dibiarkan apa adanya: itu nilai teknis untuk
  rekonsiliasi, bukan harga.
- `GUEST_DOCUMENT_REFERENCE_ENABLED = False` — dokumen tamu (penawaran, invoice, kuitansi)
  tetap rupiah saja sampai counsel menyetujui rujukan pada dokumen yang mengikat.
- `tests/test_audit_currency_reference.py` — 25 tes: aritmetika, pembulatan ke sen terdekat,
  kontrol negatif kurs berubah ⇒ angka berubah, kurs basi, kurs bertanggal masa depan,
  pasangan tak terbit, batas kesegaran tidak terbit, dan dua uji ujung-ke-ujung lewat halaman
  `/finance` (dengan kurs terbit dan tanpa kurs terbit).

## Yang masih menunggu keputusan owner

1. Nilai `tax_applies_to` per kabupaten untuk PBJT (T1).
2. Boleh tidaknya rujukan USD pada dokumen tamu (T8) — keputusan counsel atas ADR-0030/0060.
3. Pembersihan baris lama yang ditemukan `scripts/scan_stored_pii.py` dan siapa yang menyetujui
   (UU PDP 27/2022).
4. Sisi pembatalan kernel di luar graf kanonik `booking_states.py` (temuan baru pada ADR-0069).
5. S6: akun `owner_anas` pada data seed.
