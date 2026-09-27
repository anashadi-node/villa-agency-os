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

```nginx
server {
    listen 443 ssl;
    server_name ops.example.com;
    ssl_certificate     /etc/letsencrypt/live/ops.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ops.example.com/privkey.pem;

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
