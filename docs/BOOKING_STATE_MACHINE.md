# Booking State Machine — 32 Status, Graf Transisi, dan Guard-nya

Versi: 1.0 · Tanggal: 2026-09-21 · Kode: `src/booking_states.py` · Skema: `src/schema/001_initial.sql`

Dokumen ini menjelaskan **satu-satunya** state machine status booking di Villa Agency OS.
Sumber kebenaran ada di dua tempat dan keduanya dijaga tetap sinkron oleh test:

| Sumber | Isi | Dijaga oleh |
|---|---|---|
| `src/schema/001_initial.sql` — `bookings.status CHECK (status IN (...))` | 32 status kanonik | `states_from_schema()` + `tests/test_booking_states.py::TestStatesAreDerivedFromSchema` |
| `src/schema/001_initial.sql` — index parsial `uq_bookings_active_slot` | status yang **menempati** slot | `occupying_states_from_schema()` + test yang sama |
| `src/booking_states.py` — `_TRANSITION_TABLE`, `GUARD_TABLE` | graf & guard | `test_every_pair_is_classified_exactly_once` (32 × 32 = 1024 pasangan) |

Status **tidak boleh** ditulis ulang dari ingatan di modul lain. Modul ini membaca enum
langsung dari DDL; kalau DDL berubah dan modul tidak, import akan gagal (fail closed,
`SCHEMA_STATE_ENUM_MISMATCH`), bukan diam-diam salah.

---

## 1. 32 status, dikelompokkan per fase

| Fase | Status |
|---|---|
| Enquiry | `INQUIRY`, `QUALIFIED` |
| Ketersediaan | `AVAILABILITY_PENDING`, `AVAILABILITY_CONFIRMED`, `AVAILABILITY_UNAVAILABLE` |
| Penawaran | `QUOTE_DRAFTED`, `QUOTE_SENT`, `QUOTE_EXPIRED` |
| Hold inventaris | `HOLD_REQUESTED`, `HOLD_ACTIVE`, `HOLD_EXPIRED` |
| Pembayaran | `PAYMENT_PENDING`, `PAYMENT_PARTIAL`, `PAYMENT_UNMATCHED`, `PAYMENT_FAILED`, `DEPOSIT_PAID`, `PAID_IN_FULL` |
| Kepastian & kedatangan | `CONFIRMED`, `BALANCE_DUE`, `BALANCE_OVERDUE`, `PRE_ARRIVAL`, `IN_STAY` |
| Selesai | `COMPLETED` |
| Pembatalan | `CANCELLATION_REQUESTED`, `CANCELLATION_REVIEW`, `CANCELLED`, `REFUND_PENDING`, `REFUNDED` |
| Kasus khusus | `NO_SHOW`, `RELOCATED`, `DISPUTED` |
| Penutup | `CLOSED` |

`INQUIRY` adalah satu-satunya status awal; `CLOSED` satu-satunya status terminal
(`TERMINAL_STATES`), dan status terminal **tidak pernah** bisa berpindah lagi
(`TERMINAL_STATE`, diuji untuk 32 target).

### Status yang menempati slot (occupying)

`CONFIRMED`, `BALANCE_DUE`, `BALANCE_OVERDUE`, `PRE_ARRIVAL`, `IN_STAY` — persis sama
dengan predikat index `uq_bookings_active_slot`. Artinya: begitu booking masuk salah
satu status ini, tanggal + unit terkunci dan tidak bisa dijual lagi sampai booking
keluar dari himpunan ini. Status selain itu (mis. `CANCELLED`, `CLOSED`, `NO_SHOW`,
`DISPUTED`) **melepas** slot.

---

## 2. Graf transisi

Bentuk: `from_state -> frozenset(to_state)`. Jumlah sisi legal = **116** dari 1024
pasangan terurut; 31 pasangan adalah self-transition (`SELF_TRANSITION`), 32 pasangan
berasal dari status terminal (`TERMINAL_STATE`), sisanya 845 ilegal
(`INVALID_TRANSITION`).

### 2.1 Jalur utama (didokumentasikan & diuji end-to-end)

```
INQUIRY → QUALIFIED → AVAILABILITY_CONFIRMED → QUOTE_SENT → HOLD_ACTIVE
        → DEPOSIT_PAID → CONFIRMED → PRE_ARRIVAL → IN_STAY → COMPLETED → CLOSED
```

### 2.2 Jalur pintas (fast path) — melewati status pembukuan, bukan fakta

| Sisi | Alasan bisnis |
|---|---|
| `QUALIFIED → AVAILABILITY_CONFIRMED` | manager sudah memegang kalender owner, tidak perlu tahap `PENDING` |
| `AVAILABILITY_CONFIRMED → QUOTE_SENT` | quote dikirim tanpa draf terpisah (template) |
| `QUOTE_SENT → HOLD_ACTIVE` | hold langsung aktif saat bukti transfer masuk |
| `HOLD_ACTIVE → DEPOSIT_PAID` | transfer bank Indonesia sering cair **di dalam** jendela hold |
| `IN_STAY → CANCELLATION_REQUESTED` | tamu minta pulang lebih awal / membatalkan sisa inap |

Semua sisi pintas tetap **wajib melewati guard** status tujuan: tidak ada fakta
(bukti ketersediaan, bukti pembayaran, alasan) yang boleh dilewati.

### 2.3 Aturan khusus yang disengaja

1. **`NO_SHOW` tidak bisa dari `IN_STAY`.** `NO_SHOW` bukan status occupying, jadi
   menandai tamu yang sedang menginap sebagai no-show akan **melepas slot** untuk
   dijual ulang (double booking) sekaligus salah mencatat okupansi. No-show diputuskan
   *sebelum* kedatangan: dari `CONFIRMED`, `BALANCE_DUE`, `BALANCE_OVERDUE`,
   `PRE_ARRIVAL` (dan `CANCELLATION_REVIEW`). Diuji oleh
   `test_no_show_cannot_be_declared_during_a_stay`.
2. **`CLOSED` tidak punya sisi keluar.** Koreksi setelah tutup buku bukan transisi
   status; itu kasus baru (`DISPUTED` dibuka sebelum penutupan).
3. **Tidak ada self-loop.** Transisi ke status yang sama selalu `SELF_TRANSITION`.
4. Setiap status non-terminal harus bisa keluar **dan** mencapai `CLOSED`
   (`test_non_terminal_states_have_a_way_out`).

---

## 3. Guard

Guard adalah alasan state machine lebih baik daripada kolom status bebas teks.
Tiga guard bawaan:

| Guard | Field yang wajib | Dipakai pada |
|---|---|---|
| `evidence` | `evidence_ref` | semua pergerakan pembayaran, `CONFIRMED`, `AVAILABILITY_CONFIRMED` |
| `reason` | `reason` | semua pergerakan yang menulis ulang komitmen (`CANCELLED`, `NO_SHOW`, `DISPUTED`, `CANCELLATION_REQUESTED`, `RELOCATED`, `REFUND_PENDING`) |
| `approval` | `approval_ref` | pergerakan berdampak uang/tamu (`RELOCATED`, `REFUND_PENDING`, `REFUNDED`, `CANCELLATION_REVIEW→CANCELLED`) |

Aturan yang berlaku:

1. **Urutan evaluasi**: `evidence` → `reason` → `approval`. Bukti dievaluasi lebih dulu
   karena pergerakan tanpa bukti tidak boleh "disetujui menjadi sah"; kode error yang
   keluar adalah penyebab paling fundamental. (Konstanta `_GUARD_EVALUATION_ORDER`.)
2. **Urutan pelaporan**: `TransitionOutcome.guards` selalu **terurut alfabetis**, supaya
   baris jurnal stabil walau urutan hook kustom berbeda.
3. **Guard tambahan** (`extra_guards`) bisa berupa callable atau nama guard bawaan
   (`"evidence"`); nama yang tidak dikenal → `UNKNOWN_GUARD` (ditolak **sebelum**
   transisi dievaluasi). Duplikat dijalankan sekali.
4. **Guard kustom yang menolak** harus melempar `DomainError` dengan kode sendiri
   (mis. `REFUND_WINDOW_CLOSED`); modul tidak menyaring kode kustom.
5. `use_default_guards=False` mematikan guard bawaan (dipakai hanya oleh test/migrasi
   dan harus tercatat di audit).

---

## 4. Jurnal transisi

Setiap perpindahan menghasilkan satu baris `booking_state_events` lewat
`TransitionOutcome.event_row(state_event_id)`, dengan 13 kolom **persis** seperti DDL:
`state_event_id, booking_id, seq, from_state, to_state, command, actor, actor_role,
command_id, evidence_ref, approval_ref, reason, occurred_at`.

Sifat yang dijamin:

- **Append-only**: `(booking_id, seq)` unik; menulis ulang baris yang sama ditolak
  database (`IntegrityError`), tidak disaring di kode.
- `seq` adalah integer positif (bukan `bool`, bukan `float`) — diuji.
- `as_dict()` hanya memuat field yang **terisi**; field guard yang tidak dipakai tidak
  ditulis sebagai `None`, supaya `from_mapping(as_dict())` membangun request yang sama.
- `occurred_at` selalu ISO-8601 sadar zona waktu (`+00:00`); waktu naif ditolak.

---

## 5. Kode error

`UNKNOWN_STATE`, `SELF_TRANSITION`, `INVALID_TRANSITION`, `TERMINAL_STATE`,
`MISSING_FIELD`, `INVALID_FIELD`, `MISSING_REASON`, `MISSING_EVIDENCE`,
`MISSING_APPROVAL`, `UNKNOWN_GUARD`, `SCHEMA_STATE_ENUM_MISMATCH`,
`SCHEMA_ENUM_UNAVAILABLE`, `INVALID_STATE_GRAPH`. Semua `SCREAMING_SNAKE`, semua
membawa `details` berupa **tuple** (bukan list) agar bisa di-hash dan dicatat apa adanya.

---

## 6. Risiko terbuka

**Slot bisa dilepas saat pembatalan masih ditinjau.** `CANCELLATION_REQUESTED`,
`CANCELLATION_REVIEW`, dan `DISPUTED` bukan status occupying, sehingga booking yang
sedang berselisih bisa melepas tanggalnya untuk dijual ulang. Ini keputusan bisnis
(okupansi vs kecepatan jual ulang) yang **belum disetujui pemilik sistem** — lihat
`docs/adr/0002-occupancy-during-cancellation-review.md`. Perilaku saat ini dikunci
oleh `test_mid_stay_dispute_leaves_the_occupying_set` supaya tidak berubah tanpa
keputusan eksplisit.

## 7. Cara memverifikasi

```bash
python3 -m pytest tests/test_booking_states.py -q          # 60+ test modul ini
python3 -m pytest tests/ -q                                # seluruh suite
python3 -c "import sys; sys.path.insert(0,'src'); import booking_states as b; \
b.verify_against_schema('sqlite'); b.verify_against_schema('pg'); print('OK')"
python3 scripts/generate_pg_twin.py --check                # twin SQLite ⇄ PostgreSQL
```
