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
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from primitives import SystemClock
from schema_loader import connect_sqlite

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "publish_ai_allowance.py"
ALLOWANCE = "security.ai.medium_tool_allowance"

#: A window that is open *now*, so the assertions do not age.
#:
#: Derived from the platform's own clock (``SystemClock``, UTC), never from the wall clock.
#: ``datetime.date.today()`` is **local**: between 00:00 and 07:00 WIB it is already tomorrow in
#: UTC, so the published window began in the future, ``status`` answered CLOSED, and ``withdraw``
#: was refused as ``POLICY_BACKDATED_REVISION`` — the pre-deploy gate was red for seven hours of
#: every day, which is how this was found. The window opens a day early so a run that straddles
#: midnight cannot flip either way.
TODAY = SystemClock().today()
OPEN_FROM = (TODAY - datetime.timedelta(days=1)).isoformat()
OPEN_TO = (TODAY + datetime.timedelta(days=30)).isoformat()


def _run(
    db: Path | None, *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the CLI. ``db=None`` means *leave VILLA_DB unset* — the case that matters for the
    granting act, which must refuse to guess its target rather than inherit one.

    ``cwd`` is the directory the CLI resolves its ``data/villa.db`` default against. Every
    test that exercises that default points it at a synthetic tree, so the repository's own
    store is never the target — the default is only safe to test when it can be redirected.
    """
    env = dict(os.environ)
    env.pop("VILLA_DB", None)
    if db is not None:
        env["VILLA_DB"] = str(db)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=str(cwd or REPO), timeout=120,
    )


def _revisions(db: Path) -> int:
    """Count revisions with a **read-only** connection.

    ``connect_sqlite`` is the wrong tool for this: it issues ``PRAGMA journal_mode=WAL`` and
    ``apply_schema()``, so a call meant to prove "nothing was written" could itself write to
    the file it is inspecting (AUD-03). ``mode=ro`` cannot.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM policy_revisions WHERE policy_key = ?",
            (ALLOWANCE,),
        ).fetchone()
        return int(row[0])
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


def _synthetic_working_directory(tmp_path: Path) -> Path:
    """A tree whose ``data/villa.db`` *is* the CLI's default, and is not this repository's.

    The CLI's default is relative, which is what makes it testable without a live store: the
    subprocess runs here, so ``data/villa.db`` is this file and nothing else.
    """
    work = tmp_path / "work"
    (work / "data").mkdir(parents=True)
    connect_sqlite(str(work / "data" / "villa.db")).close()
    return work


def test_the_default_target_is_the_working_directorys_store(tmp_path: Path):
    """The isolation the next two tests rest on, asserted instead of assumed."""
    work = _synthetic_working_directory(tmp_path)
    default_store = work / "data" / "villa.db"

    assert default_store.exists()
    assert default_store.resolve() != REPO.joinpath("data", "villa.db").resolve()


def test_publish_without_an_explicit_target_is_refused_and_writes_nothing(tmp_path: Path):
    """The granting act names its database.

    Every other CLI in this repository falls back to ``data/villa.db``, which is the right
    default for a read or a seed — but a permission that lands in whatever ``data/villa.db``
    happens to sit in the working directory is a permission nobody decided to give. Found by
    running the documented command without ``VILLA_DB``: it published a revision into this
    repository's demo database.

    The working directory here is a synthetic one, so the assertion "nothing was written"
    is checked against the store the CLI *would* have written to — and the repository's own
    store is never opened, let alone written.
    """
    work = _synthetic_working_directory(tmp_path)
    default_store = work / "data" / "villa.db"
    before = _revisions(default_store)

    result = _run(
        None, "publish", "--approved-by", "owner_anas",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
        cwd=work,
    )

    assert result.returncode == 2
    assert "VILLA_DB" in result.stderr
    assert _revisions(default_store) == before, (
        "a refused publish must not write a revision anywhere"
    )


def test_the_refused_default_is_writable_when_it_is_named_explicitly(tmp_path: Path):
    """Positive control: the store the refusal above declined to write to accepts a write.

    Without this, "nothing was written" would also pass on a target that could never be
    written at all — the assertion would be green and empty.
    """
    work = _synthetic_working_directory(tmp_path)
    default_store = work / "data" / "villa.db"

    named = _run(
        Path("data") / "villa.db", "publish", "--approved-by", "owner_anas",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
        cwd=work,
    )

    assert named.returncode == 0, named.stderr
    assert _revisions(default_store) == 1


def test_this_file_never_names_the_repositorys_own_database():
    """A guard with teeth against the regression this repair is about.

    Two data paths lived here and both opened the real store. The isolation above only
    holds while no test in this file points at ``REPO/data/villa.db``; reading the source
    is enough to catch a reintroduced one before anything opens a file.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    anchor = "REPO"  # composed, so this guard does not match its own literal
    for tail in (' / "data"', " / 'data'"):
        assert (anchor + tail) not in source, (
            f"{anchor + tail!r} is back in {Path(__file__).name}: a test in this file would be "
            "resolving a path under this repository again, and the store it would reach is "
            "the operator's own"
        )


def test_publish_says_which_database_it_wrote_to(db: Path):
    result = _run(
        db, "publish", "--approved-by", "owner_anas",
        "--effective-from", OPEN_FROM, "--effective-to", OPEN_TO,
    )
    assert result.returncode == 0, result.stderr
    assert f"target database: {db}" in result.stdout


def test_withdraw_can_close_a_window_that_already_ended(db: Path):
    """The owner-decision end date is a fixed calendar date, so it eventually passes.

    A plain ``withdraw`` then asked for a revision that started today and ended on that
    past date: the store refused it as an inverted window and the machine path stayed
    open until someone thought to pass ``--effective-to`` by hand. A withdrawal must
    always be able to close the path, so the end date is stretched to the withdrawal day
    (the tool list is empty either way).
    """
    past_from = (TODAY - datetime.timedelta(days=12)).isoformat()
    past_to = (TODAY - datetime.timedelta(days=6)).isoformat()
    assert _run(
        db, "publish", "--approved-by", "owner_anas",
        "--effective-from", past_from, "--effective-to", past_to,
    ).returncode == 0

    withdrawn = _run(db, "withdraw", "--approved-by", "owner_anas")

    assert withdrawn.returncode == 0, withdrawn.stderr
    assert "published and empty" in _run(db, "status").stdout


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
