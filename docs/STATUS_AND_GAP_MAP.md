# Peta Status & Gap — Villa Agency OS
**Tanggal periksa: 2026-09-28** · Sumber acuan: Master Spec v1.1 (invariant domain) + **AI Workforce Architecture V1.1** (organisasi & lapisan AI) · Bukti: kode, tes, gerbang, preflight

---

## 0. Cara membaca peta ini

Setiap baris di bawah punya **bukti yang bisa diperiksa ulang**, bukan pendapat. Tiga ember:

- ✅ **OK** — sudah jalan, ada bukti
- ⚠️ **BELUM OK** — belum ada / belum lengkap, ini kerjaan
- ❓ **PERLU KEPUTUSAN ANDA** — software tidak boleh menebak

Satu hal yang saya tegaskan di depan, karena ini akar semua yang di bawah: **platform ini hari ini adalah sistem DOMAIN yang kuat, dan lapisan AI WORKFORCE yang masih nol.** Dua hal berbeda, dan peta ini memisahkannya dengan jelas.

---

## 1. Ringkasan satu layar

| Area | Status | Bukti ringkas |
|---|---|---|
| Domain: booking, hold, pembayaran, policy, bukti, identitas | ✅ **kuat** | 40 ADR, 3.117 tes hijau (+3.780 subtes), gerbang PASS, katalog policy 25/25 |
| Lapisan **AI Workforce** (6 komponen dok Anda + 1 tambahan owner 2026-10-02) | ⚠️ **0% dibangun** | 0 dari 7 komponen ada di kode |
| Infrastruktur non-agent | ✅ **6 ada, 1 belum** | Policy/Approval/Identity/Audit ada; **Outbox + Exception Engine ada (2026-10-02)**; sisa: DLP |
| Organisasi & peran (D3) | ✅ **DECIDED, kosakata sudah satu rumah** | ADR-0035 (§5 "3 kosakata hidup bersamaan") direkonsiliasi 2026-10-02 lewat `src/roles.py` + amendment ADR-0035: `approvals.ROLE_RANK` **adalah** `roles.ROLE_RANK`, nama lama tetap resolvable untuk baris historis, dan `gm` masuk tangga (rank 4). **`gm` juga sudah punya halaman** (Command Center/Approvals/Finance/Cases/Properties) dan **wajib MFA** — policy `security.mfa.required_roles` v2 = owner, finance, gm (keputusan owner 2026-10-02). |
| Kesiapan bisnis (go-live) | ❌ **1 keputusan OPEN (D17)** | Preflight 2026-10-02 setelah owner menjawab 12 keputusan: `NO-GO — 1 BLOCKING decision(s) still OPEN: D17`. Sisa kerja = fitur Kesiapan Tenant |

**Catatan penting:** `NO-GO` itu **bukan** karena software rusak. Semua pemeriksaan sistem **OK** (preflight 2026-10-02: 12 OK, 3 WARN lingkungan). Yang menahan adalah **keputusan bisnis** — dan itu memang disengaja: *"the system may be ready while the business is not"*.

---

## 2. ✅ Yang sudah OK — dengan bukti

| # | Yang sudah OK | Bukti |
|---|---|---|
| 1 | **40 ADR, semua berstatus terbaca, 0 PROPOSED** | 40 berkas `docs/adr/`; penjaga `tests/test_adr_status.py` |
| 2 | **Suite tes hijau penuh** | `3064 passed, 4 skipped, 3780 subtests passed` |
| 3 | **Gerbang pre-deploy bersih** | `SECRET-SCAN: PASS (0/195)`, `UNSAFE-SCAN: PASS (0/195)`, PG twin cocok, tree bersih |
| 4 | **Katalog policy lengkap & fail-closed** | **25 dari 25 key terbit**; key tanpa revisi **ditolak**, bukan diberi nilai karangan |
| 5 | **Invariant inti ditegakkan kode** | `HOLD_REQUESTED != HOLD_CONFIRMED` (`hold_states.py`, `reservation_kernel.py`) · `AI_EXTRACTED != VERIFIED` (`supplier_route.py`) · `SOURCE_UNAVAILABLE != NOT_AVAILABLE` (`crm.py`, `evidence.py`) |
| 6 | **"Payment reported ≠ funds received"** | Ditegakkan lewat jenis rekonsiliasi `MISSING_SETTLEMENT` + `UNVERIFIED_WEBHOOK` (`payments.py`) — bukan lewat nama itu, tapi fungsinya ada |
| 7 | **Approval terikat fingerprint target** | `request_fingerprint` (`approvals.py`), dipakai di 3 titik |
| 8 | **Identitas AI sudah ada (bukan agent)** | `ai_service.py` (ADR-0018): tier `LOW/MEDIUM/HIGH`, status `ACTIVE/SUSPENDED/RETIRED`, deny registry `security.ai.denied_actions` |
| 9 | **Audit chain + anchor** | `audit.py`, `anchors.py`, `make anchor-publish/verify` |
| 10 | **Bukti berjenjang (evidence level A–D)** | `evidence.py`; hold butuh bukti level A untuk menutup expiry |
| 11 | **Klasifikasi data P0–P4 + retensi** | `docs/DATA_CLASSIFICATION_AND_ACCESS.md`, `docs/RETENTION_AND_INCIDENT_RESPONSE.md` |
| 12 | **Keputusan owner hari ini sudah terekam** | ADR-0032 (6 key), ADR-0033 (D1), ADR-0034 (3 tahap hold) |

---

## 3. ⚠️ Yang BELUM OK — ini daftar kerjaan sebenarnya

### 3.1 Lapisan AI Workforce: **nol kode** (temuan terbesar)

Dokumen Anda mendefinisikan **6 komponen AI**, dan owner menambah **1** lagi pada 2026-10-02 (Security & Privacy Warden, ADR-0039). Yang ada di kode hari ini: **0 dari 7**.

| # | Komponen (dok Anda §7–§12) | Berkas kode | Status |
|---|---|---|---|
| 1 | AI Control Tower / Orchestrator | — | ⚠️ belum ada |
| 2 | Commercial AI Agent | — | ⚠️ belum ada |
| 3 | AI Reservation Service | — | ⚠️ belum ada |
| 4 | Operations AI Agent | — | ⚠️ belum ada |
| 5 | Finance Admin AI Agent | — | ⚠️ belum ada |
| 6 | Revenue & Business Intelligence AI Agent | — | ⚠️ belum ada |

Bukti: `grep -rl "control_tower\|reservation_service\|commercial_ai\|operations_ai\|finance_admin\|revenue_bi" src/` → **0 berkas**.
Yang **sudah** ada adalah **identitas** AI (`ai_service.py`, ADR-0018) — yaitu *siapa* AI di mata sistem dan apa yang dilarang. Itu fondasi yang benar, bukan agent-nya.

**Artinya: seluruh mandat AI-spesifik di dokumen Anda belum bisa dijalankan** — §13 human handoff, §14 event envelope, §17 kill switch berjenjang, §19 matriks tes AI, §20 guardrail. Bukan karena salah, tapi karena belum dimulai.

### 3.2 Infrastruktur non-agent (§2A.6)

| Komponen | Status | Bukti |
|---|---|---|
| Policy Engine | ✅ ada | `src/policy_engine.py` (WAVE 1-H, ADR-0017) |
| Approval Engine | ✅ ada | `src/approvals.py` |
| Identity / RBAC | ✅ ada | `src/identity.py` (+ TOTP, MFA) |
| Audit / Observability | ✅ ada | `src/audit.py`, `src/observability.py`, `src/anchors.py` |
| **Outbox / Queue** | ✅ **inti ada (2026-10-02)** | `src/outbox.py` + tabel `outbox_events`/`outbox_deliveries`/`integration_inbox` (migrasi 006). Menegakkan ADR-0007: event masuk **satu transaksi** dengan state bisnisnya, idempotent per agency, retry berjenjang + dead-letter, dan isi surat tidak bisa diubah (trigger). Belum: worker latar (`systemd`/scheduler) yang memanggil `publish_due` berkala + transport nyata (email/WA/webhook) |
| **Exception Engine** | ✅ **inti ada (2026-10-02)** | `src/exception_engine.py` + tabel `exception_fingerprints` (migrasi 007). Tujuh kategori gagal → playbook & severity **dicari di tabel, tidak ditebak** (kategori tak dikenal ditolak); kegagalan berulang = **satu insiden + penghitung** (jendela menggelinding dari kedatangan terakhir); laporannya masuk outbox (tidak bisa hilang). Belum: worker latar + kanal kirim nyata |
| **Secrets / DLP** | ⚠️ **belum** | tidak ada modul (baru ada `unsafe_config_scan.sh` + guard kartu) |

### 3.3 Dua ADR sudah ACCEPTED tapi **belum dibangun**

| ADR | Isi | Bukti belum dibangun |
|---|---|---|
| **ADR-0012** | 4 kategori rel pembayaran: `CARD`, `BANK_TRANSFER`, `PAYPAL`, `STABLECOIN` | `grep -c "method_category" src/payments.py` → **0**. `payments.py` masih pakai kosakata lama: `HOSTED_PAGE, BANK_TRANSFER, CASH, OTHER` |
| **ADR-0013** | Kontrol stablecoin USDC/USDT | `grep -rn "USDC\|USDT\|stablecoin" src/` → **kosong** |

Ini bukan kontradiksi — ADR-0012 sendiri menyatakan *"It is a design acceptance: no payment schema change is executed until the owner approves the migration itself"*. Jadi statusnya jujur: **desain diterima, eksekusi belum**.

### 3.4 Matriks tes wajib (§19 dok Anda) belum ada sebagai kategori

66 berkas tes ada, tapi **0** berkas untuk kategori yang dokumen Anda wajibkan:

| Kategori §19 | Berkas tes | Kenapa |
|---|---|---|
| Functional | ✅ ada (domain) | booking/hold/payment sudah diuji |
| Concurrency | ⚠️ 0 | — |
| Security (IDOR, prompt injection, tool abuse) | ⚠️ 0 | belum ada lapisan AI untuk diserang |
| Finance | ✅ sebagian | `tests/` punya tes payment/reconciliation |
| AI Grounding | ⚠️ 0 | agent belum ada |
| Resilience (outage, crash, failover) | ⚠️ 0 | — |
| Human Handoff | ⚠️ 0 | §13 belum dibangun |
| Audit | ✅ 1 berkas | `tests/` audit |

**Kesimpulan jujur:** §19 belum bisa dijalankan karena 6 dari 8 kategorinya menguji lapisan yang belum ada.

### 3.5 Dokumen AI Workforce belum tersambung ke traceability repo

Dokumen itu hanya disebut di `ADR-0033`, `ADR-0034` (yang saya tulis) dan matriks. **Tidak ada satu dokumen pun** di `docs/` yang memetakan 24 bagian dokumen itu ke repo ini. Artinya: acuan Anda belum jadi acuan *proyek* — masih jadi acuan *percakapan*.

### 3.6 Kill switch baru sebagian (§17)

| Status §17 | Ada di kode? |
|---|---|
| ACTIVE | ✅ |
| SUSPENDED | ✅ (`ai_service.py`, per principal) |
| **DEGRADED** | ⚠️ belum |
| **READ_ONLY** | ⚠️ belum |
| DISABLED / RETIRED | ✅ (RETIRED) |

### 3.7 Item terbuka yang sudah tercatat di ADR (jujur, belum ditutup)

| ADR | Item terbuka |
|---|---|
| ADR-0016 | Policy engine untuk komunikasi AI ke tamu (§56); item option-set belum berharga |
| ADR-0017 | ~~Kosakata peran (D3)~~ **SELESAI 2026-10-02** (`src/roles.py`, amendment ADR-0035); sisa: outbox + RBAC peran halaman 12→16; baru 1 konsumen policy engine terpasang |
| ADR-0018 | ~~Kosakata peran tidak konsisten~~ **SELESAI 2026-10-02** (satu rumah + tes matriks per-peran `tests/test_roles.py`) |
| ADR-0020 | Bukti buatan sendiri di `request_hold`; baris extension menggantung; `verify_expired_hold` belum punya tombol |

---

## 4. ❓ PERLU KEPUTUSAN ANDA

### 4.1 D3 — Organisasi & peran (**koreksi Anda saya terima**)

Anda benar dan saya salah sebelumnya. Saya usul "jangan tambah peran lain" — Anda menolak, dan alasan Anda lebih tepat: **peran manusia di dokumen itu bukan permission, tapi akuntabilitas.** Menamai peran tidak menambah risiko; yang menambah risiko adalah **wewenang** yang tidak ada pengawasnya. Jadi saya revisi: **adopsi SEMUA peran manusia di dokumen Anda.**

| Peran manusia (dok Anda §2 + §21) | Setara di spec §18 | Catatan |
|---|---|---|
| **Owner / Shareholders** | Owner/Director | boleh **satu orang** dengan GM |
| **General Manager / CEO** | *(tidak ada di §18)* | boleh **satu orang** dengan Owner sampai bisnis berkembang |
| **Commercial & Marketing Manager** | *(tidak ada di §18)* | ✅ diadopsi |
| **Partnership Manager** | Villa/Contract Admin | ✅ diadopsi — dok Anda §21: **HUMAN, bukan agent** |
| **Operations / Reservation Manager** | Reservation Manager + Operations Manager | ✅ diadopsi |
| **Reservation Agent** | Reservation Agent | ✅ diadopsi (staf di bawah Ops/Res Mgr) |
| **Finance & Accounting Manager** | Finance Manager | ✅ diadopsi |
| *(staf keuangan)* | Finance Staff | ✅ diadopsi — **wajib terpisah** dari Finance Manager |
| **Revenue Manager / GM if lean** | *(tidak ada di §18)* | ✅ diadopsi; dok Anda sendiri izinkan dipegang GM |
| **Technology / Security** | System Admin | ✅ diadopsi — **nol wewenang bisnis/finansial** |
| **Legal / Compliance** | *(tidak ada di §18)* | ✅ diadopsi (boleh pihak eksternal) |
| *(guest service)* | Guest Service | ✅ diadopsi |
| **Auditor** | Auditor | ✅ diadopsi — **read-only** |
| *(non-manusia)* | AI Service, System Service | ✅ sudah ada (`ai_service.py`) |

**Yang saya butuh dari Anda:** nama orang untuk tiap peran. Saya tidak akan mengarang nama.
**Yang tidak boleh disatukan** (ini kontrol, bukan preferensi): Finance Staff ≠ Finance Manager; Reservation Agent ≠ Reservation Manager (untuk diskon di luar policy); System Admin tanpa wewenang uang; Auditor read-only.

### 4.2 D1 — separuh yang masih terbuka

Merchant of record **sudah Anda putuskan** (agency) dan sudah terekam di ADR-0033. Yang belum: **nama provider per rel** — kartu (PSP mana?), transfer bank (bank mana?), PayPal, stablecoin (kustodian mana?). Tanpa itu, ADR-0012 & ADR-0013 tidak bisa dibangun dan preflight memang tetap menolak.

### 4.3 Dua belas keputusan lain (D2, D4–D14)

| # | Keputusan | Yang saya butuhkan dari Anda |
|---|---|---|
| D2 | Daftar property pilot + model bisnisnya | Nama villa + commission / full management |
| D4 | SLA bisnis (first response, quote, rekonsiliasi ≤48 jam, komplain) | Angka menit/jam |
| D5 | Sumber tarif PBJT per kabupaten/kota + siapa mengesahkan | Tarif + pejabat |
| D6 | WhatsApp Business + status template pesan | Provider + template |
| D7 | Ambang shadow-mode (agreement rate, unsupported claim = 0) | Angka |
| D8 | Tinjauan legal (kontrak owner, T&C tamu, privacy notice, DPIA) | Siapa/ kapan |
| D9 | Retensi per kelas **sudah Anda putuskan**; sisanya: **siapa yang mengeksekusi penghapusan** | Nama penanggung jawab |
| D10 | Backup: RPO/RTO, lokasi, siapa menguji restore | Angka + nama |
| D11 | Refund/chargeback/kompensasi: batas nilai + siapa menyetujui | Angka + nama |
| D12 | Konfirmasi tidak menyimpan data kartu + alasan tertulis | Sudah ditegakkan gate; butuh pernyataan resmi |
| D13 | Monitoring + siapa bertanggung jawab saat alarm (on-call) | Nama |
| D14 | Penerbitan anker rantai audit: siapa, seberapa sering, ke sistem mana | Nama + frekuensi |

### 4.4 Dua pertanyaan nyata (bukan administratif)

1. **Tahap 1 hold:** pada "Tentative Holding", apakah villa **sudah diminta** dan belum menjawab — atau villa **belum dihubungi sama sekali**? (Pemetaannya tetap benar, tapi ADR-0034 perlu saya perjelas.)
2. **Laporan owner (key #3):** laporan harian/mingguan/bulanan dikirim ke **siapa saja** dan lewat kanal apa? (Email? Dashboard? WhatsApp?)

### 4.5 Urutan pembangunan AI Workforce

Ini keputusan Anda, dan saya tidak akan memilihkan. Usulan saya di §6.

---

## 5. Peta Organisasi AI + Human (detail)

### 5.1 Enam komponen AI — siapa melapor ke siapa, dan statusnya

| # | Komponen | Worker inti (dok §2A) | Pemilik manusia | Laporan eksekutif | Kode |
|---|---|---|---|---|---|
| 1 | **AI Control Tower** | CT-W01…W10 (router, workflow, SLA, exception, escalation, health, retry, notif, budget) | General Manager | GM; Owner untuk insiden material | ⚠️ 0% |
| 2 | **Commercial AI** | COM-W01…W11 (lead intel, scoring, CRM, campaign, content, SEO, social, nurture, B2B, perf, abandoned recovery) | Commercial & Marketing Manager | lewat manager/GM | ⚠️ 0% |
| 3 | **AI Reservation Service** | RES-W01…W11 (inquiry, matching, availability, pricing, quote, conversation, hold, booking, amend/cancel, pre-arrival, handoff) | Operations / Reservation Manager | lewat manager/GM | ⚠️ 0% |
| 4 | **Operations AI** | OPS-W01…W10 (workflow, SLA, doc completeness, QC, readiness, task, exception, incident, comm QC, compliance) | Operations Manager | GM/Owner per severity | ⚠️ 0% |
| 5 | **Finance Admin AI** | FIN-W01…W11 (AR, AP, matching, recon, invoice validation, settlement, due monitor, unallocated, duplicate, exception, cash/exposure) | Finance & Accounting Manager | **langsung GM + Owner** | ⚠️ 0% |
| 6 | **Revenue & BI AI** | RBI-W01…W12 (revenue KPI, conversion, pace, demand, pricing intel, villa/partner/channel perf, forecast, margin, anomaly, exec report) | Revenue Manager, atau GM bila lean | **langsung GM + Owner** | ⚠️ 0% |

**Yang penting dari tabel ini:** dokumen Anda §2A.7 dan §20.1 melarang memperbanyak agent otonom, dan §21 menegaskan Partnership / GM / Owner / Finance Authority / Legal / Security adalah **MANUSIA**. Jadi keenam kotak itu adalah **satu-satunya** agent — worker `*-Wxx` bukan agent ke-7, ke-8, dst. Saya pegang aturan itu.

### 5.2 Yang boleh dan tidak boleh dilakukan AI (dok §11) — sudah ada rumahnya

| Larangan mutlak | Ditegakkan di kode? |
|---|---|
| AI menyetujui/mengeksekusi pembayaran | ✅ identitas `ai_service` + deny registry |
| AI mengubah tujuan bank/crypto | ✅ idem |
| AI menandai dana belum terverifikasi sebagai diterima | ✅ jenis rekonsiliasi `UNVERIFIED_WEBHOOK` |
| AI melewati maker-checker | ✅ `approvals.py` dual control |
| PAN/CVV masuk konteks AI atau storage biasa | ✅ `CARD_DATA_FORBIDDEN` + gate `unsafe_config_scan` |

Ini bagian yang **sudah kuat**, dan itu kabar baik: pondasi keamanan AI sudah berdiri sebelum agent-nya ada. Urutannya benar.

---

## 6. Usulan urutan kerja (untuk Anda putuskan)

Bukan urutan yang saya karang: ini mengikuti ketergantungan nyata di dokumen Anda sendiri (§20.1: deterministik dulu; §3: tanpa gateway & policy, agent tidak boleh jalan).

| Urut | Pekerjaan | Kenapa harus di urutan ini |
|---|---|---|
| 1 | **D3 peran & orang** + RBAC 12→16 peran + tes matriks per-peran | Tanpa ini, tidak ada `agency_id`/aktor yang bisa dipercaya untuk agent mana pun |
| 2 | **Outbox + Exception Engine** | §14 melarang free-form antar-agent; tanpa outbox, handoff antar agent tidak boleh dibangun |
| 3 | **AI Policy Gateway + kill switch berjenjang** (DEGRADED/READ_ONLY) | §3: agent hanya boleh lewat gateway; §17 butuh 5 status |
| 4 | **AI Reservation Service** (komponen #3) | Nilai bisnis terbesar + tamu berhadapan langsung dengannya |
| 5 | **Finance Admin AI** (komponen #5) | Menyambung alur D1 yang baru Anda putuskan |
| 6 | Control Tower, Operations AI, Commercial AI, Revenue & BI | Setelah 4 & 5 terbukti |

Yang **tidak** saya usulkan sekarang: membangun Commercial AI sebelum reservation jalan — tidak ada demand untuk diolah kalau booking-nya sendiri belum otomatis.

---

## 7. Bagaimana saya memeriksa (supaya bisa Anda ulang sendiri)

| Klaim | Perintah |
|---|---|
| 40 ADR & statusnya | `for f in docs/adr/*.md; do grep -m1 -iE '^\- ?\*?\*?Status' "$f"; done` |
| 7 komponen AI belum ada | `grep -rl "control_tower\|reservation_service\|commercial_ai\|operations_ai\|finance_admin\|revenue_bi\|security_warden" src/` |
| ADR-0012 belum dibangun | `grep -c "method_category" src/payments.py` → 0 |
| Outbox belum ada | `grep -in outbox src/schema/001_initial.sql` → kosong |
| Gerbang hijau | `make check` |
| Kesiapan bisnis | `VILLA_DB=data/villa.db /usr/bin/python3 scripts/preflight.py` |

---

**Kesimpulan satu paragraf.** Sistem domainnya **sehat dan bisa dipercaya** — 40 ADR, 3.117 tes hijau, katalog policy penuh, invariant ditegakkan kode. Yang **belum** adalah lapisan AI workforce (0 dari 6 komponen) dan tiga infrastruktur penopangnya (outbox, exception engine, DLP), plus dua ADR pembayaran yang sudah diterima tapi belum dieksekusi. Yang **menahan go-live** bukan software, tapi 1 keputusan bisnis (kesiapan tenant, D17). Dan **pendapat saya soal dokumen Anda: setuju, dokumen itu memang acuan yang tepat** — ia satu-satunya sumber yang memisahkan *agent*, *worker*, dan *manusia* dengan tegas, dan itu persis pembedaan yang mencegah sistem ini tumbuh jadi kumpulan chatbot yang saling mengklaim wewenang. Yang perlu ditambahkan hanya satu: dokumen itu harus **tersambung ke traceability repo** supaya jadi acuan proyek, bukan acuan percakapan.
