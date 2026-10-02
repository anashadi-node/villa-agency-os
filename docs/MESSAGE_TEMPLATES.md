# Pustaka Template Pesan — Villa Agency OS

**Untuk:** Owner Villa Agency (Bali & Yogyakarta) — dan setiap agency yang memakai sistem ini
**Tanggal keputusan owner:** 2026-10-02 · **Edisi dokumen:** 2026-10-02
**Tujuan dokumen ini:** memperlihatkan **seluruh kalimat** yang boleh dikirim sistem kepada tamu, dan aturan yang membuat kalimat itu tidak bisa dikarang.

---

## 1. Aturan yang berlaku untuk setiap pesan

| Aturan | Artinya | Yang menegakkannya |
|---|---|---|
| **Template ditulis platform, bukan tenant** | Kalimat resmi disediakan sistem; tenant menyetel **nomor WhatsApp** dan kanal, bukan mengarang isi pesan | dokumen ini + `src/messages.py` |
| **Tanpa angka yang diketik** | Template **tidak boleh mengandung digit sama sekali**; setiap angka/uang/tanggal harus datang dari data bersumber (invoice, quote, booking) | tes `test_templates_carry_no_literal_digits` |
| **Setiap fakta harus punya sumber** | Sebuah kalimat tidak boleh dikirim kalau tidak ada baris database yang mendukungnya (harga dari mana, tanggal dari mana) | grounding `src/messages.py` + gerbang `src/gates.py` |
| **Draf di-approve manusia sebelum dikirim** | AI menyiapkan, manusia menyetujui, lalu manusia mencatat pengiriman | `approvals` + `SEND` role |
| **Tamu tidak pernah diminta mengirim dokumen identitas lewat chat** | Chat bukan tempat untuk scan KTP/paspor; kelas data P3 memang **dilarang** keluar ke kanal chat | tes `test_no_template_asks_a_guest_to_send_identity_documents` + `gates.POLICY_TABLE` |

**Kanal yang sah:** `website`, `email`, `whatsapp_business`, `dashboard`, `phone`.

**Di mana kalimat ini disimpan:** `templates/messages/id-ID.json` — sebuah **berkas data**, bukan kode Python. Alasannya dua: (1) copy untuk tamu adalah *katalog*, bukan logika, sehingga dihitung dan dipin secara terukur oleh tes bahasa (`tests/test_language_standard.py`), bukan menyusup di dalam modul; (2) sesuai ADR-0037, berkas inilah tempat **tenant** nanti menyunting kalimatnya — platform menyediakan kalimatnya, tenant memiliki kata-katanya, tanpa perlu menyentuh Python. Katalog yang hilang atau rusak **ditolak** (`MESSAGE_CATALOGUE_MISSING`), tidak dianggap kosong.

---

## 2. Siklus pesan (12 template)

### Tahap 1 — Pertanyaan sampai penawaran

#### 1. `availability_offer` — ketersediaan unit
> Halo {guest_name}, unit {unit_code} masih tersedia untuk {check_in} sampai {check_out} ({nights} malam). Tarif {nightly_rate} per malam, total {total_amount}. Balas pesan ini bila unit ingin kami tahan untuk Anda.

Sumber angka: tarif & total dari **kebijakan harga** (`policy_version`), tanggal dari **booking**.

#### 2. `quote_offer` — penawaran resmi
> Halo {guest_name}, berikut penawaran menginap di {unit_code} untuk {check_in} sampai {check_out} ({nights} malam). Akomodasi {accommodation_amount}, biaya kebersihan {cleaning_amount}, pajak {tax_amount}, total {total_amount}. Penawaran berlaku sampai {valid_until}. Balas pesan ini untuk melanjutkan.

Sumber angka: seluruhnya dari **quotation** (`quote`) — bukan dari ingatan AI.

#### 3. `hold_confirmation` — unit ditahan
> Halo {guest_name}, unit {unit_code} kami tahan atas nama Anda sampai {hold_expires_at}. Mohon konfirmasi sebelum waktu tersebut agar unit tetap tersedia untuk {check_in} sampai {check_out}.

### Tahap 2 — Pesanan terkonfirmasi & uang masuk

#### 4. `booking_confirmed` — pesanan terkonfirmasi *(baru 2026-10-02)*
> Halo {guest_name}, pesanan Anda di {property_name} unit {unit_code} sudah terkonfirmasi untuk {check_in} sampai {check_out} ({nights} malam). Nomor pesanan Anda {booking_ref}. Check-in mulai {check_in_time}, host Anda {host_name}, dihubungi lewat {contact_channel}. Terima kasih telah memesan.

#### 5. `deposit_received` — pembayaran diterima *(baru 2026-10-02)*
> Halo {guest_name}, kami sudah menerima pembayaran sebesar {amount_received} untuk pesanan {booking_ref}, tercatat pada invoice {invoice_ref}. Bila masih ada sisa, kami kabari sebelum kedatangan Anda pada {check_in}. Terima kasih.

#### 6. `payment_reminder` — pengingat pembayaran
> Halo {guest_name}, pengingat pembayaran invoice {invoice_ref} sebesar {amount_due} yang jatuh tempo {invoice_due}. Mohon bayar melalui {payment_channel} atas nama {account_name}. Balas pesan ini bila sudah bayar.

#### 7. `balance_due_notice` — sisa pembayaran menjelang tiba *(baru 2026-10-02)*
> Halo {guest_name}, menjelang kedatangan Anda pada {check_in} di unit {unit_code}, masih ada sisa pembayaran {amount_due} pada invoice {invoice_ref} yang jatuh tempo {invoice_due}. Pembayaran melalui {payment_channel} atas nama {account_name}. Balas pesan ini bila sudah membayar.

### Tahap 3 — Kedatangan, menginap, sesudahnya

#### 8. `pre_arrival_reminder` — konfirmasi jam tiba *(baru 2026-10-02)*
> Halo {guest_name}, kedatangan Anda di unit {unit_code} pada {check_in} sebentar lagi. Agar unit kami siapkan dengan baik, mohon kabari perkiraan jam tiba Anda kepada {host_name} lewat {contact_channel}, dan beri tahu bila jumlah tamu berubah. Petunjuk kedatangan kami kirim sebelum hari kedatangan.

#### 9. `arrival_instructions` — petunjuk kedatangan
> Halo {guest_name}, petunjuk kedatangan {unit_code}, {property_name}: check-in mulai {check_in_time} oleh {host_name}. Kode akses dikirim oleh {host_name} melalui {contact_channel}. Bila tiba di luar jam tersebut, hubungi {host_name} pada nomor reservasi {booking_ref}.

*(Catatan privasi: kode akses dikirim **oleh manusia host**, bukan oleh template ini — template menyebut siapa yang mengirim, tidak memuat kodenya.)*

#### 10. `post_stay_feedback` — terima kasih & ulasan *(baru 2026-10-02)*
> Halo {guest_name}, terima kasih telah menginap di {property_name} unit {unit_code} sampai {check_out}. Bila ada yang kurang nyaman, sampaikan kepada kami lewat {feedback_channel} agar kami perbaiki. Kami senang bila Anda berkenan menuliskan ulasan. Sampai jumpa lagi.

### Tahap 4 — Hal yang tidak menyenangkan (harus tetap dikatakan)

#### 11. `complaint_ack` — komplain diterima
> Halo {guest_name}, laporan Anda mengenai {issue_summary} sudah kami terima dan kami mohon maaf atas ketidaknyamanannya. Laporan tercatat sebagai {ticket_ref} dan ditangani oleh {owner_name} paling lambat {resolution_due}. Kami akan mengabari Anda melalui {contact_channel}.

#### 12. `refund_in_progress` — pengembalian dana diproses *(baru 2026-10-02)*
> Halo {guest_name}, permintaan pembatalan Anda untuk pesanan {booking_ref} unit {unit_code} sudah kami proses. Pengembalian dana sebesar {refund_amount} kami kirim melalui {payment_channel}, ditargetkan selesai {resolution_due}. Kami kabari begitu dana dikirim.

---

## 3. Apa yang **tidak** ada di pustaka ini, dan kenapa

| Tidak ada | Alasan |
|---|---|
| Permintaan scan KTP/paspor lewat chat | Kelas P3 **dilarang** keluar ke kanal chat (gerbang klasifikasi data). Pendaftaran tamu dilakukan dengan cara yang terlindungi, bukan lewat pesan |
| Kalimat harga yang diketik langsung | Semua angka wajib bersumber; template tanpa digit dipaksa oleh tes |
| Janji di luar kebijakan | Setiap klaim harus punya baris database; kalimat yang tidak bisa dibuktikan **tidak boleh dikirim** |
| Pesan promosi massal | Belum diputuskan pemilik sistem; pustaka ini hanya pesan operasional per pesanan |

---

## 4. Yang disetel tenant (bukan milik platform)

Nomor WhatsApp Business, nama kanal, nama host, dan jam check-in **diisi masing-masing agency** (keputusan owner 2026-10-02, ADR-0037). Platform menyediakan kalimatnya; tenant mengisi datanya — dan setiap data yang diisi tenant tetap harus berasal dari baris yang sah, bukan diketik ke dalam pesan.
