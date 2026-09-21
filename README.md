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
├── src/                      # modul kernel (deterministik)
│   ├── primitives.py         # Money, Clock, IdFactory, error taxonomy
│   ├── schema_loader.py      # apply schema (sqlite/pg), statement splitting
│   ├── schema/               # 001_initial.sql (SQLite) + 001_initial.pg.sql (produksi)
│   ├── audit.py              # append-only event writer + hash chain
│   ├── policy_engine.py      # policy versioning, snapshot, deposit & cancellation rules
│   ├── quote_calculator.py   # aritmetika quote (deterministik, reproducible)
│   ├── reservation_kernel.py # hold/booking + proteksi overlap (anti double booking)
│   ├── booking_states.py     # state machine + command contract
│   └── gates.py              # gerbang fail-closed (contract pack, availability, PII outbound)
├── tests/                    # pytest — bukti empiris
└── scripts/                  # secret scan, unsafe config scan, evaluasi
```

## Menjalankan

```bash
# Test suite (bukti utama)
python3 -m pytest tests/ -q --tb=short

# Gerbang pre-deploy lengkap (syntax + test + secret scan + unsafe scan + parity skema)
make check

# Parity twin SQLite ⇄ PostgreSQL (gagal kalau drift)
make schema-parity

# Regenerasi twin PostgreSQL dari sumber SQLite (jangan edit file .pg.sql manual)
make pg-twin
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

## Dokumen wajib baca sebelum mengubah kode

1. `docs/BOOKING_STATE_MACHINE.md` — invariant & transisi resmi.
2. `docs/DATA_MODEL.md` — tabel, kunci, dan aturan bukti.
3. `docs/POLICY_MATRIX.md` — apa yang boleh berbeda per properti/kanal.
4. `docs/DATA_CLASSIFICATION_AND_ACCESS.md` — apa yang tidak boleh masuk AI.

> Dokumen ini alat kerja operasional, **bukan** nasihat hukum/pajak/lisensi.
> Review counsel lokal, konsultan pajak, dan compliance pembayaran tetap wajib
> sebelum menerima dana publik (lihat `docs/PROJECT_CHARTER.md` bagian Open decisions).
