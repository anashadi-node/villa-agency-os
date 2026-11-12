# Kebijakan Keamanan — Villa Agency OS

Repositori ini **publik tetapi bukan proyek kolaboratif terbuka**: pihak luar boleh
membaca, menelaah, dan mengaudit, tetapi **tidak** boleh menulis (lihat
[`CONTRIBUTING.md`](CONTRIBUTING.md) dan [`LICENSE`](LICENSE)).

## 1. Yang kami inginkan Anda laporkan

| Kategori | Contoh |
|---|---|
| Kebocoran rahasia | kredensial, token, kunci API, kunci privat, connection string yang masih hidup di kode, riwayat git, log, atau artefak render |
| Data pribadi | nama/telepon/surel/nomor identitas tamu, nomor rekening, bukti pembayaran yang seharusnya tidak publik |
| Cacat keamanan | *auth bypass*, IDOR, eskalasi peran, injeksi SQL/perintah, SSRF, path traversal |
| Integritas finansial | jalur yang memungkinkan *double charge*, *double booking*, atau saldo/settlement tidak seimbang |
| Logika reservasi | kondisi yang dapat menghasilkan dua booking aktif pada unit+tanggal yang sama |
| Rantai pasok | dependensi/Action yang disusupi atau tidak lagi terpelihara |

**Jangan** menguji dengan data nyata, jangan mengakses akun/data milik orang lain, dan
jangan melakukan uji beban terhadap layanan produksi. Bukti cukup dari klon lokal.

## 2. Cara melapor

Gunakan **GitHub Private Vulnerability Reporting** pada tab *Security* → *Report a
vulnerability* di repositori ini. Kanál ini privat antara Anda dan pemelihara.

Mohon sertakan: versi/commit (`git rev-parse HEAD`), langkah reproduksi, dampak yang
mungkin, dan usulan perbaikan bila ada.

**Jangan** membuka *issue* publik untuk kerentanan yang belum diperbaiki, dan jangan
menempelkan rahasia ke dalam laporan. Bila sebuah rahasia tampak bocor, laporkan
**lokasi berkas + baris**, bukan nilainya.

## 3. Yang kami janjikan

- Konfirmasi penerimaan dalam **3 hari kerja**.
- Penilaian awal (valid/tidak, tingkat keparahan) dalam **10 hari kerja**.
- Pemberitahuan saat perbaikan dirilis, dan kredit bila Anda menghendaki.
- Tidak menempuh langkah hukum terhadap riset yang dilakukan dengan itikad baik, selama
  mengikuti batasan di §1.

## 4. Postur keamanan repositori (diverifikasi 2026-11-13)

- Pemindaian rahasia pada **seluruh riwayat git sampai commit `10857de`** (189 commit, ~12 MB): hasil bersih
  setelah telaah manual — 10 kecocokan mesin diperiksa dan semuanya uji/fixture
  (`idempotency_key` literal, vektor uji RFC 6238, token contoh `abcd…`).
- Tidak ada berkas `.env`, kunci privat, basis data, log, atau CSV yang ter-track.
- Tidak ada nomor rekening, nomor identitas, atau data tamu nyata di dalam repo; data
  contoh memakai domain `example.com` / `.invalid`.
- Pemindaian rahasia, *push protection*, dan peringatan dependensi diaktifkan di GitHub.
- Percabangan `master` dilindungi: tanpa *force-push*, tanpa penghapusan, riwayat linear,
  status check wajib hijau, dan hanya pemilik yang dapat menulis.
- Hak akses tulis: **hanya pemilik**. Tidak ada kolaborator eksternal.

## 5. Cakupan

Berlaku untuk isi repositori `anashadi-node/villa-agency-os`. Layanan produksi, domain,
dan infrastruktur di luar repo **tidak** tercakup di sini; hubungi pemilik lebih dulu
sebelum menguji apa pun yang berjalan.
