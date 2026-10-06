# Riset: Arsitektur 6 Agen AI Villa Agency OS × Skills Pendukung

**Tanggal:** 6 Oktober 2026 · **Metode:** audit kode langsung (40+ pemeriksaan) × dokumen AI Workforce V1.1 × inventaris 177 skills Hermes · **Protokol:** falsification-first, triangulasi, rule 11 riset-penelitian ("ukur cakupan sebelum membangun baru")

---

## 1. Ringkasan Eksekutif

**Temuan utama — hipotesis Anda BENAR sebagian besar:** fondasi worker yang dibutuhkan 6 agen **sudah ada di repo** (7 dari 10 keluarga worker), dan **≥10 skills Hermes sudah ada** yang protokolnya bisa dipakai langsung. Yang benar-benar belum ada hanya **4 gap substantif** — bukan 47 worker dari nol seperti yang disirat status lama.

| Agen | Cakupan fondasi | Gap substantif |
|---|---|---|
| 1. Control Tower | ~60% (core routing/health/retry/escalation baru dibangun) | Budget monitor (W10), SLA sweep wiring |
| 2. Commercial AI | **~15%** — paling tipis | Hampir semua (11 worker) |
| 3. Reservation Service | **100%** — 11/11 worker ada | — (selesai) |
| 4. Operations AI | ~55% | Doc-completeness, readiness sweep, SOP store |
| 5. Finance Admin AI | **~80%** — fondasi terberat sudah ada | AR/AP aging, due monitor, exposure report |
| 6. Revenue & BI | ~45% | Forecast, anomaly detection, executive report |

**Sumber bukti:** `src/` (50.105 baris), 3.598 tes hijau, tabel `payments/invoices/reconciliation_cases/supplier_settlements/deposit_resolutions` terverifikasi hidup di `data/villa.db` (skema v016 setelah 5 migrasi tertunda diterapkan hari ini).

---

## 2. Bukti Cakupan per Agen (Apa yang SUDAH Ada)

### Agen 3 — Reservation: SELESAI (11/11 worker)
`reservation_workers.py` mendefinisikan RES-W01 s.d. RES-W11 semua (verifikasi regex): inquiry extraction, matching, availability, pricing, quotation, follow-up, hold, booking status, amend/cancel, pre-arrival, human handoff. 136+ tes khusus. Status dokumen lama "W11 belum" sudah **basi** — W11 (`hand_off`) ada di baris 1145, teruji di `test_reservation_workers.py:1659`.

### Agen 5 — Finance Admin: fondasi terberat sudah ada (~80%)
| Worker | Yang sudah ada di repo |
|---|---|
| W04 Recon Assistance | `payments.py`: 4 kind rekonsiliasi (`UNMATCHED_PAYMENT`, `AMOUNT_MISMATCH`, `DUPLICATE_EVENT`, `REFUND_WITHOUT_PAYMENT`), SLA recon, `unmatched()` |
| W08 Unallocated Receipt | `unmatched()` + tabel `reconciliation_cases` (owner + due_at) |
| W09 Duplicate Detection | webhook idempoten (`handle_webhook`, `duplicate=True` di `PaymentEvent`) |
| W05 Invoice Validation | `invoicing.py` `derive_totals` + `check_lines_against_totals`, 141 tes |
| W06 Settlement Calc | `supplier_settlements.py` DUE/PAID dengan guard overpay (ADR-0056) |
| W11 Cash/Exposure | `ledger_balance()` + `verify_ledger()` (chain append-only) |
| Deposit | `deposit_resolutions.py` (ADR-0055) |
| Fx | `fx.py` — inferensi mata uang guest (ADR-0060) |

**Yang belum:** W01 AR aging (bucket 30/60/90), W02 AP aging, W07 due monitor (kolom `due_at` ada di `invoices`, tidak ada sweep), W03 payment matching lanjutan (partial/over sudah di recon), W10 financial exception report (kompilasi).

### Agen 1 — Control Tower: core baru selesai, 4 worker sisanya sebagian sudah ada infra
- W01/W03 Router+Dispatcher: ✅ baru (`control_tower.py`, vocabulary tertutup §2A.3, outbox idempoten)
- W05/W06 Exception+Escalation: ✅ baru (lewat exception engine yang sama)
- W08 Retry: ✅ baru (pakai `publish_due` outbox; dead letter hanya bisa dibangunkan `requeue()` manusia)
- W04 SLA Monitor: **scheduler.py sudah punya** sweep `case_sla` + `ops_sla` — tinggal wiring, bukan bangun
- W07 Agent Health: **observability.py `health_checks()` sudah ada** ("UNKNOWN is never OK")
- W09 Notification: **messages.py (katalog template) + connectors.py (outbound transport) sudah ada**
- W10 Cost/Token Budget: **BELUM ADA** — tidak ada penghitung token, tidak ada policy key budget
- W02 Workflow Coordinator: parsial (correlation_id ada di outbox; tidak ada peta workflow durable)

### Agen 4 — Operations: ~55%
Ada: `ops_tasks.py` (8 kind tugas + SLA + assignment), scheduler sweeps, `incidents.py` (playbook + severity), `documents.py` (penyimpanan + format), `scan_untrusted` (comm QC), gates komersial/inventaris/finansial per-booking di reservation_workers.
Belum: W03 doc-completeness checker (tidak ada pemeriksa dokumen-wajib per booking), W04 booking QC sweep, W05 pre-arrival readiness sweep (RES-W10 ada per-konversasi, tidak ada sweep armada), W10 SOP/process compliance store.

### Agen 2 — Commercial: paling tipis (~15%)
Ada: `crm.py` (profil guest + requirements ter-tipe), `matching.py` (skoring VILLA — bukan lead; eligibility-first + score dengan alasan — pola bagus untuk disalin ke lead scoring), `reservation_kpi.py` (funnel inquiry→quote→hold→booking = konversi).
Belum: lead scoring, segmentasi, campaign analysis (konsep "campaign" **nol** di repo), content drafting, SEO, social, nurture, B2B research, abandoned-inquiry recovery.

### Agen 6 — Revenue & BI: ~45%
Ada: `reservation_kpi.py` (konversi + funnel, read-only idempoten), data pendukung lengkap di tabel (`bookings.confirmed_at/cancelled_at/total_minor/currency`, `cases.channel`, `rate_versions`, `supplier_settlements`), `shadow_eval.py` (pola KPI ber-bar dengan floor tenant).
Belum: W04 demand intel, W09 forecasting, W10 margin analysis (kolom payout owner tidak ada di `quotes` — margin hanya bisa dihitung dari settlements vs invoice), W11 anomaly detection, W12 executive report.

---

## 3. Skills Hermes yang Langsung Menunjang (Jawaban Pertanyaan Anda)

**Ya, ada — 12 skills relevan.** Tapi jujur soal batasnya: skills memberi **protokol kerja & standar analisis** untuk agen riset/BI/finans/ops saat *Hermes mengoperasikan* agen-agen itu; implementasi worker itu sendiri tetap kode deterministik di repo (aturan dokumen §20.2 — deterministic code untuk otorisasi, aritmetika, invariants; LLM hanya bahasa).

| Skill | Menunjang agen | Cara pakai konkret |
|---|---|---|
| `riset-penelitian` (always-on) | SEMUA, esp. 6 | Falsification-first + rule 11 (cek cakupan dulu) + rule 10 (oracle sebelum model) |
| `sodex-venue-ledger-reconcile` | 5 | Pola rekonsiliasi ledger-vs-venue: PnL wallet vs buku — langsung transferable ke FIN-W04 |
| `trading-system-audit` | 5, 6 | Pola audit forensik lintas-sumber (cross-DB PnL) → audit invoice vs payment vs ledger |
| `quantitative-trading-analytics` | 6 | Bootstrap CI95, metrik terukur → interval keyakinan forecast RBI-W09 |
| `causal-inference-analysis` | 2, 6 | DiD/IV untuk klaim atribusi kampanye — mencegah "claim attribution unsupported by data" (larangan §8) |
| `data-triangulation-verification` | 5, 6 | Enforce source-of-truth matrix §15: ≥2 sumber independen |
| `grounded-citations` | 6 | Setiap angka executive report wajib bawa lineage (RBI-W12) |
| `meta-analysis-synthesis` | 6 | Pigi-pool hasil kampanye/season untuk demand intel |
| `advanced-root-cause-analysis` + `structured-problem-solving` + `bottleneck-analysis` | 1, 4 | Playbook eskalasi: incident → RCA KT IS/IS-NOT, A3 untuk SLA bolong berulang |
| `anantara-master-evidence-framework` | SEMUA | Standar bukti (n, p-value, OOS) sebelum klaim "worker ini efektif" |
| `claude-code` / `codex` / `opencode` | build | Delegasi implementasi paralel antar-worker dengan review |
| `xlsx` / `pdf` / `html-to-pdf` | 6, 12 | Format executive report yang dibaca GM/Owner |

**Yang TIDAK ada dan memang tidak seharusnya sebagai skill:** worker domain villa (matching villa, hold, recon invoice). Itu kode repo — stdlib-only, fail-closed, ter-audit — bukan skill eksternal.

---

## 4. Gap Substantif (Yang Benar-Benar Harus Dibangun) — Prioritas

1. **CT-W10 Budget Monitor** — tanpa ini, kill-switch §17 punya gelar tapi tidak ada biaya yang dipantau. Butuh: policy key `ai.budget.token_daily` + tabel kecil penggunaan + sweep. (Kecil, ~1 hari)
2. **FIN AR/AP aging + due sweep** — data sudah ada (`invoices.due_at`, `supplier_settlements`), tinggal agregasi baca-saja + wiring scheduler. (Kecil, ~1 hari)
3. **OPS doc-completeness + readiness sweep** — perlu definisi dokumen-wajib per booking (policy matrix sudah menunjuk `check_in_document_required`). (Sedang, ~2 hari)
4. **RBI forecast + anomaly + executive report** — `reservation_kpi.collect()` jadi basis; anomaly = statistik sederhana dulu (z-score rolling), bukan ML. (Sedang-besar, ~3-4 hari)
5. **Commercial AI utuh** — 11 worker; mulai dari lead scoring (salin pola eligibility-first dari `matching.py`) + abandoned-inquiry recovery (query case terbuka tanpa aktivitas). (Besar, bertahap)
6. **KPI store §18** (`kpi_definition` versioned) — prasyarat sebelum target KPI dipakai sebagai kebenaran operasional. (Sedang)

## 5. Temuan Audit terhadap Pekerjaan Saya Sendiri (Falsifikasi)

Falsifikasi menemukan **2 cacat di core Control Tower yang baru dibangun** (commit `e819783`):
1. **Tool CT belum lolos gerbang:** `workflow.status`/`workflow.retry_safe` terdaftar di `ai_tools`, tetapi TIDAK ada di `TOOL_EFFECTS` gateway → `effect_of()` akan menolak dengan `AI_TOOL_EFFECT_UNKNOWN`. Route() juga belum meminta pintu gateway (langsung `outbox.emit`). Perlu ADR-0061: daftarkan efek READ/WRITE + panggil pintu di setiap aksi tower.
2. **5 migrasi tertunda** (`012`–`016`) ternyata belum diterapkan ke `data/villa.db` — dashboard yang jalan tadi melayani skema lama. **Sudah diterapkan hari ini** (v016), service perlu restart untuk memakai skema baru.

## 6. Rekomendasi Urutan Kerja (Falsifiable)

**Hipotesis kerja:** "Dengan fondasi yang ada, 5 agen tersisa mencapai core-to-done dalam urutan FIN → OPS → RBI → CT-lengkap → COM, masing-masing lulus Definition of Done §22." **Cara verifikasi:** setiap agen keluar hanya jika: tool allowlist server-side ✅, handoff tested ✅, degraded/suspend per-agen ✅, observability ✅, KPI telemetry sebelum target.

Langkah berikutnya yang disarankan: **FIN dulu** (fondasi 80%, dampak langsung ke uang, risiko paling terkendali karena zero money authority), sambil ADR-0061 memperbaiki gerbang CT.

*Keyakinan: tinggi untuk inventaris repo (diverifikasi langsung ke kode + DB); sedang untuk estimasi effort (belum ada data kecepatan historis).*
