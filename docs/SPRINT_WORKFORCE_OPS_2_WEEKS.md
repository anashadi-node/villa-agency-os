# Sprint Workforce Ops — 2 minggu (10 hari kerja)

**Status:** AKTIF — disetujui Mas Anas 2026-11-14 · **ADR acuan:** [ADR-0071](adr/0071-workforce-ops-and-the-payroll-boundary.md) (ACCEPTED)
**Basis skema:** `src/schema/018_workforce_ops.sql` (16 tabel, 2 trigger) · `src/schema/001_initial.sql` (gabungan, kembar Postgres `001_initial.pg.sql`)
**Kontrak uji:** `tests/test_workforce_ops.py` — **32 tes, hijau** (dijalankan 2026-11-14)

---

## 0. Satu paragraf ringkas

Sprint ini tidak menambah fitur baru ke otak sistem; ia **menutup jarak antara "bentuk data sudah benar" dan "kerja benar-benar tercatat, upah benar-benar bisa dipertanggungjawabkan"**. Skema Workforce Ops sudah ada dan sudah mengunci garis ADR-0071 di level database (kerja = data operasional, upah = ledger terpisah), tetapi **belum ada satu pun penulis (writer) yang mengisinya**. Jadi selama 10 hari kerja ke depan kita membangun tiga hal, berurutan, dengan gerbang di antara: **(1) jalur tulis kerja** (roster → absensi → timesheet → assignment tugas), **(2) kalkulator upah deterministik** yang tidak boleh menghitung sendiri (semua rumus dari *policy version* + *pay rate* berversi, dihitung Decimal, disegel *lock hash*), dan **(3) UI premium** yang menampilkan kerja & upah sesuai peran — supervisor melihat jam, finance melihat uang, owner melihat settlement. Yang **tidak** dikerjakan sprint ini disebut eksplisit di §7 supaya tidak ada klaim berlebih.

---

## 1. Kondisi awal yang terukur (baseline, bukan asumsi)

| # | Fakta terukur | Nilai | Cara ukur (dapat diulang) |
|---|---|---|---|
| B1 | Tabel Workforce Ops di skema | **16** | `grep -c 'CREATE TABLE' src/schema/018_workforce_ops.sql` |
| B2 | Trigger penjaga (append-only / segel) | **2** | `grep -c 'CREATE TRIGGER' src/schema/018_workforce_ops.sql` |
| B3 | Kontrak uji skema | **32 lulus, 0 gagal** | `.venv/bin/python -m pytest tests/test_workforce_ops.py -q` |
| B4 | Paritas SQLite ↔ Postgres | **OK** | `.venv/bin/python scripts/generate_pg_twin.py --check` |
| B5 | Penulis data (writer) di `src/` | **0** | `grep -rl 'workforce_' src/*.py` → kosong |
| B6 | Kalkulator upah deterministik | **0** | tidak ada modul payroll di `src/` |
| B7 | Layar Workforce di dashboard | **0** | tidak ada rute/templat workforce |
| B8 | Data upah nyata di repo/publik | **0** | gate PII fail-closed; semua fixture sintetis |

**Bacaan jujur atas baseline:** yang kita punya adalah **kontrak**, bukan **sistem berjalan**. B3 hijau berarti "bentuknya tidak bisa dilonggarkan tanpa ketahuan", **bukan** berarti "payroll sudah jalan". Ini penting supaya gerbang di §5 tidak dilewati dengan alasan "tes sudah hijau".

### 1.1 Cacat yang sudah ditemukan **dan sudah diperbaiki** pada 2026-11-14

Ditemukan lewat uji, bukan lewat dugaan. Ini contoh persis jenis kesalahan yang sprint ini ada untuk mencegah.

| Cacat | Mengapa berbahaya | Bukti | Perbaikan |
|---|---|---|---|
| Alasan wajib (`VOID`, `SWAPPED/CANCELLED/NO_SHOW`, `REJECTED`, `DROPPED`, `eligible`) ditulis `length(x) >= 3` | Di SQL, `length(NULL)` = `NULL`, dan `NULL` **bukan** `FALSE` → CHECK **lolos** saat alasan tidak diisi. Aturan "wajib beralasan" bisa dilanggar dengan mengosongkan kolom. | Uji `test_void_run_requires_a_reason` gagal: insert `status='VOID'` dengan `void_reason=NULL` **diterima** | Kelima constraint diubah menjadi `x IS NOT NULL AND length(x) >= 3` di `018` **dan** `001` |

**Pelajaran yang masuk ke disiplin sprint:** di SQL, "tidak diisi" bukan "salah". Setiap CHECK yang memakai `length()` atau perbandingan apa pun **wajib** menyebut `IS NOT NULL` lebih dulu. Uji regresi untuk pola ini wajib ada sebelum sprint ditutup (§4, T1.6).

---

## 2. Tujuan sprint (3 hasil, bukan 12)

| Hasil | Definisi selesai yang bisa diuji | Metrik |
|---|---|---|
| **H1 — Kerja tercatat** | Setiap hari kerja staf punya roster + event absensi berstempel waktu, dan setiap klaim jam punya timesheet yang bisa ditutup jadi `APPROVED` dengan `lock_hash` | Cakupan absensi = (hari-staf dengan event masuk **dan** keluar) ÷ (hari-staf terjadwal) ≥ **98%** pada data uji 30 hari |
| **H2 — Upah bisa dipertanggungjawabkan** | Setiap baris slip gaji bisa ditelusuri ke (pay rate berversi, policy version, timesheet `APPROVED`) dan aritmetikanya lolos segel | Selisih rekonsiliasi = **0** baris tanpa jejak; `lock_hash` mismatch = **0** |
| **H3 — Tampilan premium & sesuai peran** | Supervisor, finance, dan owner melihat layar berbeda dengan angka yang sama; nilai uang IDR + USD | Audit kontras WCAG AA lulus; 0 angka uang tanpa pasangan USD; 0 istilah teknis di layar supervisor |

**Non-tujuan (tegas):** sprint ini **tidak** menaikkan harga, tidak mengubah target bisnis, tidak menyentuh Reservation Kernel, tidak mengubah ADR-0004/0010/0032/0068/0069/0070, dan **tidak** menjalankan payroll nyata (uang keluar) tanpa persetujuan tertulis owner per-run.

---

## 3. Hipotesis kerja & cara memfalsifikasinya

Sprint ditulis sebagai hipotesis yang bisa salah, bukan rencana yang pasti benar.

| # | Hipotesis | Akan salah jika… | Konsekuensi bila salah |
|---|---|---|---|
| H-1 | Skema 018 cukup untuk 4 alur inti (roster, absensi, timesheet, payroll) tanpa tabel baru | Saat menulis writer, muncul kebutuhan kolom/tabel ke-17 yang tidak bisa dipetakan ke yang ada | Stop, tulis ADR revisi 0071 §skema, jangan tempel kolom "sementara" |
| H-2 | Absensi bisa masuk lewat satu jalur (check-in/out) tanpa integrasi mesin fingerprint di Fase 1 | ≥20% staf tidak punya ponsel/akses, atau owner menuntut integrasi perangkat | Turunkan target cakupan ke input supervisor, catat sebagai keterbatasan terukur |
| H-3 | Rumus upah cukup dari policy version + pay rate (tanpa mesin aturan umum) | Muncul ≥3 kasus yang butuh cabang aturan baru di luar policy | Hentikan penambahan cabang; naikkan ke keputusan owner (bukan ditambal di kode) |
| H-4 | Tema premium bisa dicapai tanpa framework CSS baru | Waktu desain >2 hari atau ukuran CSS >120 KB | Potong dekorasi, pertahankan tipografi + kontras + motif |
| H-5 | Angka upah tidak boleh dihitung LLM | (tidak bisa salah — ini aturan, bukan hipotesis) | — |

**Aturan main:** satu perubahan → ukur → baru lanjut. Tidak ada dua perubahan sekaligus pada jalur kritis (bottleneck). Bila throughput total tidak naik, perubahan dibatalkan, bukan dipertahankan karena "kelihatan modern".

---

## 4. Rencana 10 hari kerja (WBS)

Setiap tugas punya **pemilik peran**, **artefak**, **DoD**, dan **perintah bukti**. Tidak ada tugas tanpa perintah bukti.

### Minggu 1 — jalur tulis kerja (H1)

| ID | Hari | Tugas | Artefak | DoD (bisa diuji) | Bukti |
|---|---|---|---|---|---|
| T1.1 | 1 | `src/workforce/repository.py` — koneksi + transaksi + `idempotency_key` untuk roster & absensi | modul + `tests/test_workforce_repository.py` | insert ganda dengan kunci sama → 1 baris, bukan 2 | `pytest -q tests/test_workforce_repository.py` |
| T1.2 | 1–2 | `src/workforce/roster.py` — terbitkan roster dari shift template, deteksi bentrok (staf sama, jam tumpang tindih) | modul + uji | dua shift tumpang tindih untuk staf sama ditolak; bentrok lintas properti ditolak | uji + `EXPLAIN QUERY PLAN` indeks terpakai |
| T1.3 | 2–3 | `src/workforce/attendance.py` — check-in/out, koreksi oleh supervisor (append-only, alasan wajib) | modul + uji | koreksi **tidak** menimpa event lama; hanya menambah event + merujuk `supersedes_id` | uji trigger + hitung baris sebelum/sesudah |
| T1.4 | 3–4 | `src/workforce/timesheet.py` — tutup periode: `regular_minutes`, `overtime_minutes`, `break_minutes` dari event, tolak ≥2400 menit/periode | modul + uji | timesheet `APPROVED` wajib `submitted_at` + `approved_by` + `lock_hash`; ubah setelah approve ditolak | uji + verifikasi CHECK |
| T1.5 | 4 | `src/workforce/tasks.py` — tautkan `ops_tasks` ke staf (assignment, `DROPPED` wajib alasan) | modul + uji | tugas tanpa staf aktif ditolak; `DROPPED` tanpa alasan ditolak | uji |
| T1.6 | 5 | Uji regresi "NULL bukan FALSE" untuk **semua** CHECK beralasan di 018 + 001 | `tests/test_workforce_null_semantics.py` | ≥6 kasus, semuanya menolak `NULL` | `pytest -q tests/test_workforce_null_semantics.py` |
| T1.7 | 5 | **Gate G1**: jalur tulis lulus di SQLite **dan** Postgres (kontainer), paritas skema OK | log gerbang | 0 gagal; `--check` OK; jalur tulis idempoten terbukti | `make check` + `generate_pg_twin.py --check` |

### Minggu 2 — upah deterministik (H2) + tampilan (H3)

| ID | Hari | Tugas | Artefak | DoD (bisa diuji) | Bukti |
|---|---|---|---|---|---|
| T2.1 | 6 | `src/workforce/payroll_policy.py` — muat `workforce_policy_versions` + `workforce_pay_rates` berversi; **tidak ada** angka pajak/BPJS di kode | modul + uji | ubah policy = versi baru; slip lama **tidak** berubah | uji: hitung dua kali dengan policy v1, lalu v2 |
| T2.2 | 6–7 | `src/workforce/payroll_calc.py` — Decimal penuh, `sanity` fail-closed (upah bersih ≥0, lembur ≤18 j/minggu, lembur ≤4 j/hari) | modul + uji | lembur 4,5 j/hari atau 19 j/minggu → ditolak, bukan dibulatkan | uji batas (4/4,5/18/19) |
| T2.3 | 7 | `src/workforce/payslip.py` — tulis `workforce_payslips` + `workforce_payslip_lines`, segel `lock_hash` (sha256 kanonik) | modul + uji | mengubah 1 baris setelah segel → terdeteksi | uji: ubah `gross`, hitung ulang hash, bandingkan |
| T2.4 | 7–8 | Alur status `DRAFT → CALCULATED → APPROVED → PAID → (VOID)`; `PAID` wajib `calculated_at` + `approved_at` | uji alur | lompat status ditolak; `VOID` tanpa alasan ditolak (regresi §1.1) | uji tabel transisi |
| T2.5 | 8 | **Gate G2**: rekonsiliasi 0 selisih pada dataset uji 30 hari × 12 staf sintetis | laporan angka | setiap slip punya jejak rate+policy+timesheet; 0 slip tanpa jejak | skrip `scripts/workforce_reconcile.py` (dibuat hari itu) |
| T2.6 | 8–9 | `src/workforce/views.py` — layar per peran (supervisor/finance/owner), uang IDR + USD dari `money_display` | templat + uji render | 0 istilah teknis di layar supervisor; 0 nilai uang tanpa USD | uji render + grep templat |
| T2.7 | 9 | Tema premium: token warna, tipografi, motif etnik-alam, kontras AA, `prefers-reduced-motion` | `src/static/theme.css` + uji kontras | ≥8 pasangan warna lulus AA; animasi mati saat reduced-motion | skrip uji kontras (dibuat hari itu) |
| T2.8 | 10 | **Gate G3 + G4**: suite penuh hijau, `make check`, audit PII, dokumen status diperbarui | log + PR | 0 gagal; 0 PII; angka status cocok kenyataan | `pytest -q`, `make check`, `git diff --stat` |

**Beban:** ±6,5 hari untuk jalur kerja, ±3 hari untuk upah, ±2 hari untuk tampilan, ±1 hari buffer. Buffer **tidak** dipakai untuk menambah fitur.

---

## 5. Gerbang (tidak boleh dilewati, tidak boleh "nanti")

| Gerbang | Syarat lulus | Bila gagal |
|---|---|---|
| **G0** — sebelum kode | ADR-0071 ACCEPTED, baseline §1 terukur, kamus istilah (kerja/upah/shift/lembur) disepakati | Jangan mulai; perbaiki dokumen dulu |
| **G1** — jalur tulis | T1.1–T1.6 hijau, idempotensi terbukti, paritas skema OK | Stop minggu 2; perbaiki jalur tulis |
| **G2** — upah | Rekonsiliasi 0 selisih, segel teruji, batas lembur teruji | **Uang tidak boleh dihitung** sebelum lulus |
| **G3** — tampilan | Kontras AA, uang IDR+USD 100%, peran terpisah, tanpa data nyata | Tampilan tidak dianggap selesai |
| **G4** — rilis | Suite penuh hijau, PII gate lulus, dokumen status selaras, PR direview | Tidak di-merge |

**Aturan keras:** `PAID` dan transfer uang **di luar** sprint ini. Sprint berhenti di `APPROVED` + laporan siap bayar. Menjalankan `PAID` butuh persetujuan owner per-run, tertulis, setelah G2 lulus.

---

## 6. Metrik: definisi, rumus, dan cara mengukur

Semua metrik dihitung program, bukan dibaca mata. Tanpa angka dari program → bukan klaim.

| Metrik | Rumus | Ambang | Alat |
|---|---|---|---|
| Cakupan absensi | hari-staf (masuk ∧ keluar) ÷ hari-staf terjadwal | ≥98% | `scripts/workforce_reconcile.py` |
| Ketepatan timesheet | timesheet `APPROVED` dgn segel sah ÷ total `APPROVED` | 100% | idem |
| Slip tanpa jejak | slip tanpa (rate ∧ policy ∧ timesheet) | 0 | idem |
| Selisih aritmetika | Σ\|bruto − Σbaris\| dan Σ\|net − (bruto − potongan)\| | 0 rupiah | idem |
| Pelanggaran batas lembur | kejadian >4 j/hari atau >18 j/minggu | 0 | `payroll_calc` uji batas |
| Kontras tampilan | pasangan warna gagal AA | 0 | skrip kontras di T2.7 |
| Kebocoran PII | baris PII nyata di tree publik | 0 | gerbang PII fail-closed |
| Waktu layar | p95 render halaman workforce (12 staf, 30 hari) | ≤300 ms lokal | pengukuran manual 5× |

---

## 7. Di luar cakupan sprint (disebut supaya tidak diklaim)

1. **Integrasi mesin absensi fisik / fingerprint** — butuh keputusan perangkat + biaya.
2. **Transfer bank otomatis / disbursement** — di luar mandat; butuh keputusan owner + bank.
3. **PPh 21 final & e-Bupot** — sprint hanya menyiapkan **angka**; pelaporan resmi ke DJP bukan scope.
4. **Multi-negara / multi-mata uang pajak** — hanya Indonesia (Bali, Yogyakarta).
5. **Penjadwalan otomatis (auto-roster)** — optimasi setelah data 60 hari terkumpul.
6. **Aplikasi mobile staf** — layar web responsif dulu.
7. **Perhitungan pesangon/PHK** — butuh review hukum tersendiri.
8. **Payroll run produksi** — butuh persetujuan tertulis owner.

---

## 8. Risiko (dengan pemicu, bukan harapan)

| # | Risiko | Pemicu terdeteksi | Mitigasi | Pemilik |
|---|---|---|---|---|
| R1 | Data absensi tidak lengkap → upah salah | cakupan <95% dua hari berturut | koreksi supervisor wajib beralasan + laporan harian | Ops |
| R2 | Aturan pajak/BPJS berubah | policy version lama dipakai >masa berlaku | semua tarif dari policy berversi bertanggal | Finance |
| R3 | PII staf bocor ke tree publik | gerbang PII merah | data sintetis saja; `workforce_staff_private` terpisah | Security |
| R4 | Skema perlu diubah di tengah sprint | T1/T2 butuh kolom baru | ADR revisi dulu; larangan kolom "sementara" | Arsitek |
| R5 | Tampilan premium menambah berat halaman | p95 >300 ms | batasi CSS ≤120 KB, tanpa framework baru | Frontend |
| R6 | Sengketa upah tanpa bukti | ada slip tanpa segel/jejak | G2 wajib; slip tanpa jejak tidak boleh terbit | Finance |
| R7 | Scope creep ke payroll nyata | ada permintaan "sekalian bayar" | tolak, rujuk §5 aturan keras | Owner |

---

## 9. Lampiran hukum — status verifikasi (jujur, per 2026-11-14)

Angka yang **sudah** saya baca sendiri dari sumber primer (bukan blog, bukan rangkuman):

| Aturan | Isi yang terverifikasi | Sumber (dibaca 2026-11-14) |
|---|---|---|
| PP 35/2021 Pasal 26 ayat (1) | Lembur paling lama **4 jam/hari** dan **18 jam/minggu**; tidak termasuk lembur pada hari istirahat mingguan/libur resmi | `peraturan.bpk.go.id/Download/154582/PP Nomor 35 Tahun 2021.pdf` |
| PP 35/2021 (upah lembur) | Hari kerja: jam lembur pertama **1,5×** upah sejam, jam berikutnya **2×**; hari istirahat/libur resmi (6 hari kerja × 40 jam): 2×, 3×, 4× menurut jam ke- | idem |
| PP 35/2021 Pasal 21 | Waktu kerja **40 jam/minggu** (7 j × 6 hari atau 8 j × 5 hari) | idem |
| Perpres 82/2018 Pasal 31 ayat (1) | Iuran JKN peserta PPU **5%** dari gaji: **4% pemberi kerja**, **1% peserta** | `peraturan.bpk.go.id/Download/254897/Perpres Nomor 82 Tahun 2018.pdf` |
| PMK 168/2023 | PPh 21 memakai **tarif efektif (TER) bulanan** untuk masa pajak selain masa terakhir; biaya jabatan 5%, maks Rp6.000.000/tahun atau Rp500.000/bulan | `jdih.kemenkeu.go.id/.../2023pmkeuangan168.pdf` |

Angka yang **belum** saya verifikasi ke sumber primer dan **karena itu tidak boleh dipakai sebagai aturan final**: iuran JKK/JKM/JHT/JP (instrumen: PP 44/2015 dan PP 46/2015), tarif PPh 21 progresif tahunan Pasal 17, UMP/UMK Bali & DIY 2026, aturan THR. **Semua angka ini masuk sebagai isi `workforce_policy_versions` bertanggal, disahkan Finance + counsel sebelum gerbang G2.** Tidak ada satu pun yang di-hard-code di kode (red line #3 SOUL).

**Catatan:** sistem ini menjalankan aturan yang sudah disetujui manusia; ia **bukan** pengganti nasihat hukum/pajak.

---

## 10. Yang membuat sprint ini dinyatakan gagal (falsifikasi)

Sprint **gagal** — dan harus ditulis begitu dalam laporan — bila salah satu terjadi:

1. Ada slip gaji yang tidak bisa ditelusuri ke rate + policy + timesheet.
2. Ada selisih aritmetika ≠ 0 pada dataset uji.
3. Ada CHECK yang masih bisa dilanggar dengan `NULL` (regresi §1.1).
4. Tampilan menampilkan uang tanpa pasangan USD, atau mencampur peran.
5. Suite penuh tidak hijau saat G4, atau klaim "hijau" tanpa log.
6. Ada PII nyata masuk tree publik.
7. Ada `PAID` dijalankan tanpa persetujuan tertulis owner.

---

## 11. Empat hal yang butuh keputusan owner (bukan keputusan teknis)

| # | Pertanyaan | Mengapa butuh owner | Default bila dibiarkan |
|---|---|---|---|
| D1 | Periode payroll: bulanan tanggal berapa, potong-off absensi jam berapa? | Mengubah definisi "satu bulan kerja" | Usul: bulan kalender, cut-off 25 pukul 23:59 WITA/WIB per properti |
| D2 | Golongan jabatan tertentu yang dikecualikan dari upah lembur (PP 35/2021 Pasal 27 ayat (2)–(3)) | Berdampak langsung ke biaya & kepatuhan | Usul: tidak ada pengecualian di Fase 1 |
| D3 | Pembulatan uang: ke rupiah penuh terdekat, atau simpan desimal? | Mengubah angka yang dibayar | Usul: simpan desimal, bulatkan hanya saat tampil & transfer |
| D4 | Apakah supervisor boleh melihat nominal upah staf yang ia supervisi? | Batas privasi & ADR-0071 | Usul: **tidak** — supervisor melihat jam, bukan rupiah |

---

## 12. Riwayat perubahan

| Tanggal | Perubahan |
|---|---|
| 2026-11-14 | Dokumen dibuat; baseline §1 diukur; cacat "NULL lolos CHECK" ditemukan & diperbaiki; kontrak uji 32 hijau; paritas skema OK |
