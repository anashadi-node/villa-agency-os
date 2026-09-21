# Build Status — Villa Agency OS

Tanggal: 2026-09-22
Eksekutor: Hermes Anantara v3
Scope: hardening release-readiness Phase 2–4 + sales/deployment package

## Keputusan teknis

**PASS untuk code/test gate.** Sistem belum boleh diklaim produksi-live untuk pembeli akhir sampai keputusan bisnis di `docs/GO_LIVE_DECISIONS.md` selesai, provider pembayaran/WA/PMS dipilih, dan pilot nyata 10–20 properti dijalankan.

## Bukti eksekusi terbaru

```text
make check
python3 -m compileall -q src tests scripts
python3 -m pytest tests/ -q --tb=short
2282 passed, 4 skipped, 3549 subtests passed
SECRET-SCAN: PASS (0 findings, 96 files scanned)
UNSAFE-SCAN: PASS (0 findings, 96 files scanned)
OK: src/schema/001_initial.pg.sql matches the generated twin
=== PRE-DEPLOY GATE: PASS ===
```

Demo seeder juga dijalankan nyata:

```text
python3 scripts/seed_demo.py --db /tmp/villa-demo-check.sqlite
created: 3 properties, 4 units, 3 cases, 1 hold, 1 booking, 2 invoices,
1 payment, 2 payment events, 1 reconciliation case, 2 ops tasks,
1 incident, 1 approval request.

python3 scripts/seed_demo.py --db /tmp/villa-demo-dry.sqlite --dry-run
properties_after_dry_run=0
```

## Perubahan utama

1. `src/messages.py` — kontrak grounded outbound message diperbaiki:
   - `render_template()` mengembalikan binding terpakai sebagai iterable stabil.
   - unmatched claim sekarang objek `Claim`, bukan string mentah.
   - placeholder angka/uang/tanggal wajib diisi binding kind yang benar.
   - approval message tetap source-of-truth di approval request; verifier mendeteksi approval row hilang.
   - `GateRefused` tidak lagi menimpa error code domain.
   - recipient direct identifier tidak bocor di pesan error.
2. `src/schema/001_initial.sql` + twin PG — constraint approval draft disesuaikan supaya audit verifier bisa menemukan missing approval row, bukan disembunyikan FK.
3. `tests/test_phase3_payments_verification.py` — marker unsafe-scan eksplisit untuk fixture yang membuktikan PAN ditolak.
4. `scripts/seed_demo.py` — data demo runnable, dry-run rollback benar, dan tidak mencampur database berisi row non-demo.
5. `docs/DEPLOYMENT.md`, `docs/OPERATIONS_RUNBOOK.md`, `docs/SALES_PACKAGE.md` — paket deployment, operasi, dan sales untuk calon pembeli.
6. `README.md` — angka test dan struktur release diperbarui.

## Defect yang diperbaiki sesi ini

| ID | Defect | Dampak | Status |
|---|---|---|---|
| M-1 | `render_template()` API drift | test dan pemanggil salah membaca binding yang benar-benar dipakai | FIXED |
| M-2 | text binding bisa mengisi placeholder uang/angka | klaim nominal bisa tersimpan ungrounded tanpa fail-before-insert | FIXED |
| M-3 | gate denial mengganti code jadi `MISCLASSIFIED_CONTENT` | caller gagal branch pada `GATE_REFUSED` | FIXED |
| M-4 | approval row hilang tidak bisa diuji karena FK menutup simulasi | verifier tidak membuktikan corrupt approval state | FIXED |
| M-5 | error recipient berisi nomor `+62 ...` | PII bocor di exception | FIXED |
| M-6 | unsafe scan flag fixture payment proof | preflight false positive | FIXED |
| M-7 | `seed_demo.py` tidak bisa jalan (policy unique, kolom salah, FK fiktif) | demo sales gagal di depan calon pembeli | FIXED |
| M-8 | `seed_demo.py` tanpa transaksi eksplisit | failure bisa meninggalkan row demo parsial | FIXED |

## Batasan jujur sebelum dijual/dipakai live

1. Isi `[ISI]` di `docs/SALES_PACKAGE.md` sebelum ditawarkan: harga, support SLA, SEV1 response, paket kunjungan.
2. Selesaikan keputusan OPEN/BLOCKING di `docs/GO_LIVE_DECISIONS.md` untuk merchant-of-record, payment provider, WA provider, PMS/channel manager, legal/tax boundary.
3. Jalankan pilot terbatas dengan data real 10–20 properti; target acceptance: 0 confirmed double booking, reconciliation ≥99%, unsupported AI claim = 0.
4. Untuk deployment produksi: database bersih non-demo, backup aktif, anchor directory aktif, env var production valid, dan `scripts/preflight.py` harus GO.

## Session log

Files changed:
- README.md
- docs/DEPLOYMENT.md
- docs/OPERATIONS_RUNBOOK.md
- docs/SALES_PACKAGE.md
- reports/BUILD_STATUS_2026-09-22.md
- scripts/seed_demo.py
- src/messages.py
- src/schema/001_initial.sql
- src/schema/001_initial.pg.sql
- tests/test_phase3_payments_verification.py

Tests: 2282 passed, 4 skipped, 3549 subtests passed.
Deploy: tidak dideploy; repo package hardening + release docs.
Expected impact: release-readiness naik; demo sales runnable; message safety gate fail-closed.
Monitor: keputusan go-live bisnis + pilot metrics.
