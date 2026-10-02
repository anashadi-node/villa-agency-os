# Operations Runbook — Villa Management & Agency OS

Panduan harian untuk staf yang menjalankan sistem. Ditulis untuk orang yang
sedang buru-buru: setiap antrean punya **gejala → tindakan → bukti yang harus
tertinggal**. Kalau sebuah tindakan tidak meninggalkan bukti di sistem, tindakan
itu belum selesai.

## 0. Peran (siapa boleh apa)

| Peran | Melihat | Boleh memutuskan |
|---|---|---|
| `reservation` | kasus, inventory, properti | transisi kasus, hold, quote, kirim draft yang sudah disetujui |
| `finance` | keuangan, invoice, rekonsiliasi | mencocokkan pembayaran, menagih, mengusulkan refund |
| `operations` | operasi, tugas, insiden | menyelesaikan tugas, mencatat insiden, menjalankan playbook |
| `owner` | semua | approval akhir atas uang, pengecualian policy, relokasi, perubahan rekening |
| `auditor` | audit & kepatuhan (read-only) | tidak ada keputusan operasional |

Dua aturan yang tidak bisa dinegosiasikan: **pemohon tidak boleh menyetujui
permintaannya sendiri**, dan **aksi uang/legal butuh dua penyetuju berbeda**
(dual control) — ditegakkan modul `approvals`, bukan kebiasaan.

## 1. Ritme harian

### Pagi (±30 menit)

1. `python3 src/observability.py --db ... --anchor-dir ...` → pastikan tidak ada
   `CRITICAL`. Kalau ada, kerjakan bagian 3 dulu sebelum apa pun.
2. Buka **Command Center** dashboard: KPI kasus terbuka, approval menunggu, hold
   akan kedaluwarsa, invoice belum lunas, pembayaran belum tercocokkan, tugas
   terlambat, insiden terbuka.
3. Bersihkan **antrean approval** (bagian 5) dan **rekonsiliasi** (bagian 6) —
   dua antrean ini yang paling mahal kalau menumpuk.
4. **Perbarui kurs harian** (sekali sehari, ADR-0031):

   ```bash
   VILLA_DB=data/villa.db python3 scripts/refresh_fx_rate.py --approved-by owner_anas
   ```

   Kurs adalah harga satu hari, dan lantai margin §11 dikonversi dengannya untuk
   booking di luar mata uang floor (Bali USD, Yogyakarta IDR). Lewat **dua hari**
   (batas owner, `commercial.fx.max_age_hours` = 48), konversi **MENOLAK**
   (`FX_RATE_STALE`) — fail-closed, bukan memakai kurs basi.

   Yang perlu dibaca dari keluarannya:
   * `FETCHED` — angka dan waktu kutipan dari provider. Kalau provider sendiri
     melaporkan kutipan lebih tua dari `--max-feed-age-hours`, alat ini menolak
     menerbitkan: feed yang mati tidak boleh menjadi kurs.
   * `UNCHANGED` — pasar belum bergerak hari ini; tidak ada revisi baru. Normal.
   * `REFUSED` — payload dicurigai (lonjakan > `--max-move-pct`, rate bukan
     desimal eksak, base salah, atau kutipan bertanggal masa depan). **Tidak ada
     yang tertulis**; periksa provider sebelum mengulang, jangan naikkan batasnya.

   Jangan pernah memberi `--effective-from` bertanggal lokal WIB. Revisi bertanggal
   lokal lahir "besok" bagi platform (UTC): hari ini tak terlihat, dan store menolak
   mundur untuk mengoreksinya.

### Sepanjang hari

Heartbeat berjalan tiap 5 menit (`villa-scheduler.timer`) dan menagih sendiri:
hold kedaluwarsa, approval lewat waktu, SLA kasus, SLA tugas, rekonsiliasi,
tenggat notifikasi regulator, dan invoice lewat jatuh tempo. Kalau pengadwal mati,
gejalanya justru terlihat: banyak `sla_breaches` baru sekaligus.

### Sore (±15 menit)

1. Tutup kasus yang sudah selesai (`CLOSED_WON`/`CLOSED_LOST`/`CLOSED_DUPLICATE`).
2. Pastikan setiap tugas operasi yang jatuh tempo besok sudah punya penanggung jawab.
3. Cek jam: `python3 src/scheduler.py --db ... --dry-run` → harus **0 perubahan**
   kalau tidak ada deadline baru. Angka bukan-nol di sore hari berarti ada yang
   belum ditangani, bukan berarti penjadwal bekerja keras.

## 2. Antrean: kasus & SLA

**Gejala:** baris pertama/quote sudah lewat tenggat; dashboard menandai SLA.
**Tindakan:** balas lewat **draft** yang dibuat sistem (semua angka di draft
sudah terikat bukti), minta persetujuan manusia bila draft menyangkut uang, lalu
tandai `first_responded_at`/`quoted_at`.
**Bukti yang harus tertinggal:** satu baris `case_events` per perubahan + draft
yang tercatat dengan `grounding = GROUNDED`.

Aturan: **jangan** mengarang ketersediaan. Kalau bukti villanya `UNKNOWN`, tulis
begitu adanya; ini lebih baik daripada janji yang tidak bisa ditepati.

## 3. Kalau ada CRITICAL (rantai audit, anker, insiden, notifikasi)

1. **Kandangkan, jangan bersihkan.** Jangan menghapus baris, jangan menjalankan
   ulang perintah yang menulis, dan jangan menerbitkan anker baru di atas rantai
   yang rusak (sistem akan menolak — biarkan ia menolak).
2. Catat insiden lewat modul `incidents` dengan playbook yang sesuai.
3. Simpan bukti: keluaran `observability.py`, waktu, pelaku, dan berkas anker
   terakhir (jangan ditimpa).
4. Kalau ada data pribadi: tandai `regulatory_notification_required` dan isi
   `notice_due_at` ≤ 72 jam; heartbeat akan menagih tenggat itu.
5. Post-mortem wajib untuk SEV1/SEV2 sebelum insiden boleh ditutup
   (`postmortem_ref` harus terisi).

Penyebab paling sering "rantai rusak" bukan serangan, melainkan **dua penulis
dengan resep berbeda**. Hanya modul `audit` yang boleh menulis rantai; kalau ada
kode baru yang menghitung hash sendiri, itu bug yang harus diperbaiki.

## 4. Hold kedaluwarsa & konflik tanggal

**Gejala:** tamu mengklaim sudah dijanjikan, tetapi tanggalnya dipegang orang lain.
**Tindakan:**

1. Periksa riwayat hold dan booking unit tersebut (halaman inventory + audit).
2. Kalau hold tamu memang **aktif**: pertahankan slot, tawarkan tanggal/unit lain.
3. Kalau hold sudah **kedaluwarsa**: slot memang bebas — jangan memaksa sistem
   mengembalikannya, dan jangan menghapus booking yang sudah sah.
4. Konflik yang melibatkan janji yang sudah keluar: eskalasi ke owner, catat
   insiden `DOUBLE_BOOKING` walau belum jadi double booking, dan putuskan
   kompensasi lewat approval.

## 5. Antrean approval

**Gejala:** KPI "approval menunggu" naik; ada yang lewat `expires_at`.
**Tindakan:** buka halaman approvals, baca *alasan* dan buktinya, putuskan
menyetujui/menolak. Yang butuh dua penyetuju: refund, payout, perubahan rekening.
**Bukti:** dua baris keputusan berbeda penyetuju; status diturunkan dari log
keputusan — mengubah kolom status langsung **tidak** akan mengubah kenyataan.

Kalau approval kadaluwarsa: jangan menyetujui dengan mengubah jam. Buat permintaan
baru dengan alasan "permintaan sebelumnya kedaluwarsa", supaya jejaknya jujur.

## 6. Rekonsiliasi pembayaran (SLA 48 jam)

**Gejala:** pembayaran masuk tetapi tidak cocok dengan invoice apa pun
(`UNMATCHED_PAYMENT`, `AMOUNT_MISMATCH`, `DUPLICATE_EVENT`, `UNVERIFIED_WEBHOOK`).
**Tindakan:**

1. **Jangan** menandai invoice lunas berdasarkan screenshot. Cari bukti di
   penyedia pembayaran / mutasi bank.
2. Cocokkan: nominal, tanggal, referensi, dan nama pengirim.
3. Selesaikan kasus rekonsiliasi **dengan catatan** (wajib oleh skema).
4. Kalau ternyata bukan pembayaran sah: kembalikan sesuai kebijakan (butuh
   approval bila ada uang keluar), catat sebagai insiden fraud bila ada indikasi.

**Bukti:** kasus rekonsiliasi berstatus `RESOLVED` dengan `resolution_note`, plus
baris ledger yang menyeimbangkan.

## 7. Invoice & penagihan

Heartbeat membuka tugas `PAYMENT_CHASE` untuk invoice lewat jatuh tempo (naik
prioritas setelah 7 hari). Tugas itu **tidak** menghubungi tamu; ia menugaskan
manusia untuk menyiapkan draft pesan. Penagihan ke tamu tetap butuh persetujuan.

## 8. Operasi villa

| Tugas | Kapan dibuat | Bukti selesai |
|---|---|---|
| `CHECKIN_PREP` | H-1 sebelum check-in | checklist terisi + foto/status |
| `CLEANING` | setelah check-out | tugas `DONE` (wajib `completed_at`) |
| `MAINTENANCE` | saat kerusakan ditemukan | catatan + vendor + biaya bila ada |
| `VERIFICATION` | saat bukti ketersediaan basi | bukti baru dari owner |

Tugas yang `BLOCKED` **wajib** mencantumkan alasan — kalau tidak, itu bukan
"tertahan", itu "tidak ada yang mengerjakan".

## 9. Mingguan

1. **Terbitkan anker** dari mesin/akun berbeda: `make anchor-publish`, lalu
   `make anchor-verify` (harus hijau). Catat nama berkasnya di tiket operasional.
2. Tinjau SLA: berapa kasus lewat tenggat dan kenapa. Kalau polanya jam atau
   kanal tertentu, usulkan perubahan **parameter SLA lewat keputusan bisnis**,
   bukan menambal di kode.
3. Tinjau insiden minggu itu; pastikan tiap SEV1/SEV2 punya post-mortem.
4. Tinjau properti yang bukti ketersediaannya basi (`evidence_level` turun).

## 10. Bulanan

1. **Uji restore penuh** dari backup ke database sementara + jalankan
   `observability.py` dan `preflight.py` terhadap salinan itu. Catat hasilnya.
2. Tinjau akses: siapa punya token peran apa; cabut yang tidak lagi relevan.
3. Tinjau retensi data: hapus yang memang sudah jatuh tempo sesuai
   `docs/RETENTION_AND_INCIDENT_RESPONSE.md` — penghapusan pun harus tercatat.
4. Perbarui `docs/GO_LIVE_DECISIONS.md` bila ada keputusan yang berubah.

## 11. Kalau preflight berkata NO-GO

| Kegagalan | Arti | Tindakan |
|---|---|---|
| `go-live decisions` | keputusan bisnis BLOCKING belum diambil | selesaikan dokumen itu; jangan menambal dengan mengubah status palsu |
| `published anchor` | anker hilang/basi/gagal verifikasi | terbitkan anker baru setelah memeriksa rantai; investigasi kalau gagal verifikasi |
| `demo data` | database produksi berisi data contoh | buat database bersih; **jangan** menghapus baris demo sembarangan di produksi |
| `audit chain` | rantai tidak utuh | berhenti, mulai prosedur insiden (bagian 3) |
| `backup` | backup hilang/terlalu kecil/terlalu lama | perbaiki penjadwalan backup; uji restore |
| `secret scan` / `unsafe config scan` | ada rahasia/switch berbahaya di repo | perbaiki kode; **jangan** melemahkan gate |

## 12. Eskalasi

1. **Staf** menyelesaikan sesuai runbook ini dan mencatat bukti.
2. **Supervisor/owner** mengambil keputusan uang, pengecualian policy, relokasi,
   dan perubahan rekening (dual control).
3. **Teknis** dipanggil kalau: rantai audit tidak utuh, anker gagal verifikasi,
   preflight menolak karena alasan data, atau heartbeat berhenti menagih.
4. **Legal/counsel** dilibatkan untuk: sengketa tamu, dugaan fraud, insiden yang
   melibatkan data pribadi, dan pertanyaan pajak/izin.

Nomor kontak dan jam layanan: `[ISI]` (diisi saat onboarding).

---

## 13. Keputusan operasional yang disetujui owner 2026-10-02

Tiga keputusan yang sebelumnya menahan go-live sekarang punya angka. **Ini angka yang berlaku**;
mengubahnya bukan pekerjaan harian — ia milik owner.

### 13.1 D10 — Cadangan & pemulihan

| Hal | Nilai yang disetujui | Artinya |
|---|---|---|
| **RPO** (*Recovery Point Objective*) | **24 jam** | Paling banyak **satu hari** data boleh hilang |
| **RTO** (*Recovery Time Objective*) | **4 jam** | Sistem harus hidup lagi dalam **4 jam** sejak gangguan |
| Lokasi salinan | **di luar server utama** | Server utama rusak/terserang → salinan tetap ada |
| Uji pemulihan | **setiap 3 bulan**, oleh **Mr. TS** (Technology-Security Manager) | Cadangan yang tidak pernah diuji bukan cadangan; hasil uji disimpan sebagai bukti |

### 13.2 D13 — Siapa yang wajib merespons saat alarm berbunyi (*on-call*)

| Waktu | Siapa | Batas respons |
|---|---|---|
| Jam kerja | **Mr. TS** (teknologi), eskalasi ke **Mr. CEO** | 30 menit |
| Di luar jam kerja | hanya alarm **level tertinggi** yang memanggil; penerima tetap Mr. TS dengan eskalasi ke Mr. CEO | 30 menit |
| Alarm yang tidak direspons | naik otomatis ke GM/CEO, lalu Owner (lihat §3 dan §12) | — |

### 13.3 D14 — Penerbitan anker rantai audit

| Hal | Nilai yang disetujui |
|---|---|
| Frekuensi | **setiap hari** |
| Siapa yang menerbitkan | akun yang **bukan milik server** (mis. surel Mr. Owner) — kalau penanda diterbitkan oleh sistem yang sama, ia tidak membuktikan apa-apa |
| Ke mana | sistem/akun milik pihak berbeda (direktori arsip milik owner) |
| Pemeriksaan | otomatis **setiap pagi**; merah → alarm CRITICAL (§3) |
| Perintah | `make anchor-publish` (menerbitkan) / `make anchor-verify` (memeriksa) |

### 13.4 D4 — Janji waktu layanan (SLA) yang berlaku

| Janji | Batas yang disetujui | Terpasang di |
|---|---|---|
| Balasan pertama | **≤ 15 menit** (jam kerja) | `src/cases.py` — `SLA_CEILING_MINUTES`; kanal boleh **lebih ketat** (WhatsApp 10, agent 5), tidak boleh lebih longgar |
| Penawaran harga | **≤ 2 jam** (120 menit) | sama |
| Rekonsiliasi pembayaran | **≤ 48 jam** | §6 runbook ini |
| Komplain | balasan **≤ 15 menit**, **ditutup ≤ 3 hari** | §2 runbook ini |

### 13.5 D7 — Ambang "naik kelas" pegawai AI (shadow mode)

Pegawai AI wajib melewati **minimal 100 kasus** dalam mode bayangan dengan **kecocokan ≥ 95%**
dan **0 klaim tanpa bukti**, sebelum boleh bertindak nyata. Yang menyetujui kenaikan: **GM/CEO**.
Kalau setelah naik kelas kecocokan turun **< 90% dalam 50 tindakan terakhir**, atau ada satu saja
klaim tanpa bukti sampai ke tamu, pegawai itu **otomatis kembali ke mode bayangan** dan kegawatan
dilaporkan ke GM/CEO & Owner. Rincian: `docs/adr/0038-ai-agents-graduate-from-shadow-mode-by-measurement.md`.

### 13.6 Pegawai AI ke-7 — Divisi Security & Privacy

Ditetapkan owner 2026-10-02. Matriks wewenang ("boleh sendiri" vs "wajib persetujuan manusia"),
tingkat kegawatan S1–S4, dan 4 entri daftar larangan yang menunggu persetujuan owner:
`docs/adr/0039-a-seventh-ai-agent-the-security-and-privacy-warden.md`.
