# STATE — Villa Agency OS (dibaca agen di awal sesi; diperbarui di akhir gelombang)

Diperbarui: 2026-11-17 · Sumber: `git log`, `docs/AUDIT_2026-11-13_FASE0_STATUS.md`, eksekusi suite pada sesi ini

## Posisi terakhir (terverifikasi dari git)
- HEAD: `c0169ee` — outbound DLP guard on the outbox, slice 1 of Secrets/DLP (plan A) (#38)
- Sebelumnya: `13b23fd` crash resilience outbox publisher (plan B, #37); `45f8c6a` refresh status go-live (plan C, #36); `6acfc96` gate T2.8 (#34)
- Working tree 17 Nov (belum commit): `scripts/preflight.py`, `tests/test_marketing_site.py`, `tests/test_preflight.py` + dokumen `docs/PROJECT_VILLA_AGENCY_AZ_2026-11-17.html`

## Selesai 17 Nov 2026 (bukti)
- **Gerbang tes hijau**: `/usr/bin/python3 -m pytest tests/ -q` → RC=0, 4.492 tes, 4.485 lulus, 7 skip, 0 gagal (`reports/pytest-2026-11-17b.txt`). Tes merah `TestTrustedProxyFlag::test_a_junk_request_line_does_not_break_the_log_path` diperbaiki: akar masalah = mode HTTP/0.9 tidak menulis baris status, jadi bukti 400 dipindah ke baris log + server tetap melayani.
- **Preflight `--production`**: `WARN "not checked"` (anker, backup) menjadi `FAIL`. Terbukti: `preflight.py --db data/villa.db --production` → NO-GO, 2 FAIL, RC=1. Diuji dua arah di `tests/test_preflight.py`.
- **Dokumen induk A–Z**: `docs/PROJECT_VILLA_AGENCY_AZ_2026-11-17.html` (28 bagian + 6 lampiran).

## Lubang yang masih terbuka (jangan dianggap selesai)
- `SCHEMA_VERSION = "001_initial"` hanya string bootstrap → preflight melaporkan "schema version OK" untuk DB yang belum menjalankan migrasi 017/018. Perbaikan: bandingkan himpunan migrasi terpasang vs `src/schema/*.sql`.
- `data/villa.db` tertinggal dari skema: 73 tabel, tanpa `workforce_*`, tanpa efek `017_property_geo.sql`. Perlu dibuat ulang + `apply_migrations.py`.
- `make lint` gagal karena `pyflakes` tidak terpasang untuk `/usr/bin/python3` (lingkungan, bukan kode). Ruff manual: tidak ada temuan baru dibanding HEAD.

## Fase 0 — status butir (dari dokumen audit 13 Nov)
- TERKIRIM: K1, K2, K5, S1, S2, S4
- SEBAGIAN: T1 (nilai pajak per kabupaten menunggu keputusan owner), T8 (dokumen tamu menunggu counsel)
- TERBUKA: S6 (akun `owner_anas` di seed; ditunda bersama perubahan seed berikutnya)

## GARIS FINIS — DRAF, menunggu persetujuan owner
Gelombang berjalan "Secrets/DLP" selesai bila: slice berikutnya merge dengan tes hijau (`make test`), tidak ada secret/PII di `git diff`, dan STATE.md ini diperbarui.
Fase 0 dianggap tutup bila T1, T8, S6 tidak lagi SEBAGIAN/TERBUKA **atau** owner menundanya secara tertulis di sini.
Anggaran gelombang: ±150 panggilan API; lewat itu agen berhenti dan lapor.

## Keputusan menunggu owner
- Nilai pajak per kabupaten (T1)
- Dokumen tamu / counsel (T8)
- Waktu perubahan seed (S6)
- Apakah perbaikan cek versi skema (migrasi 017/018) dikerjakan berikutnya

## Aturan kerja
Lihat bagian "PROTOKOL HEMAT & CEPAT" di SOUL.md profil villa-agency.

## PACK-02 — FR-917 Effective Migration Ledger (2026-11-17)
- Status: kode + tes selesai; satu commit lokal atas izin Owner (hash: lihat `git log -1`), **belum push**. `tests/test_marketing_site.py` dan dokumen untracked sengaja tidak ikut commit.
- Efek pada "Lubang yang masih terbuka" di atas: cek versi skema kini dilengkapi cek `migration ledger` (FR-917); baris lama dibiarkan sebagai catatan historis.
- Perubahan: `src/schema_loader.py` (+`required_migrations`, +`missing_migrations`), `scripts/preflight.py` (+cek `migration ledger` di `check_database`), `tests/test_schema.py` (+5 tes), `tests/test_preflight.py` (+9 tes, `_make_db` kini mengisi ledger).
- Aturan: set wajib = isi `src/schema/` (bukan daftar tulisan tangan, celah nomor 009 tidak dianggap hilang); tanpa ledger / ledger tak terbaca / entri kurang = FAIL (NO-GO di `--production`), nama file migrasi disebut.
- Tidak berubah: `data/villa.db` (stat sebelum=sesudah), kernel, money ledger, policy, approval, konfigurasi produksi. Tidak ada migrasi dijalankan.
- Backup pekerjaan lama (di luar repo): `~/villa-agency-os-backups/pack02-2026-11-17/` (MANIFEST.sha256 terverifikasi).
- Belum dibuktikan: full suite; anti-overlap PostgreSQL (`VILLA_PG_DSN` kosong, 4 tes PG di-skip); `data/villa.db` asli masih kurang 018 dan akan NO-GO sampai Owner menyetujui migrasi.

## PACK-03 — FR-918 Restore Proof (2026-11-17)
- Status: kode + tes selesai; satu commit lokal atas izin Owner (hash: `git log -1`), **belum push**. **Bukan klaim production readiness.**
- Implementasi: `src/restore_proof.py` (baru) memulihkan backup lewat `sqlite3.backup()` ke direktori sementara privat, lalu memeriksa salinan read-only (`mode=ro`): integrity_check, foreign_key_check, migration ledger (FR-917), audit chain, `verify_published_anchors`. Tidak memanggil `connect_sqlite`/`apply_schema`. `scripts/preflight.py`: cek `restore proof` setelah cek `backup`; tanpa `--backup`/`--anchor-dir` = "not checked" -> FAIL di `--production`.
- Sumber dibuka `mode=ro&immutable=1` hanya bila tidak ada `-wal`/`-journal` tertunda; sha256 + isi direktori dibandingkan sebelum/sesudah, berbeda = FAIL. Backup dari DB hidup wajib lewat `.backup`, bukan `cp`.
- Bukti tes (eksekusi nyata, DB sintetis di tmp_path): `tests/test_restore_proof.py` 25 tes lulus; suite relevan (restore_proof, preflight, schema, migrations, preflight_host_check, tenant_onboarding_path, anchors, audit, marketing_site) RC=0, 4 skip PG-live. `stat` `data/villa.db*` tidak berubah.
- Negative control: `test_negative_control_a_weakened_checker_lets_the_same_files_through` (checker dilemahkan meloloskan berkas yang sama). Tolak: berkas sampah 8192 B, SQLite kosong tanpa skema, ledger kurang/tanpa 018, audit chain rusak (termasuk varian trigger dipasang ulang), DB korup, backup tidak ada, -wal/-journal tertunda, anchor hilang/kosong, backup lebih tua dari anchor, backup berubah saat dibaca, anggaran waktu terlampaui.
- Perubahan perilaku disengaja: fixture backup di tes GO `tests/test_preflight.py` diganti backup SQLite sungguhan (berkas sampah 8 KiB tadinya lolos).
- Keterbatasan: durasi restore diukur pada data sintetis = bukti mekanisme, BUKAN bukti RTO 4 jam produksi (D10 tidak diubah).
- NOT_VERIFIED: PostgreSQL live (`VILLA_PG_DSN` kosong). NOT_RUN: full suite. Lint: gerbang pyflakes (F,E9) bersih; ruff dengan aturan gaya tambahan menandai berkas baru (I001/UP045/RUF100) dan tidak dirapikan agar sesuai gaya repo.
- OPEN reconciliation: `BACKUP_MAX_AGE_HOURS=26` (preflight) vs RPO 24 jam (D10). Perilaku 26 jam dipertahankan; butuh keputusan Owner.
- OPEN: kadensi uji restore, `DEPLOYMENT.md §5` (bulanan) vs D10 (3 bulanan).

## PACK-04A — FR-904 bukti constraint PostgreSQL, lapisan DB saja (2026-11-17)
- Status: **IMPLEMENTED_PENDING_CI**. Satu commit lokal atas izin Owner (hash: `git log -1`), **belum push**, CI belum dijalankan. BUKAN PASS_VERIFIED. BUKAN klaim Reservation Kernel berjalan di PostgreSQL (kernel tetap SQLite-only; tidak diubah).
- Pengamanan DROP: tiga jalur destruktif di job `postgres-parity` ditutup. `TestLivePostgres.setUpClass` tidak lagi `DROP SCHEMA public CASCADE` pada DSN apa pun; `test_workforce_gate_g1.py` tidak lagi `DROP SCHEMA gate_g1` pada DSN yang diberikan. Keduanya kini meminta database BARU milik harness (`villa_t_<hex>`) lewat `disposable_database()` di `tests/test_pg_overlap_concurrency.py`; hanya nama yang dibuat proses itu yang bisa di-DROP.
- Penjaga (`prove_disposable_server`) sebelum operasi destruktif: GitHub-hosted + `VILLA_PG_DISPOSABLE_RUN == GITHUB_RUN_ID-GITHUB_RUN_ATTEMPT`, postmaster < 1 jam, tidak ada database asing di cluster, role bukan superuser. Host/port/nama DB sengaja bukan bukti. Ragu → `DisposableRefused` (FAIL, bukan skip).
- `check.yml`: DSN superuser literal dihapus; langkah baru membuat role `villa_ci` (NOSUPERUSER, CREATEDB, sandi acak di-mask) dan mengekspor DSN + token run; berkas konkurensi ikut dijalankan; langkah baru menggagalkan job bila ada tes PG ber-SKIP. Kontrak dijaga `test_gate_dependencies.py`.
- Tes baru: 2 koneksi psycopg terpisah; B teramati MENUNGGU (`pg_stat_activity.wait_event_type='Lock'`) sebelum ditolak `23P01`; 12 putaran barrier; jendela identik; unit berbeda; back-to-back `[)` tanpa menunggu; idempotency `23505`; lock_timeout `55P03` + retry; rollback; sesi dimatikan (`pg_terminate_backend` hanya pada PID harness); promosi status konkuren; kontrol negatif (constraint dilepas hanya di DB harness kedua, penghitung invarian self-join independen harus melihat 1 pasangan tumpang tindih). Semua untuk `bookings` dan `holds`.
- Bukti lokal (nyata): ruff F,E9 bersih; tanpa DSN 109 lulus + 33 skip (semua tes live PG), `VILLA_PG_REQUIRED=1` tanpa DSN → 0 skip, 33 error; DSN ada tapi bukan CI → semua tes live GAGAL `DisposableRefused`, 0 skip (interpreter ber-psycopg); lingkungan CI simulasi + host tak ada → gagal di koneksi, tanpa operasi destruktif; `data/villa.db*` tidak berubah.
- NOT_RUN: seluruh tes konkurensi terhadap PostgreSQL sungguhan (tidak ada PG yang diizinkan lokal). SQL harness hanya dry-run di twin SQLite (kolom/CHECK/status cocok); perilaku PG (`btree_gist`, `EXCLUDE`, trigger, FK seed) baru terbukti setelah CI.
- G6 CONTRACT_DRIFT tetap terbuka (blocker PG produksi): `connect_pg(apply=True)` hanya menerapkan 001; `ex_*_no_overlap` ada di 002. Loader tidak diubah; fixture menerapkan 001+002 eksplisit.
- RISIKO terbuka (bukan hasil lulus): HOLD vs BOOKING lintas tabel tidak dijaga constraint PG; ditandai `xfail(strict)` supaya kemajuan terlihat. Kernel→PG (adapter/ADR) = PACK-04B, belum dimulai.

## PACK-04A — HASIL CI + batas klaim (2026-11-18)
- Status: **PASS_VERIFIED** (lapisan DB PostgreSQL saja). Commit `28a2bcd` (perbaikan `40P01` transien) di-push ke `feat/fr904-pg-ci`; CI run `44182634091` hijau: **136 PASS, 0 FAIL, 0 SKIP, 1 XFAIL, 0 XPASS**. Master tidak berubah (`c0169ee`).
- Perbaikan: penulis kalah boleh ditolak `23P01`/`23505`/`40P01` (deadlock victim = transien, BUKAN penolakan bisnis; butuh bounded retry di kernel PG berikutnya), syarat tetap: tepat satu commit, 0 overlap, SQLSTATE spesifik, 0 partial commit.
- BATAS KLAIM: ini bukti constraint DB PG (EXCLUDE `ex_*_no_overlap` + UNIQUE) di server sungguhan — BUKAN bukti Reservation Kernel berjalan di PG. Kernel tetap SQLite-only. `40P01` = risiko operasional untuk kernel PG berikutnya.
- Masih terbuka (tidak berubah oleh PACK-04A): G6 CONTRACT_DRIFT (loader PG hanya 001; 002 & 003–018 tak diterapkan jalur produksi); HOLD-vs-BOOKING lintas tabel (XFAIL, unresolved); G4/G5 (rollback/disconnect/READ-COMMITTED belum diuji kernel).

## PACK-05 — Booking Lifecycle Closure, bukti ketujuh kondisi + isolasi tenant (2026-11-18)
- Keputusan Owner: **SQLite internal pilot** (satu agency, 10–20 vila, data sintetis); PostgreSQL tetap target produksi (ADR). PACK-05 TIDAK membangun adapter PG.
- TEMUAN UTAMA (anti-confirmation-bias): tiga fakta §30 yang dulu "tak punya owner" **SUDAH punya owner** sebelum PACK-05 — settlement `src/supplier_settlements.py` (ADR-0056), deposit `src/deposit_resolutions.py` (ADR-0055), dokumen `src/documents.py` + policy key `operations.booking.required_document_kinds` (ADR-0054). Gate `booking_closure.evaluate` sudah membaca ketiganya. Jadi PACK-05 = menutup bukti yang belum ada, BUKAN menulis modul baru.
- Perubahan (TES SAJA, tidak ada kode produksi): `tests/test_booking_closure.py` +3 tes + helper `publish_documents` dipindah ke base class (tanpa mengubah makna).
  - `test_a_booking_closes_for_real_when_all_seven_conditions_hold` — 7 fakta di-seed lewat writer asli (settlement DUE+PAID, deposit RETURNED, dokumen+policy), lalu `close_booking` NYATA menulis `CLOSED` + state event + audit `booking.closed`. Tanpa mock (tes lama mem-mock `evaluate`).
  - `test_facts_recorded_under_another_agency_do_not_satisfy_this_tenants_gate` — settlement+deposit milik `agency_other` untuk booking yang sama TIDAK memenuhi gate tenant ini (tetap `SUPPLIER_GAP`/`DEPOSIT_GAP`). Isolasi ADR-0037.
  - `test_closing_a_booking_that_has_not_completed_its_stay_is_refused_and_writes_nothing` — `IN_STAY` ditolak `BOOKING_NOT_CLOSABLE`, 0 state event, 0 audit `booking.closed`.
- Tidak berubah: `reservation_kernel.py` (docstring lama yang menyebut "facts no module records yet" dibiarkan, menunggu izin sentuh kernel), money ledger, policy, approval, financial authorization, skema (tanpa migrasi 019).
- Financial safety dipertahankan: settlement/deposit tetap butuh `decided_by` (manusia), `actor_role`, `evidence_ref`, `idempotency_key`; tidak ada PAID tanpa bukti; tidak ada auto-approval.
- Bukti: `pytest tests/test_booking_closure.py tests/test_deposit_resolutions.py tests/test_supplier_settlements.py` → 42 passed (sebelumnya 39). Full suite lihat laporan PACK-05.
- PILOT SAFETY GATE (belum lulus, jangan klaim pilot siap): partial-overlap SQLite belum dibuktikan tes balapan eksplisit; HOLD-vs-BOOKING lintas tabel (XFAIL); G4/G5 (rollback/disconnect/READ-COMMITTED); backup/restore evidence; approval gate finansial. Ini scope gelombang berikutnya, BUKAN PACK-05.

## PACK-06 — SQLite Reservation Concurrency Safety (2026-11-18)
- Status: **PASS** (tes saja; tidak ada kode produksi, skema, atau DB asli yang berubah). Tidak ditemukan defect kernel; kegagalan yang muncul saat menulis tes ternyata bug harness (ID seed ganda antar-race, kunci idempotency dipakai ulang antar-ronde, asumsi kolom `agency_id`) dan diperbaiki di sisi tes, bukan dengan melemahkan asersi.
- Tempat: `tests/test_no_double_booking_concurrency.py` (+21 tes, 10 kelas total). DB file-backed sintetis di scratch tmpdir per tes, satu koneksi per thread, `threading.Barrier` + bukti rentang waktu tumpang tindih (`test_the_writers_really_run_at_the_same_time`).
- Cakupan A–J: A partial-overlap (pasangan, 10 putaran, tangga 6 jendela); B HOLD vs BOOKING (hold vs konfirmasi booking, 3 hold vs booking CONFIRMED); C konfirmasi ganda; D dua vila; E back-to-back; F idempotency (kunci sama = replay, kunci beda satu hold = satu booking, satu kunci dua jendela = `IDEMPOTENCY_CONFLICT`); G rollback (kegagalan setelah tulis + racer yang benar-benar diblokir lalu menang); H busy/timeout (`database is locked` setelah menunggu, dan lock singkat ditunggu lalu sukses); I status occupying dibaca dari indeks parsial skema dan dicocokkan dengan konstanta kernel, tiap status occupying menolak hold balapan, tiap status non-occupying meloloskan tepat satu; J kontrol negatif `UnguardedKernel` (tanpa `_assert_no_overlap`) → 2 pemenang + 1 pasangan tumpang tindih terdeteksi, pasangan tes yang sama di kernel asli bersih.
- Invarian dibaca dari DB sesudah race: nol pasangan occupying tumpang tindih, jumlah baris holds/evidence/audit == jumlah pemenang, tabel uang tidak bergerak, tidak ada agency asing.
- Hasil: 74 tes (junit, subtest terhitung terpisah) 0 gagal / 0 error / 0 skip, stabil 5 run berturut-turut. Regresi terarah (kernel + closure + PG overlap): 150 lulus, 0 gagal, 26 skip (tes live PG tanpa `VILLA_PG_DSN`).
- NOT_RUN: full suite (permintaan background tidak disetujui); hanya file lain yang tidak diubah. Temuan ruff RUF059 di baris `test_a_rival_hold_is_refused_while_the_booking_awaits_payment` sudah ada di HEAD, tidak disentuh.
- Batas klaim: bukti ini untuk kernel di SQLite; tidak membuktikan PostgreSQL, multi-proses lintas host, atau beban tinggi. SQLite `BEGIN IMMEDIATE` menyerialkan penulis, jadi "concurrency" = antrean kunci tulis, bukan paralelisme tulis. Pilot belum dinyatakan siap.

## W1-SAFETY — Test Database Isolation (2026-11-18)
- Izin Owner: DEC-W1-01..05 (`2b0e8a`) + audit independen (`VILLA_AGENCY_GITHUB_INDEPENDENT_AUDIT_2026-11-18.md`). Bounded: hanya isolasi tes, tanpa refactor.
- Akar masalah (AUD-01/02/03, terbukti baca kode): `tests/test_ai_allowance_cli.py` memanggil `_revisions(REPO/data/villa.db)` dan menjalankan subprocess dengan `cwd=REPO`; `tests/test_roles.py` memakai default `LIVE_DB = REPO/data/villa.db`. `src/schema_loader.connect_sqlite()` menjalankan `PRAGMA journal_mode=WAL` + `apply_schema()`, jadi panggilan yang niatnya "baca saja" tetap bisa menyentuh WAL/SHM store asli.
- Perubahan (3 file kode tes + 1 Makefile + 1 register):
  - `tests/test_ai_allowance_cli.py` — `_run(..., cwd=)`; tes penolakan kini berjalan di direktori kerja sintetis yang punya `data/villa.db` sendiri (default CLI relatif), `_revisions()` dibaca via `mode=ro`; ditambah kontrol positif (store yang sama, disebut eksplisit → publish sukses, 1 revisi) + kontrol isolasi (path default ≠ store repo) + guard sumber yang menolak `REPO / "data"` muncul lagi.
  - `tests/test_roles.py` — `LIVE_DB` tidak lagi punya fallback ke store repo (None bila `VILLA_DB` kosong); `_live_db()` skip/fail dengan pesan yang menyebut sebabnya; pemeriksaan ladder diekstrak ke `_published_ladder_in()` (`mode=ro`) + `_assert_ladder_names_known_roles()`; kontrol positif+negatif sintetis (`_synthetic_ladder_store`) membuktikan pemeriksaan itu bergigi.
  - `Makefile` — `check-store` gagal-closed bila `VILLA_DB` kosong (exit 2) dan tetap menjalankan pemeriksaan deployment-store sebagai target terpisah.
  - `docs/W1_FULLSTACK_BASELINE_DRAFT.md` — register DRAFT (DESIRED_BASELINE / AS_BUILT_STATE / RECONCILIATION_DELTA, provenance per sumber, OPEN-W1-01..41). Tidak ada requirement baru yang dianggap approved.
- DEC-W1-04 dipenuhi tanpa perubahan: `tests/test_migrations.py` sudah aman — `_run()` membuang `VILLA_DB` warisan dan setiap tes memberi store `tmp_path`; `scripts/apply_migrations.py` menolak (exit 2) sebelum membuka apa pun bila `VILLA_DB` kosong. Tidak ada jalur default ke DB repo.
- Bukti isolasi (strace `-f`, seluruh syscall file, artefak scratch `w1safety-20261118-082635/strace.log`): 0 baris non-ENOENT yang menyebut `villa-agency-os/data`; 35 akses sukses ke 3 store sintetis; `sha256sum data/villa.db` identik sebelum/sesudah.
- Hasil tes: `tests/test_ai_allowance_cli.py tests/test_roles.py` → 51 tes, 0 gagal, 0 error, 1 skip (pemeriksaan store deployment, benar karena `VILLA_DB` tidak diset). Regresi role/approval terkait (`test_roles`, `test_approvals`, `test_agency_roles`, `test_workforce_views`, `test_wave4d_discount_engine`, `test_ai_gateway`, `test_dashboard`) → semua lulus. `make check-store` tanpa `VILLA_DB` → exit 2; dengan store sintetis → 38 passed.
- Lint: `.venv/bin/pyflakes` bersih pada kedua file (gate Makefile). `ruff check` menyisakan PLW1510 dan `ruff format --check` menyisakan reformat — keduanya sudah ada di HEAD (diverifikasi dengan `git show HEAD:…`), di luar scope, tidak disentuh.
- Operasional (read-only, tanpa restart): `villa-dashboard.service` = `dashboard.py --port 8085 --role owner --token-file …` dan `villa-marketing.service` = `marketing_site.py --port 8090`, keduanya bind loopback, `WorkingDirectory` repo, runtime `/usr/bin/python3`. `make interpreter` → **Python 3.14.4 / SQLite 3.46.1** (komentar Makefile yang menyebut 3.12.3/3.45.1 sudah usang).
- Risiko WAL (AUD-07) terverifikasi di sumber primer: sqlite.org/releaselog/3_51_3.html menyebut perbaikan "WAL-reset database corruption bug" di 3.51.3 (2026-03-13); runtime 3.46.1 < 3.51.3 → eksposur ada secara prinsip, peluangnya disebut sangat rendah. Tidak ada upgrade otomatis; perlu jendela perubahan terpisah.
- UNKNOWN (jujur): mode/permission file token dashboard tidak diperiksa; atribusi perubahan `-shm` 07:16:42 tidak bisa dibedakan antara tes lama dan service yang memegang store; versi SQLite di `.venv` tidak diukur (perintahnya terblokir guard).
- Tidak diklaim: seluruh produk selesai; requirement mana pun VERIFIED_COMPLETE; full suite dijalankan pada gelombang ini (dilarang oleh DEC-W1-05).
