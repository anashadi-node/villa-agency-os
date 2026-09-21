# Laporan Gate Keamanan & Privasi — villa-agency-os

Versi: 1.2 · Tanggal: 2026-09-21 · Commit kode: `dc6c27e` (R-1 ditutup oleh ADR-0002 Opsi A) · Reviewer: Hermes Anantara v3

Lingkup: seluruh repo `/home/anashadi/villa-agency-os` (37 file ter-track). Kode milik
pengguna; tidak ada target pihak ketiga yang diaudit, tidak ada eksploit yang dikembangkan.

Keputusan: **PASS untuk Phase 1 (Reservation Kernel)**, termasuk `audit.py` dan `gates.py`
yang pada versi 1.0 laporan ini belum punya test (dan karenanya belum boleh disebut aman).
Risiko residual tercatat di §5.

---

## 1. Metode (semua dapat direproduksi)

| # | Perintah | Hasil |
|---|---|---|
| 1 | `bash scripts/secret_scan.sh` | PASS — 0 temuan / 40 file (termasuk `docs/`, `reports/`) |
| 2 | `bash scripts/unsafe_config_scan.sh` | PASS — 0 temuan / 37 file |
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
| F-6 | **Kebocoran PII** | `audit._normalise_key` tidak memecah batas camelCase: kunci `guestName` menjadi `guestname` sehingga tidak cocok dengan registry field terlarang maupun registry pseudonim — **nama tamu tersimpan apa adanya** di jurnal (tidak ada pola nilai yang bisa menangkapnya) | batas camelCase/PascalCase/akronim dipecah sebelum tanda baca dilipat; test per varian (`Guest-Email`, `guestName`, `GUEST_EMAIL`) |
| F-7 | Integritas rantai audit | `compute_event_hash` hanya memeriksa *panjang* `prev_hash` (64), bukan bentuk hex | pemeriksaan karakter hex — tautan non-hex kini ditolak |
| F-8 | Fail-closed gate | `ContractPack.from_row` melempar exception saat kolom `items` bukan container | container rusak = pack kosong = **tolak**, bukan crash |

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
- `unsafe_config_scan.sh` melewati `reports/*` sejak laporan ini mulai **mengutip** aturannya
  (`shuffle=true`, `dry_run=false`) — prosa bukan konfigurasi. Kebocoran kredensial tetap
  dipindai `secret_scan.sh` di seluruh berkas, termasuk `reports/`.

### 3.3 Risky pattern (diterima, dengan alasan)

- Rantai hash audit disimpan di tabel yang sama dengan datanya: penyerang yang punya akses
  tulis penuh ke DB bisa menulis ulang seluruh rantai. Mitigasi berjenjang: trigger
  append-only, `ChainAnchor` (anchor eksternal), dan `verify_chain` untuk rekonsiliasi.
  Anker eksternal (file mingguan / WORM) belum diaktifkan → dicatat sebagai item Phase 2.

## 4. Yang masih tidak bisa saya klaim

- **Cakupan redaksi per kelas data di `gates.py`** (`apply_redaction` untuk P0..P4) belum
  diuji penuh; suite `tests/test_gates.py` menguji gerbang, kode alasan, dan klasifikasi.
- **Uji beban / volume** belum ada — di luar lingkup Phase 1, tapi jangan diklaim ada.
- **Anker audit eksternal** (file mingguan / WORM) belum diterbitkan; hanya mekanisme
  verifikasinya yang ada dan teruji.

Sebelumnya (versi 1.0) dua butir lain juga belum bisa diklaim, kini **sudah**:
uji PostgreSQL hidup dan test untuk `audit.py`/`gates.py` (lihat §1 dan
`reports/release-quality-gate.md` §2).

## 5. Risiko residual yang tersisa

| # | Risiko | Dampak | Status |
|---|---|---|---|
| R-1 | Okupansi dilepas saat `CANCELLATION_REQUESTED`/`CANCELLATION_REVIEW`/`DISPUTED` | Unit bisa dijual ulang saat tamu masih terikat/di dalam unit → double booking | **DITUTUP.** ADR-0002 Opsi A diputuskan pemilik sistem 2026-09-21: predikat occupying 5 → 8 status di tiga lapis penegakan (kernel, index parsial SQLite, `ex_bookings_no_overlap` PostgreSQL), dibuktikan 5 test termasuk di PostgreSQL 16 hidup |
| R-2 | Anker rantai audit belum eksternal | Penulisan ulang rantai oleh aktor ber-privilege DB | Phase 2 |
| R-3 | Okupansi/redaksi: jalur `apply_redaction` per kelas data belum diuji penuh | Data keluar bisa teredaksi tidak sesuai kelasnya | Item iterasi berikutnya (dicatat di `reports/release-quality-gate.md` §5) |

## 6. Kesimpulan gate

Tidak ada temuan **blocking**. Red line repo (tanpa kredensial ter-commit, tanpa data
kartu, tanpa saklar uang-hidup, tanpa shuffle, tanpa update/delete audit) terbukti
ditegakkan otomatis oleh dua skrip + trigger DB + test. Gate aman untuk melanjutkan
Phase 1; re-run wajib sebelum setiap commit berikutnya (`make check`).
