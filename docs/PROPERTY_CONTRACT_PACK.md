# Property Contract Pack — Apa yang Wajib Ada Sebelum Villa Boleh Dijual

Versi: 1.0 · Tanggal: 2026-09-21

Dokumen ini mendefinisikan **gate #0**: sebuah unit hanya boleh muncul di hasil
pencarian, quote, atau hold **jika** contract pack-nya lengkap, valid, dan belum
kedaluwarsa. Tidak ada pengecualian, termasuk untuk "properti teman owner".

---

## 1. Aturan status properti (fail closed)

| Status | Arti | Boleh di-quote? | Boleh di-hold? | Boleh dikonfirmasi? |
|---|---|---|---|---|
| `SELLABLE` | Semua dokumen wajib valid & belum kedaluwarsa | Ya | Ya | Ya |
| `SELLABLE_WITH_REVIEW` | Ada 1 item non-blokir hilang (mis. foto terbaru) | Ya, dengan flag `review_required` | Ya | Ya, dengan approval manager |
| `NOT_SELLABLE` | Ada item blokir hilang/kedaluwarsa (izin, kontrak, pajak, rekening) | **Tidak** | **Tidak** | **Tidak** |
| `SUSPENDED` | Dihentikan manual (sengketa, kerusakan, komplain berat) | **Tidak** | **Tidak** | **Tidak** |

Aturan evaluasi: **status = yang paling lemah** di antara semua item wajib.
Satu item blokir merah ⇒ seluruh properti `NOT_SELLABLE`.

Item blokir vs non-blokir dibedakan hanya di **satu tempat**:
`policy_matrix` properti (`blocking: true|false` per item). Jangan menyebar
logika ini ke beberapa file.

---

## 2. Item contract pack

| # | Item | Blokir | Kedaluwarsa | Verifikasi oleh | Bukti |
|---|---|---|---|---|---|
| C01 | Perjanjian agency/management ditandatangani | ✔ | sesuai periode kontrak | Legal/owner | file kontrak + tanggal tanda tangan + pihak |
| C02 | Bukti kepemilikan / hak mengelola (sertifikat, surat kuasa, sewa) | ✔ | sesuai masa sewa/kuasa | Legal | nomor dokumen + penerbit |
| C03 | Identitas owner & NPWP/NIK pajak | ✔ | — | Admin | doc-id (disamarkan di UI) |
| C04 | Rekening pembayaran owner (nama bank, nomor, nama pemilik) | ✔ | dicek ulang tiap 90 hari | Finance | verifikasi dua langkah (baca ulang + panggilan balik) |
| C05 | NIB (Nomor Induk Berusaha) | ✔ | dicek ulang tiap 180 hari | Admin | nomor + tanggal + status |
| C06 | Sertifikat Standar Usaha Pariwisata / izin usaha akomodasi | ✔ | dicek ulang tiap 90 hari | Admin | nomor + masa berlaku |
| C07 | Status PBJT (pajak daerah) — kabupaten/kota pemungut | ✔ | dicek ulang tiap 90 hari | Finance/tax | tarif + dasar hukum + tanggal berlaku |
| C08 | NPWPD / pendaftaran pajak daerah | ✔ | dicek ulang tiap 180 hari | Finance/tax | nomor + tanggal |
| C09 | Rate card bertanggal (harga publik, minimum stay, biaya tambahan) | ✔ | 90 hari | Reservation manager | file + tanggal berlaku |
| C10 | Policy pembatalan & no-show bertanggal | ✔ | 180 hari | Reservation manager | versi policy + tanggal |
| C11 | Kapasitas maksimum tamu & aturan anak | ✔ | 365 hari | Reservation manager | angka + sumber |
| C12 | Aturan event/pesta, hewan, merokok, jam tenang | ✖ | 365 hari | Reservation manager | teks kebijakan |
| C13 | Check-in/check-out time + prosedur akses | ✖ | 365 hari | Ops | jam + prosedur |
| C14 | Aturan deposit kerusakan & jam tangan | ✔ jika menagih deposit | 180 hari | Reservation manager | jumlah + mekanisme klaim |
| C15 | Kontak darurat & vendor (cleaning, maintenance, transport) | ✖ | 180 hari | Ops | nama + nomor |
| C16 | Foto/video asli bertanggal (bukan render) | ✖ | 365 hari | Marketing | file + tanggal |
| C17 | Protokol force majeure lokal (gempa, erupsi, banjir, upacara) | ✖ | 365 hari | Ops | teks + kontak |
| C18 | Kapasitas & konfigurasi kamar (kasur, kamar mandi, kolam) | ✖ | 365 hari | Ops | angka |
| C19 | Aturan khusus budaya/lokal (mis. Nyepi di Bali) | ✖ | 365 hari | Ops | tanggal/tahun berlaku |
| C20 | Asuransi (jika ada) — polis & masa berlaku | ✖ | sesuai polis | Admin | nomor polis |

**Blokir (C01–C11, C14-if-charged)** = tidak ada penjualan jika tidak valid.
**Non-blokir (C12, C13, C15–C20)** = wajib dilengkapi, boleh menjual dengan flag review.

---

## 3. Aturan penjualan yang tidak bisa di-override

1. **Unit ≠ properti.** Blokir di properti berlaku ke semua unit di dalamnya.
   Satu kompleks bisa punya beberapa unit dengan kapasitas berbeda.
2. **Rate card bertanggal adalah satu-satunya sumber harga.** Tanpa rate card valid, tidak ada quote (`RATE_CARD_MISSING`).
3. **Policy pembatalan harus bertanggal & versi.** Quote menyimpan snapshot; perubahan policy **tidak** mengubah quote/booking yang sudah terbit.
4. **Kuota kanal.** Setiap kanal punya alokasi unit; alokasi tidak boleh membuat unit terjual dobel lintas kanal.
5. **Precedence kanal.** Urutan sumber kebenaran: (1) booking internal terkonfirmasi, (2) hold internal aktif, (3) manajer properti (tertulis), (4) iCal/OTA (indikatif), (5) memori lama (tidak dipakai untuk penjualan).
6. **Izin & pajak tidak boleh di-backfill.** Properti dengan izin/PBJT bermasalah langsung `SUSPENDED`, tanpa menunggu audit berikutnya.

---

## 4. Alur verifikasi (dv/dv dua langkah)

```
Draft (Admin isi)
   ↓  verifikasi 1: dokumen dibaca ulang & dicocokkan ke angka sistem
Verified (verifier tanda tangan digital + tanggal)
   ↓  verifikasi 2: panggilan balik ke owner/verified channel untuk rekening & harga
Activated (status properti dievaluasi ulang → SELLABLE / SELLABLE_WITH_REVIEW)
   ↓  pemeriksaan berkala (lihat tabel kedaluwarsa, tiap item punya next_review_at)
Expired → otomatis turun status (job harian), tanpa menunggu manusia
```

**Rekening payout wajib verifikasi dua langkah** (baca ulang dokumen **dan**
panggilan balik ke nomor owner yang terverifikasi). Perubahan rekening = aksi
berisiko tinggi: butuh approval berjenjang + jeda 24 jam + notifikasi ke owner.

---

## 5. Data yang tidak boleh disimpan berlebihan

- Scan KTP/passport: simpan hanya bila hukum/registrasi tamu mewajibkan, di
  penyimpanan terenkripsi terpisah, dengan masa retensi yang ditetapkan; jangan
  pernah masuk ke log, chat, atau model AI.
- Nomor kartu/CVV: **tidak pernah** masuk sistem ini (PCI scope).
- File identitas owner: tampilkan disamarkan di UI; akses penuh butuh peran khusus + audit.

---

> Catatan: daftar izin di atas adalah **kerangka operasional**. Persyaratan pasti
> per kabupaten/kota (Bali: Badung, Gianyar, Denpasar, Buleleng; DIY: Sleman,
> Kota Yogyakarta, Bantul, Gunungkidul, Kulon Progo) berbeda dan harus
> dikonfirmasi ke Dinas Pariwisata/Penanaman Modal Modal setempat serta konsultan pajak.
