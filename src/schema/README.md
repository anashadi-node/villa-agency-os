# Migrasi skema — urutan dan nomor yang hilang

Setiap berkas di direktori ini adalah satu langkah migrasi. `scripts/apply_migrations.py`
mengambil berkas dengan `sorted(SL.SCHEMA_DIR.glob("*.sql"))` (baris 77) dan melewati
berkas skema awal serta berkas berakhiran `.pg.sql` (baris 78), jadi **tiga digit awalan
adalah urutan eksekusi**, bukan nomor urut cantik yang boleh dilubangi sesuka hati.

Aturan yang berlaku:

1. **Satu perubahan skema = satu berkas baru.** Berkas yang sudah pernah dijalankan di
   lingkungan mana pun tidak boleh disunting isinya; tambahkan berkas berikutnya.
2. **Nomor berikutnya = nomor tertinggi + 1.** Saat ini tertinggi `017_property_geo.sql`,
   jadi migrasi baru bernama `018_<topik>.sql`.
3. **Kembaran PostgreSQL memakai sufiks `.pg.sql`** dengan isi yang setara. `make
   schema-parity` membandingkan keduanya; selisih akan menggagalkan gerbang.
4. **Jangan memakai ulang nomor yang sudah terpakai**, walau berkasnya sudah dihapus —
   riwayat git tetap menyimpan nomornya dan pembaca lama akan bingung.

## Kenapa tidak ada `009`

Berkas `009_*.sql` **tidak pernah ada**. Diperiksa 2026-11-13 lewat riwayat git:

```
git log --oneline --all -- 'src/schema/009*'   → kosong
git log --diff-filter=D --oneline -- 'src/schema/*' → kosong (tidak ada migrasi yang dihapus)
```

Jadi nomor `009` dilompati sejak awal, bukan hasil penghapusan. Urutan nyata yang berjalan:
`001 … 008`, lalu `010`. Lompatan ini **tidak berbahaya** — pelaksana migrasi tidak pernah
menghitung jumlah berkas, hanya mengurutkan nama — tetapi dicatat di sini supaya tidak
dikira ada migrasi yang hilang. Nomor `009` tidak akan dipakai lagi (aturan 4).
