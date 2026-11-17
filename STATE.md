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
