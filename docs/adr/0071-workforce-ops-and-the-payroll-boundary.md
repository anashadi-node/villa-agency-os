# ADR-0071 — Workforce Ops: kerja adalah data operasional, upah adalah ledger terpisah

- Status: **ACCEPTED** (disetujui Mas Anas, 2026-11-14; permintaan awal: "mulai Workforce Ops,
  buat ADR + skema + rencana sprint")
- Date: 2026-11-14
- Deciders: Mas Anas (owner) · divisi Villa Management & Agency
- Melengkapi: ADR-0004 (append-only evidence), ADR-0010 (§10 gerbang mutu), ADR-0032 (policy
  sebagai data berversi), ADR-0068 (presisi lokasi), ADR-0069 (`CONFIRMED` hanya setelah bukti),
  ADR-0070 (rupiah adalah nilainya)
- Spec: `src/schema/018_workforce_ops.sql` · `src/schema/001_initial.sql` (bentuk yang sama di
  basis baru) · `docs/SPRINT_WORKFORCE_OPS_2_WEEKS.md` · `tests/test_workforce_ops.py`

## Masalah

Villa management tanpa pencatatan kerja yang rapi punya satu penyakit yang berulang: **jam kerja
dan upah hidup di tempat yang berbeda**. Absensi di grup WhatsApp, jadwal shift di papan tulis,
lembur di ingatan supervisor, dan gaji di spreadsheet pribadi. Akibatnya bukan hanya salah hitung
gaji — akibatnya **sengketa yang tidak bisa diselesaikan**, karena tidak ada satu pun pihak yang
bisa menunjukkan bukti apa adanya.

Ada empat keputusan yang harus diambil bersamaan, dan salah mengambil satu saja merusak sisanya:

1. **Apakah kehadiran boleh dikoreksi?** Kalau ya, bukti jadi tidak berarti (siapa pun bisa
   menulis ulang masa lalu). Kalau tidak, satu kesalahan perangkat mengunci gaji selamanya.
2. **Apakah supervisor boleh membaca nominal gaji?** Kalau ya, data sensitif menyebar ke setiap
   lapisan operasional. Kalau tidak, supervisor tidak bisa menjawab "kenapa gaji Kadek beda".
3. **Apakah agen AI boleh menghitung upah?** Kalau ya, melanggar prinsip inti proyek (LLM tidak
   menghitung uang). Kalau tidak, siapa yang menghitung lembur 47 menit lintas tengah malam?
4. **Apakah data pribadi (NIK, NPWP, rekening) disimpan di tabel staf?** Kalau ya, satu dump
   database = satu kebocoran identitas lengkap. Kalau tidak, bagaimana payroll membayar orang?

Tanpa jawaban tertulis, tiap layar akan menjawab sendiri-sendiri — dan itu cara paling cepat
membuat sistem yang secara teknis berjalan tetapi secara operasional tidak dipercaya.

## Keputusan

ADR ini menarik **satu garis** melalui seluruh domain: **kerja** dan **upah** adalah dua benda
berbeda dengan pemilik berbeda.

1. **Kerja = data operasional yang bisa dibaca supervisor.** Siapa masuk shift mana
   (`workforce_roster_entries`), siapa benar-benar hadir (`workforce_attendance_events`), apa
   yang dikerjakan (`workforce_task_assignments`), berapa menit yang diakui
   (`workforce_timesheets`, `workforce_attendance_days`). Semua ini bisa dibaca atasan langsung
   dan dipakai untuk keputusan operasional harian.
2. **Upah = ledger terpisah yang tidak dibaca supervisor dan tidak ditulis agen.**
   `workforce_pay_rates`, `workforce_payroll_runs`, `workforce_payslips`,
   `workforce_payslip_lines` hanya dibaca peran payroll/finance dan owner; penulisnya layanan
   deterministik setelah **approval manusia**. Tidak ada agent tool yang menulis ke empat tabel
   itu (lihat §5).
3. **Pemisahan ditegakkan storage, bukan hanya service.** Tabel upah berada di file skema
   terpisah dari tabel operasional pada level hak akses basis data produksi, sehingga skrip yang
   lupa memeriksa izin tetap tidak bisa membaca gaji.
4. **Bukti kehadiran append-only.** `workforce_attendance_events` menolak `UPDATE` dan `DELETE`
   lewat trigger (`trg_workforce_attendance_no_update`, `trg_workforce_attendance_no_delete`).
   Koreksi adalah **kejadian baru** bertipe `MANUAL_ADJUST` yang **wajib** membawa `evidence_ref`
   dan `actor_id` — bukan penyuntingan sejarah. Ini penerapan ADR-0004 pada domain baru.
5. **Uang dihitung program, bukan model.** Setiap baris gaji adalah integer minor unit
   (rupiah sen), dan tabel menutup aritmetikanya sendiri:
   `CHECK (net_minor = gross_minor - deduction_minor)`, semua komponen `>= 0`. Agen AI boleh
   menjelaskan slip gaji, tidak boleh menghasilkan angkanya.
6. **Identitas dan rekening hanya sebagai ciphertext.** `workforce_staff_private` menyimpan
   `national_id_ct`, `tax_id_ct`, `bank_account_ct`, `contact_ct` sebagai ciphertext dengan
   `key_id`, `alg`, `wrapped_dek`, dan `aad_context`; `workforce_staff` sendiri hanya memuat
   identitas tampilan (`display_name`, `employee_no`, `role_code`). Dump database tanpa kunci
   tidak bisa mengidentifikasi orang atau memindahkan uang.
7. **Lokasi tidak disimpan.** Geofence hanya menghasilkan **putusan** (`geofence_verdict` ∈
   `INSIDE`/`OUTSIDE`/`UNKNOWN`) plus **bucket akurasi** (`accuracy_bucket_m`), tidak pernah
   koordinat. Ini kelanjutan ADR-0068: presisi adalah saklar, bukan data.
8. **Jam kerja, lembur, dan potongan adalah policy berversi, bukan konstanta.** Batas
   (`work_hours`, `overtime`, `break`, `leave`, `payroll_tax`, `payroll_bpjs`,
   `attendance_geofence`, `task_capacity`) disimpan di `workforce_policy_versions` dengan
   `checksum_sha256`, `approved_by`, dan jendela berlaku. Slip gaji menunjuk
   `policy_version_id` yang dipakainya, jadi gaji bulan lalu bisa direproduksi persis walau
   aturan berubah bulan ini (ADR-0032).
9. **Tidak ada orang di dua shift yang tumpang tindih.** `UNIQUE (agency_id, staff_id,
   starts_at_utc)` menutup kasus paling umum; penjaga tumpang tindih penuh (interval) ada di
   penulis roster (Sprint 2) dan pada dialek PostgreSQL memakai exclusion constraint di
   `002_pg_range_exclusions.pg.sql`, pola yang sama dengan anti-double-booking properti.
10. **Kapasitas tugas dibatasi angka, bukan niat baik.** `planned_minutes` dibatasi
    `CHECK BETWEEN 1 AND 480` per penugasan, dan penulis penugasan menolak menumpuk beban di luar
    sisa kapasitas harian orang itu (Sprint 2, `workforce_capacity` di service).

## Alasan

- **Bukti yang bisa diedit bukan bukti.** Trigger append-only lebih murah dan lebih kuat daripada
  kesepakatan prosedur, dan ia bekerja bahkan pada skrip yang tidak tahu aturan.
- **Gaji adalah data paling sensitif di perusahaan kecil.** Yang paling sering bocor bukan
  database tamu, tetapi file gaji yang diteruskan di grup chat. Memisahkan ledger upah membuat
  kebocoran itu butuh dua kegagalan, bukan satu.
- **Kepatuhan Indonesia menuntut reproduktifitas.** PPh 21 skema TER dan iuran BPJS berubah
  menurut aturan yang berlaku pada masa pajak; tanpa `policy_version_id` pada slip, audit tidak
  bisa membuktikan angka bulan lalu benar. Sistem tidak menafsirkan aturan pajak — ia
  menjalankan parameter yang disetujui dan menyimpan versinya.
- **Agen tetap berguna tanpa memegang uang.** Agen bisa menyusun draf jadwal, mengingatkan
  timesheet belum disetujui, menemukan shift yang kurang orang, dan menjelaskan slip — semuanya
  tanpa pernah menulis nominal. Ini batas yang sama dengan ADR-0069 pada pembayaran.

## Konsekuensi

- **Skema bertambah 16 tabel** (`workforce_*`) pada `018_workforce_ops.sql`, dengan bentuk yang
  sama di `001_initial.sql` sehingga basis baru dan basis hasil migrasi memegang kontrak identik.
  `001_initial.pg.sql` diregenerasi dari sumber SQLite — kembaran tidak boleh menyimpang.
- **Belum ada satu baris pun kode aplikasi** yang menulis tabel ini. ADR ini menetapkan kontrak;
  Sprint 1–2 di `docs/SPRINT_WORKFORCE_OPS_2_WEEKS.md` mengisi penulis, pembaca, dan gerbangnya.
  Sampai itu selesai, tabel kosong adalah keadaan yang benar — bukan cacat.
- **Data yang tidak boleh masuk repo:** tidak ada NIK, NPWP, rekening, atau koordinat di tree
  publik; contoh di tes memakai nilai sintetis. Penjaga PII (`make pii-scan`) tetap fail-closed.
- **Yang harus ditinjau counsel sebelum produksi:** dasar pemotongan PPh 21 (TER vs tarif
  Pasal 17), komponen BPJS yang menjadi kewajiban pemberi kerja vs pekerja, perlakuan lembur
  menurut UU Ketenagakerjaan, dan status hubungan kerja `DAILY`/`INTERN`. Sistem tidak
  menggantikan nasihat hukum/pajak; ia menjalankan parameter yang sudah disetujui.
- **Batas yang tetap berlaku:** refund, payout, perubahan rekening owner, dan perubahan tarif
  produksi tetap butuh approval manusia eksplisit; ADR ini tidak membuka satu pun dari itu.
