"""Tests for scripts/phishing_drill_score.py — the drill scorer must fail closed."""

from __future__ import annotations

import csv
import importlib.util
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "phishing_drill_score.py"
HEADER = [
    "recipient_id",
    "template",
    "delivered",
    "clicked",
    "submitted_credentials",
    "reported",
]


def _load_module():
    spec = importlib.util.spec_from_file_location("phishing_drill_score", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve `cls.__module__` through sys.modules; a bare spec has no entry.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(tmp_path, rows):
    path = tmp_path / "drill.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return str(path)


def _row(recipient, template, delivered=1, clicked=0, submitted=0, reported=0):
    return [recipient, template, delivered, clicked, submitted, reported]


def test_clean_drill_meets_the_bar(tmp_path):
    module = _load_module()
    rows = [
        _row("s1", "booking_palsu", reported=1),
        _row("s2", "booking_palsu", reported=1),
        _row("s3", "booking_palsu", reported=1),
        _row("s4", "invoice_vendor", reported=1),
        _row("s5", "invoice_vendor", reported=1),
        _row("s6", "invoice_vendor", reported=1),
    ]
    summary = module.load(_write(tmp_path, rows))
    code, problems = module.verdict(summary)
    assert summary.delivered == 6
    assert summary.clicked == 0
    assert code == 0, problems
    assert problems == []


def test_click_ceiling_is_enforced(tmp_path):
    module = _load_module()
    rows = [_row(f"s{i}", "booking_palsu", reported=1) for i in range(6)]
    rows[0] = _row("s0", "booking_palsu", clicked=1, reported=1)
    summary = module.load(_write(tmp_path, rows))
    assert round(summary.by_template["booking_palsu"].rate(1), 1) == 16.7
    code, problems = module.verdict(summary)
    assert code == 2
    assert any("klik" in problem for problem in problems)
    assert not any("kredensial" in problem for problem in problems)


def test_any_submitted_credential_fails_the_run(tmp_path):
    module = _load_module()
    rows = [_row(f"s{i}", "booking_palsu", reported=1) for i in range(6)]
    rows[2] = _row("s2", "booking_palsu", clicked=1, submitted=1, reported=1)
    summary = module.load(_write(tmp_path, rows))
    code, problems = module.verdict(summary)
    assert code == 2
    assert any("kredensial" in problem for problem in problems)


def test_small_cohort_is_insufficient_not_pass(tmp_path):
    module = _load_module()
    rows = [_row("s1", "booking_palsu", reported=1), _row("s2", "booking_palsu", reported=1)]
    summary = module.load(_write(tmp_path, rows))
    code, problems = module.verdict(summary)
    assert code == 3
    assert "tidak cukup" in problems[0]


def test_duplicate_recipient_is_rejected(tmp_path):
    module = _load_module()
    rows = [_row("s1", "booking_palsu"), _row("s1", "invoice_vendor")]
    with pytest.raises(ValueError, match="duplikat"):
        module.load(_write(tmp_path, rows))


def test_non_binary_flag_is_rejected(tmp_path):
    module = _load_module()
    rows = [["s1", "booking_palsu", "ya", 0, 0, 0]]
    with pytest.raises(ValueError, match="harus 0 atau 1"):
        module.load(_write(tmp_path, rows))


def test_submitted_without_click_is_rejected(tmp_path):
    module = _load_module()
    rows = [_row("s1", "booking_palsu", clicked=0, submitted=1)]
    with pytest.raises(ValueError, match="tanpa clicked"):
        module.load(_write(tmp_path, rows))
