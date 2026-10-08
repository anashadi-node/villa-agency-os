# ADR-0066 — Dataset demo di-anchor saat seed, bukan tanggal kalender beku

- Status: **ACCEPTED** (keputusan owner 2026-10-08: pilih "anchor saat seed + tes anti-rot")
- Date: 2026-10-08
- Deciders: Mas Anas (owner) · divisi engineering Villa Agency OS
- Spec: §10 (gerbang kualitas) · ADR-0020 (masa berlaku hold/bukti) · ADR-0065 (hint & copy publik) ·
  `scripts/seed_demo.py` · `tests/test_seed_demo_cli.py`

## Konteks

`scripts/seed_demo.py` menulis tanggal **beku** ke seluruh dataset contoh: konstanta
`NOW="2026-09-21"`, `LAST_WEEK="2026-09-14"`, `NEXT_WEEK="2026-09-28"`, stay hold `2026-10-12`,
booking `2026-10-20`, `approval_requests.expires_at="2026-09-28"`.

Baris-baris itu punya **masa berlaku**, dan fitur publik/produk membaca masa berlaku itu terhadap jam
nyata: `PublicReadModel.availability_hint` hanya memakai bukti dengan `valid_until > now`, queue
approval hanya menampilkan keputusan yang belum `expires_at`, dan SLA dihitung dari `quote_due_at`.

## Bukti yang diukur (2026-10-08)

| Pemeriksaan di database demo yang baru di-seed | Hasil |
| --- | --- |
| `availability_evidence.valid_until > now` | **0 dari 4** — semua kedaluwarsa |
| Teks hint yang benar-benar tampil di 3 halaman villa | **kalimat netral semuanya**, padahal `result='AVAILABLE'`, `evidence_level='A'`, `can_confirm=1` |
| `approval_requests.expires_at` | 2026-09-28 (sudah lewat → keputusan pending kedaluwarsa) |
| `cases.quote_due_at` | 2026-09-28 (sudah lewat → SLA terbaca bocor sejak detik pertama) |

Arah kegagalannya yang berbahaya: membusuk ke arah **menyembunyikan fitur yang sudah jalan**. Demo
yang selalu menampilkan state kosong membuat fitur yang sudah dibangun terbaca "belum ada" — persis
risiko yang ADR-0065 hindari untuk copy publik.

## Keputusan

1. **Satu anchor: waktu seed.** `ANCHOR = datetime.now(timezone.utc)` (dipotong ke jam), dan seluruh
   tanggal sensitif waktu dinyatakan sebagai offset dari anchor lewat `_at(days, hours)` (stamp) dan
   `_day(days)` (kolom DATE). `NOW`, `DAY`, `YESTERDAY`, `LAST_WEEK`, `NEXT_WEEK` adalah turunan
   anchor, bukan literal.
2. **Narasi demo tidak berubah.** Offsettnya persis sama seperti sebelumnya: hold kedaluwarsa 6 jam,
   invoice lewat jatuh tempo kemarin, kasus due besok, keputusan pending seminggu lagi, stay 3 malam
   di +21 hari (hold) dan +29 hari (booking). Yang berubah hanya titik nolnya.
3. **Aturan untuk baris baru:** setiap kolom waktu yang **dibaca terhadap jam nyata** wajib lewat
   `_at()`/`_day()`. Tanggal yang hanya cerita (mis. `policy_versions.effective_from`) boleh tetap,
   karena ia tidak pernah dibandingkan dengan `now`.
4. **Tes anti-rot menjaga aturannya**, bukan niatnya: `tests/test_seed_demo_cli.py` menegaskan, pada
   dataset yang baru di-seed dan terhadap **jam nyata**: ada bukti ketersediaan yang masih valid, ada
   keputusan pending yang belum kedaluwarsa, ada kasus yang belum lewat `quote_due_at`, **dan** lewat
   konsumen aslinya (`PublicReadModel.availability_hint`) minimal satu villa tidak jatuh ke kalimat
   netral. Tes ini gagal pada seeder versi tanggal beku (terbukti: 0 baris valid).
5. **Teks pesan tamu tidak memuat tanggal absolut.** Kalimat tamu memakai frasa relatif
   ("minggu depan", "bulan depan") atau tanggal yang diambil dari konstanta stay, sehingga pesan tidak
   pernah bertentangan dengan booking yang di-seed.

## Konsekuensi

- Demo bisa di-seed kapan saja dan tetap memperlihatkan fitur yang dibangun: hint ketersediaan,
  queue approval, SLA, dan hold "expiring" semuanya hidup pada saat dibuka.
- Dataset demo tidak lagi bisa dipakai sebagai referensi tanggal absolut. Tidak ada yang boleh
  mengandalkan tanggal spesifik dari seed (dan memang tidak ada tes yang melakukannya).
- Satu tes menahan regresi: menambahkan baris demo baru dengan tanggal keras dan masa berlaku akan
  ketahuan di gerbang, bukan di depan pembeli.

## Reversibility

Mengembalikan ke kapsul waktu = menyetel `ANCHOR` ke konstanta tetap. Struktur offset tetap, jadi
perubahannya satu baris — tetapi tes anti-rot akan gagal, dengan sengaja.
