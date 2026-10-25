# Deployment — Villa Management & Agency OS

Panduan memasang, mencadangkan, memantau, dan mengembalikan sistem. Semua
perintah di sini diasumsikan dijalankan sebagai user layanan `villa`
(`useradd --system --create-home --shell /usr/sbin/nologin villa`).

> **Batas tanggung jawab.** Dokumen ini menjelaskan perangkat lunaknya. Legal,
> pajak (PBJT), izin pariwisata, kontrak owner, dan kepatuhan payment provider
> tetap wajib ditinjau counsel/tax advisor lokal — lihat
> `docs/GO_LIVE_DECISIONS.md`.

## 1. Prasyarat

| Komponen | Keterangan |
|---|---|
| Python | 3.11+ (dikembangkan & diuji pada 3.11; hanya pustaka standar) |
| Database | SQLite (pilot) atau PostgreSQL 16 (produksi lebih besar) |
| Scheduler | systemd timer (unit disediakan di bawah) |
| Direktori | `/var/lib/villa/` (data), `/var/lib/villa/anchors/` (anker), `/backups/` |
| Jaringan | dashboard hanya di `127.0.0.1`; akses luar lewat reverse proxy + TLS |

Anker **wajib** berada di direktori (idealnya sistem/akun) yang berbeda dari
penulis database. Anker yang ditulis di mesin yang sama dengan database tidak
membuktikan apa pun: yang menyerang database juga bisa menulis ulang ankernya.

## 2. Pemasangan

```bash
sudo -u villa git clone <repo> /opt/villa-agency-os
cd /opt/villa-agency-os

# 1. Buktikan kode layak dipromosikan (syntax, test, scan, parity skema)
make check            # harus berakhir: === PRE-DEPLOY GATE: PASS ===

# 2. Siapkan database
sudo -u villa python3 - <<'PY'
import sys; sys.path.insert(0, "src")
from schema_loader import connect_sqlite, applied_version
conn = connect_sqlite("/var/lib/villa/villa.db")     # membuat + apply skema
print("schema:", applied_version(conn))
conn.close()
PY

# PostgreSQL: pakai connect_pg + apply_pg_schema, lalu hardening 002_pg_range_exclusions.pg.sql
#   (constraint EXCLUDE anti-overlap sebagai jaring kedua di level database)

# 3. Direktori operasional
sudo -u villa mkdir -p /var/lib/villa/anchors /var/lib/villa/documents /var/lib/villa/outbox
sudo -u villa chmod 750 /var/lib/villa /var/lib/villa/anchors

# 4. Data demo (HANYA di mesin demo; preflight akan menolak produksi yang berisi demo)
sudo -u villa python3 scripts/seed_demo.py --db /tmp/demo/villa.db
```

### Kredensial

Disimpan di `/etc/villa/villa.env` (mode `0640`, owner `root:villa`), **tidak
pernah** di repo. Nama variabel yang dipakai sistem:

| Variabel | Dipakai oleh | Catatan |
|---|---|---|
| `VILLA_DB` | semua CLI | path database |
| `VILLA_ANCHOR_DIR` | anchors, preflight, observability | direktori anker |
| `VILLA_ANCHOR_LABEL` | publish_anchor | label bebas (mis. `minggu-38`) |
| `VILLA_ACTOR` | publish_anchor, scheduler | nama aktor di rantai audit |
| `VILLA_WA_WEBHOOK_SECRET` | connectors | secret HMAC webhook WhatsApp — hanya dari env, tidak pernah di DB atau log |
| `VILLA_PG_DSN` | tes PostgreSQL | contoh bentuk: `postgresql://***@127.0.0.1:5432/villa` (nilai asli tidak ditulis di dokumen) |

Aturan yang ditegakkan kode: `.env` tidak pernah di-commit (`make secret-scan`
gagal kalau ada), rahasia tidak pernah masuk DB, log, atau output perintah.

## 3. Layanan (systemd)

`/etc/systemd/system/villa-dashboard.service`

```ini
[Unit]
Description=Villa Agency OS — dashboard internal
After=network-online.target

[Service]
Type=simple
User=villa
WorkingDirectory=/opt/villa-agency-os
EnvironmentFile=/etc/villa/villa.env
ExecStart=/usr/bin/python3 src/dashboard.py --db /var/lib/villa/villa.db \
          --host 127.0.0.1 --port 8080 --role owner \
          --token "${VILLA_DASHBOARD_TOKEN}"
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/villa
```

`VILLA_DASHBOARD_TOKEN` dibaca dari `EnvironmentFile` di atas (systemd memperluas `${...}` di
`ExecStart`). **Jangan menghilangkannya:** tanpa `--token`, dashboard membuat token acak tiap
kali start dan hanya mencetaknya ke journal, jadi setiap restart mencabut akses semua URL yang
sudah dipakai dan setiap konfigurasi reverse proxy harus diubah. Token juga terlihat di
`systemctl status` dan `ps`; karena dashboard hanya bind ke loopback, yang bisa melihatnya
adalah pengguna lokal — kalau itu tidak dapat diterima, taruh token di proxy dan batasi akses
ke unit-nya.

> Dashboard **read-only** untuk data. Satu-satunya aksi tulis adalah approval,
> dan itu lewat `ApprovalService` (satu penulis) dengan `idempotency_key` wajib.
> Jangan bind ke `0.0.0.0`: taruh di belakang reverse proxy dengan TLS + auth.

`/etc/systemd/system/villa-scheduler.service` (dijalankan tiap 5 menit)

```ini
[Unit]
Description=Villa Agency OS — heartbeat deadline

[Service]
Type=oneshot
User=villa
WorkingDirectory=/opt/villa-agency-os
EnvironmentFile=/etc/villa/villa.env
ExecStart=/usr/bin/python3 src/scheduler.py --db /var/lib/villa/villa.db
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/villa
```

`/etc/systemd/system/villa-scheduler.timer`

```ini
[Unit]
Description=Jalankan heartbeat Villa Agency OS tiap 5 menit

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
```

`/etc/systemd/system/villa-anchor.service` + `.timer` (mingguan)

```ini
# villa-anchor.service
[Service]
Type=oneshot
User=villa
WorkingDirectory=/opt/villa-agency-os
EnvironmentFile=/etc/villa/villa.env
ExecStart=/usr/bin/python3 scripts/publish_anchor.py publish
# villa-anchor.timer
[Timer]
OnCalendar=Mon 06:00
Persistent=true
[Install]
WantedBy=timers.target
```

`/etc/systemd/system/villa-backup.service` + `.timer` (harian, 02:00)

```ini
# villa-backup.service
[Service]
Type=oneshot
User=villa
ExecStart=/bin/bash -c '/usr/bin/sqlite3 /var/lib/villa/villa.db \
          ".backup /backups/villa-$(date +%%F).sqlite"'
# villa-backup.timer
[Timer]
OnCalendar=*-*-* 02:00
Persistent=true
[Install]
WantedBy=timers.target
```

Aktifkan: `systemctl daemon-reload && systemctl enable --now villa-scheduler.timer villa-anchor.timer villa-backup.timer villa-dashboard.service`.

## 4. Reverse proxy (contoh nginx)

### 4.1 Dashboard internal (ops.example.com)

```nginx
server {
    listen 443 ssl;
    server_name ops.example.com;
    ssl_certificate     /etc/letsencrypt/live/ops.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ops.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    server_tokens       off;

    # HSTS dipasang DI SINI, bukan di aplikasi: upstream berjalan HTTP dan header
    # HSTS yang datang lewat HTTP diabaikan peramban. `always` ikut mengirim pada
    # respons galat. Jangan tambahkan `preload` sebelum semua subdomain memang
    # HTTPS permanen — daftar preload sulit ditarik kembali.
    #
    # Catatan nginx: `add_header` di dalam blok `location` MENGGANTIKAN seluruh
    # add_header selevel `server`, bukan menambah. Kalau kelak menambah header di
    # location, ulangi HSTS di sana atau warisi dengan sadar.
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # Batasi ke kantor/VPN. Dashboard tidak dirancang untuk internet terbuka.
    allow 203.0.113.10;
    deny all;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Host $host;
    }
}
```

Token peran dikirim ke dashboard sebagai header identitas
(`Authorization: Bearer <token>`). Simpan token di password manager, bukan di
URL, karena URL tercatat di log proxy.

### 4.2 Situs publik (villa.example.com → 127.0.0.1:8090)

Situs publik dilayani unit `deploy/systemd-user/villa-marketing.service`, yang
mengikat **hanya loopback**. TLS, nama domain, dan pembatasan laju tingkat
jaringan adalah tugas reverse proxy; port 8090 tidak pernah mendengar dari luar
mesin ini.

```nginx
server {
    listen 443 ssl;
    server_name villa.example.com;
    ssl_certificate     /etc/letsencrypt/live/villa.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/villa.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    server_tokens       off;

    # HSTS: lihat catatan di §4.1 — wajib di proxy, tidak bisa dari aplikasi.
    # Situs publik adalah tempat paling penting untuk header ini: pengunjung baru
    # yang mengetik "villa.example.com" harus langsung dipaksa HTTPS.
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location / {
        proxy_pass http://127.0.0.1:8090;

        # WAJIB timpa, bukan tambahkan. Unit berjalan dengan --trusted-proxy dan
        # hanya membaca hop TERAKHIR header ini — hop yang ditulis proxy kita
        # sendiri. Bentuk "tambahkan" ($proxy_add_x_forwarded_for) menyimpan
        # alamat kiriman pengunjung di depan, jadi jangan dipakai di sini.
        proxy_set_header X-Forwarded-For $remote_addr;

        # $http_host, bukan $host: $host membuang nomor port, sedangkan
        # pemeriksaan same-origin pada form membandingkan Origin dengan Host.
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Kenapa `--trusted-proxy` bersifat opt-in: tanpa proxy di depan, header
`X-Forwarded-For` hanyalah data yang bisa dikirim siapa saja. Satu pengunjung
bisa memutar alamat palsu di setiap permintaan dan menghindari pembatasan laju
sepuluh menit. Karena itu XFF dipercaya **hanya** ketika unit menyatakannya, dan
CLI menolak kombinasi `--trusted-proxy` + `--allow-remote` supaya socket yang
menerima koneksi langsung tidak pernah bisa salah menyangka pemalsu sebagai
proxy (ADR-0067).

Verifikasi, dalam urutan ini:

```bash
# 1. Unit user tidak menerima PrivateDevices= / ProtectKernelModules= /
#    CapabilityBoundingSet=: pengelola user tidak bisa melepas kapabilitas yang
#    tidak dimilikinya, dan unit yang memuatnya mati dengan status=218/CAPABILITIES
#    (diukur di mesin ini, 2026-10-08 — bukan asumsi).
systemd-analyze --user verify deploy/systemd-user/villa-marketing.service

# 2. Probe unit yang sedang berjalan: header keamanan, honeypot, gate dashboard,
#    dan tidak ada port yang menjawab dari alamat non-loopback.
python3 scripts/preflight.py --db data/villa.db --host-check

# 3. Buktikan unit yang SAMA benar-benar start, melayani, dan masih boleh menulis.
#    Port bebas + SALINAN database: jangan pernah menjalankan probe ini di atas
#    port 8090 atau di atas database yang hidup. Salinan harus lewat `.backup`
#    (mode WAL: `cp` berkas utama saja kehilangan frame yang sudah commit).
sqlite3 data/villa.db ".backup data/.probe.db"
systemd-run --user --unit=villa-probe --collect \
  -p ProtectSystem=strict -p ProtectHome=read-only \
  -p ReadWritePaths="$PWD/data" -p NoNewPrivileges=true -p PrivateTmp=true \
  -p ProtectKernelTunables=true -p ProtectControlGroups=true \
  -p RestrictNamespaces=true -p RestrictRealtime=true -p LockPersonality=true \
  -p MemoryDenyWriteExecute=true -p SystemCallArchitectures=native -p UMask=0077 \
  -p 'RestrictAddressFamilies=AF_INET AF_INET6' \
  /usr/bin/python3 src/marketing_site.py --db data/.probe.db \
  --host 127.0.0.1 --port 8199 --trusted-proxy
# lalu: GET / harus 200 (Server: VillaMarketing/1.0), POST /inquiry honeypot 400,
# dan POST /inquiry sah harus menambah tepat satu baris `cases`.
systemctl --user stop villa-probe
rm -f data/.probe.db data/.probe.db-wal data/.probe.db-shm
```

Daftar properti di langkah 3 sengaja ditulis lengkap: `PrivateDevices=` dan
`ProtectKernelModules=` **tidak** ada di sana karena keduanya mematikan unit user
(`status=218/CAPABILITIES`), dan begitulah isi unit di repo ini.

`--host-check` menjalankan empat pemeriksaan; semuanya read-only kecuali satu
POST honeypot, yang ditolak intake **sebelum** pembatasan laju dan sebelum ada
penulisan apa pun — jadi aman dijalankan pada situs yang hidup. Unit yang sama
juga dibuktikan benar-benar start dan masih boleh menulis ke database dengan
menjalankannya pada port bebas atas **salinan** database (bukan yang hidup);
salinan itu harus dibuat lewat `.backup` SQLite, karena `cp` berkas utama saja
melewatkan frame WAL yang sudah commit dan terlihat seperti skema yang tertinggal.

Catatan terukur (2026-10-08): halaman depan situs ini 537 KiB, dan 455 KiB di
antaranya adalah satu gambar berbentuk data URI di dalam HTML. Setiap tampilan
halaman mengunduh ulang gambar itu, tanpa cache berkas gambar yang terpisah.
Cukup untuk pilot, tetapi rute `/media/` dengan header cache panjang adalah
langkah berikutnya sebelum trafik membesar.

### 4.3 Siapa memasang header apa

Audit 2026-10-25 mengukur header pada respons nyata. Pembagiannya sengaja, dan
mengubahnya tanpa memahami alasan ini akan menurunkan keamanan:

| Header | Dipasang oleh | Alasan |
|---|---|---|
| `Content-Security-Policy` | **aplikasi** | Kebijakan bergantung pada isi halaman; hanya aplikasi yang tahu apa yang dimuat |
| `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` | **aplikasi** | Berlaku sama untuk semua jalur; sudah diuji per respons (termasuk 401/404) |
| `Cache-Control` | **aplikasi** | Hanya aplikasi yang tahu halaman mana boleh di-cache (`no-store` untuk dashboard dan 404) |
| `Strict-Transport-Security` | **proxy** | Aplikasi melayani HTTP; HSTS lewat HTTP diabaikan peramban |
| `ssl_protocols`, `server_tokens` | **proxy** | Milik lapisan TLS/nginx |
| `Permissions-Policy`, `COOP`, `CORP` | belum dipasang | Tidak ada fitur yang membutuhkannya hari ini; dicatat, bukan diklaim selesai |

Uji cepat setelah deploy:

```bash
curl -sSI https://villa.example.com/ | grep -i strict-transport-security
curl -sSI https://ops.example.com/    | grep -iE "strict-transport|content-security"
```

## 5. Backup & pemulihan (diuji, bukan diasumsikan)

```bash
# 1. Backup konsisten (SQLite)
sqlite3 /var/lib/villa/villa.db ".backup /backups/villa-$(date +%F).sqlite"

# 2. VERIFIKASI: pulihkan ke lokasi sementara dan jalankan pemeriksaan
cp /backups/villa-$(date +%F).sqlite /tmp/restore-check.sqlite
cd /opt/villa-agency-os
python3 src/observability.py --db /tmp/restore-check.sqlite --format json | head -20
python3 scripts/preflight.py --db /tmp/restore-check.sqlite \
    --anchor-dir /var/lib/villa/anchors --decisions docs/GO_LIVE_DECISIONS.md
```

Aturan yang disepakati bisnis: **RPO 24 jam, RTO 4 jam**, uji restore penuh
**bulanan** dicatat di tiket operasional, dan backup **tidak** dianggap sah
sebelum langkah verifikasi di atas lulus. Backup tanpa uji restore adalah
harapan, bukan rencana.

## 6. Monitoring

```bash
# Snapshot manusiawi (exit 1 = ada temuan CRITICAL)
python3 src/observability.py --db /var/lib/villa/villa.db --anchor-dir /var/lib/villa/anchors

# Format Prometheus (scrape atau textfile collector node_exporter)
python3 src/observability.py --db /var/lib/villa/villa.db --format prometheus \
  > /var/lib/node_exporter/textfile/villa.prom
```

Alert minimum yang wajib hidup (severity dari `health_checks`):

| Temuan | Severity | Tindakan |
|---|---|---|
| `audit_chain` rusak | CRITICAL | Hentikan penulisan, mulai prosedur insiden, jangan menimpa bukti |
| `audit_anchor` gagal/basi (>45 hari) | CRITICAL | Terbitkan anker, periksa siapa yang bisa menulis DB |
| `schema_version` tidak cocok | CRITICAL | Jangan jalankan; deploy versi kode yang cocok |
| Insiden SEV1/SEV2 terbuka | CRITICAL | Ikuti playbook di modul `incidents` |
| Kewajiban notifikasi regulator lewat tenggat | CRITICAL | Notifikasi ≤72 jam (UU PDP) + catat bukti |
| Rekonsiliasi pembayaran lewat SLA 48 jam | WARNING | Selesaikan di antrean keuangan |
| Approval kedaluwarsa masih PENDING | WARNING | Bersihkan antrean; cek apakah penanggung jawab tidak ada |
| Hold kedaluwarsa tapi masih terbuka | WARNING | Jalankan heartbeat; periksa error penjadwal |
| SLA kasus / tugas operasi lewat | WARNING | Tagih pemiliknya di antrean |

## 7. Upgrade & rollback

```bash
cd /opt/villa-agency-os
sudo -u villa sqlite3 /var/lib/villa/villa.db ".backup /backups/villa-prasupgrade.sqlite"
sudo -u villa git fetch && sudo -u villa git checkout <commit-bersih>
sudo -u villa make check                       # gate kode
sudo -u villa python3 scripts/preflight.py --db /var/lib/villa/villa.db \
    --anchor-dir /var/lib/villa/anchors --decisions docs/GO_LIVE_DECISIONS.md
sudo -u villa systemctl restart villa-dashboard.service
sudo -u villa systemctl start villa-scheduler.service   # jalankan heartbeat sekali, lihat hasilnya
sudo -u villa python3 src/observability.py --db /var/lib/villa/villa.db --anchor-dir /var/lib/villa/anchors
```

Rollback = `git checkout <commit-sebelumnya>` **dan** pulihkan backup bila ada
perubahan skema. Karena skema belum dibekukan sebagai migrasi bernomor (repo
masih pra-deploy pertama), satu-satunya jalur aman untuk perubahan skema
sekarang adalah: backup → verifikasi restore → deploy → verifikasi. Setelah
deploy pertama, setiap perubahan skema wajib berbentuk migrasi berurutan.

## 8. Checklist go-live (pilot 10–20 properti)

1. `docs/GO_LIVE_DECISIONS.md`: semua baris BLOCKING → `DECIDED` dengan bukti.
2. `make check` PASS pada commit yang akan dipasang.
3. `scripts/preflight.py` → **VERDICT: GO**.
4. Anker terbit dari mesin/akun berbeda, terverifikasi < 7 hari.
5. Backup harian hidup, satu uji restore penuh sudah lulus.
6. Alert monitoring tersambung ke orang yang bertanggung jawab (bukan ke folder).
7. Semua pesan keluar masih **direview manusia** sampai ambang shadow-mode (D7)
   dipenuhi; jangan melonggarkan lebih cepat dari data.
8. Nomor kasus, SLA, dan alur eskalasi diumumkan ke staf (lihat
   `docs/OPERATIONS_RUNBOOK.md`).
9. Uji coba terakhir di DB demo: jalankan `scripts/seed_demo.py`, buka
   dashboard, tunjukkan satu alur lengkap (inquiry → bayar → konfirmasi).

## 9. Insiden

Prosedur lengkap ada di `docs/RETENTION_AND_INCIDENT_RESPONSE.md`. Ringkas:

1. **Kandangkan** (stop penulisan yang berbahaya; jangan hapus bukti).
2. Catat insiden di modul `incidents` dengan playbook yang sesuai
   (`DOUBLE_BOOKING`, `PAYMENT_FRAUD`, `DATA_BREACH`, `VENUE_OUTAGE`, …).
3. Kalau ada data pribadi terlibat: tandai `regulatory_notification_required`
   dan isi `notice_due_at` (≤72 jam). Heartbeat akan menagih tenggat ini.
4. Komunikasi ke tamu/owner memakai draft yang disetujui manusia, bukan kalimat
   yang dikarang otomatis.
5. Post-mortem wajib untuk SEV1/SEV2: `postmortem_ref` harus terisi sebelum
   insiden boleh ditutup.
