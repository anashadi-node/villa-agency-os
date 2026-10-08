# ADR-0067 — Alamat pengunjung dipercaya hanya dari hop yang ditulis proxy sendiri

- Status: **ACCEPTED** (keputusan owner 2026-10-08: hosting produksi situs publik, "Approve")
- Date: 2026-10-08
- Deciders: Mas Anas (owner) · divisi engineering Villa Agency OS
- Spec: `src/marketing_site.py` (`MarketingSite(trusted_proxy=…)`, `--trusted-proxy`) · `scripts/preflight.py` (`--host-check`) · `deploy/systemd-user/villa-marketing.service` · `docs/DEPLOYMENT.md` §4.2 · `tests/test_marketing_site.py`

## Konteks

Pembatasan laju form pertanyaan tamu memakai **alamat socket pengirim** sebagai kunci
(`5` permintaan / `600` detik per alamat). Selama situs diakses langsung, itu benar.

Begitu situs berdiri di belakang reverse proxy — dan hosting produksi memang begitu, karena
TLS dan nama domain adalah tugas proxy — setiap permintaan tiba dari `127.0.0.1`. Kunci
pembatas laju lalu runtuh menjadi **satu ember untuk seluruh dunia**: lima pertanyaan per
sepuluh menit untuk semua pengunjung, dan satu pengirim otomatis bisa menghabiskan jatah
seluruh situs. Log permintaan juga berhenti berguna, karena semuanya tercatat sebagai
pengunjung yang sama.

Jalan keluarnya adalah header `X-Forwarded-For`, tetapi header itu **dapat ditulis siapa pun**.
Mempercayainya secara default akan menukar satu cacat (semua tamu berbagi jatah) dengan cacat
yang lebih buruk: satu tamu memutar alamat palsu di setiap permintaan dan pembatasan laju tidak
pernah menyala.

## Bukti yang diukur (2026-10-08)

| Percobaan | Hasil |
| --- | --- |
| Situs default (proxy tidak dinyatakan), 5 permintaan dengan XFF berputar `203.0.113.1…5` | semua `200`, permintaan ke-6 `429` — header diabaikan, jadi tidak ada jatah tambahan |
| Situs dengan `--trusted-proxy`, satu alamat 6 permintaan | 5 × `200` lalu `429` |
| Situs dengan `--trusted-proxy`, alamat kedua di tengah | `200` — tamu lain bukan korban yang pertama |
| Situs dengan `--trusted-proxy`, hop palsu di depan (`203.0.113.n, 127.0.0.1`) | tidak pernah jadi kunci; alamat ke-6 tetap `429` |
| Unit `villa-marketing.service` dijalankan pada port bebas atas salinan database | `GET /` `200`, POST sah `200` (baris bertambah 1), POST honeypot `400` (baris tetap) — unit yang ditulis benar-benar start dan masih boleh menulis |
| `scripts/preflight.py --host-check` pada sistem yang hidup | 4 pemeriksaan `OK`: header keamanan ada, honeypot ditolak `400`, dashboard `401` tanpa token, tidak ada port yang menjawab dari 4 alamat non-loopback mesin ini |

Catatan metodologi yang ikut terukur: `cp` berkas `villa.db` (mode WAL, `-wal` tidak kosong)
menghasilkan snapshot **basi** — salinan itu terlihat seperti skema yang tertinggal satu
migrasi. Salinan untuk uji harus lewat `.backup` SQLite.

## Keputusan

1. **Opt-in, default mati.** `MarketingSite` menerima `trusted_proxy` (default `False`) dan CLI
   menerima `--trusted-proxy`. Tanpa bendera itu, `X-Forwarded-For` tetap data, bukan identitas.
2. **Hanya hop terakhir yang dibaca.** `client_address_from_forwarded_for` mengambil elemen
   terakhir header. Proxy kita menulis elemen itu — dengan mengganti (`$remote_addr`) maupun
   menambahkan (`$proxy_add_x_forwarded_for`) — sedangkan pengunjung hanya dapat menyisipkan
   elemen di depannya. Elemen terakhir karena itu satu-satunya yang tidak bisa dipalsukan tamu.
3. **Gagal tertutup.** Header kosong, elemen terakhir bukan IP, atau nilai aneh apa pun jatuh ke
   alamat socket — bukan ke klaim pengunjung.
4. **Satu pengunjung, satu kunci.** Nilai dinormalkan lewat `ipaddress`, termasuk bentuk
   IPv4-mapped (`::ffff:203.0.113.7` → `203.0.113.7`) dan `ip:port` kiriman proxy, supaya satu
   tamu tidak memperoleh dua ember hanya karena notasinya berbeda.
5. **Kombinasi yang berbahaya ditolak, bukan didokumentasikan.** CLI menolak
   `--trusted-proxy` + `--allow-remote` dengan `parser.error` (exit 2): socket yang menerima
   koneksi langsung tidak punya cara membedakan proxy dari pemalsu.
6. **Unit menyalakan bendera itu, dan itu benar** karena proxy berjalan di mesin yang sama,
   port tetap loopback, dan proxy **mengganti** header. `docs/DEPLOYMENT.md` §4.2 memuat contoh
   nginx yang mengganti (`$remote_addr`) dan meneruskan `Host` utuh (`$http_host`), sebab
   pemeriksaan same-origin pada form membandingkan `Origin` dengan `Host`.
7. **Alamat yang dipakai juga alamat yang dicatat.** `log_message` memakai alamat yang sama,
   sehingga log situs menyebut pengunjung, bukan proxy.

## Konsekuensi

- Situs boleh berdiri di belakang proxy tanpa satu pun cacat pembatasan laju: jatah lewat proxy
  sama persis dengan jatah tanpa proxy.
- Ada satu kesalahan konfigurasi yang perlu dijaga manusia: memasang unit **dengan**
  `--trusted-proxy` tetapi proxy yang **menambahkan** header, bukan menggantinya. Bentuk itu
  tetap aman (hop terakhir tetap milik proxy), dan itulah alasan hop terakhir dipilih.
- Unit user tidak dapat memuat `PrivateDevices=`, `ProtectKernelModules=`, atau
  `CapabilityBoundingSet=`: pengelola user tidak bisa melepas kapabilitas yang tidak dimilikinya
  dan unit mati dengan `status=218/CAPABILITIES`. Hardening itu milik unit sistem.
- Situs belum membuktikan dirinya lewat jaringan publik; yang dibuktikan adalah empat
  pemeriksaan loopback di atas. `--host-check` sengaja tidak dijalankan otomatis di `make check`,
  karena `make check` tidak berjalan pada mesin yang melayani tamu.

## Reversibility

Menghapus `--trusted-proxy` dari unit mengembalikan perilaku lama dalam satu baris: kunci
kembali ke alamat socket. Itu benar hanya bila situs diakses langsung; di belakang proxy,
perubahan itu mengembalikan cacat satu-ember-untuk-semua, dan tes pembatasan laju akan gagal.
