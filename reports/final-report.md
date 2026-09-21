# Laporan Akhir Phase 1 — villa-agency-os

Versi: 1.0 · Tanggal: 2026-09-21 · Commit kode: `e83225a` · Eksekutor: Hermes Anantara v3
(Ekosistem Copy Cloning Super Dev Builders)

Repo: `/home/anashadi/villa-agency-os` — sistem operasi reservasi & agency untuk
**Villa Management & Agency** (Bali + Yogyakarta). Fondasi deterministik sebelum lapisan AI
diaktifkan.

Keputusan akhir: **Phase 1 SELESAI dan PASS** (gate keamanan + gate mutu rilis). Satu
keputusan bisnis masih menunggu pemilik sistem: **ADR-0002** (okupansi saat pembatalan
ditinjau) — lihat §6.

---

## 1. Apa yang dibangun

| Modul | Baris | Fungsi |
|---|---|---|
| `src/primitives.py` | 668 | Uang = integer minor unit + ISO-4217 (tidak pernah float), `FrozenClock`, `IdFactory` deterministik, fingerprint, redaksi, tata bahasa error ber-kode |
| `src/schema_loader.py` | 808 | Penerap DDL + pembaca model skema (tabel/kolom/index/trigger/enum) untuk kedua dialek |
| `src/schema/001_initial.sql` ⇄ `001_initial.pg.sql` | 412 / 416 | **Twin semantik**: 13 tabel, 14 enum, 13 index, 8 trigger, 1 seed. Berkas kedua **dibangkitkan** dari yang pertama |
| `src/schema/002_pg_range_exclusions.pg.sql` | 69 | Pengerasan khusus PostgreSQL (EXCLUDE pada `daterange`) — eksplisit **di luar** pasangan twin |
| `src/policy_engine.py` | 548 | Satu objek policy versioned + fingerprint stabil; batas nilai mencerminkan CHECK di DDL |
| `src/quote_calculator.py` | 713 | Aritmetika harga deterministik: musim, long-stay, cleaning, ekstra, deposit, pajak, service |
| `src/reservation_kernel.py` | 1548 | Hold → booking → jurnal; **inti 0 double booking** |
| `src/booking_states.py` | 1038 | State machine 32 status (116 sisi legal dari 1024 pasangan) + 3 guard |
| `src/audit.py` | 878 | Jurnal audit ber-rantai hash, append-only, penjaga PII, anker eksternal |
| `src/gates.py` | 1466 | Gerbang contract-pack, ketersediaan, unit, keluar (redaksi P0..P4) |
| `tests/` (8 berkas) | 7144 | 583 test lulus (2800 subtest) |
| `docs/` (7 dokumen + 2 ADR) | ~760 | Charter, Property Contract Pack, Policy Matrix, Data Model, State Machine, klasifikasi data, retensi/insiden |
| `reports/` (3 laporan) | ~380 | Gate keamanan, gate mutu rilis, serah terima (berkas ini) |

Cakupan domain yang melekat di kode: timezone per properti (`Asia/Makassar` untuk Bali,
`Asia/Jakarta` untuk Yogyakarta), PBJT (pajak daerah Bali), deposit & service fee, bukti
ketersediaan bertingkat (A–D) dengan aturan "hanya A/B boleh mengonfirmasi".

## 2. Bukti eksekusi (dapat direproduksi)

```
$ python3 -m pytest tests/
583 passed, 2 skipped, 2800 subtests passed in 2.53s
2 skipped = uji PostgreSQL hidup (aktif bila VILLA_PG_DSN diset)

$ VILLA_PG_DSN=postgresql://…@127.0.0.1:55432/villa_test python3 -m pytest tests/
tests=3398, failures=0, errors=0, skipped=0  (PostgreSQL 16, container sekali pakai)

$ make check
compile OK · pytest OK · SECRET-SCAN PASS (37 file) · UNSAFE-SCAN PASS (37 file)
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
| ADR-0002 | Okupansi saat `CANCELLATION_REQUESTED` / `CANCELLATION_REVIEW` / `DISPUTED` | **Proposed** — butuh keputusan Anda (§6) |

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

## 6. Keputusan yang menunggu Anda

**ADR-0002.** `DISPUTED`, `CANCELLATION_REQUESTED`, dan `CANCELLATION_REVIEW` bukan status
okupansi. Konsekuensinya: booking yang pembatalannya sedang ditinjau — atau tamu yang sedang
bersengketa **di dalam villa** — melepas slotnya untuk dijual ulang (risiko double booking).

- **Opsi A (rekomendasi)**: perluas predikat index okupansi menjadi 8 status. Unit tetap
  terblokir sampai hasil pembatalan diputuskan (fail-closed). Biayanya peluang, bukan risiko.
- **Opsi B**: larang perpindahan dari status okupansi ke tiga status itu. Lebih murah, tapi
  operasional kehilangan kemampuan mencatat pembatalan/sengketa di tengah masa inap.

Perilaku saat ini sudah dikunci oleh test karakterisasi
(`test_mid_stay_dispute_leaves_the_occupying_set`) supaya tidak berubah tanpa sengaja.

## 7. Yang belum dikerjakan (Phase 2, jangan diklaim ada)

1. Penerbitan anker rantai audit ke sistem kedua (file mingguan / WORM).
2. Uji redaksi `apply_redaction` per kelas data P0..P4 secara menyeluruh.
3. Lapisan AI (narrative/intake) — sengaja ditunda sampai fondasi deterministik selesai.
4. Uji beban/volume, observability (metrik/panel), dan SOP operasional harian.
5. Integrasi kanal nyata (WhatsApp/OTA) — belum ada kredensial apa pun di repo ini.

## 8. Jejak commit

```
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
