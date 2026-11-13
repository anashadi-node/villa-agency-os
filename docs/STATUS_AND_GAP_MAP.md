# Peta Status & Gap — Villa Agency OS
**Tanggal periksa: 2026-09-28** · **Pembaruan 2026-10-02**: tahap 4 (AI Reservation Service) — inti selesai · Sumber acuan: Master Spec v1.1 (invariant domain) + **AI Workforce Architecture V1.1** (organisasi & lapisan AI) · Bukti: kode, tes, gerbang, preflight

> **CATATAN STATUS (2026-10-03) — baca ini dulu.** Dokumen ini adalah **potret bertanggal
> 2026-09-28/10-02**, bukan status hari ini. Beberapa baris di bawah sudah tidak berlaku:
> lapisan AI Reservation Service (agent #3) **sudah ada kodenya** (`src/reservation_service.py`,
> `src/reservation_workers.py` 11 worker, `src/ai_gateway.py`), begitu pula `channels.py`,
> `shadow_eval.py`, KPI, dan dashboard Phase 4. Status terkini ada di
> `docs/PHASE2_TO_4_EXECUTION_PLAN.md` §5 dan buktinya di `reports/final-report.md`.
> Dokumentasi tidak dihapus karena fungsi aslinya — daftar kekurangan bertanggal — tetap
> berguna selama dibaca sebagai potret, bukan sebagai klaim hari ini.

---

## 0. Cara membaca peta ini

Setiap baris di bawah punya **bukti yang bisa diperiksa ulang**, bukan pendapat. Tiga ember:

- ✅ **OK** — sudah jalan, ada bukti
- ⚠️ **BELUM OK** — belum ada / belum lengkap, ini kerjaan
- ❓ **PERLU KEPUTUSAN ANDA** — software tidak boleh menebak

Satu hal yang saya tegaskan di depan, karena ini akar semua yang di bawah: **pada tanggal potret ini (2026-09-28) platform adalah sistem DOMAIN yang kuat, dan lapisan AI WORKFORCE yang masih nol.** Dua hal berbeda, dan peta ini memisahkannya dengan jelas. *(Sejak 2026-10-02 lapisan AI Reservation Service — agent #3 — sudah punya kode; lihat catatan status di atas.)*

---

## 1. Ringkasan satu layar

| Area | Status | Bukti ringkas |
|---|---|---|
| Domain: booking, hold, pembayaran, policy, bukti, identitas | ✅ **kuat** | 68 ADR, 3.768 tes hijau (+4.008 subtes), gerbang PASS, katalog policy 25/25 |
| Lapisan **AI Workforce** (6 komponen dok Anda + 1 tambahan owner 2026-10-02) | 🟡 **5 dari 7 ada** | Control Tower, Reservation, Finance Admin, Operations, **Revenue & BI (slice 1, 2026-10-07)** ada di kode; belum: Commercial, Security & Privacy Warden |
| Infrastruktur non-agent | ✅ **6 ada, 1 belum** | Policy/Approval/Identity/Audit ada; **Outbox + Exception Engine ada (2026-10-02)**; sisa: DLP |
| Organisasi & peran (D3) | ✅ **DECIDED, kosakata sudah satu rumah** | ADR-0035 (§5 "3 kosakata hidup bersamaan") direkonsiliasi 2026-10-02 lewat `src/roles.py` + amendment ADR-0035: `approvals.ROLE_RANK` **adalah** `roles.ROLE_RANK`, nama lama tetap resolvable untuk baris historis, dan `gm` masuk tangga (rank 4). **`gm` juga sudah punya halaman** (Command Center/Approvals/Finance/Cases/Properties) dan **wajib MFA** — policy `security.mfa.required_roles` v2 = owner, finance, gm (keputusan owner 2026-10-02). |
| Kesiapan bisnis (go-live) | ❌ **1 keputusan OPEN (D17)** | Preflight 2026-10-02 setelah owner menjawab 12 keputusan: `NO-GO — 1 BLOCKING decision(s) still OPEN: D17`. Sisa kerja = fitur Kesiapan Tenant |

**Catatan penting:** `NO-GO` itu **bukan** karena software rusak. Semua pemeriksaan sistem **OK** (preflight 2026-10-02: 12 OK, 3 WARN lingkungan). Yang menahan adalah **keputusan bisnis** — dan itu memang disengaja: *"the system may be ready while the business is not"*.

---

## 2. ✅ Yang sudah OK — dengan bukti

| # | Yang sudah OK | Bukti |
|---|---|---|
| 1 | **68 ADR — 68 Accepted (ADR-0061 ✓ opsi B; ADR-0062 ✓ presisi detektor identifier; ADR-0063 ✓ id tidak berbentuk identitas; ADR-0064 ✓ nama kanonik RBI-W01…W12 + denominator okupansi resmi — keputusan owner 2026-10-07; ADR-0065 ✓ copy villa publik menggambarkan wilayah, bukan properti — kalimat disclosure disetujui owner 2026-10-08; ADR-0066 ✓ dataset demo di-anchor saat seed; ADR-0067 ✓ alamat tamu dipercaya hanya dari hop proxy sendiri — hosting produksi situs publik disetujui owner 2026-10-08; ADR-0068 ✓ presisi + sumber lokasi properti: tiga keadaan lokasi, presisi sebagai saklar, satu aturan untuk dua penulis, 2026-11-13); semua status terbaca** | 68 berkas `docs/adr/`; penjaga `tests/test_adr_status.py` |
| 2 | **Suite tes hijau penuh** | `3768 passed, 4 skipped, 4008 subtests passed` (snapshot gerbang terakhir, 2026-10-08 — `make check`: PRE-DEPLOY GATE PASS) |
| 3 | **Gerbang pre-deploy bersih** | `SECRET-SCAN: PASS (0 findings, 344 files)`, `UNSAFE-SCAN: PASS (0 findings, 344 files)`, PG twin cocok |
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
| 1 | AI Control Tower / Orchestrator | `src/control_tower.py` | 🟡 **inti ada 2026-10-06**: tabel `ROUTES`, `UnroutableEvent` (tujuan tak dikenal ditolak, tidak ditebak), keputusan routing membawa alasan, eskalasi lewat `ai.escalation.raised` di gerbang yang sama, tool-nya **dideklarasikan tanpa didaftarkan** (ADR-0061 opsi B). **Belum**: CT-W01…W10 |
| 2 | Commercial AI Agent | — | ⚠️ belum ada |
| 3 | AI Reservation Service | `src/reservation_service.py` + `src/schema/012_reservation_service.sql` | 🟡 **inti ada 2026-10-02**: ledger percakapan berisolasi, tangga §13 (8 rung), kosakata eskalasi tertutup, paket handoff berdigest, ambang keyakinan deny-first, handoff hanya ke peran berisi orang. **Ada juga**: 10 worker RES-W01…RES-W10 (inquiry, matching, availability, pricing, versi penawaran, follow-up, hold, status booking, amend/cancel assist, pre-arrival) lewat pintu `ai_gateway` + `src/reservation_workers.py`, pricing di `reservation_kernel.quote_stay` (mengumpulkan aturan yang sudah ada, **tidak menulis**) dan penawaran lewat satu tindakan `quotation.send` (MEDIUM) — keduanya memakai nama §56 yang sudah ada, katalog tidak ditambah. **Belum**: 1 worker sisanya (RES-W11 handoff) + CLI |
| 4 | Operations AI Agent | `src/operations_agent.py` | 🟡 **inti read-only 2026-10-07**: beban kerja, SLA tugas+insiden, gap dokumen, kesiapan kedatangan, survei eksepsi, KPI §10 (`None` saat kosong), tool LOW `ops.workflow/sla/readiness`. **Belum**: OPS-W04 booking QC, OPS-W09 comm QC, OPS-W10 SOP/compliance store |
| 5 | Finance Admin AI Agent | `src/finance_admin.py` | 🟡 **inti read-only 2026-10-07**: AR aging, AP, due monitor, eksepsi, kas/eksposur, KPI §11. **Belum**: FIN-W03/05/08/09 |
| 6 | Revenue & Business Intelligence AI Agent | — | ⚠️ belum ada |

Bukti: `ls src/control_tower.py src/reservation_service.py src/finance_admin.py src/operations_agent.py` → **4 ada**; sisa `commercial_ai`, `revenue_bi`, `security_warden` belum ada. (Jangan pakai `grep` nama komponen sebagai bukti: `control_tower.py` menyebut COM/RBI di ROUTES meski modulnya belum ada.)
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
| Setelan tenant (D17) | ✅ **inti ada (2026-10-02)** | `src/settings.py` (katalog 10 kunci, 5 wajib sebelum go-live) + `readiness()` + preflight `check_tenant_readiness`. Sisa: impor villa sekaligus dan penyunting peran per tenant |
| Penyunting peran per tenant (D3) | ✅ **inti ada (2026-10-02)** | `src/agency_roles.py` + `scripts/manage_roles.py`: 8 peran cetak biru di-seed, nama boleh diganti, peran bisa dinonaktifkan, pemegang = rujukan tanpa nama orang; wewenang dibaca dari cetak biru sehingga tenant tidak bisa menaikkan wewenang atau menghidupkan lagi peran pembuat AI |
| **Secrets / DLP** | ⚠️ **belum** | tidak ada modul (baru ada `unsafe_config_scan.sh` + guard kartu) |

### 3.3 Dua ADR sudah ACCEPTED tapi **belum dibangun**

| ADR | Isi | Bukti belum dibangun |
|---|---|---|
| **ADR-0012** | 4 kategori rel pembayaran: `CARD`, `BANK_TRANSFER`, `PAYPAL`, `STABLECOIN` | `grep -c "method_category" src/payments.py` → **0**. `payments.py` masih pakai kosakata lama: `HOSTED_PAGE, BANK_TRANSFER, CASH, OTHER` |
| **ADR-0013** | Kontrol stablecoin USDC/USDT | `grep -rn "USDC\|USDT\|stablecoin" src/` → **kosong** |

Ini bukan kontradiksi — ADR-0012 sendiri menyatakan *"It is a design acceptance: no payment schema change is executed until the owner approves the migration itself"*. Jadi statusnya jujur: **desain diterima, eksekusi belum**.

### 3.4 Matriks tes wajib (§19 dok Anda) — potret September + audit ulang 2026-11-13

> **Koreksi terukur (2026-11-13).** Angka "66 berkas, 0 untuk semua kategori" adalah potret
> September. Audit ulang dihitung **per badan fungsi uji** (bukan grep nama berkas), atas
> `tests/test_*.py` (104 berkas / 57.995 baris) ditambah berkas konkurensi baru. Hasilnya
> tidak "0": beberapa kategori sudah punya uji nyata, sisanya tetap belum terbukti —
> dan itu ditulis apa adanya.

| Kategori §19 | Status terukur (2026-11-13) | Bukti / cara ukur |
|---|---|---|
| Functional | ✅ ada | tes domain booking/hold/payment (~2.719 `def test_` total) |
| Concurrency | ✅ **1 berkas / 7 uji** | `tests/test_no_double_booking_concurrency.py` — file-backed, `BEGIN IMMEDIATE`, 2–3 penulis + `threading.Barrier`; **terbukti bisa gagal** lewat kontrol negatif. Rincian: `docs/DOUBLE_BOOKING_EVIDENCE.md` |
| Security (IDOR/tenant, prompt injection, tool abuse) | 🟡 **sebagian**: tenant/IDOR 5 berkas / 11 uji · penolakan tool 7 / 15 · injeksi-sebagai-data 2 / 6 | klasifikasi regex per `def test_`; cakupan belum sistematis, tidak ada uji serangan terarah menyeluruh |
| Finance | ✅ sebagian | tes payment/reconciliation |
| AI Grounding | 🟡 16 berkas / 86 uji menyebut `grounding`/`evidence_ref` — **batas atas** (token longgar), bukan bukti kualitas | klasifikasi regex |
| Resilience (outage, crash, failover) | 🟡 `retry`/`idempoten`/`replay` 42 berkas / 302 uji — **batas atas**; tidak ada uji failover/outage terarah | klasifikasi regex |
| Human Handoff | 🟡 10 berkas / 43 uji (`handoff`/`escalat`) — permukaan ada, alur §13 belum utuh | klasifikasi regex |
| Audit | ✅ 1 berkas | `tests/` audit |

**Kesimpulan jujur:** dari 8 kategori §19, **1 tertutup penuh** (concurrency, dengan bukti bisa
gagal), **3 tertutup sebagian** (security, finance, audit), **3 punya permukaan tapi belum
terbukti** (grounding, resilience, handoff). Tidak ada kategori yang boleh disebut "selesai"
tanpa uji terarah yang bisa gagal.

### 3.5 Dokumen AI Workforce belum tersambung ke traceability repo — **USANG sejak 2026-09-28**

> **Koreksi (2026-11-13):** pernyataan di bawah **tidak lagi benar**. Berkas
> `docs/AI_WORKFORCE_TRACEABILITY.md` sudah ada (23.149 byte, diperiksa 2026-09-28, metode:
> pemeriksaan langsung kode/skema/tes/gerbang) dan memetakan 24 bagian dokumen AI Workforce
> dengan tanda ✅ DIIKUTI / 🟡 SEBAGIAN / ❌ BELUM. Yang **belum** dilakukan: audit ulang atas
> ketepatan pemetaan itu (lihat Bab 25 butir 6 dokumen A–Z). Isi paragraf lama dipertahankan
> di bawah sebagai potret sejarah, bukan sebagai status hari ini.

Dokumen itu hanya disebut di `ADR-0033`, `ADR-0034` (yang saya tulis) dan matriks. **Tidak ada satu dokumen pun** di `docs/` yang memetakan 24 bagian dokumen itu ke repo ini. Artinya: acuan Anda belum jadi acuan *proyek* — masih jadi acuan *percakapan*.

### 3.6 Kill switch baru sebagian (§17)

| Status §17 | Ada di kode? |
|---|---|
| ACTIVE | ✅ |
| SUSPENDED | ✅ (`ai_service.py`, per principal) |
| **DEGRADED** | ✅ (hanya alat tingkat LOW; menolak MEDIUM walau policy mengizinkan) |
| **READ_ONLY** | ✅ (hanya alat berefek READ; menolak tulis berisiko rendah sekalipun) |
| DISABLED / RETIRED | ✅ (RETIRED, terminal) |

Circuit breaker: ✅ `AiGateway.record_failure` — 3 kegagalan berturut dalam 15 menit → `READ_ONLY` otomatis + insiden SEV3.

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
| 1 | **AI Control Tower** | CT-W01…W10 (router, workflow, SLA, exception, escalation, health, retry, notif, budget) | General Manager | GM; Owner untuk insiden material | 🟡 **inti 2026-10-06** — router + eskalasi (`src/control_tower.py`); tool deklaratif (ADR-0061 opsi B); worker CT-W01…W10 belum |
| 2 | **Commercial AI** | COM-W01…W11 (lead intel, scoring, CRM, campaign, content, SEO, social, nurture, B2B, perf, abandoned recovery) | Commercial & Marketing Manager | lewat manager/GM | ⚠️ 0% |
| 3 | **AI Reservation Service** | RES-W01…W11 (✅ lengkap 2026-10-03: inquiry, matching, availability, pricing, quote, follow-up, hold, booking-status, amend/cancel-assist, pre-arrival, handoff ADR-0053) | Operations / Reservation Manager | lewat manager/GM | ✅ **lengkap 2026-10-03** — inti (ledger + tangga §13 + eskalasi), gerbang komersial §9 (ADR-0050), gerbang penutupan §30 lengkap (ADR-0052/0054/0055/0056), CLI staf (`closure_facts.py`), telemetri KPI (ADR-0057), **pengukuran shadow-mode (ADR-0058)**, **adapter kanal (ADR-0059: pintu HMAC per-tenant, replay idempoten, kirim tetap manusia)**; **halaman dashboard Kanal & Mutu AI/shadow selesai 2026-10-04**; tersisa: halaman KPI + bukti go-live end-to-end |
| 4 | **Operations AI** | OPS-W01…W10 (workflow, SLA, doc completeness, QC, readiness, task, exception, incident, comm QC, compliance) | Operations Manager | GM/Owner per severity | 🟡 **inti read-only 2026-10-07** — beban kerja, SLA, gap dokumen, kesiapan kedatangan, survei eksepsi, KPI (`src/operations_agent.py`, 33 tes); sisa OPS-W04 booking QC sweep, OPS-W09 comm QC sweep, OPS-W10 SOP/process compliance store belum; tool agen **dideklarasikan tanpa didaftarkan** (ADR-0061 opsi B, 2026-10-07) |
| 5 | **Finance Admin AI** | FIN-W01…W11 (AR, AP, matching, recon, invoice validation, settlement, due monitor, unallocated, duplicate, exception, cash/exposure) | Finance & Accounting Manager | **langsung GM + Owner** | 🟡 **inti read-only 2026-10-07** — AR aging, AP, due monitor, exception, cash/exposure, KPI (`src/finance_admin.py`, 23 tes); sisa FIN-W03/05/08/09 belum |
| 6 | **Revenue & BI AI** | RBI-W01…W12 (nama kanonik ADR-0064: revenue_kpi, conversion, pace_leadtime, demand_intel, pricing_intel, villa_performance, partner_performance, channel_performance, forecast, margin, anomaly, exec_report) | Revenue Manager, atau GM bila lean | **langsung GM + Owner** | 🟡 ~25% (slice 1: M1/M2/M3/M6/M8/M9 baca-saja, 28 tes; sisa slice 2-5) |

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
| ~~3~~ | ~~AI Policy Gateway + kill switch berjenjang~~ ✅ **SELESAI 2026-10-02** | `src/ai_gateway.py`: satu pintu wajib (dijaga tes statis: tidak ada modul lain memanggil `authorize`), 5 status §17, circuit breaker, tombol operator `scripts/ai_mode.py` (kemampuan `ai.control` = Owner + Technology-Security Manager); 22 tes gateway + 9 tes CLI |
| 4 | **AI Reservation Service** (komponen #3) — **SEDANG JALAN 2026-10-02**: inti (ledger + tangga §13 + eskalasi + handoff) ✅; sisa: 11 worker + CLI | Nilai bisnis terbesar + tamu berhadapan langsung dengannya |
| 5 | **Finance Admin AI** (komponen #5) | Menyambung alur D1 yang baru Anda putuskan |
| 6 | Control Tower, Operations AI, Commercial AI, Revenue & BI | Setelah 4 & 5 terbukti |

Yang **tidak** saya usulkan sekarang: membangun Commercial AI sebelum reservation jalan — tidak ada demand untuk diolah kalau booking-nya sendiri belum otomatis.

---

## 7. Bagaimana saya memeriksa (supaya bisa Anda ulang sendiri)

| Klaim | Perintah |
|---|---|
| 68 ADR & statusnya | `for f in docs/adr/*.md; do grep -m1 -iE '^\- ?\*?\*?Status' "$f"; done` |
| 2 dari 7 komponen AI belum ada (Commercial, Security Warden); Revenue & BI **slice 1 sudah ada** | `ls src/commercial_ai.py src/security_warden.py` → No such file; `ls src/revenue_bi_agent.py` → ada (`tests/test_revenue_bi_agent.py`, 28 tes) |
| ADR-0012 belum dibangun | `grep -c "method_category" src/payments.py` → 0 |
| Outbox belum ada | `grep -in outbox src/schema/001_initial.sql` → kosong |
| Gerbang hijau | `make check` |
| Kesiapan bisnis | `VILLA_DB=data/villa.db /usr/bin/python3 scripts/preflight.py` |

---

**Kesimpulan satu paragraf.** Sistem domainnya **sehat dan bisa dipercaya** — 68 ADR, 3.768 tes hijau, katalog policy penuh, invariant ditegakkan kode. Yang **sudah** ada: inti 5 dari 7 komponen AI workforce (Control Tower, Reservation, Finance Admin, Operations, Revenue & BI slice 1 — ADR & baris traceability). Yang **belum** adalah 2 komponen penuh sisanya (Commercial, Security & Privacy Warden), empat slice lanjutan Revenue & BI (M4/M5/M7/M12/M13 · M14/M16 · M15 · M10/M11) dan tiga infrastruktur penopangnya (outbox, exception engine, DLP), plus dua ADR pembayaran yang sudah diterima tapi belum dieksekusi. Yang **menahan go-live** bukan software, tapi 1 keputusan bisnis (kesiapan tenant, D17). Dan **pendapat saya soal dokumen Anda: setuju, dokumen itu memang acuan yang tepat** — ia satu-satunya sumber yang memisahkan *agent*, *worker*, dan *manusia* dengan tegas, dan itu persis pembedaan yang mencegah sistem ini tumbuh jadi kumpulan chatbot yang saling mengklaim wewenang. Yang perlu ditambahkan hanya satu: dokumen itu harus **tersambung ke traceability repo** supaya jadi acuan proyek, bukan acuan percakapan.
