# ADR-0001 — Runtime Storage: SQLite (dev/test) dengan PostgreSQL sebagai target produksi

- Status: **ACCEPTED**
- Tanggal: 2026-09-21
- Konteks fase: Phase 1 (Reservation Kernel)

## Konteks

Kernel reservasi butuh jaminan **atomik & serializable** untuk mencegah double
booking, plus audit append-only. Pilihannya:

1. **PostgreSQL** sekarang (single source of truth), atau
2. **SQLite** untuk pengembangan/uji + skema PostgreSQL disiapkan untuk produksi.

Fakta lingkungan saat keputusan diambil:

- PostgreSQL server **berjalan** di `127.0.0.1:5432` (terdeteksi), tetapi **tidak
  ada kredensial yang diberikan** kepada agen. Menebak password adalah pelanggaran
  aturan absolut repo ini (tidak boleh menebak kredensial).
- CLI `psql` tidak terpasang (koneksi hanya bisa dari klien Python).
- Modul `sqlite3` tersedia (3.53.1) — mendukung `STRICT` table, generated column,
  dan trigger `RAISE(ABORT)`, yang cukup untuk menegakkan audit append-only.

Menunda seluruh Phase 1 sampai kredensial database tersedia akan menghentikan
pekerjaan tanpa alasan teknis yang kuat.

## Keputusan

1. **Semua kode domain berbicara ke satu `Store`/koneksi DB-API**, tanpa dialek
   yang di-hard-code di tempat lain. Saat ini adapter yang aktif: **SQLite** (dev/test).
2. Skema **PostgreSQL tetap menjadi otoritas produksi** dan ikut dipelihara di
   repo: `src/schema/001_initial.pg.sql`.
3. Skema SQLite `src/schema/001_initial.sql` **wajib setara secara semantik**:
   tabel, kolom, constraint (`CHECK`, `UNIQUE`, `FOREIGN KEY`), dan trigger
   append-only. Kesetaraan dijaga oleh **parity test** (`tests/test_schema.py`)
   yang membandingkan daftar tabel/kolom/constraint dari kedua file.
4. Setiap pernyataan SQL wajib **portabel**: `?` placeholder di SQLite
   dinormalisasi ke `%s` untuk psycopg saat produksi; tidak ada fitur eksklusif
   satu dialek di kode domain (mis. `RETURNING` dihindari pada jalur tulis).
5. Migrasi berikutnya (`002_*.sql` + `002_*.pg.sql`) mengikuti pola yang sama,
   dan parity test diperluas otomatis lewat pembacaan direktori.

## Konsekuensi

**Positif**

- Pengembangan & uji tidak terblokir; test suite cepat (in-memory / file sementara).
- Kernel tetap bebas dialek, sehingga pindah ke PostgreSQL berisiko rendah.
- Aturan bisnis (overlap, hold expiry, audit) diuji dengan bukti empiris sekarang,
  bukan menunggu infrastruktur.

**Negatif / risiko**

- **Isolation level berbeda**: SQLite hanya punya lock tingkat-database
  (`BEGIN IMMEDIATE`), PostgreSQL punya `SELECT ... FOR UPDATE` dan
  `SERIALIZABLE`. Kernel karena itu **tidak** memakai asumsi PostgreSQL-only;
  proteksi overlap dilakukan lewat transaksi tulis tunggal + query konflik.
  Saat migrasi, disarankan menambahkan constraint eksklusi PostgreSQL
  (`EXCLUDE USING gist (unit_id WITH =, daterange(...) WITH &&)`) sebagai
  sabuk pengaman kedua.
- `NUMERIC` PostgreSQL vs `INTEGER` minor-unit SQLite: diselesaikan dengan
  menyimpan **minor unit integer** di kedua dialek (lihat ADR-0002 rencana).
- Perbedaan tipe waktu (TEXT ISO-8601 di SQLite vs `timestamptz` di PostgreSQL):
  kode domain selalu membaca/menulis **UTC ISO-8601**, dan skema PG memakai
  `timestamptz` dengan konversi di lapisan adapter.

## Alternatif yang ditolak

| Alternatif | Alasan ditolak |
|---|---|
| Menunggu kredensial PostgreSQL | Memblokir Phase 1 tanpa manfaat teknis; kredensial bukan prasyarat correctness kernel |
| Menebak/mencoba kredensial DB lokal | Melanggar aturan keamanan absolut repo |
| ORM penuh (SQLAlchemy) | Menambah dependency tanpa kebutuhan yang terbukti; skema eksplisit lebih mudah diaudit |
| JSON file / in-memory saja | Tidak bisa menjamin atomisitas & durability untuk uang dan inventory |

## Tindak lanjut

- [ ] Dapatkan kredensial PostgreSQL dari pemilik sistem (tidak melalui chat).
- [ ] Jalankan `src/schema/001_initial.pg.sql` di database bersih, verifikasi parity test lulus.
- [ ] Tambahkan constraint eksklusi PostgreSQL untuk anti-overlap sebagai lapisan kedua.
- [ ] Tambahkan job retensi (`retention_until` + purge harian) sebelum data tamu nyata masuk.
