# Sesi Operator & Rotasi Token (ADR-0005)

**Untuk:** pemilik agensi, GM, dan siapa pun yang memberi akses dashboard
**Berkas terkait:** `src/identity.py` (aturan), `scripts/session_token.py` (alat), `src/dashboard.py` (penerima)

---

## 1. Dua jenis kredensial, dua tujuan

| | Token mesin (statis) | Sesi operator (baru) |
|---|---|---|
| Dibuat oleh | `--token` saat menjalankan dashboard, atau otomatis saat start | `scripts/session_token.py login` |
| Menyebut siapa | tidak — hanya peran | ya — identitas + peran |
| Kedaluwarsa | tidak (harus dirotasi manual) | idle 30 menit, absolut 12 jam |
| Bisa dicabut per orang | tidak | ya, seketika |
| Jejak audit | tidak ada | `auth_events`: LOGIN, ACCESS_DENIED, SESSION_REFUSED |
| Dipakai untuk | cron, skrip, integrasi | manusia |

Keduanya diterima dashboard lewat header yang sama:

```
Authorization: Bearer <token>
# atau
X-Dashboard-Token: <token>
```

**Jangan** memberi sesi operator ke skrip otomatis: sesi mati dalam 30 menit tanpa aktivitas dan setiap login butuh kode TOTP.

---

## 2. Alur lengkap: dari orang baru sampai bisa masuk

Semua perintah dijalankan di server, oleh orang yang sudah punya akses shell.

### Langkah 1 — daftarkan faktor TOTP

```bash
python3 scripts/session_token.py enroll --db data/villa.db --identity alice
```

Yang keluar:

```
faktor     : mfa_e281a4f30033ab87 (status DISABLED sampai dikonfirmasi)
secret_ref : alice
variabel   : VILLA_TOTP_SECRET_ALICE=<secret>
otpauth    : otpauth://totp/Villa:alice?secret=...&issuer=Villa&digits=6&period=30
```

Tiga hal yang wajib dilakukan sekarang:

1. Pindai `otpauth://` dengan aplikasi authenticator (atau masukkan manual).
2. Simpan secret di password manager. **CLI tidak menyimpannya** — yang masuk database hanya `secret_ref` (`alice`), bukan secret-nya.
3. Pasang secret sebagai variabel lingkungan proses dashboard, **bukan** di git:

```bash
# /etc/villa/dashboard.env  (mode 0600, milik user service)
VILLA_TOTP_SECRET_ALICE=XXXX...
```

Nama variabel = `VILLA_TOTP_SECRET_` + `secret_ref` huruf besar, `-`/`.` menjadi `_`.

### Langkah 2 — aktifkan faktor

```bash
set -a; . /etc/villa/dashboard.env; set +a
python3 scripts/session_token.py confirm --db data/villa.db --identity alice --code 123456
```

Kode diambil dari aplikasi authenticator. Faktor yang belum dikonfirmasi **tidak bisa dipakai login**.

### Langkah 3 — buka sesi

```bash
python3 scripts/session_token.py login --db data/villa.db --identity alice --role owner --code 123456
```

Peran yang memerlukan kode: **owner, finance_manager, gm**. Peran lain (reservation_agent, reservation_manager, auditor) tidak diminta kode — itu keputusan ADR-0005 §1, bukan kelalaian; lihat §5 untuk batasnya.

Keluaran:

```
identitas  : alice (owner)
sesi       : sess_b20af9a2f3f4d617
idle s/d   : 2026-10-29T16:39:29+00:00
absolut s/d: 2026-10-30T04:09:29+00:00
token (tampil sekali, kirim sebagai 'Authorization: Bearer ...'):
478a8843...
```

Token **hanya ditampilkan sekali**; yang disimpan database adalah hash SHA-256-nya.

> **Kode TOTP sekali pakai.** Kalau `confirm` baru saja memakai kode untuk menit ini, `login` dengan kode yang sama akan ditolak `MFA_CODE_REPLAYED`. Tunggu kode berikutnya (±30 detik).

### Langkah 4 — pakai

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/finance
```

Sesi menyegarkan batas idle setiap permintaan, tetapi **tidak pernah** melewati batas absolut 12 jam.

### Langkah 5 — akhiri

```bash
python3 scripts/session_token.py revoke --db data/villa.db --identity alice
```

Sesi yang dicabut langsung ditolak pada permintaan berikutnya (401) dan peristiwanya tercatat di `auth_events`.

---

## 3. Perintah lain

| Perintah | Kegunaan |
|---|---|
| `list --db ...` | daftar semua sesi + status aktif/REVOKED + dua batas waktunya |
| `rotate --db ... --token <lama>` | tukar token; token lama mati pada transaksi yang sama |
| `revoke --session-id <id>` | cabut satu sesi saja (mis. laptop hilang) |
| `revoke --identity alice --reason "resign"` | cabut semua sesi orang itu sekaligus |

Semua perintah menolak peran yang tidak ada di `src/roles.py` (tidak ada peran karangan).

---

## 4. Rotasi token mesin (90 hari)

Token mesin tidak punya kedaluwarsa; itu harus diganti manusia. Jadwal yang disarankan:

| Kapan | Tindakan |
|---|---|
| Setiap 90 hari | buat token baru, pasang, restart dashboard, hapus token lama |
| Salah satu anggota tim keluar | rotasi **semua** token mesin + cabut semua sesi orang itu |
| Kecurigaan kebocoran | rotasi hari itu juga, lalu periksa `auth_events` untuk login yang tidak dikenal |

```bash
# buat token baru (192 bit)
python3 -c "import secrets; print(secrets.token_urlsafe(24))"

# pasang di unit systemd (mode 0600), lalu
systemctl --user restart villa-dashboard
```

Catat tanggal rotasi terakhir di buku operasi. Token yang tidak pernah dirotasi adalah kredensial permanen — persis yang ingin dihindari ADR-0005.

---

## 5. Batas jujur (jangan diklaim lebih)

1. **Tidak ada faktor kata sandi.** Platform tidak menyimpan password. Sesi dibuka dengan menyatakan identitas + kode TOTP; siapa pun yang bisa menjalankan `session_token.py` sudah bisa membaca database. Yang sesi ini tambahkan adalah **akuntabilitas, kedaluwarsa, dan pencabutan** — bukan pertahanan terhadap penyerang lokal.
2. **Peran tidak diminta dari pengguna saat request.** Peran disimpan di baris sesi; header `X-Role` diabaikan (diuji: 403).
3. **Dashboard tetap hanya untuk loopback/VPN.** Sesi tidak mengubah itu. Membuka dashboard ke internet tetap memerlukan login berlapis + MFA untuk semua peran, dan itu belum ada.
4. **Kode TOTP bisa dipakai ulang antar sesi?** Tidak. `last_used_step` menolak langkah yang sudah terpakai (`MFA_CODE_REPLAYED`).
5. **Sesi di memori?** Tidak — sesi ada di database, jadi restart dashboard tidak membatalkan sesi yang masih berlaku. Untuk memaksa semua keluar, cabut per identitas atau per sesi.

---

## 6. Jika ada yang tidak beres

| Gejala | Penyebab paling sering | Perbaikan |
|---|---|---|
| `401` padahal token baru | token salah salin, atau sesi sudah idle 30 menit | `list`, lalu `login` lagi |
| `MFA_REQUIRED` saat login | peran owner/finance/gm tanpa `--code` | kirim kode dari authenticator |
| `MFA_FACTOR_INACTIVE` | `confirm` belum dijalankan | jalankan `confirm` dengan kode |
| `MFA_CODE_REPLAYED` | kode menit ini sudah dipakai | tunggu kode berikutnya |
| `UNKNOWN_FACTOR` / secret tidak ditemukan | variabel `VILLA_TOTP_SECRET_*` tidak ada di lingkungan proses | pasang di `EnvironmentFile` lalu restart |
| `SESSION_NOT_FOUND` | token milik database lain, atau sudah dirotasi | cek `--db` yang benar |
