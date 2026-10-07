# Riset — Agent 6: Revenue & BI AI (RBI)

**Tanggal:** 7 Oktober 2026 · **Jenis:** riset pra-implementasi · **Metode:** audit kode langsung (tabel, kolom, modul, kosakata) × dokumen acuan repo · **Protokol:** *falsification-first* + rule 11 `riset-penelitian` — *ukur cakupan sebelum membangun baru*.

> **Pembaruan status (7 Oktober 2026, setelah laporan ini).** Q1 dan Q5 di §10 **sudah dijawab** dan **slice 1 sudah dibangun**, jadi kalimat "belum ada satu baris kode Agent 6" di atas berlaku untuk *tanggal laporan*, bukan hari ini:
> - **Q5 terjawab** — dua belas worker kini punya nama kanonik (`docs/adr/0064-…md`): `revenue_kpi`, `conversion`, `pace_leadtime`, `demand_intel`, `pricing_intel`, `villa_performance`, `partner_performance`, `channel_performance`, `forecast`, `margin`, `anomaly`, `exec_report`; lima nomor yang sudah dikutip dokumen lain (W04/W09/W10/W11/W12) tidak bergeser.
> - **Q1 terjawab** — denominator okupansi resmi = **unit-nights `units.status='ACTIVE'`** di dalam jendela, dipotong `units.created_at`; malam terjual dari `reservation_kpi.BOOKED_STATUSES`; malam MAINTENANCE/SUSPENDED **dilaporkan** di samping rasio. Jalur bukti ketersediaan tetap **didefinisikan tapi tidak diterbitkan**: terukur **0 baris** `occupancy_ranges` di DB nyata dan **0 pembaca** di seluruh kode — menerbitkannya hari ini berarti mengarang.
> - **Slice 1 landed** — `src/revenue_bi_agent.py` (M1, M2, M3, M6, M8, M9 per mata uang + lineage per metrik), 28 tes; §7 Q2–Q4 masih terbuka (label margin, penandaan ramalan, ambang observasi) dan tidak memblokir slice 2.
> - Satu koreksi untuk slice 2: `invoicing.derive_totals` memasukkan DISCOUNT **ke dalam** subtotal (net, bukan gross), sehingga kedalaman diskon M12 harus dibagi terhadap subtotal **+ |diskon|** bila yang diinginkan porsi terhadap harga kotor.

---

## 1. Ringkasan untuk pemilik

**Apa agen ini, dalam satu kalimat.** Pegawai AI nomor 6 adalah **analis bisnis**: ia menghitung, membandingkan, dan meramal — lalu menyerahkan angkanya kepada manusia. Ia tidak mengubah apa pun.

**Tiga angka yang menentukan bentuk pekerjaannya:**

| Pertanyaan | Jawaban hari ini | Bukti |
|---|---|---|
| Berapa yang sudah ada? | **~45% fondasi** — 1 kolektor KPI sudah hidup, data pendukung lengkap di tabel | `src/reservation_kpi.py`, tabel `bookings/quotes/invoices/invoice_lines/payments/ledger_entries/supplier_settlements/rate_cards/rate_seasons/cases` |
| Berapa yang harus dibangun? | **5 hal substantif**: demand intel, forecast, margin, anomaly, executive report | `docs/RESEARCH_AI_WORKFORCE_SKILLS_2026-10-06.md` §2 (Agen 6) |
| Berapa berkas kode Agent 6 yang ada? | **0** | `docs/AI_WORKFORCE_TRACEABILITY.md` baris 12 |

**Temuan yang paling penting untuk Anda, sebelum belanja kode:**

1. **Ramalan tidak boleh disajikan sebagai fakta.** Itu bukan nasihat gaya bahasa — itu larangan yang tertulis di dokumen acuan (`docs/ECOSYSTEM_EXPLAINED.md` baris 239). Maka setiap angka ramalan di Agent 6 wajib keluar dengan **interval keyakinan + label "ramalan" + ambang minimum observasi**; kalau datanya kurang, jawabannya harus `None` — bukan angka.
2. **Margin belum bisa dihitung penuh.** Tidak ada kolom pembayaran ke pemilik vila di skema. Angka margin v1 hanya boleh berarti *"pendapatan tagihan − biaya supplier yang tercatat"*, dan **wajib dilabeli parsial**. Menyebutnya "profit" akan salah.
3. **Denominator okupansi adalah keputusan Anda**, bukan keputusan teknis. Ada dua kandidat sumber yang sah (daftar unit vs bukti ketersediaan), dan keduanya menghasilkan angka berbeda.

---

## 2. Mandat & batas wewenang (dari dokumen, bukan karangan)

| Sumber di repo | Isinya |
|---|---|
| `docs/ECOSYSTEM_EXPLAINED.md:236–241` | Tugas: analisa pendapatan, laju booking, permintaan, harga, performa villa/mitra/kanal, ramalan, deteksi anomali, laporan eksekutif. **Tidak boleh:** mengubah harga resmi; memalsukan data pesaing; menyajikan ramalan sebagai fakta. Melapor ke GM + Owner (langsung). |
| `docs/AI_WORKFORCE_TRACEABILITY.md:87` | Nama worker: **RBI-W01…W12**; lini pelaporan **langsung GM + Owner** |
| `docs/AI_WORKFORCE_TRACEABILITY.md:12` | Status kode: **belum ada** (0 berkas) |
| `docs/STATUS_AND_GAP_MAP.md:216` | Komponen 6, kemajuan **⚠️ 0%** |
| `src/control_tower.py:63` | Rute sudah disiapkan: `REVENUE_BI` → `revenue_bi_ai` |
| `src/control_tower.py:75` | Peristiwa pemicunya sudah ada: `revenue.report.requested` |
| `docs/RESEARCH_AI_WORKFORCE_SKILLS_2026-10-06.md` §2 | Cakupan ~45%; **3 gap**: forecasting, anomaly detection, executive report |

**Kesimpulan wewenang (mengikuti pola 4 agen yang sudah dikirim):** Agent 6 adalah **agen baca-saja**. Ia menyusun angka, menandai anomali, dan mengusulkan perhatian manusia — ia tidak menulis tarif, tidak mengubah tagihan, tidak menyentuh uang. Batas itu harus ditegakkan **oleh platform**, bukan oleh niat baik modul (lihat §7).

---

## 3. Sumber kebenaran per angka (matriks nyata dari kode)

Aturan proyek: **satu angka, satu pemilik kebenaran.** Modul Agent 6 tidak boleh menyalin rumus milik modul lain — ia membacanya.

| Konsep | Tabel & kolom yang benar-benar ada | Pemilik kebenaran |
|---|---|---|
| Booking & masa inap | `bookings.check_in/check_out/nights/status/currency/total_minor/amount_paid_minor/confirmed_at/cancelled_at/property_id/unit_id` | `src/reservation_kernel.py`, `src/booking_states.py` (kosakata status) |
| Harga yang dijanjikan (quotation) | `quotes.accommodation_minor/extra_guest_minor/cleaning_minor/extras_minor/discount_minor/subtotal_minor/service_fee_minor/tax_minor/total_minor/rate_card_id/status/created_at/expires_at` | `src/quote_calculator.py` |
| Tarif resmi per malam | `rate_cards.name/version/effective_from/effective_to/unit_scope/weekly_discount_pct/monthly_discount_pct/approved_by`, `rate_seasons.nightly_rate_minor/min_stay_nights/start_date/end_date/priority` | `src/reservation_kernel.py` (tulisan tarif = manusia, ada `approved_by`) |
| Pendapatan terlaksana (per jenis baris) | `invoices.subtotal_minor/tax_minor/service_minor/total_minor/amount_paid_minor/status/payment_status/issued_at/due_at/voided_at`, `invoice_lines.kind` ∈ {`NIGHTLY`,`SURCHARGE`,`CLEANING`,`DISCOUNT`,`TAX`,`SERVICE`,`OTHER`}, `invoice_lines.amount_minor/nights/rate_card_id` | `src/invoicing.py` (`LINE_KINDS`) |
| Uang masuk/keluar & buku besar | `payments.provider/method/currency/amount_minor/status/settled_at` (status ∈ PENDING/SUCCEEDED/…/REFUNDED), `ledger_entries.account/direction/amount_minor/currency/occurred_at/entry_hash/prev_hash` | `src/payments.py` |
| Biaya supplier | `supplier_settlements.booking_id/route_id/action/amount_minor/currency/decided_by/actor_role/evidence_ref` | `src/supplier_settlements.py` |
| Funnel & kanal | `cases.channel` ∈ {`website`,`email`,`whatsapp_business`,`dashboard`,`phone`}, `channel_ref`, `first_responded_at`, `quoted_at`, `booking_id` | `src/cases.py` (`CASE_CHANNELS`), `CASE_CHANNELS` **tidak boleh dikarang ulang** |
| Inventori (denominator) | `units.status/created_at/unit_type/bedrooms`, `properties.status/region` | `src/villa_import.py` / kernel |
| Bukti ketersediaan (denominator alternatif) | `availability_evidence.unit_id/result/occupancy_ranges/can_quote/can_hold/can_confirm/captured_at/valid_until/source_type` | `src/availability.py` |
| **KPI funnel yang sudah hidup** | `reservation_kpi.collect(conn, *, agency_id, clock, at, window_days)` → `funnel`, `rates`, `sla`, `governance` | **Agent 3** — Agent 6 **memanggil**, tidak menyalin |

---

## 4. Kamus metrik v1 (definisi, rumus, kapan harus `None`)

Aturan yang berlaku untuk **semua** baris: hasil = turunan (bukan simpanan), idempoten, satu lintasan `SELECT`, tanpa satu pun kolom identitas tamu (aturan telemetri: **tidak ada data P2+ yang masuk ke metrik**).

| # | Metrik | Rumus (kolom nyata) | `None` bila | Catatan wajib |
|---|---|---|---|---|
| M1 | Okupansi | `booked_nights / available_nights` — pembilang dari `bookings` (status aktif, `nights`), penyebut dari inventori/bukti (§6 poin 2) | penyebut 0 | denominator = keputusan owner |
| M2 | ADR (harga rata-rata per malam) | `SUM(NIGHTLY) / booked_nights` pada `invoice_lines` | 0 malam | ADR v1 = **sebelum** diskon; net ADR menyusul setelah tanda `DISCOUNT` diverifikasi (§6 poin 7) |
| M3 | RevPAR | `SUM(NIGHTLY) / available_nights` | penyebut 0 | boleh lintas-booking, **tidak boleh lintas mata uang** |
| M4 | Pace / pickup | booking `confirmed_at` dalam 7/28 hari terakhir untuk `check_in` di masa depan (per minggu kedatangan) | tidak ada booking baru | ini *leading indicator*, bukan pendapatan |
| M5 | Lead time | `check_in − confirmed_at` (median + p90) | tidak ada sampel | sebaran, bukan rata-rata saja |
| M6 | ALOS | `SUM(nights) / COUNT(booking)` | 0 booking | — |
| M7 | Campuran kanal | `cases.channel` → `bookings` lewat `cases.booking_id`; sisanya = `UNKNOWN` | tidak ada booking | atribusi **hanya** lewat tautan resmi `cases.link_booking` (§6 poin 3) |
| M8 | Tingkat pembatalan | `cancelled_at IS NOT NULL / seluruh booking` pada jendela | 0 booking | pakai kosakata `booking_states`, jangan menebak status |
| M9 | Pendapatan neto tagihan | `SUM(invoices.total_minor)` untuk `voided_at IS NULL` − pembayaran `REFUNDED` | tidak ada invoice | **per mata uang**, tidak pernah dijumlahkan antar mata uang |
| M10 | Biaya supplier | `SUM(supplier_settlements.amount_minor)` per booking | tidak ada settlement | — |
| M11 | Margin (parsial) | M9 − M10, **dilabeli parsial** | tidak ada invoice | **tidak boleh disebut "profit"** — tanpa pembayaran ke pemilik (§6 poin 1) |
| M12 | Kedalaman diskon | `SUM(DISCOUNT)/SUM(subtotal)` | subtotal 0 | arah tanda diverifikasi dulu (§6 poin 7) |
| M13 | Sebaran tarif | `rate_seasons.nightly_rate_minor` min/median/max per properti-tipe | tidak ada musim | ini **bukan** intel pesaing (§6 poin 4) |
| M14 | Anomali | z-score / MAD atas deret harian metrik pilihan, jendela bergulir | sampel < 14 titik | statistik sederhana dulu, **bukan ML** |
| M15 | Ramalan | proyeksi + **interval keyakinan** (bootstrap) | titik observasi < ambang | wajib label "ramalan", bukan fakta |
| M16 | Laporan eksekutif | bundel M1–M15 + lineage (tabel + jendela + filter) | — | setiap angka wajib membawa asal-usulnya |

---

## 5. Peta worker: apa yang ada → apa yang dibangun

> **Temuan penting (harus Anda ketahui sebelum kode ditulis):** nama kanonik **RBI-W01…RBI-W12 tidak ada di repo ini**. Yang ada hanya (a) daftar 12 slot di `docs/AI_WORKFORCE_TRACEABILITY.md:87`, (b) parafrase 10 kemampuan di `docs/STATUS_AND_GAP_MAP.md:216`, dan (c) 5 nomor yang disebut `docs/RESEARCH_AI_WORKFORCE_SKILLS_2026-10-06.md` (W04 demand intel, W09 forecasting, W10 margin, W11 anomaly, W12 exec report). Tabel di bawah = **rekonstruksi dari ketiganya, bukan kutipan kanonik** — sama seperti worker OPS/FIN yang juga tidak dinamai di kode.

| Worker (rekonstruksi) | Yang sudah ada | Yang harus dibangun |
|---|---|---|
| W01 KPI pendapatan | `reservation_kpi.py` (funnel, rates, SLA, governance) | lapisan agregasi pendapatan (M1–M3, M6) |
| W02 Konversi | `reservation_kpi` rates + `cases` | konversi per kanal (M7 × funnel) |
| W03 Pace | data `confirmed_at`, `check_in` | kurva pickup (M4) + lead time (M5) |
| W04 Demand intel | `rate_seasons`, `bookings` | pola musiman + hari-dalam-minggu; **bukan** data pesaing |
| W05 Pricing intel | `rate_cards`, `rate_seasons`, `discounts.py` | sebaran tarif & kedalaman diskon (M12, M13) |
| W06–W08 Performa villa / mitra / kanal | `properties`, `units`, `supplier_settlements`, `cases.channel` | peringkat + penyebab di balik angka (bukan vonis) |
| W09 Forecasting | — | M15 dengan interval + ambang observasi |
| W10 Margin | `invoices`, `supplier_settlements` | M11 **parsial** + daftar celah data yang membuatnya parsial |
| W11 Anomaly | — | M14 (z-score/MAD) + daftar anomali ber-evidence |
| W12 Executive report | `shadow_eval.py` (pola ambang per KPI) | M16 + lineage setiap angka |

**Dua worker yang paling berisiko salah:** W09 (ramalan) dan W10 (margin). Keduanya adalah tempat paling mudah bagi sistem untuk **mengarang keyakinan** — karena itu keduanya dijadwalkan paling akhir, setelah angkanya bisa dipertanggungjawabkan.

---

## 6. Celah data yang harus diakui, bukan diakali

| # | Celah | Akibat | Yang **tidak boleh** dilakukan | Rekomendasi |
|---|---|---|---|---|
| 1 | Tidak ada kolom pembayaran ke pemilik vila | Margin tidak lengkap | menyebut M11 sebagai "profit"/"laba" | labeli **"margin parsial (pendapatan − biaya supplier tercatat)"**; tambahkan ADR bila bisnis ingin margin penuh |
| 2 | Denominator okupansi punya 2 kandidat sah (`units` vs `availability_evidence.occupancy_ranges`) | dua angka okupansi berbeda dari data yang sama | memilih diam-diam | **keputusan owner** (§10 Q1); v1 boleh hitung keduanya dengan label, keputusan menetapkan yang resmi |
| 3 | Tautan booking↔kanal (`cases.booking_id`) diisi manusia lewat `link_booking` | campuran kanal tidak lengkap | menebak kanal dari isi pesan | sisa masuk `UNKNOWN`, dan persen `UNKNOWN` ikut dilaporkan |
| 4 | Tidak ada data pesaing/pasar di sistem mana pun | W05 tidak bisa jadi pembanding pasar | mengarang angka pesaing (larangan dokumen) | W05 v1 = **internal saja**, dan dokumennya menyebut itu eksplisit |
| 5 | Multi-mata-uang (`currency` di mana-mana) | penjumlahan IDR+USD = angka palsu | menjumlahkan lintas mata uang | semua metrik uang **per mata uang**; total lintas mata uang hanya lewat FX resmi + tanggal |
| 6 | Belum ada KPI store ber-versi (§18) | tidak ada pemilik definisi target | menyebut "di bawah target" tanpa sumber target | v1 **tanpa** klaim target; definisi hidup di kode sebagai konstanta + `None` bila penyebut 0 |
| 7 | Tanda baris `DISCOUNT` di `invoice_lines` belum diverifikasi | net ADR/M12 bisa salah tanda | mengasumsikan negatif | **langkah pra-kode #1**: satu tes yang membaca sampel `DISCOUNT` + `derive_totals` dan memastikan arahnya |
| 8 | Aturan telemetri: tanpa data P2+ | metrik tidak boleh memuat identitas/pesan tamu | menaruh `guest_id`, isi percakapan, atau nama tamu di laporan | setiap field metrik = angka/jumlah/durasi/rasio saja |

---

## 7. Otoritas struktural yang akan dipakai (pola 4 agen sebelumnya)

| Kontrol | Bagaimana di Agent 6 | Diuji oleh |
|---|---|---|
| Baca-saja secara bentuk | modul hanya `SELECT`; laporan = turunan; tanpa `INSERT/UPDATE/DELETE` | tes: katalog baris masuk = keluar; hitung tabel sebelum/sesudah |
| Nama tool = **deklarasi, bukan pendaftaran** | `REVENUE_TOOLS` LOW/READ, tanpa `ensure_*` (ADR-0061 opsi B) | 4 penjaga di `tests/test_ai_gateway.py` + `test_control_tower.py` |
| Penolakan terverifikasi | `payment.approve` → `AI_DENIED_ACTION`; `ops_tasks.create` → `AI_UNKNOWN_TOOL` | tes per agen (pola OPS/FIN) |
| Kill switch & mode ladder | principal `revenue_bi_ai`; naik-turun mode lewat kontrol ber-pemegang (§17) | `tests/test_ai_gateway.py` |
| Rute & eskalasi | `revenue.report.requested` → `revenue_bi_ai`, rute tertutup §2A.3 | `tests/test_control_tower.py` |
| Ramalan ≠ fakta | setiap keluaran M15 wajib: interval + label + ambang observasi (pola ambang `shadow_eval.py`) | tes: tanpa cukup sampel → `None`, bukan angka |
| DoD §22 (5 bukti) | telemetri KPI, rute handoff, kill switch + reinstate, jejak observability, allowlist tool server-side | pola `tests/test_operations_agent.py::TestDefinitionOfDone` |

---

## 8. Rencana tes (RED lebih dulu)

1. **Sampel kosong** → semua metrik `None`/0 yang sah, tidak ada pembagian dengan nol yang meledak.
2. **Satu booking terkonfirmasi** → M1/M2/M3/M6 angka persis (dihitung tangan).
3. **Invoice `VOID`** tidak masuk M9; **`REFUNDED`** mengurangi M9.
4. **Dua mata uang** dalam satu jendela → dua baris, bukan satu jumlah.
5. **Booking tanpa tautan kasus** → M7 `UNKNOWN` bertambah, bukan ditebak.
6. **`DISCOUNT`** terbaca dengan arah yang benar → M12 punya tanda yang benar (langkah pra-kode #1).
7. **Anomali**: deret datar → tidak ada anomali; satu lonjakan sintetis → tepat satu anomali terdeteksi, dengan skor.
8. **Ramalan dengan sampel kurang** → `None` + alasan; dengan sampel cukup → angka **+ interval** yang memuat nilai observasi terakhir.
9. **Tidak menulis**: seluruh tabel katalog & fakta tidak berubah setelah `collect()`, dipanggil dua kali → hasil identik (idempoten).
10. **Tidak ada P2+**: setiap field keluaran bukan referensi orang (dicek atas kunci yang dikenal: `guest_id`, `guest_ref`, `channel_ref`, isi pesan).
11. **Penolakan tool**: `payment.approve` → `AI_DENIED_ACTION`; `ops_tasks.create` → `AI_UNKNOWN_TOOL`; deklarasi tidak masuk katalog (4 penjaga ADR-0061 tetap hijau).
12. **Exec report ber-lineage**: setiap angka membawa tabel + jendela + filter; tidak ada angka tanpa asal-usul.

---

## 9. Urutan kerja & perkiraan usaha

| Slice | Isi | Perkiraan |
|---|---|---|
| **0. Pra-kode** | verifikasi tanda `DISCOUNT`; keputusan owner Q1 (denominator okupansi) & Q5 (nama worker) | 1–2 jam |
| **1. Inti RBI** | kerangka modul baca-saja + M1–M3, M6, M8, M9 (per mata uang) + tes 1–5, 9, 11 | ~1 hari |
| **2. Funnel & kanal** | M4, M5, M7, M12, M13 (memanggil `reservation_kpi`, bukan menyalin) + tes 6 | ~0,5 hari |
| **3. Anomali & laporan** | M14, M16 + lineage + tes 7, 12 | ~1 hari |
| **4. Ramalan** | M15 + interval + ambang + tes 8 | ~0,5 hari |
| **5. Margin (parsial)** | M10, M11 + label + daftar celah | ~0,5 hari |

**Total ~4 hari kerja** — sejalan dengan perkiraan riset sebelumnya ("sedang-besar, 3–4 hari"), dan **lebih kecil** karena 45% fondasinya sudah ada.

---

## 10. Pertanyaan terbuka untuk owner (keputusan-ready)

| # | Pertanyaan | Opsi | Dampak bila tidak dijawab |
|---|---|---|---|
| **Q1** | Denominator okupansi memakai apa? | (a) daftar unit aktif × malam jendela; (b) bukti ketersediaan `availability_evidence` | dua angka okupansi berbeda terbit bersamaan |
| **Q2** | Margin v1 boleh dilabeli "parsial" saja, atau tunggu kolom pembayaran pemilik? | (a) parsial sekarang; (b) tunda W10 | W10 ditunda → laporan eksekutif tanpa sisi biaya |
| **Q3** | Rambu pemilik: "ramalan" cukup dengan interval, atau wajib penandaan visual berbeda di laporan? | (a) interval + label teks; (b) label + warna/ikon khusus | risiko ramalan dibaca sebagai fakta |
| **Q4** | Ambang minimum observasi untuk ramalan (14/28/56 hari)? | (a) 28 hari; (b) 56 hari | terlalu rendah = ramalan goyah; terlalu tinggi = lama tidak ada ramalan |
| **Q5** | Nama kanonik RBI-W01…W12 ada di dokumen AI Workforce V1.1 Anda? | (a) kirim daftarnya; (b) bangun per kemampuan tanpa nomor W | nama worker di kode tidak akan cocok dengan dokumen Anda |

---

## 11. Falsifikasi diri — apa yang bisa membatalkan riset ini

1. **Kalau `cases.booking_id` ternyata tidak pernah terisi di data nyata**, M7 kehilangan dasar; campuran kanal harus bergeser ke "kanal inkuiri" (lead-only) dan itu harus dikatakan di laporan.
2. **Kalau `invoice_lines.kind='NIGHTLY'` tidak dipakai konsisten** oleh `derive_totals` (mis. harga kamar masuk sebagai `OTHER`), maka M2/M3 harus dihitung dari `quotes` + `bookings` — dan itu **mengubah sumber kebenaran**, jadi butuh ADR kecil, bukan tambalan.
3. **Kalau sampel data demo/tenant kosong**, semua metrik benar tetapi tidak informatif; riset ini tidak boleh disimpulkan "sudah terbukti berguna" sampai ada data nyata — standar bukti proyek menuntut n ≥ 30 dan jendela bergulir.
4. **Kalau Anda memilih denominator bukti ketersediaan (Q1b)**, biaya slice 1 naik (perlu memetakan `occupancy_ranges` dan masa berlaku bukti) dan sebagian angka historis menjadi `None` — itu konsekuensi yang harus diterima, bukan diakali.

**Yang akan membuat saya mengganti rekomendasi:** bukti bahwa `availability_evidence` kosong atau jarang terisi di data nyata → maka Q1 jatuh ke (a) inventori, dan riset ini harus diperbarui, bukan dipertahankan.

---

*Laporan ini tidak mengubah satu baris kode pun. Langkah berikutnya menunggu keputusan Anda pada §10 (Q1 dan Q5 adalah dua yang memblokir slice 1).*
