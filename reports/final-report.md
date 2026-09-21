# Laporan Akhir Phase 1 — villa-agency-os

Versi: 1.1 · Tanggal: 2026-09-21 · Commit kode: `dc6c27e` (ADR-0002 Opsi A) di atas `e83225a` · Eksekutor: Hermes Anantara v3
(Ekosistem Copy Cloning Super Dev Builders)

Repo: `/home/anashadi/villa-agency-os` — sistem operasi reservasi & agency untuk
**Villa Management & Agency** (Bali + Yogyakarta). Fondasi deterministik sebelum lapisan AI
diaktifkan.

Keputusan akhir: **Phase 1 SELESAI dan PASS** (gate keamanan + gate mutu rilis). Kedua ADR
sudah **Accepted**: ADR-0001 (storage/runtime) dan ADR-0002 **Opsi A** (okupansi saat
pembatalan ditinjau — diputuskan pemilik sistem 2026-09-21, sudah diterapkan & diverifikasi)
— lihat §6. Tidak ada keputusan bisnis yang menggantung.

---

## 1. Apa yang dibangun

| Modul | Baris | Fungsi |
|---|---|---|
| `src/primitives.py` | 668 | Uang = integer minor unit + ISO-4217 (tidak pernah float), `FrozenClock`, `IdFactory` deterministik, fingerprint, redaksi, tata bahasa error ber-kode |
| `src/schema_loader.py` | 808 | Penerap DDL + pembaca model skema (tabel/kolom/index/trigger/enum) untuk kedua dialek |
| `src/schema/001_initial.sql` ⇄ `001_initial.pg.sql` | 416 / 420 | **Twin semantik**: 13 tabel, 14 enum, 13 index, 8 trigger, 1 seed. Berkas kedua **dibangkitkan** dari yang pertama |
| `src/schema/002_pg_range_exclusions.pg.sql` | 70 | Pengerasan khusus PostgreSQL (EXCLUDE pada `daterange`) — eksplisit **di luar** pasangan twin |
| `src/policy_engine.py` | 548 | Satu objek policy versioned + fingerprint stabil; batas nilai mencerminkan CHECK di DDL |
| `src/quote_calculator.py` | 713 | Aritmetika harga deterministik: musim, long-stay, cleaning, ekstra, deposit, pajak, service |
| `src/reservation_kernel.py` | 1553 | Hold → booking → jurnal; **inti 0 double booking** |
| `src/booking_states.py` | 1051 | State machine 32 status (116 sisi legal dari 1024 pasangan) + 3 guard |
| `src/audit.py` | 878 | Jurnal audit ber-rantai hash, append-only, penjaga PII, anker eksternal |
| `src/gates.py` | 1466 | Gerbang contract-pack, ketersediaan, unit, keluar (redaksi P0..P4) |
| `tests/` (8 berkas) | 7241 | 586 test lulus (2814 subtest); 589 lulus dengan PostgreSQL hidup |
| `docs/` (7 dokumen + 2 ADR) | ~760 | Charter, Property Contract Pack, Policy Matrix, Data Model, State Machine, klasifikasi data, retensi/insiden |
| `reports/` (3 laporan) | ~380 | Gate keamanan, gate mutu rilis, serah terima (berkas ini) |

Cakupan domain yang melekat di kode: timezone per properti (`Asia/Makassar` untuk Bali,
`Asia/Jakarta` untuk Yogyakarta), PBJT (pajak daerah Bali), deposit & service fee, bukti
ketersediaan bertingkat (A–D) dengan aturan "hanya A/B boleh mengonfirmasi".

## 2. Bukti eksekusi (dapat direproduksi)

```
$ python3 -m pytest tests/
586 passed, 3 skipped, 2814 subtests passed in 2.52s
3 skipped = uji PostgreSQL hidup (aktif bila VILLA_PG_DSN diset)

$ VILLA_PG_DSN=postgresql://…@127.0.0.1:55433/villa_test python3 -m pytest tests/
589 passed, 2827 subtests passed in 3.27s  (PostgreSQL 16, container sekali pakai; 0 skip)

$ make check
compile OK · pytest OK · SECRET-SCAN PASS (0 findings, 40 file) · UNSAFE-SCAN PASS (0 findings, 40 file)
twin parity OK · === PRE-DEPLOY GATE: PASS ===
```

Yang dibuktikan uji PostgreSQL hidup: kedua berkas PostgreSQL terpasang dari nol, katalog
identik dengan model SQLite, dan dua hold ACTIVE yang tumpang tindih ditolak
`ExclusionViolation` — sedangkan stay berdempetan tetap diterima karena rentang half-open.

## 3. Defect nyata yang ditemukan & diperbaiki (30 tercatat)

Enam yang paling berkonsekuensi:

1. **Uang**: `Money.minor_units` mengembalikan eksponen (0 untuk IDR), bukan jumlah minor
   unit → setiap perhitungan uang salah.
2. **Double booking**: `IN_STAY → NO_SHOW` tadinya sisi legal; no-show bukan status okupansi,
   jadi slot bisa dilepas saat tamu sedang menginap. Sekarang ditolak.
3. **PII**: `audit._normalise_key` tidak memecah camelCase → kunci `guestName` lolos dari
   registry field terlarang; nama tamu tersimpan apa adanya di jurnal.
4. **Referensi**: `require_ref` menerima `bad/id"` dan `-`, padahal id dipakai di nama file,
   log, dan prompt.
5. **Integritas audit**: `prev_hash` hanya dicek panjangnya (64), bukan bentuk hex.
6. **Fail-closed**: `ContractPack.from_row` crash saat kolom `items` rusak, bukan menolak.

Daftar lengkap D-1..D-30: `reports/release-quality-gate.md` §4. Tidak ada defect yang
"diperbaiki" dengan melonggarkan assertion.

## 4. Keputusan arsitektur

| ADR | Isi | Status |
|---|---|---|
| ADR-0001 | SQLite untuk dev/test, PostgreSQL untuk produksi; berkas skema kembar wajib setara, dijaga test parity | **Accepted** |
| ADR-0002 | Okupansi saat `CANCELLATION_REQUESTED` / `CANCELLATION_REVIEW` / `DISPUTED` | **Accepted (Opsi A)** — diputuskan 2026-09-21, diterapkan & diverifikasi (§6) |

Pendukung keputusan lain: berkas PostgreSQL **dibangkitkan** dari berkas SQLite
(`scripts/generate_pg_twin.py`) sehingga tidak bisa melenceng diam-diam; uang selalu integer
minor unit + ISO-4217; policy ada di satu objek versioned ber-fingerprint (payload dibekukan
agar fingerprint stabil).

## 5. Cara menjalankan

```
make test            # seluruh suite
make check           # gerbang pre-deploy: syntax + test + secret + unsafe + parity twin
make schema-parity   # buktikan twin Postgres identik dengan SQLite
make pg-twin         # bangkitkan ulang berkas twin (setelah mengubah DDL)

# uji PostgreSQL hidup (DB sekali pakai — setUpClass me-reset skema public)
docker run -d --rm -p 127.0.0.1:55432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_USER=villa_test -e POSTGRES_DB=villa_test postgres:16-alpine
VILLA_PG_DSN=postgresql://villa_test@127.0.0.1:55432/villa_test python3 -m pytest tests/
```

Aturan kredensial repo: `.env` tidak pernah di-commit, kredensial asli tidak pernah ditempel
ke chat/tiket/dokumen/AI co-pilot, nilai contoh hanya `[REDACTED]`/placeholder.

## 6. ADR-0002 — diputuskan: Opsi A diterapkan

**Keputusan pemilik sistem (2026-09-21): Opsi A.** `CANCELLATION_REQUESTED`,
`CANCELLATION_REVIEW`, dan `DISPUTED` sekarang **menahan** tanggal: unit tetap terkunci
sampai pembatalannya diputuskan (`→ CANCELLED`) atau sengketa ditutup. Sebelumnya ketiganya
melepas slot, sehingga booking yang sedang ditinjau — atau tamu yang masih **fisik di dalam
villa** — bisa dijual ulang (risiko double booking).

Perubahan (5 berkas kode/DDL + 3 berkas test): predikat `uq_bookings_active_slot` 5 → 8
status, `OCCUPYING_STATES`, `OCCUPYING_BOOKING_STATES`, dan `ex_bookings_no_overlap`
(PostgreSQL) ikut disamakan; twin PostgreSQL diregenerasi dan parity-nya hijau.

Ditegakkan di **tiga lapis**, masing-masing dengan testnya sendiri:

| Lapis | Test |
|---|---|
| Pemeriksaan okupansi kernel | `test_reservation_kernel.py::TestNoDoubleBooking::test_cancellation_under_review_still_blocks_a_new_hold` |
| Index parsial SQLite (`uq_bookings_active_slot`) | `test_booking_states.py::TestAgainstSqlite::test_review_and_dispute_bindings_are_refused_by_the_database` + `test_a_decided_cancellation_does_release_the_slot` |
| EXCLUDE PostgreSQL (`ex_bookings_no_overlap`) | `test_schema.py::TestLivePostgres::test_booking_exclusion_holds_the_dates_during_review_and_dispute` |

Biaya yang diterima sadar: unit bisa tertahan selama review; itu dibayar dengan
**mempercepat keputusan pembatalan**, bukan dengan membuka slot sebelum ada keputusan.
Kalau keputusan ini ingin dibatalkan, cukup kembalikan predikat 8 → 5 di tiga tempat dan
hapus lima test di atas — tidak ada data lama yang menjadi tidak valid. Rincian: `docs/adr/0002-occupancy-during-cancellation-review.md`.

## 7. Yang belum dikerjakan (Phase 2, jangan diklaim ada)

1. Penerbitan anker rantai audit ke sistem kedua (file mingguan / WORM).
2. Uji redaksi `apply_redaction` per kelas data P0..P4 secara menyeluruh.
3. Lapisan AI (narrative/intake) — sengaja ditunda sampai fondasi deterministik selesai.
4. Uji beban/volume, observability (metrik/panel), dan SOP operasional harian.
5. Integrasi kanal nyata (WhatsApp/OTA) — belum ada kredensial apa pun di repo ini.

## 8. Jejak commit

```
dc6c27e  feat(schema,kernel): ADR-0002 Opsi A — pembatalan ditinjau & sengketa menahan slot (5 test, termasuk PostgreSQL hidup)
41a7b1a  docs(reports): tutup gate Phase 1 — laporan akhir, angka terverifikasi, 6 defect baru
e83225a  test(audit,gates): 276 test baru + 3 perbaikan modul; verifikasi mandiri atas hasil delegasi
9ae52e1  test(schema): buktikan 001+002 di PostgreSQL 16 nyata; laporan gate keamanan & mutu
1ccdcce  fix(guardrails): scan docs for secrets, mata-rantai false positive, artefak .tmp
8c4c777  feat(kernel): Phase 1 reservation kernel — deterministik, fail-closed, parity SQLite⇄PostgreSQL
```

Catatan proses: dua suite terakhir (`test_audit.py`, `test_gates.py`) ditulis oleh agen
otomatis yang saya delegasikan, lalu **saya verifikasi sendiri**: dua test di dalamnya gagal
saat dijalankan (klaim agen "semua hijau" tidak akurat) dan tiga perubahan modul yang mereka
lakukan di luar permintaan saya direview satu per satu. Ringkasan agen tidak pernah dipakai
sebagai bukti.
