# ADR-0053 — RES-W11 handoff: satu gestur, dua rung, tanpa pintu §56

- Status: Accepted
- Date: 2026-10-03
- Deciders: platform + Mas Anas (job §9 ke-11; katup aman tanpa pintu §56)
Bagian dari: dokumen 9 (Agent 3 — AI Reservation Service), §13, §56

## Konteks

§9 punya sebelas job untuk Agent 3; sepuluh pertama sudah jadi worker
(RES-W01…W10). Yang tersisa adalah **handoff**: jalan keluar AI dari sebuah
percakapan. Ladder §13 sudah ada di `reservation_service` (8 rung, kosakata
triger tertutup 12 kode, paket handoff berdigest) — yang belum ada adalah
**worker** yang memanggilnya dari sisi AI, sehingga job §9 yang ke-11 belum
punya pemilik di katalog worker.

## Keputusan

1. **Dua rung, satu gestur.** Dari `AI_RUNNING` ladder menuntut dua rung:
   `ESCALATED` lalu `HANDOFF_REQUESTED`. Worker yang hanya menawarkan rung
   kedua akan ditolak setiap kali, jadi `hand_off()` adalah satu metode yang
   menempuh keduanya lewat modul pemilik ladder — worker menulis nol baris.
2. **Triger wajib dari AI yang sedang jalan, terkunci setelah itu.** Dari
   `AI_RUNNING`, handoff tanpa triger ditolak (`MissingRequirements`) sebelum
   rung apa pun terekam — kosakata tertutup itulah yang membuat handoff bisa
   dihitung dan di-route. Dari `ESCALATION_TRIGGERED`, handoff menyelesaikan
   rung kedua tanpa triger baru; triger baru ditolak
   (`HANDOFF_TRIGGER_LOCKED`). Handoff kedua ditolak ladder itu sendiri
   (`RungRefused`).
3. **Tanpa pintu §56.** Katalog §56 tertutup dan tidak punya nama untuk
   *berhenti*. Membuat nama baru adalah keputusan owner, bukan keputusan
   kode — dan yang lebih penting: handoff hanya **menyempitkan** wewenang
   mesin. Menutup katup aman di belakang gerbang deny-first (MEDIUM,
   `security.ai.medium_tool_allowance`) berarti tamu bisa terjebak dengan AI
   yang tidak boleh lanjut tapi tidak bisa pergi. Audit tetap ada: rung dan
   alasan ditulis oleh modul pemilik ladder, dengan alasan berprefiks
   `RES-W11:` (kalimat operasional, bukan teks tamu).
4. **Aman bahkan di READ_ONLY.** Karena tidak melewati gateway, §17 tidak
   bisa mematikan katup ini — dan itu disengaja: mode yang menyempit justru
   alasan yang paling sah untuk menyerahkan percakapan ke manusia.

## Alternatif yang ditolak

| Alternatif | Kenapa tidak |
|---|---|
| Menambahkan tool §56 `handoff.transfer` (LOW/MEDIUM) | Katalog tertutup adalah daftar owner; menambah nama tanpa keputusan owner melanggar disiplin yang mengunci daftar itu lewat tes identitas. Dan MEDIUM berarti deny-first — katup aman jadi bisa tertutup. |
| Menggunakan `comm.client_selected`/`comm.supplier_selected` | Itu nama untuk komunikasi AI *keluar* ke klien/supplier; handoff adalah AI *berhenti*. Menyatukan keduanya memberi AI kapabilitas komunikasi baru di bawah sampul "berhenti" — arah yang salah. `comm.supplier_selected` tetap tanpa pemanggil (celah jujur ADR-0023) sampai ada komunikasi supplier sungguhan. |
| Mengizinkan handoff tanpa triger | Alasan bebas tidak bisa dihitung, di-route, atau diuji; kosakata tertutup adalah kontrak §9. |
| Worker menulis rung sendiri ke tabel | Satu penulis per tabel: ladder adalah milik `reservation_service`. Duplikasi jalur tulis = dua tempat invarian bisa diingkari. |

## Konsekuensi

- Katalog worker §9 lengkap: RES-W01…W11 semua punya pemilik.
- §56 tetap jujur: coverage MEDIUM tidak berubah (dua `comm.*` masih tanpa
  pemanggil), dan ADR-0023 tidak perlu direvisi.
- Operator melihat dua event outbox per handoff dari AI yang jalan
  (`reservation.escalated`, `reservation.handoff_requested`) dan dua rung
  audit berprefiks `RES-W11:`.
- Handoff oleh manusia (tanpa triger, dari state apa pun yang diizinkan
  ladder) tetap jalur service langsung — worker ini khusus gestur AI.

## Verifikasi

- `tests/test_reservation_workers.py` — 7 tes baru (klaim 15): dua rung
  terekam + event outbox + prefiks audit; tanpa triger = nol rung dan nol
  event; menyelesaikan thread yang sudah tereskalasi; triger kedua ditolak
  (`HANDOFF_TRIGGER_LOCKED`, tidak ada rung baru); handoff kedua ditolak
  (`RungRefused`); katup terbuka di READ_ONLY; percakapan asing gagal sebagai
  *not found* sebelum rung apa pun.
- Suite worker: 66 tes hijau. Run penuh + `make check` sebelum commit.