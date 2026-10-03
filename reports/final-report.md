# Villa Agency OS — Laporan Akhir & Paket Bukti Komersial

Versi: 2.0 · Tanggal: 2026-10-03 · Database acuan: dibuat dari nol saat laporan ini dibuat
Sumber tunggal: repo `~/villa-agency-os`. **Setiap angka di bawah berasal dari keluaran
perintah yang tertera di sebelahnya** — tidak ada klaim tanpa perintah.

---

## 1. Di mana sistem ini berdiri (dan kenapa itu bisa dipercaya)

Sistem ini adalah platform reservation & agency untuk villa management yang dijual /
di-berlangganan **per agency** (ADR-0037): platform menyediakan tempat untuk setelan
(provider pembayaran, nomor WhatsApp, daftar villa, tarif pajak, rate card, peran), dan
**agency pemakainya yang mengisi**. Platform tidak memilih vendor untuk tenant.

Inti yang menentukan nilainya — uang, tanggal, status, bukti, gate §30 — deterministic,
diaudit, dan sudah berjalan utuh dari webhook tamu sampai booking `CLOSED` di database kosong.

## 2. Bukti yang bisa dijalankan ulang oleh pembeli

| Bukti | Perintah | Hasil terverifikasi 2026-10-03 |
|---|---|---|
| Suite tes + semua gerbangnya | `make check` | **3463 lulus, 4 skip, 3950 subtest, 0 gagal**; secret-scan 0 temuan (275 berkas); unsafe-scan 0 temuan; twin skema cocok; `PRE-DEPLOY GATE: PASS` |
| Suite yang sama di interpreter kedua | `python3 -m pytest tests/ -q` | 3463 lulus, 4 skip (bebas-interpreter: 3.12.3/SQLite 3.45.1 dan 3.11.16/SQLite 3.53.1) |
| Skema PostgreSQL diterapkan ke server **kosong** | `VILLA_PG_DSN=... python3 -m pytest tests/test_schema.py` | 75 lulus (4 di antaranya tes PostgreSQL hidup: DDL, model, trigger, dan `EXCLUDE` anti-tumpang-tindih) |
| Jalur tamu utuh di database kosong | `python3 scripts/go_live_smoke.py --db /tmp/go-live.db --report /tmp/go-live.json` | **`GO-LIVE SMOKE: PASS` — 16/16 langkah, keluar 0** |
| Gerbang go-live per tenant | `python3 scripts/preflight.py --db ... --anchor-dir ...` | `NO-GO` dengan daftar yang bisa dibaca: anker belum diterbitkan, data demo ada, kesiapan tenant belum diisi — **fail-closed, seperti desainnya** |
| Perintah operasional terdokumentasi | `docs/DEPLOYMENT.md`, `docs/TENANT_SETUP.md`, `docs/OPERATIONS_RUNBOOK.md` | dijalankan satu per satu di database baru, semua sesuai dokumentasi (seed, scheduler dry+real, observability, dashboard 11 halaman) |

## 3. Bukti jalur tamu end-to-end (keluaran nyata, satu kali jalan)

```
 1. tenant          OK      7. offer accepted   OK     13. stay            OK
 2. policy values   OK      8. hold             OK     14. settlement      OK
 3. inbound webhook OK      9. booking          OK     15. closure §30     OK
 4. intake review   OK     10. invoice          OK     16. evidence        OK
 5. availability    OK     11. payment          OK
 6. quote           OK     12. documents        OK
```

Fakta yang dihasilkan perjalanan itu (dari `report.json` run tersebut):

| Fakta | Nilai |
|---|---|
| Kasus | `case_744c0b7d6fee0355` (dibuka oleh webhook WhatsApp **bertanda tangan**) |
| Quote | `quote_b85c63f08fe6f57e` — **IDR 5.775.000** untuk 3 malam (2 dewasa, 1 anak) |
| Hold | `hold_94ced2b7db70a837` — hold kedua pada jendela yang sama **ditolak** |
| Booking | `book_0f524d4e9406c3c3` → **CLOSED** |
| Riwayat status | `HOLD_ACTIVE→CONFIRMED → PRE_ARRIVAL → IN_STAY → COMPLETED` |
| Invoice | `INV-202610-00001` (nomor dari urutan, bukan angka manual) |
| Pembayaran | `payment_788d6e21cc630246` — intent hosted + webhook provider bertanda tangan; **4 entri ledger diverifikasi utuh** |
| Settlement supplier | 4.042.500 minor DUE → PAID (rute `route_default`) |
| Deposit | 2.000.000 minor → RETURNED |
| Dokumen wajib | CONFIRMATION + RECEIPT tercatat (kebijakan `operations.booking.required_document_kinds`) |
| Rantai audit | 18 baris, **hash-chain terverifikasi** setelah seluruh perjalanan |

**Gerbang §30 — tujuh pemeriksaan, semuanya lulus sebelum `CLOSED`:**

1. `guest_checked_out` — booking `COMPLETED`
2. `open_guest_issues` — kasus terbuka 0, follow-up tamu hidup 0
3. `financial_reconciliation` — invoice 5.775.000 dari 5.775.000 tersettle; rekonsiliasi terbuka 0
4. `supplier_reconciliation` — 4.042.500 IDR DUE 1 / PAID 1
5. `security_deposit_resolved` — 2.000.000 IDR RETURNED 1
6. `incidents_closed` — tidak ada insiden terbuka yang menyebut booking ini
7. `required_documents` — 2 dari 2 jenis dokumen wajib tercatat

## 4. Yang ditemukan & diperbaiki pada audit 2026-10-03

Audit ini menemukan **tiga defect nyata**. Dua di antaranya membuat janji sistem tidak bisa
dibuktikan sama sekali — bukan kosmetik.

**4.1 Gate §30 mustahil tercapai (blocker).**
Bukti: hanya `cancel_booking` dan `close_booking` menulis `bookings.status`; `booking_states.apply_transition()`
murni validator dan tidak dipanggil kode produksi mana pun. Gate menuntut booking `COMPLETED`,
tetapi **tidak ada perintah yang bisa menghasilkan `COMPLETED`**. Satu-satunya test yang pernah
mencapai booking selesai melakukannya dengan `UPDATE` mentah — bentuk khas perintah yang hilang.
Akibatnya: gate terimplementasi, teruji, dan tak bisa dibuka; jalur tamu berhenti di `CONFIRMED`.
Perbaikan: `prepare_arrival`, `check_in`, `complete_stay` di `ReservationKernel` (satu penulis per
tabel, dijurnal + diaudit dalam transaksi yang sama, idempoten pada status yang sama, menolak
lompatan status). Dikunci `tests/test_booking_stay_progress.py` (10 tes).

**4.2 Skema PostgreSQL tidak bisa dibuat dari nol (blocker dialek produksi).**
Bukti: `001_initial.pg.sql` mati saat diterapkan ke server kosong dengan
`relation "evidence_objects" does not exist` — `contract_versions` dan `rate_versions`
mendeklarasikan FK ke tabel yang baru dibuat ±1.500 baris kemudian. SQLite menoleransi FK
maju; PostgreSQL tidak. `make schema-parity` tak menangkapnya karena hanya membandingkan
*model*, tak pernah menerapkan DDL ke server. **Empat tes PostgreSQL hidup tidak pernah
berjalan** (`skipUnless VILLA_PG_DSN`).
Perbaikan: urutan tabel dibereskan di `001_initial.sql` (0 rujukan maju tersisa), twin
diregenerasi. Bukti: `tests/test_schema.py` + `VILLA_PG_DSN` → 75 lulus (sebelumnya 4 ERROR).

**4.3 Fixture tes busuk** (terungkap setelah 4.2): `test_range_exclusion_really_blocks_an_overlap`
memakai status hold `'ACTIVE'` yang sudah dihapus WAVE 4-B (ADR-0020). Diganti `'CONFIRMED'`
(anggota `OCCUPYING_HOLD_STATES`).

**4.4 Paket komersial & dokumen menyimpang dari kenyataan.**
`SALES_PACKAGE.md` menyebut 22 modul / 34 tabel / 2.282 test (aktual 51 / 72 / 3.463);
`DATA_MODEL.md` menyebut 13 tabel (aktual 72); `README.md` masih menyatakan "Phase 1 berjalan";
dua docstring menunjuk berkas tes yang tidak ada; satu keputusan bisnis menunjuk nama ADR yang
salah. Semua diperbaiki dan angkanya kini cocok dengan keluaran perintah.

## 5. Batas yang jujur — belum ditutup

Ini bukan daftar "nanti", ini daftar **yang tidak boleh diklaim sudah selesai**:

1. **Keputusan bisnis per agency masih menunggu pengisian** (provider pembayaran per rel,
   nomor WhatsApp, daftar villa, tarif PBJT per kabupaten, rate card, pemegang peran). Platform
   menolak jalan sampai terisi — itu perilaku yang benar, bukan bug.
2. **Anker rantai audit belum diterbitkan** di lingkungan mana pun; produksi harus menerbitkan
   berkala (`make anchor-publish`) supaya penghapusan baris terakhir pun terdeteksi.
3. **Pekerja latar (systemd/scheduler) untuk outbox belum dipasang**: event sudah ditulis satu
   transaksi dengan state bisnisnya, tetapi pengiriman nyata (email/WA/webhook) belum berjalan.
4. **Halaman dashboard Phase 4 untuk `channels.py` dan `shadow_eval.py` belum ada** — kedua modul
   sudah teruji, tetapi belum punya pemanggil produksi.
5. **Pelapisan bahasa (ADR-0011) belum tuntas**: teks Indonesia lama masih ada di `dashboard.py`
   (158), `connectors.py` (90), `incidents.py` (74), `cases.py` (42). Dijaga ratchet — boleh
   menyusut, tidak boleh bertambah.
6. **Modul DLP (pencegahan kebocoran data) belum ada** (baru ada pemindai setelan berbahaya).
7. **Belum ada bukti di lingkungan produksi nyata**: semua bukti di laporan ini dari
   database lokal/PM2 container. Pemasangan produksi (PostgreSQL + anker + monitoring)
   dijalankan lewat `docs/DEPLOYMENT.md` dan belum dieksekusi.

## 6. Cara memverifikasi ulang (5 menit)

```bash
cd ~/villa-agency-os
python3 scripts/go_live_smoke.py --db /tmp/go-live.db --report /tmp/go-live.json   # 16/16, keluar 0
make check                                                                          # 3463 lulus + semua scan
VILLA_PG_DSN=<postgres-kosong> python3 -m pytest tests/test_schema.py -q             # skema PG benar-benar berlaku
python3 scripts/preflight.py --db data/villa.db --anchor-dir /var/lib/villa/anchors  # gerbang go-live per tenant
```

Jika salah satu dari empat perintah di atas gagal, laporan ini tidak berlaku lagi — buktinya
adalah perintahnya, bukan dokumen ini.

---

*Laporan ini menggantikan `reports/final-report.md` versi 2026-09-21 (yang menggambarkan
kondisi Phase 1 dan sudah tidak berlaku).*
