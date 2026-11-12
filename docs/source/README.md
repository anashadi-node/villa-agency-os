# `docs/source/` — Arsip Dokumen Sumber (Read-Only)

Direktori ini menyimpan **salinan apa adanya** dokumen sumber eksternal yang dirujuk
oleh dokumen di repo. Tujuannya: repo bisa diaudit tanpa bergantung pada berkas di
luar repo yang bisa hilang, berubah, atau tidak bisa diakses pengaudit.

**Aturan direktori ini:**

1. Berkas di sini **tidak diedit**. Kalau dokumen sumber berubah, tambahkan versi baru
   (`..._V1_2_...`) dan catat di tabel bawah; versi lama tetap disimpan.
2. Setiap berkas wajib punya baris di tabel: asal, ukuran, SHA-256, stempel waktu.
3. Isi direktori ini adalah **dokumentasi**, bukan kode yang dieksekusi. Tes dan gerbang
   tidak mengimpornya.

## Daftar arsip

| Berkas | Asal | Ukuran | SHA-256 | mtime asal | Disalin |
|---|---|---|---|---|---|
| `Villa_Agency_AI_Workforce_Architecture_V1_1_Hermes.html` | `~/Downloads/` (Owner) | 47.896 byte | `5368429b6958b155a66d099afdec2d30115c072e8feab06165b7ed987559ae4c` | 2026-09-30 10:59 | 2026-11-13 |

### Catatan berkas `Villa_Agency_AI_Workforce_Architecture_V1_1_Hermes.html`

- Judul halaman: *Villa Agency OS — AI Workforce Architecture V1*.
- **Mandiri (self-contained):** 0 rujukan `http(s)://` pada `src`/`href`, 0 `<script>`,
  0 `<input>` — tidak ada aset jaringan, tidak ada kode yang dieksekusi saat dibuka.
- **Tidak memuat data pribadi:** 0 alamat surel, 0 nomor telepon, 0 nomor rekening.
- **Pemetaan ke repo:** lihat [`../AI_WORKFORCE_TRACEABILITY.md`](../AI_WORKFORCE_TRACEABILITY.md).
  Aturan presedensi dari dokumen itu sendiri (§1): bila bentrok, *invariant* keamanan,
  privasi, kontrol finansial, *business-state*, dan *source-of-truth* pada Master
  Specification yang menang — bukan dokumen ini.
- Dokumen ini adalah **kekayaan intelektual Owner**. Hak pakai mengikuti [`../../LICENSE`](../../LICENSE)
  (all rights reserved); tersedia publik hanya untuk dibaca dan diaudit.

## Cara memverifikasi ulang

```bash
sha256sum docs/source/Villa_Agency_AI_Workforce_Architecture_V1_1_Hermes.html
# harus sama dengan nilai di tabel atas

grep -c '<script' docs/source/Villa_Agency_AI_Workforce_Architecture_V1_1_Hermes.html   # 0
```

Bila angka berbeda dari tabel, berarti berkas berubah: **jangan** perbarui hash tanpa
mencatat versi baru dan alasan perubahan.
