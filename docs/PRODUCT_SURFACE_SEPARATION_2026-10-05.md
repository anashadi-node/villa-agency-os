# Villa Agency OS — Pemisahan Produk Website Marketing vs Dashboard Internal

Tanggal: 2026-10-05 WIB  
Status: **arah produk wajib** untuk Bali + Yogyakarta  
Sumber riset: Elite Havens, OriVista, The Luxury Bali, Manta Villas, Bohemian Jogja Villas + audit repo `~/villa-agency-os`

---

## 1. Koreksi arah: dua produk berbeda, tidak boleh dicampur

Mas Anas benar: **Website Marketing untuk konsumen** dan **Dashboard Manajemen/Karyawan** adalah dua permukaan yang kebutuhan, bahasa, desain, risiko, dan metriknya berbeda hampir 180 derajat.

| Permukaan | Pemakai | Tujuan utama | Bahasa desain | Risiko terbesar | Metrik sukses |
|---|---|---|---|---|---|
| **Website Marketing Villa** | calon tamu, keluarga, group, wedding/event planner, digital nomad, traveler luar negeri | menjual pengalaman menginap, membuat inquiry/booking masuk | emosional, foto besar, mewah, Bali/Jogja, cepat dipahami | klaim berlebihan, harga/ketersediaan tidak sinkron, UX buruk → lead hilang | inquiry qualified, conversion to quote, booking request, WhatsApp click, SEO organic |
| **Dashboard Internal Agency** | owner, GM, reservation, finance, ops, housekeeping, auditor | mengendalikan operasi agar tidak double booking, uang rapi, approval jelas, audit kuat | utilitarian, data padat, permissioned, evidence-first | salah status, salah uang, data lintas tenant, approval bypass | 0 double booking, rekonsiliasi ≥99%, SLA, approval latency, audit integrity |

**Keputusan produk:** keduanya harus **dipisah sebagai aplikasi/area berbeda**. Boleh berbagi kernel data/API, tetapi **tidak boleh berbagi UX, role, narasi, atau tujuan halaman**.

---

## 2. Bukti riset ringkas dari website serupa

| Sumber | Yang terlihat | Pelajaran untuk Villa Agency OS |
|---|---|---|
| Elite Havens — Bali Luxury Villas | halaman destinasi Bali memecah area seperti Canggu, Jimbaran, Ubud, Uluwatu; listing menampilkan lokasi, kamar, kapasitas, fasilitas, promo; narasi “villa specialists” dan “Book Direct” | website konsumen harus menjual **destinasi + kurasi + kepercayaan**, bukan menunjukkan panel operasi |
| OriVista | hero “concierge-led hospitality”, 52 curated villas, rating 4,9, harga per malam, benefit direct booking/partner privileges | tampilkan kartu villa dengan harga awal, rating, kapasitas, lokasi, dan CTA WhatsApp/inquiry cepat |
| The Luxury Bali | hand-picked villas, fully staffed, private chef, daily housekeeping, concierge first-message-to-departure, best rate guarantee | posisi premium harus kuat: curated, staffed, concierge, direct booking, bukan “software dashboard” |
| Manta Villas | memisahkan dua audiens: rentals untuk tamu dan management untuk owner | bila website juga menjual layanan owner, buat jalur terpisah: **For Guests** vs **For Villa Owners** |
| Bohemian Jogja Villas | Jogja menekankan private pool, family/couple/group, lokasi Sleman/Kota, fasilitas konkret | untuk Yogyakarta, desain harus lebih hangat-Jawa: private pool, keluarga, retreat, dekat kota/wisata, bukan beach-only Bali |

**Kesimpulan terfalsifikasi:** jika landing page menonjolkan audit, approval, dan dashboard internal di depan calon tamu, conversion kemungkinan turun karena tamu mencari villa, harga, foto, fasilitas, lokasi, dan rasa percaya. Dashboard internal tetap penting, tetapi untuk staf/owner setelah login.

---

## 3. Arsitektur produk yang benar

```text
PUBLIC INTERNET
  |
  |-- marketing.villa-agency.example
  |     Pemakai: tamu/calon konsumen
  |     Fungsi: katalog, destinasi, villa detail, inquiry, booking request, trust, SEO
  |     Auth: tidak wajib login untuk browsing/inquiry
  |     Tone: hospitality, luxury, Bali + Yogyakarta
  |
  |-- owner.villa-agency.example (opsional portal owner ringan)
  |     Pemakai: villa owner/property owner
  |     Fungsi: laporan owner, onboarding dokumen, payout summary
  |     Auth: wajib
  |
  |-- dashboard.villa-agency.example
        Pemakai: karyawan/management/internal
        Fungsi: cases, approvals, finance, operations, channels, shadow AI, audit
        Auth: wajib kuat; role-based; no public SEO

SHARED BACKEND / KERNEL
  reservation_kernel, policy_engine, matching, quote_calculator, payments,
  approvals, audit, cases, connectors, observability
```

**Aturan batas:**
1. Website Marketing boleh **membuat inquiry / booking request**, tetapi tidak boleh langsung mengubah booking final tanpa kernel + policy + human/approved workflow.
2. Dashboard Internal boleh mengelola operasi, tetapi tidak boleh menjadi wajah publik untuk calon tamu.
3. Data yang sama boleh dibaca, tetapi representasi berbeda: tamu melihat “Villa Ubud 4BR · private chef”; staf melihat case ID, evidence freshness, SLA, approval, policy snapshot.
4. Public SEO dan internal audit dipisah total: no `noindex` untuk marketing; wajib `noindex`/auth untuk dashboard.

---

## 4. Website Marketing — struktur yang seharusnya dibangun

### 4.1 Tujuan
Menjual villa-villa Bali dan Yogyakarta secara indah, cepat dipercaya, dan mudah mengubah minat menjadi inquiry/booking.

### 4.2 Halaman utama
| Halaman | Fungsi | Isi wajib |
|---|---|---|
| Home | impression pertama | hero foto/ilustrasi villa, search tanggal-lokasi-kapasitas, trust badges, area Bali/Jogja, curated villas, concierge promise |
| Villas Collection | katalog | filter lokasi, tanggal, bedrooms, guests, pool, family, event, price range |
| Destination Bali | SEO + browsing | Canggu, Ubud, Uluwatu, Seminyak, Jimbaran, Sanur, Nusa Dua; mood tiap area |
| Destination Yogyakarta | SEO + browsing | Kaliurang/Sleman, Kotagede, Prawirotaman, Bantul, dekat Malioboro, family/private pool/retreat |
| Villa Detail | conversion | galeri besar, kapasitas, kamar, amenity, rate from, availability hint, house rules, map area, FAQ, inquiry CTA |
| Experiences | upsell | airport transfer, private chef, spa, yoga, temple tour, sunrise, batik/culture, driver |
| Concierge / Trip Designer | trust + lead capture | form preferensi: tanggal, tamu, acara, budget, vibe |
| For Villa Owners | supply acquisition | layanan management/marketing villa, revenue reporting, compliance, channel ops — **bukan dashboard internal** |
| About / Trust | credibility | local experts, direct booking, policies, support, proof |
| Contact / WhatsApp | lead capture | WhatsApp resmi, email, response SLA, office area |

### 4.3 Komponen UX wajib
- Search bar: destination, check-in/out, guests, bedrooms, budget.
- Kartu villa: foto, area, bedrooms, guests, pool, “from price”, highlight, CTA.
- Area cards: Bali beach/cliff/jungle; Jogja heritage/private-pool/family-retreat.
- Trust strip: curated, verified availability, direct booking, concierge, secure payment.
- Lead form ringan: tanggal + jumlah tamu + vibe + budget; jangan paksa akun.
- WhatsApp CTA jelas, tetapi tetap direkam sebagai case di backend.

### 4.4 Arah visual Bali + Yogyakarta
- **Bali:** sandstone, frangipani, teak green, sunset gold, terracotta, ocean/cliff/rice-field language.
- **Yogyakarta:** batik parang geometry halus, warm clay, joglo silhouette, merapi mist, heritage serif, family-retreat tone.
- Tetap modern: white space besar, kartu rounded, foto/ilustrasi kuat, typography premium.
- Jangan menaruh tabel audit/approval di website tamu.

---

## 5. Dashboard Internal — struktur yang benar

Dashboard yang sudah ada tetap penting, tetapi posisinya adalah **internal operating system**, bukan website marketing.

| Area dashboard | Pemakai | Kegunaan |
|---|---|---|
| Command Center | owner/manager | status global, risiko, SLA, health |
| Cases | reservation | inquiry masuk, timeline, next action |
| Case Detail | reservation/manager | satu sumber kebenaran per tamu/case |
| Inventory Calendar | reservation/ops | tanggal kosong/hold/booked/maintenance |
| Finance | finance/owner | invoice, ledger, reconciliation, payout |
| Operations | ops/housekeeping | task pre-arrival, in-stay, post-stay |
| Approvals | manager/owner/finance | uang, legal, discount, exception |
| Properties | admin/owner | villa, unit, policy, contract pack |
| Channels | reservation/ops | inbound/outbound email/WA/web, draft approved-not-sent |
| AI Quality / Shadow | owner/auditor/ops | kualitas usulan AI vs keputusan manusia |
| Audit Chain | auditor/owner | bukti append-only, anchor, verification |

**Prinsip dashboard:** dense, role-based, token/auth wajib, no SEO, semua angka punya sumber, semua aksi sensitif butuh approval/audit.

---

## 6. Model data/API pemisah

| Domain | Public website boleh | Dashboard internal boleh |
|---|---|---|
| Villa | baca listing publik yang sellable | CRUD/update policy sesuai role |
| Availability | baca hint/availability request | baca evidence freshness, hold, conflict, maintenance |
| Inquiry | buat inquiry/case awal | review, merge, assign, escalate |
| Quote | minta quote / lihat quote approved | calculate, approve exception, send grounded draft |
| Payment | redirect hosted PSP | reconciliation, refunds, ledger, approvals |
| Message | kirim form/WA inbound | draft, approve, send via official connector |
| AI | trip-preference assist yang tidak menetapkan fakta | shadow eval, proposal review, no unsupported claims |

---

## 7. Roadmap implementasi tanpa mengacaukan kernel

1. **Hari ini — pisahkan dokumen dan IA.** Dokumen ini menjadi rujukan: marketing ≠ dashboard.
2. **Marketing prototype.** Buat landing/katalog demo Bali+Jogja yang indah, read-only, tanpa menyentuh booking kernel.
3. **Public API read model.** Export villa cards publik dari data yang sudah lolos sellability gate.
4. **Inquiry intake.** Form website menulis `case` via adapter resmi, bukan langsung booking.
5. **Villa detail + availability request.** Availability ditampilkan sebagai request/hint sampai kernel membuktikan freshness.
6. **Owner/services page.** Jalur pemilik villa dipisahkan dari jalur tamu.
7. **Dashboard tetap internal.** Lanjut KPI dashboard/outbox/PG/production tanpa mencampur UI publik.

---

## 8. Definition of Done untuk pemisahan ini

- Tidak ada dokumen utama yang menyebut dashboard internal sebagai website tamu.
- Marketing website punya IA sendiri: home, collection, destination, villa detail, inquiry.
- Dashboard internal tetap role-based dan tidak dijual sebagai public villa storefront.
- Lead dari marketing masuk sebagai case yang dapat diaudit.
- Semua klaim villa publik punya sumber: property master, contract pack, rate/availability policy.
- Bahasa visual Bali+Yogyakarta terasa hospitality, bukan SaaS generik.
- Test/gate tetap hijau: secret scan, unsafe scan, `make check` sebelum commit.

---

## 9. Keputusan desain yang saya rekomendasikan

**Gunakan dua gaya visual:**
1. **Marketing website:** Airbnb-like photography cards + Apple-like cinematic hero + Bali/Jogja local motifs. Target: tamu tertarik dan percaya.
2. **Dashboard internal:** Linear-like dark/dense operational control. Target: staf cepat mengambil keputusan aman.

Dengan begitu produk terasa premium di depan tamu, tetapi tetap kuat dan audit-ready di belakang layar.
