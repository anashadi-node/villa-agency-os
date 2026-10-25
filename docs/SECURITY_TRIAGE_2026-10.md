# Triase Keamanan — Oktober 2026

Audit kedua (lanjutan `docs/SCALING_AND_UPGRADE.md`). Fokus: **kerentanan yang sedang
banyak dieksploitasi**, dipetakan ke permukaan yang benar-benar ada di repo ini.
Metode: falsification-first — setiap klaim diuji ke kode/database nyata, bukan diasumsikan.

Periode data: 2026-08-26 → 2026-10-23. Tanggal audit: 2026-10-25.

---

## 0. Ringkasan

| # | Temuan | Tingkat | Status |
|---|---|---|---|
| 1 | `CVE-2026-0865` (header injection `http.server`) **hidup** di interpreter kita; tidak terjangkau karena semua header literal | MEDIUM (laten) | **Ditutup berlapis** — `SafeHeaderMixin` fail-closed + 3 tes |
| 2 | `Server:` membocorkan `Python/3.12.3` di situs publik | LOW | **Ditutup** — `sys_version = ""` |
| 3 | 47 temuan semgrep (38 SQLAlchemy + 5 formatted-SQL + 4 urllib) | — | **0 dapat dieksploitasi** (triase §3) |
| 3b | Feed FX menerima `http://` padahal docstring menjanjikan HTTPS → kurs bisa diubah di transit, harga semua booking ikut salah | LOW (dampak uang) | **Ditutup** — `https://` + `file://` saja (2 tes) |
| 4 | Reflected XSS lewat path/query | — | **Tidak ada** (diuji, §4) |
| 5 | `trivy` melaporkan 0 — tetapi **tidak memindai apa pun** | — | Dicatat jujur: bukan bukti |

---

## 1. Alat & hasil mentah

| Alat | Perintah | Hasil |
|---|---|---|
| semgrep `p/python` + `p/secrets` | `--severity ERROR` | **0 temuan** (rc=0) |
| semgrep `p/default` + `p/owasp-top-ten` + `p/secrets` | JSON | **47 temuan**: 38 SQLAlchemy raw-query (ERROR), 5 formatted-SQL (WARN), 4 dynamic-urllib (WARN); 0 error pemindaian |
| trivy `fs` (misconfig, secret) | — | tabel kosong: **semua target "Not scanned"** (tak ada lockfile/IaC) |
| CISA KEV (data primer) | `known_exploited_vulnerabilities.json` | katalog **2026.10.23**, 1.756 entri, **80** ditambahkan sejak 2026-08-26 |
| Probe CRLF header | server mini, interpreter produksi | **rentan**: `X-Probe: safe` + `Injected: yes` jadi dua header |
| Probe XSS | situs marketing nyata | **tidak terpantul** di 3 jalur (404, 404, 200) |

Catatan kejujuran: `semgrep --config auto` **gagal** dijalankan (`rc=2`: "cannot create auto
config when metrics are off"), jadi hasil "auto" pertama bukan PASS melainkan tidak ada
pemindaian. Diganti paket eksplisit (`p/default`, `p/owasp-top-ten`) supaya tidak mengirim
metrik dan tetap nyata.

---

## 2. CVE-2026-0865 — header injection di `http.server`

**Fakta (triangulasi):** cve.org CVE-2026-0865 (CVSS 4.0 = 5.9 MEDIUM, CWE-74), OpenCVE,
cveguru, Debian security tracker, dan OSV `PSF-2026-6` — semuanya menyebut versi terdampak
`< 3.10.20`, `< 3.11.15`, `< 3.12.13`, `< 3.13.12`, `< 3.14.3`. Akar masalah:
`wsgiref.headers.Headers` dan `BaseHTTPRequestHandler.send_header()` menganggap input sudah
disanitasi, sehingga `\r\n` di nama/nilai header memecah respons menjadi header tambahan.

**Relevansi runtime ini (diukur, bukan disimpulkan):**

```
Python 3.12.3 (/usr/bin/python3, paket 3.12.3-1ubuntu0.17)
  → send_header("X-Probe", "safe\r\nInjected: yes") DITERIMA
  → respons wire berisi dua header: "X-Probe: safe" dan "Injected: yes"
```

Artinya **Ubuntu belum membackport perbaikan ini** ke `ubuntu0.17`, dan runtime kita berada
di rentang terdampak. Python 3.14.7 juga `< 3.14.3`.

**Reachability (bagian yang menentukan):** di seluruh `src/` ada 19 pemanggilan `send_header`.
Semua **nama** header literal; nilai berasal dari literal, `str(len(payload))`, atau variabel
`cache` yang hanya berisi dua literal. **Tidak ada** `Location`, `Content-Disposition`,
`Set-Cookie`, atau header lain yang membawa input pengguna. Jadi: **tidak dapat dieksploitasi
hari ini**, tetapi satu baris `send_header("Location", request_path)` di masa depan akan
langsung membukanya.

**Perbaikan (fail-closed, meniru semantic upstream):** `SafeHeaderMixin.send_header()` menolak
`\r`, `\n`, `\x00` dengan `ValueError`. Kedua handler mewarisinya. Tiga tes mengunci:
nilai normal lolos, 4 varian jahat ditolak dan **tidak pernah mencapai wire**, kedua handler
benar-benar memakai mixin.

**CVE-2026-3644** (`http.cookies` validasi karakter kontrol tidak lengkap) **tidak berlaku**:
repo ini tidak memakai cookie sama sekali (autentikasi dashboard = bearer token).

---

## 3. Triase 38 temuan semgrep (injeksi SQL)

Aturan yang menyala: `python.sqlalchemy.security.sqlalchemy-execute-raw-query` — ia menandai
**penggabungan string ke `execute()`**, apa pun library-nya. Repo ini tidak memakai SQLAlchemy
(`sqlite3`/`psycopg2`), jadi ini positif palsu struktural, tetapi tetap harus dibuktikan
satu per satu.

Pola yang ditemukan, seluruhnya:

| Pola | Contoh | Jumlah | Kenapa aman |
|---|---|---|---|
| Nama tabel/kolom **konstanta modul** | `audit.py` `{AUDIT_TABLE}`, `observability` `LIKE_HOLD_STATUSES` | mayoritas | tidak ada jalur input |
| **Allowlist eksplisit** | `settings.READINESS_FILTERS`, `intake.MUTABLE_COLUMNS`, `messages.SOURCE_TABLES`, `reservation_service` dict literal | 5 | kunci tak dikenal → error, bukan diteruskan |
| **Placeholder dinamis** | `", ".join("?" for _ in STATUSES)` | 4 | hanya `?` yang digabung; nilai tetap di-bind |
| **Nama kolom dari caller internal** | `cases.extra_sets`, `ops_tasks(column=...)` | 2 | satu pemanggil, literal (`"quoted_at"`) |
| Suffix literal | `sql + " LIMIT 1"` | 2 | teks tetap |

Semua **nilai** tetap lewat parameter binding (`?`). Tidak ada satu pun tempat yang menyisipkan
nilai pengguna ke teks SQL.

Situs yang paling berisiko sudah dibaca utuh dan diberi alasan di kode: `intake.update_field`
menolak kolom di luar `MUTABLE_COLUMNS` (IMMUTABLE_FIELD), `settings._count_ready` menolak
tabel di luar `READINESS_FILTERS`.

**Kesimpulan falsifiable:** bila ada injeksi, sebuah string berisi `' OR 1=1 --` harus bisa
mengubah hasil query. Tidak ada jalur yang meneruskan nilai ke teks query, sehingga tidak ada
yang bisa diuji lebih jauh tanpa mengubah arsitektur.

### 3b. Sembilan temuan sisanya (bukan SQLAlchemy)

| Aturan | Lokasi | Verdict |
|---|---|---|
| `formatted-sql-query` ×5 | `audit.py` ×2, `policy_engine.py`, `go_live_smoke.py`, `seed_demo.py` | Sama kelasnya dengan §3: nama tabel konstanta modul atau argumen skrip ops, nilai ter-bind. **Aman** |
| `dynamic-urllib-use-detected` ×4 | `preflight.py` ×2, `perf_probe.py`, `refresh_fx_rate.py` | Tiga yang pertama menyusun URL dari `http://127.0.0.1:{int}` — skema tidak bisa diubah pemanggil, `file://` mustahil; diberi `nosemgrep` + alasan tertulis. Yang keempat **temuan nyata** dan sudah ditutup (§3b-1) |

**3b-1. Feed FX menerima `http://`.** Docstring menjanjikan HTTPS, tetapi `fetch_json()` tidak
memeriksa skema. Dampaknya bukan pembacaan file lokal (isi tetap harus lolos parser payload),
melainkan **integritas kurs**: jawaban `http://` bisa ditulis ulang di transit, dan satu kurs
yang salah akan salah menarif semua booking yang memakainya. Guard sekarang hanya menerima
`https://` dan `file://` (jalur kutipan tersimpan yang memang dipakai CLI dan harness);
`http://` dan `ftp://` ditolak sebelum ada request. Dua tes: penolakan + bukti `file://` tetap jalan.

> Catatan metode: laporan pertama saya menyebut "38 temuan" karena hanya menghitung aturan yang
> saya triase. Total sebenarnya **47**. Angka yang tidak dihitung bukan angka nol — begitulah
> celah triase terlihat.

---

## 4. Probe perilaku (bukan pembacaan kode)

| Uji | Hasil |
|---|---|
| Path `/<script>alert("xss")</script>` → 404 | payload **tidak ada** di body; halaman 404 generik (tidak memantulkan path) |
| `/villas/<script>…` → 404 | sama |
| `/inquiry?ref=<script>…` → 200 | `ref` **tidak dipantulkan** ke HTML |
| Header `Server` situs publik | sebelumnya `BaseHTTP/0.6 Python/3.12.3` → sekarang tanpa versi interpreter |

### 4b. Limiter formulir — diuji hidup, bukan diasumsikan

| Uji | Hasil |
|---|---|
| 7× POST `/inquiry` ke server nyata, DB **salinan** di `/tmp` (produksi tidak disentuh) | `400,400,400,400,400,429,429` |
| Arti | batas 5/600 s ditegakkan **sebelum** validasi — form tidak valid pun tidak membeli kuota |
| Kunci | hop terakhir `X-Forwarded-For`, hanya bila `--trusted-proxy`; dinormalisasi `ipaddress` |
| Sudah ditutup tes | `test_post_rate_limit_429`, "rotating the header buys no extra quota", batas 5→429 |

> Koreksi: draf pertama dokumen ini dan rekomendasi lisan saya menyebut "server stdlib tidak
> punya limiter". Itu salah — saya menyimpulkan sebelum membaca `marketing_site.py`. Kontrol
> sudah ada, benar, dan teruji; yang tersisa hanya tiga hal kecil di §6 butir 2.

---

## 5. Lanskap ancaman yang sedang marak — dan relevansinya di sini

### 5.1 Data primer: CISA KEV (katalog 2026.10.23)

- 1.756 kerentanan pernah dieksploitasi; **80 ditambahkan dalam 60 hari** terakhir.
- Vendor terbanyak dalam jendela itu: Cisco (5), Microsoft (5), Citrix (5), Linux (5),
  Plex (4), JFrog (4), Fortinet (3), MikroTik (3), Google (3), Arista (2).
- **0** dari 80 ditandai `Known` untuk kampanye ransomware (mayoritas `Unknown` — jadi ini
  bukan bukti "ransomware menurun", hanya bahwa atribusi belum ditegakkan).
- 5 dari 80 adalah CVE **lama** yang baru masuk katalog Oktober 2026: BIND `CVE-2015-5477`,
  Struts `CVE-2016-3081`, ProFTPD `CVE-2015-3306`, Strapi `CVE-2023-22894`, ONLYOFFICE
  `CVE-2021-3199`. **Pelajaran:** penyerang masih memanen layanan internet yang tidak pernah
  ditambal — usia CVE bukan pelindung.

**Relevansi:** tidak satu pun dari 80 menyentuh Python/SQLite/PostgreSQL. Yang terdampak
adalah *edge appliance* (Citrix NetScaler, Ivanti Connect Secure, PAN-OS, FortiMail,
Cisco SD-WAN, MikroTik, Plex, BeyondTrust, Atlassian, Zammad). Repo ini tidak memakai satu pun.

### 5.2 OWASP Top 10:2025 (dirilis 6 November 2025)

| Peringkat | Kategori | Posisi repo ini |
|---|---|---|
| A01 | **Broken Access Control** (100% aplikasi uji punya minimal satu; SSRF dilebur ke sini) | Multi-tenant `agency_id` diterapkan sebagai gelombang eksplisit; peran + bearer token (192-bit) di dashboard; penolakan lintas-agensi diuji. **Sisa risiko:** belum ada uji penetrasi independen |
| A02 | Security Misconfiguration | Header lengkap (CSP + frame-ancestors + nosniff), tanpa endpoint debug, bind loopback default |
| A03 | **Software Supply Chain Failures** (kategori baru) | **Struktural imun**: 0 dependency runtime; hanya pytest/pyflakes/psycopg2 di jalur uji |
| A04 | Cryptographic Failures | `secrets`, `hmac.compare_digest` di 6 modul; SQLite/Postgres untuk data, bukan kripto buatan sendiri |
| A05 | Injection | 38 flag → 0 dapat dieksploitasi (§3) |
| A10 | **Mishandling of Exceptional Conditions** (kategori baru) | Justru inti perbaikan sesi ini: timeout socket, fail-closed header guard, galat internal tidak membocorkan traceback |

Dua kategori baru itu penting: A03 menaikkan rantai pasok menjadi risiko kelas atas — dan
satu-satunya alasan repo ini aman di sana adalah **tidak punya dependency**, bukan karena
alat pemindainya bagus.

### 5.3 Sektor perhotelan/villa — serangan yang paling relevan secara bisnis

- **Spear-phishing berbasis data reservasi asli.** Juni 2026: penipu memakai detail booking
  nyata untuk menargetkan tamu di **≥350 hotel dan vacation rental di 50 negara**, lewat SMS,
  WhatsApp, dan email yang menyamar sebagai staf properti (NCDIT, 9 Juni 2026; WIRED).
- **September 2026:** penyedia terhubung mengonfirmasi data reservasi (nama tamu, properti,
  tanggal, nomor booking, email, telepon) **terbukti dieksfiltrasi lewat sebuah kerentanan**;
  reservasi periode Januari–13 September 2026 dilaporkan terdampak (heise, 25 Sep 2026).
- **Juni 2026:** aktor menargetkan *staf* properti mitra Booking.com dengan email yang
  menyamar sebagai keluhan tamu, untuk menjalankan file berbahaya (Infosecurity Magazine,
  30 Juni 2026).

**Kenapa ini penting untuk villa-agency-os:** aplikasi ini menyimpan persis himpunan data yang
dipakai kampanye di atas (nama, kontak, tanggal menginap, properti) dan punya jalur pesan
keluar (channel/connector). Artinya:

1. Kompromi dashboard/DB = bahan mentah phishing bermutu tinggi, bukan sekadar "data bocor".
2. Pengambilalihan kanal keluar = pesan penipuan dikirim dari nomor/akun agensi sendiri —
   reputasi yang sulit dipulihkan.
3. Kontrol yang sudah ada dan harus tetap hidup: MFA dashboard (ADR-0005), audit hash-chain,
   scoping `agency_id`, allowlist unggahan, redaksi PII di log.
4. Aturan produk yang layak ditambahkan: **tidak pernah** meminta data kartu lewat tautan;
   tautan "verifikasi" selalu membawa token sekali-pakai dan domain tetap.

### 5.4 AI agent / MCP — untuk lapisan AI yang direncanakan

- **Tool poisoning**: instruksi jahat ditanam di *metadata* tool (deskripsi), dibaca model
  sebagai konteks tepercaya, tak terlihat oleh manusia, dan menetap lintas sesi.
- **MCPTox** (arXiv 2508.14925; AAAI 2026): 45 server MCP nyata, 353 tool, 1.312 kasus uji →
  tingkat serangan sukses hingga **72,8%**, dan model yang lebih patuh instruksi justru **lebih
  rentan**; tingkat penolakan terbaik < 3%.
- Laporan industri menyebut ~**492 server MCP tanpa autentikasi** terjangkau dari internet
  publik (Trend Micro, dikutip replyant.com) — angka ini perlu dikonfirmasi ulang sebelum
  dipakai sebagai klaim internal.
- Relevansi: begitu modul AI di repo ini mulai memakai tool/MCP, metadata tool harus
  diperlakukan sebagai **input tak tepercaya** — pin + diff hash, allowlist manifest, dan
  persetujuan manusia di luar jalur model untuk tool sensitif.

---

## 6. Prioritas berikutnya (berurut dampak ÷ usaha)

1. ~~Naikkan patch interpreter untuk `CVE-2026-0865`~~ — **sudah tertutup oleh backport distro.**
   Terpasang `python3.12 3.12.3-1ubuntu0.17`; `changelog.Debian.gz` mencantumkan
   `debian/patches/CVE-2026-0865.patch` (+ `-2`, "Permit HTAB in header values"). Probe perilaku
   pada interpreter produksi: CR, LF, dan CRLF ditandai ilegal, **TAB diizinkan** (sesuai patch),
   dan `putheader("X-Test", "a\r\nInjected: 1")` → `ValueError: Invalid header value`.
   *Tidak perlu naik versi; cukup jaga `apt upgrade`.* Catatan kecil: NUL (`\x00`) tidak
   ditandai oleh regex `_is_illegal_header_value` — di luar cakupan CVE ini, tetapi jangan
   pernah meneruskan input pengguna mentah ke header.
   *Koreksi: draf sebelumnya menyebut "3.12.13+ / 3.14.3+" tanpa memeriksa changelog Debian —
   Ubuntu membackport, jadi syarat versi itu tidak berlaku di mesin ini.*
2. ~~Rate limit `/inquiry`~~ — **sudah ada dan terbukti bekerja.** `SlidingWindowRateLimiter`
   (5 event / 600 s) memakai hop **terakhir** `X-Forwarded-For` hanya bila `--trusted-proxy`,
   dinormalisasi lewat `ipaddress` (IPv4-mapped IPv6 = satu kunci). Probe hidup di DB salinan:
   5×400 lalu **429 pada request ke-6**. Sisa: (a) hitungan per proses — beberapa worker
   mengalikan kuota, jadi scale-out butuh limiter di proxy. Ditutup 2026-10-25:
   (b) 429 kini mengirim `Retry-After: 600`; (c) peta kunci dibatasi
   `MAX_TRACKED_CLIENTS = 4096` dengan eviksi — klien yang memutar alamat tidak lagi
   menumbuhkan memori tanpa batas. *Usaha: kecil.*
3. **Uji penetrasi eksternal** sebelum go-live publik — A01 tidak bisa dibuktikan dari dalam.
   *Usaha: besar.*
4. **Latihan phishing** (staf, bukan tamu) + template pesan resmi yang tidak pernah meminta
   data kartu. **Paket siap pakai:** `docs/security/PHISHING_DRILL_KIT.md` (6 minggu, 3 skenario,
   prasyarat SPF/DKIM/DMARC, batas etika/UU PDP) + `scripts/phishing_drill_score.py` (fail-closed,
   7 tes). Sisa: consent tertulis owner sebelum gelombang pertama. *Usaha: kecil, dampak bisnis tinggi.*
5. **Pin metadata tool** sebelum modul AI memakai MCP. *Usaha: sedang, hanya bila relevan.*

---

## 7. Batas pengujian ini

Tidak ada uji beban >20 konkuren; tidak ada uji penetrasi eksternal; tidak ada pengujian di
lingkungan dengan TLS (semua uji loopback); `trivy` tidak memindai apa pun di repo ini;
semgrep hanya menjangkau 4 ruleset, bukan seluruh registry; Python 3.14.7 diuji impor modul,
bukan suite penuh (pytest tidak terpasang).
