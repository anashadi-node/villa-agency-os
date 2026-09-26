# Villa Agency OS

**Sistem operasi reservation & agency untuk Villa Management (Bali + Yogyakarta)** —
fondasi deterministik sebelum lapisan AI diaktifkan.

Status: **Phase 0 selesai, Phase 1 (Reservation Kernel) berjalan.**
AI co-pilot, pembayaran, dan integrasi kanal belum diaktifkan (lihat `docs/`).

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
├── templates/                # 12 berkas HTML dashboard (di-escape terpusat)
├── tests/                    # pytest — bukti empiris; angka terkini dari `make check`
└── scripts/                  # preflight, anchor, twin generator, secret & unsafe scan
```

## Menjalankan

```bash
# Test suite (bukti utama)
python3 -m pytest tests/ -q --tb=short

# Gerbang pre-deploy lengkap (syntax + test + secret scan + unsafe scan + parity skema)
make check

# Lint (opsional, butuh: pip install pyflakes)
make lint

# Parity twin SQLite ⇄ PostgreSQL (gagal kalau drift)
make schema-parity

# Regenerasi twin PostgreSQL dari sumber SQLite (jangan edit file .pg.sql manual)
make pg-twin
```

## Menjalankan sistem (operasional)

```bash
# Siapkan database + data demo (aman: menolak DB yang sudah berisi data nyata)
python3 scripts/seed_demo.py --db /tmp/demo/villa.db

# Dashboard internal (read-only; POST hanya untuk approval lewat ApprovalService)
python3 src/dashboard.py --db /tmp/demo/villa.db --host 127.0.0.1 --port 8080 --role owner
python3 src/dashboard.py --db /tmp/demo/villa.db --dump /tmp/demo/pages   # snapshot HTML statis

# Heartbeat: menagih hold/SLA/approval/tenggat notifikasi/penagihan invoice
python3 src/scheduler.py --db data/villa.db --dry-run     # lihat rencana, tanpa menulis
python3 src/scheduler.py --db data/villa.db               # jalankan (idempoten)

# Kesehatan & metrik
python3 src/observability.py --db data/villa.db --anchor-dir /var/lib/villa/anchors
python3 src/observability.py --db data/villa.db --format prometheus

# Kanal masuk (email) — isi pesan selalu diperlakukan sebagai data, bukan instruksi
python3 src/connectors.py email --dir inbox/ --db data/villa.db --dry-run

# Rantai audit: terbitkan anker (dari mesin/akun berbeda) lalu verifikasi
make anchor-publish VILLA_DB=data/villa.db VILLA_ANCHOR_DIR=/var/lib/villa/anchors
make anchor-verify  VILLA_DB=data/villa.db VILLA_ANCHOR_DIR=/var/lib/villa/anchors

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
2. `docs/DATA_MODEL.md` — tabel, kunci, dan aturan bukti (34 tabel).
3. `docs/POLICY_MATRIX.md` — apa yang boleh berbeda per properti/kanal.
4. `docs/DATA_CLASSIFICATION_AND_ACCESS.md` — apa yang tidak boleh masuk AI.
5. `docs/GO_LIVE_DECISIONS.md` — keputusan bisnis yang harus ada sebelum go-live.
6. `docs/DEPLOYMENT.md` — pemasangan, backup, rollback, monitoring.

> Dokumen ini alat kerja operasional, **bukan** nasihat hukum/pajak/lisensi.
> Review counsel lokal, konsultan pajak, dan compliance pembayaran tetap wajib
> sebelum menerima dana publik (lihat `docs/PROJECT_CHARTER.md` bagian Open decisions).
