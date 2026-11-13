# Bukti anti-double-booking: uji balapan file-backed dan kontrol negatif

- Status: **catatan bukti** (bukan ADR; angka yang dapat diaudit ulang, 2026-11-13)
- Dipicu oleh: Lampiran F butir 8/10/11 dokumen A–Z — klaim "tidak ada uji konkurensi" perlu
  dijawab dengan bukti, bukan dengan kalimat
- Rujukan: `tests/test_no_double_booking_concurrency.py` · `tests/test_reservation_kernel.py` ·
  `src/reservation_kernel.py` (`BEGIN IMMEDIATE`, `SLOT_ALREADY_HELD`, `SLOT_ALREADY_BOOKED`) ·
  `src/schema/001_initial.sql` (`uq_holds_active_slot`) · North Star: **0 confirmed double booking**

"0 double booking" adalah syarat mutlak, bukan aspirasi. Syarat mutlak hanya bermakna kalau
ada uji yang **bisa gagal** ketika jaminannya dicabut. ADR ini mencatat cara membuktikannya,
supaya klaim itu tidak bergantung pada ingatan siapa pun.

## Masalah: uji konkurensi yang hampa

Uji balapan yang ditulis di atas `sqlite3.connect(":memory:")` **tidak menguji balapan**.
Setiap koneksi mendapat basis data privat; dua penulis tidak pernah bertemu di satu berkas,
sehingga hasilnya selalu "satu pemenang" karena memang hanya satu penulis yang ada. Uji
seperti itu lulus 100% dan tidak membuktikan apa pun — lebih berbahaya daripada tidak ada uji,
karena memberi rasa aman palsu.

Karena itu fixture kernel diberi `db_path` (default `:memory:` tetap dipertahankan untuk 103
berkas uji lain yang memang menguji logika, bukan konkurensi), dan uji balapan menunjuk ke
**satu berkas nyata**.

## Keputusan

1. **Uji balapan wajib file-backed.** `ReservationKernel` dibuka pada satu berkas, `journal_mode=WAL`,
   penulisan lewat `BEGIN IMMEDIATE` (kunci tulis diambil di awal transaksi). 2–3 penulis nyata
   (`threading.Thread` + `threading.Barrier`) menyerbu slot yang sama.
2. **Dua penjaga harus terbukti berlapis.** Jaminan "satu slot, satu pemegang" tidak boleh
   bergantung pada satu mekanisme saja: (i) kunci tulis `BEGIN IMMEDIATE`, (ii) partial unique
   index `uq_holds_active_slot` + pemilihan status penempati slot yang dibaca **dari skema**,
   bukan dikarang di dalam uji. Uji harus lulus hanya kalau lapisan-lapisannya utuh.
3. **Assertion menguji invarian, bukan pemenang.** Yang diuji: `bookings + hold_penempati_slot == 1`.
   Siapa yang menang adalah detail penjadwalan; yang tidak boleh terjadi adalah dua pemegang.
   Penulis yang kalah harus membawa kode domain `SLOT_ALREADY_HELD` atau `SLOT_ALREADY_BOOKED`,
   dan pada kasus "sudah menjadi booking", `details["conflicting_booking"]` harus sama dengan
   `booking_id` yang benar-benar ada di tabel `bookings`.
4. **Kontrol negatif wajib, dijalankan pada salinan repo.** Repo kerja tidak boleh
   dikontaminasi: salin `src/`, `tests/`, `pytest.ini` ke direktori sementara, cabut penjaganya,
   jalankan uji, lalu hapus salinan. Uji yang tetap hijau setelah penjaganya dicabut berarti
   uji itu hampa dan harus diperbaiki.
5. **Kontrol positif wajib.** Dua penulis pada **unit berbeda** harus dua-duanya menang. Tanpa
   kontrol ini, harness yang diam-diam menyerialkan semua penulis akan lulus uji balapan
   dengan cara yang salah.

## Bukti terukur (2026-11-13, bukan kutipan)

| Keadaan | Hasil |
| --- | --- |
| Repo apa adanya | 7 uji; 10 kali berturut-turut: 70 titik, 0 gagal, RC=0 (sebelumnya 20 kali juga RC=0) |
| `make check` (gerbang pre-deploy) | **PASS** — 3882 lulus, 4 dilewati, 4008 subtest |
| Kontrol negatif A: `BEGIN IMMEDIATE` → `BEGIN` (kunci tulis dicabut, 2 penggantian) | **5 dari 7 uji GAGAL**, RC=1 — termasuk kedua kontrol positif dan uji idempotensi-balapan |
| Kontrol negatif B: kunci tulis **dipertahankan**, overlap guard dilumpuhkan (`return` sebelum query) **dan** `uq_holds_active_slot` dihapus | **4 dari 7 uji GAGAL**, RC=1, dan double booking benar-benar terjadi: `book_writer0000001` `CONFIRMED` 2026-10-01→2026-10-03 **dan** `hold_writer1000002` `CONFIRMED` 2026-10-02→2026-10-04 pada `unit_1`; assertion berbunyi `exactly one writer may succeed, got [2]` |

Pembacaan hasil:

- Kontrol A membuktikan uji ini **benar-benar menjalankan penulis paralel** dan bergantung pada
  kunci tulis. Kalau harness menyerialkan penulis, mencabut `BEGIN IMMEDIATE` tidak akan
  mengubah apa pun.
- Kontrol B adalah bukti yang paling penting: dengan kunci tulis masih utuh, mencabut lapisan
  kedua menghasilkan **dua penempati slot yang bertumpang tindih**. Artinya uji menangkap
  pelanggaran pada lapisan yang berbeda, dan lapisan kedua memang menanggung beban — bukan
  hiasan.
- Kedua kontrol positif **tetap lulus** pada percobaan B (tidak ada dalam daftar gagal), jadi
  kegagalan di B berasal dari jaminan yang dicabut, bukan dari harness yang rusak.

## Konsekuensi

- Menambah ±4 detik pada setiap gerbang pre-deploy. Harga ini dibayar sadar.
- Uji konkurensi baru **wajib** file-backed; `:memory:` untuk uji balapan ditolak pada review.
- Salinan kontrol negatif tidak pernah di-commit; yang di-commit hanya hasil dan cara
  menjalankannya (di sini), agar bisa diulang oleh auditor luar.
- Beban kunci tulis bertambah pada berkas basis data uji, jadi uji ini tidak dijalankan paralel
  dengan uji lain pada berkas yang sama (`db_path` unik per kelas).

## Yang tidak dibuktikan ADR ini

- Balapan **antar-proses/antar-host** (uji ini antar-thread pada satu proses, satu berkas).
- Perilaku pada PostgreSQL (twin `.pg.sql` diuji terpisah oleh target `pg-twin`; uji balapan ini
  hanya SQLite).
- Kinerja/latensi jalur tulis di produksi: tidak diukur di sini. Uji ini mengukur kebenaran
  (satu slot, satu pemegang), bukan kecepatan.

Klaim yang jujur karenanya: **"uji balapan nyata ada, hijau, dan terbukti bisa gagal"** — bukan
"double booking mustahil". Jaminan produksi tetap bertumpu pada transaksi basis data, kunci
tulis, dan unique index; uji ini mengawal ketiganya agar tidak dicabut tanpa sadar.
