# ADR-0058 — Pengukuran shadow-mode: bar D7 jadi policy key, modul hanya mengukur

- Status: Accepted
- Date: 2026-10-03
- Deciders: platform + Mas Anas (implementasi D7/ADR-0038 — item Phase 4 pertama)
Bagian dari: dokumen shadow-mode (ADR-0038), `src/shadow_eval.py`, `src/schema/015`

## Konteks

ADR-0038 memutuskan angkanya (agreement ≥ 95%, klaim tanpa bukti = 0, volume ≥ 100 kasus,
rolling per agent+worker) tetapi tidak ada modul yang mengukurnya. Estate sudah punya kanal
bayangan bawaan — `intake_proposals.method='llm_shadow'` dengan `extractor` dan `source_span`
per field — dan gerbang grounding di `message_drafts`.

## Keputusan

1. **Pengukuran membaca kanal bayangan itu saja.** `rule`/`manual` bukan objek ukuran.
   Agreement = field `ACCEPTED` atas field diputuskan, per `extractor` — tidak ada rata-rata
   platform yang bisa menyembunyikan satu worker yang terus keliru.
2. **Klaim tanpa bukti = proposal tanpa `source_span`** — teks yang mengklaim fakta tanpa
   jangkar ke pesan asalnya. Satu klaim begitu menolak bar, bahkan di agreement 100%
   (peringkat ADR-0038 disengaja).
3. **Bar jadi policy key dengan baseline platform**: `governance.shadow.min_agreement_rate`
   (PCT 95) dan `governance.shadow.min_cases` (COUNT 100), baseline terbit `2026-10-02`
   (D7). Tenant boleh lebih **ketat** saja — revisi lebih longgar ditolak
   (`SHADOW_BAR_LOOSER_THAN_BASELINE`), dibaca pada instan yang sama dengan pengukuran.
4. **`meets_bar` adalah data, bukan keputusan.** Modul menulis nol baris; kelulusan tetap
   approval manusia (ADR-0038), dan invariant "klaim tanpa grounding sampai ke tamu = 0"
   dihitung dari `message_drafts` (gerbang skema menolaknya; angka 0 diukur, bukan diasumsikan).

## Alternatif ditolak

| Alternatif | Kenapa tidak |
|---|---|
| Tabel KPI persisten | Salinan kedua dari fakta append-only yang sudah ada; melenceng saat refresh |
| Bar hard-code konstanta | ADR-0038: perubahan bar harus revisi berdokumen, bukan edit sunyi |
| Bar per-tenant bebas (boleh longgar) | Meniadakan fungsi bar; baseline = lantai platform |
| Mengukur semua proposal (rule+manual) | Bukan agen yang sedang dinilai |

## Verifikasi

- `tests/test_shadow_eval.py` — 12 tes: hanya kanal shadow, per-extractor, span kosong =
  klaim tanpa bukti, window nyata, baseline terpin ke ADR-0038, bar ketat mengikat, bar
  longgar ditolak, modul menulis nol baris.
- Baseline policy + fingerprint diregenerasi lewat `PolicyResolver.publish` (bukan hash tangan);
  migrasi 015 idempoten untuk DB lama.