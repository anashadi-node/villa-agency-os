# ADR-0002 — Okupansi saat Pembatalan Ditinjau dan Sengketa

- Status: **Accepted** — Opsi A, diputuskan pemilik sistem 2026-09-21
- Tanggal: 2026-09-21
- Konteks kode: `src/schema/001_initial.sql` → index parsial `uq_bookings_active_slot`;
  `src/booking_states.py` → `OCCUPYING_STATES`, `_TRANSITION_TABLE`;
  `src/reservation_kernel.py` → `OCCUPYING_BOOKING_STATES`

## Masalah

Slot unit dilepas (bisa dijual ulang) hanya ketika status booking bukan salah satu dari
lima status occupying: `CONFIRMED`, `BALANCE_DUE`, `BALANCE_OVERDUE`, `PRE_ARRIVAL`,
`IN_STAY`.

Graf transisi mengizinkan perpindahan **keluar** dari status occupying ke status
**non**-occupying sebelum komitmen diselesaikan:

| Sisi | Arti di lapangan |
|---|---|
| `CONFIRMED/BALANCE_DUE/BALANCE_OVERDUE/PRE_ARRIVAL/IN_STAY → CANCELLATION_REQUESTED` | tamu minta batal; keputusan belum diambil |
| `CANCELLATION_REQUESTED → CANCELLATION_REVIEW` | review internal, bisa **ditolak** (`CANCELLATION_REVIEW → CONFIRMED`) |
| `IN_STAY → DISPUTED` | sengketa saat tamu masih **fisik** di unit |

Pada ketiga keadaan itu unit masih terikat (atau masih dihuni), tetapi index dan
`reservation_kernel` sudah berhenti menghitungnya sebagai occupying. Akibatnya booking
baru untuk unit + tanggal yang sama **lolos** pemeriksaan tumpang tindih → risiko
double booking, yaitu pelanggaran invariant #1 divisi ini (0 double booking).

## Opsi

**A. Perluas predikat occupying** — tambahkan `CANCELLATION_REQUESTED`,
`CANCELLATION_REVIEW`, `DISPUTED` ke `uq_bookings_active_slot` (8 status),
`OCCUPYING_STATES`, dan `OCCUPYING_BOOKING_STATES`.
Fail-closed: tanggal tetap terkunci sampai pembatalan **diputuskan** (`CANCELLED`) atau
sengketa selesai. Biaya: unit tertahan lebih lama saat review → potensi kehilangan
penjualan. Untuk mengejar penjualan, pembatalan cukup **diputuskan lebih cepat**, bukan
slotnya dibuka sebelum diputuskan.

**B. Perkecil graf** — larang perpindahan dari status occupying ke `DISPUTED`,
`CANCELLATION_REQUESTED`, `CANCELLATION_REVIEW` tanpa syarat.
Konsekuensi: kebutuhan operasional nyata (tamu minta batal, sengketa saat inap) menjadi
tidak terekam sebagai status, sehingga justru mendorong pencatatan di luar sistem.

**C. Biarkan seperti sekarang** — terima risiko, andalkan SOP bahwa manager tidak
menjual tanggal yang sedang direview.
Tidak ada penegakan teknis; kesalahan manusia = double booking.

## Rekomendasi

**Opsi A.** Alasan: (1) invariant #1 divisi adalah 0 double booking, dan A menutupnya
secara struktural, bukan lewat SOP; (2) A hanya menukar risiko *uang* (unit tertahan)
dengan risiko *kehilangan tamu* (double booking) — untuk bisnis berbasis reputasi,
yang kedua jauh lebih mahal; (3) perubahan bersifat menambah proteksi, sehingga tidak
ada data lama yang menjadi tidak valid, dan pemulihannya (revert) hanya satu predikat.

## Dampak implementasi (Opsi A — sudah diterapkan 2026-09-21)

| # | Berkas | Perubahan |
|---|---|---|
| 1 | `src/schema/001_initial.sql` | predikat `uq_bookings_active_slot` 5 → 8 status + komentar ADR |
| 2 | `src/schema/001_initial.pg.sql` | regenerasi twin (`generate_pg_twin.py --write`), `--check` hijau |
| 3 | `src/schema/002_pg_range_exclusions.pg.sql` | `ex_bookings_no_overlap` (EXCLUDE) 5 → 8 status |
| 4 | `src/booking_states.py` | `OCCUPYING_STATES` 5 → 8 (tetap diverifikasi otomatis terhadap DDL) |
| 5 | `src/reservation_kernel.py` | `OCCUPYING_BOOKING_STATES` 5 → 8 |
| 6 | `docs/BOOKING_STATE_MACHINE.md`, `docs/DATA_MODEL.md` | 8 status; literatur predikat disamakan huruf besar dengan DDL |

Test yang menjaga keputusan ini (semuanya lulus, termasuk di PostgreSQL hidup):

| Test | Lapis penegakan yang dibuktikan |
|---|---|
| `test_booking_states.py::TestTransitionGraph::test_cancellation_review_and_dispute_hold_the_slot` | himpunan `OCCUPYING_STATES` (menggantikan test karakterisasi) |
| `test_booking_states.py::TestAgainstSqlite::test_review_and_dispute_bindings_are_refused_by_the_database` | index parsial SQLite: re-sale ditolak |
| `test_booking_states.py::TestAgainstSqlite::test_a_decided_cancellation_does_release_the_slot` | `CANCELLED` benar-benar melepas tanggal |
| `test_reservation_kernel.py::TestNoDoubleBooking::test_cancellation_under_review_still_blocks_a_new_hold` | pemeriksaan okupansi kernel (hold baru ditolak) |
| `test_schema.py::TestLivePostgres::test_booking_exclusion_holds_the_dates_during_review_and_dispute` | `ex_bookings_no_overlap` di PostgreSQL 16 hidup |

## Keputusan

**Opsi A diterima** oleh pemilik sistem pada 2026-09-21. Tanggal + unit tetap terkunci
sampai pembatalan **diputuskan** (`CANCELLATION_REQUESTED`/`CANCELLATION_REVIEW` →
`CANCELLED`) atau sengketa ditutup (`DISPUTED` → `COMPLETED`/`CLOSED`).

Konsekuensi yang diterima sadar: unit bisa tertahan selama review berlangsung — biaya
tersebut dibayar dengan **mempercepat keputusan pembatalan**, bukan dengan membuka slot
sebelum ada keputusan. Untuk membatalkan keputusan ini, cukup kembalikan predikat 8 → 5 di
tiga tempat (berkas 1, 3, 4, 5) dan hapus lima test di atas; tidak ada data lama yang
menjadi tidak valid.
