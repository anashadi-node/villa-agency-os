# ADR-0056 — Settlement supplier jadi fakta berpunya: `supplier_settlements` (DUE/PAID)

- Status: Accepted
- Date: 2026-10-03
- Deciders: platform + Mas Anas (menutup blocker §30 terakhir — gerbang penutupan kini punya
  ketujuh pemiliknya)
Bagian dari: dokumen 30 (gerbang penutupan booking), §9 (Agent 3), ADR-0052/0054/0055

## Konteks

ADR-0052 menjadikan tujuh kondisi §30 gerbang penutupan yang fail-closed. Tiga fakta tak
berpunya; 0054 dan 0055 menutup dua. Yang tersisa: **settlement supplier** — `supplier_route`
memverifikasi kontrak/rate/hubungan villa, tapi tidak ada modul yang merekam **uang yang
menyelesaikannya**. Tanpa pemilik, tidak ada booking yang bisa ditutup — dan itulah kondisi
yang benar sampai fakta ini punya rumah.

Keputusan komersial yang menentukan bentuknya: platform ini dijual sebagai produk white-label
(konfigurasi milik tiap agency). Karena itu **besaran yang supplier terima tidak boleh
dihitung di kode** — kontrak tiap route berbeda (komisi, full management, hybrid), dan
menghardcode satu formula = memaksa satu model bisnis ke semua customer.

## Keputusan

1. **Tabel `supplier_settlements`** (001_initial.sql + migrasi `014`), append-only (trigger
   menolak UPDATE/DELETE), FK ke `bookings`, unik `(agency_id, idempotency_key)`.
2. **Dua aksi, satu tabel**: `DUE` menyatakan apa yang supplier terima untuk booking ini
   (keputusan manusia, dengan kontrak/SOP sebagai `evidence_ref`); `PAID` merekam bagian
   yang melunasinya. `route_id` ikut terekam dari baris booking — setiap settlement bisa
   dijawabkan ke route yang kontraknya menghasilkannya.
3. **Satu penulis**: `src/supplier_settlements.py`. Manusia saja (predikat machine-actor
   bersama ADR-0055), mata uang = mata uang booking, over-payment ditolak di penulis.
4. **Gate membaca ulang aritmetika** (`booking_closure._supplier`): tanpa baris → gap;
   pembayaran tanpa `DUE` (keadaan tak mungkin lewat penulis — tetap digerbangi defensif);
   sisa belum dibayar; lebih bayar — semuanya blocker bernama. `sum(PAID) == sum(DUE)` → lolos.
5. **Uang integer minor**, tidak ada float, tidak ada angka manual di gate.

## Alternatif yang ditolak

| Alternatif | Kenapa tidak |
|---|---|
| Hitung share supplier dari contract/rate di kode | Formula pembagian berbeda per model layanan dan per tenant; menhardcode satu formula melanggar framing white-label dan mengubah modul yang kontraknya terkunci (ADR versi contract butuh keputusan owner terpisah) |
| Satu kolom `settled` boolean di bookings | Klaim tanpa angka dan tanpa bukti — bukan rekonsiliasi; tidak bisa diaudit, tidak bisa dibantah |
| `PAID` tanpa `DUE` | Membiarkan uang mengalir tanpa deklarasi yang bisa diuji; gate tidak punya pembanding |

## Dampak

- **Positive**: ketujuh kondisi §30 kini punya pemilik; `close_booking` bisa lolos gerbang
  secara sah; blocker list tetap bernama untuk setiap keadaan yang belum selesai.
- **Negative**: tenant wajib mendeklarasikan `DUE` (satu tindakan manusia dengan bukti)
  sebelum melunasi — sengaja: deklarasi yang bisa diuji adalah inti rekonsiliasi.

## Verifikasi

- `tests/test_supplier_settlements.py` — 11 tes: penulis (6) dan gate (5) sesuai daftar klaim
  di header berkas; ledger append-only diuji via trigger.
- Full suite + `make check` (secret/unsafe scan 0, twin parity, preflight) → PASS sebelum commit.

## Implikasi CLI (tindak lanjut)

Fakta §30 ditulis staf tenant lewat CLI (`scripts/closure_facts.py` — deposit + settlement +
status gate), bukan lewat kode — sama seperti `publish_policy_value.py` untuk kebijakan.