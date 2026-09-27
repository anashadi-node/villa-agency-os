"""The daily rate is fetched — and a fetched rate is refused when it cannot be trusted.

The provider is never called from here: every test injects the answer it wants judged,
so the suite runs offline and a flaky network can never turn a refusal test green. What
is pinned is the judgement — an undated quote, a provider-reported failure, a base that
is not the pair's base, a rate that is not an exact positive decimal, a quote the
provider itself dates days ago, and a move no market made.

The last test in the file is the point of the whole thing: a rate published by the CLI
is the rate the *engine* converts with.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import fx as FX
from policy_engine import PolicyResolver, PolicyValueNotPublished
from primitives import FrozenClock, IdFactory
from reservation_kernel import ReservationKernel
from schema_loader import connect_sqlite

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from primitives import SystemClock  # noqa: E402  (path setup must run first)

# --------------------------------------------------------------------------- #
# The tool under test, loaded by path: scripts/ is not a package
# --------------------------------------------------------------------------- #
def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "refresh_fx_rate", REPO_ROOT / "scripts" / "refresh_fx_rate.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["refresh_fx_rate"] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


R = _load_tool()

NOW = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)
URL = "https://provider.example/v6/latest/USD"


def payload(**overrides):
    """A well-formed provider answer; each test moves exactly one thing."""
    answer = {
        "result": "success",
        "base_code": "USD",
        "provider": "https://provider.example",
        "rates": {"IDR": 17897.896326},
        "time_last_update_unix": int(NOW.timestamp()),
        "time_last_update_utc": NOW.strftime("%a, %d %b %Y %H:%M:%S +0000"),
    }
    answer.update(overrides)
    return answer


def quote(**overrides):
    return R.quote_from_payload(payload(**overrides), pair="USD/IDR", url=URL)


# --------------------------------------------------------------------------- #
# Reading an answer
# --------------------------------------------------------------------------- #
def test_a_quote_carries_the_pair_the_rate_and_the_providers_own_clock():
    found = quote()
    assert found.pair == "USD/IDR"
    assert found.quoted_at == NOW
    assert "quoted_at=" in found.source_ref and URL in found.source_ref


def test_a_json_float_is_read_as_its_exact_decimal_not_as_a_binary_approximation():
    """A float rate is a decimal written by the provider: read it back verbatim."""
    found = quote(rates={"IDR": 17897.896326})
    assert found.rate == Decimal("17897.896326")
    assert isinstance(found.rate, Decimal)


def test_the_quote_time_is_read_from_an_epoch_an_rfc2822_stamp_or_a_plain_date():
    epoch = quote()
    assert epoch.quoted_at == NOW
    stamped = R.quote_from_payload(
        {"result": "success", "base": "USD", "rates": {"IDR": 17914},
         "time_last_update_utc": "Fri, 25 Sep 2026 00:00:00 +0000"},
        pair="USD/IDR", url=URL,
    )
    assert stamped.quoted_at == datetime(2026, 9, 25, tzinfo=timezone.utc)
    dated = R.quote_from_payload(
        {"base": "USD", "rates": {"IDR": 17914}, "date": "2026-09-25"},
        pair="USD/IDR", url=URL,
    )
    assert dated.quoted_at == datetime(2026, 9, 25, tzinfo=timezone.utc)


def test_a_rate_nobody_dated_is_refused():
    """An undated rate cannot be judged fresh, so it is not a rate this tool publishes."""
    with pytest.raises(R.FeedError) as refused:
        R.quote_from_payload({"result": "success", "base_code": "USD", "rates": {"IDR": 17000}},
                             pair="USD/IDR", url=URL)
    assert "quote time" in str(refused.value)


def test_a_provider_reported_failure_is_refused():
    with pytest.raises(R.FeedError) as refused:
        quote(result="error")
    assert "error" in str(refused.value)


def test_a_payload_based_on_another_currency_is_refused():
    with pytest.raises(R.FeedError) as refused:
        quote(base_code="EUR")
    assert "EUR" in str(refused.value)


def test_a_payload_that_is_not_an_object_is_refused():
    with pytest.raises(R.FeedError):
        R.quote_from_payload([1, 2, 3], pair="USD/IDR", url=URL)


def test_a_missing_rate_table_is_refused():
    with pytest.raises(R.FeedError):
        R.quote_from_payload({"result": "success", "base_code": "USD"}, pair="USD/IDR", url=URL)


@pytest.mark.parametrize("bad", [0, -5, "17.897,9", None, "tujuh belas ribu"])
def test_a_rate_that_is_not_an_exact_positive_decimal_is_refused(bad):
    with pytest.raises(R.FeedError):
        quote(rates={"IDR": bad})


# --------------------------------------------------------------------------- #
# Judging an answer
# --------------------------------------------------------------------------- #
def test_a_quote_the_provider_dates_days_ago_is_refused():
    stale = R.Quote(pair="USD/IDR", rate=Decimal("17000"), quoted_at=NOW - timedelta(hours=72),
                    source_ref="x", provider="p")
    with pytest.raises(R.Refused) as refused:
        R.check_feed_age(stale, now=NOW, max_age_hours=48)
    assert "dead feed" in str(refused.value)


def test_a_quote_inside_the_age_limit_is_used():
    fresh = R.Quote(pair="USD/IDR", rate=Decimal("17000"), quoted_at=NOW - timedelta(hours=22),
                    source_ref="x", provider="p")
    R.check_feed_age(fresh, now=NOW, max_age_hours=48)


def test_a_quote_dated_in_the_future_is_refused():
    """A feed ahead of the clock is a clock disagreement, not a rate."""
    ahead = R.Quote(pair="USD/IDR", rate=Decimal("17000"), quoted_at=NOW + timedelta(hours=6),
                    source_ref="x", provider="p")
    with pytest.raises(R.Refused):
        R.check_feed_age(ahead, now=NOW, max_age_hours=48)


def test_a_move_no_market_made_is_refused():
    with pytest.raises(R.Refused) as refused:
        R.check_move(Decimal("30000"), Decimal("17000"), max_pct=Decimal("20"))
    assert "76.47%" in str(refused.value)


def test_an_ordinary_move_is_used():
    R.check_move(Decimal("17897.896326"), Decimal("17000"), max_pct=Decimal("20"))


def test_the_first_rate_has_nothing_to_move_against():
    R.check_move(Decimal("17897.896326"), None, max_pct=Decimal("20"))


# --------------------------------------------------------------------------- #
# Building the table that gets published
# --------------------------------------------------------------------------- #
def test_the_new_table_updates_one_pair_and_leaves_the_rest_alone():
    table = R.build_payload({"USD/IDR": "17000", "USD/SGD": "1.3521"}, pair="USD/IDR",
                            rate=Decimal("17897.896326"))
    assert table == {"USD/IDR": "17897.896326", "USD/SGD": "1.3521"}


def test_the_table_is_published_from_an_empty_one_too():
    assert R.build_payload(None, pair="USD/IDR", rate=Decimal("17897.896326")) == {
        "USD/IDR": "17897.896326"
    }


@pytest.mark.parametrize("table", [{"USD/IDR": "bukan angka"}, {"USD/IDR": "-5"}, {"USDIDR": "1"}])
def test_a_corrupt_published_table_is_refused_rather_than_quietly_dropped(table):
    with pytest.raises(R.Refused):
        R.build_payload(table, pair="USD/IDR", rate=Decimal("17897.896326"))


def test_the_previous_rate_is_read_from_the_table_or_reported_absent():
    assert R.previous_rate({"USD/IDR": "17000"}, pair="USD/IDR") == Decimal("17000")
    assert R.previous_rate({"USD/SGD": "1.3521"}, pair="USD/IDR") is None
    assert R.previous_rate(None, pair="USD/IDR") is None


# --------------------------------------------------------------------------- #
# The CLI as a program. The provider is a file the test writes, so the run is real
# (argv, exit code, store) while the answer stays controlled — and a flaky network
# can never turn a refusal test green.
# --------------------------------------------------------------------------- #
SCRIPT = REPO_ROOT / "scripts" / "refresh_fx_rate.py"

#: A window that is open now, so the assertions do not age. Passed to every run as
#: ``--effective-from``: a revision dated by the wall clock stops covering the
#: resolver's frozen date the moment the calendar rolls over.
FROZEN_NOW = "2026-09-28T06:00:00+00:00"
FROZEN_DAY = FROZEN_NOW[:10]


def _run(db: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI. ``db=None`` means *leave VILLA_DB unset* — a write must refuse to guess."""
    env = dict(os.environ)
    env.pop("VILLA_DB", None)
    if db is not None:
        env["VILLA_DB"] = str(db)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


def _quote_file(
    tmp_path: Path,
    *,
    rate: str = "17897.896326",
    currency: str = "IDR",
    quoted_at: datetime | None = None,
    base: str = "USD",
    result: str = "success",
) -> str:
    """A provider answer on disk, as a ``file://`` URL.

    Dated by the **platform's** day at 00:02 UTC unless told otherwise — never by the wall
    clock. The tool under test reads the real clock, so a fixture dated "now" is the one date a
    test cannot reproduce; a quote at the start of the platform's day is always in the past (so
    never future-dated) and never older than the feed limit. A fixture built from
    ``datetime.now()`` is a suite that ages.
    """
    stamp = quoted_at or datetime.combine(SystemClock().today(), time(0, 2), tzinfo=timezone.utc)
    payload = {
        "result": result,
        "base_code": base,
        "time_last_update_unix": int(stamp.timestamp()),
        "rates": {currency: rate},
    }
    path = tmp_path / f"quote-{currency}-{rate.replace('.', '_')}-{int(stamp.timestamp())}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path.as_uri()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "villa.db"
    kernel = ReservationKernel.open(
        str(path),
        clock=FrozenClock.at(FROZEN_NOW),
        ids=IdFactory(deterministic=True, seed="fx"),
        agency_id="agency_default",
    )
    kernel.close()
    return path


def _revisions(db: Path, key: str = FX.FX_RATES_KEY) -> int:
    conn = connect_sqlite(str(db))
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM policy_revisions WHERE policy_key = ?", (key,)
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def _effective(db: Path, key: str = FX.FX_RATES_KEY):
    """The revision a reader sees on the frozen day, or ``None`` when nothing is published."""
    conn = connect_sqlite(str(db))
    try:
        resolver = PolicyResolver(
            conn,
            clock=FrozenClock.at(FROZEN_NOW),
            ids=IdFactory(deterministic=True, seed="read"),
            agency_id="agency_default",
        )
        try:
            return resolver.resolve(key, at=FROZEN_NOW)
        except PolicyValueNotPublished:
            return None
    finally:
        conn.close()


def _publish(db: Path, tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return _run(
        db,
        "--url",
        _quote_file(tmp_path),
        "--approved-by",
        "owner_anas",
        "--effective-from",
        FROZEN_DAY,
        *extra,
    )


def test_the_cli_publishes_the_fetched_rate_with_its_source(db, tmp_path):
    done = _publish(db, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "PUBLISHED" in done.stdout

    published = _effective(db)
    assert published is not None
    # `.value` is the *stored text* of a JSON key — `as_json()` is the parsed object. Reading
    # `.value` as if it were the object is what made the tool drop the other published pairs.
    assert published.as_json() == {"USD/IDR": "17897.896326"}
    assert published.approved_by == "owner_anas"
    # The provenance is the provider document itself, down to the quote time: a rate whose
    # source cannot be re-read is a number nobody can audit.
    assert "quote-" in published.source_doc_ref
    assert "#quoted_at=" in published.source_doc_ref


def test_the_published_rate_is_the_one_the_engine_converts_with(db, tmp_path):
    assert _publish(db, tmp_path).returncode == 0
    table = FX.FxTable.from_policy(_effective(db).as_json())
    # 25 USD of margin, expressed in rupiah and rounded up — the number the floor guard uses.
    assert table.convert_minor(2500, from_currency="USD", to_currency="IDR") == 447448


def test_the_cli_writes_nothing_without_an_approver(db, tmp_path):
    done = _run(db, "--url", _quote_file(tmp_path), "--effective-from", FROZEN_DAY)
    assert done.returncode == 2
    assert "--approved-by is required" in done.stderr
    assert _effective(db) is None
    assert _revisions(db) == 0


def test_the_cli_writes_nothing_without_an_explicit_database(tmp_path):
    done = _run(None, "--url", _quote_file(tmp_path), "--approved-by", "owner_anas")
    assert done.returncode == 2
    assert "VILLA_DB must be set explicitly" in done.stderr


def test_a_dry_run_reports_the_rate_and_publishes_nothing(db, tmp_path):
    done = _run(
        db,
        "--url",
        _quote_file(tmp_path),
        "--approved-by",
        "owner_anas",
        "--effective-from",
        FROZEN_DAY,
        "--dry-run",
    )
    assert done.returncode == 0, done.stderr
    assert "DRY RUN" in done.stdout
    assert "17897.896326" in done.stdout
    assert _effective(db) is None
    assert _revisions(db) == 0


def test_the_cli_refuses_a_quote_the_provider_dates_days_ago(db, tmp_path):
    three_days = datetime.combine(SystemClock().today(), time(0, 2), tzinfo=timezone.utc)
    three_days -= timedelta(days=3)
    done = _run(
        db,
        "--url",
        _quote_file(tmp_path, quoted_at=three_days),
        "--approved-by",
        "owner_anas",
        "--effective-from",
        FROZEN_DAY,
    )
    assert done.returncode == 1
    assert "REFUSED" in done.stderr
    assert _effective(db) is None


def test_the_cli_refuses_a_move_no_market_made(db, tmp_path):
    assert _publish(db, tmp_path).returncode == 0
    jumped = _run(
        db,
        "--url",
        _quote_file(tmp_path, rate="30000"),
        "--approved-by",
        "owner_anas",
        "--effective-from",
        FROZEN_DAY,
    )
    assert jumped.returncode == 1
    assert "REFUSED" in jumped.stderr
    # The published table is untouched: a refusal leaves the last real rate in force.
    assert _effective(db).as_json() == {"USD/IDR": "17897.896326"}


def test_the_cli_refreshes_one_pair_and_leaves_the_other_alone(db, tmp_path):
    first = _run(
        db,
        "--url",
        _quote_file(tmp_path, rate="17000"),
        "--approved-by",
        "owner_anas",
        "--effective-from",
        FROZEN_DAY,
    )
    assert first.returncode == 0, first.stderr

    second = _run(
        db,
        "--pair",
        "USD/SGD",
        "--url",
        _quote_file(tmp_path, rate="1.3521", currency="SGD"),
        "--approved-by",
        "owner_anas",
        "--effective-from",
        FROZEN_DAY,
    )
    assert second.returncode == 0, second.stderr
    assert _effective(db).as_json() == {"USD/IDR": "17000", "USD/SGD": "1.3521"}


def test_an_unreachable_provider_has_its_own_exit_code(db, tmp_path):
    done = _run(
        db,
        "--url",
        (tmp_path / "no-such-quote.json").as_uri(),
        "--approved-by",
        "owner_anas",
        "--effective-from",
        FROZEN_DAY,
    )
    assert done.returncode == 3
    assert "PROVIDER REFUSED" in done.stderr
    assert _effective(db) is None


def test_a_second_run_on_the_same_day_publishes_nothing_new(db, tmp_path):
    assert _publish(db, tmp_path).returncode == 0
    again = _publish(db, tmp_path)
    assert again.returncode == 0, again.stderr
    assert "UNCHANGED" in again.stdout
    assert _revisions(db) == 1
