# Kredit Foto — Wikimedia Commons (CC BY-SA / CC0)

Semua foto di `assets/photos/` bersumber dari Wikimedia Commons, berlisensi
bebas-pakai (termasuk komersial) dengan syarat atribusi untuk berkas CC BY-SA
(CC0 tidak mewajibkan atribusi, tetap dicantumkan demi transparansi).
Diunduh & dikompres 6 Oktober 2026. **Bukan foto properti riil yang dikelola
Villa Agency OS** — dipakai sebagai placeholder suasana sampai foto asli tiap
villa tersedia (lihat `docs/DESIGN_BENCHMARK_2026-10-06.md` §10).

## Hero (beranda)

| File lokal | Judul asli Wikimedia Commons | Lisensi | Sumber |
|---|---|---|---|
| `hero_bali_pool.jpg` (1600px) | Main swimming pool at Paradisus by Meliá Bali | CC BY-SA 4.0 | https://commons.wikimedia.org/wiki/File:Main_swimming_pool_at_Paradisus_by_Meli%C3%A1_Bali.jpg |

## Kartu & detail villa (`card/`, `detail/`)

| Kata kunci villa | Judul asli Wikimedia Commons | Lisensi | Sumber |
|---|---|---|---|
| `canggu` | The Reserve Garden Villa at Paradisus by Meliá Bali | CC BY-SA 4.0 | https://commons.wikimedia.org/wiki/File:The_Reserve_Garden_Villa_at_Paradisus_by_Meli%C3%A1_Bali.jpg |
| `jatiluwih` | Jatiluwih UNESCO Bali landscape cycling | CC BY-SA 4.0 | https://commons.wikimedia.org/wiki/File:Jatiluwih_UNESCO_Bali_landscape_cycling.jpg |
| `uluwatu` | Uluwatu cliff facing Indian ocean | CC BY-SA 4.0 | https://commons.wikimedia.org/wiki/File:Uluwatu_cliff_facing_Indian_ocean.jpg |
| `joglo` / `merapi` | Joglo Pencu, Rumah Tradisional Kudus | CC BY-SA 4.0 | https://commons.wikimedia.org/wiki/File:Joglo_Pencu,_Rumah_Tradisional_Kudus.jpg |

## Strip "Ambience" (halaman detail villa — `gallery/`)

| File lokal | Judul asli Wikimedia Commons | Lisensi | Sumber |
|---|---|---|---|
| `gallery/amb1_reserve_pool.jpg` | The Reserve Pool at Paradisus by Meliá Bali | CC BY-SA 4.0 | https://commons.wikimedia.org/wiki/File:The_Reserve_Pool_at_Paradisus_by_Meli%C3%A1_Bali.jpg |
| `gallery/amb2_private_pool_villa.jpg` | Private pool villa at Six Senses Krabey Island, Cambodia | CC BY-SA 4.0 | https://commons.wikimedia.org/wiki/File:Private_pool_villa_at_Six_Senses_Krabey_Island,_Cambodia.jpg |
| `gallery/amb3_pool_sunset.jpg` | Exotic swimming pool at sunset (Unsplash) | CC0 | https://commons.wikimedia.org/wiki/File:Exotic_swimming_pool_at_sunset_(Unsplash).jpg |

Strip ambience diberi label eksplisit "Illustrative resort imagery — not this
villa" di halaman, agar tidak disalahartikan sebagai foto properti yang dijual.

## Pemrosesan

Resize (hero 1600px, detail 1200px, kartu 640px, ambience 800px) + re-encode
JPEG q78–80 — kompresi teknis, bukan derivative kreatif; berkas tetap tunduk
lisensi asal. Disematkan ke HTML sebagai data-URI base64 (properti "single-file
response" modul marketing: tanpa route statis baru, tanpa permintaan keluar dari
halaman yang dirender) — lihat `src/marketing_site.py`.

## Catatan kepatuhan

Tidak ada foto yang diambil dari 5 situs kompetitor yang diaudit di
`docs/DESIGN_BENCHMARK_2026-10-06.md` — itu secara eksplisit **DITOLAK** karena
risiko hak cipta & misrepresentasi properti (lihat dokumen tsb §6 & §10).
Sebagai gantinya dipakai foto berlisensi bebas Wikimedia Commons.
