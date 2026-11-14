"""Timestamp dashboard ditampilkan ganda: mesin + manusia, dan itu diuji.

Kenapa test ini ada
-------------------
Basis data menyimpan instant sebagai ISO-8601 UTC (``2026-11-07T00:00:00+00:00``).
Angka itu benar tapi tidak terbaca: pemilik villa harus menghitung sendiri selisih
tujuh jam ke WIB. Mengganti isinya dengan teks cantik akan menghilangkan presisi,
jadi dipilih jalan tengah yang bisa diuji:

    <time datetime="2026-11-07T00:00:00+00:00">07 Nov 2026, 07:00 WIB</time>

Atribut ``datetime`` mempertahankan nilai mesin (test lama yang mencari string ISO
tetap lolos, copy-paste tetap akurat), isi elemen adalah yang dibaca mata.

Batas yang dijaga test ini
--------------------------
* Hanya instant ISO **lengkap** yang diubah. Tanggal telanjang (``2026-11-10``),
  nomor invoice (``INV-202609-0001``), dan teks bebas tidak disentuh — kalau
  fungsi ini salah menebak, ia bisa merusak data yang bukan tanggal.
* Instant tanpa zona (``2026-11-07T00:00:00``) **tidak** diubah: tanpa zona tidak
  ada arti tunggal, dan menebak UTC adalah kebohongan kecil yang mahal.
* String yang bukan pola persis tidak pernah ditulis ulang sebagian — suffix
  berbahaya tetap jatuh ke jalur escape biasa.
* Tanpa basis data zona waktu, hasilnya tetap +07:00 (bukan crash, bukan UTC).
"""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timedelta, timezone

import pytest

import dashboard
import time_display as td


# --------------------------------------------------------------------------- #
# Pengenalan pola
# --------------------------------------------------------------------------- #
def test_full_instant_with_offset_is_recognised() -> None:
    moment = td.parse("2026-11-07T00:00:00+00:00")
    assert moment is not None
    assert moment == datetime(2026, 11, 7, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-14T09:30:00Z",
        "2026-11-14T09:30:00.123456+00:00",
        "2026-11-14 09:30:00+00:00",
        "2026-11-14T16:30+07:00",
    ],
)
def test_iso_shapes_the_database_writes_are_all_accepted(text: str) -> None:
    assert td.parse(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-10",  # tanggal jatuh tempo, bukan instant
        "2026-11",
        "INV-202609-0001",
        "14 November 2026",
        "",
        "2026-11-07T00:00:00",  # tanpa zona: ambigu, sengaja tidak disentuh
        "2026-13-40T00:00:00+00:00",  # bentuk benar, tanggal mustahil
    ],
)
def test_non_instants_are_left_alone(text: str) -> None:
    assert td.parse(text) is None
    assert td.humanize(text) is None


def test_hostile_suffix_is_never_partially_rewritten() -> None:
    """Satu sel hanya boleh diubah kalau SELURUH isinya instant.

    Kalau pencocokan dibuat longgar (tanpa ``$``), penyerang bisa menyelipkan
    markup setelah timestamp dan berharap ia ikut lolos sebagai HTML.
    """
    hostile = "2026-11-07T00:00:00+00:00<script>alert(1)</script>"
    assert td.humanize(hostile) is None


# --------------------------------------------------------------------------- #
# Konversi ke WIB
# --------------------------------------------------------------------------- #
def test_utc_midnight_reads_as_seven_in_the_morning() -> None:
    html = td.humanize("2026-11-07T00:00:00+00:00")
    assert html is not None
    assert "07 Nov 2026, 07:00 WIB" in html


def test_rollover_crosses_into_the_next_day_and_year() -> None:
    html = td.humanize("2026-12-31T20:00:00Z")
    assert html is not None
    assert "01 Jan 2027, 03:00 WIB" in html


def test_an_instant_already_in_wib_is_not_shifted_twice() -> None:
    html = td.humanize("2026-11-14T17:45:00+07:00")
    assert html is not None
    assert "14 Nov 2026, 17:45 WIB" in html


def test_machine_value_survives_in_the_datetime_attribute() -> None:
    raw = "2026-11-14T09:30:00+00:00"
    html = td.humanize(raw)
    assert html is not None
    assert f'datetime="{raw}"' in html
    assert f'title="{raw}"' in html


def test_month_names_are_indonesian_not_english() -> None:
    assert td.label(datetime(2026, 5, 1, tzinfo=timezone.utc)).endswith("Mei 2026, 07:00 WIB")
    assert td.label(datetime(2026, 8, 1, tzinfo=timezone.utc)).startswith("01 Agu")
    assert td.label(datetime(2026, 10, 1, tzinfo=timezone.utc)).startswith("01 Okt")


def test_missing_timezone_database_falls_back_to_fixed_offset(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise zoneinfo.ZoneInfoNotFoundError("no tzdata on this host")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", unavailable)
    html = td.humanize("2026-11-07T00:00:00+00:00")
    assert html is not None, "fallback harus tetap menghasilkan tampilan, bukan menyerah"
    assert "07 Nov 2026, 07:00 WIB" in html


# --------------------------------------------------------------------------- #
# Sambungan ke dashboard
# --------------------------------------------------------------------------- #
def test_dashboard_cell_renders_the_time_element() -> None:
    cell = dashboard._cell("2026-11-01T00:00:00+00:00")
    assert cell.startswith('<time datetime="2026-11-01T00:00:00+00:00"')
    assert "01 Nov 2026, 07:00 WIB" in cell


def test_dashboard_cell_still_escapes_ordinary_text() -> None:
    assert dashboard._cell("<b>Villa Ubud</b>") == "&lt;b&gt;Villa Ubud&lt;/b&gt;"
    assert dashboard._cell("2026-11-10") == "2026-11-10"
    assert dashboard._cell(5500000) == "5500000"


def test_dashboard_money_cell_is_untouched_by_time_formatting() -> None:
    """Uang dan waktu sama-sama 'teks yang diformat' — jangan sampai tertukar."""
    assert dashboard._cell("IDR 5.500.000") == "IDR 5.500.000"


def test_fallback_offset_matches_jakarta() -> None:
    assert td.FALLBACK.utcoffset(None) == timedelta(hours=7)
