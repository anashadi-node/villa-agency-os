# Booking State Machine — 32 Status, Graf Transisi, dan Guard-nya

Versi: 1.1 · Tanggal: 2026-09-21 · Kode: `src/booking_states.py` · Skema: `src/schema/001_initial.sql`

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

### Tahapan hold menurut bahasa bisnis (ADR-0034)

Tim reservasi memakai tiga nama tahap. Ketiganya **label**, bukan status baru — dan
setiap label mendarat di status kanonik yang sudah ada:

| Tahap (bahasa bisnis) | Status booking | Status hold | Syarat yang harus benar |
|---|---|---|---|
| **1. Tentative Holding** | `HOLD_REQUESTED` | `REQUESTED` / `AWAITING_SUPPLIER` | unit ditahan sementara tamu memutuskan; **villa belum mengonfirmasi** hold |
| **2. Request Hold** | `HOLD_ACTIVE` -> `PAYMENT_PENDING` | `CONFIRMED` | **villa sudah mengonfirmasi** hold — uang tidak pernah diterima atas hold yang belum dikonfirmasi |
| **3. Holding Booking Confirmed / Villa Booked** | `DEPOSIT_PAID` / `PAID_IN_FULL` -> `CONFIRMED` | `CONVERTED` | dana terverifikasi masuk ke akun agency (ADR-0033); `CONFIRMED` masih menuntut bukti block dari villa |
| **batal / tamu tidak membayar** | `CANCELLED` | `RELEASE_REQUESTED` -> `RELEASE_CONFIRMED` | Reservation mengirim surat/email release hold ke villa |

**Satu tempat label dan status berbeda — jangan sampai tertukar:** tahap 3 disebut
*"Booking Confirmed"*, tapi status `CONFIRMED` baru dicapai setelah bukti block dari
villa diterima (ADR-0033). Dana masuk saja = `DEPOSIT_PAID` / `PAID_IN_FULL`. Laporan
yang menghitung `CONFIRMED` sebagai "uang masuk" akan **kurang hitung** booking yang
sudah dibayar; laporan yang menghitung `DEPOSIT_PAID` sebagai "sudah booked penuh"
akan **lebih hitung**.

### Status yang menempati slot (occupying)

Delapan status: `CONFIRMED`, `BALANCE_DUE`, `BALANCE_OVERDUE`, `PRE_ARRIVAL`, `IN_STAY`,
`CANCELLATION_REQUESTED`, `CANCELLATION_REVIEW`, `DISPUTED` — persis sama dengan predikat
index `uq_bookings_active_slot`. Artinya: begitu booking masuk salah satu status ini,
tanggal + unit terkunci dan tidak bisa dijual lagi sampai booking keluar dari himpunan
ini. Status selain itu (mis. `CANCELLED`, `CLOSED`, `NO_SHOW`, `COMPLETED`, `RELOCATED`)
**melepas** slot.

Tiga status terakhir ditambahkan oleh **ADR-0002 Opsi A** (diputuskan pemilik sistem
2026-09-21): pembatalan yang masih ditinjau, dan sengketa saat tamu masih **fisik di dalam
unit**, tetap menahan tanggal — slot baru dibuka ketika pembatalannya *diputuskan*.
Fail-closed: lebih baik unit tertahan selama review daripada double booking.

---

## 2. Graf transisi

Bentuk: `from_state -> frozenset(to_state)`. Jumlah sisi legal = **120** dari 1024
pasangan terurut; 31 pasangan adalah self-transition (`SELF_TRANSITION`), 32 pasangan
berasal dari status terminal (`TERMINAL_STATE`), sisanya 841 ilegal
(`INVALID_TRANSITION`).

Empat sisi `CONFIRMED`/`BALANCE_DUE`/`PRE_ARRIVAL`/`IN_STAY` → `CANCELLED` ditambahkan
oleh **ADR-0069 opsi (a)** (keputusan owner 2026-11-13). Sebelumnya kernel sudah menulis
sisi itu sementara graf tidak memuatnya. Setiap sisi pembatalan tetap wajib `reason`;
jalur permintaan tamu yang berdampak uang tetap lewat `CANCELLATION_REQUESTED`.

### 2.1 Jalur utama (didokumentasikan & diuji end-to-end)

```
INQUIRY → QUALIFIED → AVAILABILITY_CONFIRMED → QUOTE_SENT → HOLD_ACTIVE
        → DEPOSIT_PAID → CONFIRMED → PRE_ARRIVAL → IN_STAY → COMPLETED → CLOSED
```

Jalur ini **dijalankan utuh** oleh `scripts/go_live_smoke.py` di database kosong
(webhook bertanda tangan → kasus → intake direview → bukti ketersediaan → quote →
penawaran diterima tamu → hold → booking → invoice → pembayaran lewat webhook provider →
dokumen → menginap → settlement supplier + deposit → gate §30 → `CLOSED`),
dikunci oleh `tests/test_go_live_smoke.py`.

### 2.1.1 Siapa yang menjalankan tiap langkah sesudah booking

Setiap perpindahan status booking adalah **satu perintah bernama**, dijalankan aktor
manusia yang tercatat, dijurnal di `booking_state_events` dan diaudit di `audit_events`
dalam transaksi yang sama:

| Perintah (`ReservationKernel`) | Dari → ke | Dijalankan oleh |
|---|---|---|
| `reserve_from_hold` | `HOLD_ACTIVE` → `PAYMENT_PENDING` | reservation; hold tetap `CONFIRMED` dan memegang slot, invoice kini bisa diterbitkan |
| `mark_paid` | `PAYMENT_PENDING`/`PAYMENT_PARTIAL` → `DEPOSIT_PAID`/`PAID_IN_FULL`; `DEPOSIT_PAID` → `PAID_IN_FULL` | finance, dengan pembayaran `SUCCEEDED` pada invoice booking itu (bukti = `payment_id`) |
| `confirm_booking` | `DEPOSIT_PAID`/`PAID_IN_FULL` → `CONFIRMED` | reservation, dengan bukti blok villa level A/B `can_confirm` yang ditangkap **setelah** reservasi; hold → `CONVERTED` (ADR-0033) |
| ~~`confirm_from_hold`~~ | ~~`HOLD_ACTIVE` → `CONFIRMED`~~ | **dipensiunkan** (audit 2026-11-13, K2): selalu menolak `CONFIRM_REQUIRES_PAYMENT_AND_BLOCK_PROOF`; sisi itu tidak ada di graf kanonik |
| `prepare_arrival` | `CONFIRMED` → `PRE_ARRIVAL` | tim reservasi, saat menyiapkan kedatangan tamu |
| `check_in` | `PRE_ARRIVAL` → `IN_STAY` | tim reservasi/greeting, saat tamu tiba |
| `complete_stay` | `IN_STAY` → `COMPLETED` | tim operasi, saat masa inap berakhir |
| `cancel_booking` | (hampir semua status hidup) → `CANCELLED` | reservation + approval untuk uang (ADR-0002) |
| `close_booking` | `COMPLETED` → `CLOSED` | sistem, **hanya bila** gate §30 lulus penuh |

`prepare_arrival`, `check_in`, dan `complete_stay` adalah perintah yang **wajib ada**
supaya `close_booking` bisa tercapai: gate §30 menuntut booking sudah `COMPLETED`, jadi
tanpa ketiganya gate itu terimplementasi, teruji, dan **mustahil dibuka**. Ketiganya
idempoten pada status yang sama (perintah yang diulang dari front desk bukan error) dan
menolak lompatan status (`BOOKING_TRANSITION_REFUSED`) — diuji di
`tests/test_booking_stay_progress.py`.

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

Tidak ada risiko okupansi yang menggantung. Risiko "slot dilepas saat pembatalan masih
ditinjau" **ditutup** oleh ADR-0002 Opsi A (diputuskan pemilik sistem 2026-09-21):
`CANCELLATION_REQUESTED`, `CANCELLATION_REVIEW`, dan `DISPUTED` adalah status occupying,
ditegakkan di tiga lapis — pemeriksaan kernel, index parsial SQLite
(`uq_bookings_active_slot`), dan `ex_bookings_no_overlap` di PostgreSQL. Yang tersisa
sebagai **biaya bisnis**, bukan risiko teknis: unit bisa tertahan selama review; itu
dibayar dengan mempercepat keputusan pembatalan.
Lihat `docs/adr/0002-occupancy-during-cancellation-review.md`.

## 7. Cara memverifikasi

```bash
python3 -m pytest tests/test_booking_states.py -q          # 60+ test modul ini
python3 -m pytest tests/ -q                                # seluruh suite
python3 -c "import sys; sys.path.insert(0,'src'); import booking_states as b; \
b.verify_against_schema('sqlite'); b.verify_against_schema('pg'); print('OK')"
python3 scripts/generate_pg_twin.py --check                # twin SQLite ⇄ PostgreSQL
```
