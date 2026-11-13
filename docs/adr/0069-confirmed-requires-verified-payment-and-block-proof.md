# ADR-0069 — `CONFIRMED` hanya setelah pembayaran terverifikasi dan bukti blok villa

- Status: **PROPOSED** (patch audit 2026-11-13, temuan K2; menunggu review Mas Anas)
- Date: 2026-11-13
- Deciders: Mas Anas (owner) · divisi engineering Villa Agency OS
- Supersedes (sebagian): ADR-0050 (gerbang komersial tetap berlaku, tetapi pintunya pindah dari
  `confirm_from_hold` ke `reserve_from_hold`) · melengkapi ADR-0033 · ADR-0052
- Spec: `src/reservation_kernel.py` · `src/booking_states.py` · `tests/test_audit_k2_paid_then_confirmed.py`
  · `tests/booking_support.py` · `scripts/go_live_smoke.py`

## Masalah

ADR-0033 menetapkan urutan: tamu membayar agensi → Finance memverifikasi → booking
`DEPOSIT_PAID`/`PAID_IN_FULL` → villa memblok tanggal → `CONFIRMED`. Kode tidak mengikuti
urutan itu: `confirm_from_hold` menulis `CONFIRMED` langsung dari hold, dengan
`amount_paid_minor = 0`, dan menulis sisi `HOLD_ACTIVE → CONFIRMED` yang **tidak ada** di
graf kanonik `booking_states.py`. Akibatnya status paling kuat di sistem (yang memicu voucher,
pre-arrival, dan KPI okupansi) bisa lahir tanpa uang dan tanpa bukti villa.

## Keputusan

1. `confirm_from_hold` dipensiunkan: selalu menolak dengan
   `CONFIRM_REQUIRES_PAYMENT_AND_BLOCK_PROOF`. Nama dipertahankan supaya pemanggil lama gagal
   keras, bukan diam-diam hilang.
2. Tiga perintah menggantikannya, masing-masing satu sisi graf kanonik:
   - `reserve_from_hold` — `HOLD_ACTIVE → PAYMENT_PENDING`. Semua pemeriksaan lama (quote,
     unit, policy, void, kedaluwarsa, gerbang komersial ADR-0050) tetap di sini. Hold tetap
     `CONFIRMED` sehingga slot tetap terkunci selama menunggu pembayaran.
   - `mark_paid` — `PAYMENT_PENDING`/`PAYMENT_PARTIAL` → `DEPOSIT_PAID`/`PAID_IN_FULL`, dan
     `DEPOSIT_PAID → PAID_IN_FULL`. Wajib `payment_id` milik invoice booking itu dengan status
     `SUCCEEDED`; nominal di bawah deposit policy ditolak (`PAYMENT_BELOW_DEPOSIT`).
   - `confirm_booking` — `DEPOSIT_PAID`/`PAID_IN_FULL → CONFIRMED`. Wajib bukti ketersediaan
     unit yang sama, level A/B, `can_confirm`, hasil `AVAILABLE*`, dan ditangkap **setelah**
     booking dibuat (bukti lama tidak bisa dipakai ulang). Hold → `CONVERTED`.
3. `cancel_booking` sebelum konfirmasi meminta pelepasan hold (`HOLD_RELEASE_REQUESTED`) agar
   slot kembali ke penjualan lewat jalur pelepasan yang sudah ada.

## Konsekuensi

- Booking kini menempuh 3 langkah manusia (reservation, finance, reservation). Smoke go-live
  bertambah satu langkah (17).
- **Temuan baru, sengaja tidak diubah di patch ini:** kernel juga menulis sisi pembatalan
  `CONFIRMED/PRE_ARRIVAL/IN_STAY/BALANCE_DUE → CANCELLED` yang tidak ada di graf kanonik
  `booking_states.py`. Perlu keputusan owner: tambahkan ke graf (dengan alasan bisnis) atau
  arahkan lewat `CANCELLATION_REQUESTED`.
- Data lama: booking `CONFIRMED` dengan `amount_paid_minor = 0` perlu ditinjau manual;
  patch ini tidak memigrasi baris yang sudah ada.
