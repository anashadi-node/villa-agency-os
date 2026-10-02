# AI Workforce Architecture V1.1 → Traceability ke Villa Agency OS

**Sumber acuan:** `Villa_Agency_AI_Workforce_Architecture_V1_1_Hermes.html` (24 bagian)
**Tanggal periksa:** 2026-09-28 · **Metode:** pemeriksaan langsung kode, skema, tes, gerbang
**Aturan presedensi dari dokumen itu sendiri (§1):** *"If a conflict exists, locked security, privacy, financial-control, business-state and source-of-truth invariants in the Master Specification take precedence."* → **Master Spec menang** kalau bentrok.

## Cara membaca

| Tanda | Arti |
|---|---|
| ✅ **DIIKUTI** | Sudah ada dan berlaku di repo ini, dengan bukti |
| 🟡 **SEBAGIAN** | Sebagian ada / ada padanannya tapi belum lengkap atau belum dipetakan |
| ❌ **BELUM** | Belum ada sama sekali |

**Ringkasan:** dari **32 baris** yang diperiksa — **7 ✅ · 12 🟡 · 13 ❌**.
Semua ❌ terpusat di satu tempat: **lapisan 6 agent AI-nya sendiri**, yang memang belum dimulai.

---

## 1. Peta utama — 24 bagian dokumen

| § | Isi dokumen | Keadaan di repo ini | Bukti | Status |
|---|---|---|---|---|
| **1** | Status, scope, instruksi implementasi: jangan buat agent tambahan; jangan beri LLM kredensial; UI produksi `en-US` | Bahasa `en-US` terkunci; tidak ada agent tambahan yang dibuat; identitas AI punya daftar larangan | ADR-0011 (LOCKED); `src/ai_service.py` (`DENIED_ACTIONS_KEY`); 0 agent dibuat | ✅ |
| **2** | Tata kelola manusia + AI workforce | Organisasi manusia diadopsi (D3 disetujui owner 2026-09-28); **nama orang belum ada** → semua peran `UNASSIGNED` (fail-closed) | `docs/GO_LIVE_DECISIONS.md` D3; `docs/ECOSYSTEM_EXPLAINED.md` Bab 4 | 🟡 |
| **2A** | Peta organisasi AI kanonik (6 komponen dok + **1 tambahan owner 2026-10-02** = 7, `*-Wxx` = worker bukan agent) | Peta tercatat; **0 dari 7 komponen ada di kode** | `grep -rl "control_tower\|reservation_service\|commercial_ai\|operations_ai\|finance_admin\|revenue_bi" src/` → 0 | ❌ |
| **2A.1** | Peta pelaporan (pemilik manusia per komponen) | Butuh peran terisi; sekarang belum ada yang memegang peran | D3 `UNASSIGNED` | 🟡 |
| **2A.2** | Taksonomi worker (capability / execution / deterministic / handoff) | Konsep tercatat; belum ada kode karena agent belum ada | — | ❌ |
| **2A.3** | Batas routing lintas domain | Belum ada | — | ❌ |
| **2A.4** | Handoff antar-agent (tanpa transfer wewenang) | Jalur **domain**-nya ada (pesan + status pembayaran otoritatif); jalur **AI**-nya belum, karena butuh outbox | `src/messages.py`, `src/payments.py`; ADR-0007 *accepted* tapi belum dibangun | 🟡 |
| **2A.5** | Peta konkurensi reservasi (satu penulis inventaris; konteks tamu tidak boleh tercampur) | Prinsipnya **sudah** ditegakkan di domain: satu penulis inventaris, cek tumpang-tindih, isolasi tenant | `src/reservation_kernel.py`; `uq_bookings_active_slot`; tenant-scoping dashboard (commit `8a718f4`) | 🟡 |
| **2A.6** | Infrastruktur non-agent (8 komponen) | **4 ada · 4 belum** | lihat §2 di bawah | 🟡 |
| **2A.7** | Aturan anti-bias untuk implementer | Dipatuhi: audit ini **tidak** membuat agent/peran baru tanpa keputusan owner | dokumen ini + `docs/STATUS_AND_GAP_MAP.md` | ✅ |
| **3** | Hukum arsitektur universal (output LLM bukan otorisasi) | Ditegakkan **secara struktur**: persetujuan butuh aktor manusia; identitas AI punya larangan; tidak ada jalur LLM → otorisasi | `src/approvals.py`; `src/ai_service.py` | 🟡 |
| **4** | Tingkat wewenang L0–L3 | Kode memakai **3 tier** `LOW/MEDIUM/HIGH` dari Master Spec §56 — bukan L0–L3 dokumen ini. **Pemetaannya belum dicatat** | `AI_TIERS` (`src/ai_service.py`); `MEDIUM_ALLOWANCE_KEY` | 🟡 |
| **5** | Agent vs worker vs workflow (workflow harus hidup di luar memori worker) | Belum ada mesin workflow durable; butuh outbox | ADR-0007 belum dibangun | ❌ |
| **6** | Matriks tanggung jawab | Sama dengan 2A.1 | D3 `UNASSIGNED` | 🟡 |
| **7** | Agent 1 — AI Control Tower | **Belum ada** | 0 berkas | ❌ |
| **8** | Agent 2 — Commercial AI | **Belum ada** | 0 berkas | ❌ |
| **9** | Agent 3 — AI Reservation Service | **Belum ada** | 0 berkas | ❌ |
| **10** | Agent 4 — Operations AI | **Belum ada** | 0 berkas | ❌ |
| **11** | Agent 5 — Finance Admin AI | **Belum ada.** Larangan mutlaknya (tidak menyetujui/memindahkan uang) **sudah** ditegakkan di tingkat identitas | `src/ai_service.py` deny registry; `src/approvals.py` | ❌ |
| **12** | Agent 6 — Revenue & BI AI | **Belum ada** | 0 berkas | ❌ |
| **13** | State machine human handoff (AI_RUNNING → … → AI_RESUME_ALLOWED) | **Belum ada.** Yang ada: manajemen kasus + SLA (bukan state machine handoff) | `grep -rn "handoff\|HANDOFF" src/` → **0**; `src/cases.py` | ❌ |
| **14** | Kontrak komunikasi antar-agent (envelope bertipe + outbox) | Envelope **sebagian** ada: `idempotency_key` pada pesan. **Outbox transaksional belum ada** | `src/connectors.py`, `src/messages.py`; tidak ada tabel outbox di `001_initial.sql` | 🟡 |
| **15** | Matriks sumber kebenaran | Sebagian besar **sudah** ditegakkan: fakta villa/tarif dari versi terverifikasi, ketersediaan dari observasi bersumber, dana dari bukti otoritatif | `src/evidence.py`, `src/supplier_route.py`, `src/policy_engine.py`, `src/availability.py` | ✅ |
| **16** | Invariant bisnis kritis | Sebagian besar **sudah**: `HOLD_REQUESTED != HOLD_CONFIRMED`, `AI_EXTRACTED != VERIFIED`, `SOURCE_UNAVAILABLE != NOT_AVAILABLE`, approval terikat fingerprint | `src/hold_states.py`, `src/supplier_route.py`, `src/evidence.py`, `src/approvals.py` | ✅ |
| **17** | Kegagalan, degraded mode, kill switch (5 status) | **3 dari 5**: `ACTIVE`, `SUSPENDED`, `RETIRED`. **Belum**: `DEGRADED`, `READ_ONLY`. Circuit breaker belum ada | `src/ai_service.py` | 🟡 |
| **18** | Tata kelola KPI (definisi berversi: formula, target, ambang, pemilik) | **Belum ada tabel KPI**; hanya `dashboard.py` yang menyebut KPI | `grep -in kpi src/schema/001_initial.sql` → kosong | ❌ |
| **19** | Matriks tes wajib (8 kategori) | Domain **sebagian** ada (functional, finance, audit). **0 berkas** untuk concurrency, security, AI grounding, resilience, human handoff | 66 berkas tes; `ls tests/ \| grep -ci <kategori>` → 0 | 🟡 |
| **20** | Guardrail implementasi (12 butir) | Dipatuhi **berdasarkan konstruksi**: deterministik dulu, tanpa SQL/shell bebas untuk agent, state di luar memori worker, observability ada. **Belum bisa diuji** karena agent belum ada | `src/observability.py`, `src/policy_engine.py`, `src/ai_service.py` | 🟡 |
| **21** | Non-agent eksplisit di V1 (Partnership, GM, Owner, Finance Authority, Legal, Security, HR = MANUSIA) | **Dipatuhi**: tidak ada agent untuk fungsi-fungsi ini | 0 agent dibuat | ✅ |
| **22** | Definition of Done per agent (11 butir) | Belum berlaku — tidak ada agent untuk di-DoD-kan | — | ❌ |
| **23** | Architecture freeze V1 (6 komponen terkunci) | Tercatat dan dipegang sebagai acuan; perubahan butuh ADR + analisis risiko + persetujuan manusia. **Contoh berlakunya aturan ini:** owner menambah komponen ke-7 (Security & Privacy Warden) pada 2026-10-02, dicatat di ADR-0039 dengan matriks wewenang — bukan langsung di kode | dokumen ini; ADR-0032/0033/0034 | ✅ |
| **24** | Dasar desain (NIST AI RMF, OWASP, PCI SSC) | Prinsipnya sudah tercermin: minimisasi data kartu, tokenisasi, pemisahan wewenang, jejak audit | `src/payments.py` (`CARD_DATA_FORBIDDEN`), `scripts/unsafe_config_scan.sh`, ADR-0012 | ✅ |

---

## 2. Rincian §2A.6 — infrastruktur non-agent

| Komponen | Ada? | Bukti |
|---|---|---|
| AI Policy Gateway | ❌ | belum ada (belum ada agent yang perlu dilewatkan) |
| Policy Engine | ✅ | `src/policy_engine.py` (ADR-0017) |
| Approval Engine | ✅ | `src/approvals.py` |
| Exception Engine | ❌ | tidak ada modul |
| Identity / RBAC | ✅ | `src/identity.py` (+ TOTP/MFA) |
| Audit / Observability | ✅ | `src/audit.py`, `src/observability.py`, `src/anchors.py` |
| Outbox / Inbox / Queue | ❌ | ADR-0007 *accepted*, belum dibangun; tidak ada tabel |
| Secrets / DLP / Security | ❌ | baru pemindai konfigurasi (`unsafe_config_scan.sh`) + guard kartu |

**4 ada · 4 belum.**

---

## 3. Peta tujuh komponen → status kode

> Enam komponen pertama datang dari dokumen owner (freeze V1). Komponen **ke-7 (Security & Privacy Warden)** ditambahkan owner pada 2026-10-02 dan dicatat di ADR-0039.

| # | Komponen | Worker (dokumen) | Pemilik manusia | Lapor eksekutif | Kode |
|---|---|---|---|---|---|
| 1 | AI Control Tower | CT-W01…W10 | General Manager | GM; Owner untuk insiden material | ❌ |
| 2 | Commercial AI | COM-W01…W11 | Commercial & Marketing Manager | lewat manager/GM | ❌ |
| 3 | AI Reservation Service | RES-W01…W11 | Operations / Reservation Manager | lewat manager/GM | ❌ |
| 4 | Operations AI | OPS-W01…W10 | Operations Manager | GM/Owner per severity | ❌ |
| 5 | Finance Admin AI | FIN-W01…W11 | Finance & Accounting Manager | **langsung GM + Owner** | ❌ |
| 6 | Revenue & BI AI | RBI-W01…W12 | Revenue Manager / GM | **langsung GM + Owner** | ❌ |

**Catatan penting yang saya pegang dari §2A.7 dan §20.1:** `*-Wxx` **bukan** agent ke-7, ke-8, dst. — ia *kegiatan* milik satu agent induk. Dan jumlah agent **tidak** bertambah saat tamu bertambah; yang bertambah adalah kapasitas worker.

---

## 4. Yang paling menentukan urutan kerja

Tiga temuan di tabel utama yang saling mengunci, dan menentukan kenapa urutannya tidak bisa dibalik:

1. **§14 (envelope + outbox) 🟡** dan **§5 (workflow durable) ❌** — dokumen melarang agent saling memberi wewenang lewat obrolan bebas. Tanpa outbox, handoff antar agent **tidak boleh** dibangun. → **Outbox dulu.**
2. **§2A.6 (AI Policy Gateway) ❌** dan **§17 (kill switch berjenjang) 🟡** — agent hanya boleh lewat gateway, dan harus bisa dimatikan. → **Gateway + kill switch dulu.**
3. **§2/§2A.1 (peran & pemilik) 🟡** — setiap agent punya "pemilik manusia" yang menerima eskalasi. Tanpa peran terisi, tidak ada tujuan eskalasi. → **Peran dulu.**

---

## 5. Cara memverifikasi (bisa Anda ulang sendiri)

```bash
cd ~/villa-agency-os

# 7 komponen AI belum ada
grep -rl "control_tower\|reservation_service\|commercial_ai\|operations_ai\|finance_admin\|revenue_bi" src/   # 0 berkas

# outbox belum ada
grep -in outbox src/schema/001_initial.sql        # kosong

# handoff belum ada
grep -rn "handoff\|HANDOFF" src/                  # kosong

# KPI belum ada
grep -in kpi src/schema/001_initial.sql           # kosong

# tier wewenang yang benar-benar dipakai
grep -n "AI_TIERS\|MEDIUM_ALLOWANCE_KEY" src/ai_service.py

# status kill switch yang benar-benar ada
grep -n "AI_STATUS" src/ai_service.py

# infrastruktur yang ada
ls src/ | grep -iE "policy_engine|approvals|identity|audit|observability|anchors"
```

---

*Dokumen ini hasil pemeriksaan langsung pada 2026-09-28. Kalau ada baris yang Anda ragukan, saya bisa buktikan ulang di depan Anda dengan perintah di §5.*
