"""Tests for the ``num`` column contract of ``dashboard.table()``.

Money and counters read wrong when they are left-aligned as ordinary text: the
digits never line up and an amount cannot be compared at a glance. ``table()``
therefore takes an explicit list of numeric column indices and marks exactly
those cells (and their headers) with ``class="num"``, which the stylesheet
renders right-aligned with tabular figures.

Pinned here: nothing is guessed from the cell value, unlisted columns stay
untouched, the class does not bypass escaping, and the finance page really does
opt its amount columns in.
"""

from __future__ import annotations

import pytest

import dashboard as D

from test_dashboard import make_app, render, seed


@pytest.fixture()
def seeded_app(conn, clock):
    seed(conn, clock)
    return make_app(conn, clock)


def test_table_without_num_columns_is_unchanged():
    rendered = D.table(["A", "B"], [("x", "1")], empty="baris")
    assert rendered == (
        "<table ><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>x</td><td>1</td></tr></tbody></table>"
    )


def test_table_marks_only_the_declared_columns():
    rendered = str(D.table(["Nama", "Total"], [("Villa A", "IDR 1.000")], empty="x", num=(1,)))
    assert '<th class="num">Total</th>' in rendered
    assert "<th>Nama</th>" in rendered
    assert '<td class="num">IDR 1.000</td>' in rendered
    assert "<td>Villa A</td>" in rendered


def test_table_marks_several_columns_and_leaves_the_rest_plain():
    rendered = str(
        D.table(
            ["A", "B", "C", "D"],
            [("a", "1", "2", "d")],
            empty="x",
            num=(1, 2),
        )
    )
    assert '<td>a</td><td class="num">1</td><td class="num">2</td><td>d</td>' in rendered


def test_numeric_class_does_not_bypass_escaping():
    rendered = str(D.table(["Total"], [("<script>alert(1)</script>",)], empty="x", num=(0,)))
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert '<td class="num">&lt;script&gt;' in rendered


def test_empty_rows_render_the_empty_note_and_no_table():
    rendered = str(D.table(["Total"], [], empty="invoice", num=(0,)))
    assert "<table" not in rendered
    assert "invoice" in rendered


def test_finance_invoice_amounts_are_right_aligned(seeded_app):
    page = render(seeded_app, "finance", "finance")
    assert '<th class="num">Total</th>' in page
    assert '<th class="num">Terbayar</th>' in page
    assert '<th class="num">Jumlah</th>' in page
    # The seeded IDR invoices carry their amount in a num cell, not a plain one.
    assert '<td class="num">IDR ' in page
    # Status columns must stay ordinary text even though they sit beside amounts.
    assert '<td class="num">ISSUED</td>' not in page
    assert '<td class="num">PAID</td>' not in page
