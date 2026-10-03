# ADR-0054 — Daftar dokumen wajib §30 jadi satu policy key milik tenant

- Status: Accepted
- Date: 2026-10-03
- Deciders: platform + Mas Anas (menutup 1 dari 3 blocker §30 yang fail-closed)
Bagian dari: dokumen 30 (gerbang penutupan booking), §16 (policy), ADR-0052

## Konteks

ADR-0052 menegakkan gerbang penutupan §30 dengan tujuh kondisi, dan **tiga fakta
belum berpunya** membuat setiap booking gagal ditutup (sengaja). Salah satunya:
`required_documents` — platform tidak tahu dokumen apa yang tenant wajibkan.
Menambahkan daftar itu lewat kode berarti mengubah kebijakan komersial tiap
tenant lewat deploy — bertentangan dengan §15 ("threshold adalah configuration,
bukan hard-coded") dan dengan produk white-label: tiap agency punya daftar
dokumen penutupannya sendiri.

## Keputusan

1. **Satu key, milik tenant**: `operations.booking.required_document_kinds`
   (domain `OPERATIONS`, `JSON`, risk `MEDIUM`, owner `owner`). Isinya daftar
   `kind` dari kosakata tertutup `documents.DOCUMENT_KINDS`
   (CONFIRMATION/RECEIPT/INVOICE/CONTRACT/NOTICE/REPORT).
2. **Deklarasi ≠ pemberian.** Key dideklarasikan di `policy_defs` saja; tanpa
   revisi terbit, cek dokumen tetap fail-closed dengan pesan lama — perilaku
   yang sama dengan dua fakta §30 lain yang belum berpunya.
3. **Gate membaca, platform tidak menebak** (`booking_closure._documents`):
   - tidak terbit → fail-closed, nama gap + kind yang sudah terekam;
   - terbit dan semua kind `GENERATED` → **lolos**, detail menyebut id revisi;
   - ada kind yang belum terekam → gagal, kind yang kurang dinamai;
   - kind di luar kosakata platform → gagal tertutup **dengan nama** (daftar
     yang menuntut kind tak terekam akan menahan semua booking terbuka selamanya
     sambil terlihat seperti ketelitian);
   - bentuk tak terbaca (bukan list string) → gagal tertutup, isi dimunculkan;
   - **list kosong = keputusan nyata** (tenant tidak mewajibkan apa pun) → lolos;
     berbeda dengan tidak terbit (tidak memutuskan).
4. **Publikasi lewat alat yang ada**: `scripts/publish_policy_value.py set
   --key ... --value '["CONFIRMATION","INVOICE"]' --source-doc-ref ... --approved-by
   ...` — tanpa kode baru, tanpa deploy, jejak revisi permanen (append-only).

## Alternatif yang ditolak

| Alternatif | Kenapa tidak |
|---|---|
| Daftar wajib dari kode / `DOCUMENT_KINDS` semua diwajibkan | Kebijakan komersial per tenant masuk kode; white-label mati. |
| Kolom settings (`settings.py`) | Bukan setelan operasional yang dibaca pesan/pajak — ini keputusan tata kelola yang butuh revisi, sumber dokumen, dan penyetuju manusia (§16). |
| Bolehkan kind di luar kosakata | Kind yang tidak bisa pernah terekam membuat gerbang tidak bisa lolos sekalipun dokumen nyata ada — konfigurasi salah yang terlihat benar. |
| Validasi kosakata di `publish` | Katalog policy generik tidak tahu kosakata dokumen; yang tahu adalah `documents`. Validasi dikerjakan oleh konsumen yang membaca (gate), gagal tertutup dan bernama. |

## Konsekuensi

- Blocker §30 turun dari tiga ke **dua** (settlement supplier, resolusi
  deposit) — keduanya masih fail-closed sampai pemiliknya dibangun.
- Tenant white-label bisa mendeklarasikan dokumen penutupannya sendiri lewat
  CLI policy yang sudah ada; produk tetap satu pola kebijakan (§16).
- Tes identitas `test_wave1h_policy_engine` tetap hijau: key baru lolos syarat
  `spec_ref` diawali `§` dan risk/domain/type terdaftar.

## Verifikasi

- `tests/test_booking_closure.py` — 4 tes baru: daftar terpenuhi lolos dan
  menyebut revisinya; kind belum terekam dinamai; kind di luar kosakata gagal
  tertutup bernama; list kosong lolos sebagai keputusan. Tes lama
  (tidak terbit → fail-closed) tidak berubah dan tetap hijau.
- Seed schema: `001_initial.sql` + `001_initial.pg.sql` (twin) satu baris
  `policy_defs`; tanpa revisi awal.
- Suite penuh + `make check` hijau sebelum commit.