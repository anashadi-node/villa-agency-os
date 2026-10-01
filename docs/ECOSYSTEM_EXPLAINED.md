# Villa Agency OS — Dijelaskan dari Depan, Bahasa Awam

**Untuk:** Owner Villa Agency (Bali & Yogyakarta)
**Tanggal:** 2026-09-28
**Tujuan dokumen ini:** menjawab kebingungan *"apa yang sedang dibuat, dan bagaimana ini?"* — dari nol, berurutan, tanpa istilah yang tidak dijelaskan.

---

## Daftar Isi

1. [Jawaban singkat: apa yang sedang dibuat](#bab-1)
2. [Dua lapis yang berbeda: GEDUNG dan PEGAWAI](#bab-2)
3. [Lapis 1 — Gedungnya: apa yang SUDAH dibangun](#bab-3)
4. [Organisasi manusia (dari dokumen Anda)](#bab-4)
5. [Enam Pegawai AI — satu per satu, dengan analogi](#bab-5)
6. [Kenapa "worker" bukan berarti pegawai baru](#bab-6)
7. [Lapis 2 — Yang BELUM ada (jujur, apa adanya)](#bab-7)
8. [Kenapa urutannya harus begitu, tidak bisa dibalik](#bab-8)
9. [Apa yang menahan go-live (bukan software)](#bab-9)
10. [Provider pembayaran — kriteria, bukan tebakan](#bab-10)
11. [Yang saya butuhkan dari Anda](#bab-11)
12. [Kamus istilah: teknis → bahasa awam](#bab-12)
13. [Ringkasan satu halaman](#bab-13)

---

<a id="bab-1"></a>
## Bab 1 — Jawaban singkat: apa yang sedang dibuat

**Yang sedang dibuat: satu sistem operasi untuk bisnis villa agency Anda** — yang nanti akan dijalankan oleh **campuran manusia dan enam "pegawai AI"**, persis seperti dokumen *Villa Agency AI Workforce Architecture V1.1* yang Anda kirimkan.

Analogi paling mudah: Anda sedang membangun **sebuah kantor**.

| Bagian kantor | Di proyek ini | Status |
|---|---|---|
| Gedung, ruangan, pintu, loker berkunci, alarm | **Sistem (software)** | ✅ **sudah hampir selesai** |
| Buku aturan & SOP tertulis | **Katalog kebijakan + ADR** | ✅ sudah ada |
| Para pegawai (manusia) | **Organisasi manusia** | ⏳ tinggal Anda isi namanya |
| **Para pegawai AI** | **6 AI Agent** | ❌ **belum dibangun sama sekali** |

**Ini poin terpenting, dan saya harus jujur:** gedungnya sudah berdiri dan aman, **tetapi para pegawai AI-nya belum direkrut**. Dari 6 pegawai AI di dokumen Anda, yang sudah ada: **nol**. Yang sudah ada baru **kartu identitas** mereka (siapa mereka di mata sistem dan apa yang dilarang) — bukan pegawainya.

Kalau saya tidak bilang ini, Anda akan mengira sistemnya sudah jalan sendiri. Itu tidak benar.

---

<a id="bab-2"></a>
## Bab 2 — Dua lapis yang berbeda: GEDUNG dan PEGAWAI

Ini akar kebingungannya, dan bukan salah Anda — dokumennya sendiri menggabungkan dua hal yang sangat berbeda dalam satu naskah.

**Lapis 1 — GEDUNG (sistem yang menentukan aturan).**
Ini mesin yang menyimpan data, menegakkan aturan, dan menolak hal yang salah. Ia **tidak punya kecerdasan** — ia hanya patuh. Contoh nyata: kalau seseorang mencoba menandai "dana sudah masuk" padahal bukti bank belum ada, **sistem menolak**. Bukan karena pintar, tapi karena aturannya begitu.

**Lapis 2 — PEGAWAI (yang berpikir dan bicara).**
Ini enam AI Agent. Mereka yang akan **membaca pesan tamu, menawar, menjawab pertanyaan, menyusun penawaran, dan mengirim laporan**. Mereka punya bahasa, tapi **tidak punya wewenang** — setiap tindakan mereka tetap harus lewat gedung (Lapis 1).

Analogi: **kasir bank** bisa bicara ramah dan menjelaskan produk, tapi ia tidak bisa memindahkan uangmu tanpa lewat sistem bank. Kalau ia bisa, itu bukan bank — itu masalah.

**Urutan pembangunannya wajib Gedung dulu, Pegawai kemudian.** Kalau dibalik: Anda punya chatbot yang bisa menjanjikan apa saja tanpa ada yang menghentikannya. Itu yang dicegah.

---

<a id="bab-3"></a>
## Bab 3 — Lapis 1: apa yang SUDAH dibangun

Semua yang di bawah ini **sudah ada dan sudah diuji**, bukan rencana.

| # | Bagian gedung | Fungsinya (bahasa awam) | Bukti |
|---|---|---|---|
| 1 | **Katalog kebijakan** | Daftar resmi pertanyaan bisnis yang jawabannya harus Anda tentukan, mis. "berapa % DP untuk booking jauh hari?" | **25 dari 25 sudah terjawab** |
| 2 | **Mesin status booking** | 32 tahapan perjalanan satu booking, dari "tamu bertanya" sampai "selesai" | 32 status + 116 jalur sah, diuji |
| 3 | **Mesin status hold** | 9 tahapan menahan unit villa | 9 status, diuji |
| 4 | **Mesin pembayaran** | Mencatat uang masuk, mencocokkan dengan tagihan, menandai yang belum jelas | `payments.py`, uji rekonsiliasi |
| 5 | **Mesin bukti** | Membedakan bukti kuat (dari sistem villa) dan bukti lemah (screenshot tamu) — level A sampai D | `evidence.py` |
| 6 | **Identitas & hak akses** | Siapa boleh apa; MFA untuk peran sensitif | `identity.py`, TOTP |
| 7 | **Rantai audit** | Catatan yang tidak bisa diubah diam-diam | `audit.py` + anchor |
| 8 | **Gerbang kualitas** | 3.018 tes otomatis sebelum perubahan apa pun boleh masuk | `make check` → PASS |
| 9 | **34 keputusan tercatat** | Setiap keputusan teknis/bisnis ditulis + alasannya | `docs/adr/` |

**Analogi bagian ini:** ini semua adalah *sistem saraf + pengaman gedung* — kunci pintu, alarm, CCTV, buku besar. Membosankan, tapi justru inilah yang membuat bisnis tidak bisa dibohongi.

**Yang belum ada di Lapis 1** (jujur): 3 alat penopang — **Outbox** (kurir antar-bagian yang tidak pernah hilang), **Exception Engine** (mesin penanganan kejadian tak terduga), dan **DLP** (pelindung data sensitif). Saya jelaskan di Bab 7.

---

<a id="bab-4"></a>
## Bab 4 — Organisasi manusia (dari dokumen Anda)

Anda benar menegur saya sebelumnya. Saya sempat mengusulkan *"jangan tambah peran manusia di luar 12 peran teknis"*. Itu salah. **Peran manusia bukan izin akses — peran manusia adalah tanggung jawab.** Menamainya tidak menambah risiko; yang menambah risiko adalah wewenang tanpa pengawas.

Jadi: **seluruh peran manusia di dokumen Anda diadopsi.**

| Peran manusia | Tugasnya | Yang TIDAK boleh | Boleh satu orang? |
|---|---|---|---|
| **Owner / Shareholders** | Pemilik; menerima laporan berkala (harian, mingguan, bulanan) dan mengoreksi anomali | Menjadi pembuat pembayaran yang ia setujui sendiri | ✅ boleh **satu orang** dengan GM |
| **General Manager / CEO** | Mengatur seluruh operasi; menerima laporan | Memindahkan uang sendiri | ✅ boleh **satu orang** dengan Owner sampai bisnis membesar |
| **Commercial & Marketing Manager** | Mencari tamu, kampanye, CRM | Menjanjikan harga/diskon yang tidak resmi | — |
| **Partnership Manager** | Hubungan dengan pemilik villa & manajemen villa, kontrak | Menandatangani kontrak tanpa persetujuan manusia berwenang | — |
| **Operations / Reservation Manager** | Mengawasi reservasi & operasi harian; menyetujui diskon di luar kebijakan | Menyetujui hal yang ia buat sendiri | — |
| **Reservation Agent** | Melayani tamu, membuat penawaran, hold, booking | **Menyetujui diskonnya sendiri** | ❌ wajib terpisah dari Manager |
| **Finance & Accounting Manager** | Menyetujui pembayaran & refund; mengawasi keuangan | Menjadi pembuat pembayaran yang ia setujui | ❌ wajib terpisah dari Staff |
| **Finance Staff** | Menyiapkan tagihan, mencocokkan mutasi bank, rekonsiliasi | **Menyetujui pembayaran** | ❌ wajib terpisah dari Manager |
| **Revenue Manager** | Analisa harga & pendapatan | Mengubah tarif kontrak | ✅ boleh dipegang GM bila masih ramping |
| **Technology / Security** | Menjaga sistem & keamanan | **Punya wewenang bank/pembayaran** | ❌ dilarang punya wewenang uang |
| **Legal / Compliance** | Kontrak, T&C, privasi, tinjauan hukum | — | ✅ boleh pihak eksternal |
| **Guest Service** | Melayani tamu saat menginap | — | — |
| **Auditor** | Memeriksa | **Menyetujui apa pun** (hanya boleh melihat) | ❌ read-only |

**Siapa yang memegang tiap peran (keputusan Anda, 2026-09-28):**

| Peran | Pemegang |
|---|---|
| Owner | **Mr. Owner** |
| General Manager / CEO | **Mr. CEO** |
| Commercial & Marketing Manager | **Mr. CMM** |
| Partnership Manager | **Mr. PM** |
| Operations / Reservation Manager | **Ms. RM** |
| Finance & Accounting Manager | **Ms. FAM** |
| Technology / Security Manager | **Mr. TS** |
| **Greeting Service Team** (peran BARU) | **Ms. Greeting** |

Empat posisi lain belum dipegang manusia, dan semuanya karena keputusan Anda sendiri:

| Posisi | Keputusan Anda | Artinya |
|---|---|---|
| **Revenue Manager** | ditunda dulu | laporan Revenue & BI sementara ke GM |
| **Auditor** | ditunda dulu | **rantai audit tetap jalan otomatis** — yang belum ada hanya orang yang membacanya |
| **Legal / Compliance** | kemungkinan eksternal | persetujuan legal dicatat **atas nama penasihat eksternal**, dengan surat/kontrak sebagai buktinya |
| **Finance Staff** | **DIHAPUS, digantikan AI** | peran manusia dihapus; **fungsi pembuat** pindah ke **Finance Admin AI** (belum dibangun) |
| **Reservation Agent** | **DIHAPUS, digantikan AI** | peran manusia dihapus; **fungsi pembuat** pindah ke **AI Reservation Service** (belum dibangun) |

**Owner dan GM boleh satu orang.** Hari ini Anda memberi dua nama (Mr. Owner dan Mr. CEO), jadi sekarang dua orang.

**Peran manusianya DIHAPUS — tapi fungsi pembuatnya tidak boleh ikut hilang.** Ini bedanya, dan ini inti jawaban saya.

Yang dihapus adalah **peran manusianya**. Yang **tidak** boleh dihapus adalah **fungsi pembuat**: kalau fungsi pembuat hilang, tidak ada lagi yang bisa *mengajukan* permintaan, sehingga tidak ada apa pun untuk diperiksa oleh penyetuju. **Kontrol tanpa pembuat bukan kontrol — itu pintu terbuka.**

| | Peran manusia | Fungsi pembuat |
|---|---|---|
| Finance Staff | **DIHAPUS** | dipindah ke **identitas AI Service** (`ai_service`) |
| Reservation Agent | **DIHAPUS** | dipindah ke **identitas AI Service** (`ai_service`) |

**Yang tetap MANUSIA selamanya: penyetujunya.** Ms. FAM menyetujui **uang keluar**; Ms. RM menyetujui **diskon di luar batas**. Ini bukan selera saya — dokumen Anda §11 menyebutnya **larangan mutlak**: AI tidak boleh menyetujui atau mengeksekusi pembayaran. Jadi: **AI mengerjakan, manusia menyetujui.**

**Konsekuensi jujurnya:** sampai pegawai AI #3 dan #5 dibangun, tidak ada yang menjalankan fungsi pembuat — jadi **tidak ada permintaan pembayaran dan tidak ada penawaran yang bisa dibuat**. Sistem **menolak**, bukan menyerahkan ke manusia. Artinya: **sisi pembayaran belum bisa go-live sebelum Finance Admin AI ada.**

**Satu keuntungan nyata yang ikut datang:** pembuat manusia bisa **ditelepon** — *"ini bos, transfer sekarang, nanti saya tanda tangan"* — dan itu salah satu cara paling umum agensi villa kehilangan uang. Pembuat AI tidak bisa diburu-buru atau dibujuk. Sisi lain juga nyata: AI tidak punya pertimbangan untuk kasus yang *kelihatan* benar tapi tidak — **justru karena itu penyetuju manusia jadi lebih penting, bukan kurang.**

### Empat contoh ringan nyata — siapa menyetujui apa, untuk siapa

| Kasus ringan nyata | Siapa yang **membuat** | Siapa yang **menyetujui** | Untuk apa persetujuan itu | Kalau satu orang melakukan keduanya |
|---|---|---|---|---|
| Tamu batal; DP 50% (Rp 7.500.000) harus dikembalikan | **Finance Admin AI** menyiapkan: hitung sesuai kebijakan pembatalan, lampirkan bukti transfer | **Ms. FAM** (manusia) | **uang keluar** dari rekening agency | Rp 7,5 juta bisa dikirim ke rekening mana pun dengan alasan "refund"; ketahuan saat rekonsiliasi bulanan, uangnya sudah pergi |
| Email masuk: *"rekening villa berubah, transfer ke nomor baru"* | **AI** mencatat + melampirkan emailnya | **Ms. FAM** (manusia) **+ telepon ke nomor LAMA** | **mengubah tujuan transfer** | DP tamu mendarat di rekening penipu — ini kehilangan paling umum di agensi villa & travel |
| Tamu minta diskon 25%; batas AI hanya 10% | **AI Reservation Service** menyusun penawaran + alasannya (14 malam, low season) | **Ms. RM** (manusia) | **harga di bawah batas kebijakan** | harga villa ditekan sesuka hati; margin bulanan jebol dan tidak ada yang tahu dari mana |
| Bayar villa setelah tamu check-out | **AI Finance** menghitung: *nilai booking − komisi agency − PBJT*, dicocokkan ke kontrak | **Ms. FAM** (manusia) | **membayar villa** dengan jumlah benar, sekali saja | villa dibayar terlalu banyak atau dua kali; kerugian baru terlihat berbulan-bulan kemudian |

**Jawaban singkat "siapa approve ke siapa untuk apa":**

| Alur | Pembuat | Penyetuju |
|---|---|---|
| Uang keluar: refund, bayar villa, ubah rekening | AI | **Ms. FAM** |
| Harga di bawah batas: diskon | AI | **Ms. RM** |
| Transaksi harian | — | **Owner TIDAK meng-approve.** Sesuai keputusan Anda di key #3: Owner menerima **laporan berkala** (harian/mingguan/bulanan) dan **mengoreksi anomali**. GM juga menerima laporan, bukan tombol approve. |

**Perubahan pada tabel di atas:** baris **Guest Service dihapus** dan digantikan **Greeting Service Team** yang tugasnya berbeda — **mengunjungi tamu di villa** untuk mendapat masukan, sekaligus menjaga hubungan dengan manajemen villa dan kru villa. Ini **peran lapangan**, bukan peran meja: ia perlu melihat data siapa menginap di mana dan kapan (untuk merencanakan kunjungan) serta mencatat hasil kunjungan — dan **tidak** perlu wewenang booking, pembayaran, atau persetujuan.

---

<a id="bab-5"></a>
## Bab 5 — Enam Pegawai AI, satu per satu

Ini isi dokumen Anda bagian 7 sampai 12. Saya terjemahkan ke bahasa awam.

### Pegawai AI #1 — AI Control Tower / Orchestrator
**Analogi:** **pengatur lalu lintas di bandara.** Ia tidak menerbangkan pesawat dan tidak menjual tiket — ia memastikan setiap pesan sampai ke bagian yang benar, memantau mana yang macet, dan memanggil manusia kalau ada yang tidak beres.
**Tugas:** mengarahkan pesan, mengawasi alur kerja, mendeteksi macet, mencoba ulang yang aman, mengeskalasi ke manusia.
**Tidak boleh:** menyetujui uang, mengubah rekening, mengubah bukti audit, memberi dirinya wewenang baru.
**Melapor ke:** General Manager.
**Status kode:** ❌ belum ada.

### Pegawai AI #2 — Commercial AI Agent
**Analogi:** **tim marketing.** Mencari tamu, menilai mana yang serius, menyusun materi promosi.
**Tugas:** analisa sumber tamu, skoring prospek, segmentasi CRM, draf konten & kampanye, menangkap prospek yang batal.
**Tidak boleh:** mengarang harga/ketersediaan/diskon; mengubah kontrak; membuat janji komersial.
**Melapor ke:** Commercial & Marketing Manager.
**Status kode:** ❌ belum ada.

### Pegawai AI #3 — AI Reservation Service
**Analogi:** **resepsionis depan.** Ini yang paling sering berhadapan dengan tamu.
**Tugas:** menerima pertanyaan, menggali kebutuhan tamu, mencocokkan villa, mengecek ketersediaan, menghitung harga resmi, menyusun penawaran, follow-up, mengajukan hold, memproses booking, mengurus batal/ubah, komunikasi sebelum kedatangan.
**Tidak boleh:** mengarang ketersediaan/harga/fasilitas; mengklaim hold sudah dikonfirmasi padahal belum; mengklaim pembayaran sudah masuk padahal belum; memasukkan data paspor ke percakapan AI.
**Melapor ke:** Operations / Reservation Manager.
**Status kode:** ❌ belum ada. **Ini yang paling berdampak bisnis**, jadi sebaiknya dibangun lebih dulu.

### Pegawai AI #4 — Operations AI Agent
**Analogi:** **supervisor operasional.** Ia berjalan keliling memastikan tidak ada langkah yang terlewat.
**Tugas:** memantau tenggat, mendeteksi dokumen yang kurang, QC booking, kesiapan sebelum tamu datang, membuat tugas perbaikan, mendeteksi kejadian tak biasa.
**Tidak boleh:** mengubah catatan keuangan; menandai dana masuk; memaksa status booking.
**Melapor ke:** Operations Manager.
**Status kode:** ❌ belum ada.

### Pegawai AI #5 — Finance Admin AI Agent
**Analogi:** **staf administrasi keuangan.** Ia mencatat, mencocokkan, dan menyiapkan berkas — tapi **tangannya tidak boleh menyentuh uang**.
**Tugas:** memantau tagihan, mencocokkan mutasi bank dengan tagihan, menemukan pembayaran kembar/nyasar, menyiapkan permintaan pembayaran untuk disetujui manusia, membuat laporan.
**Tidak boleh (mutlak):** menyetujui/mengeksekusi pembayaran; menyetujui refund; mengubah tujuan bank; mengakses kunci privat; menandai dana belum terverifikasi sebagai diterima.
**Melapor ke:** Finance & Accounting Manager, **dan langsung ke GM + Owner** untuk laporan/pengecualian.
**Status kode:** ❌ belum ada. **Ini nomor dua yang paling penting**, karena menyambung alur pembayaran yang baru Anda putuskan.

### Pegawai AI #6 — Revenue & Business Intelligence AI Agent
**Analogi:** **analis bisnis.** Ia menghitung, membandingkan, dan meramal.
**Tugas:** analisa pendapatan, laju booking, permintaan, harga, performa villa/mitra/kanal, ramalan, deteksi anomali, laporan eksekutif.
**Tidak boleh:** mengubah harga resmi; memalsukan data pesaing; menyajikan ramalan sebagai fakta.
**Melapor ke:** Revenue Manager, atau GM bila masih ramping, **dan langsung ke GM + Owner**.
**Status kode:** ❌ belum ada.

---

<a id="bab-6"></a>
## Bab 6 — Kenapa "worker" bukan berarti pegawai baru

Di dokumen Anda ada banyak kode seperti `RES-W01`, `FIN-W04`, `CT-W09`. Mudah salah baca: *"wah, ada puluhan pegawai AI baru."* **Bukan.**

**Analogi:** seorang resepsionis punya banyak **kegiatan** — menjawab telepon, mencatat pesanan, mengantar tamu, mengisi buku. Kegiatan-kegiatan itu **bukan pegawai baru**; itu **tugas dari satu orang yang sama**.

| Istilah | Arti awam | Analogi |
|---|---|---|
| **Agent** | Satu "pegawai" dengan tanggung jawab & batas wewenang | Resepsionis |
| **Worker** | Salah satu **kegiatan** dari pegawai itu | Menjawab telepon |
| **Workflow** | Alur kerja panjang yang tersimpan di sistem, bukan di kepala pegawai | Berkas pesanan yang berpindah meja |
| **Deterministic worker** | Tugas yang cukup dikerjakan **program biasa**, tidak perlu AI | Kalkulator, bukan orang |

**Aturan dari dokumen Anda yang saya pegang:** kalau tugasnya bisa diselesaikan program biasa (hitung, validasi, cek aturan), **jangan pakai AI**. AI dipakai untuk hal yang memang butuh bahasa dan penalaran.

Dan satu lagi: **jumlah pegawai AI tidak bertambah saat tamu bertambah.** Yang bertambah adalah **kapasitas** — seperti menambah meja kasir saat ramai, bukan menambah jenis pegawai baru.

---

<a id="bab-7"></a>
## Bab 7 — Lapis 2: yang BELUM ada (jujur)

| # | Yang belum ada | Kenapa penting | Bukti |
|---|---|---|---|
| 1 | **6 Pegawai AI** | Ini inti dokumen Anda | 0 dari 6 ada di kode |
| 2 | **Outbox** (kurir antar-bagian) | Tanpa ini, pesan antar pegawai AI bisa hilang — dan dokumen Anda melarang pegawai AI saling memberi wewenang lewat obrolan bebas | ADR-0007 *diterima* tapi baru di atas kertas; tidak ada tabelnya |
| 3 | **Exception Engine** (mesin kejadian tak terduga) | Tanpa ini, setiap hal aneh berhenti tanpa penanganan berjenjang | tidak ada modulnya |
| 4 | **DLP** (pelindung data sensitif) | Mencegah data pribadi keluar lewat jalur yang salah | baru ada pemindai konfigurasi |
| 5 | **4 rel pembayaran** (kartu, transfer, PayPal, stablecoin) | Sudah **diputuskan** di ADR-0012, tapi belum dibangun: kode masih pakai cara lama | `method_category` = 0 di kode |
| 6 | **Stablecoin USDC/USDT** | ADR-0013 sudah diterima, belum dibangun | tidak ada jejaknya di kode |
| 7 | **Kategori tes AI** (keamanan, serangan prompt, handoff, ketahanan gagal) | Dokumen Anda bagian 19 mewajibkannya | 0 berkas untuk kategori ini |
| 8 | **Kill switch berjenjang** | Baru ada ACTIVE / SUSPENDED / RETIRED; belum ada DEGRADED & READ_ONLY | `ai_service.py` |

**Satu hal yang sudah kuat dan patut Anda tahu:** larangan-larangan mutlak untuk AI **sudah ditegakkan lebih dulu** — AI tidak bisa menyetujui pembayaran, tidak bisa mengubah rekening, tidak bisa melewati persetujuan berlapis, dan data kartu tidak bisa disimpan. Jadi pondasi keamanannya sudah berdiri **sebelum** pegawai AI-nya ada. Urutannya benar.

---

<a id="bab-8"></a>
## Bab 8 — Kenapa urutannya harus begitu

**Analogi:** Anda tidak bisa merekrut resepsionis **sebelum** gedungnya punya pintu, loker berkunci, dan buku aturan. Kalau dipaksa, resepsionis akan menaruh uang tamu di meja terbuka.

| Urut | Yang dikerjakan | Kalau dilompati, apa yang rusak |
|---|---|---|
| 1 | **Peran & orang** (D3) | Tidak ada yang bisa dipercaya sebagai "siapa yang bertindak" |
| 2 | **Outbox + Exception Engine** | Pegawai AI saling mengirim pesan yang bisa hilang, tanpa jejak |
| 3 | **AI Policy Gateway + kill switch berjenjang** | Pegawai AI bisa bertindak tanpa ada yang bisa mematikannya |
| 4 | **AI Reservation Service** | — (nilai bisnis terbesar) |
| 5 | **Finance Admin AI** | — (menyambung alur pembayaran Anda) |
| 6 | Control Tower, Operations AI, Commercial AI, Revenue & BI | Setelah 4 & 5 terbukti |

**Yang TIDAK saya usulkan sekarang:** membangun Commercial AI lebih dulu. Tidak ada gunanya mencari tamu lebih banyak kalau proses booking-nya sendiri belum otomatis.

---

<a id="bab-9"></a>
## Bab 9 — Apa yang menahan go-live (bukan software)

Pemeriksaan sistem hari ini: **13 dari 13 OK.** Yang menahan: **14 keputusan bisnis** yang belum Anda putuskan. Ini disengaja — *"sistemnya bisa siap sementara bisnisnya belum"*.

| # | Keputusan | Status | Yang saya butuhkan |
|---|---|---|---|
| D1 | Provider pembayaran & merchant-of-record | 🟡 **setengah** | Model sudah (agency); provider belum |
| D2 | Daftar villa pilot + model bisnisnya | ❌ | Nama villa + commission/full management |
| D3 | Peran & orang | 🟢 **disetujui** | Nama orang (belum ada) |
| D4 | SLA bisnis | ❌ | Angka menit/jam |
| D5 | Pajak PBJT per kabupaten/kota | ❌ | Tarif + siapa mengesahkan |
| D6 | WhatsApp Business + template pesan | ❌ | Provider + template |
| D7 | Ambang shadow-mode | ❌ | Angka |
| D8 | Tinjauan legal | ❌ | Siapa & kapan |
| D9 | Retensi data | 🟡 **setengah** | Masa retensi sudah; **siapa yang menghapus** belum |
| D10 | Backup: RPO/RTO | ❌ | Angka + siapa menguji |
| D11 | Refund/chargeback | ❌ | Batas nilai + siapa menyetujui |
| D12 | Pernyataan tidak menyimpan data kartu | ❌ | Pernyataan resmi (teknisnya sudah ditegakkan) |
| D13 | Monitoring & on-call | ❌ | Nama |
| D14 | Penerbitan anker audit | ❌ | Nama + frekuensi |

---

<a id="bab-10"></a>
## Bab 10 — Provider pembayaran: kriteria dulu, nama kemudian

**Laporan jujur:** pencarian web saya hari ini mengembalikan **hasil kosong** — bahkan untuk kueri sederhana. Jadi saya **tidak bisa** merekomendasikan nama vendor dengan bukti. **Saya dilarang menebak.** Jadi saya berikan yang bisa saya pertanggungjawabkan: **kriteria wajib** yang berasal dari aturan sistem Anda sendiri (ADR-0012), supaya Anda bisa menilai vendor mana pun dengan daftar ini.

| Rel | Kriteria yang **wajib** dipenuhi | Alasan (dari sistem Anda) |
|---|---|---|
| **Kartu** | Halaman pembayaran milik provider (hosted page), tokenisasi, dukungan 3-D Secure, API refund, webhook bertanda tangan | Sistem **melarang** menyimpan nomor kartu/CVV — tanpa hosted page, sistem Anda menolak |
| **Transfer bank** | Nomor Virtual Account **per booking** (bukan satu rekening untuk semua) | Agar uang masuk **otomatis cocok** dengan booking; tanpa ini, setiap transfer harus dicocokkan manual |
| **PayPal** | Akun bisnis, webhook, API refund, penarikan ke bank lokal | Untuk tamu internasional |
| **Stablecoin** | — | ⚠️ **Tunda dulu.** Aspek legalnya belum jelas untuk perusahaan di Indonesia, dan ini sudah masuk keputusan D8 (tinjauan legal). Ini risiko tertinggi dengan permintaan tamu terendah |

**Satu hal yang wajib ada di semua rel:** provider harus bisa memberi **bukti penyelesaian** dari sisi server. Halaman "pembayaran berhasil" di browser tamu **bukan** bukti — itu hanya tampilan.

**Yang saya butuhkan:** nama provider yang **sudah** Anda kenal/pakai, atau bank yang Anda pakai untuk operasional. Dari situ saya bisa analisa kesesuaiannya dengan kriteria di atas. Kalau belum ada, tulis "belum" — saya catat terbuka, bukan saya karang.

---

<a id="bab-11"></a>
## Bab 11 — Yang saya butuhkan dari Anda

**Urut dari yang paling cepat dibalas:**

1. ~~**Nama orang per peran**~~ — ✅ **SUDAH, 2026-09-28.** Delapan pemegang sudah tercatat (Bab 4). *(Satu-satunya hal yang tidak bisa saya analisa, dan benar: hanya Anda yang bisa mengisinya.)*
2. **Nama provider pembayaran / bank** (Bab 10) — atau "belum".
3. **D2, D4, D5, D6, D7, D8, D10, D11, D12, D13, D14** (Bab 9).
4. **D9 sisanya:** siapa yang menjalankan penghapusan data (retensi sudah Anda putuskan).
5. **Urutan pembangunan AI:** setuju dengan usulan Bab 8, atau Anda mau urutan lain?

---

<a id="bab-12"></a>
## Bab 12 — Kamus istilah: teknis → bahasa awam

| Istilah | Arti awam |
|---|---|
| **ADR** | Catatan keputusan + alasannya. Supaya 6 bulan lagi tidak ada yang bertanya "kenapa begini?" |
| **Policy / katalog kebijakan** | Daftar pertanyaan bisnis yang jawabannya Anda tentukan. Sistem **menolak bekerja** kalau jawabannya belum ada — tidak menebak |
| **Hold** | Menahan unit villa sementara untuk calon tamu |
| **Occupying** | Status yang **mengunci** tanggal+unit, tidak bisa dijual ke tamu lain |
| **Evidence level A–D** | Tingkat kekuatan bukti. A = dari sistem villa (kuat). D = catatan manual (lemah) |
| **Fail-closed** | Kalau ragu, **menolak**. Bukan "jalan terus sambil berasumsi" |
| **Invariant** | Aturan yang tidak boleh dilanggar, mis. "hold diminta ≠ hold dikonfirmasi" |
| **Preflight** | Pemeriksaan sebelum sistem dipakai dengan uang & tamu nyata |
| **RBAC** | Siapa boleh melakukan apa |
| **Outbox** | Kurir pesan antar-bagian yang dijamin tidak menghilangkan pesan |
| **Kill switch** | Tombol mematikan AI, bisa per-pegawai atau semuanya |
| **Merchant of record** | Pihak yang **secara hukum menerima uang tamu** |
| **UNASSIGNED** | Peran yang belum dipegang siapa pun → tidak ada yang bisa bertindak dalam peran itu |

---

<a id="bab-13"></a>
## Bab 13 — Ringkasan satu halaman

1. **Yang dibangun:** satu sistem operasi villa agency untuk Bali & Yogyakarta, dijalankan **manusia + 6 pegawai AI**.
2. **Sudah jadi:** "gedungnya" — katalog kebijakan (25/25 terjawab), mesin status booking & hold, mesin pembayaran, mesin bukti, identitas & hak akses, rantai audit, dan gerbang 3.018 tes. Semuanya **hijau**.
3. **Belum jadi:** **keenam pegawai AI-nya** (0 dari 6), plus 3 alat penopang (outbox, exception engine, DLP) dan 4 rel pembayaran.
4. **Organisasi manusia SUDAH LENGKAP** (keputusan Anda 2026-09-28): delapan pemegang — Mr. Owner, Mr. CEO, Mr. CMM, Mr. PM, Ms. RM, Ms. FAM, Mr. TS, Ms. Greeting. Revenue Manager & Auditor ditunda; Legal eksternal; **Finance Staff & Reservation Agent direservasi untuk AI — bukan dihapus**, supaya maker-checker tetap bermakna; **Guest Service dihapus**, diganti Greeting Service Team.
5. **Yang menahan go-live bukan software** — 13/13 pemeriksaan sistem OK. Yang menahan: 14 keputusan bisnis.
6. **Yang saya butuh:** nama orang, nama provider, dan 11 keputusan sisanya.
7. **Yang saya tidak akan lakukan:** mengarang nama, mengarang vendor, atau mengklaim sesuatu sudah jadi padahal belum.

---

*Dokumen ini dibuat dari pemeriksaan langsung kode, tes, dan gerbang pada 2026-09-28. Setiap angka di sini bisa Anda minta saya periksa ulang di depan Anda.*
