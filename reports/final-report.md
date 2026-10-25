# Villa Agency OS — Laporan Akhir & Paket Bukti Komersial

Versi: 2.1 · Tanggal: 2026-10-04 · Database acuan: dibuat dari nol saat laporan ini dibuat
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
| Suite tes + semua gerbangnya | `make check` | **3472 lulus, 4 skip, 3950 subtest, 0 gagal**; secret-scan 0 temuan (279 berkas); unsafe-scan 0 temuan (279 berkas); twin skema cocok; `PRE-DEPLOY GATE: PASS` |
| Suite yang sama di interpreter kedua | `~/.hermes/hermes-agent/venv/bin/python -m pytest tests/` | keluar **0**, tanpa kegagalan (bebas-interpreter: Python 3.12.3/SQLite 3.45.1 dan Python 3.11.16/SQLite 3.53.1 — dijalankan ulang 2026-10-04) |
| Skema PostgreSQL diterapkan ke server **kosong** | `VILLA_PG_DSN=... python3 -m pytest tests/test_schema.py` | 75 lulus (4 di antaranya tes PostgreSQL hidup: DDL, model, trigger, dan `EXCLUDE` anti-tumpang-tindih) |
| Seluruh suite di atas PostgreSQL hidup | `VILLA_PG_DSN=... python3 -m pytest tests/ -q` | 3467 tes terkumpul, keluar **0**, tanpa kegagalan |
| Jalur tamu utuh di database kosong | `python3 scripts/go_live_smoke.py --db /tmp/go-live.db --report /tmp/go-live.json` | **`GO-LIVE SMOKE: PASS` — 16/16 langkah, keluar 0** |
| **Onboarding tenant (white-label)** | `scripts/import_villas.py --template/--file/--apply`, `scripts/manage_roles.py list/seed/appoint/rename/add/disable`, `scripts/publish_policy_value.py set/show` | **semua `rc=0`** di `/tmp/tenant-verify`: 1 villa+unit diimpor (2 baris audit), 8 peran di-seed, tangga uang (finance_manager, gm, owner) terisi, kebijakan dokumen wajib diterbitkan; `preflight` berhenti menyebut "money ladder has no holder" |
| Gerbang go-live per tenant | `python3 scripts/preflight.py --db ... --anchor-dir ...` | `NO-GO` dengan daftar yang bisa dibaca: anker belum diterbitkan, data demo ada, kesiapan tenant belum diisi — **fail-closed, seperti desainnya** |
| Perintah operasional terdokumentasi | `docs/DEPLOYMENT.md`, `docs/TENANT_SETUP.md`, `docs/OPERATIONS_RUNBOOK.md` | dijalankan satu per satu di database baru, semua sesuai dokumentasi (seed, scheduler dry+real, observability, dashboard 13 berkas halaman) |

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

Audit ini menemukan **lima defect nyata**. Tiga di antaranya membuat janji sistem tidak bisa
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

**4.4 Perintah di dokumen onboarding ditolak platform sendiri (blocker white-label).**
`docs/TENANT_SETUP.md` mengajarkan `--actor "Mr. Owner"` pada enam tempat; platform menolaknya
dengan `INVALID_FIELD: actor must not contain whitespace` — dan penolakan itu benar: aktor
adalah **rujukan**, bukan nama orang. Artinya customer pertama yang mengikuti dokumen akan
berhenti di langkah pertama impor villa. Bukti: perintah dijalankan apa adanya di `/tmp/tenant-verify`
(plan-only `rc=0`, `--apply` `rc=1`), lalu dijalankan dengan `--actor owner_anas` → `VERDICT: APPLIED`.
Perbaikan: enam contoh diganti token yang sah + aturan token aktor ditulis di dokumen.
Dikunci `tests/test_tenant_onboarding_path.py` (8 tes) — termasuk tes yang **memaku penolakan**
itu, supaya dokumen tidak bisa diam-diam kembali mengajarkan perintah yang tidak bisa jalan.

**4.5 Paket komersial & dokumen menyimpang dari kenyataan.**
`SALES_PACKAGE.md` menyebut 22 modul / 34 tabel / 2.282 test (aktual 51 / 72 / 3.472);
`DATA_MODEL.md` menyebut 13 tabel (aktual 72); `README.md` masih menyatakan "Phase 1 berjalan";
dua docstring menunjuk berkas tes yang tidak ada; satu keputusan bisnis menunjuk nama ADR yang
salah. Semua diperbaiki dan angkanya kini cocok dengan keluaran perintah.

## 5. Batas yang jujur — belum ditutup

Ini bukan daftar "nanti", ini daftar **yang tidak boleh diklaim sudah selesai**:

1. **Keputusan bisnis per agency masih menunggu pengisian** (provider pembayaran per rel,
   nomor WhatsApp, daftar villa pelanggan sendiri, tarif PBJT per kabupaten, rate card, pemegang
   peran, serta 14 kunci kebijakan lain yang belum diterbitkan). **Mekanismenya sudah terbukti
   jalan** — impor villa, peran, dan penerbitan kebijakan semuanya `rc=0` (bukti §2 baris
   onboarding tenant) — yang belum ada adalah **isinya**, dan isi itu keputusan pemilik agency,
   bukan pekerjaan platform. Platform menolak jalan sampai terisi; itu perilaku benar, bukan bug.
2. **Anker rantai audit belum diterbitkan** di lingkungan mana pun; produksi harus menerbitkan
   berkala (`make anchor-publish`) supaya penghapusan baris terakhir pun terdeteksi.
3. **Pekerja latar (systemd/scheduler) untuk outbox belum dipasang**: event sudah ditulis satu
   transaksi dengan state bisnisnya, tetapi pengiriman nyata (email/WA/webhook) belum berjalan.
4. **Website Marketing publik: skeleton production-ready selesai (2026-10-05)** —
   `src/marketing_site.py` + `templates/marketing/` (8 template) + `tests/test_marketing_site.py`
   (35 test, hijau): home, villa collection, destination Bali/Yogyakarta, villa detail,
   inquiry intake via `CrmService.open_inquiry` (channel `website`, SLA `web_v1`, PII di tabel
   `guests`, idempotency + honeypot + rate limit + same-origin). Server loopback + mode dump
   statis. Sisa pekerjaan: hosting produksi, foto/copy per villa, availability hint, owner page.
5. **Pelapisan bahasa (ADR-0011) belum tuntas**: teks Indonesia lama masih ada di `dashboard.py`
   (158), `connectors.py` (90), `incidents.py` (74), `cases.py` (42). Dijaga ratchet — boleh
   menyusut, tidak boleh bertambah.
6. **Modul DLP (pencegahan kebocoran data) belum ada** (baru ada pemindai setelan berbahaya).
7. **Belum ada bukti di lingkungan produksi nyata**: semua bukti di laporan ini dari
   database lokal dan satu container PostgreSQL uji. Pemasangan produksi (PostgreSQL +
   anker + monitoring) dijalankan lewat `docs/DEPLOYMENT.md` dan belum dieksekusi.

## 6. Audit lanjutan 2026-10-04 (setelah laporan ini disusun)

Empat lapis pemeriksaan dijalankan ulang pada 2026-10-04. Hasilnya:

| Lapis | Perintah | Hasil |
|---|---|---|
| Gerbang penuh | `make check` | **3472 lulus, 4 skip, 0 gagal**; secret-scan 0 temuan (279 berkas); unsafe-scan 0 temuan (279 berkas); twin skema cocok; `PRE-DEPLOY GATE: PASS` |
| Suite interpreter kedua | `venv/bin/python -m pytest tests/` | keluar **0**, tanpa kegagalan (3.11.16 / SQLite 3.53.1) |
| Audit struktur (baru) | `make audit-structure` | **0 FAIL, 0 WARN, 14 INFO** — 14 pemeriksaan: jejak modul→tes, perintah dokumentasi yang masih menunjuk berkas nyata (16 target make), docstring modul, penanda TODO, `except` telanjang, `shuffle=True`, halaman⇄template, **filter tenant di setiap baca**, **agregat uang lintas mata uang**, jam dinding di tes, literal angka SQL, aset pihak ketiga, daftar bahasa |
| Perintah dokumentasi di database baru (22 perintah) | `docs/DEPLOYMENT.md`, `README.md`, `docs/TENANT_SETUP.md` | semua sesuai dokumen; seluruh `rc≠0` adalah penolakan yang dirancang (seed ulang menolak menimpa, preflight menolak DB demo jadi produksi: 5 gagal → 2 setelah anker + cadangan ada) |

Temuan & perbaikan pada 2026-10-04:

1. **Agregat uang lintas mata uang di dashboard (cacat nyata).** Halaman keuangan menjumlahkan
   `SUM(total_minor) FROM invoices` tanpa memisahkan mata uang — benar hanya selama semua tagihan
   sejenis Rupiah. Diperbaiki menjadi satu baris per mata uang
   (`GROUP BY currency`), dengan tes baru `test_finance_summary_states_each_currency_separately`
   yang gagal bila dua mata uang digabung lagi.
2. **Alat audit baru** `scripts/audit_structure.py` + target `make audit-structure` (sengaja bukan
   bagian `make check`: temuannya keputusan manusia, bukan penghalang rilis).
3. **Dokumentasi:** dua skrip yang tidak pernah disebut (audit struktur, `build_deliverables.py`)
   kini ada di README; hitungan dashboard dinormalisasi menjadi 13 berkas HTML
   (12 halaman terdaftar + base); `demo/README.md` disesuaikan dengan aplikasi demo baru.
4. **Tidak ditemukan:** bug di jalur uang/tanggal/status/izin, kebocoran rahasia, perintah
   dokumentasi yang busuk, maupun pembacaan data lintas agensi.

Batas yang tetap terbuka pada audit ini (jujur): suite **PostgreSQL hidup** tidak dijalankan ulang
(butuh server PostgreSQL; hasil 2026-10-03 tetap yang terakhir: 75 lulus untuk skema, seluruh suite
keluar 0), dan pemasangan produksi belum dieksekusi.

## 7. Audit keamanan & skalabilitas (2026-10-25)

**Premis diuji lebih dulu: repo ini tidak punya PHP, Node, atau Laravel.** Census: 0 berkas
`.php`/`.js`/`.ts`/Blade, 0 `composer.json`/`package.json`, 0 dependency runtime (57 modul
`src/` = stdlib murni). Kelas kerentanan PHP/Laravel/Node karena itu **tidak berlaku** — bukan
karena sudah ditambal, tetapi karena kodenya tidak ada. Yang benar-benar diaudit: dua server
HTTP stdlib, lapisan data, kripto webhook/TOTP, penanganan berkas, dan rantai pasok.

Temuan & perbaikan:

1. **DoS satu socket (SEDANG → ditutup).** Kedua server single-threaded **tanpa batas baca**
   (`timeout=None`). Satu klien yang mengirim setengah permintaan memegang satu-satunya slot
   layanan selamanya — tanpa kredensial. Diperbaiki: `CONNECTION_TIMEOUT_SECONDS = 20`. Bukti
   dua arah: dengan `None` socket tetap terbuka setelah 1 dtk; dengan timeout, server menutup
   dan langsung melayani klien berikutnya (`tests/test_server_hardening.py`).
2. **Antrean listen terlalu dangkal (SEDANG → ditutup).** `request_queue_size` bawaan 5 → SYN
   dibuang kernel, klien mengulang 1 dtk kemudian. 20 pengunjung serentak: **1.248,2 ms → 74,3 ms
   (16,8×)**, throughput 16,0 → **269,3 req/s**, klien terlambat 1.000 ms → 63,7 ms.
3. **Dashboard bisa di-iframe (RENDAH → ditutup).** `default-src 'none'` tidak mengatur frame;
   ditambah `X-Frame-Options: DENY` + `frame-ancestors 'none'; base-uri 'none'`.

Pengukuran (`data/villa.db`, 3 properti): `/` median **3,42 ms**, `/villas` **0,77 ms**, plafon
aritmetika `1/latency` ≈ **292 req/s**. Hambatan berikutnya: penulis tunggal SQLite — pindah ke
PostgreSQL bila utilisasi penulis >0,9 (jalur sudah diuji, seluruh suite lulus di PG).

Scan: `semgrep` (p/python + p/secrets) **0 temuan**; `trivy fs` **0 secret, 0 misconfig**
(catatan jujur: tanpa lockfile/IaC hasilnya nyaris kosong *by construction* — bukan bukti kuat);
secret-scan internal 0 temuan / 279 berkas.

Upgrade: **57/57 modul `src/` diimpor di Python 3.14.7**; suite penuh lulus di 3.11.16 dan
3.12.3. Runtime stdlib → tanpa treadmill CVE pihak ketiga.

Rincian, tangga scaling, dan checklist go-live: `docs/SCALING_AND_UPGRADE.md`.
Ukur ulang sendiri: `python3 scripts/perf_probe.py --db data/villa.db`.

Batas jujur: tidak ada uji beban di atas 20 konkuren; TLS tetap tugas reverse proxy (bukan kode
ini); tidak ada uji penetrasi eksternal; Python 3.14 belum menjalankan suite (pytest tidak
terpasang untuk interpreter itu).

**Di luar keamanan: gerbang rilisnya sendiri sedang merah.** `make check` di HEAD `0b5f0a3` gagal
pada `tests/test_reservation_kpi_cli.py`: kasus uji dipaku ke `NOW` (21-Sep) sedangkan perintah KPI
memakai jam nyata dengan jendela 28 hari — pada 25-Okt kasus itu berumur 34 hari, bundle mencetak
`inquiries 0`, tes gagal. Bukan bug produk (jendela 28 hari memang benar); tesnya yang kedaluwarsa.
Fixture kini menempatkan baris relatif terhadap jam nyata dan memenuhi `ck_cases_sla_window`.
Status *pre-existing* dibuktikan dengan `git stash`: tes gagal juga tanpa perubahan audit ini.

## 8. Cara memverifikasi ulang (10 menit)

```bash
cd ~/villa-agency-os
python3 scripts/go_live_smoke.py --db /tmp/go-live.db --report /tmp/go-live.json   # 16/16, keluar 0
make check                                                                          # 3472 lulus + semua scan
VILLA_PG_DSN=<postgres-kosong> python3 -m pytest tests/test_schema.py               # skema PG benar-benar berlaku
VILLA_PG_DSN=<postgres-kosong> python3 -m pytest tests/ -q                          # seluruh suite di PostgreSQL

# Onboarding seperti customer baru (semua di /tmp, tak menyentuh data/villa.db):
python3 scripts/import_villas.py --template > /tmp/villa-list.csv
VILLA_DB=/tmp/tenant.db python3 scripts/import_villas.py --file /tmp/villa-list.csv --actor owner_anas \
    --agency agency_default --actor-role owner --source-ref villa-list-okt-2026.csv --apply
VILLA_DB=/tmp/tenant.db python3 scripts/manage_roles.py seed --apply --actor owner_anas --actor-role owner
VILLA_DB=/tmp/tenant.db python3 scripts/manage_roles.py list                        # tangga uang harus punya pemegang
```

Jika salah satu perintah di atas gagal, laporan ini tidak berlaku lagi — buktinya
adalah perintahnya, bukan dokumen ini.

---

*Laporan ini menggantikan `reports/final-report.md` versi 2026-09-21 (yang menggambarkan
kondisi Phase 1 dan sudah tidak berlaku).*
