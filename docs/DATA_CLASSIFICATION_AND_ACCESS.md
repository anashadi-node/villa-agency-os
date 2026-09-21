# Data Classification & Access Control

Versi: 1.0 · Tanggal: 2026-09-21

Aturan ini menentukan **data apa yang boleh masuk AI, boleh masuk log, dan siapa
yang boleh melihatnya**. Pelanggaran klasifikasi = pelanggaran keamanan, bukan
sekadar "salah setting".

---

## 1. Tingkat klasifikasi

| Tier | Nama | Definisi | AI (LLM) | Log | Retensi |
|---|---|---|---|---|---|
| **P0** | Public | Informasi properti yang memang publik | Boleh | Boleh | bebas |
| **P1** | Internal | Data operasional non-pribadi (nomor booking, status, jadwal) | Boleh | Boleh | bebas |
| **P2** | Confidential | Identitas & kontak tamu, riwayat menginap, preferensi, nomor telepon/email | **Redaksi wajib** (pseudonim) | Hanya ID, tanpa isi | terbatas (§2) |
| **P3** | Restricted | Dokumen identitas (KTP/paspor), data rekening, dokumen legal, kontrak, NPWP, data medis, data anak | **DILARANG TOTAL** | DILARANG | ketat, terenkripsi |
| **P4** | Prohibited | Nomor kartu/CVV, password, OTP, private key, kredensial PSP | **DILARANG TOTAL** | DILARANG | tidak disimpan sama sekali |

## 2. Matriks akses per peran

| Data | Guest | Agent | Manager | Admin | Finance | Owner (properti sendiri) | AI co-pilot |
|---|---|---|---|---|---|---|---|
| Info publik villa | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Status booking & hold | hanya miliknya | ✔ | ✔ | ✔ | ✔ | hanya propertinya | ✔ (tanpa PII) |
| Kontak tamu (P2) | miliknya | ✔ | ✔ | ✔ | sebagian | ✖ | **hanya pseudonim** |
| Dokumen identitas (P3) | miliknya | ✖ | terbatas, tercatat | ✔ | ✖ | ✖ | ✖ |
| Rekening owner (P3) | ✖ | ✖ | terbatas (topeng) | ✔ | ✔ | ✔ | ✖ |
| Kontrak & NPWP (P3) | ✖ | ✖ | baca saja | ✔ | ✔ | miliknya | ✖ |
| Laporan keuangan agregat | ✖ | ✖ | ✔ | ✔ | ✔ | propertinya | ✔ (agregat) |
| Data kartu/OTP (P4) | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |

Aturan tambahan:
- **Pinjaman akses sementara** (mis. marketing perlu lihat kontrak) harus berbatas waktu + tercatat di `audit_events`.
- **Owner tidak boleh melihat data tamu properti lain**, walau se-grup.
- **Akses finansial tidak otomatis memberi akses identitas**, dan sebaliknya.
- Export data (CSV/PDF) mewarisi tier tertinggi di dalamnya; export P3 hanya oleh Admin/Finance dengan alasan tercatat.

## 3. Aturan pemakaian AI (paling sering dilanggar)

1. **Pseudonimisasi wajib.** Ke LLM dikirim `guest_ref` (ID acak) + atribut yang benar-benar perlu (mis. jumlah tamu, tanggal). Nama/telepon/email/paspor **diganti token** sebelum dikirim.
2. **Konten eksternal = data, bukan instruksi.** Semua teks dari email, PDF, WhatsApp, atau halaman web masuk sebagai data yang tidak dipercaya. Instruksi di dalamnya **tidak pernah** dijalankan sebagai perintah.
3. **Tidak ada tool write dari jalur bahasa.** Model tidak punya akses membuat/mengubah booking, harga, refund, atau pesan keluar tanpa approval manusia.
4. **Tidak ada klaim tanpa ID.** Setiap fakta di balasan AI harus merujuk record (rate card, policy, availability evidence). Tanpa rujukan ⇒ kalimat tidak boleh dikirim.
5. **Angka dihitung tool, bukan model.** Model dilarang melakukan aritmetika harga; ia hanya menyusun kalimat dari output `quote_calculator`.
6. **Confidence ≠ kepastian.** Ketidakpastian dinyatakan apa adanya; dilarang menyimpulkan "tersedia" dari data level C/D.
7. **Penyimpanan prompt.** Prompt berisi P2/P3 tidak boleh disimpan ke log/tracing pihak ketiga.
8. **Opt-out.** Penolakan penggunaan data untuk pelatihan/peningkatan model harus dihormati dan tersimpan sebagai atribut pada `guest_ref`.

## 4. Kontrol teknis yang harus ada

| Kontrol | Implementasi |
|---|---|
| Pemisahan rahasia dari kode | `.env` tidak di-commit; hanya `.env.template` |
| Scan rahasia pra-commit | `scripts/secret_scan.sh` + `make check` |
| Scan konfigurasi berbahaya | `scripts/unsafe_config_scan.sh` (deteksi `dry_run=false`, `shuffle=true`, dsb.) |
| Audit tak terbantahkan | `audit_events` append-only dengan hash chain (trigger DB menolak UPDATE/DELETE) |
| Idempotensi pembayaran | `UNIQUE(psp_event_id)` + ledger terpisah dari status booking |
| Bukti ketersediaan ter-gate | freshness window + level A–D + `can_confirm` |
| Proteksi overlap | transaksi tunggal `BEGIN IMMEDIATE` + query konflik sebelum insert |
| Enkripsi at-rest P3 | disk/volume terenkripsi + kolom sensitif terenkripsi aplikasi |
| Redaksi log | helper `redact()`; dilarang log objek request mentah |

## 5. Larangan eksplisit

- **Tidak** menyalin data tamu ke spreadsheet pribadi, Google Sheet publik, atau grup chat.
- **Tidak** mengirim daftar tamu/rekening via kanal non-resmi.
- **Tidak** menempelkan isi paspor/kontrak ke prompt AI, walau "hanya untuk diterjemahkan".
- **Tidak** menyimpan password/OTP; jika ada yang mengirim OTP lewat chat, itu indikasi penipuan — eskalasi, jangan simpan.
- **Tidak** membuat akun bersama (shared login) untuk PSP/email; setiap akses punya identitas.

## 6. Insiden data (ringkas — detail di RETENTION_AND_INCIDENT_RESPONSE.md)

Setiap kejadian yang menyentuh P3/P4 = **insiden**, dilaporkan < 1 jam ke pemilik
data & penanggung jawab, dengan pencatatan: apa, kapan, siapa, berapa subjek,
tindakan, dan notifikasi. Pemberitahuan ke subjek data & otoritas mengikuti
kewajiban UU 27/2022 (PDP) dan kontrak — **bukan** keputusan sepihak tim teknis.
