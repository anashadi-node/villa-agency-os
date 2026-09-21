# Project Charter — Villa Agency OS

Versi: 1.0 · Tanggal: 2026-09-21 · Pemilik dokumen: Agency Owner
Status: **DRAFT — menunggu keputusan legal/finance (lihat §7 Open decisions)**

---

## 1. Tujuan (North Star)

Membangun sistem operasi reservation untuk agency villa di **Bali** dan
**Yogyakarta** yang:

1. Menghasilkan **0 double booking** dan **0 konfirmasi dari ketersediaan yang tidak terverifikasi**.
2. Menjadikan setiap angka di quote, invoice, dan laporan owner **reproducible** dari input bertanggal.
3. Memungkinkan AI mempercepat respons (ekstraksi, pencarian, draft) **tanpa** memberi AI wewenang menetapkan status, harga, atau kepastian booking.
4. Rekonsiliasi pembayaran **≥ 99%** dan sisa selisih 100% punya pemilik kasus dan batas waktu.

Ukuran keberhasilan tidak boleh berupa "jumlah villa" atau "jumlah chat
terbalas otomatis". Metrik pada §5.

## 2. Ruang lingkup pilot

| Aspek | Keputusan |
|---|---|
| Wilayah | Bali + Yogyakarta |
| Jumlah properti pilot | **10–20 properti** (usulan; butuh konfirmasi final) |
| Model bisnis | Hybrid per properti: `AGENCY_COMMISSION`, `FULL_MANAGEMENT`, `HYBRID` |
| Kanal masuk | Website, email, dashboard internal, WhatsApp Business resmi |
| Kanal bayar | Hosted payment page PSP berlisensi PJP; tidak ada data kartu di sistem kami |
| Mode AI | Human-supervised; shadow mode sebelum auto-send terbatas |
| Multi-tenant SaaS | **Di luar lingkup MVP** |

Ruang lingkup pilot **dibekukan** sebelum kapasitas, staffing, SLA, dan biaya
bisa dihitung. "Banyak villa" bukan target launch.

## 3. Persona & tanggung jawab

| Persona | Yang mereka butuhkan | Yang tidak boleh mereka lakukan sendiri |
|---|---|---|
| Guest | Jawaban cepat & benar, quote jelas, kepastian tertulis | — |
| Reservation Agent | Kasus terkonsolidasi, evidence ketersediaan, draft siap review | Menetapkan harga/pengecualian policy di luar batas |
| Reservation Manager | Antrean SLA, approval, override tercatat | Mengubah konfirmasi tanpa jejak audit |
| Owner / Property Manager | Laporan bulanan, net payout, status izin | Menyalakan properti tanpa contract pack lengkap |
| Administration | Contract pack, NIB/Sertifikat Standar, PBJT per kabupaten/kota | Menerbitkan invoice tanpa entity & struktur pajak disetujui |
| Finance | Ledger, rekonsiliasi, refund path | Rilis dana tanpa approval berjenjang |
| AI co-pilot | Data ber-ID, policy snapshot | Mengubah status booking, menghitung harga final, menerima PII sensitif |

## 4. Ruang lingkup produk (fase)

- **Phase 0 (DONE)** — charter, legal boundary, contract pack, policy matrix, state machine, klasifikasi data.
- **Phase 1 (IN PROGRESS)** — Reservation Kernel: data model transaksional, proteksi overlap, transisi status eksplisit, policy + quote calculator, audit append-only.
- **Phase 2** — case management, intake terstruktur, matching yang bisa dijelaskan, approval queue + draft pesan (semua pesan eksternal masih direview manusia).
- **Phase 3** — invoice, hosted payment, webhook + rekonsiliasi idempoten, hold timer, dokumen, task operasional, incident playbook.
- **Phase 4** — integrasi kanal resmi, adapter inventory campuran, dashboard internal, shadow-mode evaluation, go-live readiness.

**Di luar lingkup MVP:** microservices, Kafka, voice bot otonom, custody dana
owner, OTA scraping, otomatisasi WhatsApp personal, dynamic pricing otonom.

## 5. Metrik sukses (baseline dulu, target kemudian)

| Kategori | Metrik | Target awal |
|---|---|---|
| Reliability | Double booking | 0 / bulan |
| Reliability | Quote error | < 1% |
| Reliability | False availability yang tidak dieskalasi | 0 |
| Financial | Payment reconciliation match rate | ≥ 99% |
| Financial | Unmatched payment diselesaikan | < 48 jam |
| Speed | First response (jam operasional) | < 15 menit |
| Speed | Jawaban ketersediaan / quote awal | < 2 jam / < 4 jam |
| Service | Complaint kritis direspons / diselesaikan | < 1 jam / < 24 jam |
| AI governance | Unsupported claim rate | 0 ditoleransi |
| AI governance | PII incident | 0 |

Baseline conversion funnel (inquiry→qualified→quote→hold→paid→confirmed)
diukur **4–6 minggu pertama per kanal** sebelum angka target ditetapkan.

## 6. Asumsi legal & kepatuhan (wajib diverifikasi)

1. Setiap villa yang dijual wajib punya **NIB** dan **Sertifikat Standar Usaha Pariwisata** valid; properti tanpa izin valid = `NOT_SELLABLE`.
2. **PBJT** jasa akomodasi maksimal **10%**, tarif ditetapkan per kabupaten/kota (Bali vs Yogyakarta berbeda — tidak boleh hard-code satu tarif).
3. Invoice & instruksi settlement domestik dalam **Rupiah** (kewajiban mata uang NKRI); valas hanya sebagai referensi kurs.
4. Pembayaran melalui **PSP berlisensi PJP**; sistem tidak menyimpan data kartu (PCI scope dikurangi).
5. Data pribadi tunduk **UU 27/2022 (PDP)**: minimisasi, retention, hak akses/koreksi/hapus.
6. Dokumen pesan dari luar (email/PDF/chat) adalah **untrusted data**, bukan instruksi.

## 7. Open decisions (butuh keputusan manusia — blokir go-live)

| # | Keputusan | Pemilik | Status |
|---|---|---|---|
| D1 | Merchant of record & pemilik akun PSP | Owner + legal | **OPEN** |
| D2 | Penerbit invoice (entitas agency vs owner) | Owner + tax advisor | **OPEN** |
| D3 | Alur dana: custody vs split langsung ke owner | Owner + finance + legal | **OPEN** |
| D4 | Tanggung jawab refund & chargeback | Owner + legal | **OPEN** |
| D5 | Jumlah properti pilot final (10–20?) | Owner | **OPEN** |
| D6 | Struktur PBJT per kabupaten/kota + PND | Tax advisor | **OPEN** |
| D7 | Provider WhatsApp Business resmi + consent template | Owner | **OPEN** |
| D8 | Angka threshold go-live shadow mode | Owner + reservation manager | **OPEN** |
| D9 | Roster on-call & after-hours coverage | Reservation manager | **OPEN** |
| D10 | Model komisi vs management fee per properti | Owner | **OPEN** |

**Aturan keras:** selama D1–D4 masih OPEN, sistem **tidak boleh** menerima dana
tamu produksi. Development, paper, dan shadow mode tetap boleh berjalan.

## 8. Risiko utama & mitigasi

| Risiko | Dampak | Mitigasi di sistem |
|---|---|---|
| Double booking | Tinggi / tinggi | Lock transaksional + proteksi overlap (`reservation_kernel`) |
| Policy berbeda per properti dilupakan | Sedang / tinggi | Policy versioned per properti/kanal/booking window + snapshot di quote |
| Availability basi dianggap pasti | Sedang / tinggi | Level bukti A–D + `can_confirm` + freshness window |
| Kawin channel (OTA vs internal) | Sedang / tinggi | Precedence kebijakan kanal + sellability; iCal = indikatif saja |
| Prompt injection dari dokumen tamu | Sedang / tinggi | Semua konten eksternal = data; tool permission; tidak ada status dari teks |
| Payment mismatch / screenshot palsu | Sedang / tinggi | Ledger + webhook idempotent; screenshot tidak pernah `CLEARED` |
| Force majeure regional | Rendah–sedang / tinggi | Klausul force majeure per properti + crisis playbook + backup inventory |
| Properti belum patuh PBJT/izin | Sedang / tinggi | Contract pack gate → `NOT_SELLABLE` otomatis |
| Instruksi bayar valas untuk transaksi domestik | Rendah / tinggi | Invoice & settlement selalu Rupiah; PSP berlisensi untuk konversi |

## 9. Approval

| Peran | Nama | Tanggal | Keputusan |
|---|---|---|---|
| Agency owner | ___ | ___ | ___ |
| Reservation manager | ___ | ___ | ___ |
| Legal/tax advisor | ___ | ___ | ___ |
| Finance | ___ | ___ | ___ |

> Dokumen ini rancangan operasional, bukan opini hukum. Implementasi di Indonesia
> memerlukan review counsel lokal untuk entity agency, kontrak owner, pajak/PND,
> consumer protection, tourism licensing, cross-border data transfer, e-signature,
> dan payment/PSP arrangement.
