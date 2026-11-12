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
- Pemindaian rahasia, *push protection*, dan pembaruan keamanan dependensi **aktif** di
  GitHub (diverifikasi lewat `security_and_analysis`, 2026-11-13).
- Percabangan `master` dilindungi: tanpa *force-push*, tanpa penghapusan, riwayat linear,
  status check `pre-deploy gate (Python 3.12)` wajib hijau, wajib lewat pull request,
  dan aturan ini **berlaku juga untuk admin** (`enforce_admins: true`).
- Hak akses tulis: **hanya pemilik** (1 kolaborator: `anashadi-node`).
- Pelaporan kerentanan privat: **aktif**.
- Uji dari luar (anonim, tanpa kredensial) 2026-11-13: `git clone` **berhasil**
  (baca), `git push` **ditolak** — `! [remote rejected] master -> master
  (protected branch hook declined)`.

### Batas yang tidak dapat ditegakkan GitHub

Jujur soal batas platform, supaya tidak ada klaim berlebih:

1. **Fork tidak bisa dimatikan** pada repositori publik milik akun pribadi (GitHub
   menolak `allow_forking=false` dengan HTTP 422). Artinya siapa pun dapat membuat
   **salinan** repo ini di akunnya sendiri dan mengubah salinan itu. Yang tidak bisa
   mereka lakukan: mengubah repo asal — semua jalur tulis (push langsung, force-push,
   hapus cabang) ditolak, dan penggabungan hanya oleh pemilik.
2. **Pull request dari luar tetap bisa dibuka** (tanpa hak gabung). PR semacam itu tidak
   mengubah isi repo sampai pemilik menekan *merge*, dan kebijakan repo ini adalah
   menutupnya (lihat `CONTRIBUTING.md`).
3. Repo publik dapat diarsipkan/disalin pihak ketiga (mis. arsip web, cermin) di luar
   kendali GitHub. Karena itu **jangan pernah** menaruh rahasia di repo ini — bukan
   karena pemindaian gagal, tetapi karena publikasi bersifat permanen.

## 5. Cakupan

Berlaku untuk isi repositori `anashadi-node/villa-agency-os`. Layanan produksi, domain,
dan infrastruktur di luar repo **tidak** tercakup di sini; hubungi pemilik lebih dulu
sebelum menguji apa pun yang berjalan.
