# ADR-0070 — Rupiah adalah nilainya, USD adalah rujukan

- Status: **PROPOSED** (permintaan Mas Anas, 13 Nov 2026: "mata uang pakai IDR dan di sampingnya
  harus ada konversi otomatis ke USD")
- Date: 2026-11-13
- Deciders: Mas Anas (owner) · divisi Villa Management & Agency
- Melengkapi: ADR-0030 dan ADR-0060 (mata uang penawaran), ADR-0011 §55 (bahasa produksi)
- Spec: `src/money_display.py` · `src/dashboard.py` (`money_reference_source`, `view_finance`)
  · `templates/finance.html` · `tests/test_audit_currency_reference.py`

## Masalah

Dua hal yang benar tetapi saling tarik-menarik:

1. **Tamu dan staf perlu membaca angka yang berarti.** Nilai transaksi proyek ini rupiah, tetapi
   tamu utama Bali adalah wisatawan asing dan sebagian pemilik berpikir dalam dolar. Tanpa
   rujukan, setiap orang mengonversi sendiri — dengan kurs yang tidak diketahui siapa pun, dan
   hasilnya tidak bisa diperiksa.
2. **Harga yang mengikat hanya boleh satu mata uang.** Surat Edaran BI melarang pencantuman
   ganda rupiah + valuta asing pada harga, dan audit 2026-11-13 (T8) mencatat bahwa penawaran
   berdenominasi USD untuk Bali berisiko terhadap kewajiban rupiah itu.

Kalau keduanya tidak dipisahkan dengan tegas, "konversi otomatis" berubah menjadi harga kedua
yang diam-diam mengikat — persis yang dilarang.

## Keputusan

1. **Rupiah adalah nilainya.** Setiap angka yang mengikat — penawaran, invoice, kuitansi,
   ledger, laporan pemilik — tetap dalam IDR, dan itu tidak berubah oleh ADR ini.
2. **USD adalah rujukan, bukan nilai.** Di samping angka rupiah, platform boleh mencetak satu
   angka USD berlabel `≈` dan kata **`indicative`**, dengan kurs dan tanggal kurs di baris
   keterangan. Angka itu **tidak pernah** masuk perhitungan: tidak ke quote, invoice, laporan,
   komparasi, atau approval. `src/money_display.py` hanya mengembalikan teks untuk ditampilkan.
3. **Kurs adalah fakta terbit, bukan default.** Kurs dibaca dari policy `commercial.fx.rates`
   dan harus lulus `fx.require_fresh` dengan batas `commercial.fx.max_age_hours` — penjaga yang
   sama dengan lantai margin §11. Tidak ada kurs terbit, tidak ada tanggal, tidak ada batas
   kesegaran, pasangan mata uang tidak terbit, atau kurs terlalu tua ⇒ **rujukan tidak
   ditampilkan**, dan halaman mengatakan alasannya. Modul ini tidak punya kurs cadangan, tidak
   memakai paritas, dan tidak menyimpan angka konstanta.
4. **Permukaan tamu ditahan sampai counsel menyetujui.** `GUEST_DOCUMENT_REFERENCE_ENABLED`
   bernilai `False`. Dashboard internal (staf membaca angkanya sendiri) memakai rujukan hari
   ini; dokumen yang keluar ke tamu tetap rupiah saja. Menyalakannya adalah keputusan counsel
   atas ADR-0030/ADR-0060, bukan keputusan engineering.
5. **Satu formatter.** Angka diformat lewat `documents.format_amount` (locale `id-ID`), bukan
   formatter baru, supaya satu nilai tidak pernah punya dua gaya penulisan di dua halaman.

## Alasan

- **Bisa diperiksa.** Rujukan tanpa kurs adalah klaim; rujukan dengan kurs + tanggal adalah
  aritmetika yang bisa diulang pembaca. Itulah bedanya dengan "kira-kira setara sekian dolar".
- **Fail-closed lebih jujur daripada kurs karangan.** Kurs basi atau tidak terbit adalah alasan
  untuk tidak menampilkan sesuatu, bukan alasan memakai angka lama. Sama dengan penjaga §11.
- **Pemisahan nilai vs rujukan menyelesaikan tabrakan dengan BI tanpa mengabaikan permintaan
  Owner.** Tamu tetap melihat angka yang mereka pahami; harga yang mengikat tetap satu mata uang.

## Konsekuensi

- Halaman `/finance` sekarang mencetak `IDR 4.500.000 (≈ USD 276,92)`; pengelompokan mengikuti
  locale Indonesia (`4.500.000`, bukan `4,500,000`). Format uang dashboard yang lama
  (`D.format_money`) tetap ada dan tetap dipakai untuk angka teknis berlabel `(minor)`.
- Teks produksi baru ditulis **en-US** sesuai ratchet ADR-0011 §55 (`money_display.py` = 0
  literal Indonesia, `dashboard.py` dan `templates/finance.html` tidak bertambah). Kalimat
  keterangan pada halaman karena itu berbahasa Inggris; terjemahannya adalah pekerjaan
  penerjemahan halaman, bukan pengecualian ratchet.
- Rujukan hilang sendiri saat kurs melewati batas umur. Tidak ada proses yang perlu dimatikan:
  `scripts/refresh_fx_rate.py` yang menerbitkan kurs, dan kesegarannya yang menentukan.
- Bila counsel menyetujui rujukan pada dokumen tamu: balik satu konstanta, lalu tambahkan tes
  emas yang membandingkan angka dokumen dengan `money_display.reference` untuk kurs yang sama.
- Belum dikerjakan dan sengaja dipisah: mengubah `guests.preferred_currency` default dan mata
  uang penawaran Bali (T8) — itu perubahan policy + seed yang menggeser fingerprint, dan
  menunggu keputusan owner #2 pada dokumen audit.

## Alternatif yang ditolak

- **Menampilkan dua harga setara.** Dilarang SE BI dan menciptakan dua harga yang bisa
  dipertengkarkan tamu.
- **Mengunci kurs di kode (mis. 16.000).** Menjadi kurs karangan yang dipakai orang untuk
  memutuskan, dan tidak ada yang tahu kapan terakhir benar.
- **Menampilkan rujukan pada dokumen tamu sekarang juga.** Melanggar batas yang audit sendiri
  tandai sebagai keputusan counsel.
