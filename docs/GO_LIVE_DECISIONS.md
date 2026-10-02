# Keputusan Go-Live — Villa Management & Agency OS

Dokumen ini adalah **daftar keputusan bisnis yang harus ada sebelum sistem dipakai
dengan uang dan tamu nyata**. Software tidak bisa memutuskan hal-hal ini, dan
kode tidak boleh menebaknya: kalau tarif pajak, provider pembayaran, atau siapa
yang berwenang menyetujui refund ditebak di dalam kode, kesalahannya baru
terlihat setelah ada tamu yang dirugikan.

`scripts/preflight.py` membaca tabel di bawah ini. Selama masih ada baris
bertanda **BLOCKING** yang statusnya `OPEN`, preflight akan menolak go-live
(exit code 1) — walaupun semua test hijau. Itu disengaja: "software siap" dan
"bisnis siap" adalah dua hal berbeda, dan keduanya harus terbukti.

## Cara mengisi

1. Ubah kolom **Status** dari `OPEN` menjadi `DECIDED`.
2. Isi **Nilai/nilai yang diputuskan** (singkat, konkret, terukur).
3. Isi **Diputuskan oleh** dan **Tanggal** (YYYY-MM-DD).
4. Isi **Bukti** — berkas, kontrak, atau tiket tempat keputusan itu tersimpan.
   Preflight hanya memeriksa bahwa kolomnya terisi, bukan isinya: dokumen ini
   adalah penunjuk, bukan tempat menyimpan rahasia.

Jangan pernah menulis kredensial, nomor rekening, atau data pribadi di dokumen
ini. Bukti cukup berupa rujukan (mis. `docs/legal/kontrak-owner-2026-09.md`).

## Daftar keputusan

| # | Keputusan | Blocking | Status | Nilai yang diputuskan | Diputuskan oleh | Tanggal | Bukti |
|---|-----------|----------|--------|-----------------------|-----------------|---------|-------|
| D1 | Provider pembayaran & model merchant-of-record (siapa menerima uang tamu) | BLOCKING | DECIDED | **Merchant of record = AGENCY** (uang tamu wajib masuk ke agency; villa dibayar oleh agency). Alur 6 langkah: tamu bayar -> Finance verifikasi & lapor ke Reservation -> Reservation konfirmasi ke tamu (booked) -> Reservation update ke Villa Management/Owner (Booked confirmed, bukan Hold) -> villa WAJIB kirim bukti surat/email unit di-Blocked -> booking CONFIRMED. **Provider per rel: DIISI OLEH MASING-MASING AGENCY (keputusan owner 2026-10-02).** Sistem ini dijual/di-berlangganan, jadi provider pembayaran, nomor WhatsApp, daftar villa, tarif pajak, dan rate card **diisi oleh agency yang memakainya**, bukan oleh pembuat sistem. Platform wajib menyediakan tempatnya (setelan per agency), bukan memilih vendornya. | owner | 2026-09-28 | `docs/adr/0033-guest-money-enters-the-agency-and-the-block-proof-closes-the-booking.md` |
| D2 | Daftar property pilot beserta model bisnisnya (agency commission / full management) | BLOCKING | DECIDED | **Diisi oleh masing-masing agency** (owner, 2026-10-02): dua cara — (a) input manual satu per satu, (b) **impor berkas sekaligus** (CSV/XLSX gelondongan) untuk agency yang sudah punya daftar villa. Model bisnis per villa (komisi / full management) ikut diisi per baris. Belum dibangun: layar setelan + pengimpor berkas (dicatat sebagai pekerjaan fitur). | owner | 2026-10-02 | `docs/adr/0037-platform-dijual-per-agency-setelan-dan-peran-dapat-diedit.md` |
| D3 | Peran & orang: siapa reservation, finance, operations, owner, auditor | BLOCKING | DECIDED | **8 pemegang:** Owner=Mr. Owner; GM/CEO=Mr. CEO; Commercial & Marketing Manager=Mr. CMM; Partnership Manager=Mr. PM; Operations/Reservation Manager=Ms. RM; Finance & Accounting Manager=Ms. FAM; Technology-Security Manager=Mr. TS; Greeting Service Team=Ms. Greeting. **Ditunda:** Revenue Manager, Auditor. **Eksternal:** Legal/Compliance. **PERAN MANUSIA DIHAPUS, digantikan AI:** Finance Staff -> Finance Admin AI (agent #5); Reservation Agent -> AI Reservation Service (agent #3). **Fungsi PEMBUAT tidak dihapus** - ia pindah ke identitas AI Service (ai_service), karena kontrol tanpa pembuat = pintu terbuka. Yang tetap MANUSIA: penyetuju (Ms. FAM untuk uang keluar, Ms. RM untuk diskon di luar batas), sesuai AI Workforce 11 (ABSOLUTE DENY: AI tidak boleh menyetujui/mengeksekusi pembayaran). Konsekuensi: sampai agent #3 dan #5 dibangun, tidak ada yang menjalankan fungsi pembuat -> sistem MENOLAK, jadi sisi pembayaran belum bisa go-live. **TIGA ATURAN UANG (owner, 2026-09-28):** (1) transfer dana nyata WAJIB oleh MANUSIA, AI hanya memberi report beserta detailnya; (2) ubah nomor rekening / account tujuan transfer WAJIB oleh MANUSIA; (3) semua aktifitas di atas wajib persetujuan GM/CEO lebih dulu, APPROVE BERJENJANG (Finance Manager -> GM). Aturan 1 dan 2 SUDAH ditegakkan: security.ai.denied_actions memuat payment.execute, payment.approve, refund.execute, bank.change, bank.approve, crypto.transfer, treasury.control. **Guest Service dihapus**, diganti Greeting Service Team (kunjungan tamu di villa + relasi manajemen/kru villa). **PERAN DAPAT DIEDIT (owner, 2026-10-02):** setiap agency boleh menambah, mengubah nama, atau menonaktifkan peran pemakainya sendiri lewat setelan; peran bawaan (ADR-0035) tetap ada sebagai cetak biru. Belum dibangun: layar penyunting peran + jejak auditnya. | owner | 2026-09-28 | `docs/adr/0035-the-human-organisation-and-who-holds-each-role.md` |
| D4 | SLA yang disetujui bisnis (first response, quote, rekonsiliasi ≤48 jam, komplain) | BLOCKING | OPEN | **Menunggu keputusan Anda (penjelasan dikirim 2026-10-02).** SLA = *Service Level Agreement* = janji waktu layanan. Usulan angka ada di kotak penjelasan; setelah Anda setujui, angkanya menjadi setelan per agency. | owner | | |
| D5 | Perpajakan: sumber tarif PBJT per kabupaten/kota & siapa yang mengesahkan | BLOCKING | DECIDED | **Tarif diisi oleh masing-masing agency** (owner, 2026-10-02), per kabupaten/kota dan per villa. Pengesahan berjenjang: **Finance & Accounting Manager (Ms. FAM) yang mengesahkan → wajib di-approve GM/CEO**. Di kode: `policy_versions.tax_pbjt_pct` per property; tidak boleh ada tarif di-hard-code. | owner | 2026-10-02 | `docs/adr/0037-the-platform-is-sold-per-agency-and-the-tenant-fills-the-settings.md` |
| D6 | Kanal resmi: provider WhatsApp Business + status template pesan | BLOCKING | DECIDED | **Nomor WhatsApp diisi oleh masing-masing agency** (owner, 2026-10-02); **template pesan dibuat oleh sistem** (pembuat sistem menyediakan template resmi berbahasa Indonesia, boleh disunting tenant). Belum dibangun: setelan kanal per agency + pustaka template yang dapat disunting. | owner | 2026-10-02 | `docs/adr/0037-the-platform-is-sold-per-agency-and-the-tenant-fills-the-settings.md` |
| D7 | Ambang shadow-mode sebelum otomasi diperluas (agreement rate, unsupported claim = 0) | BLOCKING | OPEN | **Menunggu keputusan Anda (penjelasan dikirim 2026-10-02).** Shadow mode = mode "bayangan": AI bekerja tapi **tidak boleh mengirim/mengeksekusi apa pun**; hasilnya dibandingkan dengan keputusan manusia. Ambang = batas angka kelulusan sebelum AI boleh naik dari bayangan ke nyata. Usulan: agreement rate ≥ 95% dan klaim tanpa bukti = 0, minimal 100 kasus. | owner | | |
| D8 | Tinjauan legal: kontrak owner, T&C tamu, privacy notice, DPIA | BLOCKING | DECIDED | **Ditinjau setiap bulan** oleh **GM/CEO + Owner** (owner, 2026-10-02). Cakupan: kontrak owner/villa, T&C tamu, privacy notice, dan penilaian dampak privasi (DPIA). Jadwal bulanan dicatat di scheduler; hasil tinjauan disimpan sebagai berkas bukti. | owner | 2026-10-02 | `docs/adr/0037-the-platform-is-sold-per-agency-and-the-tenant-fills-the-settings.md` |
| D9 | Masa retensi data per kelas (P0–P4) & siapa yang mengeksekusi penghapusan | BLOCKING | DECIDED | **Hapus setelah 7 tahun** (owner, 2026-10-02). Eksekusi dilakukan **AI Agent**, tetapi wajib **disetujui GM/CEO + Owner** (approve berjenjang) sebelum dijalankan; setiap penghapusan tercatat di rantai audit. Catatan: kewajiban pembukuan/pajak ikut terpenuhi pada 7 tahun. | owner | 2026-10-02 | `docs/adr/0037-the-platform-is-sold-per-agency-and-the-tenant-fills-the-settings.md` |
| D10 | Backup & pemulihan: RPO/RTO, lokasi, dan siapa yang menguji restore | BLOCKING | OPEN | **Menunggu keputusan Anda (penjelasan dikirim 2026-10-02).** RPO = *Recovery Point Objective* = **seberapa banyak data boleh hilang** (mis. 24 jam terakhir). RTO = *Recovery Time Objective* = **berapa lama sistem boleh mati** sebelum harus hidup lagi. Usulan: RPO 24 jam, RTO 4 jam, salinan di luar server utama, dan uji pemulihan tiap 3 bulan oleh Mr. TS. | owner | | |
| D11 | Kebijakan refund, chargeback, dan kompensasi (batas nilai + siapa menyetujui) | BLOCKING | DECIDED | **Mengikuti kontrak/rate card setiap villa** (owner, 2026-10-02) — bukan angka seragam. Persetujuan **berjenjang: Finance Manager → GM/CEO**. Nilai di atas ambang kontrak wajib naik ke GM/CEO. | owner | 2026-10-02 | `docs/adr/0037-the-platform-is-sold-per-agency-and-the-tenant-fills-the-settings.md` |
| D12 | Konfirmasi tidak menyimpan data kartu (Rupiah, hosted page) & alasan tertulisnya | BLOCKING | DECIDED | **Diterapkan sekarang** (owner, 2026-10-02): pembayaran lewat **hosted page** provider; sistem **tidak pernah** menyimpan nomor kartu/CVV. Ditegakkan mesin: `scripts/unsafe_config_scan.sh` menggagalkan gate bila ada kolom/bidang data kartu. | owner | 2026-10-02 | `docs/adr/0037-the-platform-is-sold-per-agency-and-the-tenant-fills-the-settings.md` |
| D13 | Monitoring & siapa yang bertanggung jawab saat alarm berbunyi (on-call) | BLOCKING | OPEN | **Menunggu keputusan Anda (penjelasan dikirim 2026-10-02).** On-call = orang yang **wajib merespons saat alarm berbunyi** dan jam berapa ia wajib siaga. Usulan: jam kerja → Mr. TS (teknologi) dan eskalasi ke Mr. CEO; di luar jam kerja → hanya alarm level tertinggi yang memanggil, dengan batas respons 30 menit. | owner | | |
| D14 | Penerbitan anker rantai audit: siapa, seberapa sering, ke sistem mana | BLOCKING | OPEN | **Menunggu keputusan Anda (penjelasan dikirim 2026-10-02).** Anker = sidik jari rantai audit yang dikirim ke **sistem lain milik pihak berbeda**, supaya menambah/menghapus catatan lama bisa dibuktikan. Perintah siap: `make anchor-publish` / `make anchor-verify`. Usulan: setiap hari, oleh akun email/repositori yang **bukan** milik server (mis. surel Mr. Owner), dengan pemeriksaan otomatis tiap pagi. | owner | | |
| D15 | Inventori campuran: sumber data mana yang dianggap bukti `A` dan siapa verifikatornya | NON-BLOCKING | OPEN | | | | |
| D17 | Kesiapan tenant sebelum go-live agency mana pun: setelan wajib terisi (provider pembayaran, nomor WhatsApp, daftar villa, tarif pajak, rate card, peran) + peran hantu dijadikan pemegang nyata | BLOCKING | OPEN | **Konsekuensi keputusan 2026-10-02 (sistem dijual per agency):** platform boleh siap lebih dulu, tetapi **setiap agency wajib lolos pemeriksaan kesiapan sendiri** sebelum dipakai dengan uang & tamu nyata. Belum dibangun: layar "Kesiapan Tenant" + pemeriksaanya di preflight. | owner | 2026-10-02 | `docs/adr/0037-the-platform-is-sold-per-agency-and-the-tenant-fills-the-settings.md` |
| D16 | Jumlah unit & tarif (rate card) yang dipublikasikan ke tamu | NON-BLOCKING | OPEN | | | | |

## Catatan teknis yang mendukung keputusan di atas

- **D4 (SLA)** sudah ada padanannya di kode: `src/cases.py` (`SLA_POLICIES`) dan
  `src/scheduler.py` (penagihan tenggat). Bisnis harus mengesahkan angkanya, bukan
  membiarkan nilai default menjadi kebijakan tanpa persetujuan.
- **D5 (pajak)** diterapkan per property dari `policy_versions` (`tax_pbjt_pct`);
  tidak boleh ada tarif yang di-hard-code di satu tempat pun.
- **D12** ditegakkan oleh gate `scripts/unsafe_config_scan.sh`: model data kartu
  (PAN/CVV) sekalipun hanya sebagai kolom akan menggagalkan gate.
- **D14** dijalankan dengan `make anchor-publish` / `make anchor-verify`
  (`scripts/publish_anchor.py`). Anker harus diterbitkan dari sistem/akun yang
  berbeda dari penulis database, kalau tidak ia tidak membuktikan apa-apa.
- **D15** menentukan tingkat bukti (`evidence_level` A–D) yang boleh dipakai untuk
  menjual (`can_quote`, `can_hold`, `can_confirm`) — diatur di tabel
  `availability_evidence`, bukan di kepala orang.
