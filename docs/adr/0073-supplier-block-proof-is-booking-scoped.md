# ADR-0073 — Bukti blok supplier harus menempel pada booking, bukan pada unit

- Status: **ACCEPTED** (arahan teknis owner, 2026-11-18)
- Date: 2026-11-18
- Deciders: Mas Anas (owner) · divisi engineering Villa Agency OS
- Melengkapi: ADR-0069 (gerbang `CONFIRMED`) · ADR-0033 §2.6 (urutan konfirmasi)
- Spec: `src/schema/019_supplier_block_proofs.sql` · `src/reservation_kernel.py`
  (`record_supplier_block_proof`, `revoke_supplier_block_proof`, `confirm_booking`) ·
  `src/dashboard.py` (`_confirmation_table`, `post_confirm_request`) ·
  `tests/test_supplier_block_proof.py` · `scripts/go_live_smoke.py`

## Masalah

ADR-0069 menetapkan `CONFIRMED` hanya setelah uang terverifikasi **dan** bukti blok villa.
Bukti itu diambil dari `availability_evidence`, yang menjawab pertanyaan tingkat **unit**
("apakah villa ini kosong saat ditanya?") dan tidak menyimpan `booking_id`, `hold_id`,
`check_in`, maupun `check_out`. Akibatnya tiga hal tidak bisa dibuktikan, padahal
statusnya dipakai untuk voucher, pre-arrival, dan KPI okupansi:

1. Bukti itu memblokir **tanggal booking tersebut** — bukan tanggal lain.
2. Bukti itu milik **booking ini** — bukan booking lain di unit yang sama.
3. Satu jawaban `AVAILABLE` tidak dipakai ulang untuk menutup dua booking berbeda.

`confirm_booking` juga tidak memeriksa `valid_until` saat konfirmasi, dan tidak tahu
supplier mana yang menerbitkan bukti. `occupancy_ranges` ada di skema tetapi tidak pernah
dibaca jalur konfirmasi. Stage D karena itu menahan aksi konfirmasi dan melaporkan gap ini
sebagai HIGH, bukan mengeksposnya.

## Keputusan

1. **Tabel aditif `supplier_block_proofs`.** Satu baris mengikat: satu booking, satu
   agency, satu unit, satu jendela inap (`check_in`/`check_out`), satu supplier, satu
   relasi supplier-villa, satu baris `availability_evidence`, opsional satu
   `evidence_objects`, metode verifikasi, dan **nama manusia yang memverifikasi**.
2. **Verifikasi tidak boleh diturunkan dari dokumen.** `verified_by` +
   `verification_method` + `verified_at` wajib; unggahan hanya provenance
   (`evidence_object_id`, wajib berstatus `STORED` bila diisi).
3. **Reuse dicegah struktur, bukan ingatan reviewer.** `uq_sbp_evidence` (UNIQUE
   `evidence_id`) berarti satu jawaban supplier hanya bisa menopang satu booking, selamanya.
   `ux_sbp_active_booking` (partial unique pada `booking_id` WHERE `revoked_at IS NULL`)
   berarti satu booking hanya punya satu bukti hidup.
4. **Immutability + lifecycle.** Baris tidak diubah. Bukti yang salah dicabut lewat
   `revoke_supplier_block_proof` (alasan wajib, terekam), dan penggantinya baris baru.
   Pencabutan tidak menghapus apa pun, jadi jejaknya tetap bisa diaudit.
5. **`confirm_booking` menerima `block_proof_id`, bukan `block_evidence_id`.** Kernel
   memeriksa ulang di dalam transaksinya: agency, booking, unit, jendela inap, belum
   dicabut, belum kedaluwarsa, supplier masih `ACTIVE`, relasi masih `ACTIVE` dan mencakup
   unit/properti, serta evidence masih level A/B dengan `can_confirm`. Semua gagal-tertutup
   dengan kode bernama (`BLOCK_PROOF_*`). Bukti lama tidak "diperbaiki" di jalan.
6. **Konfirmasi tetap satu pintu:** `ReservationKernel.confirm_booking`, hold
   `CONFIRMED` → `CONVERTED`, event + audit atomik, replay idempoten. `confirm_from_hold`
   tetap pensiun dan tetap menolak.

### Alternatif yang ditolak

- **Menambah kolom `booking_id`/`check_in`/`check_out` ke `availability_evidence`.**
  Ditolak: tabel itu dipakai jalur quote/hold/check yang tidak punya booking, sehingga
  kolomnya harus nullable dan "bukti tanpa booking" menjadi keadaan sah — persis ambiguitas
  yang mau dihilangkan.
- **Menganggap `AVAILABLE` + level A/B + freshness sebagai bukti blok.** Ditolak: itu
  pernyataan ketersediaan sesaat, bukan komitmen memblokir tanggal tertentu untuk tamu
  tertentu. Ini juga yang membuat gap Stage D berbahaya.
- **Menaruh bukti di `evidence_objects`.** Ditolak: tabel itu untuk file yang masuk dari
  luar (karantina, scan, klasifikasi), bukan untuk pernyataan bisnis terverifikasi manusia.
  `evidence_objects` tetap dipakai sebagai provenance opsional.
- **Tabel baru untuk dokumen mentah.** Ditolak: bertentangan dengan aturan "jangan simpan
  dokumen sensitif di tabel baru tanpa pembatasan".

## Konsekuensi

- Booking lama yang sudah `CONFIRMED` di bawah aturan lama **tidak diubah**: migrasi 019
  aditif dan tidak menulis ulang fakta historis. Booking yang belum dikonfirmasi tidak bisa
  dikonfirmasi lagi tanpa bukti baru — arah gagal-tertutup yang disengaja.
- Operator melihat status bukti (`VERIFIED`/`MISSING`/`EXPIRED`/`REVOKED`), referensi
  evidence, dan siapa yang memverifikasi, di halaman kalender unit; tombol konfirmasi hanya
  muncul saat bukti hidup dan hanya untuk Reservation/Operations.
- Satu langkah operasional baru: merekam bukti blok sebelum konfirmasi. Itu memang
  pekerjaan yang sebelumnya tidak ada di sistem, bukan biaya yang ditambahkan.
- **Belum** menyelesaikan drift loader migrasi PostgreSQL dan adaptor kernel SQLite-only;
  kesiapan produksi PostgreSQL tetap tidak diklaim.
- Requirement kandidat (PROPOSED, belum dipromosikan): FR-905 bukti blok supplier
  per-booking · DATA-214 tabel `supplier_block_proofs` · SEC-131 verifikasi manusia wajib
  untuk bukti blok · UX-88 panel konfirmasi di kalender unit · TEST-402 acceptance A–S.

## Review independen (2026-11-19) dan perbaikan

Satu review terbatas dijalankan model **Claude Sonnet 5.5** (konteks terbatas: diff
kernel, DDL, migrasi 019, `confirm_booking`, ADR ini, daftar tes). Temuan yang
terverifikasi di repo, dan yang diubah karena itu:

1. **HIGH — jawaban restricted diterima sebagai blok.** `record_supplier_block_proof`
   dan `confirm_booking` menerima `AVAILABLE_BUT_RESTRICTED`, padahal jawaban itu justru
   menyatakan tanggal membawa syarat yang belum disetujui supplier. Sekarang hanya
   `AVAILABLE` yang dihitung sebagai bukti blok; jawaban lain ditolak dengan
   `BLOCK_PROOF_DATES_RESTRICTED`. `occupancy_ranges` tidak dipakai untuk keputusan ini
   karena belum ada produsen maupun kontrak kunci untuk kolom itu (dicatat
   `src/revenue_bi_agent.py`), jadi menafsirkannya akan mengarang arti.
2. **HIGH — "immutable" hanya konvensi.** Tidak ada trigger pada tabel ini sementara
   tabel append-only lain punya 66. Satu `UPDATE` bisa memindahkan bukti hidup ke booking
   lain tanpa melanggar `uq_sbp_evidence`. Sekarang `trg_supplier_block_proofs_revoke_only`
   (satu-satunya UPDATE yang sah: tiga kolom pencabutan, NULL → isi, kolom lain tidak
   berubah) dan `trg_supplier_block_proofs_no_delete` dipasang di initial schema, di
   migrasi 019, dan di twin PostgreSQL lewat fungsi `villa_revoke_only()`
   (`scripts/generate_pg_twin.py` mengenali penanda `twin:revoke-only`).
3. **HIGH — pertahanan sisi konfirmasi tidak diuji.** Empat kode penolakan
   (`BLOCK_PROOF_WRONG_UNIT`, `WRONG_DATES`, `NOT_AUTHORITATIVE`, `PREDATES_BOOKING`)
   hanya diuji di jalur perekaman, sehingga re-check di `confirm_booking` tidak pernah
   dieksekusi. Sekarang `TestConfirmationDistrustsTheRow` menulis baris menyimpang lewat
   SQL lalu memanggil konfirmasi, ditambah tes trigger dan tes restricted.
4. **MEDIUM — klaim tes yang tidak ada.** Header migrasi 019 menyebut
   `tests/test_schema.py` membandingkan DB hasil migrasi dengan DB baru; klaim itu tidak
   benar. Sekarang klaimnya sesuai kenyataan dan tesnya benar-benar ada:
   `TestSupplierBlockProofMigration` membangun DB pra-019, menjalankan runner, lalu
   membandingkan tabel, indeks, dan trigger dengan DB dari initial schema.

Temuan yang **tidak** diubah di sini dan tetap terbuka: tidak ada runner migrasi
PostgreSQL untuk DB yang sudah ada (`apply_pg_schema` menolak DB non-kosong) sehingga PG
existing belum mendapat tabel ini — bagian dari blocker produksi PostgreSQL yang sudah
dicatat; `confirm_booking` tidak memeriksa `actor_role` (otorisasi masih di lapisan
halaman, sama seperti operasi kernel lain); `expires_at` belum dinormalisasi ke ISO UTC
dan tidak ada pemeriksaan "stay masih di masa depan"; `record_supplier_block_proof`
belum menerima idempotency key. Jumlah tes acceptance file ini kini 27 (A–P pada
kontrak awal, ditambah 8 tes sisi konfirmasi dan trigger).
