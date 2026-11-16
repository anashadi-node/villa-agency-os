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

**Status pelaksanaan (2026-11-15):**

| ID | Status | Bukti |
|---|---|---|
| T1.1 | **Selesai** | `src/workforce/repository.py` + `tests/test_workforce_repository.py` (10 uji) |
| T1.2 | **Selesai** | `src/workforce/roster.py` + `tests/test_workforce_roster.py` (37 uji, termasuk uji rencana kueri `EXPLAIN QUERY PLAN`) |
| T1.3 | **Selesai** | `src/workforce/attendance.py` + `tests/test_workforce_attendance.py` (17 uji): koreksi menambah baris `MANUAL_ADJUST` bertanda `supersedes_event_id`, baris lama utuh, `UPDATE`/`DELETE` ditolak trigger |
| T1.4 | **Selesai** | `src/workforce/timesheet.py` + `tests/test_workforce_timesheet.py` (19 uji): menit dihitung dari event kehadiran, istirahat memotong, lembur hanya di luar shift terbit, punch menggantung menolak penutupan, `APPROVED` membekukan isi lewat `lock_hash` |
| T1.5 | **Selesai** | `src/workforce/tasks.py` + `tests/test_workforce_tasks.py` (28 uji): penugasan terikat identitas staf aktif, staf nonaktif/asing properti ditolak, `DROPPED` wajib alasan ≥3 karakter, penugasan `DONE` tidak bisa dibuka lagi |
| T1.6 | Selesai (sudah ada) | `tests/test_workforce_null_semantics.py` |
| T1.7 | **Selesai** (2026-11-15) | `tests/test_workforce_gate_g1.py` (13 uji: 10 di SQLite, 3 di PostgreSQL sungguhan) — lihat §4.1 |
| T2.1 | **Selesai** (2026-11-15) | `src/workforce/payroll_policy.py` + lima belas uji kebijakan (bagian dari **35 uji** di `tests/test_workforce_payroll_calc.py`): jendela `[from, to)` setengah terbuka, dua versi/dua tarif pada satu tanggal = `AMBIGUOUS` (bukan "terbaru menang"), checksum kanonik menolak payload yang diubah tanpa versi baru, tarif agensi lain tidak terlihat, dan **tidak ada** angka pajak/BPJS di kode |
| T2.2 | **Selesai** (2026-11-15) | `src/workforce/payroll_calc.py` + 20 uji: seluruh uang lewat `Decimal`, dibulatkan sekali per baris (`ROUND_HALF_UP`), batas lembur 240 mnt/hari & 1080 mnt/pekan ditolak saat dilanggar, gaji bulanan pada periode tidak penuh = `PRORATION_REQUIRED`, upah bersih negatif tidak pernah menjadi slip, setiap baris wajib `basis_note` + `policy_version_id` |
| T2.3 | **Selesai** (2026-11-15) | `src/workforce/payslip.py` + 30 uji pada `tests/test_workforce_payslip.py`: satu orang = satu slip per run (`PayslipExists`, bukan ditimpa), aritmetika dihitung ulang dari baris tersimpan, run `APPROVED/PAID/VOID` = `PayrollRunFrozen`, timesheet harus `APPROVED/LOCKED` milik staf yang sama, segel `PayrollCalculation.digest()` masuk `audit_events.evidence_ref` dan `verify_seal()` mendeteksi baris diubah/dihapus/total diubah |
| T2.4 | **Selesai** (2026-11-15) | `src/workforce/payslip.py` (`transition_run`, `RUN_TRANSITIONS`, `IllegalRunTransition`) + 19 uji pada `tests/test_workforce_payroll_run.py`: alur satu langkah `DRAFT→CALCULATED→APPROVED→PAID`, lompat/mundur ditolak, `PAID`/`VOID` final, `VOID` wajib alasan ≥3 karakter, `PAID` hanya bila `calculated_at`+`approved_at` terisi **dan** setiap slip masih cocok dengan segelnya, balapan status ditolak lewat `expected_status`, total dihitung ulang dari slip tersimpan, satu event audit `WORKFORCE_PAYROLL_RUN_STATUS_CHANGED` per transisi — lihat §4.4 |
| T2.5 | **Selesai** (2026-11-15) | **Gate G2** dijalankan oleh `scripts/workforce_reconcile.py` + 20 uji pada `tests/test_workforce_reconcile.py`: dataset sintetis 30 hari x 12 staf di dua properti (Bali `Asia/Makassar`, Yogyakarta `Asia/Jakarta`) disemai, rantai roster→punch→timesheet→tarif/policy→slip→run dijalankan, lalu **dihitung ulang dari tabel mentah oleh kode yang berbeda** — 9 tahap, **1.000 pemeriksaan, 0 selisih** (R1 180 · R2 72 · R3 144 · R4 108 · R5 18 · R6 427 · R7 1 · R8 50). Kontrol negatif membuktikan gerbangnya memang bisa gagal: menit timesheet dirusak → R1; tarif dirusak setelah slip terbit → R3; baris slip dirusak → R3+R4; tajuk slip dirapikan agar lolos CHECK constraint → R4+R5; event audit slip dihapus → R6; rantai hash disunting → R7; payload policy disunting di tempat → R8+R3; tarif kehilangan penanggung jawab → R8. Batas gerbangnya juga dikunci uji: tarif yang salah **tetapi** utuh dan ditandatangani tidak bisa dinilai oleh rekonsiliasi internal (butuh perbandingan dengan rate card yang disetujui owner). Ratchet bahasa `src/workforce/` juga dibuat (`scripts/check_workforce_language.py`, 0 temuan) dan keduanya masuk `make check` — lihat §4.5 |

Dua temuan saat T1.1–T1.2 dikerjakan, keduanya masuk ke kode dan tes:

1. `ck_workforce_roster_published` menuntut `published_by` untuk **semua** status selain DRAFT, termasuk `CANCELLED` — artinya membatalkan draft mustahil tanpa mengarang nama penerbit. Constraint diubah menjadi `status IN ('DRAFT', 'CANCELLED') OR (published_by IS NOT NULL AND published_at IS NOT NULL)`.
2. `publish_shift()` membaca jam dua kali, sehingga `published_at` dan `updated_at` bisa berbeda mikrodetik. Kini satu pembacaan per tindakan.

Tiga temuan saat T1.3 dikerjakan:

1. **Koreksi mewarisi orang dan shift-nya, tidak memilih.** `correct()` tidak menerima `staff_id`; ia mengambil staf dan `roster_entry_id` dari baris yang dikoreksi. Supervisi karena itu mustahil tanpa sengaja memindahkan jam kerja seseorang kepada orang lain — kesalahan yang paling mahal di penggajian.
2. **Kiriman ulang koreksi harus dikenali sebelum cek "sudah dikoreksi".** Urutan sebaliknya membuat kiriman identik ditolak `ALREADY_SUPERSEDED` karena koreksi pertama sudah menjadi pengganti baris target. Kini `idempotency_key` diperiksa lebih dulu di dalam transaksi yang sama.
3. **Satu baris dikoreksi paling banyak sekali, tetapi koreksi atas koreksi tetap sah.** Rantai koreksi harus lurus: dua pengganti untuk satu baris berarti dua "kebenaran" untuk detik yang sama.

Empat temuan saat T1.4 dikerjakan:

1. **`workforce_timesheets` tidak punya `created_at`/`updated_at`.** Kolom itu tidak ada di DDL yang sudah disetujui, jadi modul tidak mengarangnya: jejak waktu memakai `submitted_at` dan `approved_at` yang memang ada, dan setiap perubahan isi menaikkan `version`. Menambah kolom demi kenyamanan kode akan mengubah kontrak yang belum perlu diubah.
2. **`break_minutes` belum ada di skema.** Kolom ini dibutuhkan untuk memotong istirahat dari menit kerja, jadi ditambahkan ke `001_initial.sql` dan `018_workforce_ops.sql` (CHECK 0–1440) dan kembaran Postgres diregenerasi. Tanpa kolom ini, istirahat 30 menit akan tampil sebagai upah.
3. **`submitted_by`/`rejected_by` belum punya kolom.** Nilainya tetap divalidasi (tidak kosong, ≤64 karakter) tetapi tidak disimpan — menambah kolom audit baru harus lewat ADR, bukan lewat kode yang sedang ditulis. Dicatat sebagai utang Sprint 2 bila audit identitas pengaju dibutuhkan.
4. **Ambiguitas "lembur" diselesaikan dengan jadwal, bukan dengan tebakan.** Menit di luar shift terbit = lembur; menit tanpa shift sama sekali = reguler dan tetap dihitung di `unrostered_minutes` supaya terlihat, bukan disembunyikan. Periode ≥2400 menit (40 jam) ditolak dan naik ke manusia — bukan dipotong otomatis.

### 4.1 T1.7 — gerbang G1 dijalankan (2026-11-15)

T1.1–T1.5 menguji tiap modul sendiri-sendiri. Gerbang G1 menguji klaim gabungannya:
**empat penulis berbeda, di satu basis data, tidak pernah menghasilkan dua baris untuk
satu kunci.** Yang dijalankan:

| Bukti | Perintah | Hasil |
|---|---|---|
| Suite penuh + PII gate + twin `--check` | `make check` | `4445 passed, 7 skipped, 4176 subtests`, `PRE-DEPLOY GATE: PASS`, RC=0 — **diukur pada `6b08a9b`** |
| Gerbang G1 di SQLite | `.venv/bin/python -m pytest tests/test_workforce_gate_g1.py` | `10 passed` |
| Gerbang G1 di PostgreSQL sungguhan (kontainer `villa-pg-parity`) | `VILLA_PG_DSN=... pytest tests/test_workforce_gate_g1.py` | `13 passed` |
| Paritas + gerbang di satu sesi (persis perintah CI) | `pytest tests/test_schema.py tests/test_workforce_gate_g1.py` | `88 passed, 796 subtests passed` |

Tiga temuan saat gerbang dijalankan:

1. **Kunci yang dipakai ulang untuk isi berbeda ditelan sebagai "replay".** Kiriman
   ulang dengan `idempotency_key` sama tetapi `event_type`/`occurred_at_utc` berbeda
   mengembalikan baris lama seolah sukses — bukti yang sebenarnya dilaporkan perangkat
   hilang tanpa jejak. Kini `attendance.record()` membandingkan isi baris tersimpan
   dengan kiriman; beda isi → `WORKFORCE_ATTENDANCE_IDEMPOTENCY_MISMATCH` dengan daftar
   `differing_fields`. Kunci yang sama dengan isi yang sama tetap replay yang sah.
2. **Bukti idempotensi di SQLite bukan bukti untuk PostgreSQL.** `ON CONFLICT`, tipe,
   dan trigger `PL/pgSQL` adalah kode yang berbeda dari SQLite; menguji di satu dialek
   membuktikan mesin yang tidak dipakai produksi. Sisi PostgreSQL kini menerapkan twin
   ke *schema* privat `gate_g1`, lalu membuktikan di server: kunci ganda ditolak
   (`UniqueViolation`), bukti absensi menolak `UPDATE`/`DELETE`, dan lima kunci unik
   penulis ada di katalog. Job CI `postgres-parity` menjalankan **kedua** berkas tes.
3. **`make check` memakai `/usr/bin/python3`, yang tidak punya driver PostgreSQL.**
   Tanpa penanganan eksplisit, tes PG gagal di `import` — atau lebih buruk, dilewati
   diam-diam sehingga gerbang "hijau" tanpa pernah menyentuh server. Driver kini
   diselesaikan seperti `schema_loader.open_pg` (`psycopg` → `psycopg2`), dan bila
   tidak ada: **dilewati dengan alasan tercetak** (`-rs`), bukan lulus. Dengan
   `VILLA_PG_REQUIRED=1` ketiadaan DSN/driver = gagal keras, bukan lewat.

Sisa celah yang dicatat jujur (utang Sprint 2, bukan klaim selesai): penulis
(`attendance`, `timesheet`, `tasks`, `roster`) masih berjalan di SQLite; sisi
PostgreSQL membuktikan **jaminan basis data**-nya (kunci + trigger), bukan eksekusi
modul Python-nya. Menutup celah ini berarti adaptor dialek untuk lapisan SQL — ADR
tersendiri, bukan tambahan senyap di sprint ini.

### 4.2 T2.1–T2.2 — upah deterministik (2026-11-15)

Yang diuji `tests/test_workforce_payroll_calc.py` (35 uji) — semuanya memakai data
sintetis lewat SQL, tidak ada nominal upah orang sungguhan di kode:

| Klaim | Uji yang memfalsifikasi |
|---|---|
| Jendela berlaku setengah terbuka `[from, to)` | `test_policy_window_is_half_open_at_the_seam`, `test_rate_window_end_is_exclusive` |
| Dua versi/dua tarif pada satu tanggal **gagal keras** | `test_two_open_policy_versions_are_ambiguous_not_newest_wins`, `test_two_rates_for_one_component_on_one_date_are_ambiguous` |
| Payload yang diubah tanpa versi baru terdeteksi | `test_policy_checksum_rejects_payload_edit_without_new_version` (dan reformat JSON bukan tamper) |
| Orang tanpa tarif **bukan** upah nol | `test_person_without_any_rate_fails_instead_of_earning_zero` |
| Batas lembur 240 mnt/hari & 1080 mnt/pekan mengikat | `test_overtime_over_daily_upper_bound_is_refused`, `test_weekly_upper_bound_binds_longer_periods` |
| Pembulatan sekali, `ROUND_HALF_UP`, tanpa `float` | `test_hourly_amount_rounds_once_half_up` |
| Gaji bulanan pada periode tidak penuh → manusia, bukan prorata tebakan | `test_monthly_salary_on_partial_month_needs_a_human_decision` |
| Upah bersih negatif tidak pernah menjadi slip | `test_negative_net_pay_is_never_a_payslip` |

Empat temuan saat T2.1–T2.2 dikerjakan:

1. **`load_pay_rates` mengembalikan `{}` ketika tidak ada tarif, bukan melempar.** Itu
   perilaku yang benar untuk pembaca per-tarif, tetapi **berbahaya** kalau dipakai
   langsung: orang tanpa tarif akan dihitung sebagai nol. Karena itu `load_bundle`
   wajib melempar `PayRateNotFound`, dan tes mengunci keduanya (kamus kosong **dan**
   paket gagal). Uji awal saya menuntut `load_pay_rates` melempar — itu salah tulis
   tes, bukan salah kode.
2. **`DomainError` menyimpan konteks di `.details`, bukan `.context`.** Empat asersi
   awal memakai nama yang tidak ada; diperbaiki ke `.details` supaya uji mengunci
   kontrak primitif yang nyata, bukan nama karangan.
3. **Ambang bonus kehadiran wajib eksplisit di payload policy.** Menebak "0 hari
   absen" sebagai default berarti membayar/menahan bonus tanpa keputusan tertulis;
   tanpa `attendance_bonus_max_absence_days`, perhitungan berhenti dengan
   `PolicyPayloadInvalid`.
4. **Iuran pemberi kerja tidak boleh menyentuh upah bersih.** Uji awal saya
   mengharapkan bruto yang salah (memakai paket tanpa bonus); angka sebenarnya
   Rp5.800.000 bruto → Rp5.400.000 bersih dengan potongan Rp400.000, dan
   `BPJS_EMPLOYER` tampil di slip tanpa mengurangi net. Angka di tes diperbaiki ke
   hasil hitung nyata, bukan sebaliknya.

### 4.3 T2.3 — slip gaji sebagai segel, bukan pembayaran kedua (2026-11-15)

`tests/test_workforce_payslip.py` (30 uji yang dikumpulkan pytest) mengunci klaim berikut. Seluruh nominal
sintetis (`Kadek Uji`, angka bulat), tidak ada upah orang sungguhan.

| Klaim | Uji yang memfalsifikasi |
|---|---|
| Satu orang, satu run = satu slip; yang kedua **ditolak**, bukan menimpa | `test_second_slip_for_the_same_person_in_one_run_is_refused_not_overwritten` |
| Run yang sudah dibekukan tidak menerima slip baru | `test_frozen_run_refuses_new_slips` |
| Aritmetika dihitung ulang dari baris, bukan dipercaya dari payload | `test_gross_that_disagrees_with_its_lines_is_refused`, `test_net_that_is_not_gross_minus_deduction_is_refused` |
| Hitung tanpa baris / bersih nol tidak pernah menjadi slip | `test_calculation_without_lines_never_becomes_a_slip`, `test_zero_net_pay_never_becomes_a_slip` |
| Mata uang dan periode hitung harus sama dengan run | `test_calculation_currency_must_match_the_run`, `test_calculation_for_another_period_is_refused` |
| Timesheet wajib `APPROVED`/`LOCKED` dan milik staf yang sama | `test_timesheet_must_be_approved_before_it_is_paid`, `test_timesheet_of_another_person_cannot_pay_this_slip` |
| Segel hanya bertambah sekali, tercatat dengan aktornya | `test_seal_is_written_to_the_audit_chain_with_the_actor` |
| Segel yang utuh lolos, yang diubah **tidak** | `test_verify_seal_accepts_an_untouched_slip`, `test_verify_seal_detects_an_edited_line`, `test_verify_seal_detects_edited_payslip_totals`, `test_verify_seal_detects_a_dropped_line`, `test_slip_without_a_seal_is_an_error_not_a_pass` |
| Run dibuka idempoten dan meninggalkan **satu** jejak audit | `test_open_run_is_idempotent_for_the_same_period`, `test_opening_a_run_leaves_one_audit_event_for_the_opener` |
| Total run dihitung dari slip tersimpan, run kosong = 0 bukan `NULL` | `test_run_totals_sum_the_stored_slips_only`, `test_run_totals_of_an_empty_run_are_zero_not_null` |
| Run agensi lain tidak terlihat; run tak dikenal dilaporkan, bukan dibuat | `test_run_of_another_agency_is_invisible`, `test_unknown_run_is_reported_not_created_on_the_fly` |

Lima temuan saat T2.3 dikerjakan:

1. **`workforce_payslips` tidak punya kolom `lock_hash`.** Kolom itu milik
   `workforce_timesheets`. Alih-alih menambah kolom/migrasi baru, segel memakai
   `PayrollCalculation.digest()` yang ditulis ke `audit_events.evidence_ref` (rantai
   hash append-only yang sudah ada). Nol perubahan skema, nol risiko ke jalur booking;
   `verify_seal()` menghitung ulang digest dari baris tersimpan dan menolak slip yang
   berubah, kehilangan baris, atau tanpa segel.
2. **Slip adalah pembayaran kedua kalau dipaksa.** `uq_workforce_payslips_once`
   (run, staff) sudah ada di DDL, tetapi kode tetap harus menolak dengan pesan yang
   bisa dibaca operator (`PayslipExists`) sebelum menabrak constraint — constraint
   menjadi jaring terakhir, bukan pesan kesalahan pertama.
3. **Payload hitung tidak boleh dipercaya.** Bruto/potongan/bersih selalu dijumlah
   ulang dari baris yang akan disimpan; selisih sekecil satu minor unit menolak slip.
   Kalau tidak, dua angka benar bisa hidup berdampingan di satu slip.
4. **Membuka run yang sudah ada bukan peristiwa baru.** `open_run` idempoten dan
   hanya menulis satu `WORKFORCE_PAYROLL_RUN_OPENED`; run yang dipakai bersama tidak
   membanjiri rantai audit dengan event duplikat.
5. **ADR-0011 berlaku untuk modul baru.** Pesan galat T2.1–T2.3 semula berbahasa
   Indonesia dan ditolak `tests/test_language_standard.py`; seluruh pesan produksi
   diterjemahkan ke en-US (docstring tetap Bahasa Indonesia untuk pembaca manusia).

### 4.4 T2.4 — status run hanya bergerak satu langkah, lalu berhenti (2026-11-15)

`tests/test_workforce_payroll_run.py` (19 uji terkolaksi, termasuk 3 varian parametrik)
mengunci klaim berikut. Nama staf sintetis (`Kadek Uji`, `stf_2`), nominal bulat.

| Klaim | Uji yang memfalsifikasi |
|---|---|
| Alur normal menandai setiap langkah dengan waktu dan pelakunya | `test_run_walks_calculated_to_approved_to_paid_and_stamps_each_step` |
| Target berhuruf kecil dinormalkan, bukan diabaikan diam-diam | `test_lowercase_target_is_normalised_not_silently_ignored` |
| Lompat status atau mundur ditolak (3 varian: `PAID`, `DRAFT`, `CALCULATED`) | `test_skipping_or_reversing_a_status_is_refused` |
| Status tak dikenal ditolak sebelum menyentuh baris | `test_unknown_target_status_is_refused` |
| `PAID` dan `VOID` final — tidak ada jalan keluar | `test_paid_run_is_final_for_both_forward_and_void` |
| `VOID` wajib alasan; tanpa alasan ditolak dan status tidak berubah | `test_void_without_a_reason_is_refused` |
| `VOID` mempertahankan slip dan mencatat alasannya | `test_void_from_calculated_keeps_payslips_and_records_the_reason` |
| Run tanpa slip tidak pernah menjadi `CALCULATED` | `test_run_without_payslips_never_becomes_calculated` |
| Pembayaran menolak slip yang angkanya diubah setelah persetujuan | `test_payment_refuses_a_slip_whose_numbers_were_edited_after_approval` |
| Total run dihitung ulang dari slip tersimpan, bukan dari argumen pemanggil | `test_approval_recomputes_totals_from_stored_slips_not_from_the_caller` |
| Setiap transisi meninggalkan **satu** event audit dengan before/after, aktor, peran, alasan | `test_every_transition_leaves_one_audit_event_with_before_and_after` |
| Transisi yang ditolak tidak meninggalkan event audit apa pun | `test_refused_transition_leaves_no_audit_event` |
| Aktor wajib; tanpa aktor `MISSING_FIELD` dan status tetap | `test_actor_is_required_for_every_transition` |
| Status basi ditolak, bukan ditimpa (penjaga balapan) | `test_stale_expected_status_is_refused_instead_of_overwritten` |
| Run `VOID` menolak slip baru | `test_void_run_refuses_new_payslips` |
| Detail galat `open_run` rata di `details`, tidak bersarang | `test_open_run_reports_a_backwards_period_as_flat_details`, `test_open_run_reports_a_short_currency_code_as_flat_details` |

Empat keputusan saat T2.4 dikerjakan:

1. **Allowlist, bukan update bebas.** `RUN_TRANSITIONS` menyatakan transisi yang boleh;
   apa pun di luar tabel ditolak `IllegalRunTransition`. Tanpa tabel ini, "pindahkan
   status" adalah pintu belakang untuk membayar run yang belum disetujui.
2. **`PAID` memverifikasi ulang segel setiap slip.** Menyetujui run dan membayarnya
   adalah dua peristiwa berbeda di waktu berbeda; kalau angka slip berubah di antaranya,
   pembayaran berhenti (`PayslipInconsistent` + `drift`), bukan melanjutkan dengan angka
   baru yang tak pernah disetujui.
3. **`expected_status` menutup balapan dua operator.** Dua orang membuka layar yang sama:
   yang kedua ditolak dengan `expected`/`actual` yang bisa dibaca, bukan menimpa.
4. **Bug halus ditemukan saat menulis tes:** `PayslipInconsistent(..., details={...})`
   menumpuk detail satu tingkat di bawah kunci `"details"` karena `DomainError` menerima
   `**details`. Dua tempat di `open_run` (periode terbalik, kode mata uang) diperbaiki
   menjadi kwargs rata, dan sekarang dikunci dua uji di tabel ini.

### Minggu 2 — upah deterministik (H2) + tampilan (H3)

| ID | Hari | Tugas | Artefak | DoD (bisa diuji) | Bukti |
|---|---|---|---|---|---|
| T2.1 | 6 | `src/workforce/payroll_policy.py` — muat `workforce_policy_versions` + `workforce_pay_rates` berversi; **tidak ada** angka pajak/BPJS di kode | modul + uji | ubah policy = versi baru; slip lama **tidak** berubah | uji: hitung dua kali dengan policy v1, lalu v2 |
| T2.2 | 6–7 | `src/workforce/payroll_calc.py` — Decimal penuh, `sanity` fail-closed (upah bersih ≥0, lembur ≤18 j/minggu, lembur ≤4 j/hari) | modul + uji | lembur 4,5 j/hari atau 19 j/minggu → ditolak, bukan dibulatkan | uji batas (4/4,5/18/19) |
| T2.3 | 7 | `src/workforce/payslip.py` — tulis `workforce_payslips` + `workforce_payslip_lines`, segel `PayrollCalculation.digest()` (sha256 kanonik) ke `audit_events.evidence_ref`; `workforce_payslips` tidak punya kolom `lock_hash`, jadi segel tidak mengarang kolom baru | modul + uji | mengubah 1 baris setelah segel → terdeteksi | uji: ubah `gross`/hapus baris, `verify_seal()` menolak |
| T2.4 | 7–8 | Alur status `DRAFT → CALCULATED → APPROVED → PAID → (VOID)`; `PAID` wajib `calculated_at` + `approved_at` | uji alur | lompat status ditolak; `VOID` tanpa alasan ditolak (regresi §1.1) | uji tabel transisi |
| T2.5 | 8 | **Gate G2**: rekonsiliasi 0 selisih pada dataset uji 30 hari × 12 staf sintetis | laporan angka | setiap slip punya jejak rate+policy+timesheet; 0 slip tanpa jejak | skrip `scripts/workforce_reconcile.py` — **selesai 2026-11-15: 1.000 pemeriksaan, 0 selisih (30 hari x 12 staf x 2 properti), 20 uji + 11 kontrol/batas, keluar 1 saat data dirusak, masuk `make check`** (lihat §4.5) |
| T2.6 | **Selesai** (2026-11-15) | `src/workforce/views.py` + 3 templat (`workforce_supervisor/finance/owner.html`) + katalog label `src/workforce/screen_labels.json` + 20 uji pada `tests/test_workforce_views.py` (14 saat T2.6 selesai; T2.6b menambah 6): tiga layar per peran, uang selalu lewat `money_display` (IDR dulu, referensi USD hanya bila policy menerbitkan kurs yang masih segar), layar read-only, gerbang peran *deny by default*, dan 0 istilah mesin di layar supervisor — lihat §4.6 | templat + uji render | 0 istilah teknis di layar supervisor; 0 nilai uang tanpa USD | uji render + grep templat |
| T2.6b | **Selesai** (2026-11-15) | **Penyaluran rute** tiga layar workforce ke `dashboard.py` (tidak ada di rencana awal §4 hari 10): `/workforce/today`, `/workforce/payroll`, `/workforce/cost` → page key, `PAGE_ROLES` satu peran per layar, judul halaman, entri `NAV` + grup `Workforce`, dan `WorkforceViews` **kehilangan kerangka halamannya sendiri** supaya hanya ada satu kerangka. Dua gerbang (rute dan isi) dikunci setara oleh uji; 20 uji `tests/test_workforce_views.py` + 40 kombinasi (halaman × peran) di matriks HTTP `tests/test_dashboard.py` — lihat §4.7 | rute + `PAGE_ROLES` + uji HTTP | 200 hanya untuk peran pemiliknya; 403 untuk lainnya; 401 tanpa identitas; 404 menyebut pilihan | matriks rute × peran lewat HTTP nyata |
| T2.7 | **Selesai** (2026-11-15) | Tema premium: token warna, tipografi, motif etnik-alam, kontras AA, `prefers-reduced-motion` | **Lapisan tema di `templates/base.html`** (CSS inline) + uji kontras — bukan `src/static/theme.css`: CSP dashboard `default-src 'none'; style-src 'unsafe-inline'` menolak berkas gaya eksternal, dan tidak ada rute `/static`. Alasan + angka di §4.8 | ≥8 pasangan warna lulus AA → **80 pasangan diukur, 80 lulus**; animasi mati saat reduced-motion → **dikunci uji**, termasuk kontrol negatif | `tests/test_ui_contrast.py` (rumus WCAG) + `tests/test_dashboard_bale_ui.py` |
| T2.8 | **Selesai** (2026-11-15) | **Gate G3 + G4**: suite penuh hijau, `make check`, audit PII, dokumen status diperbarui. **Lima** cacat ditemukan saat menjalankan gerbangnya sendiri: `scripts/pii_scan.py` mencetak PASS atas pohon yang tidak pernah dibaca (kini fail-closed, dikunci `tests/test_pii_gate.py` 8 uji); empat angka dokumen salah (kini dikunci `tests/test_sprint_doc_claims.py`); dan pengunci itu sendiri menuduh dokumen yang jujur di clone dangkal CI (kini `fetch-depth: 0` + skip beralasan). Butir G3 diperiksa satu per satu — lihat §4.9 | log + PR + `reports/gate-g3-g4-workforce.md` | 0 gagal; 0 PII; angka status cocok kenyataan (dikunci uji, bukan disiplin manual) | `make check`, `scripts/pii_scan.py`, `scripts/scan_stored_pii.py`, `git diff --stat` |

**Beban:** ±6,5 hari untuk jalur kerja, ±3 hari untuk upah, ±2 hari untuk tampilan, ±1 hari buffer. Buffer **tidak** dipakai untuk menambah fitur.

---

### 4.5 T2.5 — gerbang G2: 1.000 pemeriksaan, 0 selisih, dan bukti gerbangnya bisa gagal (2026-11-15)

Klaim "rekonsiliasi nol selisih" gampang diucapkan dan sulit dipercaya. Karena itu
gerbangnya dibuat dengan satu aturan: **yang menghitung ulang tidak boleh kode yang
menulis**. `scripts/workforce_reconcile.py` membaca `workforce_roster_entries`,
`workforce_attendance_events`, `workforce_timesheets`, `workforce_pay_rates`,
`workforce_policy_versions`, `workforce_payslips`, `workforce_payslip_lines`, dan
`audit_events`, lalu menghitung menit dan uangnya sendiri. Kalau penulis dan pemeriksa
memakai fungsi yang sama, keduanya bisa sama-sama salah dan gerbangnya tetap hijau.

**Dataset sintetis (bukan data orang nyata).** 12 staf (`Ops Staff 01..12`) di 2 properti,
30 hari (2026-11-01…2026-11-30), 5 jendela timesheet pekanan, 859 punch, 214 baris roster.
Dua bentuk run diuji sekaligus karena bisnis punya keduanya: 1 run bulanan (6 staf bergaji
bulanan) dan 5 run pekanan (6 staf bertarif harian). Angka policy disemai sebagai payload
berversi (`PAYROLL_TAX` bertanda `synthetic: true`, 5 %) supaya **tidak ada tarif yang
di-hard-code**; angka itu contoh dan wajib disahkan Finance + counsel sebelum dipakai uang
sungguhan (§7). Tidak ada satu pun run berstatus `PAID` di dataset — aturan sprint berhenti
di `APPROVED`.

| Tahap | Yang diperiksa | Jumlah | Selisih |
|---|---|---|---|
| R1 | punch mentah → baris `workforce_timesheets` (menit reguler, lembur, istirahat) | 180 | 0 |
| R2 | jumlah timesheet periode → `worked_minutes`/`overtime_minutes` slip | 72 | 0 |
| R3 | tarif + payload policy bertanggal → bruto/potongan/bersih **dan** baris slip | 144 | 0 |
| R4 | baris slip → tajuk slip (bruto, potongan, bersih) | 108 | 0 |
| R5 | tajuk slip → total run | 18 | 0 |
| R6 | jejak: policy berversi di tiap baris, tarif per komponen, timesheet `APPROVED`, segel, event audit | 427 | 0 |
| R8 | policy & tarif utuh dan disahkan: checksum payload dihitung ulang, tiap tarif punya penanggung jawab | 50 | 0 |
| R7 | rantai hash audit utuh (`verify_chain`) | 1 | 0 |
| | **total** | **1.000** | **0** |

Angka yang keluar di dataset ini: 60 timesheet, 36 slip, 180 baris slip, 6 run, 98.580 menit
dibayar, bruto 695.400 IDR, potongan 34.770 IDR, bersih 660.630 IDR — dan setiap angka di
kolom "bersih" adalah hasil penjumlahan ulang, bukan nilai yang dipercaya dari penulisnya.

**Kontrol negatif (20 uji, `tests/test_workforce_reconcile.py`; 11 di antaranya merusak data
lebih dulu atau mengunci batas).** Tiap kelas cacat disimulasikan lebih dulu, lalu gerbangnya
harus menandai tahap yang tepat; kalau tidak, uji itu gagal. Empat di antaranya membuktikan
pertahanan berlapis di luar skrip:

1. `UPDATE workforce_payslips SET net_minor = net_minor + 500000` **ditolak database**
   (`ck_workforce_payslip_arithmetic`). Penyerang yang tetap ingin lolos harus menaikkan bruto
   dan bersih bersama — versi itulah yang kemudian ditangkap R4 dan R5, karena barisnya
   dijumlahkan ulang dari bukti.
2. `UPDATE`/`DELETE` pada `audit_events` ditolak trigger append-only. Uji yang menyerupai
   penyerang ber-hak-DDL menjatuhkan trigger itu lebih dulu, membuktikan **lapis kedua**
   (rantai hash) tetap menangkap: satu baris disunting → R7; satu event issu dihapus → R6.
3. Payload policy disunting di tempat (menaikkan `tax_percent` tanpa versi baru) sudah ditolak
   **penulisnya sendiri**: `load_bundle` melempar `PolicyChecksumMismatch` saat slip disusun
   (dibuktikan lewat CLI, lihat di bawah). Bila penyuntingan terjadi setelah slip terbit, R8
   yang menangkap dan R3 menolak menghitung ulang memakai policy yang tidak bisa dipercaya.
4. Tarif dengan penanggung jawab kosong ditolak constraint skema (`length(approved_by) >= 3`).
   Uji mematikan constraint itu lebih dulu untuk membuktikan **lapis kedua**, R8, tetap melihat
   tarif yang kehilangan penanggung jawab.

**Empat hal nyata yang ditemukan gerbang ini saat pertama dijalankan** — inilah nilainya,
bukan angka 1.000-nya:

1. **Slip bisa lolos tanpa segel, dan skripnya mati.** `verify_seal()` melempar
   `EntityNotFound` bila event segelnya hilang, sehingga rekonsiliasi berhenti dengan
   traceback alih-alih melaporkan. Sekarang segel yang gagal diverifikasi dicatat sebagai
   temuan R6 (`gagal diverifikasi: EntityNotFound`) — gerbang fail-closed tapi tetap
   melapor.
2. **Koreksi manusia hilang di hitungan ulang.** Satu `MANUAL_ADJUST` (jam device meleset,
   dikoreksi +15 menit) membuat R1 melaporkan 1.830 vs 1.350 menit pada `stf_11`, karena
   perhitungan ulang membuang baris koreksi sebagai "bukan punch". Aturan sistemnya benar
   (koreksi mewarisi peran baris induknya, rantai, dan baris asli tidak dihapus); yang salah
   implementasi pemeriksa. Ini contoh tepat mengapa pemeriksa harus ditulis terpisah: tanpa
   itu, koreksi manusia menjadi selisih yang tak terlihat.
3. **Run tidak bisa dipindah ke `CALCULATED` dengan tangan.** `issue()` sudah memarkir run di
   `CALCULATED` saat slip terakhir ditulis, jadi transisi `CALCULATED` (dan sebelumnya juga
   `PAID`) ditolak `IllegalRunTransition` — perilaku yang benar, dan skripnya sekarang
   mengikuti alur nyata: tutup dengan `APPROVED`, `PAID` tetap keputusan Owner per run.
4. **Gerbangnya sendiri pernah hijau di atas tarif yang salah.** Saat mencoba membuktikan
   fail-closed lewat CLI, tarif dinaikkan **sebelum** slip terbit; slipnya jadi konsisten
   dengan tarif yang salah itu, dan R1–R7 tidak punya alasan mengeluh. Itu batas nyata, bukan
   bug yang bisa ditutup dengan kode: rekonsiliasi membuktikan kecocokan slip dengan tarif dan
   policy *yang ada di basis data*, bukan kewajaran tarifnya terhadap kontrak. Karena itu R8
   ditambahkan (checksum payload + penanggung jawab tiap tarif), dan batas ini **dikunci
   sebagai uji** — `test_internal_consistency_cannot_vouch_for_a_wrong_but_signed_rate` — supaya
   tidak ada yang lupa dan mengira gerbang ini memvalidasi tarif.

**Batas yang harus dibaca sebelum mempercayai angka ini.** (a) Membandingkan tarif dengan rate
card yang disetujui owner adalah kontrol manusia, di luar lingkup rekonsiliasi: yang bisa
dikatakan R8 adalah "tarif ini utuh dan ditandatangani", bukan "tarif ini benar". (b) Detektor
bahasa `scripts/check_workforce_language.py` memakai daftar penanda kata, jadi kata Indonesia di
luar daftar itu masih bisa lolos — batas ini dicatat di docstring skripnya. (c) Angka pajak di
dataset bertanda `synthetic: true`; kebijakan pajak sungguhan belum ada dan wajib disahkan
Finance + counsel (§7).


**Cara mereproduksi angkanya** (tanpa mengubah basis data apa pun — jalannya di basis data in-memory): `make workforce-reconcile` mencetak tabel yang sama seperti di atas; `python3 scripts/workforce_reconcile.py --json <berkas>` menyimpan versi mesin-baca (verdict, jumlah pemeriksaan per tahap, tiap selisih berikut diff-nya). Keluar 1 bila ada satu selisih, jadi CI tidak mungkin hijau di atas jumlah yang salah.

**Bukti fail-closed pada jalur CLI (bukan hanya di pytest)** — `main()` yang sama seperti
`make workforce-reconcile`, dijalankan tiga kali dengan data berbeda:

| Data yang direkonsiliasi | Temuan | Kode keluar |
|---|---|---|
| policy disunting di tempat setelah slip terbit | 37 | **1** |
| tarif dinaikkan setelah slip terbit | 120 | **1** |
| dataset bersih | 0 | 0 |

**Kedua gerbang masuk `make check`.** Target `workforce-language` dan `workforce-reconcile`
dijalankan setiap kali gerbang pra-deploy berjalan, supaya angka "0 selisih" selalu berasal
dari eksekusi nyata, bukan dari ingatan sesi sebelumnya. Ratchet bahasa khusus
`src/workforce/` dibuat karena tes global memakai daftar penanda terbatas + pin berkas
legacy: kata Indonesia di luar daftar itu (mis. `"issue() hanya menerima
PayrollCalculation"`) lolos tanpa jejak, dan berkas workforce baru tidak punya pin apa pun.


### 4.6 T2.6 — tiga layar, satu kebenaran uang (2026-11-15)

Tiga orang melihat fakta upah yang sama dan butuh tiga gambar berbeda: **supervisor** melihat
shift yang sedang berjalan, **keuangan** melihat uangnya, **pemilik** melihat biaya per properti.
Modulnya `src/workforce/views.py`; templatnya tiga berkas `templates/workforce_*.html`; katalog
kata-katanya `src/workforce/screen_labels.json`.

| Klaim | Uji yang menguncinya | Angka/hasil nyata |
|---|---|---|
| Uang selalu IDR dulu, referensi USD menempel di baris yang sama | `test_every_rupiah_figure_carries_a_dollar_reference` | 3 layar, setiap `IDR x` diikuti `(≈ USD y)` |
| Tanpa kurs terbitan: rupiah tetap, USD hilang, halaman mengatakan sebabnya | `test_without_a_published_rate_the_rupiah_stays_and_the_dollar_goes` | `IDR` ada, `≈ USD` tidak ada, `NO_RATE_NOTE` tampil |
| Kurs kedaluwarsa tidak dipakai (batas 48 jam diuji, bukan diasumsikan) | `test_a_stale_rate_drops_the_dollar_figure_and_says_so` | kurs 29 hari → USD hilang |
| Total pemilik = jumlah slip tersimpan, bukan hitungan kedua | `test_the_owner_total_is_the_sum_of_the_slips_not_a_second_calculation` | `paid_net_minor` = `SUM(net_minor)`; teks = `money_display.line()` dengan jam yang sama |
| 0 istilah mesin di layar supervisor | `test_the_supervisor_screen_shows_no_machine_words` | gaya (CSS), skrip, dan komentar dibuang dulu; yang boleh tampil hanya `IDR`, `USD`, `WIB` (ditulis terbuka sebagai daftar putih) |
| Layar tetap berguna (gerbang "tanpa istilah mesin" tidak boleh lulus karena kosong) | `test_the_supervisor_screen_still_says_something_useful` | kata katalog benar-benar muncul di teks yang dibaca |
| Setiap status yang boleh ada di basis data punya kata | `test_the_catalogue_covers_every_state_the_database_allows` | kode dibaca dari `CHECK (...) IN (...)` di DDL, bukan dari daftar yang saya ketik sendiri |
| Kode tak dikenal → sel kosong, bukan kata karangan | `test_an_unknown_code_leaves_the_cell_empty_rather_than_inventing_a_word` | `""` |
| Nama staf tidak bisa menyuntik markup | `test_a_staff_name_cannot_inject_markup_into_the_screen` | `<script>` tidak muncul, `&lt;script&gt;` muncul |
| Tabel kosong selebar tajuknya; `colspan` = jumlah `<th>` | `test_an_empty_table_spans_exactly_its_header_row` | 3 layar, tiap tabel |
| Layar read-only: dua kali render tidak mengubah satu baris | `test_rendering_a_screen_writes_nothing` | 6 tabel, jumlah baris sebelum = sesudah |
| Peran tidak bisa membuka layar yang bukan miliknya | `test_a_role_cannot_open_a_screen_it_does_not_own` | `operations`→supervisor, `finance`, `owner`, `guest`→(), `None`→() |
| Tidak ada markup yang tampil sebagai teks (tag ter-escape dua kali) | `test_no_escaped_markup_reaches_a_reader` | 3 layar, 0 `&lt;` di seluruh halaman |
| Judul peran di kepala halaman = nama peran blueprint, bukan nama orang | `test_the_role_title_comes_from_the_blueprint_map` | 3 peran dari `ROLE_DISPLAY_LABEL`; peran di luar bagan jatuh ke kodenya sendiri |

**Bahasa.** Copy yang dibaca staf berbahasa Indonesia (pembacanya di Bali dan Yogyakarta, alasan
yang sama dengan katalog pesan tamu ADR-0037), sementara `src/workforce/*.py` tetap **0 kata
Indonesia** (ADR-0011 §55). Yang menjembatani keduanya adalah katalog data: kata untuk setiap
status hidup di `screen_labels.json`, jumlahnya dipaku **9** di `scripts/check_workforce_language.py`,
dan pin templat dipaku di `tests/test_language_standard.py` (supervisor 7, finance 5, owner 9).

Cara mereproduksi angkanya:

```
make workforce-language                      # 0 temuan di src/workforce/*.py + katalog 9 kata
/usr/bin/python3 -m pytest tests/test_workforce_views.py -q   # 20 uji
```

Kontrol negatif gate katalog (dijalankan, bukan diasumsikan): satu kata `tidak` disisipkan ke
katalog → skrip keluar **1** (`11 > 9`), berkas dikembalikan → keluar **0**.

**Tema Bale-UI menuntut halaman baru punya identitasnya sendiri.** `make check` pertama untuk T2.6
gagal tepat di sini: `test_every_page_has_its_own_hue_and_kicker` menolak `workforce_supervisor`,
`workforce_finance`, dan `workforce_owner` sebagai "halaman tanpa rona". Itu bukan rintangan untuk
dilewati — halaman yang tidak bisa dibedakan dari halaman lain adalah keluhan yang memang ingin
dicegah tes ini. Ketiganya diberi rona sendiri (`--h: 175` / `240` / `305`) dan kicker sendiri, lalu
diukur ulang oleh tes kontras WCAG AA di mode gelap **dan** terang. Kicker ditulis dalam bahasa
Inggris karena begitulah konvensi `base.html` (kicker halaman lain pun Inggris), sehingga pin bahasa
shell tidak bergeser.

**Dua cacat yang ditemukan render nyata, bukan oleh ekspektasi saya.** Setelah tes hijau, tiga
layar dirender dari dunia G2 (12 staf, 30 hari) lewat skrip terpisah, lalu teksnya dibaca sendiri:
(1) slot `waktu` diisi string biasa sehingga penggantian templat meng-escape-nya dan pembaca melihat
tag `<time ...>` sebagai teks — diperbaiki dengan `_clock_slot()` dan dikunci oleh
`test_no_escaped_markup_reaches_a_reader`; (2) kepala halaman menampilkan **nama orang** sebagai
label peran (`peran anas (anas)`) — diperbaiki dengan mengambil `ROLE_DISPLAY_LABEL` dari dashboard
(bagan organisasi ADR-0035) dan dikunci oleh `test_the_role_title_comes_from_the_blueprint_map`.
Pelajaran: tes yang saya tulis mengunci janji yang saya ingat; membaca keluaran nyata menemukan janji
yang tidak saya ingat.

**Batas yang jujur (belum selesai di T2.6):**

1. Layar ini **belum dirutekan** dashboard HTTP: `views.render()` menghasilkan halaman utuh, tetapi
   `nav` sengaja kosong dan tautan antar layar masih jangkar dalam halaman. Konsekuensinya terlihat:
   dengan `actor`/`role` kosong (belum ada principal), kepala halaman menampilkan `peran ()` — bukan
   bug, melainkan tanda bahwa belum ada yang menyalurkannya. Penyaluran ke `dashboard.py`
   (rute + peran + `nav`) adalah pekerjaan tersendiri.
2. Campur bahasa yang memang bawaan shell: `footnote`/`NO_RATE_NOTE` dari `money_display` berbahasa
   **Inggris** (ADR-0011 §55) dan judul peran memakai nama jabatan bagan organisasi yang juga
   Inggris (`ROLE_DISPLAY_LABEL`, ADR-0035) — persis seperti halaman dashboard lainnya. Copy isi
   halaman tetap Indonesia. Menyeragamkan ini keputusan produk, bukan sesuatu yang saya putuskan
   sendiri; kalau diputuskan, ia berlaku untuk **semua** halaman, bukan hanya tiga layar ini.
3. Bentuk run ("Bulanan"/"Pekanan") **disimpulkan** dari ada/tidaknya `timesheet_id` di slip
   (`_run_shapes`), bukan dari kolom yang memang mencatatnya. Selama tidak ada kolom itu, labelnya
   bisa keliru untuk pola run yang tidak biasa — dan itu tertulis di sini supaya tidak jadi klaim.
4. Keempat berkas baru (`views.py`, 3 templat, 1 katalog) hanya diuji lewat jalur render; belum ada
   uji HTTP karena rutenya memang belum ada.

### 4.7 T2.6b — layar workforce akhirnya bisa dibuka (2026-11-15)

T2.6 membuat tiga layar, tetapi belum ada yang bisa membukanya: `views.render()` adalah
pintasan uji, `nav` sengaja kosong, dan kepala halaman menampilkan `peran ()` karena belum
ada principal. Layar yang tidak bisa dibuka tidak bisa dinilai manusia — jadi T2.6 belum
selesai sebelum rutenya ada. Pekerjaan ini tidak ada di WBS §4 (hari 9–10 sudah terisi tema
dan gerbang G3), maka barisnya ditambahkan sebagai **T2.6b**, bukan disembunyikan di dalam
baris T2.6.

**Rute.** `WORKFORCE_ROUTES` memetakan slug yang diketik orang ke page key yang dipakai kode:

| Yang diketik | Page key | Peran | Judul halaman |
|---|---|---|---|
| `/workforce/today` | `workforce_supervisor` | `operations` | Tenaga Kerja — Hari Ini |
| `/workforce/payroll` | `workforce_finance` | `finance` | Tenaga Kerja — Jalankan Upah |
| `/workforce/cost` | `workforce_owner` | `owner` | Tenaga Kerja — Biaya per Properti |

`/workforce` sendiri dan slug yang salah adalah **404 yang menyebut pilihan** (`known=[…]`),
bukan tebakan satu layar: menebak akan menampilkan layar payroll kepada supervisor.

**Dua gerbang, satu keputusan.** `dashboard.PAGE_ROLES` memutuskan apakah *rute* boleh
dibuka (menjawab 403) dan `workforce.views.SCREEN_ROLES` memutuskan apakah *isi* boleh
digambar (menolak walau pemanggil lupa memeriksa peran). Keduanya harus sama; uji
`test_the_dashboard_role_map_is_the_screen_map` gagal kalau salah satu dilonggarkan tanpa
yang lain, dan masing-masing gerbang punya ujinya sendiri.

**Satu kerangka halaman.** `WorkforceViews.render()`, `shell_context()`, dan `_clock_slot()`
**dihapus**: kerangka kedua adalah cara cacat label peran (§4.6) lahir — modul itu sempat
punya daftar sendiri untuk jam, judul peran, dan nav. Sekarang hanya `dashboard.py` yang
menggambar kerangka, dan `test_the_module_keeps_no_shell_of_its_own` gagal kalau kerangka
kedua tumbuh lagi. Halaman diuji lewat `Dashboard.dispatch` — jalur yang sama dengan
permintaan HTTP — bukan lewat fungsi render.

**Bukti (dijalankan, bukan disimulasikan).** Dashboard dijalankan di atas salinan berkas
dunia G2 dan disapa dengan token seperti browser (skrip probe, keluaran nyata):

| Rute | operations | finance | owner | auditor | guest | tanpa token |
|---|---|---|---|---|---|---|
| `/workforce/today` | **200** | 403 | 403 | 403 | 401 | 401 |
| `/workforce/payroll` | 403 | **200** | 403 | 403 | 401 | 401 |
| `/workforce/cost` | 403 | 403 | **200** | 403 | 401 | 401 |

Halaman 200 memuat `IDR` **dan** `≈ USD`, 0 istilah mesin; slug salah → 404. Kepala halaman
berbunyi `identitas agent-operations-1 peran Operations / Reservation Manager (operations)`
— label peran dari bagan organisasi, bukan nama orang.

**Tiga jebakan yang ditemukan saat menguji, bukan setelah rilis.**
1. Penanda `data-page="…"` **tidak** membuktikan halaman yang benar: blok `<style>` di kepala
   memuat penanda **semua** halaman, jadi halaman 403 pun lulus. Bukti sekarang tiga lapis:
   penanda `<section data-page=…>` harus muncul *setelah* `<body>`, judul `<h2>` layar harus
   ada, dan tautan antar layar harus menunjuk rute nyata.
2. Tautan "Layar lain" masih jangkar dalam halaman (`#workforce_finance`) — tidak membawa
   siapa pun ke mana. Sekarang rute nyata, dan diuji.
3. `RECORDED_SRC["dashboard.py"]` naik 156 → 159 (tiga judul halaman). Kenaikan itu
   **disengaja dan diberi alasan** di berkas pin, karena judul halaman dashboard memang
   berbahasa Indonesia (`Kasus`, `Keuangan`, `Rantai Audit`); ADR-0011 §55 mengatur pesan
   produksi dan teks tamu, bukan kerangka internal.

### 4.8 T2.7 — tema premium: diukur, bukan diklaim (2026-11-15)

Baris WBS T2.7 menuntut lima hal dan menyebut dua angka. Diperiksa satu per satu terhadap kode
dan uji — bukan terhadap niat:

| Butir WBS | Keadaan nyata | Bukti |
|---|---|---|
| token warna | `:root` token + `--h` per halaman (17 rona, semuanya berbeda) dan `--acc-s/--acc-l` per mode | `test_every_page_has_its_own_hue_and_kicker` |
| tipografi | dua tumpukan **lokal** (`ui-serif` untuk judul, `ui-monospace` untuk angka), 0 `@font-face`; angka uang tabular & rata kanan di `th.num`, `td.num`, `dd`, nilai KPI | `test_figures_line_up_because_the_type_is_local_and_tabular` (**uji baru**) |
| motif etnik-alam | tenun dua arah benang (`repeating-linear-gradient` 45°/-45°) + butir (data-URI SVG `feTurbulence`) — 0 berkas, 0 URL jaringan | `test_the_motif_is_woven_in_css_with_no_file_behind_it` (**uji baru**) |
| kontras AA | **80 pasangan diukur** (68 aksen halaman × 2 permukaan × 2 mode, + 12 nada status), **80 lulus**; terlemah **4,59:1** (`workforce_finance` h=240 di kartu gelap `#101917`); kontrol negatif kuning-terang di putih = **1,83:1** (ambangnya hidup) | `test_page_hues_meet_wcag_aa_on_canvas_and_card`, `test_state_tones_meet_aa_on_tiles`, `tests/test_ui_contrast.py` |
| `prefers-reduced-motion` | blok reduce mematikan `animation`/`transition`/`scroll-behavior` untuk `*, *::before, *::after` dan menetralkan gerakan hover; **sebelumnya tidak dikunci uji mana pun** | `test_motion_stops_when_the_reader_asks_for_no_motion` (**uji baru**) |

**Penyimpangan bentuk yang harus dicatat.** WBS menyebut deliverable `src/static/theme.css`;
berkas itu **tidak ada** dan tidak akan bisa bekerja: dashboard mengirim CSP
`default-src 'none'; style-src 'unsafe-inline'` dan tidak melayani rute `/static`, sehingga
berkas gaya eksternal akan diblokir browser. Lapisan tema karena itu **inline di
`templates/base.html`** (1.015 baris CSS, 256 aturan) dan dijaga
`test_layer_adds_no_script_or_external_asset`. Kriteria lulusnya terpenuhi; nama berkasnya di
rencana yang tidak. Itu ditulis di sini, bukan dibiarkan sebagai selisih senyap.

**Tiga kunci baru, masing-masing dengan kontrol negatif yang dijalankan.** Uji yang tidak bisa
gagal tidak membuktikan apa pun, jadi tiap kunci disabotase satu per satu dan harus gagal:

| Sabotase | Akibat nyata |
|---|---|
| `animation: none !important` dihapus dari blok reduce | **RC=1**, 1 uji gagal |
| butir berubah jadi URL jaringan (`https://cdn.example.com/grain.svg`) | **RC=1**, 2 uji gagal (motif + larangan aset eksternal) |
| `tabular-nums` dihapus dari `th.num` | **RC=1**, 1 uji gagal |

`templates/base.html` dipulihkan dan diverifikasi **sha256 sama** sebelum/sesudah sabotase.

**Dua jebakan yang tertangkap saat menguji.** (1) Kontrol negatif gerakan saya semula menuntut
"tidak ada `animation: none` di luar blok reduce" — ternyata blok `@media print` **juga** beku,
dan itu benar: kertas bukan pembaca yang bergerak. Kontrolnya kini mengecualikan blok cetak
*setelah membuktikan* blok itu memang beku. (2) Motif ternyata punya **empat** deklarasi, bukan
dua: dua mode (gelap/terang) × dua cara memilih mode (`prefers-color-scheme` mengikuti mesin, dan
sakelar `:target` di halaman). Ujinya sekarang menuntut keempatnya, karena satu jalur yang hilang
membuat separuh pembaca kehilangan motifnya tanpa satu pun uji lain gagal.

**Batas yang diakui:** tidak ada uji visual. Yang dikunci adalah kontrak (CSS ↔ templat ↔ kode) dan
angka kontras dari rumus WCAG yang sama dengan CSS — bukan piksel hasil render. Kesamaan rumus
dipaku `test_formula_matches_css`, jadi angka kontras tidak bisa benar sementara warna CSS berbeda.

### 4.9 T2.8 — gerbang G3 + G4: satu gerbang yang berbohong, dan empat angka yang salah (2026-11-15)

Menjalankan gerbang tidak sama dengan mempercayainya. Dua hal ditemukan saat menjalankannya,
dan keduanya adalah jenis kegagalan yang tidak bisa dilihat dari hasil "hijau".

**1. Gerbang PII mencetak PASS atas pohon yang tidak pernah dibaca.** `scripts/pii_scan.py`
diarahkan ke direktori yang **tidak ada** — keluaran nyatanya:

```
PII-SCAN: PASS (0 findings, 0 text files scanned)      RC=0
```

Nol berkas dibaca, nol temuan, hijau. Direktori kosong, berkas biner, atau root yang salah tulis
semuanya menghasilkan "bersih" yang sama. Ini kelas kegagalan yang paling berbahaya untuk sebuah
gerbang: satu-satunya bukti yang bisa dihasilkan gate PII yang hijau adalah "tidak ada temuan",
dan gate ini bisa menghasilkan kalimat itu tanpa membaca apa pun. Keluaran jalur normal sekarang
menyebut jumlah berkas yang benar-benar dibaca (423 berkas saat baris ini ditulis), sehingga
"0 findings" tidak bisa lagi berarti "0 dibaca".

Diperbaiki **fail-closed**: root yang bukan direktori → `FAIL`, RC=1; pohon tanpa satu pun berkas
teks terbaca → `FAIL`, RC=1. Dikunci `tests/test_pii_gate.py` (8 uji), termasuk kontrol positif
yang menanam surel, nomor seluler, dan nomor kartu Luhn-valid yang **sengaja bukan** salah satu
nomor uji di allowlist — menanam `4111…` hanya membuktikan allowlist bekerja, bukan bahwa
pemindainya bekerja. Kontrol arah sebaliknya juga ada: pohon yang hanya memuat nilai sintetis
tetap lulus, supaya gerbang ini tidak jadi penghalang yang semua orang akali.

Gate ini juga menangkap berkas ujinya sendiri: nomor seluler literal di draf pertama membuat
pemindaian repo gagal. Nilai tanamnya sekarang dibangun saat runtime.

**2. Empat angka dokumen tidak benar.** `tests/test_sprint_doc_claims.py` mengukur ulang setiap
berkas uji yang disebut dokumen dan menuntut angka di dokumen sama dengan kenyataan:

| Klaim dokumen | Kenyataan |
|---|---|
| T2.1 menulis "15" untuk berkas yang sebenarnya **35 uji** (`tests/test_workforce_payroll_calc.py`: 15 kebijakan + 20 kalkulasi) | angka 15 menempel pada berkas yang salah |
| T2.6 menulis "14" di dua tempat; berkas itu kini **20 uji** (`tests/test_workforce_views.py`) | T2.6b menambah 6 uji, angka lama tertinggal |
| §6: `make check` → "4297 passed" | **4445 passed** pada `6b08a9b` |

Keempatnya diperbaiki, dan dua aturan sekarang dikunci uji: (a) setiap angka "N uji" harus sama
dengan hasil collection nyata, (b) setiap klaim total "N passed" harus menyebut commit tempat ia
diukur, dan commit itu harus ada di riwayat — total tanpa atribusi adalah klaim yang tidak bisa
diperiksa siapa pun. Uji pengunci ini juga menolak lulus secara hampa: ia gagal kalau jumlah klaim
yang ditemukan turun di bawah 8, jadi menulis ulang dokumen agar klaimnya "menghilang" bukan jalan
keluar.

**Versi pertama pengunci itu sendiri salah, dan itu ditemukan dengan menjalankannya.** Jendela
pencocokan 60 karakter membuatnya membaca **kutipan angka lama** sebagai klaim status hari ini —
termasuk angka yang sedang ia keluhkan di tabel di atas. Empat positif palsu, semuanya dari
kalimat yang jujur. Yang diperbaiki adalah detektornya, bukan dokumennya: klaim sekarang harus
**menempel** pada berkasnya (jarak ≤12 karakter, dua arah), dan dua kutipan angka lama ditulis
ulang supaya tidak berbentuk klaim. Pengunci yang diperketat diuji ulang dengan tiga sabotase, dan
ketiganya membuatnya gagal: angka uji dirusak (`20` → `99`) · total tanpa commit · commit
pengukuran yang tidak ada (sha palsu tujuh huruf). Dokumen dipulihkan dengan sha256 identik.

**CI menemukan cacat ketiga — pada pengunci itu sendiri.** Tiga job gate gagal dengan pesan
`commit 6b08a9b tidak ada di riwayat`. Penyebabnya bukan dokumennya: `actions/checkout@v7` di
workflow memakai clone **dangkal** (`fetch-depth: 1`), jadi commit yang dikutip dokumen memang tidak
ada di objek lokal — dan penguncinya menyimpulkan "tidak ada" dari "tidak terlihat dari sini". Itu
tuduhan palsu kepada dokumen yang jujur.

Dua hal diperbaiki: (a) workflow kini memakai **riwayat penuh** (`fetch-depth: 0`) sehingga
pertanyaan itu benar-benar terjawab di CI; (b) penguncinya membedakan kedua keadaan — di clone
dangkal ia **skip dengan alasan tertulis**, tidak pernah menuduh, dan `git fetch origin <sha>`
sengaja **tidak** dipakai sebagai bukti karena server boleh menolak sha yang valid sekalipun.
Dibuktikan dengan tiga kontrol: clone dangkal → `skip` beralasan; clone penuh → periksa sungguhan;
clone penuh + commit palsu (sha palsu tujuh huruf) → RC=1.

**Cacat keempat: repo ini tidak mengizinkan commit merge.** Percobaan merge PR #34 ditolak GitHub
(`Merge commits are not allowed on this repository`; `allow_merge_commit=false`). Akibatnya sha cabang
yang dipakai sebagai provenance angka **hilang saat squash** — dan atribusi kepada commit yang tidak
bertahan adalah atribusi yang tidak bisa diperiksa orang lain. Jadi dokumen tidak lagi mengutip sha
cabang: angka T2.8 diatribusikan ke **run CI**, dan pengunci baru memastikan **setiap token sha yang
dikutip dokumen sprint ada di riwayat** (id run CI dikecualikan karena ia bertahan di luar git).
Tiga sabotase membuktikannya masih bergigi: sha hantu tujuh huruf → RC=1 · angka uji `20` → `99` →
RC=1 · provenance dilepas dari baris klaim total → RC=1; dokumen dipulihkan dengan sha256 identik.

**Cacat kelima: rujukan menggantung, dan kunci yang buta di mesin yang menulisnya.** Aturan
rujukan diperluas ke seluruh `docs/` + `reports/` + README, dan langsung menemukan rujukan nyata
yang menggantung: `docs/AUDIT_2026-11-13_FASE0_STATUS.md` mengutip rentang commit patch yang
**tidak bisa dilihat clone bersih** — ketujuh patch itu di-rebase dan mendarat ulang dengan sha baru
(subjek sama). Dokumennya kini mengutip rentang yang bertahan, dan menyebut terus terang bahwa sha
aslinya sudah tidak ada.

Yang lebih penting: kuncinya sendiri buta. Ia bertanya "apakah objeknya ada" (`cat-file -e`), dan di
clone yang pernah rebase objek lama **masih ada** — hanya tak terjangkau. Jadi pemeriksaan itu
**lolos di mesin penulis dan gagal di clone bersih**: hijau lokal, merah di CI, rujukan menggantung
tetap terkirim. Sekarang kuncinya meminta commit **terjangkau dari ref** (dengan resolusi singkatan
lebih dulu, karena `rev-list` memakai sha 40 huruf sedangkan dokumen mengutip 7 huruf).
Kontrolnya: objek c488222… di sini justru tidak ditulis sebagai token sha — aturan yang sama berlaku untuk contoh; yang diperiksa adalah commit dari audit itu yang objeknya ADA secara lokal tapi TIDAK terjangkau → dokumen bersih RC=0, sesudah
menyisipkan rujukan itu RC=1 (1 uji gagal), dipulihkan sha256 identik.

**4. Cacat susunan yang ikut terbawa: §4.5 terlempar ke akhir berkas**, sesudah §12 Riwayat
perubahan. Dipindahkan ke tempatnya (sebelum §4.6). Isi tidak berubah — diverifikasi dengan
membandingkan jumlah baris berisi sebelum/sesudah.

**Keadaan gerbang G3 (tampilan)** — semuanya dikunci uji, angka diambil dari eksekusi:

| Syarat G3 | Angka nyata | Pengunci |
|---|---|---|
| kontras AA | **80 pasangan diukur, 80 lulus**; terlemah **4,59:1**; kontrol negatif **1,83:1** | `tests/test_dashboard_bale_ui.py`, `tests/test_ui_contrast.py` |
| uang IDR + USD 100% | setiap angka rupiah membawa referensi dolar; kurs basi/tak terbit → dolar hilang dan layar mengatakannya | `test_every_rupiah_figure_carries_a_dollar_reference`, `test_without_a_published_rate_the_rupiah_stays_and_the_dollar_goes`, `test_a_stale_rate_drops_the_dollar_figure_and_says_so` |
| peran terpisah | `/workforce/today` → `operations`, `/workforce/payroll` → `finance`, `/workforce/cost` → `owner`; 200 hanya pemiliknya, 403 lainnya, 401 tanpa identitas, 404 menyebut pilihan | `tests/test_workforce_views.py` (20 uji) + matriks HTTP `tests/test_dashboard.py` (+40 kombinasi) |
| tanpa data nyata | staf bernama `Ops Staff 01..12`; `scan_stored_pii.py --db data/villa.db` → **7 kolom diperiksa, 0 baris** membawa P3/P4; `pii_scan.py` → **0 temuan atas 423 berkas teks**; gerbang bahasa → **0 istilah Indonesia di 10 berkas** `src/workforce/` + katalog 9 label; layar supervisor → 0 istilah mesin | `scripts/scan_stored_pii.py`, `scripts/check_workforce_language.py`, `test_the_supervisor_screen_shows_no_machine_words` |

**Angka T2.8 sendiri, dan kenapa dua lingkungan berbeda.**
- Lokal: **4455 passed, 7 skipped** (+4176 subtests, 408,10 s), `PRE-DEPLOY GATE: PASS` — diukur pada run CI `43797100871`.
- CI, tiga interpreter, run yang sama: **4454 passed, 8 skipped** — provenance run CI `43797100871`.
- Selisih satu uji bukan kegagalan: uji pembaca store butuh `data/villa.db`, dan clone bersih CI tidak
  punya berkas itu — persis alasan skip yang sudah dicatat dokumen A-Z. Keduanya ditulis supaya tidak
  ada pembaca yang mengira salah satunya salah. Repo ini tidak mengizinkan commit merge (squash/rebase
  saja), jadi commit cabang tidak dipertahankan; provenance yang tahan adalah run CI + laporan lokal.

**Keadaan gerbang G4 (rilis):** suite penuh hijau dan PII gate lulus lewat `make check` (angka
per-commit di `reports/gate-g3-g4-workforce.md`), dokumen status selaras (butir 2 dan 3 di atas,
kini dikunci uji), PR dibuka — dan CI pertamanya **gagal**, yang justru memunculkan cacat ketiga di atas; sesudah diperbaiki, keempat job gate plus job twin PostgreSQL hijau (lihat cek PR #34).

**Batas yang diakui.** Tidak ada uji visual: kontras dihitung dari rumus WCAG yang sama dengan CSS,
bukan dari piksel hasil render. Gate PII hanya memindai berkas teks — berkas biner, nama, dan
alamat bukan cakupannya (dinyatakan sendiri oleh skripnya, bukan disimpulkan). Uji pengunci angka
dokumen mengaudit **atribusi** klaim total, bukan menjalankan ulang suite penuh untuk membuktikan
totalnya.

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
| 2026-11-15 | T2.8 gerbang G3+G4: gate PII mencetak PASS atas 0 berkas yang dibaca → diperbaiki fail-closed + dikunci 8 uji; empat angka dokumen salah (15→35 uji, 14→20 uji ×2, 4297→4445) → diperbaiki + dikunci `tests/test_sprint_doc_claims.py`; detektor klaim versi pertama menghasilkan 4 positif palsu → diperketat; CI pakai clone dangkal sehingga penguncinya menuduh dokumen jujur → `fetch-depth: 0` + skip beralasan; §4.5 yang terlempar ke akhir berkas dikembalikan ke urutannya — §4.9 |
| 2026-11-15 | T2.7 diverifikasi butir-per-butir: 80 pasangan warna diukur (80 lulus, terlemah 4,59:1), gerakan/motif/tipografi kini dikunci uji dengan kontrol negatif yang dijalankan; deliverable berbentuk inline `templates/base.html` karena CSP menolak berkas gaya eksternal — §4.8 |
| 2026-11-15 | T2.6b: tiga layar workforce disalurkan ke `dashboard.py` (rute + peran + nav); kerangka halaman kedua di `workforce/views.py` dihapus; pin bahasa `dashboard.py` 156 → 159 dengan alasan |
| 2026-11-14 | Dokumen dibuat; baseline §1 diukur; cacat "NULL lolos CHECK" ditemukan & diperbaiki; kontrak uji 32 hijau; paritas skema OK |

