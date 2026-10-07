# ADR-0061 — Efek tool agen telemetri (OPS/FIN): klasifikasi READ, atau tetap di luar gateway

- Status: PROPOSED (menunggu keputusan owner — belum mengubah wewenang apa pun)
- Date: 2026-10-07
- Deciders: Mas Anas (owner). Platform hanya mengusulkan + membawa bukti.
- Bagian dari: §17 (kill switch / tangga status), §56 (katalog tool), dokumen AI Workforce V1.1
  §22 butir *tool allowlist server-side*; temuan riset `docs/RESEARCH_AI_WORKFORCE_SKILLS_2026-10-06.md`

## Konteks

Dua agen yang sudah dikirim mendaftarkan tool baca-saja ke katalog:

| Agen | Modul | Tool yang didaftarkan |
|---|---|---|
| Agent 4 — Operations | `src/operations_agent.py` | `ops.workflow`, `ops.sla`, `ops.readiness` |
| Agent 5 — Finance Admin | `src/finance_admin.py` | `finance.ar_aging`, `finance.ap_payables`, `finance.exposure` |

`src/ai_gateway.py` menyatakan invarian **dua arah** di atas `TOOL_EFFECTS`: *"Every tool in
`ai_tools` must appear here, and no tool may appear that the catalogue does not hold (both
directions are tested)"* — dan invarian itu memang diuji
(`tests/test_ai_gateway.py::test_every_registered_tool_is_classified_and_nothing_else_is`).

**Bukti terukur (2026-10-07, DB berseed penuh, `schema_loader.connect_sqlite`)**

```
katalog sebelum : 26 baris | TOOL_EFFECTS: 26    invarian: True
setelah ensure_operations_tools() + ensure_finance_tools():
katalog         : 32 baris | ditambah: finance.ap_payables, finance.ar_aging, finance.exposure,
                                ops.readiness, ops.sla, ops.workflow
invarian        : False  | 6 baris tanpa klasifikasi efek
```

Akibatnya hari ini: gateway **menolak** keenam tool itu dengan `AI_TOOL_EFFECT_UNKNOWN`
(`ToolEffectUnknown`) setiap kali dipanggil. Itu arah yang aman — ditolak, bukan dieksekusi —
tetapi berarti:

1. Tool yang didaftarkan agen **belum bisa dipakai** oleh worker mana pun lewat gateway.
2. Invarian yang didokumentasikan di `ai_gateway.py` **pecah** begitu `ensure_*_tools()`
   dijalankan (suite tetap hijau karena fixture `world` memakai katalog berseed, bukan katalog
   yang sudah ditambahi — jadi invarian itu tidak dijaga di jalur ini).
3. Butir §22 *tool allowlist server-side* karena itu **belum bisa diklaim ✅** untuk OPS/FIN.

Kenapa tidak langsung diklasifikasi saja: himpunan READ di `TOOL_EFFECTS` **dipin sengaja** oleh
`tests/test_ai_gateway.py::test_the_read_set_is_the_four_tools_that_change_nothing` dengan alasan
tertulis — *"widening READ widens the read-only rung for every tenant at once"*. Menambah 6 nama
READ berarti memperlebar rung `READ_ONLY` untuk **semua tenant** sekaligus. Itu perubahan
permukaan wewenang, dan aturan proyek: perubahan otoritas agen butuh ADR + analisis risiko +
bukti + persetujuan owner. Karena itu ADR ini berstatus PROPOSED.

## Keputusan yang diusulkan

| Opsi | Isi | Dampak wewenang | Catatan |
|---|---|---|---|
| **A — klasifikasi READ** | Tambahkan 6 tool ke `TOOL_EFFECTS` sebagai `READ` + perluas katalog §56 + perbarui tes yang mem-pin himpunan READ | **Memperlebar rung `READ_ONLY` semua tenant** | Perlu persetujuan owner; setelah ini tool agen benar-benar bisa dipanggil lewat gateway |
| **B — tetap di luar gateway** (rekomendasi platform) | Hentikan `ensure_*_tools()` menyisipkan nama tool baru ke `ai_tools`; telemetri tetap fungsi pustaka deterministik (`collect()` dipanggil kode/dashboard/CLI), dan otoritas agen dibuktikan oleh **penolakan** gateway atas verba manusia (`ops_tasks.create`, `payment.approve`) untuk principal itu | **Tidak mengubah** permukaan wewenang | Invarian `TOOL_EFFECTS ⇄ ai_tools` kembali benar; butir §22 dijawab sebagai "allowlist = penolakan yang diuji", bukan "tool terdaftar" |
| **C — kelas efek ketiga (`REPORT`)** | Tambah rung efek baru khusus telemetri yang tak boleh masuk konteks LLM | Mengubah tangga §17 | Perubahan besar; tidak disarankan sebelum ada worker LLM yang benar-benar memerlukannya |

**Rekomendasi platform: B sekarang, A saat benar-benar dibutuhkan.** Alasannya: sampai ada
worker yang memanggil telemetri *lewat* gateway (hari ini OPS/FIN adalah pustaka yang dipanggil
kode deterministik), mengklasifikasi READ hanya menambah permukaan wewenang tanpa pemakai —
persis bentuk perubahan yang dokumen ini larang demi keamanan. B juga langsung menutup invarian
yang pecah, tanpa menyentuh rung tenant mana pun.

## Alternatif yang ditolak

| Alternatif | Kenapa tidak |
|---|---|
| Biarkan seperti sekarang (terdaftar, tak terklasifikasi) | Invarian `ai_gateway.py` pecah dan tak ada tes yang menangkapnya — kelas cacat yang sama dengan `CONFIRMED_BOOKING_STATUSES` di OPS: angka yang tampak benar, salahnya diam |
| Klasifikasi diam-diam sebagai READ tanpa ADR | Memperlebar `READ_ONLY` seluruh tenant tanpa persetujuan owner |
| Hapus `ensure_*_tools()` tanpa jejak | Menghilangkan deklarasi niat + tes tier LOW; penolakan gateway atas verba manusia tetap perlu diuji, dan itu justru bagian yang benar |

## Verifikasi (setelah keputusan)

- Opsi B: `ensure_*_tools()` berhenti menulis ke `ai_tools`; tes invarian di jalur "katalog +
  ensure" ditambahkan; tes `test_read_tools_are_declared_but_not_yet_classified`
  (`tests/test_operations_agent.py::TestDefinitionOfDone`) diganti dengan "penolakan verba
  manusia tetap berlaku".
- Opsi A: `TOOL_EFFECTS` + seed §56 + tes pin READ diperbarui; tes yang sama dibalik menjadi
  "efek terklasifikasi = READ".

## Status bukti saat ini

4 tes `TestDefinitionOfDone` di `tests/test_operations_agent.py` (32 tes OPS hijau) mengunci
keadaan hari ini apa adanya, termasuk penolakan `AI_TOOL_EFFECT_UNKNOWN` — sehingga keputusan
owner nanti hanya perlu **membalik satu tes**, bukan mencari lagi apa yang sebenarnya terjadi.
