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
| D1 | Provider pembayaran & model merchant-of-record (siapa menerima uang tamu) | BLOCKING | OPEN | **Merchant of record = AGENCY** (uang tamu wajib masuk ke agency; villa dibayar oleh agency). Alur 6 langkah: tamu bayar -> Finance verifikasi & lapor ke Reservation -> Reservation konfirmasi ke tamu (booked) -> Reservation update ke Villa Management/Owner (Booked confirmed, bukan Hold) -> villa WAJIB kirim bukti surat/email unit di-Blocked -> booking CONFIRMED. **Provider per rel masih OPEN.** | owner | 2026-09-28 | `docs/adr/0033-guest-money-enters-the-agency-and-the-block-proof-closes-the-booking.md` |
| D2 | Daftar property pilot beserta model bisnisnya (agency commission / full management) | BLOCKING | OPEN | | | | |
| D3 | Peran & orang: siapa reservation, finance, operations, owner, auditor | BLOCKING | OPEN | | | | |
| D4 | SLA yang disetujui bisnis (first response, quote, rekonsiliasi ≤48 jam, komplain) | BLOCKING | OPEN | | | | |
| D5 | Perpajakan: sumber tarif PBJT per kabupaten/kota & siapa yang mengesahkan | BLOCKING | OPEN | | | | |
| D6 | Kanal resmi: provider WhatsApp Business + status template pesan | BLOCKING | OPEN | | | | |
| D7 | Ambang shadow-mode sebelum otomasi diperluas (agreement rate, unsupported claim = 0) | BLOCKING | OPEN | | | | |
| D8 | Tinjauan legal: kontrak owner, T&C tamu, privacy notice, DPIA | BLOCKING | OPEN | | | | |
| D9 | Masa retensi data per kelas (P0–P4) & siapa yang mengeksekusi penghapusan | BLOCKING | OPEN | | | | |
| D10 | Backup & pemulihan: RPO/RTO, lokasi, dan siapa yang menguji restore | BLOCKING | OPEN | | | | |
| D11 | Kebijakan refund, chargeback, dan kompensasi (batas nilai + siapa menyetujui) | BLOCKING | OPEN | | | | |
| D12 | Konfirmasi tidak menyimpan data kartu (Rupiah, hosted page) & alasan tertulisnya | BLOCKING | OPEN | | | | |
| D13 | Monitoring & siapa yang bertanggung jawab saat alarm berbunyi (on-call) | BLOCKING | OPEN | | | | |
| D14 | Penerbitan anker rantai audit: siapa, seberapa sering, ke sistem mana | BLOCKING | OPEN | | | | |
| D15 | Inventori campuran: sumber data mana yang dianggap bukti `A` dan siapa verifikatornya | NON-BLOCKING | OPEN | | | | |
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
