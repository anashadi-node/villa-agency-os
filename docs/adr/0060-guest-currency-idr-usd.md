# ADR-0060 — Default mata uang tamu: IDR/USD disimpulkan dari negara asal tamu, hanya tampilan

- Status: Accepted
- Date: 2026-10-06
- Deciders: platform + Mas Anas

Bagian dari: permintaan fitur currency otomatis (tamu Indonesia → IDR, luar Indonesia → USD);
melengkapi ADR-0036 (uang keluar lewat tangan manusia) dan ADR-0011 (bahasa produksi en-US).
Dokumen pendukung HTML: `docs/currency-idr-usd-2026-10-06.html`.

## Konteks

Estate melayani tamu Indonesia dan internasional. Tim internal ingin tahu sejak awal kasus
mata uang mana yang tamu harapkan dipakai, tanpa membuka CRM. Platform hari ini **tidak
menampilkan harga sama sekali** di situs marketing — setiap kutipan dikonfirmasi dan dikirim
manusia. Fitur yang diminta hanyalah: simpulkan currency default tamu dari lokasi yang
terlihat saat inquiry masuk, simpan di tamu, tampilkan ke staf.

## Keputusan

1. **Dua mata uang, tidak lebih.** Kosakata resmi `fx.GUEST_CURRENCIES = ("IDR", "USD")`.
   Tidak ada currency ketiga; kolom memakai CHECK constraint dua nilai, dan service
   `crm.register_guest(preferred_currency=...)` menolak di luar itu.
2. **Pemetaan murni, gagal-terbuka ke USD.** `fx.infer_guest_currency(country)` mengembalikan
   `IDR` hanya untuk `"ID"` (ISO-3166 alpha-2, case-insensitive, di-trim); nilai lain —
   negara lain, kosong, `None`, sampah dari header tak tepercaya — mengembalikan `USD`.
   Fungsi tidak pernah melempar: lokasi tebakan adalah kosmetik dan tidak boleh memblokir
   inquiry (berbeda dari anti-abuse lain di situs yang justru fail-closed).
3. **Sumber hint: header tepi, bukan basis data geo.** `MarketingIntake.submit` menerima
   `country_hint` opsional; handler HTTP meneruskan header `CF-IPCountry` apa adanya.
   Header ditulis oleh edge tepercaya (cloudflare), bukan diklaim dari IP oleh aplikasi.
4. **Hanya tamu baru.** Hint dipakai saat `register_guest` membuat tamu; tamu yang sudah
   ada tidak pernah ditulis ulang karena inquiry kedua datang dari negara berbeda.
5. **Tampilan, bukan harga.** `guests.preferred_currency` adalah default tampilan untuk
   staf (badge "Guest's preferred currency" di detail kasus dashboard). Ia **tidak pernah**
   menentukan currency kutipan — `quote_calculator`'s `policy.currency` tetap satu-satunya
   otoritas — dan tidak pernah memicu pergerakan uang (ADR-0036 tetap berlaku).
6. **Skema.** Kolom baru `guests.preferred_currency TEXT NOT NULL DEFAULT 'USD' CHECK
   (preferred_currency IN ('IDR','USD'))` di `001_initial.sql`; database yang sudah ada
   dimutasi lewat migrasi `016_guest_preferred_currency.sql` (DEFAULT membuat semua baris
   lama valid tanpa menebak negara mereka). Twin PostgreSQL diregenerasi dari skema.

## Alternatif ditolak

| Alternatif | Kenapa |
|---|---|
| Deteksi currency dari IP di aplikasi | Menambah dependensi geo-IP dan klaim akurasi palsu; header tepi sudah membawa negara tanpa biaya |
| Lebih dari dua currency (EUR, AUD, …) | Menjanjikan harga dalam currency yang tidak pernah dikutip; menyebarkan asumsi kurs ke permukaan staf |
| Simpan currency di kasus, bukan tamu | Currency adalah preferensi orang, bukan properti kasus; kasus kedua tamu sama harus mewarisi default yang sama |
| Terjemahkan seluruh situs untuk tamu ID | Lingkup berbeda (copy, bukan currency); ditangani jalur locale ADR-0011, bukan di sini |
| Tulis ulang preferred_currency tamu lama saat inquiry baru | Merusak catatan yang sudah benar; upsert tidak boleh menimpa data orang |

## Konsekuensi

- Tim staf melihat harapan currency tamu sejak kasus terbuka — tidak ada langkah lookup.
- Inquiry tidak pernah ditolak karena lokasi tidak diketahui; semuanya default ke USD.
- Jika suatu hari platform benar-benar mengutip dalam IDR, keputusan itu adalah perubahan
  `quote_calculator` + ADR-0036 alignment, bukan kolom ini.
