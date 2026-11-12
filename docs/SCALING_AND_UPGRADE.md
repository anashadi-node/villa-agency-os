# Skalabilitas & Jalur Upgrade

Dokumen ini berisi **angka hasil pengukuran** di mesin pengembangan (Linux 6.8,
loopback), bukan perkiraan. Semua bisa diulang dengan `scripts/perf_probe.py`.

Tanggal pengukuran: 2026-10-25 · Database: `data/villa.db` (3 properti).

---

## 1. Permukaan serangan yang benar-benar ada

Sebelum menambal apa pun, premisnya diuji dulu.

| Yang dicari | Hasil census |
|---|---|
| Berkas PHP / Blade / `artisan` | **0** |
| `composer.json` / `composer.lock` (Laravel) | **0** |
| Berkas JS/TS, `package.json`, `node_modules` | **0** |
| Dependency runtime pihak ketiga | **0** pada jalur SQLite (bawaan) — 58 modul `src/` hanya memakai stdlib. Satu-satunya impor pihak ketiga adalah **driver PostgreSQL opsional** (`psycopg` v3 atau `psycopg2`), diimpor *lazy* di dalam `schema_loader.connect_postgres` dan hanya dibutuhkan bila memakai kembaran PG |
| Dependency build/test | pytest, pyflakes; `markdown` hanya dipakai skrip build `scripts/build_deliverables.py`; `psycopg2`/`psycopg` untuk uji paritas PG |

**Konsekuensinya, dan ini yang penting:** kelas kerentanan PHP/Laravel/Node
(unserialize injection, `composer` supply chain, `npm` lifecycle script, template
engine RCE, mass-assignment Eloquent, `.env` bocor lewat `phpinfo`) **tidak
berlaku di sini karena kodenya tidak ada**. Menambal "kerentanan Laravel" pada
repo ini sama dengan menambal pintu yang tidak dibangun.

Yang **ada** dan karenanya diaudit: satu server HTTP stdlib (`dashboard.py`,
`marketing_site.py`), lapisan data SQLite/PostgreSQL, kriptografi webhook/TOTP,
penanganan berkas, dan rantai pasok pengembangan.

---

## 2. Temuan hari ini

### 2.1 Satu socket menggantung bisa mematikan seluruh layanan (SEDANG → ditutup)

Kedua server memakai `http.server` **single-threaded** dan **tanpa batas waktu
baca** (`timeout = None`, bawaan stdlib). Klien yang mengirim setengah baris
permintaan lalu diam akan memegang satu-satunya slot layanan **selamanya** —
tanpa kredensial, tanpa eksploit, cukup satu `nc`.

Ini bukan teori: `tests/test_server_hardening.py` membuktikannya dua arah.
Dengan `timeout=None` (kondisi pra-perbaikan) socket tetap terbuka setelah 1
detik; dengan `CONNECTION_TIMEOUT_SECONDS = 20` socket ditutup oleh server dan
server melayani klien berikutnya.

Perbaikan: `timeout = CONNECTION_TIMEOUT_SECONDS` pada kedua handler
(`dashboard.py`, `marketing_site.py`). Nilai 20 detik jauh di atas kebutuhan
permintaan nyata (median 3,4 ms) dan jauh di bawah kesabaran penyerang.

### 2.2 Antrean listen terlalu dangkal → pengunjung serentak di-drop (SEDANG → ditutup)

`socketserver` memberi `request_queue_size = 5`. Saat antrean penuh, kernel
Linux **membuang SYN** dan klien mengulang **1 detik** kemudian. Terukur pada
situs marketing:

| Antrean | 20 pengunjung serentak | Throughput | Klien terlambat |
|---|---|---|---|
| 5 (bawaan) | **1.248,2 ms** | 16,0 req/s | ~1.000 ms (retransmit) |
| 64 (sekarang) | **74,3 ms** | 269,3 req/s | 63,7 ms |

**16,8× lebih cepat**, tanpa mengubah satu baris pun pada loop permintaan.
Perbaikannya `ListeningHTTPServer.request_queue_size = REQUEST_QUEUE_SIZE (64)`
di `dashboard.serve()` — dipakai kedua server karena `marketing_site`
mengimpor `serve` dari `dashboard`.

Pelajaran metodologisnya: hambatannya ternyata **bukan** "single-threaded",
melainkan antrean kernel. Menambah thread duluan akan menutupi gejala dan
menambah risiko (koneksi `sqlite3` terikat thread).

### 2.3 Dashboard bisa di-iframe (RENDAH → ditutup)

`default-src 'none'` **tidak** mengatur frame; yang mengatur adalah
`frame-ancestors`. Dashboard kini mengirim `X-Frame-Options: DENY` dan CSP
`frame-ancestors 'none'; base-uri 'none'`.

---

## 3. Kapasitas terukur

| Halaman | Median | Min | Maks |
|---|---|---|---|
| `/` (beranda) | 3,42 ms | 3,09 ms | 3,62 ms |
| `/villas` | 0,77 ms | 0,70 ms | 0,83 ms |

**Plafon aritmetika** server satu-thread: `1 / latency`.

```
1 / 0,00342 s ≈ 292 permintaan/detik
```

Angka ini adalah **batas atas**, bukan janji: ia berlaku bila setiap permintaan
selesai dalam 3,4 ms dan tidak ada yang menggantung. Dipakai untuk membedakan
"kami sudah di batas" dari "ada yang rusak".

Untuk dashboard operator (loopback, satu pengguna) plafon yang sama berlaku,
tetapi kebutuhan nyatanya jauh di bawah itu.

---

## 4. Bottleneck berikutnya (dan cara memastikannya)

Setelah antrean listen diperbaiki, hambatan berpindah ke **penulis tunggal
SQLite** (`BEGIN IMMEDIATE`): semua tulis diserialkan, dan itu memang disengaja
— itulah yang membuat hold ganda mustahil. Untuk membuktikan apakah ia sudah
menjadi penghambat, ukur antrean tunggu tulis, bukan latensi baca:

```bash
# rata-rata waktu tunggu lock tulis (naik terus = bottleneck sudah pindah)
sqlite3 data/villa.db "PRAGMA busy_timeout; PRAGMA journal_mode;"
```

Aturan praktis (Little's Law, `L = λW`): bila utilisasi penulis melewati ~0,9,
antrean tumbuh tanpa batas dan latensi melonjak walau CPU masih sepi.

**Kapan pindah ke PostgreSQL:** ketika utilisasi penulis > 0,9 pada beban
puncak, atau ketika butuh lebih dari satu proses penulis. Jalur itu **sudah
ada dan sudah diuji** — skema PostgreSQL diterapkan ke server kosong dan
seluruh suite berjalan di atasnya (`VILLA_PG_DSN=... make check`).

---

## 5. Tangga skalabilitas

| Tahap | Konfigurasi | Batas praktis | Tindakan pemicu |
|---|---|---|---|
| 0 | 1 proses, SQLite, loopback | ~290 req/s baca, 1 penulis | — |
| 1 | + reverse proxy (TLS, HSTS, rate limit jaringan) | sama; TLS dilepas dari Python | sebelum go-live publik |
| 2 | N proses di belakang proxy (port berbeda) | N × ~290 req/s baca | p95 latensi naik saat ramai |
| 3 | PostgreSQL + N proses | penulis paralel | utilisasi penulis > 0,9 |
| 4 | PostgreSQL + replika baca | baca horizontal | baca mendominasi |

Catatan jujur: `http.server` **bukan** frontend kelas produksi untuk trafik
besar. Untuk tahap 2 ke atas, frontend publik sebaiknya dilayani proxy/CDN
(nginx, Caddy) yang menyajikan halaman statis hasil `--dump`, dan Python hanya
melayani formulir + dashboard. Ini sudah didukung: `marketing_site.py --dump`
menulis seluruh halaman sebagai HTML statis.

---

## 6. Kebijakan upgrade

1. **Tanpa treadmill CVE pihak ketiga.** Runtime 100% stdlib → tidak ada
   `npm audit`, tidak ada `composer audit`, tidak ada pin dependency yang harus
   dinaikkan tiap bulan. Permukaan rantai pasok praktis nol.
2. **Rentang Python yang diuji:**

   | Interpreter | Hasil |
   |---|---|
   | 3.11.16 (venv, SQLite 3.53.1) | seluruh suite lulus |
   | 3.12.3 (`/usr/bin/python3`, SQLite 3.45.1) | seluruh suite lulus (`make check`) |
   | 3.14.9 | 58/58 modul `src/` berhasil diimpor (diukur ulang 2026-11-13); suite belum dijalankan (pytest tidak terpasang untuk interpreter ini) |

   Artinya kode tidak memakai API yang dihapus sampai 3.14. Untuk menaikkan
   versi produksi: jalankan `make check` dengan interpreter baru; bila lulus,
   itulah buktinya — bukan asumsi.
3. **Skema:** migrasi maju-saja, paritas SQLite↔PostgreSQL dijaga
   `make schema-parity` (twin diregenerasi, bukan ditulis tangan).
4. **Gerbang wajib sebelum naik versi:** `make check` (suite + secret-scan +
   unsafe-scan + paritas skema + pre-deploy gate) harus PASS pada interpreter
   dan database target.
5. **Tidak ada `pip install` di produksi.** Semua skrip jalan dari sumber dengan
   `PYTHONPATH=src`; tidak ada langkah build yang bisa menyimpang dari yang diuji.

---

## 7. Checklist keamanan go-live (tugas operator, bukan kode)

| Item | Siapa | Bukti |
|---|---|---|
| TLS + HSTS di reverse proxy | operator | `curl -I https://domain` → `strict-transport-security` |
| Pembatasan laju di lapisan jaringan | operator | uji 100 req/detik dari satu IP → 429/limit |
| Dashboard tetap `127.0.0.1` | operator | `ss -ltnp` → hanya loopback |
| Token dashboard di manajer rahasia, bukan argumen CLI | operator | riwayat shell bersih |
| Rotasi kredensial kanal/pembayaran | operator | `docs/ROTASI_KREDENSIAL` (bila ada) |
| Backup + uji restore | operator | restore ke DB baru, `make check` lulus |
| Anker audit terbit berkala | operator | `make anchor-publish` + verifikasi |

---

## 8. Cara mengukur ulang

```bash
python3 scripts/perf_probe.py --db data/villa.db --concurrency 20
# opsional: bandingkan dua build
python3 scripts/perf_probe.py --db data/villa.db --paths /,/villas,/inquiry
```

Yang harus Anda lihat bila perbaikan ini utuh:
`slowest client` **jauh di bawah 1.000 ms** (retransmit SYN) dan throughput
20-serentak mendekati plafon `1/latency`.

Bila `slowest client` kembali menyentuh ~1 detik, antrean listen sedang penuh
lagi — periksa `REQUEST_QUEUE_SIZE`, jangan menambah thread.
