# Demo Artefak — Villa Agency OS

Artefak ini dibuat untuk presentasi. Semua data di dalamnya adalah **mock/demo**, bukan data tamu atau properti nyata.

**Pemisahan penting:** demo/dashboard internal bukan Website Marketing Villa. Website Marketing
publik untuk calon tamu harus terpisah dan berisi katalog villa, destinasi Bali/Yogyakarta,
halaman detail villa, inquiry, dan concierge. Rujukan: `docs/PRODUCT_SURFACE_SEPARATION_2026-10-05.md`.

## File

| File | Fungsi |
|---|---|
| `customer-demo-app.html` | mock aplikasi interaktif untuk meeting/live demo |
| `villa-marketing-bali-yogyakarta-2026-10-05.html` | prototype visual Website Marketing publik Bali + Yogyakarta (terpisah dari dashboard internal) |
| `villa-agency-os-mock-video.mp4` | mock video 28 detik untuk pembuka pitch |
| `villa-agency-os-mock-video-cover.png` | cover/thumbnail video |
| `render_mock_video.py` | script reproducible untuk render ulang video |

## Cara pakai cepat

1. Buka `customer-demo-app.html` di browser (tanpa internet, tanpa akun — semua gambar ilustrasi
   SVG lokal, tidak ada aset pihak ketiga).
2. Pilih sudut pandang di kanan atas: **Tamu**, **Staf agency**, atau **Pemilik villa**. Menu kiri
   ikut menyesuaikan — supaya saat meeting bisa ditunjukkan "yang dilihat tamu" vs "yang dilihat
   staf" tanpa berpindah aplikasi.
3. Klik **Jalankan demo 90 detik** untuk membuka alur yang disarankan, lalu jalankan berurutan:
   - **Villa kami** — katalog villa: lokasi, kapasitas, tarif per malam, tanggal kosong terdekat.
   - **Ketersediaan & harga** — peta tanggal October per villa (kosong / ditahan / terisi /
     perawatan) + tarif. Klik satu kotak tanggal: muncul jawaban "malam ini villa mana yang
     masih kosong".
   - **Percakapan → booking** — chat tamu (kiri) berubah jadi pesanan bernomor (kanan): kasus,
     penawaran, penahanan tanggal, booking, invoice.
   - **Uang & pembayaran** — pencocokan pembayaran dari bukti provider, pembagian uang pemilik
     villa, dan apa yang sengaja tidak dicocokkan otomatis (diserahkan ke manusia).
   - **Pesan ke tamu (aman)** — kalimat yang ditolak sistem karena mengarang harga, lalu jalan
     persetujuannya (penyusun ≠ penyetuju).
   - **Laporan pemilik** — okupansi & uang per villa; pemilik bisa melihat sendiri.
4. Tombol **Cetak / PDF** mencetak seluruh layar dalam satu dokumen (untuk lampiran proposal).
5. Putar `villa-agency-os-mock-video.mp4` sebagai intro/closing.

## Desain — kenapa seperti ini

Permukaan lama terasa seperti dashboard SaaS umum (biru gelap, angka di kartu), padahal pembelinya
adalah **orang villa**: pemilik villa, staf reservasi, tamu. Jadi bahasanya diganti ke ranah
hospitality:

- **Warna & huruf**: krem pasir, hijau teak, terakota, aksen emas; judul memakai huruf serif
  (hangat, seperti brosur villa), isi tetap huruf sistem yang mudah dibaca.
- **Isi yang villa-spesifik**, bukan angka generik: kamar, kapasitas tamu, kolam, jarak ke pantai,
  tarif per malam, deposit jaminan, hold tanggal, jam check-in/check-out.
- **Gambar**: tiga ilustrasi SVG yang digambar tangan di dalam berkas ini sendiri (villa kolam di
  Canggu, villa sawah di Ubud, rumah joglo di Yogyakarta). Bukan foto stok, bukan tautan keluar —
  jadi tidak ada urusan lisensi dan demo tetap jalan tanpa internet.
- **Setiap layar diberi satu baris "layar ini untuk siapa"**, karena masalah utama saat presentasi
  bukan fiturnya, melainkan orang tidak tahu sedang melihat apa.
- **Kalender tanggal adalah bintangnya**: masalah termahal agency villa adalah dua orang menahan
  tanggal yang sama. Di sini penahanan tanggal terlihat sebagai warna dan terkunci di database.

## Talk track 60 detik

> Ini bukan chatbot villa dan bukan spreadsheet. Ini operating system untuk reservation dan agency: tamu bertanya, sistem membuat case, mencocokkan villa, menahan slot, membuat invoice, menagih approval manusia, lalu meninggalkan audit trail. Target bisnisnya sederhana: 0 double booking, rekonsiliasi pembayaran rapi, dan tidak ada klaim AI yang tidak punya bukti.

## Batasan jujur

- Artefak ini adalah mock visual untuk penjualan/demo; **tidak** terhubung ke database mana pun.
  Angka besarannya diambil dari uji jalur tamu 16 langkah yang benar-benar dijalankan
  (`scripts/go_live_smoke.py`), jadi bukan angka karangan.
- Nama tamu, nomor pesanan, dan uang di dalam demo adalah contoh.
- Production go-live tetap butuh keputusan bisnis di `docs/GO_LIVE_DECISIONS.md`.
- Harga paket di `docs/SALES_PACKAGE.md` masih placeholder `[ISI]` sampai Mas Anas memutuskan angka komersial.
