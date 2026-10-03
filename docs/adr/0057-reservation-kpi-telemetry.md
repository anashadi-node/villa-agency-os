# ADR-0057 — Telemetri KPI Agent 3: hitungan atas fakta tercatat, tanpa data person

- Status: Accepted
- Date: 2026-10-03
- Deciders: platform + Mas Anas (menutup item terakhir Agent 3 — mengikuti dokumen 9)
Bagian dari: dokumen 9 (Agent 3 — AI Reservation Service), charter §5 (Metrik sukses), §22 KPI

## Konteks

Row 9 dokumen AI Workforce menyisakan dua item: CLI operator (✅ 2026-10-03,
`closure_facts.py`) dan telemetri KPI. Charter §5 sudah menamai metrik yang ingin dipantau
(double booking 0, first response, funnel inquiry→quote→hold→paid→confirmed), tetapi tidak ada
modul yang menghitungnya — pertanyaan "apakah funnel ini sehat?" hanya bisa dijawab dengan query
ad-hoc, yang tidak terevisi dan tidak bisa diuji.

## Keputusan

1. **`src/reservation_kpi.py`** — satu fungsi `collect(conn, *, agency_id, window_days=28, at=None)`
   menghasilkan `ReservationKpis` (dataclass, 4 bagian):
   - **funnel** — inquiries (cases), quotes_sent (SENT+ACCEPTED), quotes_accepted,
     holds_confirmed (CONFIRMED+CONVERTED), holds_converted, bookings_confirmed,
     payments_succeeded — setiap tahap dibaca di tabel **pemiliknya**.
   - **rates** — acceptance/conversion sebagai rasio; denominator kosong = `None`, bukan 0
     (rasio dari funnel kosong adalah kebohongan yang tampak presisi).
   - **sla** — `first_response_breached` dan `quote_sla_breached`: dijawab lewat tenggat, atau
     masih diam setelah tenggat lewat — dua bentuk pelanggaran yang sama-sama dihitung.
   - **governance** — conversations, escalated, handed_off dari **ledger §13 sendiri**
     (`ai_conversation_events`), plus `overlapping_bookings` sebagai detektor pasif:
     constraint kernel mencegahnya, tetapi jika suatu hari ia bernilai > 0, itu bukan
     artefak query — itu insiden.
2. **Tanpa P2+**: bundle hanya berisi integer dan rasio. `guest_ref`, teks pesan, nama,
   dan identitas tidak pernah masuk query SELECT maupun keluaran — tes mengunci ini.
3. **CLI** `scripts/reservation_kpi.py --days N --agency A`: baca-saja, exit 0/2, keluaran
   tanpa data person (4 tes proses-terpisah).

## Alternatif ditolak

| Alternatif | Kenapa tidak |
|---|---|
| Menyimpan KPI ke tabel per hari | KPI adalah fungsi dari fakta yang sudah append-only; menyimpan salinannya menciptakan dua sumber kebenaran yang bisa berbeda |
| Memasukkan teks/mood percakapan | Melanggar §8/§56 (audit tidak membawa kata tamu) dan tidak diperlukan untuk metrik operasional |
| Rate = 0 saat funnel kosong | Mengubah "belum ada data" menjadi "performa buruk" — keputusan salah lahir dari kebohongan kecil |

## Verifikasi

- `tests/test_reservation_kpi.py` — 10 tes: funnel per tahap di meja pemiliknya, rasio
  dengan denominator kosong = None, jendela waktu nyata dan bisa digeser, SLA dua bentuk,
  governance dari ledger, detektor overlap, idempoten baca-ulang, dan bundle tanpa P2+.
- `tests/test_reservation_kpi_cli.py` — 4 tes proses-terpisah: keluaran pada DB sungguhan,
  DB hilang = exit 2, window 0 = exit 2, dan tidak ada `guest_ref`/id case di keluaran.
- Diverifikasi end-to-end di DB sungguhan: 4 inquiry, 2 quotes sent, 1 accepted, 2 holds
  confirmed, 1 converted, 1 booking, 1 payment — rasio dan SLA tercetak, overlap 0.