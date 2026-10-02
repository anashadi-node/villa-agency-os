# Panduan Setelan Tenant — Villa Agency OS

**Untuk:** setiap Villa Agency yang memakai sistem ini (bukan pemilik platform)
**Edisi:** 2026-10-02
**Sumber:** `src/settings.py`, `src/villa_import.py`, `docs/GO_LIVE_DECISIONS.md` (D17)

Dokumen ini menjawab satu pertanyaan: **apa yang harus SAYA (agency) siapkan sebelum sistem ini
dipakai dengan tamu dan uang nyata?** Semua isian di bawah dikerjakan oleh agency masing-masing —
platform hanya menyediakan tempatnya. Aturan, template pesan, dan pengaman sistem sudah disiapkan
platform dan **tidak perlu** Anda isi.

---

## 1. Kapan agency boleh go-live (pemeriksa otomatis)

Platform menolak go-live agency yang belum siap, dan **menyebutkan yang kurang**. Pemeriksanya ada
dua tempat: `preflight` (pemeriksa sebelum deploy) dan `AgencySettings.readiness()` (dipakai layar
Kesiapan Tenant).

| Yang diperiksa | Kenapa |
|---|---|
| 5 setelan wajib terisi (daftar di bab 2) | Tanpa ini, pesan tidak bisa terkirim, pajak tidak bisa dihitung, alarm tidak punya penerima |
| Ada **≥ 1 properti berstatus `SELLABLE`** | Agency tanpa villa yang bisa dijual belum bisa menjual |
| Ada **≥ 1 unit berstatus `ACTIVE`** | Villa tanpa unit tidak bisa dipesan |
| **Tangga uang punya pemegang**: `finance_manager`, `gm`, `owner` masing-masing punya orang | Tanpa pemegang, tidak ada yang bisa menyetujui pembayaran |

Contoh penolakan nyata (dari `preflight` pada database yang belum diisi):

```
FAIL  tenant readiness   agency_default: required setting not filled: messaging.whatsapp_number;
                         ...; no property in SELLABLE state; no unit in ACTIVE state
```

---

## 2. Setelan yang Anda isi

Katalog setelan **tertutup**: kunci di luar daftar ini ditolak (supaya Anda tidak mengisi kolom yang
tidak dibaca sistem mana pun). Tanda ✅ = wajib sebelum go-live.

| Kunci | ✅ | Isinya | Contoh |
|---|---|---|---|
| `payment.provider` | ✅ | Nama penyedia pembayaran yang Anda pakai | `midtrans` |
| `payment.rails` | ✅ | Rel yang Anda aktifkan (JSON list) | `["CARD","BANK_TRANSFER"]` |
| `payment.hosted_page` | — | Apakah memakai hosted page penyedia (sistem tidak menyimpan data kartu) | `true` |
| `messaging.whatsapp_number` | ✅ | Nomor WhatsApp Business **milik agency Anda** | `+628123456789` |
| `messaging.feedback_channel` | — | Kanal umpan balik yang disebut ke tamu | `email` |
| `tax.pbjt_source` | ✅ | Dari mana tarif PBJT Anda diambil (per kabupaten/kota) | `peraturan daerah kabupaten Badung` |
| `operations.oncall_primary` | ✅ | Yang wajib merespons alarm pada jam kerja | `Mr. TS` |
| `operations.oncall_escalation` | — | Ke siapa alarm naik bila penerima utama tidak merespons | `Mr. CEO` |
| `legal.review_weekday` | — | Hari tinjauan legal bulanan (GM/CEO + Owner) | `Jumat` |
| `retention.data_years` | — | Masa simpan data sebelum boleh dihapus (bawaan 7 tahun) | `7` |

**Aturan penulisan:**
1. Nilai divalidasi saat **disimpan** — nomor WhatsApp yang bukan nomor ditolak saat itu juga,
   bukan saat pesan tamu gagal terkirim.
2. Yang boleh mengubah: **Owner, GM/CEO, atau Technology-Security Manager** agency Anda. Peran lain
   (reservasi, finance, auditor) ditolak — mereka bukan pemilik setelan ini.
3. Setiap perubahan masuk **rantai audit** (siapa, kapan, nilai sebelum, nilai sesudah). Jadi
   "siapa yang mengganti nomor WhatsApp kita?" selalu bisa dijawab.
4. Setelan **tidak bisa** menimpa pengaman platform: daftar larangan AI, ambang approval, dan peran
   wajib MFA adalah milik pemilik platform, bukan tenant.

---

## 3. Daftar villa & unit: impor satu berkas (gelondongan)

Tidak perlu mengetik satu per satu. Satu berkas = seluruh daftar Anda.

### 3.1 Ambil kerangka berkas

```bash
python3 scripts/import_villas.py --template > villa-list.csv
```

Berkas itu berheader 16 kolom; **satu baris = satu unit**, kolom properti diulang. Kolom yang
boleh dikosongkan beserta bawaannya:

| Kolom | Wajib | Bawaan bila kosong |
|---|---|---|
| `property_id`, `property_name`, `region` | ✅ | — |
| `unit_id`, `unit_name`, `bedrooms`, `bathrooms`, `base_capacity`, `max_guests` | ✅ | — |
| `property_type` | — | `villa` |
| `province` | — | `Bali` |
| `timezone` | — | `Asia/Makassar` |
| `property_status` | — | `SELLABLE` |
| `contract_pack_status` | — | `VERIFIED` |
| `unit_type` | — | `entire_villa` |
| `unit_status` | — | `ACTIVE` |

Aturan penulisan id: huruf kecil, angka, tanda hubung atau garis bawah (3–64 karakter).

### 3.2 Dua langkah, dan langkah pertama tidak menulis apa pun

```bash
# 1. rencana saja (bawaan) - TIDAK menulis apa pun ke database
python3 scripts/import_villas.py --file villa-list.csv

# 2. baru menulis, bila rencananya bersih
VILLA_DB=data/villa.db python3 scripts/import_villas.py --file villa-list.csv \
    --agency agency_default --actor "Mr. Owner" --actor-role owner \
    --source-ref "villa-list-okt-2026.csv" --apply
```

**Yang perlu Anda tahu tentang keselamatannya:**

| Aturan | Artinya bagi Anda |
|---|---|
| **Dry run bawaan** | Salah tulis nama berkas → tidak terjadi apa-apa |
| **Satu baris salah = seluruh berkas ditolak**, dengan nomor baris & nama kolom | Tidak ada impor separuh: Anda selalu tahu persis isi database |
| **Impor ulang = memperbarui, bukan menggandakan** | Perbaiki berkas, jalankan lagi, aman |
| **Nama agency tidak bisa ditulis di berkas** | Berkas CSV tidak bisa menyusup ke data agency lain |
| **Semua baris masuk rantai audit**, dengan sidik jari berkasnya | "Berkas mana yang memasukkan villa ini?" bisa dijawab bertahun kemudian |
| **Yang boleh mengimpor:** Owner, GM/CEO, Technology-Security Manager, Reservation/Operations Manager | AI, finance, dan auditor tidak boleh |

### 3.3 Contoh berkas (2 unit di 1 villa)

```csv
property_id,property_name,property_type,region,province,timezone,property_status,contract_pack_status,unit_id,unit_name,unit_type,bedrooms,bathrooms,base_capacity,max_guests,unit_status
villa_ubud_01,Villa Ubud Satu,villa,Ubud,Bali,Asia/Makassar,SELLABLE,ACTIVATED,unit_ubud_01,Unit Utama,entire_villa,2,2,2,4,ACTIVE
villa_ubud_01,Villa Ubud Satu,villa,Ubud,Bali,Asia/Makassar,SELLABLE,ACTIVATED,unit_ubud_02,Unit Belakang,entire_villa,1,1,2,2,ACTIVE
```

Catatan: berkas `.xlsx` belum dibaca langsung — di Excel pilih **Save As → CSV UTF-8**, lalu impor
berkas itu.

---

## 4. Yang TIDAK perlu Anda isi (sudah disiapkan platform)

| Sudah disiapkan | Di mana |
|---|---|
| 12 template pesan WhatsApp/email dalam bahasa Indonesia | `templates/messages/id-ID.json` |
| Aturan keamanan: AI dilarang membayar, menyetujui, mengubah rekening | daftar larangan v3 (27 entri) |
| Tangga persetujuan uang: Finance Manager → GM/CEO, eksekusi oleh manusia | `finance.approval.payment_thresholds_minor` |
| Ambang SLA (balasan ≤ 15 menit) & jawaban bayar ≤ 2 jam | `src/cases.py` |
| Masa simpan 7 tahun & alur penghapusan ber-approval | D9 + `retention.data_years` (boleh Anda ubah) |

---

## 5. Peran & pemegangnya — boleh Anda ubah sendiri

Peran yang Anda pakai bisa Anda **beri nama sendiri, aktif/nonaktifkan, dan isi pemegangnya**.
Yang **tidak** berubah: tingkat wewenangnya. Wewenang selalu mengikuti **cetak biru platform**
(`based_on`), bukan nama yang Anda tulis. Jadi mengganti "Finance & Accounting Manager" menjadi
"Bagian Keuangan" **tidak** menaikkan atau menurunkan wewenangnya satu tingkat pun.

### 5.1 Perintah yang tersedia

```bash
# lihat daftar peran + siapa pemegangnya + apa yang belum distafkan
python3 scripts/manage_roles.py list

# mulai dari cetak biru platform (8 peran ADR-0035) - sekali saja
VILLA_DB=data/villa.db python3 scripts/manage_roles.py seed \
    --apply --actor "Mr. Owner" --actor-role owner

# beri nama sendiri sesuai istilah agency Anda
VILLA_DB=data/villa.db python3 scripts/manage_roles.py rename \
    --role finance_manager --label "Bagian Keuangan Pajak" \
    --apply --actor "Mr. Owner"

# tambah peran baru (wewenangnya mengikuti cetak biru yang Anda pilih)
VILLA_DB=data/villa.db python3 scripts/manage_roles.py add \
    --role finance_helper --label "Asisten Keuangan" --based-on finance_manager \
    --apply --actor "Mr. Owner"

# isi pemegang peran (rujukan, bukan nama orang)
VILLA_DB=data/villa.db python3 scripts/manage_roles.py appoint \
    --role gm --holder mr_ceo --apply --actor "Mr. Owner"

# nonaktifkan peran yang tidak dipakai (harus dikosongkan dulu pemegangnya)
VILLA_DB=data/villa.db python3 scripts/manage_roles.py disable \
    --role commercial_manager --apply --actor "Mr. Owner"
```

Tanpa `--apply`, semua perintah hanya **merencanakan** dan tidak menulis apa pun.

### 5.2 Aturan yang dipegang sistem (dan alasannya)

| Aturan | Artinya bagi Anda |
|---|---|
| **Nama bebas, wewenang tetap** | Mengganti nama peran tidak bisa menaikkan wewenang — wewenang dibaca dari cetak biru |
| **Peran pembuat AI tidak bisa diisi manusia** | Finance Staff & Reservation Agent dihapus owner (ADR-0035); fungsinya milik identitas AI, jadi tidak bisa Anda isi ke manusia |
| **Peran berbeban tidak bisa dinonaktifkan** | Kalau masih ada pemegang aktif, sistem menolak — supaya tidak ada orang yang tetap punya wewenang lewat peran yang Anda kira sudah mati |
| **Pemegang = rujukan, bukan nama orang** | Sistem hanya perlu tahu **bahwa** peran punya orang, bukan siapa. Jadi tidak ada nama staf yang disimpan (privasi) |
| **Yang boleh mengubah peran:** Owner, GM/CEO, Technology-Security Manager | Reservasi, finance, auditor, dan AI tidak boleh |
| **Setiap perubahan masuk rantai audit** | Siapa mengubah apa, kapan, nilai sebelum–sesudah |

### 5.3 Tangga uang wajib punya orang (bagian dari go-live)

Tiga peran ini **wajib punya pemegang aktif** sebelum agency Anda boleh go-live, karena tanpa
mereka **tidak ada yang bisa menyetujui pembayaran**:

| Peran | Kenapa |
|---|---|
| `finance_manager` | Penyetuju tingkat 1 untuk uang keluar (ADR-0036) |
| `gm` | Penyetuju tingkat 2 |
| `owner` | Pemilik agency; menerima laporan dan mengoreksi anomali |

`python3 scripts/manage_roles.py list` akan menutup dengan baris ini bila belum lengkap:

```
money ladder with no holder: finance_manager, gm, owner
go-live stays refused until each of them has a holder (decision D17)
```

---

*Dokumen ini ditulis dari kode dan dokumen repository pada 2026-10-02. Setiap angka di sini bisa
diminta diperiksa ulang dengan `make check` dan `scripts/preflight.py`.*
