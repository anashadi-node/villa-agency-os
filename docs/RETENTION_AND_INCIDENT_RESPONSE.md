# Retention & Incident Response

Versi: 1.0 · Tanggal: 2026-09-21

---

## 1. Masa retensi

| Data | Simpan | Setelah itu | Dasar |
|---|---|---|---|
| Inquiry tidak lanjut (tidak jadi booking) | 12 bulan | hapus/anonymize | kepentingan operasional |
| Quote kedaluwarsa | 24 bulan | arsip agregat tanpa PII | sengketa harga |
| Booking terkonfirmasi (data transaksi) | 10 tahun | arsip pajak | kewajiban pembukuan/pajak |
| Data identitas tamu (registrasi) | sesuai kewajiban registrasi tamu setempat; jika tidak diwajibkan → **tidak disimpan** | hapus | minimisasi PDP |
| Log akses & audit | 10 tahun | arsip | ketertelusuran |
| Log aplikasi (non-PII) | 90 hari | hapus | operasional |
| Rekaman percakapan/transkrip AI | 12 bulan (P2 ter-redaksi) | hapus | QA & perbaikan |
| Kontrak, izin, NPWP, PBJT (contract pack) | masa kontrak + 10 tahun | arsip | legal & pajak |
| Dokumen rekening owner | selama relasi + 10 tahun | arsip | audit finansial |
| Data kartu/CVV (P4) | **tidak pernah disimpan** | — | PCI scope |

Aturan teknis: setiap tabel ber-PII wajib punya kolom `retention_until` dan job
harian `purge_expired` yang mencatat ke `audit_events` berapa baris dihapus.
Penghapusan harus **soft-delete + anonymize**, bukan `DELETE` mentah, agar
referensi audit (hash chain) tetap utuh.

---

## 2. Enam playbook insiden

### P1 — Villa tidak bisa dihuni (overbooking, kerusakan, konflik)
1. **Deteksi**: job overlap harian menemukan 2+ booking aktif pada unit & tanggal sama, atau laporan tamu/ops.
2. **Hentikan kerusakan** (< 15 menit): suspend unit (`SUSPENDED`) supaya tidak ada booking baru; jangan hapus record apa pun.
3. **Tentukan siapa yang pertama**: waktu konfirmasi paling awal (bukan paling mahal, bukan paling penting) = hak pertama. Bukti: `audit_events`.
4. **Relokasi tamu berikutnya**: tawarkan unit setara/lebih baik, biaya selisih ditanggung agency, batas waktu jawaban 2 jam.
5. **Kompensasi**: sesuai policy + keputusan manusia; tercatat, bukan otomatis.
6. **Root cause**: kenapa overlap lolos (kanal mana, siapa memasukkan, kenapa tidak ada hold). Perbaikan harus menyentuh **sumber**, bukan hanya unit yang bermasalah.
7. **Komunikasi**: satu narasi, satu penanggung jawab. Tidak ada janji di luar kewenangan.

### P2 — Pembayaran tidak cocok (nominal/kurs/referensi)
1. Booking masuk `PAYMENT_UNMATCHED` — **jangan** tandai lunas.
2. Cek 4 hal: nominal, mata uang, waktu, referensi/berita transfer.
3. **Bukti yang boleh menutup**: mutasi bank/PSP feed. Screenshot/chat **tidak pernah** cukup.
4. Jika selisih kecil karena biaya bank: catat sebagai penyesuaian dengan approval finance, bukan "dibulatkan".
5. Jika tidak selesai < 48 jam → eskalasi ke manager; batas hold tetap berlaku (jangan perpanjang tanpa approval).

### P3 — Double booking lintas kanal (OTA vs internal)
1. Bukti precedence: booking internal terkonfirmasi mengalahkan iCal (indikatif).
2. Tutup kalender kanal luar **segera** setelah konfirmasi internal (SLA < 10 menit).
3. Sinkronisasi iCal adalah **satu arah untuk informasi**, bukan sumber konfirmasi.
4. Jika tamu OTA sudah datang: playbook P1 langkah 3–5.

### P4 — Force majeure regional (gempa/erupsi/banjir/upacara/pandemi)
1. Aktifkan `FORCE_MAJEURE` pada properti terdampak (semua unit → `SUSPENDED`).
2. Prioritaskan **keselamatan**, lalu akomodasi alternatif, lalu penyelesaian dana.
3. Klausul force majeure per properti dibaca apa adanya; jangan improvisasi janji refund.
4. Notifikasi massal memakai template yang disetujui manager; tidak ada janji nominal sebelum dihitung finance.

### P5 — Pembayaran gagal/chargeback/fraud
1. Jangan pernah menyimpan data kartu; semua penanganan lewat PSP.
2. Chargeback: kumpulkan bukti (kontrak, policy yang disetujui, log konfirmasi, bukti layanan) < 24 jam.
3. Indikasi fraud (identitas palsu, permintaan OTP, transfer ke rekening pribadi, urgensi ekstrem): **berhenti**, eskalasi, jangan lanjutkan pembayaran.
4. Rekening tujuan hanya dari master yang terverifikasi; permintaan ubah rekening via chat = potensi penipuan → verifikasi dua langkah + jeda 24 jam.

### P6 — Kehilangan akses / kebocoran data
1. Rotasi kredensial `[REDACTED]` (jangan pernah ditulis di dokumen/chat).
2. Cabut sesi & token yang terdampak; matikan integrasi terkait.
3. Catat: apa yang bocor, berapa subjek, sejak kapan.
4. Notifikasi internal < 1 jam ke pemilik data + penanggung jawab; keputusan notifikasi eksternal (subjek/otoritas) diambil sesuai UU 27/2022 & kontrak, bukan oleh tim teknis sendirian.

---

## 3. Tingkat keparahan & SLA

| Severity | Contoh | Response | Resolusi |
|---|---|---|---|
| **S1** | Villa tidak bisa dihuni / double booking / kebocoran P3 | < 15 menit | < 24 jam |
| **S2** | Dana tidak cocok, refund tertunda, outage sistem | < 1 jam | < 48 jam |
| **S3** | Keluhan layanan, quote salah tapi belum dibayar | < 4 jam | < 3 hari |
| **S4** | Permintaan informasi, perbaikan konten | < 1 hari | < 7 hari |

## 4. On-call & eskalasi

- Satu **incident owner** per insiden (bukan "tim").
- Eskalasi otomatis ke manager bila S1/S2 tidak di-acknowledge dalam SLA.
- Setiap insiden menyisakan **satu** item perbaikan yang dicek ulang 30 hari
  kemudian. Insiden tanpa tindak lanjut = insiden yang akan terulang.
- Setelah insiden S1/S2: post-mortem tanpa mencari kesalahan orang, fokus
  sistem (control mana yang kosong: gate, hold, verifikasi, pembatasan peran).

## 5. Data minimal satu insiden (dipakai semua playbook)

```
incident_id, severity, detected_at, detected_by, unit/property,
guest_ref (pseudonim), status booking, uang yang terlibat,
aksi yang diambil + jam, siapa yang meng-approve, akar masalah,
perbaikan + tanggal verifikasi ulang.
```
