# Gerbang G3 + G4 — Workforce Ops (T2.8)

**Tanggal:** 2026-11-15 · **Provenance:** run CI `43797100871` (cabang `gate/t2-8`, sebelum squash-merge)
· **Catatan provenance:** repo ini tidak mengizinkan commit merge (squash/rebase saja), jadi commit cabang tidak dipertahankan di riwayat; provenance yang tahan adalah run CI + laporan lokal — karena itu angka di sini diatribusikan ke run CI, bukan ke sha commit cabang yang akan hilang

Laporan ini mencatat apa yang **diukur**, bukan apa yang diharapkan. Setiap angka di bawah berasal
dari eksekusi pada commit di atas; perintah untuk mengulanginya ada di §5.

---

## 1. Putusan

| Gerbang | Syarat | Putusan |
|---|---|---|
| **G3** tampilan | kontras AA · uang IDR+USD 100% · peran terpisah · tanpa data nyata | **LULUS** — tiap butir dikunci uji (§2) |
| **G4** rilis | suite penuh hijau · PII gate lulus · dokumen status selaras · PR direview | **LULUS** — setelah **satu cacat gate dan empat angka salah diperbaiki** (§3, §4) |

Dua hal ditemukan **karena gerbangnya dijalankan, bukan karena dipercaya**:

1. `scripts/pii_scan.py` mencetak **PASS atas pohon yang tidak pernah dibaca** (0 berkas dipindai, RC=0).
2. Empat angka di `docs/SPRINT_WORKFORCE_OPS_2_WEEKS.md` tidak sama dengan kenyataan.

Keduanya sudah diperbaiki dan dikunci uji. Tanpa menjalankan gate di luar jalur normalnya, keduanya
akan lolos ke rilis sebagai "hijau".

---

## 2. G3 — bukti per butir

| Syarat G3 | Angka nyata (terukur) | Pengunci |
|---|---|---|
| Kontras AA | **80 pasangan diukur, 80 lulus**; terlemah **4,59:1** (`workforce_finance` h=240, kartu gelap `#101917`); kontrol negatif kuning-terang di putih **1,83:1** | `tests/test_ui_contrast.py`, `tests/test_dashboard_bale_ui.py` (13 uji) |
| Uang IDR + USD 100% | setiap angka rupiah membawa referensi dolar; tanpa kurs terbit → dolar **hilang** dan layar mengatakannya; kurs basi → sama | `test_every_rupiah_figure_carries_a_dollar_reference`, `test_without_a_published_rate_the_rupiah_stays_and_the_dollar_goes`, `test_a_stale_rate_drops_the_dollar_figure_and_says_so` |
| Peran terpisah | `/workforce/today` → `operations` · `/workforce/payroll` → `finance` · `/workforce/cost` → `owner`; **200** hanya pemiliknya, **403** peran lain, **401** tanpa identitas, **404** menyebut pilihan | `tests/test_workforce_views.py` (20 uji) + matriks HTTP `tests/test_dashboard.py` (231 uji) |
| Tanpa data nyata | staf `Ops Staff 01..12` (dibangkitkan `scripts/workforce_reconcile.py`); `scan_stored_pii.py --db data/villa.db` → **7 kolom diperiksa, 0 baris** membawa P3/P4; gerbang bahasa → **0 istilah Indonesia di 10 berkas** `src/workforce/` + katalog **9** label; layar supervisor → **0 istilah mesin** | `scripts/scan_stored_pii.py`, `scripts/check_workforce_language.py`, `test_the_supervisor_screen_shows_no_machine_words` |

---

## 3. G4 — cacat gate yang ditemukan dan diperbaiki

### 3.1 Gerbang PII melaporkan PASS atas pohon yang tidak dibaca

Diuji dengan mengarahkan gate ke direktori yang **tidak ada**:

```
sebelum:  PII-SCAN: PASS (0 findings, 0 text files scanned)          RC=0
sesudah:  PII-SCAN: FAIL — root is not a directory: .../tidak-ada
          PII-SCAN: FAIL — refusing to PASS a tree that was never read.   RC=1
```

Kelas kegagalan ini tidak bisa dideteksi oleh gate PII sendiri: satu-satunya kalimat yang bisa
dikeluarkannya adalah "tidak ada temuan", dan kalimat itu bisa keluar tanpa membaca apa pun.
Jalur normal kini juga menyebut **berapa berkas yang benar-benar dibaca** (424 berkas teks pada
run CI `43797100871`), sehingga "0 findings" tidak bisa lagi berarti "0 dibaca".

Perilaku baru, dan hasil ujinya (`tests/test_pii_gate.py`, 8 uji):

| Kasus | Harapan | Hasil |
|---|---|---|
| root tidak ada | FAIL, RC=1 | RC=1 ✓ |
| root berupa berkas | FAIL, RC=1 | RC=1 ✓ |
| pohon tanpa berkas teks (biner saja) | FAIL, RC=1 | RC=1 ✓ |
| surel + seluler + kartu Luhn-valid (bukan PAN allowlist) | 3 jenis dilaporkan | 3/3 ✓ |
| hanya nilai sintetis (`budi@villa.id`, `081234567890`) | PASS | RC=0 ✓ |
| pohon repo ini | PASS + jumlah berkas dibaca | RC=0, 424 berkas ✓ |

Catatan kejujuran: **gate ini menangkap berkas ujinya sendiri** — draf pertama `tests/test_pii_gate.py`
memuat nomor seluler literal, dan pemindaian repo langsung gagal. Nilai tanamnya sekarang dibangun
saat runtime.

### 3.2 Empat angka dokumen tidak benar

Diukur dengan `tests/test_sprint_doc_claims.py` (collection nyata per berkas):

| Klaim lama | Kenyataan | Perbaikan |
|---|---|---|
| T2.1: "15 uji pada `tests/test_workforce_payroll_calc.py`" | berkas itu **35 uji** (15 kebijakan + 20 kalkulasi) | angka 15 dilepas dari nama berkas |
| T2.6: "14 uji pada `tests/test_workforce_views.py`" (dua tempat) | **20 uji** setelah T2.6b | ditulis **20**, riwayat 14 disebut sebagai sejarah |
| §6: `make check` → "4297 passed" | **4445 passed** pada `6b08a9b` | angka + atribusi commit |

### 3.3 Pengunci angka dokumen — dan versi pertamanya yang salah

`tests/test_sprint_doc_claims.py` menegakkan dua aturan: (a) setiap angka "N uji" sama dengan hasil
collection nyata; (b) setiap klaim total "N passed" menyebut commit tempat ia diukur, dan commit itu
ada di riwayat. Ia juga gagal kalau jumlah klaim yang ditemukan turun di bawah 8 — supaya dokumen
tidak bisa "lulus" dengan cara menghapus klaimnya.

**Versi pertama detektornya salah** (jendela 60 karakter): ia membaca **kutipan angka lama** sebagai
klaim status hari ini — empat positif palsu, semuanya dari kalimat jujur, termasuk tabel yang sedang
melaporkan angka salah. Yang diperbaiki detektornya (klaim harus menempel, jarak ≤12 karakter, dua
arah), bukan dokumennya. Setelah diperketat, tiga sabotase membuktikan penguncinya masih hidup:

| Sabotase | Hasil nyata |
|---|---|
| angka uji dirusak (`20` → `99`) | RC=1, 1 uji gagal |
| total tanpa menyebut commit | RC=1, 1 uji gagal |
| total menyebut commit yang tidak ada (sha palsu tujuh huruf (contoh sabotase, bukan rujukan)) | RC=1, 1 uji gagal |

Dokumen dipulihkan dengan **sha256 identik** sesudah ketiga sabotase; sesudah dipulihkan RC=0, 0 gagal.

**Cacat ketiga, ditemukan CI — pada pengunci itu sendiri.** Ketiga job gate gagal dengan
`commit 6b08a9b tidak ada di riwayat`. Sebabnya bukan dokumen: `actions/checkout@v7` memakai clone
dangkal (`fetch-depth: 1`), jadi commit yang dikutip tidak ada di objek lokal — dan penguncinya
membaca "tidak terlihat dari sini" sebagai "tidak ada di riwayat". Itu tuduhan palsu kepada dokumen
yang jujur. Perbaikan: workflow memakai `fetch-depth: 0`; penguncinya membedakan dua keadaan dan
**skip dengan alasan** di clone dangkal (tanpa pernah menuduh). `git fetch origin <sha>` sengaja tidak
dipakai sebagai bukti: server boleh menolak sha yang valid.

| Kontrol | Hasil |
|---|---|
| clone dangkal (`--depth=1`) | `1 skipped` dengan alasan tertulis, **bukan** gagal |
| clone penuh (bersih, setara CI) | `2 passed` |
| clone penuh + commit palsu (sha palsu tujuh huruf (contoh sabotase, bukan rujukan)) | RC=1, `1 failed` |
| dokumen dipulihkan | sha256 identik, RC=0 |


### 3.4 Cacat susunan dokumen

§4.5 (T2.5) berada **setelah §12 Riwayat perubahan**, bukan di antara §4.4 dan §4.6. Dipindahkan ke
tempatnya; urutan §4.1–§4.8 sekarang benar dan isi tidak hilang (jumlah baris berisi sebelum/sesudah
sama).

### 3.5 Dokumen status lain yang ikut diselaraskan

`docs/STATUS_AND_GAP_MAP.md` masih menyebut keadaan `f5665c5` sebagai baris status utama
(4.099 tes lulus, pemindai 344 berkas). Baris itu kini membawa angka terukur terbaru **dengan
atribusi commit**, sementara snapshot lama tetap ada sebagai riwayat:

- suite: **4.455 passed, 7 skipped, 4.176 subtests** pada run CI `43797100871`;
- pemindai: `SECRET-SCAN`/`UNSAFE-SCAN` → **450 berkas**, `PII-SCAN` → **424 berkas teks**;
- jumlah ADR tetap **72 seluruhnya ACCEPTED** — dijaga uji yang sudah ada,
  `tests/test_adr_status.py::test_living_documents_state_the_real_number_of_adrs`.

Semua sha yang dikutip dokumen (`15` nilai) diperiksa: **semuanya ada di riwayat** (satu nilai yang
tampak seperti sha adalah nomor run CI `43558380150`, bukan commit).

---

## 4. G4 — bukti gerbang rilis pada commit run CI `43797100871`

| Langkah | Perintah | Hasil nyata |
|---|---|---|
| Suite penuh + semua pemindai (lokal) | `make check` | **4455 passed, 7 skipped, 4176 subtests**, 408,10 s, `MAKE_RC=0`, **PRE-DEPLOY GATE: PASS** |
| Suite penuh di CI (3 interpreter) | `make check` di CI | **4454 passed, 8 skipped** — selisih 1 uji = pembaca store yang butuh `data/villa.db`, di-skip di clone bersih (bukan kegagalan) |
| Pemindai rahasia | `bash scripts/secret_scan.sh` | `PASS (0 findings, 450 files scanned)` |
| Pemindai konfigurasi tidak aman | `bash scripts/unsafe_config_scan.sh` | `PASS (0 findings, 450 files scanned)` |
| Gerbang PII | `/usr/bin/python3 scripts/pii_scan.py` | `PASS (0 findings, 424 text files scanned)` |
| PII tersimpan di DB | `/usr/bin/python3 scripts/scan_stored_pii.py --db data/villa.db` | **0 baris** membawa P3/P4 (7 kolom) |
| Uji baru | `pytest tests/test_pii_gate.py tests/test_sprint_doc_claims.py` | **10 passed** |

Perbandingan dengan baseline pra-T2.8 (`6b08a9b`, T2.7): 4445 → **4455** uji (+10: 8 gerbang PII +
2 pengunci dokumen), waktu 410,38 → 408,10 s.

---

## 5. Cara mengulang

```bash
cd ~/villa-agency-os
# commit yang memuat gerbang ini ada di master sesudah squash-merge PR #34:
git checkout master && git log --oneline -3
make check                                        # 4455 passed, PRE-DEPLOY GATE: PASS
/usr/bin/python3 scripts/pii_scan.py              # PASS + jumlah berkas dibaca
/usr/bin/python3 scripts/scan_stored_pii.py --db data/villa.db
pytest tests/test_pii_gate.py -q
pytest tests/test_sprint_doc_claims.py -q
```

---

## 6. Batas yang diakui (tidak diklaim lebih dari ini)

1. **Tidak ada uji visual.** Kontras dihitung dengan rumus WCAG yang sama dengan yang dipakai CSS,
   bukan dari piksel hasil render. Tidak ada pembanding screenshot.
2. **Gate PII memindai berkas teks saja.** Isi berkas biner (PDF/gambar yang di-commit), nama,
   alamat, dan tanggal lahir **bukan** cakupannya — dinyatakan oleh skripnya sendiri, bukan
   disimpulkan pembaca.
3. **Pengunci angka dokumen mengaudit atribusi, bukan menjalankan ulang suite.** Ia membuktikan
   klaim total menyebut commit yang ada; ia tidak membuktikan angka totalnya dengan menjalankan
   4455 uji di dalam uji.
4. **Angka pemindai berubah setiap berkas ditambah/dihapus.** Yang dikunci adalah perilaku gate
   (fail-closed, mendeteksi, tidak menghalangi nilai sintetis), bukan jumlah berkas.
6. **Angka total bergantung lingkungan.** 4455/7 di lokal (ada `data/villa.db`) vs 4454/8 di CI
   (uji store di-skip). Tidak ada "satu angka benar" untuk keduanya; setiap angka di laporan ini
   menyebut lingkungannya.
7. **Repo ini tidak mengizinkan commit merge.** Karena itu commit cabang tidak dipertahankan dan
   provenance angka di sini adalah run CI, bukan sha yang hanya hidup di cabang. Ini keterbatasan
   nyata, bukan pilihan: atribusi ke commit yang tidak bertahan adalah atribusi yang tidak bisa
   diperiksa orang lain.
5. **Verifikasi G3 dilakukan terhadap kode dan uji, bukan terhadap perangkat mata.** Halaman
   dianggap memenuhi butir G3 hanya sejauh uji yang ada menutupnya; butir yang tidak punya uji
   pengunci akan lolos tanpa jejak.
