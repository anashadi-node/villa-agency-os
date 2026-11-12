# Berkontribusi — Tidak Diterima dari Luar

Repositori ini adalah **cermin publik baca-saja** dari proyek internal Villa Agency OS.

## Aturannya

1. **Pull request dari luar tidak diterima** dan akan ditutup tanpa peninjauan. Ini
   keputusan pemilik proyek, bukan penilaian atas mutu usulan Anda.
2. **Tidak ada hak tulis** untuk siapa pun selain pemilik. Percabangan `master`
   dilindungi; *force-push* dan penghapusan cabang diblokir.
3. **Isu tidak dibuka** di repositori ini. Pertanyaan tentang isi repo tidak dijawab
   lewat jalur publik.
4. **Kerentanan** dilaporkan secara privat: lihat [`SECURITY.md`](SECURITY.md).

## Yang tetap boleh dilakukan

- Membaca, mengkloning, dan menjalankan kode untuk menelaah atau mengaudit klaim teknis.
- Mengutip singkat dengan atribusi (lihat [`LICENSE`](LICENSE)).
- Melaporkan kekeliruan **faktual** (mis. angka di dokumen tidak cocok dengan kode) —
  sertakan bukti yang dapat direproduksi. Laporan semacam ini dibaca, tetapi tetap tidak
  mengubah aturan "tanpa PR eksternal": perbaikan dilakukan pemilik di dalam repo.

## Untuk pemelihara

- Jalankan `make check` sebelum setiap push; gate yang sama berjalan di CI (`check.yml`).
- Jangan pernah commit `.env`, kredensial, data tamu, atau data pembayaran. Pemindai
  rahasia berjalan di `make check` dan di GitHub *push protection*.
- Satu commit = satu tema, pesan menjelaskan *mengapa*, bukan sekadar *apa*.
