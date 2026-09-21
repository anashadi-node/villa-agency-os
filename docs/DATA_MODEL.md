# Data Model — 34 Tabel, Kunci, dan Aturan Bukti

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
| `uq_bookings_active_slot` | UNIQUE `(unit_id, check_in)` WHERE `status IN ('CONFIRMED','BALANCE_DUE','BALANCE_OVERDUE','PRE_ARRIVAL','IN_STAY','CANCELLATION_REQUESTED','CANCELLATION_REVIEW','DISPUTED')` | dua booking **menempati** unit yang sama dengan tanggal masuk sama → ditolak DB |
| `uq_holds_active_slot` | UNIQUE `(unit_id, check_in)` WHERE `status = 'active'` | dua hold aktif pada slot sama → ditolak DB |

Index ini adalah jaring terakhir: `reservation_kernel` memeriksa tumpang tindih lebih dulu (pesan error yang manusiawi), database menolak kalau ada yang lolos. Status di luar himpunan di atas **tidak** menahan slot: hanya status final (mis. `CANCELLED`, `CLOSED`, `NO_SHOW`) yang melepas tanggal. Pembatalan yang masih ditinjau dan sengketa **menahan** slot sampai diputuskan — **ADR-0002 Opsi A**, diputuskan pemilik sistem 2026-09-21.

---

## 6. Phase 2 — case, intake, matching, persetujuan, draft

Fondasi deterministik untuk pekerjaan agen: **percakapan masuk tidak pernah dipercaya**,
dan **AI tidak punya jalur tulis ke status/uang**. Dua belas tabel di bawah ini
ditegakkan DDL-nya di `tests/test_schema.py` (`TestPhase2FailClosedGuards`) dan di
PostgreSQL hidup (`TestLivePostgres`).

### `cases`
Satu permintaan tamu = satu case. PK `case_id`, UNIQUE `idempotency_key`, FK opsional
ke `bookings` (terisi setelah hold dikonversi). `status` ∈ `NEW|TRIAGED|MATCHING|
AWAITING_EVIDENCE|QUOTED|HOLD_PENDING|BOOKED|ESCALATED|CLOSED_WON|CLOSED_LOST|
CLOSED_DUPLICATE`; `channel` ∈ `website|email|whatsapp_business|dashboard|phone`
(lowercase — kolom kosakata, bukan status). Jendela SLA dijaga DB: `created_at <
first_response_due_at <= quote_due_at`. Status `CLOSED_*` wajib punya `closed_at`
**dan** `close_reason` ≥ 3 karakter.

### `case_messages` (append-only)
Isi pesan tamu disimpan **ter-minimasi**: `body_sha256` selalu dihitung dari teks
mentah, `body_redacted` wajib `NULL` untuk `P3`/`P4` (tidak pernah disimpan) dan wajib
terisi untuk `P2`. `untrusted` default 1: isi pesan adalah data, bukan perintah.

### `case_events` (append-only)
Timeline case: `seq` unik per case, `command_id` unik per case — satu command = satu
baris, sehingga retry tidak menggandakan riwayat.

### `intake_proposals` + `intake_fields`
Usulan terstruktur dari ekstraksi (metode `rule` lebih dulu, `llm_shadow` hanya
bayangan). Setiap field wajib punya `source_message_id` (provenance) dan
`source_span_start/end`; `field_value = NULL` berarti **UNKNOWN**, bukan tebakan.
Field hanya menjadi fakta setelah direview manusia: `status ≠ PROPOSED` wajib punya
`reviewed_by` + `reviewed_at`; `REJECTED` wajib punya `rejection_reason`. Nilai hasil
ekstraksi immutable — hanya kolom review yang boleh berubah.

### `match_runs` + `match_candidates` (append-only)
Satu run pencocokan menyimpan snapshot fakta + hash-nya, lalu satu baris per unit yang
diperiksa. `rank` unik per run, `score_centi` **integer** (tanpa float di jalur skor),
`reasons`/`deny_reasons` berisi kode + bobot + `evidence_refs` yang menunjuk id nyata.
Kandidat `eligible = 0` **wajib** punya alasan penolakan — dijaga CHECK, bukan konvensi.

### `approval_requests` + `approval_decisions`
Antrean persetujuan manusia. `refund`/`payout`/`bank_change` wajib `required_approvals
>= 2`; UNIQUE `(approval_id, approver)` membuat satu orang tidak bisa menjadi dua
approver. Status diturunkan dari baris keputusan (append-only), bukan dari kolom yang
bisa diakali; `payload_sha256` + `request_hash` + `signature_hash` mendeteksi
penyuntingan permintaan setelah diajukan. `money_minor`+`currency` wajib sepasang.

### `message_drafts`
Draft balasan yang dibangun dari fakta ber-ID. `grounding` ∈ `GROUNDED|UNGROUNDED|
REVIEW_REQUIRED`; CHECK memastikan draft `UNGROUNDED` **tidak bisa** berstatus
`APPROVED`/`SENT`. `SENT` wajib punya `sent_by` (manusia) + `sent_at` + `external_ref`,
dan `APPROVED`/`SENT` wajib punya approver.

### `sla_breaches`
Satu baris per pelanggaran (`subject_type`+`subject_id`+`kind` unik, jadi deteksi
berulang tidak menggandakan). Pelanggaran yang ditutup wajib punya `resolved_at` +
`resolution_note`; `detected_at >= due_at` dijaga DB.

---

## 7. Phase 3 — billing, pembayaran, rekonsiliasi, dokumen, ops, insiden

Uang tidak pernah disimpan sebagai float dan tidak pernah bergerak tanpa jejak.
Sepuluh tabel di bawah ini dijaga `TestPhase3FailClosedGuards` (SQLite) dan
`TestLivePostgres` (trigger di server nyata).

### `invoices` / `invoice_lines`
`total_minor = subtotal_minor + tax_minor + service_minor` **dijaga CHECK** — invoice
yang tidak balance tidak bisa masuk DB. Invoice `ISSUED` wajib punya `invoice_number`
unik ≥ 8 karakter dan `due_at > issued_at`. Invoice `VOID` wajib punya `voided_at` +
`void_reason`, dan **tidak boleh menyimpan uang** (`amount_paid_minor = 0`). Baris
invoice: `amount_minor = quantity * unit_amount_minor`; baris `DISCOUNT` hanya boleh
mengurangi tagihan (`amount_minor <= 0`).

### `payments`
Satu niat bayar = satu baris; `UNIQUE (provider, provider_intent_id)` membuat retry
tidak menciptakan dua niat. Status `SUCCEEDED` wajib punya `settled_at`; `amount_minor
> 0`.

### `payment_events` (append-only)
Setiap webhook yang diterima dicatat, termasuk yang ditolak. Dua CHECK yang penting:
`applied = 1` hanya mungkin bila `signature_verified = 1` (**webhook tanpa tanda tangan
yang sah tidak akan pernah menggerakkan uang**), dan `applied = 0` wajib punya
`rejection_reason`. `UNIQUE (provider, provider_event_id)` = rekonsiliasi idempoten:
event yang dikirim ulang tidak diproses dua kali.

### `ledger_entries` (append-only)
Buku besar dengan rantai hash (`entry_hash`, `prev_hash`, `seq` unik) — pola yang sama
dengan `audit_events`. Akun: `CASH_IN`, `TAX_PAYABLE`, `SERVICE_FEE`, `GUEST_DEPOSIT`,
`REFUND_OUT`, `OWNER_PAYABLE`, `PSP_FEE`.

### `reconciliation_cases`
Setiap selisih punya jenis, tenggat (`due_at > detected_at`, target 48 jam), pemilik,
dan penutupan ber-catatan. Tanpa ini, uang yang tidak cocok hanya menjadi baris log yang
tidak diurus siapa pun.

### `documents`
Dokumen guest-facing disimpan sebagai artifact disk dengan `storage_ref` relatif,
`content_class`, `redaction_profile`, dan `body_sha256`. Versi unik per `document_key`
(regenerate = versi baru, bukan menimpa). Status `GENERATED` wajib punya `storage_ref`.

### `ops_tasks`
Tugas operasional (check-in prep, cleaning, inspeksi, chase pembayaran) dengan
`idempotency_key` unik supaya job terjadwal tidak menggandakan tugas. `DONE`/`CANCELLED`
wajib punya `completed_at`; `BLOCKED` wajib punya `blocked_reason`.

### `incidents` / `incident_steps`
Insiden dengan playbook, jejak langkah append-only, dan aturan yang tidak bisa
dinegosiasi: notifikasi regulator (UU PDP) hanya boleh untuk `SEV1`/`SEV2` dan wajib
punya `notified_at`; `RESOLVED` wajib punya `summary`; `POSTMORTEM_DONE` wajib punya
`postmortem_ref`.

---

## 8. Audit & infrastruktur

### `audit_events`
`audit_event_id` (PK), UNIQUE `seq`, `seq >= 1`, `action`/`actor`/`actor_role`/`entity_type`/`entity_id` wajib ≥ 1 karakter, `hash` & `prev_hash` 64 hex. Rantai hash: setiap baris menunjuk hash baris sebelumnya; baris pertama memakai `GENESIS_HASH`. Trigger menolak `UPDATE`/`DELETE` → tamper terbukti, bukan tersembunyi.

### `idempotency_keys`
PK `idempotency_key` (global, lintas operasi), `operation`, `request_hash` (64 hex), `result_ref`/`result_hash` opsional. Dipakai supaya retry tidak menciptakan hold/booking kedua.

### `schema_meta`
`schema_version` (PK), `applied_at`, `description`.

---

## 9. Gotcha yang harus diketahui semua kontributor

1. **Casing enum: status UPPERCASE, kosakata lowercase (diverifikasi ulang 2026-09-21 lewat audit 96 literal `CHECK ... IN (...)`).** Kolom *status/keadaan* menyimpan nilai **UPPERCASE** di DDL **dan** di modul Python (`'CONFIRMED'`, `'SELLABLE'`, `'ACTIVE'`, `'AVAILABLE'`, `'PENDING'`) — jadi tidak ada normalisasi casing yang perlu dilakukan untuk status. Hanya kolom *kosakata* yang lowercase: `property_type`, `unit_type`, `unit_scope`, `source_type`, `channel`, `method`, `value_type`, `subject_type`, `region`/`province`. Klaim lama dokumen ini ("DDL menyimpan status lowercase") **salah** dan sudah dikoreksi — mempercayainya pernah mengarahkan perbaikan ke arah yang keliru.
2. **`availability_evidence.result = 'unknown'` bukan `available`.** Jangan menulis default yang menguntungkan penjualan.
3. **Persentase itu teks.** Membaca `deposit_pct` langsung sebagai float akan mengembalikan string; gunakan primitives/`policy_engine`.
4. **`policy_fingerprint` wajib disalin** ke `quotes`/`bookings`; referensi hidup ke policy tidak cukup (policy bisa berganti versi).
5. **Twin SQLite ⇄ PostgreSQL wajib tetap setara.** Jangan pernah mengedit `001_initial.pg.sql` dengan tangan: ubah `001_initial.sql`, lalu `make pg-twin`, lalu `make schema-parity`.
6. **Perubahan skema = perubahan parity test.** `tests/test_schema.py` membandingkan tabel/index/trigger kedua dialect; drift akan langsung merah.
