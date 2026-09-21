# Laporan Gate Mutu Rilis — villa-agency-os Phase 1

Versi: 1.0 · Tanggal: 2026-09-21 · Commit: `1ccdcce` · Reviewer: Hermes Anantara v3

Keputusan: **PASS untuk Phase 1 (Reservation Kernel)** — inti deterministik lolos semua
gerbang dan sudah punya bukti eksekusi. Dua modul pendukung (`audit.py`, `gates.py`)
**belum** masuk penilaian mutu karena test-nya belum ada (§5).

---

## 1. Kriteria penerimaan Phase 1 → bukti

| Kriteria (plan Phase 1) | Bukti | Status |
|---|---|---|
| Skema deterministik, twin SQLite ⇄ PostgreSQL | `make schema-parity` → `OK: src/schema/001_initial.pg.sql matches the generated twin` | PASS |
| Uang tidak pernah float | `Money` integer minor unit + ISO-4217; `tests/test_primitives.py`; test anti-float di `test_booking_states.py` | PASS |
| Policy versioned + immutable + fingerprint | `src/policy_engine.py` (payload deep-frozen) + 40 test | PASS |
| Aritmetika quote reproducible | `src/quote_calculator.py` + 49 test | PASS |
| **0 double booking** di jalur kernel | `src/reservation_kernel.py` overlap proof + 70 test (hold, hold→booking, hold kedaluwarsa boleh dilewati) | PASS (unit) |
| Status tidak bisa ditulis sebagai teks bebas | `src/booking_states.py`: 32 status dibaca dari enum DDL, 116 sisi legal dari 1024 pasangan, 3 guard | PASS |
| Audit append-only & tamper-evident | trigger DB + rantai hash; invarian diuji di `tests/test_schema.py` (UPDATE `audit_events` → `sqlite3.IntegrityError`) | PASS (struktur + SQLite) |
| Uji PostgreSQL hidup | container Postgres 16 sekali pakai (`127.0.0.1:55432`), `001_initial.pg.sql` + `002_pg_range_exclusions.pg.sql` diterapkan dari nol, katalog dibandingkan model SQLite, lalu dua hold tumpang tindih → `ExclusionViolation` | PASS |
| Gerbang kredensial & konfigurasi | `make check` → secret-scan 0 temuan, unsafe-scan 0 temuan | PASS |
| Kode tanpa dependency pihak ketiga | tidak ada import di luar Pustaka Standar Python di `src/` | PASS |
| Test hijau, bukan "sudah dicek" | `pytest tests/ -q` → **307 passed, 2 skipped, 1952 subtests passed** | PASS |

## 2. Angka eksekusi (bukan klaim)

```
$ python3 -m pytest tests/ -q
307 passed, 2 skipped, 1952 subtests passed in 3.03s
```

Dua `skipped` = uji PostgreSQL hidup di `tests/test_schema.py`, aktif hanya bila
`VILLA_PG_DSN` diset. Dengan server Postgres 16 nyata (container sekali pakai), angka
berubah menjadi **0 skipped, 0 failure**:

```
$ VILLA_PG_DSN=postgresql://…@127.0.0.1:55432/villa_test python3 -m pytest tests/ --junitxml=…
tests=2274, failures=0, errors=0, skipped=0, time=2.117s
```

Yang dibuktikan uji hidup itu (bukan klaim): `001_initial.pg.sql` + `002_pg_range_exclusions.pg.sql`
terpasang **dari nol** di PostgreSQL 16, nama kolom tiap tabel identik dengan model SQLite,
`ex_holds_no_overlap` dan `ex_bookings_no_overlap` ada, dua hold ACTIVE yang tumpang tindih
ditolak `ExclusionViolation`, dan stay berdempetan (`10-03 → 10-05` setelah `10-01 → 10-03`)
tetap diterima karena rentangnya half-open.

Sebaran per file (jumlah *testcase*, termasuk subtest yang dilaporkan JUnit):

| File | Testcase | Kelas uji |
|---|---|---|
| `tests/test_booking_states.py` | 85 | 8 |
| `tests/test_reservation_kernel.py` | 70 | 10 |
| `tests/test_quote_calculator.py` | 49 | 8 |
| `tests/test_policy_engine.py` | 40 | 6 |
| `tests/test_primitives.py` | 37 | 8 |
| `tests/test_schema.py` | 28 | 5 |
| **Total** | **309** | **45** |

Bukti mentah: `pytest --junitxml` → `tests=2261, failures=0, errors=0, skipped=2, time=1.339s`
(2261 = 307 lulus + 1952 subtest + 2 skip).

## 3. Gerbang pre-deploy (`make check`)

```
python3 -m compileall -q src tests scripts      # OK
python3 -m pytest tests/ -q --tb=short          # OK
SECRET-SCAN: PASS (0 findings, 35 files scanned)
UNSAFE-SCAN: PASS (0 findings, 35 files scanned)
python3 scripts/generate_pg_twin.py --check     # OK — twin identik
=== PRE-DEPLOY GATE: PASS ===
```

## 4. Defect nyata yang ditemukan & diperbaiki sebelum commit

Tidak satu pun dari daftar ini berasal dari review manual kosong — semuanya muncul dari
eksekusi test atau pembacaan ulang terhadap DDL.

| # | Modul | Defect | Dampak bila lolos | Perbaikan |
|---|---|---|---|---|
| D-1 | `primitives` | `Money.minor_units` mengembalikan eksponen (0 untuk IDR), bukan jumlah | Semua aritmetika uang salah total | pisahkan `minor_units` / `exponent` |
| D-2 | `primitives` | `decimal_to_str(places=N)` membuang desimal (`10.50`→`10.5`) | Bukti pembayaran tidak cocok | format kanonik |
| D-3 | `primitives` | `from_major`, `pct`, `scale` salah setelah D-1 | Quote salah 100× | ikut diperbaiki |
| D-4 | `primitives` | `require_ref("hold ABC")` diterima | Id berisi teks bebas → jejak audit kabur | tolak whitespace internal |
| D-5 | `primitives` | `require_ref("bad/id\"")`, `require_ref("-")` diterima | Id bisa masuk nama file/log/prompt | grammar `_REF_TOKEN` (token opaque) |
| D-6 | `primitives` | `FrozenClock.today()` hilang; `to_iso`/`parse_iso` melempar `ValueError` mentah | Jam beku tidak bisa dipakai; error tak berkode | ditambahkan, `DomainError` berkode |
| D-7 | `schema_loader` | Komentar header merusak klasifikasi statement; kolom `check_in` cocok dengan keyword `CHECK` | Parser skema salah baca tabel → parity palsu | splitter sadar komentar + keyword whole-word |
| D-8 | `policy_engine` | Payload policy bisa dimutasi setelah difingerprint | Fingerprint tidak lagi membuktikan isi | payload deep-frozen |
| D-9 | `policy_engine` | Batas limit tidak mengikuti CHECK DB | Data invalid lolos ke DB/format | `_LIMIT_BOUNDS` mencerminkan CHECK, diuji banding langsung |
| D-10 | `reservation_kernel` | Casing enum DB (`AVAILABLE`, `SELLABLE`, `ACTIVE`, `Bali`) tidak sesuai skema | INSERT gagal di DB nyata | casing diselaraskan skema |
| D-11 | `reservation_kernel` | Konversi hold menabrak hold-nya sendiri | Booking sah ditolak / jalur buntu | `_assert_no_overlap(exclude_hold_id=…)` |
| D-12 | `reservation_kernel` | `_GENESIS_HASH="genesis"` melanggar CHECK SHA-256 | Rantai audit tidak bisa ditulis | `"0"*64` |
| D-13 | `reservation_kernel` | `idempotency_key` pendek lolos ke `IntegrityError` mentah | Error tidak bisa ditangani pemanggil | validasi panjang + pesan berkode |
| D-14 | `booking_states` | Detail `DomainError` berupa `list` sementara kontrak modul = tuple | Pemanggil memutasi detail / test gagal | semua detail jadi tuple |
| D-15 | `booking_states` | Sisi graf operasional hilang: `QUALIFIED→AVAILABILITY_CONFIRMED`, `AVAILABILITY_CONFIRMED→QUOTE_SENT`, `QUOTE_SENT→HOLD_ACTIVE`, `HOLD_ACTIVE→DEPOSIT_PAID`, `IN_STAY→CANCELLATION_REQUESTED` | Ops harus mengarang status perantara → data tidak jujur | sisi ditambahkan + test khusus |
| D-16 | `booking_states` | `IN_STAY→NO_SHOW` **bukan** sisi legal, sebelumnya tidak dijaga | Menandai no-show tamu yang sedang menginap ⇒ slot dilepas ⇒ **double booking** | sisi ditolak + test + alasan terdokumentasi |
| D-17 | `booking_states` | Guard dievaluasi alfabetis ⇒ `MISSING_APPROVAL` dilaporkan sebelum `MISSING_EVIDENCE` | Kode error menyesatkan operator | presedensi evidence → reason → approval (laporan tetap alfabetis) |
| D-18 | `booking_states` | `as_dict()` mengisi `None` untuk field guard yang tidak dikirim | Request balik dari `from_mapping()` mengklaim field yang tak pernah ada | field tak terisi dihilangkan |
| D-19 | `booking_states` | Test drift index parsiil meminta `BOOKED` di kunci `missing` sementara `CONFIRMED` di `unknown` — tidak konsisten dengan perbandingan mana pun | Gate drift jadi teater | kontrak diperjelas: `unknown` = literal di luar enum, `missing` = status okupansi yang dilupakan predikat |
| D-20 | `tests` | Jurnal `booking_state_events` gagal FK karena seed pakai `bk_0001` sedangkan request `bk_000001` | Bukti append-only tidak tertulis | seed disamakan |
| D-21 | `guardrails` | `secret_scan` melewati `docs/*.md` | Kredensial di catatan desain lolos gate | docs ikut dipindai |
| D-22 | `guardrails` | Aturan cardholder menandai *penyebutan* nama field ⇒ denylist PII `audit.py` dihitung temuan | Gate penuh false positive → diabaikan tim | deteksi definisi field (`:`/`=`) |
| D-23 | `Makefile` | Teks `help` memuat `shuffle=true` ⇒ skrip keamanan menandai Makefile sendiri | Gate gagal karena dokumentasinya | teks help diubah |
| D-24 | `tests/test_schema.py` | Uji PostgreSQL hidup membersihkan data probe dengan `DELETE FROM policy_versions` — ditolak trigger append-only (dan skema diterapkan dua kali dalam satu DB ⇒ `DuplicateTable`) | Uji hidup gagal karena alasan yang salah; umpan balik menyesatkan | probe dibungkus satu transaksi `BEGIN … ROLLBACK`, skema diterapkan sekali di `setUpClass` setelah `public` di-reset, `VILLA_PG_DSN` didokumentasikan wajib menunjuk DB sekali pakai |

Catatan kejujuran: D-19, D-20, D-15/D-16 adalah koreksi **terhadap test/graf buatan agen
otomatis**, bukan terhadap kode yang sudah terbukti benar. Koreksi itu saya lakukan setelah
menelusuri DDL dan menolak dua assertion yang saling bertentangan
(`test_extra_guard_by_name_is_resolved` meminta satu panggilan yang sama lulus *dan* gagal).
Tidak ada assertion yang saya lemahkan agar test hijau: dua assertion yang bertentangan
memang diganti, sisanya diperketat.

## 5. Belum dinilai (jangan dianggap lulus)

| Item | Alasan | Rencana |
|---|---|---|
| `src/audit.py` (868 baris) | Tidak punya test pada commit ini; hanya bukti invarian trigger/rantai di level skema | Test ditulis; hasilnya harus lolos `make check` sebelum dimasukkan ke penilaian mutu |
| `src/gates.py` (1462 baris) | Sama — kode impor bersih, tapi perilaku belum diuji | Sama |
| Anker rantai audit eksternal | Belum ada | Phase 2 |
| Okupansi saat pembatalan ditinjau | Keputusan pemilik sistem | **ADR-0002 (Proposed)** |

## 6. Kesimpulan

Inti Phase 1 (primitives → skema → policy → quote → kernel → state machine) memenuhi
kriteria penerimaan dengan bukti eksekusi yang dapat direproduksi, tanpa satu pun klaim
"seharusnya jalan". Sisa pekerjaan yang jujur: test untuk `audit.py` + `gates.py`, uji
PostgreSQL hidup, dan keputusan ADR-0002. Gate mutu rilis **PASS bersyarat**: modul yang
belum ber-test tidak boleh dipakai untuk memutuskan uang atau ketersediaan sebelum §5
selesai.
