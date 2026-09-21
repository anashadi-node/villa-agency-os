# ADR-0002 — Okupansi saat Pembatalan Ditinjau dan Sengketa

- Status: **Proposed** (menunggu keputusan pemilik sistem)
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

## Dampak implementasi (jika A disetujui)

1. `src/schema/001_initial.sql` — predikat index 5 → 8 status.
2. `python3 scripts/generate_pg_twin.py --write` (regenerasi twin) lalu `--check` hijau.
3. `src/booking_states.py` — `OCCUPYING_STATES` 5 → 8 (diverifikasi otomatis terhadap DDL).
4. `src/reservation_kernel.py` — `OCCUPYING_BOOKING_STATES` 5 → 8.
5. Test: `tests/test_booking_states.py` (literal predikat, `len`), `tests/test_schema.py`
   (parity), test kernel yang menyebut lima status.
6. Dokumen: `docs/BOOKING_STATE_MACHINE.md` §1 dan dokumen ini (status → Accepted).

## Keputusan

Belum diputuskan. Perilaku sekarang dikunci oleh
`tests/test_booking_states.py::TestTransitionGraph::test_mid_stay_dispute_leaves_the_occupying_set`
agar perubahan apa pun harus lewat ADR ini.
