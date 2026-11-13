# Villa Agency OS — Panduan Lengkap Alur Kerja

**Untuk siapa dokumen ini:** pemilik agensi, staf baru, calon pembeli lisensi, dan siapa pun yang perlu paham cara kerja sistem **tanpa** latar belakang teknis.
**Tanggal:** 29 Oktober 2026 · **Versi sistem:** Phase 0–4 (kernel operasi + dashboard internal + situs publik)
**Berkas rujukan teknis:** `README.md`, `docs/BOOKING_STATE_MACHINE.md`, `docs/OPERATIONS_RUNBOOK.md`, `docs/POLICY_MATRIX.md`

---

## 1. Satu paragraf: ini sistem apa?

Villa Agency OS adalah **sistem operasi untuk agensi villa** — bukan aplikasi booking biasa. Ia mencatat dan mengurus tiga hal yang paling sering menimbulkan kerugian di bisnis villa:

1. **Tanggal villa** — siapa memakai unit mana, kapan, dan apakah benar-benar masih kosong.
2. **Uang** — berapa yang harus ditagih, sudah masuk berapa, cocok dengan invoice mana.
3. **Bukti** — siapa memutuskan apa, kapan, atas dasar data apa.

Semua yang berbau angka, tanggal, status, dan uang dikerjakan oleh bagian sistem yang **deterministik** (hasilnya selalu sama untuk input yang sama, bisa dihitung ulang). Bahasa manusia dan AI masuk **paling akhir**, dan hanya untuk merangkai fakta yang sudah tercatat — bukan untuk mengarang fakta baru.

**Analogi:** anggap sistem ini sebagai **buku besar + papan jadwal + lemari arsip** yang dijaga ketat, lalu di depannya ada petugas (manusia/AI) yang boleh menulis surat, tapi tidak boleh mengubah angka di buku besar.

---

## 2. Apa yang sistem ini TIDAK lakukan

Penting disebut lebih dulu supaya tidak ada salah harap:

| Tidak dilakukan | Kenapa |
|---|---|
| Menerima pembayaran langsung dari kartu tamu | Pembayaran masuk lewat penyedia (payment gateway) / transfer bank; sistem hanya **mencocokkan** bukti yang masuk |
| Mengubah status booking karena ada orang mengetik "sudah dibayar" di chat | Status hanya berubah lewat perintah tervalidasi (state machine), bukan teks bebas |
| Menjanjikan villa tersedia kalau data ketersediaan belum terbukti | Aturan **fail-closed**: data hilang, basi, atau timeout = `UNKNOWN`, bukan "tersedia" |
| Membiarkan AI menghitung harga atau memastikan ketersediaan | AI tidak punya kewenangan angka; ia menyusun kalimat dari fakta ber-ID |
| Membuka dashboard ke internet begitu saja | Dashboard internal hanya untuk loopback/VPN; membuka ke publik butuh lapisan login tambahan yang **belum** dibangun |

---

## 3. Tiga pintu masuk ke sistem

Setiap permintaan tamu masuk lewat salah satu dari tiga pintu, dan semuanya bermuara ke tempat yang sama: **kasus (case)**.

```
(1) Situs publik  ──┐
    villa & inquiry │
                    ├──►  KASUS  ──►  alur reservasi  ──►  uang  ──►  operasi
(2) Kanal pesan  ───┤     (case)        (hold, quote,      (invoice,   (tugas,
    WhatsApp/IG/email│                   booking)           pembayaran) insiden)
                    │
(3) Operator      ──┘
    mengetik manual
```

**Pintu 1 — Situs publik.** `src/marketing_site.py` melayani tujuh rute: beranda, katalog villa, detail villa, destinasi Bali, destinasi Yogyakarta, halaman pemilik, dan formulir *inquiry*. Situs ini **hanya membaca** data publik (villa, foto, harga tampil) dan mengirim pertanyaan ke sistem sebagai kasus baru. Ia tidak pernah menulis harga, tidak pernah mengunci tanggal.

**Pintu 2 — Kanal pesan.** Pesan dari WhatsApp/Instagram/email masuk lewat *connector* (penghubung). Setiap pesan disimpan dengan ID unik agar tidak diproses dua kali (idempoten) — pesan yang terkirim ulang oleh penyedia tidak membuat dua kasus.

**Pintu 3 — Operator.** Staf reservasi boleh membuat kasus manual untuk tamu yang menelepon atau datang langsung. Semua isian tetap melewati validasi yang sama.

**Yang menyatukan ketiganya:** `intake`. Di sini pertanyaan mentah ("ada villa 3 kamar dekat Seminyak tanggal 20?") diubah menjadi **syarat terstruktur**: jumlah tamu, tanggal masuk/keluar, area, anggaran, kebutuhan khusus. Hasilnya bisa berupa *proposal* yang harus **disetujui manusia** sebelum menjadi kasus resmi — AI boleh mengusulkan isian, tidak boleh menetapkannya.

---

## 4. Perjalanan satu booking, langkah demi langkah

Bagian ini adalah inti dokumen. Contoh: **Ibu Sari** mengirim pesan 12 Oktober untuk villa 3 kamar di Seminyak, 20–24 November, 6 tamu.

Setiap langkah ditulis dengan tiga hal: **siapa yang bergerak**, **apa yang dicatat sistem**, dan **apa yang bisa gagal**.

### Langkah 1 — Pertanyaan masuk (INQUIRY)

- **Siapa:** tamu (lewat situs/kanal) atau staf reservasi.
- **Yang dicatat:** kasus baru dengan nomor, sumber (situs/WhatsApp/telepon), isi pesan asli, waktu masuk, dan status `INQUIRY`.
- **Yang bisa gagal:** pesan ganda dari penyedia. Ditangani oleh kunci idempotensi — pesan kedua dengan ID sama tidak membuat kasus baru.

### Langkah 2 — Pertanyaan disaring (QUALIFIED)

- **Siapa:** staf reservasi (dibantu usulan AI).
- **Yang dicatat:** tanggal, jumlah tamu, area, anggaran, kebutuhan (bayi/kolam/aksesibilitas). Status naik ke `QUALIFIED` hanya jika syarat minimum terisi.
- **Yang bisa gagal:** tamu belum jelas tanggalnya. Kasus tetap `INQUIRY`; sistem tidak memaksa menebak.

### Langkah 3 — Cek ketersediaan (AVAILABILITY_PENDING → CONFIRMED / UNAVAILABLE)

- **Siapa:** sistem, memakai data ketersediaan yang **bertanggal** (bukan ingatan orang).
- **Yang dicatat:** `availability_checks` + `availability_evidence` — kapan dicek, dari sumber mana, berlaku sampai kapan.
- **Aturan keras:** kalau bukti hilang, terlalu tua, atau pemeriksaan timeout, hasilnya **UNKNOWN** dan status tetap menunggu. Sistem menolak menjawab "tersedia" tanpa bukti.
- **Yang bisa gagal:** villa baru saja diambil orang lain. Karena itu hasil cek punya masa berlaku; cek kedaluwarsa tidak boleh dipakai untuk konfirmasi.

### Langkah 4 — Penawaran harga (QUOTE_DRAFTED → QUOTE_SENT)

- **Siapa:** staf reservasi menyusun penawaran; sistem menghitung angka.
- **Yang dicatat:** `quotes` + `quotation_snapshots`. Harga dihitung dari **rate card** dan **policy** yang berlaku, lalu **disimpan sebagai potret** (snapshot) — bukan tautan ke harga hari ini yang bisa berubah besok.
- **Mengapa snapshot:** kalau tarif berubah minggu depan, penawaran yang sudah dikirim tetap bisa dipertanggungjawabkan angkanya.
- **Yang bisa gagal:** penawaran kedaluwarsa (`QUOTE_EXPIRED`). Tamu yang kembali setelah masa berlaku harus diberi penawaran baru.

### Langkah 5 — Menahan tanggal (tiga tahap hold)

Ini bagian yang paling sering disalahpahami, jadi ditulis terpisah di §5. Ringkasnya:

| Bahasa tim | Arti sebenarnya | Uang boleh masuk? |
|---|---|---|
| **Tentative Holding** | Unit diminta ditahan, villa **belum** menjawab | Tidak |
| **Request Hold** | Villa **sudah** mengonfirmasi tahan | Ya, setelah konfirmasi |
| **Holding Booking Confirmed** | Dana terverifikasi + bukti blok dari villa | Ya, dan tanggal terkunci |

### Langkah 6 — Uang masuk (PAYMENT_PENDING → DEPOSIT_PAID / PAID_IN_FULL)

- **Siapa:** tamu membayar lewat kanal resmi; sistem menerima **bukti** (webhook penyedia atau mutasi yang diunggah).
- **Yang dicatat:** `payments`, `payment_events`, `ledger_entries`, `invoices`, `invoice_lines`.
- **Aturan:** semua nominal adalah **bilangan bulat dalam satuan terkecil** (rupiah utuh, sen untuk USD). Tidak ada pecahan mengambang yang bisa membuat selisih satu sen hilang.
- **Yang bisa gagal:** pembayaran masuk tanpa invoice yang cocok → `PAYMENT_UNMATCHED`. Ini **bukan** error; ini antrean kerja untuk bagian keuangan (§7).

### Langkah 7 — Konfirmasi booking (CONFIRMED)

- **Siapa:** staf reservasi, setelah dana terverifikasi **dan** bukti blok villa diterima.
- **Yang dicatat:** transisi status + alasan + bukti, di `booking_state_events` (jurnal yang tidak bisa dihapus).
- **Penting:** "uang sudah masuk" ≠ "villa sudah dikonfirmasi". Status `DEPOSIT_PAID` berarti uang masuk; status `CONFIRMED` berarti villa sudah memblokir tanggal. Laporan yang mencampur keduanya akan salah hitung.

### Langkah 8 — Sebelum tamu datang (PRE_ARRIVAL)

- **Siapa:** tim operasi.
- **Yang dicatat:** `ops_tasks` — kebersihan, check-in, transport, permintaan khusus; masing-masing punya tenggat dan bukti penyelesaian.
- **Yang bisa gagal:** tugas lewat tenggat. Sistem menandai SLA terlampaui dan menaikkannya ke antrean perhatian.

### Langkah 9 — Tamu menginap (IN_STAY)

- **Siapa:** tim operasi.
- **Yang dicatat:** insiden (kalau ada), permintaan tambahan, perubahan tanggal (kalau ada), dan bukti penyelesaian.
- **Aturan:** keluhan tamu **tidak** mengubah status booking secara otomatis; ia menjadi insiden dengan langkah penanganan sendiri. Dua hal berbeda tidak dicampur.

### Langkah 10 — Selesai (COMPLETED → CLOSED)

- **Siapa:** tim operasi menutup layanan; keuangan menutup tagihan; sistem menutup kasus.
- **Yang dicatat:** ringkasan, bukti, dan status akhir. `CLOSED` bersifat terminal — tidak ada jalan kembali; koreksi dilakukan dengan kasus baru yang merujuk kasus lama, bukan dengan mengubah sejarah.

### Kalau batal di tengah jalan

Pembatalan punya jalurnya sendiri: `CANCELLATION_REQUESTED → CANCELLATION_REVIEW → CANCELLED → REFUND_PENDING → REFUNDED`. Selama masih dalam peninjauan, tanggal **tetap tertahan** — sengaja: lebih baik unit tertahan beberapa jam daripada dua tamu datang ke villa yang sama.

---

## 5. Tiga tahap hold, dijelaskan dengan analogi

Bayangkan kursi di bioskop:

1. **Tentative Holding** = Anda bilang ke kasir "tolong tahan dulu kursi ini, saya tanya teman saya". Kasir **belum** menekan tombol. Kursi belum resmi milik Anda, dan kasir boleh melepasnya kapan saja.
2. **Request Hold** = kasir menekan tombol tahan. Kursi terkunci untuk Anda selama beberapa jam, tapi tiket belum dibayar.
3. **Holding Booking Confirmed** = Anda sudah bayar, tiket tercetak, dan pintu bioskop mencatat kursi itu milik Anda.

Kesalahan yang mahal: menyebut tahap 3 sebagai "sudah confirmed" padahal yang dimaksud baru "uang masuk". Di sistem ini keduanya **status berbeda**, dan laporan keuangan memakai status uang, sedangkan laporan operasional memakai status blok villa.

---

## 6. Alur uang: dari penawaran sampai setoran ke pemilik villa

```
Penawaran (quote, snapshot harga)
      │  tamu setuju
      ▼
Invoice (rincian: malam, pajak, layanan, diskon)
      │  tamu bayar lewat kanal resmi
      ▼
Bukti pembayaran masuk  ──►  Rekonsiliasi: cocokkan nominal + tanggal + referensi
      │                                   │
      │ cocok                             │ tidak cocok
      ▼                                   ▼
PAID / DEPOSIT_PAID                 PAYMENT_UNMATCHED (antrean keuangan, SLA 48 jam)
      │
      ▼
Kewajiban ke pemilik villa (supplier settlement)
      │  butuh persetujuan (uang keluar)
      ▼
Pembayaran ke pemilik  ──►  bukti transfer dicatat  ──►  selesai
```

Empat penjaga di jalur uang:

1. **Tidak ada angka pecahan mengambang.** Semua uang bilangan bulat satuan terkecil.
2. **Tidak ada tagihan tanpa asal.** Setiap baris invoice harus bisa ditelusuri ke rate card/policy bertanggal atau ke biaya yang disetujui.
3. **Uang keluar butuh dua tanda tangan.** Refund, payout ke pemilik villa, dan perubahan rekening bank wajib **dua penyetuju berbeda** — dan **pemohon tidak boleh menyetujui permintaannya sendiri**.
4. **Kurs punya masa berlaku.** Untuk booking dalam USD (Bali) atau IDR (Yogyakarta), konversi memakai kurs harian yang disetujui. Kurs lebih tua dari 48 jam **ditolak** (`FX_RATE_STALE`), bukan dipakai diam-diam.

**Yang tidak dilakukan sistem:** ia tidak "memindahkan uang". Ia mencatat, mencocokkan, dan meminta persetujuan; eksekusi transfer tetap di rekening bank/penyedia pembayaran.

---

## 7. Antrean yang harus dikosongkan setiap hari

Sistem ini sengaja menampilkan **pekerjaan yang belum selesai** sebagai antrean, bukan menyembunyikannya di laporan.

| Antrean | Isi | Kenapa mahal kalau menumpuk |
|---|---|---|
| **Approval** | permintaan yang menunggu tanda tangan | uang keluar tertahan, tamu menunggu refund |
| **Rekonsiliasi** | pembayaran yang belum cocok dengan invoice | pemasukan tidak terlihat, tagihan salah kirim |
| **Hold kedaluwarsa** | tanggal tertahan yang waktunya habis | villa tidak bisa dijual padahal tidak ada tamu |
| **SLA kasus** | kasus yang lama tidak bergerak | tamu kabur ke agensi lain |
| **SLA tugas** | tugas operasi lewat tenggat | tamu datang ke villa yang belum siap |
| **Insiden** | masalah yang belum ditutup | keluhan berulang, reputasi turun |

**Ritme yang disarankan** (dari runbook operasi, sudah disederhanakan):

- **Pagi (±30 menit):** cek kesehatan sistem → buka Command Center → kosongkan approval & rekonsiliasi → perbarui kurs harian.
- **Sepanjang hari:** heartbeat otomatis tiap 5 menit menagih hold kedaluwarsa, approval lewat waktu, SLA kasus/tugas, dan rekonsiliasi. Manusia hanya perlu menindaklanjuti yang muncul.
- **Mingguan:** tinjauan angka (okupansi, margin, pembatalan), pemeriksaan bukti, dan penutupan kasus lama.
- **Bulanan:** audit sampel, rotasi kredensial, dan tinjauan policy.

---

## 8. Siapa boleh apa: enam peran dan dua belas halaman

Dashboard internal punya **6 peran** dan **12 halaman**. Aturannya bukan "sekadar tampilan" — server menolak permintaan yang tidak sesuai peran, dan ini sudah diuji lewat HTTP nyata (72 kombinasi peran × halaman, 0 penyimpangan).

| Halaman | Isi | reservation | finance | operations | owner | auditor | gm |
|---|---|:--:|:--:|:--:|:--:|:--:|:--:|
| Command Center | ringkasan semua antrean | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Cases | kasus & pesan tamu | ✓ | – | ✓ | ✓ | ✓ | ✓ |
| Inventory | unit & ketersediaan | ✓ | – | ✓ | ✓ | – | – |
| Finance | invoice, pembayaran, rekonsiliasi | – | ✓ | – | ✓ | – | ✓ |
| Operations | tugas & insiden | ✓ | – | ✓ | ✓ | – | – |
| Approvals | antrean persetujuan | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Properties | data villa & kontrak | ✓ | – | ✓ | ✓ | ✓ | ✓ |
| Marketing | katalog & inquiry situs | ✓ | – | ✓ | ✓ | – | – |
| Channels | kanal pesan & pengiriman | ✓ | – | ✓ | ✓ | ✓ | – |
| Shadow Eval | perbandingan keputusan AI vs manusia | – | – | ✓ | ✓ | ✓ | – |
| Outbox | pesan keluar & status kirim | – | – | ✓ | ✓ | ✓ | – |
| Audit | jejak audit (baca saja) | – | – | – | ✓ | ✓ | – |

Dua aturan tambahan yang tidak terlihat di tabel:

- **Pemohon ≠ penyetuju.** Orang yang mengajukan refund tidak boleh menjadi salah satu penandatangannya.
- **Uang keluar = dua orang.** Refund, payout, dan perubahan rekening butuh dua persetujuan berbeda; sistem menolak permintaan yang hanya meminta satu.

**Cara masuk (sejak 29 Oktober 2026):** ada dua jenis kredensial — *token mesin* untuk skrip/cron, dan *sesi operator* untuk manusia. Sesi operator menyebut **siapa** yang masuk, berlaku 30 menit tanpa aktivitas (maksimal 12 jam), dan bisa dicabut seketika. Peran owner, finance, dan GM wajib kode TOTP saat membuka sesi. Panduan langkahnya: `docs/security/SESSION_AUTH.md`.

---

## 9. Peran AI: apa yang boleh dan tidak boleh

AI di sistem ini bekerja seperti **asisten yang sangat cepat membaca, tetapi tidak memegang kasir**.

| Boleh | Tidak boleh |
|---|---|
| Merangkum percakapan tamu | Menghitung harga atau mengubah nominal |
| Mengusulkan isian intake (tanggal, jumlah tamu) — untuk disetujui manusia | Menetapkan ketersediaan villa |
| Menyusun draft balasan dari template + fakta ber-ID | Mengubah status booking |
| Menandai kasus yang perlu perhatian | Menyetujui uang keluar |
| Menjawab pertanyaan berdasarkan dokumen yang ada | Mengarang fakta yang tidak ada di sistem |

Tiga pengaman:

1. **Daftar larangan (deny registry) berasal dari policy**, bukan dari percakapan. Instruksi dalam chat tidak pernah menambah kewenangan AI.
2. **Kewenangan datang dari identitas & alat yang terdaftar**, bukan dari teks permintaan. Kalimat "anggap saya owner" tidak mengubah apa pun.
3. **Setiap keputusan AI punya jejak**: apa yang diusulkan, apa yang diputuskan manusia, dan apakah keduanya berbeda (halaman *Shadow Eval*).

---

## 10. Bukti: mengapa setiap tindakan meninggalkan jejak

Prinsipnya satu: **kalau tidak ada bukti di sistem, tindakan itu belum selesai.**

| Jenis bukti | Isi | Sifat |
|---|---|---|
| Jurnal status booking | dari status apa ke apa, oleh siapa, kapan, alasan | tidak bisa diubah/dihapus |
| Jurnal audit | setiap aksi penting, dirantai dengan hash | bila satu baris diubah, rantai putus dan terdeteksi |
| Bukti ketersediaan | sumber, waktu cek, masa berlaku | kedaluwarsa = tidak dipakai |
| Bukti pembayaran | referensi, nominal, tanggal, pencocokan | harus cocok dengan invoice |
| Bukti persetujuan | siapa menyetujui, atas payload apa | status diturunkan dari keputusan, bukan dari kolom |
| Idempotency key | kunci unik per perintah | perintah ganda tidak menggandakan efek |

**Analogi:** ini seperti buku kas yang setiap halamannya diberi nomor seri dan stempel waktu. Bisa saja seseorang memotret halaman lalu mengubah fotonya, tetapi buku aslinya akan langsung terlihat tidak nyambung.

---

## 11. Kalau ada yang salah: perilaku sistem saat gagal

Sistem ini memilih **berhenti dan bertanya** daripada melanjutkan dengan tebakan.

| Situasi | Perilaku | Yang harus dilakukan manusia |
|---|---|---|
| Data ketersediaan hilang/basi | status `UNKNOWN`, tidak bisa konfirmasi | cek ulang ke villa, unggah bukti baru |
| Pembayaran tidak cocok | `PAYMENT_UNMATCHED` | cocokkan manual, jangan paksa "sudah bayar" |
| Kurs lebih tua dari 48 jam | transaksi konversi ditolak | perbarui kurs dengan persetujuan owner |
| Dua permintaan menyentuh tanggal sama | yang kedua ditolak oleh penguncian database | tawarkan alternatif/unit lain |
| Pesan terkirim dua kali | yang kedua diabaikan (idempotensi) | tidak perlu tindakan |
| Antrean pesan keluar gagal kirim | dicoba ulang, lalu ditandai gagal | periksa kanal, kirim ulang dari Outbox |
| Rantai audit tidak valid | sistem melaporkan CRITICAL dan berhenti mempercayai laporan | hentikan perubahan, panggil penanggung jawab teknis |

**Bukan** perilaku sistem: menebak, "kira-kira tersedia", memakai kurs lama, atau menandai lunas tanpa bukti.

---

## 12. Satu hari di agensi: cerita utuh

**07:50** — Staf operasi membuka Command Center. Tiga approval menunggu, satu invoice lewat jatuh tempo, dua hold akan kedaluwarsa siang ini.

**08:10** — Kasus Bu Sari (Seminyak, 20–24 Nov) masuk dari situs. Intake mengusulkan tanggal dan jumlah tamu; staf reservasi menyetujui usulan itu (bukan AI yang menetapkan).

**08:25** — Sistem mengecek ketersediaan. Bukti dari villa berumur 3 jam → masih berlaku → `AVAILABILITY_CONFIRMED`.

**08:40** — Penawaran dihitung dari rate card November dan dikirim. Angkanya tersimpan sebagai snapshot.

**11:05** — Tamu setuju. Hold diminta; villa mengonfirmasi tahan pukul 11:20. Status `HOLD_ACTIVE`.

**13:30** — Transfer DP masuk. Keuangan mencocokkan dengan invoice: nominal dan referensi cocok → `DEPOSIT_PAID`.

**14:00** — Bukti blok dari villa diterima → `CONFIRMED`. Tanggal kini terkunci; tidak ada agensi lain yang bisa menjual unit itu.

**Sore** — Tiga tugas pra-kedatangan dibuat: kebersihan, check-in, transport. Masing-masing bertenggat dan menunggu bukti.

**Malam** — Pemilik villa mengajukan payout. Sistem menuntut dua persetujuan; pemohon tidak bisa menyetujui sendiri. Owner menyetujui, finance manager menyetujui, baru transfer dicatat.

Semua langkah di atas bisa ditelusuri ulang minggu depan, bulan depan, atau saat audit — dari satu nomor kasus.

---

## 13. Peta teknis singkat (untuk yang ingin melihat isi mesin)

| Bagian | Isi | Angka |
|---|---|---|
| Modul inti | `src/*.py` | 57 modul (potret 2026-10-29; **58** per 2026-11-13) |
| Tabel database | `src/schema/001_initial.sql` + migrasi | 72 tabel di skema awal; 73 di database berjalan |
| Status booking | state machine tunggal | 32 status, 120 transisi sah dari 1.024 pasangan |
| Halaman dashboard | `PAGE_ROLES` | 12 halaman, 6 peran |
| Rute situs publik | `marketing_site.py` | 7: beranda, katalog, detail villa, destinasi Bali, destinasi Yogyakarta, pemilik, inquiry |
| Tes otomatis | `tests/` | 3.802 tes (lihat `make check`) |
| Bahasa | Python stdlib-only | tanpa dependensi eksternal di `src/` |

Arsitektur satu arah: **situs publik → kasus → reservasi → uang → operasi → bukti**, dengan satu penulis per tabel dan satu jurnal audit.

---

## 14. Batas hari ini (jujur, bukan jualan)

| Sudah jalan | Belum |
|---|---|
| Kernel reservasi, uang, dokumen, kanal, bukti | Login berlapis untuk dashboard yang dibuka ke internet (kata sandi + TOTP untuk semua peran) |
| Situs publik 7 rute (loopback, di belakang proxy) | `robots.txt` & `sitemap.xml` (SEO) |
| Sesi operator + MFA untuk owner/finance/GM | Uji penetrasi eksternal independen |
| Rekonsiliasi, approval dual-control, audit hash-chain | Integrasi payment gateway produksi (tergantung penyedia per agensi) |
| 3.802 tes otomatis + pre-deploy gate | Rotasi kredensial terjadwal otomatis (saat ini dijalankan manual tiap 90 hari) |

Cara membaca tabel ini: kolom kiri adalah yang **sudah dibuktikan dengan tes/uji nyata**; kolom kanan adalah pekerjaan yang diketahui dan **tidak disembunyikan**.

---

## 15. Glosarium

| Istilah | Arti sederhana |
|---|---|
| **Kasus (case)** | Satu percakapan/permintaan tamu, dari pertanyaan sampai selesai |
| **Hold** | Tanggal ditahan sementara agar tidak dijual ke orang lain |
| **Quote** | Penawaran harga resmi, disimpan sebagai potret |
| **Invoice** | Tagihan rinci yang harus dibayar tamu |
| **Rekonsiliasi** | Mencocokkan uang yang masuk dengan tagihan yang dikirim |
| **Settlement** | Pembayaran dari agensi ke pemilik villa |
| **Approval** | Persetujuan manusia sebelum aksi berisiko (uang keluar, refund, ganti rekening) |
| **Fail-closed** | Kalau ragu, tolak/berhenti — bukan lanjut dengan tebakan |
| **Idempoten** | Perintah yang sama dijalankan dua kali tetap memberi hasil satu kali |
| **Snapshot** | Salinan nilai pada saat itu, tidak ikut berubah bila sumber berubah |
| **Audit chain** | Jurnal yang setiap barisnya dikunci dengan hash baris sebelumnya |
| **Shadow Eval** | Membandingkan usulan AI dengan keputusan manusia, untuk mengukur kualitas AI |

---

## 16. Satu halaman ringkasan untuk dinding kantor

1. **Tanggal** hanya bisa dikunci lewat hold yang dikonfirmasi villa.
2. **Uang** selalu bilangan bulat; tidak ada tagihan tanpa asal.
3. **Uang keluar** butuh dua tanda tangan; pemohon tidak boleh menyetujui sendiri.
4. **Status** hanya berubah lewat perintah tervalidasi — bukan karena chat.
5. **Data hilang/basi** = `UNKNOWN`, bukan "tersedia".
6. **AI** menyusun kalimat, bukan angka/status/kepastian.
7. **Setiap tindakan** meninggalkan bukti yang tidak bisa dihapus.
8. **Kalau ragu**, berhenti dan tanya — sistem dirancang untuk itu.
