"""ADR-0011 ratchet: production text is locked to U.S. English (en-US).

Master Spec v1.1 §55 locks the language of everything a user, client or integration
can see. This repository does not meet that yet, so the ADR chose a **ratchet**
instead of a big-bang translation: the files that still carry legacy Indonesian are
pinned with their measured counts, and

* a file may **shrink** (translation progress is free),
* a file may **not grow** (no new Indonesian sneaks in),
* a file that has no entry must stay at **zero**.

Method (and why it is not a regex sweep): string literals are collected with
``ast``, and **docstrings are excluded** — a developer-facing docstring is a lower
tier than production copy, and pretending otherwise would force a translation of
internal notes while the client-facing strings stayed Indonesian. Only literals a
program can actually emit to a user or a peer are counted.

One deliberate nuance: ``intake.py``'s count includes the words the *inbound parser*
recognises ("sampai", "hingga"). Those are data — guests write Indonesian, and
recognising a guest's language is not producing it. They are pinned so they cannot
grow, and the module's own refusal messages still have to be translated.

Measured 2026-09-26: 484 literal hits in 10 modules + 15 hits in 8 templates.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TEMPLATES = REPO_ROOT / "templates"

#: Indonesian function words. Chosen to be words that do not occur in English prose,
#: so a hit is (almost certainly) real Indonesian rather than a coincidence.
MARKERS = re.compile(
    r"\b(wajib|tidak|belum|peran|halaman|kasus|keuangan|properti|tamu|dengan|untuk|dari|"
    r"pada|adalah|sudah|harus|boleh|sedang|juga|atau|dan|bila|maka|yang|ini|itu|tersebut|"
    r"milik|ditolak|diizinkan|gagal|berhasil|diminta|dibatalkan)\b",
    re.IGNORECASE,
)

#: Module -> measured Indonesian literal hits. **Only ever lowered.**
RECORDED_SRC = {
    "cases.py": 42,
    "connectors.py": 90,
    "dashboard.py": 158,
    "documents.py": 7,
    "identity.py": 26,
    "incidents.py": 74,
    "intake.py": 40,
    "matching.py": 23,
    "messages.py": 19,
    "scheduler.py": 2,
}

#: Template -> measured Indonesian hits. **Only ever lowered.**
RECORDED_TEMPLATES = {
    "base.html": 5,
    "case_detail.html": 1,
    "cases.html": 4,
    "command_center.html": 1,
    "error.html": 1,
    "finance.html": 1,
    "inventory.html": 1,
    "properties.html": 1,
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ``Constant`` nodes that are docstrings, not emittable strings."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def indonesian_literals(source: str) -> int:
    """How many Indonesian markers a module emits through its string literals."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            total += len(MARKERS.findall(node.value))
    return total


def indonesian_in_template(markup: str) -> int:
    """Template text with tags and placeholders removed — what a reader sees."""
    text = re.sub(r"<[^>]+>", " ", markup)
    text = re.sub(r"\{\{[^}]*\}\}", " ", text)
    return len(MARKERS.findall(text))


# --------------------------------------------------------------------------- #
# The ratchet
# --------------------------------------------------------------------------- #
def test_no_module_gains_new_indonesian_text():
    grew = []
    for path in sorted(SRC.rglob("*.py")):
        measured = indonesian_literals(path.read_text(encoding="utf-8"))
        allowed = RECORDED_SRC.get(path.name, 0)
        if measured > allowed:
            grew.append(f"{path.name}: {measured} (allowed {allowed})")
    assert grew == [], (
        "ADR-0011 §55 locks production language to en-US; these modules gained "
        "Indonesian text that must be written in English instead: " + ", ".join(grew)
    )


def test_no_template_gains_new_indonesian_text():
    grew = []
    for path in sorted(TEMPLATES.glob("*.html")):
        measured = indonesian_in_template(path.read_text(encoding="utf-8"))
        allowed = RECORDED_TEMPLATES.get(path.name, 0)
        if measured > allowed:
            grew.append(f"{path.name}: {measured} (allowed {allowed})")
    assert grew == [], "templates gained Indonesian copy: " + ", ".join(grew)


def test_the_recorded_counts_are_still_honest():
    """A ratchet that over-states its numbers would hide real progress — or hide regression.

    The recorded value must be an **upper bound that is still tight**: if translation
    removes text, this fails so the pin gets lowered and the progress is visible.
    """
    stale = []
    for path in sorted(SRC.rglob("*.py")):
        measured = indonesian_literals(path.read_text(encoding="utf-8"))
        allowed = RECORDED_SRC.get(path.name, 0)
        if measured < allowed:
            stale.append(f"{path.name}: {measured} < {allowed}")
    for path in sorted(TEMPLATES.glob("*.html")):
        measured = indonesian_in_template(path.read_text(encoding="utf-8"))
        allowed = RECORDED_TEMPLATES.get(path.name, 0)
        if measured < allowed:
            stale.append(f"{path.name}: {measured} < {allowed}")
    assert stale == [], (
        "translation progress detected — lower these pins so the ratchet stays honest: "
        + ", ".join(stale)
    )


# --------------------------------------------------------------------------- #
# Non-vacuity: a guard that cannot fail is not a guard
# --------------------------------------------------------------------------- #
def test_the_language_guard_detects_new_indonesian_code():
    synthetic = 'def f():\n    raise ValueError("pesan ini tidak boleh dipakai")\n'
    assert indonesian_literals(synthetic) >= 2


def test_the_language_guard_ignores_docstrings_and_english_text():
    synthetic = '"""Dokumen ini tidak dihitung karena bukan keluaran program."""\n\nX = "Add a new villa"\n'
    assert indonesian_literals(synthetic) == 0


def test_the_template_guard_reads_what_a_user_sees_not_the_markup():
    assert indonesian_in_template("<option value='finance'>Keuangan</option>") == 1
    assert indonesian_in_template("<option value='dashboard'>Dashboard</option>") == 0
