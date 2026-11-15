# ADR-0072 — Sesi browser: cookie adalah karier kedua untuk token yang sama

- Status: **PROPOSED** (menunggu persetujuan Mas Anas)
- Date: 2026-11-15
- Deciders: Mas Anas (owner) · divisi Villa Management & Agency
- Melengkapi: ADR-0004 (append-only evidence), ADR-0024 (header token sebagai gerbang), ADR-0032
  (policy sebagai data), ADR-0059 (pintu bertanda tangan), ADR-0069 (`CONFIRMED` hanya setelah
  bukti), ADR-0070 (rupiah adalah nilainya)
- Spec: `src/dashboard.py` (`SESSION_COOKIE`, `authenticate`, `_serve_unlock`, `_serve_lock`,
  `_set_cookie_header`) · `templates/unlock.html` · `tests/test_dashboard_session.py`

## Masalah

Dashboard internal dibuka dengan header `X-Dashboard-Token`. Model itu benar untuk mesin: satu
header, satu pemeriksaan, satu jawaban 401, tidak ada state di server. Tetapi **manusia memakai
browser**, dan browser tidak bisa diminta menempelkan header pada setiap klik, setiap muat ulang,
dan setiap pindah halaman. Pilihan yang tersisa semuanya buruk:

1. **Menyalin token ke setiap URL** (`?token=...`) — token masuk riwayat browser, log akses,
   header `Referer` saat keluar ke situs lain, dan screenshot saat berbagi layar. Ini bukan
   kemudahan; ini kebocoran yang dirancang sendiri.
2. **Meminta basic-auth** — browser menyimpan kredensial dan mengirimnya otomatis, tetapi
   `WWW-Authenticate` juga memunculkan dialog yang tidak bisa diberi pesan galat yang jelas,
   tidak bisa dilupakan atas permintaan pengguna, dan tidak membedakan "token salah" dari
   "server mati".
3. **Membuat sesi server-side (tabel sesi + ID acak)** — benar secara teori, tetapi menambah
   tabel, masa berlaku, penyapu sesi kedaluwarsa, dan satu tempat baru yang bisa salah. Untuk
   satu-laptop, satu-operator, dan satu peran di balik gerbang, biayanya tidak sebanding.
4. **Mematikan gerbangnya** — tidak dibahas lebih lanjut.

Yang dibutuhkan: cara bagi browser untuk memegang kredensial **setelah** pemilik token
menyerahkannya sekali, tanpa menambah jalur masuk baru yang lebih lemah daripada header.

## Keputusan

**Cookie adalah karier kedua untuk token yang sama, bukan sesi baru.**

1. **Nilai cookie = token itu sendiri.** Nama cookie `villa_dashboard`. Tidak ada ID sesi, tidak
   ada tabel sesi, tidak ada state di server. Karena itu tidak ada yang bisa "bocor dari tabel
   sesi" dan tidak ada penyapu kedaluwarsa yang perlu diawasi.
2. **`authenticate()` menerima tiga karier, memeriksa semuanya dengan aturan yang sama.**
   `X-Dashboard-Token` → `Authorization: Bearer` → cookie. Setiap kandidat dibandingkan dengan
   `secrets.compare_digest` terhadap daftar token yang sah. Kandidat buruk **tidak** diselamatkan
   kandidat baik, dan permintaan tanpa karier apa pun tetap
   `Unauthenticated("token dashboard tidak dikirim")` → 401. Model header tidak dilemahkan; ia
   hanya tidak lagi satu-satunya pintu.
3. **Satu halaman publik, satu form, satu POST.** `GET /unlock` menampilkan form; `POST /unlock`
   menukar token menjadi cookie. Itu satu-satunya rute yang bisa dijangkau tanpa token.
   `GET /unlock` selalu menjawab **200** — 401 akan memicu dialog basic-auth browser karena
   `_write` menambahkan `WWW-Authenticate` pada setiap 401, dan dialog itu menutupi pesan galat
   kita sendiri.
4. **Token salah tidak mengirim cookie apa pun.** Jawaban `POST /unlock` yang gagal adalah
   `303 → /unlock?e=1` dengan badan kosong: tidak ada `Set-Cookie`, tidak ada halaman yang
   dibaca, tidak ada teks yang memantulkan token yang baru diketik. Berhasil = `303 → /` plus
   satu `Set-Cookie`.
5. **Cookie tidak bisa dipakai untuk menyelundupkan header.** Nilai cookie divalidasi
   whitelist `[A-Za-z0-9._~+/=:-]` maksimum 256 byte; `max_age > 0` wajib nilai tidak kosong.
   Nilai yang tidak lolos **ditolak** (`InvalidInput("token cannot be carried in a cookie")`),
   bukan dibersihkan diam-diam — token yang karakternya dibuang akan menghasilkan cookie yang
   tidak pernah lolos autentikasi, dan pengguna akan melihat "token salah" untuk token yang benar.
6. **Atribut cookie:** `Path=/`, `Max-Age=43200` (12 jam), `HttpOnly`, `SameSite=Strict`.
   `HttpOnly` menutup pembacaan oleh skrip; `SameSite=Strict` menutup pengiriman lintas situs.
   **Tanpa `Secure`** secara sengaja: server ini bicara HTTP polos di loopback/LAN, dan browser
   membuang cookie `Secure` yang datang lewat HTTP — hasilnya penguncian senyap pada percobaan
   login yang sebenarnya benar.
7. **Tanpa JavaScript.** CSP `default-src 'none'` memblokir `<script>`, jadi alurnya memakai form
   POST + `Set-Cookie` + redirect HTTP biasa. `templates/unlock.html` berdiri sendiri (tidak
   mewarisi `base.html`) supaya tidak membawa navigasi dashboard ke halaman pra-autentikasi.
8. **`POST /lock` melupakan cookie di browser** (`Max-Age=0`, nilai kosong) lalu redirect ke
   `/unlock`. Rute ini **tidak** mencabut token — lihat Konsekuensi.

## Alasan

- **Satu rahasia, satu tempat pemeriksaan.** Karena cookie memuat token yang sama, tidak ada
  jalur otorisasi kedua yang harus dijaga konsisten dengan yang pertama. Menambah tabel sesi akan
  menciptakan dua definisi "siapa yang masuk" yang bisa menyimpang.
- **Fail-closed lebih jujur daripada pintar.** Menolak nilai cookie yang tidak aman membuat
  kegagalan terlihat sebagai kegagalan. Memperbaiki nilai diam-diam membuat kegagalan terlihat
  sebagai "token salah", yaitu kebohongan yang paling mahal di halaman login.
- **Tanpa `Secure` adalah pilihan yang dipaksakan topologi, bukan kelalaian.** Yang harus
  diperbaiki adalah topologinya (TLS di depan server), bukan atribut cookie yang membuat login
  mustahil di topologi sekarang.
- **12 jam = satu hari kerja.** Cukup panjang untuk tidak mengganggu, cukup pendek untuk tidak
  hidup sampai besok di laptop yang dipakai bergantian.
- **Browser butuh karier, bukan header baru.** Setiap alternatif (token di URL, basic-auth)
  memindahkan rahasia ke tempat yang lebih terlihat: riwayat, log, `Referer`, dialog browser.

## Konsekuensi

- **`/lock` bukan pencabutan.** Cookie memuat token itu sendiri, jadi salinan cookie yang sudah
  diambil tetap lolos autentikasi setelah `/lock`. Pencabutan yang sebenarnya adalah **rotasi
  token di server** (`~/.config/villa/dashboard.token` diganti, server dijalankan ulang). Test
  `test_lock_does_not_revoke_the_token_itself` mengunci perilaku ini supaya tidak ada yang
  "memperbaiki" `/lock` menjadi berpura-pura mencabut.
- **Token kini hidup di dua tempat di sisi klien** (header milik skrip, cookie milik browser).
  Rotasi token otomatis membatalkan keduanya; tidak ada daftar sesi untuk dibersihkan.
- **HTTP polos berarti token tetap terkirim tanpa enkripsi**, sama seperti sebelumnya saat
  memakai header. Cookie tidak menambah atau mengurangi exposure itu; TLS di depan server tetap
  syarat sebelum dashboard dijangkau dari jaringan yang tidak dipercaya.
- **Permukaan serangan bertambah satu rute publik** (`/unlock`, GET+POST). Rute itu tidak membaca
  data, tidak menulis database, dan tidak menerima tubuh lebih dari `MAX_PREBUFFERED_BODY`
  (64 KiB) dengan `Content-Type` form; `tests/test_server_hardening.py` ikut mengujinya.
- **Tidak ada perubahan pada peran, route, gerbang PII, atau format uang.** Cookie hanya
  mengubah cara kredensial tiba, bukan apa yang boleh dilakukan setelah masuk.
- **Yang harus ditinjau sebelum produksi:** kebijakan TLS di depan dashboard, apakah 12 jam sesuai
  kebiasaan operasional, dan apakah `/lock` perlu tombol di setiap halaman atau cukup di beranda.
