# Laporan Gate Keamanan & Privasi — villa-agency-os

Versi: 1.0 · Tanggal: 2026-09-21 · Commit: `1ccdcce` (kode: `8c4c777`) · Reviewer: Hermes Anantara v3

Lingkup: seluruh repo `/home/anashadi/villa-agency-os` (35 file ter-track). Kode milik
pengguna; tidak ada target pihak ketiga yang diaudit, tidak ada eksploit yang dikembangkan.

Keputusan: **PASS untuk Phase 1 (Reservation Kernel)**, dengan 2 risiko residual tercatat
di §5 (tidak memblokir karena belum ada data tamu nyata yang diproses).

---

## 1. Metode (semua dapat direproduksi)

| # | Perintah | Hasil |
|---|---|---|
| 1 | `bash scripts/secret_scan.sh` | PASS — 0 temuan / 35 file |
| 2 | `bash scripts/unsafe_config_scan.sh` | PASS — 0 temuan / 35 file |
| 3 | `grep -rnE '\beval\(\|\bexec\(\|os\.system\|subprocess\|shell=True\|pickle\|yaml\.load' src/` | 0 temuan |
| 4 | `grep -rn 'requests\|urllib\|socket\|http.client\|httpx' src/` | 0 temuan (kernel offline) |
| 5 | `grep -rnE 'execute\(.*\{' src/` | 1 temuan, informational (lihat §3) |
| 6 | `grep -rn 'except:' src/` , `grep -rn '^ *assert ' src/` | 0 temuan |
| 7 | `python3 scripts/generate_pg_twin.py --check` | OK — twin Postgres identik |

Pola yang dipindai `secret_scan.sh`: `sk-…` (OpenAI), `ghp_…` (GitHub), `AKIA…` (AWS),
`xox[baprs]-…` (Slack), `-----BEGIN … PRIVATE KEY-----`, `0x` + 64 hext (private key EVM),
JWT `eyJ…`, dan `(api_key|secret|password|token) = "<≥16 char>"`. Whitelist:
`REDACTED`, `PLACEHOLDER`, `your_…`, `<placeholder>`, `xxx`.

Pola yang diblokir `unsafe_config_scan.sh`: (1) saklar uang-hidup
(`dry_run|live_trading|payments_live|auto_payout|production_payments = true`),
(2) `shuffle = true/yes` (menghancurkan auditabilitas), (3) **definisi** field data kartu
(`card_number|cvv|cvc|cvn|expiry_month|security_code|pan` diikuti `:` atau `=`),
(4) file `.env` yang ter-commit.

## 2. Kontrol yang terbukti ada (dengan lokasi kode)

| Kontrol | Bukti |
|---|---|
| Tidak ada kredensial di repo | `.env` di-ignore (`.gitignore` L2-7), hanya `.env.template` berisi *nama* variabel; scan #1 bersih termasuk `docs/` |
| Tidak ada data kartu | `.env.template` hanya menamai `PSP_*` sebagai variabel, tidak ada field PAN/CVV di skema 13 tabel; aturan #3 di atas |
| Referensi = token opaque | `src/primitives.py` `_REF_TOKEN` — kunci mencegah path traversal / prompt injection lewat id |
| PII tidak masuk log/model | `src/audit.py`: denylist field terlarang (`AUDIT_PII_FORBIDDEN_FIELD`), deteksi kebocoran (`AUDIT_PII_LEAK`), hash bergaram (`PII_SALT`) untuk identifier |
| Klasifikasi data keluar | `src/gates.py`: kelas `P0..P4`, `gate_outbound` + `apply_redaction`, matriks siapa-boleh-ekspor-kelas |
| Audit tidak bisa diubah | Trigger DB `trg_audit_events_no_update` / `_no_delete` + rantai hash (`verify_chain`, `CHAIN_DOMAIN`, `GENESIS_HASH`) |
| Fail closed | `DomainError` berkode di seluruh jalur; tidak ada `except:` telanjang; tidak ada `assert` yang hilang saat `python -O` |
| Uang tidak pakai float | `Money` = integer minor unit + ISO-4217; test `test_no_floating_point_anywhere_in_the_module` |
| Tidak ada dependency pihak ketiga | seluruh `src/` hanya Pustaka Standar Python (tidak ada permukaan supply-chain) |

## 3. Temuan

### 3.1 Diperbaiki sebelum commit (defect nyata)

| # | Kelas | Temuan | Perbaikan |
|---|---|---|---|
| F-1 | Input validation | `primitives.require_ref` menerima `bad/id"` dan `-` — id bisa berisi `/`, `"`, atau diawali tanda baca, lalu dipakai di nama file, log, dan prompt | Grammar token opaque `[A-Za-z0-9][A-Za-z0-9._:@+-]*` (`_REF_TOKEN`), diuji di `tests/test_booking_states.py::test_booking_id_must_be_a_ref` |
| F-2 | Logika uang | `Money.minor_units` mengembalikan **eksponen** (0 untuk IDR), bukan jumlah minor unit | `minor_units` = jumlah, `exponent` = eksponen; 6 test primitives terkait |
| F-3 | Integritas data | `decimal_to_str(10.50)` → `10.5` (desimal dibuang) | format kanonik dengan `places` |
| F-4 | Integritas audit | `_GENESIS_HASH = "genesis"` tidak memenuhi CHECK SHA-256 di skema | `"0" * 64` |
| F-5 | DoS/derajat payload | (dicek) kedalaman payload audit dibatasi `MAX_PAYLOAD_DEPTH` | sudah ada |

### 3.2 Informational (bukan dapat dieksploitasi)

- `src/audit.py:727` — `f"SELECT COUNT(*) FROM {AUDIT_TABLE}"`: interpolasi berasal dari
  **konstanta modul** (`AUDIT_TABLE = "audit_events"`), bukan input pengguna. Semua query
  lain memakai parameter `?`. Tetap dicatat sebagai pola yang tidak boleh diperluas ke
  nilai dinamis.
- `scripts/secret_scan.sh` + `unsafe_config_scan.sh` sebelumnya melewati `docs/*.md` dan
  `*.txt`: kredensial yang ditempel ke catatan desain tidak akan tertangkap. **Diperbaiki**
  (docs kini dipindai; whitelist yang menangani token contoh).
- Aturan cardholder-data versi lama menandai **penyebutan** nama field, sehingga denylist
  PII di `src/audit.py` terhitung sebagai temuan (false positive yang melatih tim mengabaikan
  gate). **Diperbaiki** menjadi deteksi definisi field.

### 3.3 Risky pattern (diterima, dengan alasan)

- Rantai hash audit disimpan di tabel yang sama dengan datanya: penyerang yang punya akses
  tulis penuh ke DB bisa menulis ulang seluruh rantai. Mitigasi berjenjang: trigger
  append-only, `ChainAnchor` (anchor eksternal), dan `verify_chain` untuk rekonsiliasi.
  Anker eksternal (file mingguan / WORM) belum diaktifkan → dicatat sebagai item Phase 2.

## 4. Bukti yang tidak bisa saya klaim hari ini

- **Uji PostgreSQL hidup** (`tests/test_schema.py`) di-skip tanpa `VILLA_PG_DSN`; lihat
  `pytest -rs` → 2 skipped. Parity yang terbukti hari ini adalah **parity struktural**
  (tabel/index/trigger/enum) via model skema, bukan eksekusi DDL di Postgres nyata.
  Rencana: container Postgres sekali pakai (image sudah ada lokal) + `psycopg2` untuk
  menjalankan `001_initial.pg.sql` + `002_pg_range_exclusions.pg.sql` dan membuktikan
  `ExclusionViolation`.
- `src/gates.py` dan `src/audit.py` (≈2.300 baris) belum punya test pada commit ini —
  penulisan test sedang berjalan. Klaim "aman" untuk kedua modul itu **belum** saya berikan.

## 5. Risiko residual yang tersisa

| # | Risiko | Dampak | Status |
|---|---|---|---|
| R-1 | Okupansi dilepas saat `CANCELLATION_REQUESTED`/`CANCELLATION_REVIEW`/`DISPUTED` | Unit bisa dijual ulang saat tamu masih terikat/di dalam unit → double booking | **ADR-0002 (Proposed)** — butuh keputusan pemilik sistem; dikarakterisasi oleh test `test_mid_stay_dispute_leaves_the_occupying_set` |
| R-2 | Anker rantai audit belum eksternal | Penulisan ulang rantai oleh aktor ber-privilege DB | Phase 2 |
| R-3 | Coverage `gates.py`/`audit.py` belum ada | Regresi tidak tertangkap | Sedang dikerjakan |

## 6. Kesimpulan gate

Tidak ada temuan **blocking**. Red line repo (tanpa kredensial ter-commit, tanpa data
kartu, tanpa saklar uang-hidup, tanpa shuffle, tanpa update/delete audit) terbukti
ditegakkan otomatis oleh dua skrip + trigger DB + test. Gate aman untuk melanjutkan
Phase 1; re-run wajib sebelum setiap commit berikutnya (`make check`).
