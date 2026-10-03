# ADR-0059 — Adapter kanal: satu pintu bertanda tangan, pengiriman tetap tindakan manusia

- Status: Accepted
- Date: 2026-10-03
- Deciders: platform + Mas Anas (modul Phase 4 terakhir sesuai rencana eksekusi)
Bagian dari: Master Spec §4/§10 (kanal resmi, data eksternal = untrusted), ADR-0059 melengkapi
ADR-0004 (API contract) dan rencana eksekusi Phase 2→4.

## Konteks

Estate menolak pesan dari mesin (`messages` menolak aktor mesin) dan menganggap semua isi
kanal eksternal sebagai data tak tepercaya (`case_messages.untrusted=1`). Yang belum ada:
pintu masuk yang **memverifikasi** bahwa webhook benar-benar dari platform kanal milik tenant
itu, dan jalur keluar yang membawa bukti transport (`external_ref`) tanpa melewati aturan
"mengirim = manusia".

## Keputusan

1. **`verify_webhook`** — HMAC-SHA256 atas body mentah, hex, dibandingkan
   `hmac.compare_digest` (constant-time). Secret per kanal **disetel per tenant** lewat
   konstruktor adapter (white-label: tiap agency membawa secret-nya sendiri); secret kosong =
   `MissingSecret`; body non-teks = ditolak. Verifikasi **sebelum** tulis apa pun: signature
   ditolak = tidak ada baris, tidak ada event, tidak ada timeline.
2. **`ChannelAdapter.receive`** — verifikasi dulu, lalu **mendelegasikan** ke
   `cases.record_inbound` (pemilik fakta menulis; adapter menulis nol baris). Replay
   (idempotency key sama) ditolak pemilik — adapter meneruskannya apa adanya.
3. **`ChannelAdapter.deliver`** — hanya draft berstatus `APPROVED`; memanggil
   `transport.send` lalu `messages.mark_sent` dengan aktor **manusia** dan
   `external_ref` = kuitansi transport. Mesin tetap tidak bisa mengirim; transport
   melaksanakan tindakan yang sudah disetujui manusia.
4. **`FakeTransport`** — transport uji deterministik (`fake-<channel>-<n>`), tanpa jaringan;
   pola transport asli mengikuti bentuk ini (satu metode, kuitansi wajib).

## Alternatif ditolak

| Alternatif | Kenapa |
|---|---|
| Secret global platform | Satu kebocoran membuka pintu semua tenant; white-label berarti isolasi per tenant |
| Signature atas payload yang sudah di-parse | Tanda tangan hidup di kawat; mem-parse dulu berarti mempercayai sesuatu sebelum memverifikasi |
| Adapter menulis langsung ke `case_messages` | Satu penulis per tabel — `cases` adalah pemiliknya |
| Transport boleh mengirim draft PENDING | Mengirim = tindakan manusia; hanya `APPROVED` yang boleh keluar |

## Verifikasi

- `tests/test_channels.py` — 10 tes: signature benar direkam oleh pemilik fakta; signature
  salah/tamper/missing ditolak dengan **nol baris**; secret tenant lain tidak membuka pintu
  tenant ini (white-label by construction); replay = satu fakta; draft belum disetujui tidak
  pernah menyentuh transport; draft terkirim membawa kuitansi + nama manusia; aktor mesin
  ditolak; kegagalan transport tidak meninggalkan rekaman.
- Full suite + `make check` → PASS (3380→3... tes, twin parity OK).
