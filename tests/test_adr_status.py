"""Every ADR must declare a status the house vocabulary can read (§10 quality gates, ADR-0010).

An ADR with no readable status line cannot be audited: a reader — or a script — cannot tell a
proposal from a decision. The repository has recorded statuses in three spellings
(``- Status: **ACCEPTED**``, ``- **Status:** ACCEPTED``, ``- **Status**: Accepted``); the test
tolerates all three and refuses anything else, so a new ADR that forgets its status fails here
instead of being discovered by hand months later.
"""

from __future__ import annotations

import pathlib
import re

ADR_DIR = pathlib.Path(__file__).resolve().parent.parent / "docs" / "adr"

#: Tolerates every spelling the repository has used for the word and its decoration.
STATUS_RE = re.compile(r"^-\s*\*{0,2}Status:?\*{0,2}:?\s*(.+)$", re.M)

#: The vocabulary a status must come from. `PROPOSED` is legitimate — a new ADR starts there.
KNOWN_STATUSES = ("PROPOSED", "ACCEPTED", "SUPERSEDED", "REJECTED", "WITHDRAWN")


def _declared_status(path: pathlib.Path) -> str | None:
    match = STATUS_RE.search(path.read_text(encoding="utf-8"))
    if match is None:
        return None
    return match.group(1).strip().lstrip("*").strip()


def test_the_three_recorded_spellings_all_parse():
    """The three spellings the repository has used must keep parsing, or the guard lies."""
    for line in (
        "- Status: **ACCEPTED** (owner decision, 2026-09-28)",
        "- **Status:** ACCEPTED (recorded with WAVE 4-A, 2026-09-26)",
        "- **Status**: Accepted",
    ):
        match = STATUS_RE.search(line)
        assert match is not None, line
        assert match.group(1).strip().lstrip("*").strip().upper().startswith("ACCEPTED"), line


def test_every_adr_declares_a_status_from_the_known_vocabulary():
    files = sorted(ADR_DIR.glob("*.md"))
    assert files, f"no ADRs found under {ADR_DIR} — the directory moved"

    problems: list[str] = []
    for path in files:
        status = _declared_status(path)
        if status is None:
            problems.append(f"{path.name}: no readable status line")
            continue
        if not status.upper().startswith(KNOWN_STATUSES):
            problems.append(f"{path.name}: unknown status {status!r}")

    assert not problems, "ADR status problems:\n" + "\n".join(problems)


def test_the_guard_would_notice_a_missing_status(tmp_path):
    """A negative control: the check must fail on a file that declares nothing."""
    stray = tmp_path / "9999-forgot-its-status.md"
    stray.write_text("# ADR-9999 — a decision nobody can audit\n\n## Decision\n\nDo the thing.\n")

    assert _declared_status(stray) is None


#: A claim about how many ADRs exist, e.g. "61 ADR". The lookahead keeps references to a
#: single record out of it: "§56 ADR-0023" is a pointer, not a count.
COUNT_CLAIM_RE = re.compile(r"\b(\d{1,3})\s+ADR(?!-)")

#: Living documents that state the size of the set. Dated artefacts (`*2026-10-06*`) are
#: deliberately absent: they record what was true on a day, and editing them to say 61 would
#: falsify an audit rather than refresh it. Only the documents that must be true *now* are held.
COUNT_CLAIM_DOCS = ("docs/SALES_PACKAGE.md", "docs/STATUS_AND_GAP_MAP.md")


def test_living_documents_state_the_real_number_of_adrs():
    """A stale count is a claim nobody can trust — the sales package said 59 for a day.

    The number lives in prose, so no test noticed as ADRs were added. This is the ratchet:
    adding an ADR without refreshing the living documents fails here.
    """
    real = len(list(ADR_DIR.glob("*.md")))
    root = ADR_DIR.parent.parent

    problems: list[str] = []
    for rel in COUNT_CLAIM_DOCS:
        path = root / rel
        assert path.exists(), f"{rel} moved — update COUNT_CLAIM_DOCS"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in COUNT_CLAIM_RE.finditer(line):
                claimed = int(match.group(1))
                if claimed != real:
                    problems.append(f"{rel}:{lineno}: claims {claimed} ADR, {ADR_DIR.name}/ holds {real}")

    assert not problems, "stale ADR counts:\n" + "\n".join(problems)


def test_the_count_claim_regex_reads_counts_and_ignores_references():
    """Negative controls for the ratchet: it must catch a count and leave a pointer alone."""
    counted = COUNT_CLAIM_RE.search("+ 61 ADR |")
    assert counted is not None
    assert counted.group(1) == "61"

    assert COUNT_CLAIM_RE.search("59 ADR") is not None

    assert COUNT_CLAIM_RE.search("celah §56 ADR-0023/0024") is None
    assert COUNT_CLAIM_RE.search("ADR-0042/0043/0044") is None
