# ADR-0065 — Copy villa publik: menggambarkan wilayah, bukan properti

- Status: **ACCEPTED** (aturan teknis: divisi engineering "Ekosistem Super Dev Builders", 2026-10-08;
  **kalimat *disclosure* disetujui owner apa adanya, 2026-10-08**)
- Date: 2026-10-08
- Deciders: Mas Anas (owner) · divisi engineering Villa Agency OS
- Spec: §10 (gerbang kualitas) · ADR-0011 (bahasa en-US) · ADR-0040 (security, safety, privacy pertama) ·
  `docs/PHOTO_CREDITS_2026-10-06.md` · `docs/DESIGN_BENCHMARK_2026-10-06.md` §10 ·
  `docs/MASTER_SPEC_TRACEABILITY_MATRIX.md:95`

Halaman villa publik sudah memuat foto sejak commit `5b4a14f`, dan owner sudah memilih **opsi 2** di
`DESIGN_BENCHMARK §10`: pakai foto stok CC berlabel "ilustratif", bukan menunggu foto asli (yang adalah
*blocker* eksternal). Yang belum ada adalah **copy**: halaman villa tidak punya satu kalimat pun yang
menjelaskan villa itu. ADR ini menetapkan dari mana copy itu datang, dan apa yang ia **tidak** boleh
klaim.

---

## Masalahnya bukan "tidak ada teks", tapi "teks yang mengklaim"

Copy marketing adalah tempat sebuah sistem paling mudah berbohong tanpa niat buruk. Satu kalimat
"private pool dengan pemandangan sawah" untuk villa yang belum pernah diverifikasi berarti tamu
memesan berdasarkan fasilitas yang tidak pernah ada — dan agensi menanggung akibat komersialnya.

Batas yang harus dipegang:

- **Terlalu sedikit** → halaman villa kosong, tidak menjual apa pun, dan tamu bertanya hal yang sama
  lewat WhatsApp berulang kali.
- **Terlalu banyak** → agensi mengklaim sesuatu yang tidak ada di dokumen properti mana pun. Ini bukan
  sekadar cacat UX: ia menyentuh janji komersial (ADR-0040).

## Bukti yang diukur sebelum memutuskan (2026-10-08)

| Pemeriksaan | Hasil |
| --- | --- |
| Foto placeholder di `assets/photos/card/` + `assets/photos/detail/` | 4 *stem* × 2 ukuran = 8 berkas (canggu, jatiluwih, uluwatu, joglo), semua CC BY-SA/CC0 |
| Kolom teks marketing per villa di `src/schema/001_initial.sql` | **0** — tidak ada tabel/kolom `villa_marketing`/`photo`/`showcase` |
| Villa SELLABLE pada seed demo yang **tidak** cocok kata kunci foto | **2** dari 3 (`Villa Contoh Ubud`, `Rumah Contoh Prawirotaman`) → jalur fallback bukan teoretis |
| Nama tempat huruf kecil pada seed demo | ada: `region='yogyakarta'` dengan `province='DI Yogyakarta'` |
| Cacat yang tertangkap saat render (sebelum perbaikan) | `This villa is in yogyakarta, DI Yogyakarta.` — huruf kecil di awal kalimat **dan** provinsi terulang |
| `evidence_objects` menerima objek tanpa pemindaian bersih + klasifikasi | **tidak** (ADR-0008) → foto asli tidak bisa "disimulasikan" sebagai bukti |

Baris terakhir itu yang menentukan: foto asli bukan soal kode, ia **dokumen properti bertanggal** yang
harus lewat jalur bukti. Menaruhnya di modul ini akan melewati pemindaian dan klasifikasi yang memang
dirancang untuk tidak bisa dilewati.

## Keputusan

1. **Foto = *binding* placeholder, satu owner.** Tabel kata kunci → *stem* pindah ke
   `src/villa_showcase.py` (`PHOTO_KEYWORDS`); `marketing_site` mendelegasikan, tidak lagi menyimpan
   salinannya. Villa tanpa kata kunci cocok jatuh ke kartu "Photo coming soon", bukan ke foto orang lain.
2. **Area note hanya tentang wilayah.** Registry `AREA_NOTES` menulis *wilayah* (Canggu, Jatiluwih,
   Uluwatu, lereng Merapi) — sesuatu yang bisa dicek tamu — **bukan** properti. Villa tanpa catatan
   wilayah memakai kalimat netral dari `province`/`region` yang tersimpan.
3. **Highlight hanya angka yang bisa dibela.** Jumlah unit, rentang kamar, dan kapasitas dibangun dari
   nilai kernel (`units.bedrooms`, `units.max_guests`), bukan dari ingatan. Nol kamar tidak dilaporkan.
4. **Setiap halaman villa membawa disclosure.** `DISCLOSURE` menyatakan bahwa catatan wilayah bukan
   fitur terverifikasi, dan tim mengonfirmasi tata letak + foto terbaru sebelum tamu memesan.
5. **Nama tempat dinormalisasi untuk prosa.** `display_place()`: nama yang seluruhnya tertulis dalam
   nama sibling dibuang (tidak ada stutter "yogyakarta, DI Yogyakarta"), nama huruf kecil di-*title
   case* (tidak ada kalimat dimulai huruf kecil). Nama yang sudah berkapitalisasi (`DI Yogyakarta`)
   dibiarkan apa adanya. Dipakai di tiga titik publik: kartu, *lede* halaman villa, dan area note.
6. **Foto/copy asli per villa bukan perubahan modul ini.** Ketika nanti tersedia, jalurnya adalah
   `evidence_objects` (bertanggal, terpindai, terklasifikasi) atau kolom konten per villa — bukan
   menambal registry. Itu perubahan skema + migrasi, dan akan punya ADR sendiri.

## Persetujuan owner atas kalimat disclosure

Owner menyetujui kalimat `villa_showcase.DISCLOSURE` **apa adanya** pada 2026-10-08. Tidak ada sisa
keputusan bisnis yang menggantung di ADR ini; teksnya tetap satu konstanta, jadi revisi kalimat di
kemudian hari adalah satu baris tanpa invasi ke struktur.

## Konsekuensi

- Halaman villa sekarang punya copy nyata untuk **semua** villa — termasuk villa hasil impor
  gelondongan yang belum punya foto — tanpa satu pun klaim fasilitas yang belum diverifikasi.
- Angka di halaman (unit, kamar, kapasitas) berasal dari kernel, jadi agensi bisa mempertahankannya
  saat tamu bertanya "dari mana angka ini".
- Aturan "jangan klaim" ditegakkan tes, bukan niat: `tests/test_villa_showcase.py` menolak copy yang
  menyebut fasilitas (`pool`, `gym`, `spa`, `wifi`, `ocean view`, `beachfront`) maupun *binding* foto
  yang menggantung ke berkas yang tidak ada.
- Villa yang tidak punya catatan wilayah tidak tampil kosong; ia tampil jujur.

## Reversibility

Tanpa perubahan nilai tersimpan. Menukar `DISCLOSURE` (atau satu catatan wilayah) adalah suntingan teks.
Mengganti area note dengan konten per villa yang nyata berarti mengisi registry dari sumber yang
terverifikasi, tanpa mengubah pemanggil maupun template.
