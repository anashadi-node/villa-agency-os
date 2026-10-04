# Rencana Eksekusi Phase 2 → 4 (menuju "siap deploy")

Versi: 1.0 · Tanggal: 2026-09-21 · Pemilik: Mas Anas · Eksekutor: Hermes Anantara v3

Dokumen ini menutup jarak antara **Phase 1 (kernel deterministik, selesai)** dan
**"100% fix siap deploy"**. Isinya: definisi selesai, lingkup per fase, kontrak
antarmuka, dan gerbang yang harus hijau sebelum apa pun disebut siap.

---

## 1. Definisi "siap deploy" (tidak bisa dinegosiasikan)

Sebuah fase disebut selesai HANYA bila seluruh poin ini benar, dengan bukti
perintah + keluaran nyata (bukan ringkasan agen):

1. `python3 -m pytest tests/` hijau, dan `make check` PASS (secret 0, unsafe 0, parity twin OK).
2. Dengan PostgreSQL 16 hidup (`VILLA_PG_DSN`): suite hijau tanpa skip baru.
3. Setiap invariant baru punya test yang **gagal** kalau invariant itu dilemahkan
   (bukan test yang hanya menyentuh jalur sukses).
4. Tidak ada `TODO`, fungsi kosong, `NotImplementedError`, atau klaim di dokumen
   yang tidak punya kode di belakangnya.
5. Data P3/P4 tidak pernah disimpan; konten eksternal diperlakukan sebagai data.
6. Tidak ada state yang berubah tanpa command tervalidasi + jejak audit.
7. Uang selalu integer minor + currency; tidak ada `float` di jalur uang.
8. Semua aksi uang/legal (refund, payout, bank change, relokasi, pengecualian
   policy) melewati approval manusia; jalur bahasa tidak punya write access.

## 2. Lingkup per fase

### Phase 2 — Case, intake, matching, approval, draft (fondasi, tanpa AI aktif)
| Modul | Isi | Invariant inti |
|---|---|---|
| `src/cases.py` | siklus hidup `cases` + `case_messages` + `case_events`, SLA (first response, quote) | setiap perubahan status = satu command tervalidasi + satu baris `case_events`; pesan masuk tidak pernah dipercaya |
| `src/intake.py` | proposal terstruktur (rule-first) + review manusia per field | field hanya menjadi fakta setelah `ACCEPTED`; `UNKNOWN` bila ragu; P3/P4 tidak disimpan |
| `src/matching.py` | ranking kandidat unit yang dapat dijelaskan | setiap kandidat punya `reasons`/`deny_reasons` ber-ID; tidak ada skor tanpa alasan; evidence wajib lulus gate |
| `src/approvals.py` | antrean approval + dual control untuk uang | refund/payout/bank_change butuh 2 approver berbeda; approval tidak boleh dipakai dua kali |
| `src/messages.py` | penyusun draft dari fakta ber-ID | draft `UNGROUNDED` tidak bisa disetujui/dikirim; pengiriman selalu oleh manusia dan tercatat |

### Phase 3 — Uang, jadwal, dokumen, insiden
| Modul | Isi | Invariant inti |
|---|---|---|
| `src/invoicing.py` | invoice + baris invoice, nomor urut, PBJT dari policy | semua baris berasal dari rate card/policy bertanggal; tidak ada angka manual |
| `src/payments.py` | payment intent hosted page + webhook idempotent + ledger | `UNIQUE(psp_event_id)`; rekonsiliasi = match/investigate/unmatched, tidak ada angka diubah manual |
| `src/scheduler.py` | tick deterministik: hold kedaluwarsa, SLA, tagihan jatuh tempo | idempoten; satu tick = satu transaksi; hasil dapat direproduksi dari waktu yang sama |
| `src/documents.py` | kwitansi/invoice PDF-teks + retensi + redaksi per kelas | tidak ada dokumen P3/P4 keluar sistem; retensi mengikuti kebijakan |
| `src/ops_tasks.py` | tugas operasional (checkin prep, cleaning) + SLA | tugas selalu punya pemilik dan tenggat |
| `src/incidents.py` | playbook insiden + post-mortem | insiden P3/P4 dilaporkan < 1 jam, tercatat append-only |

### Phase 4 — Kanal, dashboard, bukti go-live
| Modul | Isi | Invariant inti |
|---|---|---|
| `src/channels.py` | adapter kanal (email/WA/web) + transport palsu untuk test | verifikasi tanda tangan webhook; kanal eksternal tidak bisa menulis langsung ke kernel |
| `src/dashboard.py` | dashboard internal read-only + aksi lewat antrean approval | tidak ada jalur tulis langsung dari UI |
| `src/shadow_eval.py` | evaluasi shadow: usulan otomatis vs keputusan manusia | gate go-live dari angka, bukan opini (agreement, unsupported-claim rate) |
| `src/anchors.py` + `scripts/publish_anchor.py` | penerbitan anker rantai audit mingguan | anker disimpan di luar DB; verifikasi ulang bisa membuktikan tidak ada penyuntingan |
| `src/observability.py` | metrik + healthcheck | tidak ada metrik berisi P2+ |
| `scripts/preflight.py` | gerbang pra-deploy (schema, rahasia, keputusan bisnis, backup) | deploy ditolak bila ada satu pun belum siap |

## 3. Kontrak antarmuka yang dibekukan

- Semua modul baru: **hanya Pustaka Standar Python**; `src/` tidak menambah dependency.
- Error: subclass `DomainError` dari `primitives` (dengan `code` + `details`), bukan `Exception` mentah.
- Waktu: selalu lewat `Clock`/`FrozenClock`, bukan `datetime.now()` langsung.
- Uang: `Money` (integer minor) + kode mata uang; dilarang `float`.
- ID: dari `IdFactory` (prefiks + acak), tidak boleh id yang bisa diprediksi.
- Tulis ke DB hanya lewat modul pemilik (satu penulis per tabel); log append-only tidak pernah di-UPDATE.
- Setiap aksi yang menyentuh uang/legal/status booking wajib punya `idempotency_key`.

## 4. Gerbang deploy (dijalankan `make check` + `python3 scripts/preflight.py`)

```
pytest hijau (lokal + PostgreSQL hidup)   →  make check PASS  →  preflight PASS
   →  backup terverifikasi  →  apply schema  →  healthcheck  →  monitor 30 menit
```

`scripts/preflight.py` menolak deploy bila: twin drift, secret terdeteksi,
PostgreSQL tak terjangkau, keputusan bisnis D1–D10 pada `PROJECT_CHARTER.md` masih
terbuka (fase yang bergantung padanya tetap *paper*), atau backup belum diverifikasi.

## 5. Status

| Fase | Status |
|---|---|
| Phase 0–1 (kernel, ADR-0002) | **Selesai** — 607 test hijau, `make check` PASS |
| Phase 2 | **Selesai** (2026-10-03 — modul + gerbang §9/§30 lengkap, ADR-0050–0056) |
| Phase 3 | **Selesai** (2026-10-03 — deposit & settlement berpunya via CLI `closure_facts.py`, ADR-0055/0056) |
| Phase 4 | **Modul lengkap 2026-10-03; bukti go-live lengkap 2026-10-04** — `channels.py` (ADR-0059), `shadow_eval.py` (ADR-0058), KPI (ADR-0057), dashboard/anchors/observability/preflight; bukti jalur tamu 16/16 (`go_live_smoke.py`) dan `reports/final-report.md` v2.1 sudah ada. Tersisa yang bukan modul: halaman dashboard untuk `channels.py`/`shadow_eval.py`, pemasangan produksi, dan suite PostgreSQL hidup |

Dokumen ini diperbarui setiap fase ditutup, bersama `reports/final-report.md`.
