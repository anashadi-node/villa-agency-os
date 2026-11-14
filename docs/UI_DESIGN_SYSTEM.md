# Sistem Desain Dashboard — “Hutan & Pura”

Status: **AKTIF** · Terakhir diukur: **2026-11-14** · Sumber kebenaran: `templates/base.html`
(533 baris, 19.699 karakter) · Penjaga: `tests/test_ui_contrast.py` (5 tes)

Dokumen ini menjelaskan **apa yang ada di layar, kenapa begitu, dan bagaimana
mengujinya**. Setiap angka di sini adalah hasil hitung dari `templates/base.html`
yang sebenarnya — bukan perkiraan.

---

## 1. Batasan yang membentuk seluruh keputusan

Dashboard ini adalah **control plane internal** yang dijalankan di mesin kantor/LAN.
Tiga batasan itu menentukan segalanya:

| Batasan | Akibat desain |
|---|---|
| Mesin bisa saja **tanpa internet** | Nol font eksternal, nol CDN, nol `<script>`. Seluruh tampilan = satu `<style>` di `base.html`. Tidak ada permintaan jaringan keluar dari halaman. |
| Halaman **tidak untuk publik** | `<meta name="robots" content="noindex, nofollow">` di setiap halaman. |
| Data di layar = **data operasional & uang** | Angka memakai `font-variant-numeric: tabular-nums` supaya kolom rupiah lurus; mode terang/gelap dua-duanya harus lolos WCAG AA, bukan hanya yang enak dilihat. |

Konsekuensi teknis: **tidak ada build step, tidak ada preprocessor, tidak ada
dependency frontend**. Satu file CSS, satu file Python yang mengisi template.

---

## 2. Tujuh lapisan CSS (urutan wajib dipertahankan)

```
1  tokens      variabel warna, radius, ruang, bayangan
2  canvas      latar basal + tekstur serat anyam (dua gradien diagonal)
3  chrome      header, nav, footer, skip-link
4  surfaces    card, tabel, banner
5  controls    tautan, tombol, chip nav
6  states      badge ok/warn/bad, empty, hint, muted
7  adaptation  light · print · reduced-motion · <=860px
```

Lapisan 7 **tidak boleh** ditulis sebelum lapisan 1–6: semua override adaptasi
adalah penimpa token, bukan warna baru yang ditempel langsung ke elemen.

---

## 3. Token warna + rasio kontras terukur

Latar kanvas: **`--basalt`** `#090d0c` (gelap) / `#f4f1e8` (terang).
Rasio dihitung dengan rumus WCAG 2.1; ambang teks normal = **4.5:1**.

### Mode gelap — kanvas basal vulkanik

| Peran | Token | Nilai | Kontras | Status |
|---|---|---|---|---|
| Teks utama | `--ink` | `#e8f1ec` | **16.96:1** | AA |
| Teks sekunder | `--ink-soft` | `#c3d2ca` | **12.46:1** | AA |
| Teks redup/meta | `--muted` | `#8ba39a` | **7.26:1** | AA |
| Tautan & aksen jade | `--jade` | `#4fc3a1` | **8.98:1** | AA |
| Aksen emas (padi) | `--gold` | `#d9b46a` | **9.95:1** | AA |
| Status ok | `--ok` | `#5cc98f` | **9.50:1** | AA |
| Status warn | `--warn` | `#dfb054` | **9.75:1** | AA |
| Status bad | `--bad` | `#e2635a` | **5.74:1** | AA |

### Mode terang — kanvas pasir vulkanik

| Peran | Token | Nilai | Kontras | Status |
|---|---|---|---|---|
| Teks utama | `--ink` | `#14201b` | **14.85:1** | AA |
| Teks sekunder | `--ink-soft` | `#2b3d35` | **10.21:1** | AA |
| Teks redup/meta | `--muted` | `#5d7268` | **4.57:1** | AA |
| Tautan & aksen jade | `--jade` | `#1a6f59` | **5.37:1** | AA |
| Aksen emas | `--gold` | `#8a6420` | **4.74:1** | AA |
| Status ok | `--ok` | `#1e7a4c` | **4.72:1** | AA |
| Status warn | `--warn` | `#8f5f14` | **4.87:1** | AA |
| Status bad | `--bad` | `#b23a34` | **5.24:1** | AA |

### Keterbedaan tiga status (CIE76 ΔE)

Rasio kontras **tidak** dipakai untuk mengukur “apakah ok vs warn bisa dibedakan”
— dua warna terang berbeda hue bisa punya rasio luminans ≈ 1.0 padahal mata jelas
membedakannya. Metrik yang benar adalah jarak perseptual di ruang Lab.

| Pasangan | Mode gelap | Mode terang | Ambang |
|---|---|---|---|
| ok vs warn | ΔE **61.4** | ΔE **58.8** | ≥ 20 |
| warn vs bad | ΔE **50.3** | ΔE **38.4** | ≥ 20 |
| ok vs bad | ΔE **95.3** | ΔE **87.1** | ≥ 20 |

Ambang ΔE ≥ 20 dipakai karena di bawah itu dua warna mulai terbaca “sama” pada
panel murah di ruang terang.

---

## 4. Kelas yang tersedia (15 kelas, dipakai semantik)

| Kelas | Untuk apa | Contoh pemakaian |
|---|---|---|
| `card` | Kotak informasi berdiri sendiri | ringkasan per villa |
| `badge` + `badge-ok` / `badge-warn` / `badge-bad` | Status pendek, satu kata | `CONFIRMED`, `PENDING`, `OVERDUE` |
| `banner` | Peringatan selebar halaman | pratinjau mode demo |
| `empty` | “tidak ada data” — bukan galat | daftar approval kosong |
| `hint` | Penjelas kecil di bawah kolom | “waktu lokal properti” |
| `lede` | Paragraf pembuka halaman | ringkasan di Command Center |
| `muted` | Teks sekunder | keterangan tambahan |
| `tag` | Label kategori di dalam baris | nama properti di sel tabel |
| `principal` + `role-pill` | Identitas & peran yang sedang login | kepala halaman |
| `skip-link` | Lompat ke konten (aksesibilitas) | tekan Tab di awal halaman |

Hanya **10 dari 14** template yang sudah memakai kelas di atas; `approvals.html`,
`audit.html`, `case_detail.html`, `command_center.html`, `inventory.html`,
`properties.html` masih telanjang. CSS-nya sudah siap — pekerjaan yang tersisa
adalah **menandai ulang halaman**, bukan menulis CSS baru.

---

## 5. Perilaku adaptif

| Kondisi | Yang terjadi |
|---|---|
| `prefers-color-scheme: light` | Seluruh token ditimpa; chip nav, badge, `empty`, `role-pill` diberi override eksplisit karena aslinya “kaca gelap”. |
| `prefers-reduced-motion: reduce` | Semua transisi dimatikan. |
| `max-width: 860px` | Nav membungkus, tabel bisa digulir mendatar, padding kanvas mengecil. |
| `@media print` | Latar putih, teks hitam, `th` tanpa gradien, tinggi baris padat — supaya laporan bisa dicetak untuk owner. |

**Aturan angka di tabel:** sel angka diberi `td.num` → `width: 1%` +
`white-space: nowrap`. Ini mencegah nomor invoice terpotong di tengah token
(`INV-202609-` / `0001`) ketika layar sempit, dan membuat kolom catatan
mendapat sisa ruang.

---

## 6. Cara menambah halaman baru

1. Buat `templates/<nama>.html` dengan struktur: `header` → `nav` → `main` → `footer`.
2. Jangan menulis CSS di dalam template. Bila butuh gaya baru, tambahkan kelas di
   lapisan 4/6 `base.html` **dan** tokennya di lapisan 1.
3. Semua nilai uang lewat `dashboard.money()` (IDR + USD otomatis, ADR-0070).
4. Jalankan `pytest tests/test_ui_contrast.py` — token baru yang gagal AA akan
   ditolak sebelum masuk.
5. Render: `PYTHONPATH=src .venv/bin/python -m dashboard --db <db> --dump <dir>`
   lalu buka `file://<dir>/<nama>.html`. Tidak perlu server.

---

## 7. Gerbang uji: `tests/test_ui_contrast.py` (5 tes, hijau)

| Tes | Yang dijamin |
|---|---|
| `test_dashboard_text_meets_wcag_aa[dark/light]` | 8 pasangan teks/latar ≥ 4.5:1 di kedua mode |
| `test_badge_colours_are_distinguishable[dark/light]` | tiga status berjarak ΔE ≥ 20 (kontrol positif: kalau semua disetel sama, tes gagal walau kontras lolos) |
| `test_contrast_maths_matches_wcag_reference` | rumus cocok acuan: hitam/putih = 21:1, `#767676`/putih ≈ 4.54:1 |

Token dibaca **langsung dari `base.html`**, jadi mengubah satu nilai warna tanpa
menguji ulang mustahil: tes membaca sumber kebenaran, bukan salinan.

```bash
cd ~/villa-agency-os && PYTHONPATH=src .venv/bin/python -m pytest tests/test_ui_contrast.py -q
# .....  5 passed
```

---

## 8. Cacat yang ditemukan lewat render nyata, lalu diperbaiki

Bukti: 15 halaman di-`--dump`, dibuka lewat `file://`, tangkapan layar penuh per
skema warna (CDP `Page.captureScreenshot`, `captureBeyondViewport`).

| # | Cacat | Penyebab | Perbaikan |
|---|---|---|---|
| 1 | Chip nav **tak terbaca** di mode terang | label `#2b3d35` di atas pill “kaca gelap” | override light: gradien `#ffffff→#f5f2e9`, teks `#23352c` |
| 2 | Nomor invoice **pecah** di tengah token | hyphen menjadi titik lipat | `td.num { width:1%; white-space:nowrap }` + `td.num + td { padding-left:18px }` |
| 3 | `th` sticky **menabrak** header sticky | dua elemen `position: sticky; top: 0` | sticky pada `th` dihapus |
| 4 | Tekstur anyam **terlalu ramai** di area kosong | alpha `0.055`/`0.05` | turun ke `0.038` (gelap) / `0.032` (terang) |
| 5 | Kontras mode terang **gagal AA** | jade 4.34 · gold 3.78 · tombol 3.71 | jade `#1a6f59` 5.37 · gold `#8a6420` 4.74 · tombol `#23806a→#1a6b57` 4.81→6.39 |

---

## 9. Yang belum selesai (jujur)

- 6 template belum memakai kelas semantik (`approvals`, `audit`, `case_detail`,
  `command_center`, `inventory`, `properties`) — CSS siap, penandaan belum.
- Halaman Workforce Ops/Payroll **belum punya template**; skema & ADR-0071 sudah
  ada, tampilan menyusul setelah penandaan ulang selesai.
- Verifikasi visual dilakukan pada **dump statis**, belum pada server hidup
  (`dashboard --serve`). Perilaku interaktif (sort, filter) belum diuji.
