# Laporan Gate Mutu Rilis — villa-agency-os Phase 1

Versi: 1.2 · Tanggal: 2026-09-21 · Commit kode: `dc6c27e` (ADR-0002 Opsi A) di atas `e83225a` · Reviewer: Hermes Anantara v3

Keputusan: **PASS untuk Phase 1 (Reservation Kernel)** — termasuk `audit.py` dan `gates.py`,
yang pada versi 1.0 laporan ini belum dinilai karena belum punya test. Test untuk keduanya
sudah ada, dijalankan, dan **dua kegagalan yang saya temukan di dalamnya sudah diperbaiki**
(bukan dilonggarkan) sebelum masuk penilaian.

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
| Audit append-only & tamper-evident | trigger DB + rantai hash; invarian diuji di `tests/test_schema.py` (UPDATE `audit_events` → `sqlite3.IntegrityError`) dan 140 test perilaku di `tests/test_audit.py` (rantai, anchor, PII, append-only) | PASS |
| Uji PostgreSQL hidup | container Postgres 16 sekali pakai, `001_initial.pg.sql` + `002_pg_range_exclusions.pg.sql` diterapkan dari nol, katalog dibandingkan model SQLite; dua hold tumpang tindih → `ExclusionViolation`; **ADR-0002**: booking `CANCELLATION_REQUESTED`/`CANCELLATION_REVIEW`/`DISPUTED` menolak re-sale lewat `ex_bookings_no_overlap`, `CANCELLED` melepasnya, dan rentang tetap half-open | PASS |
| Gerbang kredensial & konfigurasi | `make check` → secret-scan 0 temuan, unsafe-scan 0 temuan | PASS |
| Kode tanpa dependency pihak ketiga | tidak ada import di luar Pustaka Standar Python di `src/` | PASS |
| Test hijau, bukan "sudah dicek" | `pytest tests/` → **586 passed, 3 skipped, 2814 subtests passed**; dengan PostgreSQL hidup → **589 passed, 2827 subtests passed, 0 failure, 0 error, 0 skip** | PASS |

## 2. Angka eksekusi (bukan klaim)

```
$ python3 -m pytest tests/
586 passed, 3 skipped, 2814 subtests passed in 2.52s
```

Tiga `skipped` = tiga metode `TestLivePostgres` di `tests/test_schema.py`, aktif hanya bila
`VILLA_PG_DSN` diset. Dengan server Postgres 16 nyata (container sekali pakai), angka
berubah menjadi **0 skipped, 0 failure**:

```
$ VILLA_PG_DSN=postgresql://…@127.0.0.1:55433/villa_test python3 -m pytest tests/ --junitxml=…
589 passed, 2827 subtests passed in 3.27s   (JUnit: tests=3416, failures=0, errors=0, skipped=0)
```

Yang dibuktikan uji hidup itu (bukan klaim): `001_initial.pg.sql` + `002_pg_range_exclusions.pg.sql`
terpasang **dari nol** di PostgreSQL 16, nama kolom tiap tabel identik dengan model SQLite,
`ex_holds_no_overlap` dan `ex_bookings_no_overlap` ada, dua hold ACTIVE yang tumpang tindih
ditolak `ExclusionViolation`, dan stay berdempetan (`10-03 → 10-05` setelah `10-01 → 10-03`)
tetap diterima karena rentangnya half-open. Sejak ADR-0002, probe yang sama juga membuktikan
**booking** yang sedang ditinjau/sengketa menolak re-sale di database nyata.

Sebaran per berkas (entri *testcase* JUnit, termasuk subtest yang dilaporkan terpisah):

| File | Testcase |
|---|---|
| `tests/test_audit.py` | 140 |
| `tests/test_gates.py` | 136 |
| `tests/test_booking_states.py` | 87 |
| `tests/test_reservation_kernel.py` | 71 |
| `tests/test_quote_calculator.py` | 49 |
| `tests/test_policy_engine.py` | 40 |
| `tests/test_primitives.py` | 37 |
| `tests/test_schema.py` | 29 |
| **Total** | **589** |

589 = 586 lulus + 3 skip. `tests/test_audit.py` memuat 140 metode/314 assertion dan
`tests/test_gates.py` 136 metode/306 assertion (keduanya dihitung langsung dari berkas,
bukan dari laporan agen yang menulisnya).

## 3. Gerbang pre-deploy (`make check`)

```
python3 -m compileall -q src tests scripts      # OK
python3 -m pytest tests/ -q --tb=short          # OK (586 lulus)
SECRET-SCAN: PASS (0 findings, 40 files scanned)
UNSAFE-SCAN: PASS (0 findings, 40 files scanned)
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
| D-25 | `audit` | `_normalise_key` tidak memecah batas camelCase/PascalCase: kunci `guestName` menjadi `guestname`, tidak cocok dengan registry field terlarang maupun registry pseudonim | **Nama tamu tersimpan apa adanya** di jurnal audit (kebocoran PII diam-diam; tidak ada pola nilai yang bisa menangkapnya) | batas camelCase/PascalCase/akronim dipecah sebelum tanda baca dilipat + test per varian |
| D-26 | `audit` | `compute_event_hash` hanya memeriksa *panjang* `prev_hash` (64), bukan bentuk hex | Tautan rantai bisa dibangun dari string non-hex → verifikasi kehilangan makna | pemeriksaan karakter hex |
| D-27 | `gates` | `ContractPack.from_row` melempar exception saat kolom `items` bukan container (mis. nilai skalar) | Gate contract-pack crash, bukan menolak → perilaku tidak fail-closed | container rusak diperlakukan sebagai pack kosong = tolak |
| D-28 | `tests/test_audit.py` | Test anchor "panjang sama, head beda" menamper kolom `hash` ⇒ `verify_hash` lebih dulu melempar `AuditChainBroken`, cabang anchor tidak pernah tercapai | Cabang `AuditAnchorMismatch` (deteksi riwayat ditulis ulang) **tidak punya bukti uji** padahal diklaim teruji | skenario dibangun benar: log kedua dengan jumlah event sama tetapi isi berbeda, dibandingkan ke anchor log pertama |
| D-29 | `tests/test_audit.py` | `test_events_filter_by_entity` memakai `entity_type` bawaan `"booking"` untuk `entity_id="villa_0007"`, lalu mengharapkan hasil dari `for_entity("villa", …)` — dan beberapa baris kemudian menuntut `events(entity_type="villa") == []` | Test gagal karena asumsi yang salah, bukan karena bug modul | filter diberi tipe yang benar + kasus negatif ditambahkan (filter = pasangan tipe+id); assertion lanjutan dibuat turunan dari baris yang benar-benar ada |
| D-30 | `guardrails` | `unsafe_config_scan` menandai `reports/*.md` karena laporan ini **mengutip** aturan (`shuffle=true`, `dry_run=false`) | Gate gagal karena laporannya sendiri | prosa dikecualikan; kebocoran kredensial tetap dipindai `secret_scan` di seluruh berkas |

Catatan kejujuran: D-15/D-16, D-19, D-20, D-28, D-29 adalah koreksi **terhadap test/graf buatan
agen otomatis**, bukan terhadap kode yang sudah terbukti benar. Koreksi itu saya lakukan setelah
menelusuri DDL dan menolak dua assertion yang saling bertentangan
(`test_extra_guard_by_name_is_resolved` meminta satu panggilan yang sama lulus *dan* gagal).
Tidak ada assertion yang saya lemahkan agar test hijau: assertion yang bertentangan memang
diganti, sisanya diperketat (D-29 dan D-28 masing-masing menambah kasus uji baru).

Catatan kejujuran kedua: laporan dua agen yang menulis suite `audit`/`gates` menyatakan
**"313 passed"** dan **"443 passed"** — keduanya tidak akurat. Yang benar saat saya jalankan
sendiri: **2 gagal, 581 lulus** (D-28, D-29). Modul `audit.py` dan `gates.py` juga diubah
oleh agen di luar permintaan eksplisit saya; ketiga perubahan itu (D-25..D-27) saya review
satu per satu dan saya nilai perbaikan nyata, bukan pelonggaran. Ringkasan agen yang
menyatakan "semua hijau" tidak pernah saya pakai sebagai bukti apa pun.

## 5. Belum dinilai (jangan dianggap lulus)

| Item | Alasan | Rencana |
|---|---|---|
| Anker rantai audit eksternal | Sudah ada `ChainAnchor` + `verify(anchor=…)` teruji, tetapi belum ada penerbitannya ke sistem kedua (file/sistem luar) | Phase 2 |
| Okupansi saat pembatalan ditinjau | Keputusan pemilik sistem | **SELESAI** — ADR-0002 Opsi A diputuskan 2026-09-21, diterapkan & diverifikasi (5 test, termasuk PostgreSQL hidup) |
| Cakupan uji `gates.py` terhadap seluruh kode jalan | Suite baru menguji gerbang/kode alasan/klasifikasi; jalur `apply_redaction` per kelas data belum diuji sepenuhnya | Tambah test redaksi per kelas di iterasi berikutnya |
| Uji beban/volume | Belum ada (kernel deterministik, bukan target Phase 1) | Phase 2 |

## 6. Kesimpulan

Inti Phase 1 (primitives → skema → policy → quote → kernel → state machine → audit → gates)
memenuhi kriteria penerimaan dengan bukti eksekusi yang dapat direproduksi, **tanpa satu pun
klaim "seharusnya jalan"**:

- 586 test lulus / 3 skip secara lokal (2814 subtest); 589 lulus dengan PostgreSQL 16 hidup, 0 gagal, 0 skip.
- Parity twin SQLite ⇄ PostgreSQL dibuktikan pada engine nyata, termasuk penolakan dua hold
  tumpang tindih lewat `ExclusionConstraint` PostgreSQL.
- 30 defect nyata ditemukan dan diperbaiki sebelum commit; 6 di antaranya berpotensi
  menyebabkan kebocoran PII, salah hitung uang, atau **double booking**.
- Appendix: `reports/security-gate.md` (gate keamanan & privasi) dan `reports/final-report.md`
  (serah terima Phase 1).

Gate mutu rilis **PASS**, dengan dua hal yang tetap harus disebut apa adanya: cakupan redaksi
per kelas data di `gates.py` belum penuh, dan belum ada uji beban. ADR-0002 sudah **tidak**
lagi menggantung: Opsi A diputuskan pemilik sistem 2026-09-21 dan sudah diterapkan.
