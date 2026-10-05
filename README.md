# Villa Agency OS

> **Batas produk wajib:** repo ini adalah kernel operasi + **dashboard internal** untuk
> manajemen/karyawan villa agency. **Website Marketing Villa untuk calon tamu adalah
> permukaan publik terpisah**: katalog, destinasi, villa detail, inquiry, dan concierge.
> Jangan mencampur UX publik tamu dengan dashboard internal. Rujukan desain/pemisahan:
> `docs/PRODUCT_SURFACE_SEPARATION_2026-10-05.md`.
>
> **Skeleton public site sudah ada (2026-10-05):** `python3 src/marketing_site.py --db data/villa.db --port 8090`
> melayani home/collection/destinations/villa-detail/inquiry (read model publik + intake ke kernel
> case). Detail: seksi "Public marketing website" di bawah.

**Sistem operasi reservation & agency untuk Villa Management (Bali + Yogyakarta)** —
fondasi deterministik sebelum lapisan AI diaktifkan.

Status: **Phase 0–4 selesai** — kernel, uang, dokumen, kanal, dan bukti go-live
end-to-end sudah berjalan; yang tersisa hanya keputusan bisnis per agency
(`docs/GO_LIVE_DECISIONS.md`) dan pemasangan produksi (`docs/DEPLOYMENT.md`).
Bukti jalur tamu utuh: `python3 scripts/go_live_smoke.py --db /tmp/go-live.db`.

---

## Kenapa repo ini ada

Blueprint v1.2 memberi satu peringatan utama:

> "Jangan mulai dari chatbot. Mulai dari property master, policy matrix,
> booking states, dan payment ledger."

Repo ini membangun urutan itu dari bawah: **angka, tanggal, status, dan bukti
harus deterministic dan bisa diaudit lebih dulu**; bahasa (LLM) masuk terakhir
dan tidak pernah boleh menulis aritmetika, status, atau kepastian ketersediaan.

## North Star

| Kontrol | Target | Diukur dari |
|---|---|---|
| Double booking | **0 per bulan** | `holds`/`bookings` overlap check + audit chain |
| Rekonsiliasi pembayaran | **≥ 99% match** | payment ledger vs invoice |
| Konfirmasi dari availability data basi | **0 kejadian** | `can_confirm` gate + evidence freshness |
| Quote dari policy tidak sah | **0 kejadian** | policy snapshot fingerprint |
| Perubahan status tanpa command tervalidasi | **0 kejadian** | state machine (menolak teks bebas) |
| Baris laporan keuangan dengan angka buatan | **0 kejadian** | semua baris berasal dari rate card/policy bertanggal |

## Prinsip yang mengikat semua kode di repo ini

1. **Satu penulis inventory.** Hanya `reservation_kernel` yang boleh menulis hold/booking.
2. **Uang itu integer.** Semua nominal adalah minor unit (`Money`), tidak pernah `float`.
3. **Waktu itu sadar-zona.** Penyimpanan UTC; tanggal menginap adalah kalender properti.
4. **Policy itu immutable + versioned.** Quote/booking menyimpan snapshot, bukan referensi hidup.
5. **Fail closed.** Data hilang/basi/timeout = `UNKNOWN`, bukan `AVAILABLE`.
6. **Bukti sebelum bahasa.** AI tidak membuat fakta; ia hanya merangkai fakta ber-ID.
7. **Audit append-only.** Tidak ada update/delete pada `audit_events` (ditegakkan trigger DB).

## Struktur

```
villa-agency-os/
├── docs/                     # charter, kontrak, policy matrix, state machine, data model
├── src/                      # seluruh sistem (deterministik, stdlib-only)
│   ├── primitives.py         # Money, Clock, IdFactory, error taxonomy, redact
│   ├── schema_loader.py      # apply schema (sqlite/pg), statement splitting, model parser
│   ├── schema/               # 001_initial.sql (sumber) + 001_initial.pg.sql (twin, jangan diedit)
│   ├── audit.py              # SATU-SATUNYA penulis rantai audit (append_event + verifikasi)
│   ├── gates.py              # gerbang fail-closed (contract pack, availability, PII outbound)
│   ├── policy_engine.py      # policy versioning, snapshot, deposit & cancellation rules
│   ├── quote_calculator.py   # aritmetika quote (deterministik, reproducible)
│   ├── quotation.py          # versi quotation V1/V2/V3 + 3 snapshot + penerimaan (§11)
│   ├── discounts.py          # mesin diskon: dua pihak pendana dipisah, authority + margin floor
│   │                         #   (persen DAN absolut) dari policy, eskalasi ke manusia (§11)
│   ├── reservation_kernel.py # hold/booking + proteksi overlap (anti double booking)
│   ├── booking_states.py     # state machine + command contract
│   ├── cases.py              # case management, SLA, timeline kasus
│   ├── intake.py             # ekstraksi rule-first + usulan berprovenans + review manusia
│   ├── matching.py           # pencocokan explainable (ketersediaan dari kernel)
│   ├── approvals.py          # antrean approval manusia (dual control untuk uang/legal)
│   ├── messages.py           # draft pesan terikat fakta (grounded) untuk disetujui manusia
│   ├── invoicing.py          # invoice + baris + PBJT dari policy
│   ├── payments.py           # hosted payment, webhook terverifikasi, ledger, rekonsiliasi
│   ├── documents.py          # dokumen deterministik + versi SUPERSEDED
│   ├── ops_tasks.py          # antrean tugas operasional villa
│   ├── incidents.py          # playbook insiden + tenggat notifikasi
│   ├── connectors.py         # kanal masuk/keluar (email, website, WhatsApp resmi)
│   ├── scheduler.py          # heartbeat: menagih SEMUA deadline, idempoten
│   ├── observability.py      # metrik + health berjenjang + Prometheus
│   ├── dashboard.py          # dashboard internal (stdlib http.server, read-only)
│   └── anchors.py            # publikasi & verifikasi anker rantai audit
├── templates/                # 13 berkas HTML dashboard (12 halaman terdaftar + base), di-escape terpusat
├── tests/                    # pytest — bukti empiris; angka terkini dari `make check`
├── demo/                     # aplikasi demo untuk rapat (tanpa internet, tanpa akun, data contoh)
└── scripts/                  # preflight, anchor, twin generator, scan, `audit_structure.py`, `build_deliverables.py`
```

## Menjalankan

```bash
# Test suite (bukti utama)
python3 -m pytest tests/ -q --tb=short

# Bukti jalur tamu utuh: 16 langkah di database kosong, keluar 0 hanya bila semuanya lulus
python3 scripts/go_live_smoke.py --db /tmp/go-live.db --report /tmp/go-live.json

# Gerbang pre-deploy lengkap (syntax + test + secret scan + unsafe scan + parity skema)
make check

# Audit struktur menyeluruh (read-only, tanpa database): jejak modul→tes, perintah dokumentasi
# yang sudah tidak ada, filter tenant di setiap baca, agregat uang lintas mata uang, aset
# pihak ketiga. Bukan bagian `make check` — temuannya keputusan manusia, bukan penghalang rilis.
make audit-structure

# Lint (opsional, butuh: pip install pyflakes)
make lint

# Parity twin SQLite ⇄ PostgreSQL (gagal kalau drift)
make schema-parity

# Regenerasi twin PostgreSQL dari sumber SQLite (jangan edit file .pg.sql manual)
make pg-twin
```

## Public marketing website (permukaan tamu)

Server read-only + satu form inquiry, terpisah total dari dashboard internal:

```bash
# Server publik (loopback; tambahkan --allow-remote hanya jika memang disengaja)
python3 src/marketing_site.py --db data/villa.db --port 8090

# Dump statis semua halaman publik (home, villas, destinasi, detail villa) ke folder
python3 src/marketing_site.py --db data/villa.db --dump /tmp/marketing-site
```

Halaman: `/`, `/villas`, `/villas/{property_id}`, `/destinations/bali`,
`/destinations/yogyakarta`, `/inquiry`. Batasan yang mengikat modul ini:

- Hanya property berstatus **SELLABLE** yang tampil; unit hanya yang **ACTIVE**.
- Form inquiry menulis **case kernel** via `CrmService.open_inquiry` (channel `website`,
  SLA `web_v1`) — bukan tabel baru, bukan bypass. Email tamu disimpan di tabel `guests`;
  isi pesan tersimpan tersamarkan (content class `P2`) dengan digest raw.
- Anti-abuse: honeypot, rate limit per-IP, cek same-origin pada POST, batas ukuran form.
- Halaman sukses inquiry ber-`noindex`; halaman katalog mengikuti SEO normal.
- Uji: `tests/test_marketing_site.py` (read model, XSS escape, write path, replay,
  rate limit, crawl read-only, dump statis).

## Menjalankan sistem (operasional)

```bash
# Siapkan database + data demo (aman: menolak DB yang sudah berisi data nyata)
python3 scripts/seed_demo.py --db /tmp/demo/villa.db

# Dashboard internal (read-only; POST hanya untuk approval lewat ApprovalService).
# Setiap halaman butuh token. Tanpa token server tetap start, tapi SEMUA permintaan dijawab
# 401 — token dibaca dari header `X-Dashboard-Token` (atau `Authorization: Bearer ...`).
# Tanpa `--token`, token dibuat acak per peran tiap kali server start dan hanya muncul di
# stdout, jadi URL/konfigurasi proxy mati tiap restart. Patok tokennya:
python3 src/dashboard.py --db /tmp/demo/villa.db --host 127.0.0.1 --port 8080 --role owner \
    --token "$VILLA_DASHBOARD_TOKEN"
curl -s -H "X-Dashboard-Token: $VILLA_DASHBOARD_TOKEN" http://127.0.0.1:8080/

# Snapshot HTML statis: menulis berkas lalu keluar, tidak membuka port, tidak butuh token
python3 src/dashboard.py --db /tmp/demo/villa.db --dump /tmp/demo/pages

# Heartbeat: menagih hold/SLA/approval/tenggat notifikasi/penagihan invoice
python3 src/scheduler.py --db data/villa.db --dry-run     # lihat rencana, tanpa menulis
python3 src/scheduler.py --db data/villa.db               # jalankan (idempoten)

# Kesehatan & metrik — kode keluar 0 hanya bila severity terburuk OK/UNKNOWN;
# WARNING atau CRITICAL keluar 1 (probe monitoring, bukan tanda perintahnya gagal)
python3 src/observability.py --db data/villa.db --anchor-dir /var/lib/villa/anchors
python3 src/observability.py --db data/villa.db --format prometheus

# Kanal masuk (email) — isi pesan selalu diperlakukan sebagai data, bukan instruksi
mkdir -p inbox                                       # CLI menolak path masuk yang tidak ada
python3 src/connectors.py email --dir inbox/ --db data/villa.db --dry-run

# Rantai audit: terbitkan anker (dari mesin/akun berbeda) lalu verifikasi
make anchor-publish VILLA_DB=data/villa.db VILLA_ANCHOR_DIR=/var/lib/villa/anchors
make anchor-verify  VILLA_DB=data/villa.db VILLA_ANCHOR_DIR=/var/lib/villa/anchors

# Izin §56 (tool medium-risk untuk mesin) — status kapan saja, menerbitkan hanya oleh owner.
# Menerbitkan WAJIB menyebut database-nya (tanpa VILLA_DB ditolak: izin yang jatuh ke
# data/villa.db mana pun yang kebetulan ada di direktori kerja bukan keputusan siapa pun),
# dan menerbitkan sengaja bukan target make: jalur §56 dibuka oleh keputusan, bukan refleks.
make allowance-status VILLA_DB=data/villa.db
python3 scripts/publish_ai_allowance.py publish \
    --tools availability.request --approved-by owner_anas \
    --effective-from 2026-09-27 --effective-to 2026-10-26

# Menarik kembali: tanggalnya harus disebut bila revisi terbaru mulai di masa depan —
# engine kebijakan menolak revisi yang mundur sebelum revisi terbaru (POLICY_BACKDATED_REVISION)
VILLA_DB=data/villa.db python3 scripts/publish_ai_allowance.py withdraw \
    --approved-by owner_anas --effective-from 2026-09-27

# Nilai kebijakan komersial — diisi manual oleh owner/management, bukan ditulis di kode.
# §15: "Threshold adalah configuration, bukan hard-coded" — engine membaca angkanya dari
# policy store. Lihat dulu key mana yang masih menunggu keputusan:
make policy-status VILLA_DB=data/villa.db

# PENTING: VILLA_DB harus database yang dibuat dari skema TERKINI. Skema hanya diterapkan
# saat berkasnya dibuat — membuka DB lama TIDAK menambah key baru, jadi DB lama akan menolak
# key yang ditambahkan belakangan (mis. commercial.margin_floor_minor) seolah key itu tidak
# pernah ada. `policy-status` menunjukkan key apa saja yang benar-benar ada di DB itu.
#
# Menyetujui satu nilai. WAJIB: dokumen asal nilainya, siapa yang memutuskan, dan VILLA_DB
# eksplisit (nilai yang jatuh ke data/villa.db mana pun yang kebetulan ada di direktori
# kerja adalah keputusan yang tak dibuat siapa pun). `set` sengaja BUKAN target make:
# jalur ini dibuka oleh keputusan, bukan refleks. Selama key belum diterbitkan, engine
# MENOLAK — fail-closed, tidak pernah mengarang nilai default.
VILLA_DB=data/villa.db python3 scripts/publish_policy_value.py set \
    --key commercial.margin_floor_pct --value 20 \
    --source-doc-ref "Keputusan owner 2026-09-26 — kondisi high season" \
    --approved-by owner_anas

# Key yang dibaca mesin diskon (§11): role berwenang, floor persen, floor absolut, dan
# mata uang floor itu. Tipe nilainya ditentukan katalog: ROLE_SET (JSON), PCT (teks desimal),
# COUNT (bilangan), TEXT (kode mata uang).
VILLA_DB=data/villa.db python3 scripts/publish_policy_value.py set \
    --key commercial.discount.authority_roles --value '["owner","manager"]' \
    --source-doc-ref "Keputusan owner 2026-09-26" --approved-by owner_anas
VILLA_DB=data/villa.db python3 scripts/publish_policy_value.py set \
    --key commercial.margin_floor_minor --value 2500 \
    --source-doc-ref "Keputusan owner 2026-09-27 — penjaga 2 adalah 25 USD" --approved-by owner_anas

# Floor absolut itu SATU ANGKA dan satu angka tidak menyebut mata uangnya (ADR-0030).
# Tanpa key di bawah, angka 2500 dibandingkan mentah ke mata uang booking — benar selama
# semua booking se-mata-uang (Bali, USD), salah begitu ada booking IDR (Yogyakarta).
VILLA_DB=data/villa.db python3 scripts/publish_policy_value.py set \
    --key commercial.margin_floor_currency --value USD \
    --source-doc-ref "Keputusan owner 2026-09-27 — harga villa Bali dalam USD" --approved-by owner_anas

# Kurs untuk mengonversi floor ke mata uang booking. Format: {"BASE/QUOTE": "rate"} — satu
# unit BASE bernilai sekian QUOTE. Selama belum terbit, booking di luar mata uang floor
# MENOLAK (FX_RATE_NOT_PUBLISHED), bukan memakai kurs karangan.
#
# Kurs TIDAK diketik tangan: ia harga nyata satu hari, dan harga yang tak pernah diperbarui
# tetap "terlihat benar" sambil perlahan menggeser lantai margin. Ambil dari provider, dan
# sebutkan provider itu di dokumen asalnya (ADR-0031):
VILLA_DB=data/villa.db python3 scripts/refresh_fx_rate.py --approved-by owner_anas

# Batas umur kurs: berapa lama satu revisi kurs boleh dipakai sebelum konversi MENOLAK.
# Keputusan owner: dua hari = 48 jam. Tipe nilainya HOURS; batas diterapkan dalam hari utuh
# (revisi bertanggal hari, bukan jam) dan hari yang lebih ketat yang menang. Tanpa key ini
# SEMUA konversi menolak — engine tidak mengarang batas:
VILLA_DB=data/villa.db python3 scripts/publish_policy_value.py set \
    --key commercial.fx.max_age_hours --value 48 \
    --source-doc-ref "Keputusan owner 2026-09-28 — batas umur kurs 2 hari" --approved-by owner_anas

# Tanggal efektif selalu memakai jam PLATFORM (UTC), bukan jam lokal WIB. Di sebelah timur
# UTC hari lokal mulai lebih dulu: revisi bertanggal lokal lahir "besok" bagi platform —
# hari ini ia tak terlihat (konversi menolak), dan store menolak mundur untuk mengoreksinya.
# Tugas harian menjalankannya sekali sehari; menjalankan dua kali di hari yang sama hanya
# melaporkan UNCHANGED tanpa menerbitkan revisi baru.

# Gerbang go-live: menolak jalan bila keputusan bisnis BLOCKING masih OPEN,
# anker basi, ada data demo di produksi, atau rantai audit tidak utuh
python3 scripts/preflight.py --db data/villa.db --anchor-dir /var/lib/villa/anchors \
    --decisions docs/GO_LIVE_DECISIONS.md --backup /backups/villa-$(date +%F).sqlite
```

Tidak ada dependency eksternal: seluruh kernel memakai Pustaka Standar Python
(`sqlite3`, `decimal`, `datetime`, `hashlib`). Lihat `docs/adr/0001-storage-and-runtime.md`.

## Batas keselamatan yang tidak bisa dinegosiasikan

- Tidak ada konfirmasi dari availability basi/tidak diketahui.
- Tidak ada double booking; hold kedaluwarsa tidak boleh memblokir, hold aktif harus memblokir.
- Quote, hold, pembayaran, dan konfirmasi adalah status terpisah.
- Refund, payout, perubahan rekening bank, relokasi tamu, dan pengecualian policy **wajib approval manusia**.
- Tidak ada nomor kartu/CVV/passport scan masuk ke sistem, log, atau model.
- Pesan/dokumen dari luar adalah **data**, bukan instruksi.
- Tidak ada credential yang di-commit: `.env` diabaikan, hanya `.env.template` yang ditrack.

## Kesiapan go-live

Sebelum sistem menerima tamu dan uang nyata:

```bash
python3 scripts/preflight.py --db data/villa.db \
    --anchor-dir /var/lib/villa/anchors \
    --decisions docs/GO_LIVE_DECISIONS.md \
    --backup /backups/villa-$(date +%F).sqlite
```

Preflight memeriksa tiga hal yang berbeda dan semuanya harus lulus: **kode**
(twin skema, tidak ada rahasia ter-commit, tidak ada switch berbahaya, modul
importable), **data** (versi skema cocok, rantai audit utuh, anker masih segar
dan terverifikasi, tidak ada data demo, integritas relasi), dan **bisnis**
(semua keputusan bertanda BLOCKING di `docs/GO_LIVE_DECISIONS.md` sudah
diputuskan). Test hijau **tidak** bisa menggantikan keputusan bisnis yang belum
diambil — karena itu preflight menolak jalan.

Prosedur pemasangan, systemd unit, backup, dan rollback: `docs/DEPLOYMENT.md`.
Ritme harian dan cara menangani tiap antrean: `docs/OPERATIONS_RUNBOOK.md`.
Ringkasan produk untuk calon pembeli: `docs/SALES_PACKAGE.md`.

## Dokumen wajib baca sebelum mengubah kode

1. `docs/BOOKING_STATE_MACHINE.md` — invariant & transisi resmi.
2. `docs/DATA_MODEL.md` — tabel, kunci, dan aturan bukti (72 tabel di `src/schema/001_initial.sql` + 14 migrasi).
3. `docs/POLICY_MATRIX.md` — apa yang boleh berbeda per properti/kanal.
4. `docs/DATA_CLASSIFICATION_AND_ACCESS.md` — apa yang tidak boleh masuk AI.
5. `docs/GO_LIVE_DECISIONS.md` — keputusan bisnis yang harus ada sebelum go-live.
6. `docs/DEPLOYMENT.md` — pemasangan, backup, rollback, monitoring.

> Dokumen ini alat kerja operasional, **bukan** nasihat hukum/pajak/lisensi.
> Review counsel lokal, konsultan pajak, dan compliance pembayaran tetap wajib
> sebelum menerima dana publik (lihat `docs/PROJECT_CHARTER.md` bagian Open decisions).
