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
