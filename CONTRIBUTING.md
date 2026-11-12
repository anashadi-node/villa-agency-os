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

- `master` dilindungi dengan `enforce_admins: true`: **push langsung ditolak**, termasuk
  oleh pemilik. Alur wajib: buat cabang → commit → `gh pr create` → tunggu
  `pre-deploy gate (Python 3.12)` hijau → merge (squash/rebase; merge commit dilarang
  karena riwayat wajib linear). Ini bukan birokrasi: jejak PR adalah bukti audit.
- Jalankan `make check` di lokal sebelum membuka PR; gate yang sama berjalan di CI.
- Jangan pernah commit `.env`, kredensial, data tamu, atau data pembayaran. Pemindai
  rahasia berjalan di `make check` dan di GitHub *push protection*.
- Satu commit = satu tema, pesan menjelaskan *mengapa*, bukan sekadar *apa*.
