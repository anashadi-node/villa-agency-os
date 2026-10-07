# ADR-0061 — Efek tool agen telemetri (CT/OPS/FIN): klasifikasi READ, atau tetap di luar gateway

- Status: ACCEPTED — **opsi B dipilih owner 2026-10-07** (A ditunda sebagai perubahan terpicu; lihat §Keputusan)
- Date: 2026-10-07
- Deciders: Mas Anas (owner). Platform hanya mengusulkan + membawa bukti.
- Bagian dari: §17 (kill switch / tangga status), §56 (katalog tool), dokumen AI Workforce V1.1
  §22 butir *tool allowlist server-side*; temuan riset `docs/RESEARCH_AI_WORKFORCE_SKILLS_2026-10-06.md`

## Konteks

Tiga modul yang sudah dikirim mendaftarkan tool baca-saja ke katalog:

| Agen | Modul | Tool yang didaftarkan | Jalur tulisan |
|---|---|---|---|
| Agent 1 — Control Tower | `src/control_tower.py` | `workflow.status`, `workflow.retry_safe` | **`__init__`** (setiap konstruksi) |
| Agent 4 — Operations | `src/operations_agent.py` | `ops.workflow`, `ops.sla`, `ops.readiness` | `ensure_operations_tools()` |
| Agent 5 — Finance Admin | `src/finance_admin.py` | `finance.ar_aging`, `finance.ap_payables`, `finance.exposure` | `ensure_finance_tools()` |

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

katalog sebelum : 26 baris | TOOL_EFFECTS: 26    invarian: True
setelah konstruksi ControlTower() — satu kali, tanpa aksi lain:
katalog         : 28 baris | ditambah: workflow.retry_safe, workflow.status
invarian        : False  | 2 baris tanpa klasifikasi efek
```

Temuan di baris kedua itu lebih buruk dari yang pertama: jalur tulisnya **konstruktor**, jadi
cukup platform membangun koordinatornya — dan tidak ada fixture yang pernah membangunnya di atas
katalog yang sama, karena setiap fixture membangun `ai_tools` dari seed, bukan dari kode uji.
Sekaligus: `workflow.retry_safe` *me-requeue* pengiriman, jadi efek sebenarnya adalah **WRITE** —
di bawah opsi A mengklasifikasinya `READ` bukan sekadar melebar, melainkan **salah label**.

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

## Opsi yang diajukan (sebelum keputusan)

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

## Keputusan (owner, 2026-10-07)

**Opsi B dipilih owner.** Tidak ada permukaan wewenang yang diperlebar; opsi A ditunda sebagai
perubahan **terpicu**, bukan ditolak — pemicunya: "saat ada worker yang benar-benar memanggil
telemetri *lewat* gateway".

Yang berubah (commit `fix(ai): ADR-0061 option B` dan yang menyertainya):

| Modul | Sebelum | Sesudah |
|---|---|---|
| `src/control_tower.py` | `ensure_tower_tools()` dipanggil di `__init__` | deklarasi `TOWER_TOOLS` tinggal; pemanggilan & fungsi dihapus |
| `src/operations_agent.py` | `ensure_operations_tools()` | deklarasi `OPERATIONS_TOOLS` tinggal; fungsi dihapus |
| `src/finance_admin.py` | `ensure_finance_tools()` | deklarasi `FINANCE_TOOLS` tinggal; fungsi dihapus |
| `src/ai_gateway.py` | — | tidak disentuh: tanpa penyisipan, invarian dua arah kembali benar |

Konsekuensi yang diterima: kedelapan nama itu **tidak bisa** dipanggil lewat pintu gateway
(hari ini memang tidak ada pemanggilnya), dan kesehatan/retry tetap panggilan deterministik pada
objek tower + anggaran outbox sendiri. Otoritas agen dibuktikan oleh **penolakan** yang teruji:
`ops_tasks.create` → `AI_UNKNOWN_TOOL` (verba manusia bukan tool mesin sama sekali),
`payment.approve` → `AI_DENIED_ACTION` (tool terdaftar, deny layer tetap menolak).

## Alternatif yang ditolak

| Alternatif | Kenapa tidak |
|---|---|
| Biarkan seperti sekarang (terdaftar, tak terklasifikasi) | Invarian `ai_gateway.py` pecah dan tak ada tes yang menangkapnya — kelas cacat yang sama dengan `CONFIRMED_BOOKING_STATUSES` di OPS: angka yang tampak benar, salahnya diam |
| Klasifikasi diam-diam sebagai READ tanpa ADR | Memperlebar `READ_ONLY` seluruh tenant tanpa persetujuan owner |
| Hapus `ensure_*_tools()` tanpa jejak | Menghilangkan deklarasi niat + tes tier LOW; penolakan gateway atas verba manusia tetap perlu diuji, dan itu justru bagian yang benar |

## Verifikasi

**Opsi B — dilaksanakan 2026-10-07; 4 tes baru, semuanya RED di kode lama (worktree `HEAD`) dan
hijau setelah perbaikan:**

| Tes | Yang dikunci |
|---|---|
| `tests/test_ai_gateway.py::test_only_the_schema_writes_the_tool_catalogue` | tidak ada satu pun `src/*.py` yang menulis `ai_tools` — katalog punya satu penulis, schema |
| `tests/test_ai_gateway.py::test_the_workforces_declared_names_stay_out_of_the_door` | 8 nama deklarasi tak ada di katalog maupun `TOOL_EFFECTS`, **setelah** tower dibangun di katalog yang sama |
| `tests/test_control_tower.py::test_the_towers_tools_are_declared_and_never_registered` | deklarasi = {`workflow.status`, `workflow.retry_safe`}; katalog = himpunan berklasifikasi |
| `tests/test_control_tower.py::test_constructing_the_tower_twice_leaves_the_catalogue_alone` | 26 baris masuk, 26 baris keluar, dua konstruksi |

Ditambah di modul agen: deklarasi tetap diperiksa (LOW, tanpa verba tulis), laporan OPS/FIN
tidak menyentuh katalog, dan penolakan verba manusia di-pin dengan kode error.

**Opsi A (ditunda, terpicu):** tambahkan kedelapan nama ke `TOOL_EFFECTS` sebagai `READ` + baris
katalog §56 + perbarui `test_the_read_set_is_the_four_tools_that_change_nothing`; tes
disjointness di atas dibalik menjadi "terklasifikasi = READ". Catatan penting untuk saat itu:
`workflow.retry_safe` **bukan** READ.

## Status bukti saat ini

`tests/test_operations_agent.py` (**33 tes**), `tests/test_finance_admin.py` (23),
`tests/test_control_tower.py` (11), `tests/test_ai_gateway.py` (30) — hijau; gerbang penuh
`make check` PASS. Invarian `ai_tools ⇄ TOOL_EFFECTS` kini dijaga **di jalur nyata** (katalog
berseed + konstruksi koordinator), bukan hanya di fixture yang membangun katalog dari seed.
