"""The owner's manual act: setting a policy value without a code change.

§15 makes every threshold configuration, so the numbers the owner decides have to be
settable **by the owner** — otherwise "the owner fills it in" is not true of the running
system. These tests pin the two inherited disciplines (name the database; cite the
document and the approver), the type coercion, and the point of the whole thing: the
engine obeys what was set, and only what was set.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from discounts import DiscountEngine, DiscountRequest
from policy_engine import PolicyResolver
from primitives import FrozenClock, IdFactory
from reservation_kernel import ReservationKernel
from schema_loader import connect_sqlite

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "publish_policy_value.py"

AUTHORITY = "commercial.discount.authority_roles"
FLOOR_PCT = "commercial.margin_floor_pct"
FLOOR_MINOR = "commercial.margin_floor_minor"

#: A window that is open now, so the assertions do not age.
TODAY = datetime.date.today().isoformat()
NOW = "2026-09-26T09:00:00+00:00"
DOC = "Keputusan owner — uji kondisi"


def _run(db: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI. ``db=None`` means *leave VILLA_DB unset* — the case that matters for a
    write, which must refuse to guess its target rather than inherit one."""
    env = dict(os.environ)
    env.pop("VILLA_DB", None)
    if db is not None:
        env["VILLA_DB"] = str(db)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=120,
    )


def _revisions(db: Path, key: str) -> int:
    conn = connect_sqlite(str(db))
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM policy_revisions WHERE policy_key = ?", (key,)
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def _resolver(db: Path) -> PolicyResolver:
    return PolicyResolver(
        connect_sqlite(str(db)),
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed="read"),
        agency_id="agency_default",
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "villa.db"
    kernel = ReservationKernel.open(
        str(path),
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed="cli"),
        agency_id="agency_default",
    )
    kernel.close()
    return path


def _set(db: Path, key: str, value: str, **extra: str) -> subprocess.CompletedProcess[str]:
    args = [
        "set", "--key", key, "--value", value,
        "--source-doc-ref", extra.pop("doc", DOC),
        "--approved-by", extra.pop("by", "owner_anas"),
        "--effective-from", extra.pop("effective_from", TODAY),
    ]
    for flag, val in extra.items():
        args += [f"--{flag.replace('_', '-')}", val]
    return _run(db, *args)


def _publish_discount_policy(db: Path) -> None:
    """Exactly what the owner does by hand, through the documented command."""
    assert _set(db, AUTHORITY, '["owner","supervisor"]').returncode == 0
    assert _set(db, FLOOR_PCT, "20").returncode == 0
    assert _set(db, FLOOR_MINOR, "1000000").returncode == 0


def _evaluate(db: Path, request: DiscountRequest) -> Any:
    """Open, evaluate, close — the engine holds no state worth keeping between calls."""
    conn = connect_sqlite(str(db))
    try:
        return DiscountEngine(
            conn,
            clock=FrozenClock.at(NOW),
            ids=IdFactory(deterministic=True, seed="eng"),
            agency_id="agency_default",
        ).evaluate(request)
    finally:
        conn.close()


def _request(role: str = "supervisor", amount: int = 5_000_000) -> DiscountRequest:
    return DiscountRequest(
        kind="AGENCY_FUNDED",
        amount_minor=amount,
        currency="IDR",
        reason="loyalty",
        actor="agent_ana",
        actor_role=role,
        revenue_minor=100_000_000,
        margin_before_minor=30_000_000,
        subject_ref="quote_1",
    )


# --- the reading act ---------------------------------------------------------------


def test_show_lists_the_keys_and_says_which_are_still_waiting(db: Path) -> None:
    result = _run(db, "show")
    assert result.returncode == 0, result.stderr
    assert AUTHORITY in result.stdout
    assert "waiting on an owner decision" in result.stdout


def test_show_for_one_key_names_the_owner_role_and_the_publish_command(db: Path) -> None:
    result = _run(db, "show", "--key", FLOOR_PCT)
    assert result.returncode == 0, result.stderr
    assert "NOT PUBLISHED" in result.stdout
    assert "decided by  : owner" in result.stdout
    assert f"set --key {FLOOR_PCT}" in result.stdout


# --- the writing act ---------------------------------------------------------------


def test_a_write_without_an_approver_is_refused_and_changes_nothing(db: Path) -> None:
    result = _run(
        db, "set", "--key", FLOOR_PCT, "--value", "20", "--source-doc-ref", DOC
    )
    assert result.returncode == 2
    assert "--approved-by" in result.stderr
    assert _revisions(db, FLOOR_PCT) == 0


def test_a_write_without_a_document_is_refused_and_changes_nothing(db: Path) -> None:
    result = _run(
        db, "set", "--key", FLOOR_PCT, "--value", "20", "--approved-by", "owner_anas"
    )
    assert result.returncode == 2
    assert "--source-doc-ref" in result.stderr
    assert _revisions(db, FLOOR_PCT) == 0


def test_a_write_without_villa_db_is_refused_rather_than_guessing_a_target(db: Path) -> None:
    """A value that lands in whatever ``data/villa.db`` sits in the working directory is a
    decision nobody made."""
    result = _run(
        None, "set", "--key", FLOOR_PCT, "--value", "20",
        "--source-doc-ref", DOC, "--approved-by", "owner_anas",
    )
    assert result.returncode == 2
    assert "VILLA_DB must be set explicitly" in result.stderr
    assert _revisions(db, FLOOR_PCT) == 0


def test_a_count_that_is_not_a_number_is_refused_before_the_write(db: Path) -> None:
    result = _set(db, FLOOR_MINOR, "satu juta")
    assert result.returncode == 2
    assert "whole number" in result.stderr
    assert _revisions(db, FLOOR_MINOR) == 0


def test_an_unknown_key_is_refused_by_the_store(db: Path) -> None:
    result = _set(db, "commercial.tidak.ada", "1")
    assert result.returncode == 1
    assert "POLICY_UNKNOWN_KEY" in result.stderr
    assert _revisions(db, "commercial.tidak.ada") == 0


def test_a_published_value_reads_back_with_who_decided_and_why(db: Path) -> None:
    assert _set(db, FLOOR_PCT, "20").returncode == 0
    shown = _run(db, "show", "--key", FLOOR_PCT)
    assert "20 (version 1)" in shown.stdout
    assert "owner_anas" in shown.stdout
    assert DOC in shown.stdout

    conn = connect_sqlite(str(db))
    try:
        row = conn.execute(
            "SELECT approved_by, source_doc_ref, version FROM policy_revisions"
            " WHERE policy_key = ?",
            (FLOOR_PCT,),
        ).fetchone()
    finally:
        conn.close()
    assert row["approved_by"] == "owner_anas"
    assert row["source_doc_ref"] == DOC
    assert int(row["version"]) == 1


def test_a_role_set_is_published_as_json(db: Path) -> None:
    assert _set(db, AUTHORITY, '["owner","supervisor"]').returncode == 0
    resolver = _resolver(db)
    try:
        assert resolver.resolve(AUTHORITY).as_json() == ["owner", "supervisor"]
    finally:
        resolver.conn.close()


# --- the point: the engine obeys what the owner set ---------------------------------


def test_the_engine_applies_a_discount_once_the_owner_has_published(db: Path) -> None:
    _publish_discount_policy(db)
    decision = _evaluate(db, _request())
    assert decision.outcome == "APPLIED"
    assert decision.code == "DISCOUNT_APPLIED"
    # 30,000,000 before minus the 5,000,000 granted.
    assert decision.margin_after_minor == 25_000_000
    assert (decision.floor_pct, decision.floor_minor) == (Decimal("20"), 1_000_000)


def test_a_breach_still_needs_a_human_after_publication(db: Path) -> None:
    _publish_discount_policy(db)
    decision = _evaluate(db, _request(amount=25_000_000))
    assert decision.outcome == "REQUIRES_OVERRIDE"
    assert decision.code == "MARGIN_FLOOR_BREACHED"


def test_an_unauthorised_role_is_still_refused_after_publication(db: Path) -> None:
    _publish_discount_policy(db)
    decision = _evaluate(db, _request(role="agent"))
    assert decision.outcome == "REFUSED"
    assert decision.code == "DISCOUNT_AUTHORITY_REFUSED"


def test_the_values_come_from_policy_and_not_from_the_module(db: Path) -> None:
    """Change the policy, change the behaviour — with no code change in between."""
    _publish_discount_policy(db)
    # A later revision raises the percentage floor to 29%: a 5,000,000 discount on a
    # 30,000,000 margin leaves 25,000,000, which is now below the floor.
    resolver = _resolver(db)
    try:
        resolver.publish(
            FLOOR_PCT, "29", effective_from=TODAY, source_doc_ref=DOC,
            approved_by="owner_anas",
        )
    finally:
        resolver.conn.close()
    assert _evaluate(db, _request()).code == "MARGIN_FLOOR_BREACHED"
