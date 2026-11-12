# ADR-0068 — Lokasi villa: presisi, sumber, dan satu aturan untuk dua penulis

- Status: **ACCEPTED** (aturan teknis: divisi engineering Villa Agency OS, 2026-11-13;
  perubahan skema sudah ada di migrasi 017 dan tidak diubah oleh ADR ini)
- Date: 2026-11-13
- Deciders: Mas Anas (owner) · divisi engineering Villa Agency OS
- Spec: ADR-0040 (security, safety, privacy pertama) · ADR-0065 (copy publik menggambarkan
  wilayah) · `src/schema/017_property_geo.sql` · `src/geo.py` · `tests/test_geo.py` ·
  `tests/test_villa_import.py`

Peta publik menjual lokasi. Satu pin yang salah menaruh tamu di gerbang tetangga, dan
kalimat "menurut website Anda" cukup untuk membuat agensi menanggung akibatnya. Migrasi 017
sudah menambah empat kolom (`latitude`, `longitude`, `geo_precision`, `geo_source`), tetapi
**aturan yang membuat kolom itu bermakna belum punya rumah**: komentarnya merujuk sebuah
fungsi yang tidak ada, dan dua penulis (`scripts/import_villas.py` lewat `villa_import`, dan
`reservation_kernel.upsert_property`) akan menulis kolom yang sama tanpa aturan bersama.

## Masalahnya bukan "tidak ada koordinat", tapi "koordinat tanpa derajat kepercayaan"

Tiga keadaan yang harus dibedakan, dan tidak boleh saling mewakili:

| Keadaan | Yang benar dilakukan peta |
| --- | --- |
| Belum pernah disurvei | jangan tampilkan pin; tampilkan wilayah saja |
| Perkiraan dari pusat wilayah (desa/kota) | boleh, tetapi sebagai **area**, bukan titik |
| Hasil survei atau konfirmasi tertulis pemilik | pin boleh, dengan pembulatan tetap |

Kalau ketiganya disimpan sebagai satu angka, keadaan pertama dan ketiga terlihat sama di
database, dan pembaca berikutnya (manusia atau agen) akan memilih yang paling menguntungkan
secara komersial. Itu bukan bug tampilan; itu klaim yang tidak bisa dibela.

## Keputusan

1. **Presisi adalah saklar, bukan hiasan.** `geo_precision` bernilai `UNKNOWN`, `AREA_APPROX`,
   atau `VERIFIED` (dijaga CHECK di skema). Pembaca publik memutuskan dari kolom ini, bukan
   dari ada/tidaknya angka.
2. **Satu fungsi untuk dua penulis.** `geo.validate_geo_fields(precision, latitude, longitude,
   source)` mengembalikan empat nilai yang sudah sah atau melempar `GeoError`. Dipanggil oleh
   `villa_import._geo()` (jalur CSV) dan `reservation_kernel.upsert_property` (jalur program).
   Alasannya teknis: SQLite tidak bisa `ALTER TABLE … ADD CHECK`, sehingga aturan
   "`VERIFIED` menyiratkan koordinat" tidak bisa hidup di skema; ia harus hidup di satu fungsi
   yang dipanggil semua penulis, bukan disalin ke tiap pemanggil.
3. **Tiga hasil, tidak ada yang keempat.** (a) `UNKNOWN` dengan tiga kolom kosong; (b) presisi
   bernama dengan **kedua** koordinat dan kalimat sumber; (c) penolakan. Tidak ada mode
   "koordinat tanpa presisi" dan tidak ada mode "presisi tanpa koordinat".
4. **`UNKNOWN` menolak, bukan membuang.** Baris `UNKNOWN` yang menyertakan koordinat atau
   sumber **ditolak**. Membuang diam-diam akan membuat operator mengira angkanya tersimpan.
5. **Sumber wajib berupa kalimat yang bisa ditanya.** `geo_source` minimal 2 karakter (CHECK
   skema) dan praktisnya menyebut siapa/apa yang mengonfirmasi dan kapan ("survei 2026-11-12
   oleh pemilik"). Ini yang dijawab ke auditor ketika pin dipertanyakan.
6. **Kolom kosong berarti "jangan sentuh".** Di kernel, `geo_precision=None` membuat
   `upsert_property` tidak menyentuh empat kolom geo sama sekali. Di impor, empat kolom hanya
   masuk ke payload bila presisi disebut. Konsekuensinya: **impor ulang daftar lama tidak bisa
   menghapus survei yang sudah dikerjakan orang.** Menghapus survei harus tindakan eksplisit
   (`UNKNOWN` dengan kolom kosong), bukan efek samping.
7. **Audit tidak menyimpan float.** Peristiwa audit mencatat koordinat sebagai string desimal;
   kolom menyimpan float yang dipakai SQLite. Alasan: payload audit dibaca manusia dan
   dibandingkan mesin, dan pecahan biner bukan keduanya.
8. **Nilai salah tidak menular lewat impor massal.** Satu baris ditolak → baris itu dilaporkan
   dengan nomor baris dan nama kolom; berkas tetap diproses sesuai aturan impor yang sudah ada
   (rencana dulu, `--apply` baru menulis).

## Yang **tidak** diputuskan di sini

- **Bukan** keputusan untuk mengisi koordinat villa pilot. Data tiga villa masih
  `geo_precision=UNKNOWN`; mengisinya butuh sumber tertulis, dan angka yang dikarang akan
  menjadi pin di halaman publik.
- **Bukan** perubahan cara peta publik membulatkan. Pembulatan tetap
  `geo.public_point`/`PUBLIC_ROUNDING_DP` (2 desimal ≈ 0,55 km per sumbu); ADR ini hanya
  memastikan pin yang muncul berasal dari baris yang berhak punya pin.
- **Bukan** izin menampilkan alamat tamu. Alamat tamu tetap hanya dipercaya dari hop proxy
  (ADR-0067) dan tidak pernah dipakai sebagai sumber lokasi properti.

## Konsekuensi

- Impor CSV kini punya 20 kolom (12 properti + 8 unit), bukan 16. Berkas lama tanpa kolom geo
  tetap sah dan tidak menghapus apa pun.
- Dua jalur tulis berbagi satu aturan; tes `tests/test_geo.py` memeriksa keduanya benar-benar
  memanggil fungsi itu, sehingga salinan aturan yang menyimpang akan gagal lebih dulu.
- Villa tanpa survei tampil sebagai wilayah, bukan sebagai pin. Ini bisa terasa kurang
  meyakinkan secara komersial — dan itu memang harga dari klaim yang bisa dibela.
- Menambah nilai presisi baru berarti mengubah CHECK di skema **dan** fungsi validasi; keduanya
  gagal bersamaan bila hanya satu yang diubah.

## Reversibility

Tanpa perubahan data yang merusak: setiap baris yang tidak lolos aturan ditolak sebelum
menulis, bukan diperbaiki diam-diam. Menurunkan presisi sebuah villa adalah satu baris
`UPDATE`/impor eksplisit, tercatat di audit, dan bisa dikembalikan dengan sumber yang sama.
