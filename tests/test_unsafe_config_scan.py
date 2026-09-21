"""The unsafe-config gate is part of the product's safety story, so it is tested.

A gate that fails on lookalikes trains people to ignore it; a gate that misses a
real violation is worse than none. Both directions are asserted here against
temporary trees, with no dependency on the repository's own contents.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "unsafe_config_scan.sh"


def _scan(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(root)],
        capture_output=True, text=True, timeout=120,
    )


def test_flags_a_modelled_cardholder_field(tmp_path):
    (tmp_path / "payments_bad.py").write_text(
        "class Card:\n    card_number: str\n    cvv: str\n", encoding="utf-8",
    )
    result = _scan(tmp_path)
    assert result.returncode != 0
    assert "cardholder data field defined" in result.stdout


def test_ignores_span_lookalikes(tmp_path):
    """'source_span:' and 'span = ...' must not be read as the token 'pan'."""

    (tmp_path / "intake_ok.py").write_text(
        "from typing import Optional\n"
        "check_in_span: Optional[tuple[int, int]] = None\n"
        "def diff(a, b):\n"
        "    span = b - a\n"
        "    return span\n",
        encoding="utf-8",
    )
    result = _scan(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "PASS" in result.stdout


def test_flags_a_live_money_switch(tmp_path):
    (tmp_path / "config.py").write_text("dry_run = True\n", encoding="utf-8")
    result = _scan(tmp_path)
    assert result.returncode != 0
    assert "live-money switch enabled" in result.stdout


def test_flags_shuffled_ordered_data(tmp_path):
    (tmp_path / "split.py").write_text("options = {'shuffle': True}\n", encoding="utf-8")
    result = _scan(tmp_path)
    assert result.returncode != 0
    assert "shuffle" in result.stdout


def test_flags_a_quoted_shuffle_switch(tmp_path):
    """The dict/JSON spelling is the one people actually write."""

    (tmp_path / "split.py").write_text(
        'options = {"shuffle": True}\n', encoding="utf-8",
    )
    result = _scan(tmp_path)
    assert result.returncode != 0
    assert "shuffle" in result.stdout


def test_flags_a_quoted_live_money_switch(tmp_path):
    (tmp_path / "config.json").write_text('{"dry_run": true}\n', encoding="utf-8")
    result = _scan(tmp_path)
    assert result.returncode != 0
    assert "live-money switch enabled" in result.stdout


def test_flags_a_committed_env_file(tmp_path):
    (tmp_path / ".env").write_text("VILLA_DB=data/villa.db\n", encoding="utf-8")
    result = _scan(tmp_path)
    assert result.returncode != 0
    assert "committed .env file" in result.stdout


def test_passes_on_a_quiet_tree(tmp_path):
    (tmp_path / "harmless.py").write_text("VALUE = 1\n", encoding="utf-8")
    result = _scan(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "PASS" in result.stdout


def test_the_repository_itself_passes():
    """The scan the deploy gate runs must be green on the real tree."""

    result = _scan(Path(__file__).resolve().parent.parent)
    assert result.returncode == 0, result.stdout
    assert "0 findings" in result.stdout
