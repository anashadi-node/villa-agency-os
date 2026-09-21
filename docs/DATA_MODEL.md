# Data Model — 13 Tabel, Kunci, dan Aturan Bukti

Versi: 1.0 · Tanggal: 2026-09-21 · Sumber: `src/schema/001_initial.sql` (SQLite) ⇄ `src/schema/001_initial.pg.sql` (PostgreSQL, generated)

Angka ringkas dari skema saat ini: **13 tabel, 13 index (2 di antaranya unique parsial), 8 trigger, 14 enum**. Hitung ulang kapan saja dengan:

```bash
python3 -c "import sys; sys.path.insert(0,'src'); import schema_loader as s; m=s.parse_schema_model('sqlite'); print(len(m.tables), len(m.indexes), len(m.triggers), len(m.enums))"
```

---

## 0. Prinsip yang mengikat isi tabel

1. **Uang = integer minor unit.** Tidak ada kolom `REAL`/`float` untuk nominal. Persentase disimpan sebagai **teks** (`'30'`, `'5.5'`) supaya tidak ada pembulatan biner, lalu divalidasi `cast(... as real)` di CHECK.
2. **Waktu = teks ISO-8601 UTC** (`2026-09-21T08:00:00+00:00`). Tanggal inap (`check_in`, `check_out`) adalah kalender properti (zona waktu ada di `properties.timezone`).
3. **Hash selalu 64 hex** (`prev_hash`, `hash`, `fingerprint`, `policy_fingerprint`, `request_hash`).
4. **Bukti sebelum bahasa.** Ketersediaan butuh baris `availability_evidence` dengan `captured_at < valid_until`; konfirmasi hanya boleh dari evidence level `a`/`b`.
5. **Append-only ditegakkan database**, bukan disiplin manusia: 4 tabel punya trigger penolak `UPDATE`/`DELETE`.

---

## 1. Master data

### `properties`
| Kolom kunci | Aturan |
|---|---|
| `property_id` (PK) | ref opaque |
| `property_type` | `villa` \| `resort` \| `guesthouse` \| `apartment` \| `homestay` |
| `province` | `bali` \| `di yogyakarta` |
| `timezone` | ≥ 3 karakter (`Asia/Makassar`, `Asia/Jakarta`) |
| `status` | `sellable` \| `sellable_with_review` \| `not_sellable` \| `suspended` |
| `contract_pack_status` | `draft` \| `verified` \| `activated` \| `expired` — **Gate #0**: unit hanya boleh dijual bila pack `activated` |
| `contract_pack_evaluated_at` | kapan status pack terakhir dievaluasi (dipakai gate untuk data basi) |

### `units`
`unit_id` (PK), `property_id` → FK `properties`. `unit_type ∈ {entire_villa, suite, room, bungalow, compound_unit}`, `status ∈ {active, maintenance, suspended}`, `max_guests >= base_capacity`.
Index `ix_units_property (property_id, status)`.

---

## 2. Policy & harga (versioned, immutable)

### `policy_versions`
Satu versi policy per properti (opsional per unit lewat `unit_scope`).
- UNIQUE `(property_id, unit_scope, version)`.
- `unit_scope ∈ {property, unit}`.
- Batas guardrail (dicek DB **dan** `policy_engine._LIMIT_BOUNDS`): `deposit_pct` 0–100, `service_fee_pct` 0–25, `tax_pbjt_pct` 0–10, `max_discount_pct` 0–50, `quote_valid_hours` 1–168, `hold_ttl_minutes` 15–1440, `max_hold_extensions` 0–3, `effective_from <= effective_to`.
- `payload` (JSON) + `fingerprint` (64 hex) = snapshot yang **wajib** disalin ke `quotes`/`bookings`; `source_doc_ref` + `approved_by` wajib (tidak ada angka tanpa dokumen bertanggal).
- Trigger: **tidak boleh** `UPDATE`/`DELETE` (versi baru = baris baru).

### `rate_cards` / `rate_seasons`
- `rate_cards`: UNIQUE `(property_id, unit_scope, version)`, `weekly_discount_pct` 0–50, `monthly_discount_pct` 0–60, `approved_by` + `source_doc_ref` wajib. Append-only.
- `rate_seasons`: FK `rate_card_id` `ON DELETE CASCADE`, `start_date <= end_date`, `min_stay_nights >= 1`, `priority >= 0`. Musim dipilih oleh `priority` tertinggi yang menutupi malam itu.

---

## 3. Inventory & bukti ketersediaan

### `availability_evidence`
| Kolom | Aturan |
|---|---|
| `evidence_level` | `a`/`b`/`c`/`d` (a = paling kuat) |
| `source_type` | `property_api` \| `booking_engine` \| `manager_written` \| `ical` \| `memory` \| `manual_ops` |
| `result` | `available` \| `occupied` \| `unknown` |
| `can_quote` / `can_hold` / `can_confirm` | 0/1, **satu-satunya** sumber izin tindakan |
| `occupancy_ranges` | JSON array rentang yang sudah terisi |
| `ck_evidence_window` | `captured_at < valid_until` (bukti tanpa jendela valid bukan bukti) |
| `ck_evidence_confirm_requires_ab` | `can_confirm = 1` hanya boleh bila level `a`/`b` |

Fail-closed: tidak ada evidence, atau evidence kadaluarsa/`unknown` ⇒ jawabannya `UNKNOWN`, bukan `AVAILABLE`.

### `holds`
- `hold_id` (PK), WAJIB punya `evidence_id` (FK) dan `policy_version_id` (FK).
- `status ∈ {active, released, converted, expired}`; `ck_holds_closed`: status bukan `active` ⇒ `closed_at` wajib terisi; `ck_holds_expiry`: `created_at < expires_at`.
- UNIQUE `idempotency_key` — hold tidak bisa dobel.
- Mem blokir inventory: `uq_holds_active_slot` (lihat §5).

---

## 4. Transaksi

### `quotes`
`quote_id` (PK) + relasi ke `properties`, `units`, `rate_cards`, `policy_versions`.
- Rincian uang: `accommodation_minor`, `extra_guest_minor`, `cleaning_minor`, `extras_minor`, `discount_minor`, `subtotal_minor`, `service_fee_minor`, `tax_minor`, `total_minor`, `security_deposit_minor` — semua ≥ 0, semuanya integer.
- `lines` (JSON) + `fingerprint` (64 hex) + `policy_fingerprint` (64 hex): quote bisa dibuktikan ulang malam itu juga.
- `status ∈ {draft, sent, accepted, expired, void}`; `ck_quote_expiry`: `created_at < expires_at`; `ck_quote_stay`: `check_in < check_out`.

### `bookings`
- PK `booking_id`; FK ke `properties`, `units`, `policy_versions`.
- `status`: enum 32 nilai (lowercase di DB) — lihat `docs/BOOKING_STATE_MACHINE.md`.
- `payment_status ∈ {unpaid, deposit_cleared, cleared_full, partially_refunded, refunded, failed, unmatched}`.
- `ck_booking_paid_le_total`: `amount_paid_minor <= total_minor` (tidak ada "kelebihan bayar" yang tak terjelaskan di kolom ini).
- `ck_bookings_stay`: `check_in < check_out`; `nights >= 1`; `adults >= 1`.
- `version_lock >= 1` (optimistic concurrency), `idempotency_key` UNIQUE, `policy_fingerprint` 64 hex.
- Kolom status pembayaran/konfirmasi terpisah (`confirmed_at`, `cancelled_at`) — **konfirmasi bukan efek samping pembayaran** kecuali policy menyatakan sebaliknya.

### `booking_state_events`
Jurnal transisi: `state_event_id` (PK), FK `booking_id`, UNIQUE `(booking_id, seq)` **dan** `(booking_id, command_id)`, `length(command_id) >= 8`, `seq >= 1`. Append-only (trigger). Isi baris dibentuk oleh `TransitionOutcome.event_row()`.

---

## 5. Invariant anti double booking

| Index | Definisi | Arti |
|---|---|---|
| `uq_bookings_active_slot` | UNIQUE `(unit_id, check_in)` WHERE `status IN ('confirmed','balance_due','balance_overdue','pre_arrival','in_stay')` | dua booking **menempati** unit yang sama dengan tanggal masuk sama → ditolak DB |
| `uq_holds_active_slot` | UNIQUE `(unit_id, check_in)` WHERE `status = 'active'` | dua hold aktif pada slot sama → ditolak DB |

Index ini adalah jaring terakhir: `reservation_kernel` memeriksa tumpang tindih lebih dulu (pesan error yang manusiawi), database menolak kalau ada yang lolos. Status di luar himpunan di atas **tidak** menahan slot (lihat ADR-0002 untuk konsekuensi pembatalan/sengketa yang belum diputuskan).

---

## 6. Audit & infrastruktur

### `audit_events`
`audit_event_id` (PK), UNIQUE `seq`, `seq >= 1`, `action`/`actor`/`actor_role`/`entity_type`/`entity_id` wajib ≥ 1 karakter, `hash` & `prev_hash` 64 hex. Rantai hash: setiap baris menunjuk hash baris sebelumnya; baris pertama memakai `GENESIS_HASH`. Trigger menolak `UPDATE`/`DELETE` → tamper terbukti, bukan tersembunyi.

### `idempotency_keys`
PK `idempotency_key` (global, lintas operasi), `operation`, `request_hash` (64 hex), `result_ref`/`result_hash` opsional. Dipakai supaya retry tidak menciptakan hold/booking kedua.

### `schema_meta`
`schema_version` (PK), `applied_at`, `description`.

---

## 7. Gotcha yang harus diketahui semua kontributor

1. **Casing enum berbeda antara DB dan kode.** DDL menyimpan nilai **lowercase** (`'confirmed'`, `'available'`, `'sellable'`, `'active'`, `'bali'`), sementara modul Python memakai konstanta **UPPERCASE** (`CONFIRMED`, `AVAILABLE`, `SELLABLE`, `ACTIVE`, `Bali`). Setiap jembatan DB ⇄ kode harus menormalkan; pernah menjadi sumber bug nyata di kernel.
2. **`availability_evidence.result = 'unknown'` bukan `available`.** Jangan menulis default yang menguntungkan penjualan.
3. **Persentase itu teks.** Membaca `deposit_pct` langsung sebagai float akan mengembalikan string; gunakan primitives/`policy_engine`.
4. **`policy_fingerprint` wajib disalin** ke `quotes`/`bookings`; referensi hidup ke policy tidak cukup (policy bisa berganti versi).
5. **Twin SQLite ⇄ PostgreSQL wajib tetap setara.** Jangan pernah mengedit `001_initial.pg.sql` dengan tangan: ubah `001_initial.sql`, lalu `make pg-twin`, lalu `make schema-parity`.
6. **Perubahan skema = perubahan parity test.** `tests/test_schema.py` membandingkan tabel/index/trigger kedua dialect; drift akan langsung merah.
