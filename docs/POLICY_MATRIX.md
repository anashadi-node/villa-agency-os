# Policy Matrix — Apa yang Boleh Berbeda, dan Siapa yang Boleh Mengubahnya

Versi: 1.0 · Tanggal: 2026-09-21

Semua angka bisnis (harga, deposit, pajak, pembatalan) hidup di **satu objek
versioned**: `PolicyVersion` per properti (opsional per unit). Dokumen ini
memetakan setiap field → nilai default → batas guardrail → siapa yang boleh
mengubah. Field di sini **harus sama persis** dengan field di
`src/policy_engine.py`; kalau berbeda, kode yang benar dan dokumen harus diperbarui.

---

## 1. Identitas & masa berlaku

| Field | Tipe | Default | Guardrail | Boleh diubah oleh |
|---|---|---|---|---|
| `policy_version_id` | id | — | unik, immutable | sistem |
| `property_id` | id | — | harus ada di `properties` | sistem |
| `unit_scope` | `property` \| unit_id | `property` | unit harus milik properti | Reservation manager |
| `version` | int | 1 | naik monoton per properti | sistem |
| `effective_from` / `effective_to` | date | — | tidak boleh overlap dengan versi lain properti yang sama | Reservation manager |
| `currency` | ISO-4217 | IDR | hanya mata uang dengan minor unit terdefinisi | Owner + Finance |
| `source_doc_ref` | teks | — | wajib (file policy/rate card bertanggal) | Reservation manager |
| `approved_by` | teks | — | wajib untuk versi pertama | Reservation manager |

## 2. Deposit & jadwal pembayaran

| Field | Default | Guardrail | Catatan |
|---|---|---|---|
| `deposit_pct` | 50 | 0–100; 100 = wajib lunas | dasar perhitungan deposit |
| `full_payment_window_days` | 45 | ≥ 0 | booking dengan check-in < N hari ⇒ wajib lunas |
| `balance_due_days_before_arrival` | 45 | ≥ 1 | tenggat pelunasan |
| `deposit_confirms_booking` | false | — | jika true, deposit terverifikasi boleh mengonfirmasi booking |
| `security_deposit_minor` | 0 | ≥ 0 | refundable; **bukan** bagian dari pendapatan |
| `security_deposit_refund_days` | 7 | ≥ 0 | SLA pengembalian deposit kerusakan |
| `quote_valid_hours` | 24 | 1–168 | kedaluwarsa quote |
| `hold_ttl_minutes` | 120 | 15–1440 | lama hold inventaris |
| `max_hold_extensions` | 1 | 0–3 | perpanjangan hold harus eksplisit + audit |
| `payment_terms_text` | teks | wajib | kalimat yang dikirim ke tamu (bahasa manusia) |

## 3. Pembatalan & no-show

`cancellation_tiers`: daftar `(days_before_arrival_min, penalty_pct)` diurut menurun.
Tier yang berlaku = **tier pertama yang `days_before_arrival >= days_before_arrival_min`**.

| Contoh konfigurasi | Pemakaian |
|---|---|
| `[(60, 0), (30, 50), (0, 100)]` | Long-stay / high season |
| `[(30, 0), (7, 50), (0, 100)]` | Standar villa |
| `[(0, 100)]` | Non-refundable promo |

| Field | Default | Guardrail |
|---|---|---|
| `no_show_penalty_pct` | 100 | 0–100 |
| `force_majeure_clause` | teks wajib | harus menyebut minimal 1 skenario lokal + mekanisme refund/kredit |
| `blackout_dates` | [] | daftar tanggal tertutup; booking pada tanggal ini ditolak (`BLACKOUT_DATE`) |

## 4. Harga & biaya

| Field | Default | Guardrail | Catatan |
|---|---|---|---|
| `base_capacity` | 2 | ≥ 1 | tamu yang sudah termasuk dalam tarif |
| `max_guests` | 4 | ≥ base_capacity | melebihi ini ⇒ `OVER_CAPACITY` |
| `extra_guest_fee_minor_per_night` | 0 | ≥ 0 | per tamu di atas `base_capacity`, per malam |
| `cleaning_fee_minor` | 0 | ≥ 0 | sekali per stay |
| `min_stay_nights` | 2 | ≥ 1 | aturan minimum stay properti |
| `child_policy` / `pet_policy` / `event_policy` | teks | wajib | dikirim ke tamu apa adanya |
| `max_discount_pct` | 10 | 0–50 | guardrail diskon agen (kode promo tidak boleh melebihi) |

## 5. Pajak & fee agency

| Field | Default | Guardrail |
|---|---|---|
| `tax_pbjt_pct` | per kabupaten/kota | **≤ 10** (batas legal PBJT akomodasi) |
| `tax_applies_to` | `subtotal_plus_service` | salah satu `subtotal` / `subtotal_plus_service` |
| `service_fee_pct` | 10 | 0–25 |
| `tax_included_in_rate` | false | jika true, PBJT dikeluarkan (dihitung mundur) dari tarif agar tidak dobel |

> **Jangan hard-code tarif PBJT.** Bali dan Yogyakarta, bahkan antar
> kabupaten/kota, berbeda. Tarif disimpan per policy dan wajib punya dasar hukum bertanggal.

## 6. Inventory & ketersediaan

| Field | Default | Guardrail | Catatan |
|---|---|---|---|
| `availability_freshness_A_seconds` | 900 (15 mnt) | ≤ 3600 | API/booking engine langsung |
| `availability_freshness_B_seconds` | 86400 (24 j) | ≤ 172800 | tulisan resmi manajer properti |
| `availability_freshness_C_seconds` | 172800 (48 j) | ≤ 604800 | iCal/OTA publik (indikatif) |
| `confirm_requires_level` | `A_or_B` | tidak boleh `C`/`D` | level yang boleh mengonfirmasi |
| `channel_quota` | {} | — | alokasi unit per kanal |

Level bukti (dipakai konsisten di kode, dokumen, dan UI):

| Level | Sumber | Boleh di-quote? | Boleh di-hold? | Boleh dikonfirmasi? |
|---|---|---|---|---|
| A | API/booking engine properti | Ya | Ya | Ya (dalam freshness window) |
| B | Tulisan resmi manajer properti | Ya | Ya | Ya (dalam freshness window) |
| C | iCal/OTA publik | Ya (label "indicative") | Ya, sebagai hold sementara | **Tidak** |
| D | Memori lama / tanpa sumber | Tidak | Tidak | Tidak |

## 7. Check-in / check-out

| Field | Default | Guardrail |
|---|---|---|
| `check_in_time` | `14:00` | format HH:MM, zona properti |
| `check_out_time` | `12:00` | format HH:MM, zona properti |
| `property_timezone` | `Asia/Makassar` (Bali) / `Asia/Jakarta` (Yogyakarta) | harus zona IANA valid |
| `check_in_document_required` | true | false hanya untuk properti domestik tertentu |

## 8. Aturan perubahan policy (append-only)

1. Policy **tidak pernah diedit**. Perubahan = versi baru dengan `effective_from` baru.
2. Quote & booking menyimpan `policy_version_id` **dan** `policy_fingerprint`.
3. Booking aktif tetap memakai snapshot saat dibuat, walaupun policy baru terbit.
4. Perubahan berikut **wajib** approval manusia + audit: `tax_pbjt_pct`,
   `service_fee_pct`, `deposit_pct`, `cancellation_tiers`, `security_deposit_minor`,
   `currency`, `full_payment_window_days`.
5. Rentang `effective_from`–`effective_to` antar versi tidak boleh overlap —
   jika boleh overlap, satu tanggal punya dua harga: sumber double-booking finansial.
