"""Layar per peran Workforce Ops (T2.6): yang dikunci, dan mengapa.

Tiga orang melihat fakta upah yang sama dan butuh tiga gambar berbeda — supervisor
(shift yang sedang berjalan), keuangan (uangnya), dan pemilik (portofolio). Uji di
sini mengunci empat janji modul ``src/workforce/views.py``; masing-masing punya
kontrol negatif supaya "lulus" tidak berarti "tidak diperiksa":

* **Uang selalu IDR + rupiah dulu.** Setiap angka rupiah wajib diikuti referensi USD
  pada baris yang sama; kalau policy tidak menerbitkan kurs, rupiah tetap ada,
  USD hilang, dan halaman mengatakan sebabnya.
* **Tidak ada istilah mesin di layar supervisor.** Setelah tag dan placeholder
  dibuang, tidak boleh tersisa kode seperti ``PRESENT``/``CALCULATED``, id seperti
  ``stf_``/``wfr_``, atau nama tabel.
* **Katalog label lengkap.** Setiap status yang boleh ada di basis data (dibaca dari
  ``CHECK`` constraint DDL, bukan dari daftar yang saya ketik) wajib punya kata.
* **Layar ini read-only.** Dua kali render tidak mengubah satu baris pun.

Data seluruhnya sintetis: memakai dunia G2 (nama ``Ops Staff NN``, nominal bulat)
dengan kurs terbitan ``16.250`` yang berlaku sampai 48 jam.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import fx as FX  # noqa: E402
import money_display as MD  # noqa: E402
import policy_engine as PE  # noqa: E402
import workforce.views as V  # noqa: E402
from primitives import FrozenClock, canonical_json  # noqa: E402
from test_workforce_reconcile import G2, World  # noqa: E402

AGENCY = G2.AGENCY
RATE_DAY = "2026-11-30"
USD_IDR = "16250"
SOURCE = "§11 Pricing, Quote & Discount — audit 2026-11-13"
OWNER = "human_owner_anas"

#: Huruf kapital yang boleh terlihat pembaca, dan alasannya masing-masing:
#: ``IDR``/``USD`` adalah kode mata uang yang justru diminta Owner ada di tiap angka,
#: ``WIB`` adalah nama zona waktu yang dibaca orang Indonesia. Selain ini, huruf kapital
#: di layar berarti kode mesin yang lolos.
READER_TOKENS = ("IDR", "USD", "WIB")

#: Yang tidak boleh muncul di layar yang dibaca staf.
MACHINE_TOKENS = re.compile(
    r"\b(?!" + "|".join(f"{token}\\b" for token in READER_TOKENS) + r")"
    r"(?:[A-Z][A-Z_]{2,}|stf_[a-z0-9]+|wfr_[a-z0-9]+|workforce_[a-z_]+)\b"
)


@pytest.fixture()
def world():
    instance = World()
    yield instance
    instance.close()


def publish_rate(conn, *, quoted_on: str = RATE_DAY, max_age_hours: int = 48) -> None:
    """Terbitkan kurs lewat policy — satu-satunya jalan angka USD boleh muncul."""
    resolver = PE.PolicyResolver(conn, clock=FrozenClock.at(G2.CLOCK_NOW), agency_id=AGENCY)
    resolver.publish(
        FX.FX_RATES_KEY,
        canonical_json({"USD/IDR": USD_IDR}),
        effective_from=quoted_on,
        source_doc_ref=SOURCE,
        approved_by=OWNER,
    )
    resolver.publish(
        FX.FX_MAX_AGE_KEY,
        str(max_age_hours),
        effective_from="2026-11-01",
        source_doc_ref=SOURCE,
        approved_by=OWNER,
    )


@pytest.fixture()
def rich(world):
    """Dunia G2 + kurs terbitan + jam tepat di tengah periode roster."""
    # Satu timesheet dikembalikan ke "menunggu tanda tangan": tanpa itu tabel supervisor
    # kosong, dan uji "setiap angka rupiah punya referensi USD" akan lulus tanpa memeriksa
    # apa pun. Baris menunggu diambil dari slip yang benar-benar ada, bukan dikarang.
    waiting = world.conn.execute(
        "SELECT t.timesheet_id FROM workforce_timesheets t"
        " JOIN workforce_payslips p ON p.timesheet_id = t.timesheet_id"
        " WHERE t.agency_id = ? ORDER BY t.staff_id LIMIT 1",
        (AGENCY,),
    ).fetchone()
    assert waiting is not None, "dunia uji tanpa slip terhubung timesheet"
    world.conn.execute(
        "UPDATE workforce_timesheets SET status = 'SUBMITTED' WHERE timesheet_id = ?",
        (waiting["timesheet_id"],),
    )
    work_date = world.conn.execute(
        "SELECT MIN(work_date) AS day FROM workforce_roster_entries"
        " WHERE agency_id = ? AND status IN ('PUBLISHED','CONFIRMED')",
        (AGENCY,),
    ).fetchone()["day"]
    assert work_date, "dunia uji tanpa roster: layar supervisor tidak akan punya baris"
    publish_rate(world.conn, quoted_on=str(work_date))
    world.screen_clock = FrozenClock.at(f"{work_date}T02:00:00Z")
    world.work_date = str(work_date)
    world.views = V.WorkforceViews(world.conn, clock=world.screen_clock, agency_id=AGENCY)
    return world


def visible(html: str) -> str:
    """Teks yang benar-benar dibaca pembaca.

    Tag, gaya (``<style>``), skrip, komentar HTML, dan placeholder dibuang. Tanpa membuang
    gaya, nama kelas dan komentar CSS seperti ``data-state="BOOKED"`` akan terbaca sebagai
    istilah mesin — padahal tidak satu pun kata itu sampai ke layar.
    """
    text = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\{\{[^}]*\}\}", " ", text)


def money_figures(html: str) -> list[str]:
    """Setiap kemunculan angka rupiah beserta apa pun yang mengikutinya di baris itu."""
    return re.findall(r"IDR [\d.]+(?:\s*\((?:[^)]*)\))?", html)


# --------------------------------------------------------------------------- #
# 1. Uang: rupiah dulu, referensi USD wajib
# --------------------------------------------------------------------------- #
def test_every_rupiah_figure_carries_a_dollar_reference(rich):
    rendered = {
        screen: rich.views.render(screen, role=role)
        for screen, role in (
            ("workforce_supervisor", "operations"),
            ("workforce_finance", "finance"),
            ("workforce_owner", "owner"),
        )
    }
    for screen, html in rendered.items():
        figures = money_figures(html)
        assert figures, f"{screen}: tidak ada satu pun angka rupiah untuk diperiksa"
        for figure in figures:
            assert "(≈ USD" in figure, f"{screen}: {figure!r} tanpa referensi USD"
        assert "≈ USD" in html, screen


def test_without_a_published_rate_the_rupiah_stays_and_the_dollar_goes(world):
    """Kontrol negatif: kurs tidak diterbitkan ⇒ jangan mengarang kurs, katakan sebabnya."""
    work_date = world.conn.execute(
        "SELECT MIN(work_date) AS day FROM workforce_roster_entries WHERE agency_id = ?",
        (AGENCY,),
    ).fetchone()["day"]
    views = V.WorkforceViews(
        world.conn, clock=FrozenClock.at(f"{work_date}T02:00:00Z"), agency_id=AGENCY
    )
    html = views.render("workforce_owner", role="owner")
    assert "IDR " in html, "rupiah harus tetap tampil"
    assert "≈ USD" not in html, "tidak boleh ada USD tanpa kurs terbitan"
    assert MD.NO_RATE_NOTE in html, "halaman wajib menjelaskan kenapa USD tidak ada"


def test_a_stale_rate_drops_the_dollar_figure_and_says_so(world):
    """Kurs yang kedaluwarsa sama tidak boleh dipakai: batas 48 jam diuji, bukan diasumsikan."""
    publish_rate(world.conn, quoted_on="2026-11-01", max_age_hours=48)  # 29 hari
    views = V.WorkforceViews(world.conn, clock=FrozenClock.at(G2.CLOCK_NOW), agency_id=AGENCY)
    html = views.render("workforce_finance", role="finance")
    assert "≈ USD" not in html
    assert MD.NO_RATE_NOTE in html


def test_the_owner_total_is_the_sum_of_the_slips_not_a_second_calculation(rich):
    row = rich.conn.execute(
        "SELECT COALESCE(SUM(net_minor), 0) AS net FROM workforce_payslips WHERE agency_id = ?",
        (AGENCY,),
    ).fetchone()
    context = rich.views.context("workforce_owner")
    assert context["paid_net_minor"] == int(row["net"])
    assert context["paid_net"] == MD.line(
        int(row["net"]),
        "IDR",
        source=rich.views.money_reference_source(),
        today=rich.screen_clock.now(),
    )


# --------------------------------------------------------------------------- #
# 2. Kata-kata: tidak ada istilah mesin di layar supervisor
# --------------------------------------------------------------------------- #
def test_the_supervisor_screen_shows_no_machine_words(rich):
    html = rich.views.render("workforce_supervisor", role="operations")
    text = visible(html)
    tokens = MACHINE_TOKENS.findall(text)
    assert tokens == [], f"istilah mesin muncul di layar supervisor: {sorted(set(tokens))}"
    for identifier in ("stf_", "wfr_", "workforce_staff", "workforce_"):
        assert identifier not in text, f"id {identifier!r} terbaca di layar supervisor"


def test_the_supervisor_screen_still_says_something_useful(rich):
    """Kontrol positif: gerbang 'tanpa istilah mesin' tidak boleh lulus karena kosong."""
    html = rich.views.render("workforce_supervisor", role="operations")
    text = visible(html)
    assert "Kehadiran" in text and "Tanda tangan".lower() in text.lower()
    words = {word for group in V.screen_labels().values() for word in group.values()}
    assert words & set(text.split()), "kata katalog tidak muncul sama sekali di layar"


def test_the_catalogue_covers_every_state_the_database_allows(world):
    """Kode diambil dari CHECK constraint DDL — bukan dari daftar yang saya ketik sendiri."""
    ddl = (REPO_ROOT / "src" / "schema" / "001_initial.sql").read_text(encoding="utf-8")
    groups = {
        "attendance_state": ("workforce_attendance_days", "status", "workforce"),
        "timesheet_status": ("workforce_timesheets", "status", "workforce"),
        "run_status": ("workforce_payroll_runs", "status", "workforce_payslips"),
    }
    labels = V.screen_labels()
    for group, (table, column, key) in groups.items():
        pattern = re.compile(
            rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)\s*\).*?{key}", re.DOTALL
        )
        statements = re.findall(r"CREATE TABLE \w+[^;]*;", ddl, re.DOTALL)
        target = next((s for s in statements if table in s), None)
        assert target, f"DDL {table} tidak ditemukan"
        found = re.search(rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)", target)
        assert found, f"tidak ada CHECK {table}.{column}"
        allowed = [item.strip().strip("'") for item in found.group(1).split(",")]
        missing = [code for code in allowed if code not in labels[group]]
        assert missing == [], f"{group}: kode tanpa kata di katalog: {missing}"
        del pattern
    assert set(labels["run_shape"]) == {"MONTHLY", "WEEKLY", "UNKNOWN"}


def test_an_unknown_code_leaves_the_cell_empty_rather_than_inventing_a_word(rich):
    assert rich.views.word("attendance_state", "WHO_KNOWS") == ""
    assert rich.views.word("no_such_group", "PRESENT") == ""


def test_a_staff_name_cannot_inject_markup_into_the_screen(rich):
    rich.conn.execute(
        "UPDATE workforce_staff SET display_name = ? WHERE staff_id = ("
        " SELECT staff_id FROM workforce_staff LIMIT 1)",
        ("<script>alert(1)</script>",),
    )
    html = rich.views.render("workforce_supervisor", role="operations")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --------------------------------------------------------------------------- #
# 3. Bentuk halaman: tabel kosong sejajar tajuknya, layar read-only
# --------------------------------------------------------------------------- #
def test_an_empty_table_spans_exactly_its_header_row(rich):
    """Kolom tabel kosong dihitung dari lebarnya sendiri — kalau tidak, ia dianggap hiasan."""
    empty = V.WorkforceViews(
        rich.conn, clock=FrozenClock.at("2027-01-04T02:00:00Z"), agency_id=AGENCY
    )
    for screen in V.SCREENS:
        html = empty.render(screen)
        for table in re.findall(r"<table>.*?</table>", html, re.DOTALL):
            headers = len(re.findall(r"<th[ >]", table))
            bodies = re.findall(r"<tbody>(.*?)</tbody>", table, re.DOTALL)
            body_rows = [
                row for body in bodies for row in re.findall(r"<tr>.*?</tr>", body, re.DOTALL)
            ]
            if "colspan" in table:
                span = int(re.search(r'colspan="(\d+)"', table).group(1))
                assert span == headers, f"{screen}: colspan {span} vs {headers} tajuk"
                assert len(body_rows) == 1, f"{screen}: tabel kosong punya baris lain"
    supervisor = empty.render("workforce_supervisor")
    assert 'data-date="2027-01-04"' in supervisor
    assert "Dijadwalkan" in visible(supervisor)


def test_rendering_a_screen_writes_nothing(rich):
    tables = ("workforce_staff", "workforce_roster_entries", "workforce_attendance_days",
              "workforce_timesheets", "workforce_payslips", "workforce_payroll_runs")
    before = {
        name: rich.conn.execute(f"SELECT COUNT(*) AS c FROM {name}").fetchone()["c"]
        for name in tables
    }
    for _ in range(2):
        for screen in V.SCREENS:
            rich.views.render(screen)
    after = {
        name: rich.conn.execute(f"SELECT COUNT(*) AS c FROM {name}").fetchone()["c"]
        for name in tables
    }
    assert before == after


def test_no_escaped_markup_reaches_a_reader(rich) -> None:
    """Markup yang seharusnya jadi elemen tidak boleh muncul sebagai teks.

    Dunia uji ini tidak punya nama yang mengandung ``<``, jadi setiap ``&lt;`` di halaman
    berarti ada potongan HTML yang ter-escape dua kali dan pembaca melihat tag-nya. Kontrol
    negatifnya adalah uji berikutnya di berkas ini: nama bersisi ``<script>`` memang HARUS
    tampil ter-escape — jadi aturan ini hanya berlaku untuk data bersih.
    """
    for screen, role in (("workforce_supervisor", "operations"),
                         ("workforce_finance", "finance"),
                         ("workforce_owner", "owner")):
        html = str(rich.views.render(screen, role=role, actor="anas", title=screen))
        assert "&lt;" not in html, f"{screen}: markup ter-escape tampil sebagai teks"
        assert "<time datetime=" in html, f"{screen}: slot waktu bukan elemen"


def test_the_role_title_comes_from_the_blueprint_map(rich) -> None:
    """Judul peran di kepala halaman = nama peran dari blueprint, bukan nama orang.

    Modul ini tidak boleh punya daftar nama peran sendiri: dua sumber nama peran akan
    bercabang dari bagan organisasi (ADR-0035). Nama peran yang memuat ``&`` diuji apa
    adanya supaya ekspektasinya ikut ter-escape seperti yang dibaca di halaman.
    """
    from dashboard import ROLE_DISPLAY_LABEL, escape_value

    for screen, role in (("workforce_supervisor", "operations"),
                         ("workforce_finance", "finance"),
                         ("workforce_owner", "owner")):
        html = str(rich.views.render(screen, role=role, actor="anas", title="Tenaga kerja"))
        assert escape_value(ROLE_DISPLAY_LABEL[role]) in html, screen
        assert "&amp;" in html if "&" in ROLE_DISPLAY_LABEL[role] else True
    # Peran di luar bagan jatuh ke kodenya sendiri — bukan kata karangan, bukan nama orang.
    shell = rich.views.shell_context(role="roving_manager", actor="anas", title="x")
    assert shell["actor_role_label"] == "roving_manager"
    assert shell["actor_role"] == "roving_manager"


def test_a_role_cannot_open_a_screen_it_does_not_own(rich):
    assert V.screens_for_role("operations") == ("workforce_supervisor",)
    assert V.screens_for_role("finance") == ("workforce_finance",)
    assert V.screens_for_role("owner") == ("workforce_owner",)
    assert V.screens_for_role("guest") == ()
    assert V.screens_for_role(None) == ()
    with pytest.raises(V.UnknownScreen):
        rich.views.render("workforce_finance", role="operations")
    with pytest.raises(V.UnknownScreen):
        rich.views.context("workforce_ceo")
