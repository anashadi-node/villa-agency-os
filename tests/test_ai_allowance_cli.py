"""End-to-end tests for the allowance CLI: the command that opens the machine path.

``test_wave4b3_ai_allowance_decision.py`` proves the *semantics* of the published value.
These prove that the documented command, run by a real operator against a real database
file, produces the documented effect and the documented exit code — including refusing to
write a permission that names no approver, and closing again in one revision when the
owner withdraws it.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest

from schema_loader import connect_sqlite

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "publish_ai_allowance.py"
ALLOWANCE = "security.ai.medium_tool_allowance"

#: A window that is open *now*, so the assertions do not age.
TODAY = datetime.date.today()
OPEN_FROM = TODAY.isoformat()
OPEN_TO = (TODAY + datetime.timedelta(days=30)).isoformat()


def _run(db: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI. ``db=None`` means *leave VILLA_DB unset* — the case that matters for the
    granting act, which must refuse to guess its target rather than inherit one."""
    env = dict(os.environ)
    env.pop("VILLA_DB", None)
    if db is not None:
        env["VILLA_DB"] = str(db)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120,
    )


def _revisions(db: Path) -> int:
    conn = connect_sqlite(str(db))
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM policy_revisions WHERE policy_key = ?",
            (ALLOWANCE,),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "villa.db"
    connect_sqlite(str(path)).close()
    return path


def test_status_on_a_fresh_database_reports_closed(db: Path):
    result = _run(db, "status")
    assert result.returncode == 0, result.stderr
    assert "CLOSED" in result.stdout
    assert "never published" in result.stdout


def test_publish_without_an_approver_is_refused_and_writes_nothing(db: Path):
    result = _run(db, "publish", "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO)
    assert result.returncode == 2
    assert "--approved-by" in result.stderr
    assert _revisions(db) == 0


def test_publish_then_status_shows_the_effective_value(db: Path):
    published = _run(
        db, "publish", "--approved-by", "owner_anas",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
    )
    assert published.returncode == 0, published.stderr
    assert 'version 1 = ["availability.request"]' in published.stdout

    status = _run(db, "status")
    assert status.returncode == 0, status.stderr
    assert 'effective now : ["availability.request"]' in status.stdout
    assert f"{OPEN_FROM} → {OPEN_TO}" in status.stdout


def test_a_different_tool_list_needs_no_code_change(db: Path):
    result = _run(
        db, "publish", "--approved-by", "owner_anas",
        "--tools", "availability.request,quotation.send",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
    )
    assert result.returncode == 0, result.stderr
    assert '["availability.request","quotation.send"]' in result.stdout


def test_withdraw_closes_it_again_in_one_revision(db: Path):
    assert _run(
        db, "publish", "--approved-by", "owner_anas",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
    ).returncode == 0
    assert _run(db, "withdraw", "--approved-by", "owner_anas").returncode == 0

    status = _run(db, "status")
    assert status.returncode == 0, status.stderr
    assert "published and empty: a machine actor is refused" in status.stdout
    assert "(version 2)" in status.stdout
    assert "withdrawn by the owner" in status.stdout
    assert _revisions(db) == 2


def test_a_missing_database_is_a_configuration_error(tmp_path: Path):
    result = _run(tmp_path / "nope.db", "status")
    assert result.returncode == 2
    assert "database not found" in result.stderr


def test_publish_without_an_explicit_target_is_refused_and_writes_nothing(tmp_path: Path):
    """The granting act names its database.

    Every other CLI in this repository falls back to ``data/villa.db``, which is the right
    default for a read or a seed — but a permission that lands in whatever ``data/villa.db``
    happens to sit in the working directory is a permission nobody decided to give. Found by
    running the documented command without ``VILLA_DB``: it published a revision into this
    repository's demo database.
    """
    repo_db = REPO / "data" / "villa.db"
    before = _revisions(repo_db) if repo_db.exists() else None

    result = _run(
        None, "publish", "--approved-by", "owner_anas",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
    )

    assert result.returncode == 2
    assert "VILLA_DB" in result.stderr
    after = _revisions(repo_db) if repo_db.exists() else None
    assert after == before, "a refused publish must not write a revision anywhere"


def test_publish_says_which_database_it_wrote_to(db: Path):
    result = _run(
        db, "publish", "--approved-by", "owner_anas",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
    )
    assert result.returncode == 0, result.stderr
    assert f"target database: {db}" in result.stdout


def test_withdraw_can_name_a_date_for_a_revision_that_starts_in_the_future(db: Path):
    """The documented withdrawal names its date.

    A withdrawal defaults to *today*, and the policy engine refuses a revision that starts
    before the newest one (``POLICY_BACKDATED_REVISION``). So for a revision that begins
    tomorrow, the plain ``withdraw --approved-by …`` form fails and the operator has to say
    when — which is the form the README shows.
    """
    future = (TODAY + datetime.timedelta(days=1)).isoformat()
    assert _run(
        db, "publish", "--approved-by", "owner_anas",
        "--effective-from", future, "--effective-to", OPEN_TO,
    ).returncode == 0

    too_early = _run(db, "withdraw", "--approved-by", "owner_anas")
    assert too_early.returncode == 1
    assert "POLICY_BACKDATED_REVISION" in too_early.stderr

    dated = _run(db, "withdraw", "--approved-by", "owner_anas", "--effective-from", future)
    assert dated.returncode == 0, dated.stderr
