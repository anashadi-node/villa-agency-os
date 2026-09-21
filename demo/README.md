# Demo Artefak — Villa Agency OS

Artefak ini dibuat untuk presentasi ke calon konsumen/pembeli. Semua data di dalamnya adalah **mock/demo**, bukan data tamu atau properti nyata.

## File

| File | Fungsi |
|---|---|
| `customer-demo-app.html` | mock aplikasi interaktif untuk meeting/live demo |
| `villa-agency-os-mock-video.mp4` | mock video 28 detik untuk pembuka pitch |
| `villa-agency-os-mock-video-cover.png` | cover/thumbnail video |
| `render_mock_video.py` | script reproducible untuk render ulang video |

## Cara pakai cepat

1. Buka `customer-demo-app.html` di browser.
2. Klik tombol **Jalankan alur demo 90 detik**.
3. Jelaskan urutan ini:
   - Command Center: semua antrean dalam satu layar.
   - Inquiry → Booking: pesan tamu menjadi case, matching, hold, invoice.
   - Anti Double Booking: slot aktif tidak bisa dijual dua kali.
   - Finance: pembayaran dicocokkan dari bukti, bukan screenshot.
   - Safe Message: draft pesan harus grounded dan disetujui manusia.
4. Putar `villa-agency-os-mock-video.mp4` sebagai intro/closing.

## Talk track 60 detik

> Ini bukan chatbot villa dan bukan spreadsheet. Ini operating system untuk reservation dan agency: tamu bertanya, sistem membuat case, mencocokkan villa, menahan slot, membuat invoice, menagih approval manusia, lalu meninggalkan audit trail. Target bisnisnya sederhana: 0 double booking, rekonsiliasi pembayaran rapi, dan tidak ada klaim AI yang tidak punya bukti.

## Batasan jujur

- Artefak ini adalah mock visual untuk penjualan/demo.
- Production go-live tetap butuh keputusan bisnis di `docs/GO_LIVE_DECISIONS.md`.
- Harga paket di `docs/SALES_PACKAGE.md` masih placeholder `[ISI]` sampai Mas Anas memutuskan angka komersial.
