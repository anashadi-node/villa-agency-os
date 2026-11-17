"""PACK-04A (FR-904, DB layer only) — PostgreSQL range exclusion under real concurrency.

What this file proves, when it runs against a live server:
    ``ex_holds_no_overlap`` / ``ex_bookings_no_overlap`` and the idempotency uniques reject
    a conflicting write issued from a *second connection* while the first is still open.

What it does NOT prove — and must never be quoted as proving:
    * that ``ReservationKernel`` runs on PostgreSQL (it is SQLite-only today, no adapter);
    * HOLD-versus-BOOKING consistency: the two exclusions live on two tables, so nothing in
      the database stops a hold and a booking from overlapping (see the strict xfail below);
    * production readiness: ``connect_pg(apply=True)`` applies 001 only, so a database built
      through the production loader has NO exclusion constraint (CONTRACT_DRIFT, reported).

Safety model (this is the part that matters more than the tests)
    1. Nothing here ever drops or recreates a database/schema it did not create. The harness
       ``CREATE DATABASE``s its own ``villa_t_<random>`` and drops exactly that name.
    2. Before the first destructive statement the server must prove it is a disposable CI
       service (:func:`prove_disposable_server`): GitHub-hosted runner, a run token that
       equals ``GITHUB_RUN_ID-GITHUB_RUN_ATTEMPT``, a freshly started postmaster, no
       foreign database on the cluster, and a non-superuser role. Host/port/db name are
       deliberately not part of the proof. Any doubt raises :class:`DisposableRefused`.
    3. A DSN that is present but unproven FAILS; only "no DSN and not required" skips.

``tests/test_schema.py`` and ``tests/test_workforce_gate_g1.py`` import
:func:`disposable_database` so the whole ``postgres-parity`` job shares one guard.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "src" / "schema"
PG_001 = SCHEMA_DIR / "001_initial.pg.sql"
PG_002 = SCHEMA_DIR / "002_pg_range_exclusions.pg.sql"

DB_PREFIX = "villa_t_"
MAX_SERVER_AGE_SECONDS = 3600  # the CI job has a 15 minute timeout; a service older than 1 h is not ours
STATE_TS = "2026-09-21T00:00:00+00:00"
EXPIRES_TS = "2026-09-22T00:00:00+00:00"
FINGERPRINT = "0" * 64

#: Names this process created and may therefore drop. Nothing else is ever dropped.
_CREATED: set[str] = set()


# =========================================================================== guard ==
class DisposableRefused(RuntimeError):
    """The environment could not be proven disposable. Always fail closed."""


def _driver() -> Any:
    try:
        import psycopg  # type: ignore
    except ImportError as exc:  # pinned in requirements-dev.txt
        raise DisposableRefused(
            "psycopg (v3) is required for the live PostgreSQL tests: "
            "python -m pip install -r requirements-dev.txt"
        ) from exc
    return psycopg


def check_ci_environment(env: Mapping[str, str]) -> None:
    """Refuse unless this is the GitHub-hosted job that owns the service container.

    This stops the accident (a DSN left in a shell profile), not a determined actor:
    anyone who can set these variables can already run SQL.
    """
    problems = []
    if env.get("GITHUB_ACTIONS") != "true":
        problems.append("not running inside GitHub Actions")
    if env.get("RUNNER_ENVIRONMENT") != "github-hosted":
        problems.append("runner is not GitHub-hosted")
    run_id, attempt = env.get("GITHUB_RUN_ID"), env.get("GITHUB_RUN_ATTEMPT")
    expected = f"{run_id}-{attempt}" if run_id and attempt else None
    if expected is None:
        problems.append("GITHUB_RUN_ID/GITHUB_RUN_ATTEMPT missing")
    elif env.get("VILLA_PG_DISPOSABLE_RUN") != expected:
        problems.append("VILLA_PG_DISPOSABLE_RUN does not match this run")
    if problems:
        raise DisposableRefused("refusing destructive PostgreSQL tests: " + "; ".join(problems))


def check_server_evidence(age_seconds: Optional[float], databases: list[str], is_superuser: bool) -> None:
    """Server-side facts that a long-lived or shared cluster cannot satisfy."""
    problems = []
    if age_seconds is None or age_seconds < 0 or age_seconds > MAX_SERVER_AGE_SECONDS:
        problems.append("server has been up too long to be this job's service container")
    foreign = [name for name in databases if name != "postgres" and not name.startswith(DB_PREFIX)]
    if foreign:
        problems.append(f"{len(foreign)} database(s) other than 'postgres' exist on this cluster")
    if is_superuser:
        problems.append("connected role is a superuser; tests require a least-privilege role")
    if problems:
        raise DisposableRefused("refusing destructive PostgreSQL tests: " + "; ".join(problems))


def prove_disposable_server(dsn: str, env: Optional[Mapping[str, str]] = None) -> None:
    check_ci_environment(os.environ if env is None else env)
    psycopg = _driver()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        age, names, superuser = conn.execute(
            "SELECT EXTRACT(EPOCH FROM now() - pg_postmaster_start_time())::float8,"
            " (SELECT array_agg(datname) FROM pg_database WHERE NOT datistemplate),"
            " current_setting('is_superuser') = 'on'"
        ).fetchone()
    check_server_evidence(age, list(names or []), bool(superuser))


def _drop_owned(dsn: str, name: str) -> None:
    """Drop ``name`` only if this process created it. Anything else is refused."""
    if name not in _CREATED or not re.fullmatch(re.escape(DB_PREFIX) + r"[0-9a-f]{12}", name):
        raise DisposableRefused("refusing to drop a database this harness did not create")
    psycopg = _driver()
    from psycopg import sql  # type: ignore

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    _CREATED.discard(name)


@contextlib.contextmanager
def disposable_database(dsn: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> Iterator[str]:
    """Yield a DSN for a brand-new, harness-owned database; drop it afterwards."""
    dsn = dsn or os.environ.get("VILLA_PG_DSN")
    if not dsn:
        raise DisposableRefused("no DSN given")
    prove_disposable_server(dsn, env)
    psycopg = _driver()
    from psycopg import sql  # type: ignore
    from psycopg.conninfo import make_conninfo  # type: ignore

    name = DB_PREFIX + secrets.token_hex(6)
    _CREATED.add(name)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        yield make_conninfo(dsn, dbname=name)
    finally:
        if name in _CREATED:
            _drop_owned(dsn, name)


def _require_dsn() -> str:
    """No DSN: skip, unless the job demands PostgreSQL — then fail, never skip."""
    dsn = os.environ.get("VILLA_PG_DSN")
    if dsn:
        return dsn
    if os.environ.get("VILLA_PG_REQUIRED") == "1":
        raise RuntimeError(
            "VILLA_PG_REQUIRED=1 but VILLA_PG_DSN is unset: refusing to report a passing gate "
            "while the PostgreSQL constraints are unproven."
        )
    pytest.skip("VILLA_PG_DSN not set: live PostgreSQL concurrency tests skipped")


def apply_twin(dsn: str) -> None:
    """Apply 001 then 002 explicitly (the production loader applies 001 only: CONTRACT_DRIFT)."""
    import schema_loader as SL

    psycopg = _driver()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        for path in (PG_001, PG_002):
            for statement in SL.split_statements(path.read_text(encoding="utf-8")):
                if statement.strip():
                    conn.execute(statement)


# ===================================================================== SQL helpers ==
KINDS: Mapping[str, Mapping[str, str]] = {
    "booking": {
        "table": "bookings", "pk": "booking_id", "live": "CONFIRMED", "free": "PAYMENT_PENDING",
        "exclusion": "ex_bookings_no_overlap", "idem": "uq_bookings_idempotency",
    },
    "hold": {
        "table": "holds", "pk": "hold_id", "live": "CONFIRMED", "free": "REJECTED",
        "exclusion": "ex_holds_no_overlap", "idem": "uq_holds_idempotency",
    },
}

_BOOKING_SQL = (
    "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights, status,"
    " guest_ref, adults, children, infants, currency, policy_version_id, policy_fingerprint,"
    " total_minor, payment_status, idempotency_key, created_at, updated_at)"
    " VALUES (%(id)s, 'prop_cc', %(unit)s, %(ci)s, %(co)s, 2, %(status)s, 'guest_cc', 2, 0, 0,"
    " 'IDR', 'policy_cc', %(fp)s, 1000000, 'UNPAID', %(idem)s, %(ts)s, %(ts)s)"
)
_HOLD_SQL = (
    "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights, status,"
    " guest_ref, quote_ref, policy_version_id, evidence_id, idempotency_key, created_at,"
    " expires_at, expires_timezone, closed_at, close_reason, extended_count)"
    " VALUES (%(id)s, %(unit)s, 'prop_cc', %(ci)s, %(co)s, 2, %(status)s, 'guest_cc', NULL,"
    " 'policy_cc', %(evid)s, %(idem)s, %(ts)s, %(exp)s, 'Asia/Makassar', %(closed)s, %(reason)s, 0)"
)


def seed_world(dsn: str) -> None:
    psycopg = _driver()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute(
            "INSERT INTO properties (property_id, name, property_type, region, province, timezone,"
            " status, contract_pack_status, contract_pack_evaluated_at, created_at, updated_at)"
            " VALUES ('prop_cc', 'Concurrency probe', 'villa', 'Probe', 'Bali', 'Asia/Makassar',"
            " 'SELLABLE', 'VERIFIED', NULL, %s, %s)", (STATE_TS, STATE_TS),
        )
        conn.execute(
            "INSERT INTO policy_versions (policy_version_id, property_id, unit_scope, version,"
            " effective_from, effective_to, currency, deposit_pct, service_fee_pct, tax_pbjt_pct,"
            " max_discount_pct, quote_valid_hours, hold_ttl_minutes, max_hold_extensions, payload,"
            " fingerprint, source_doc_ref, approved_by, created_at)"
            " VALUES ('policy_cc', 'prop_cc', 'property', 1, '2026-01-01', '2027-12-31', 'IDR',"
            " '30', '5', '10', '15', 48, 720, 2, '{}', %s, 'probe', 'user:probe', %s)",
            (FINGERPRINT, STATE_TS),
        )


def new_unit(dsn: str) -> str:
    unit = "unit_cc_" + secrets.token_hex(4)
    psycopg = _driver()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute(
            "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
            " base_capacity, max_guests, status, created_at, updated_at)"
            " VALUES (%s, 'prop_cc', 'Probe unit', 'entire_villa', 1, 1, 2, 4, 'ACTIVE', %s, %s)",
            (unit, STATE_TS, STATE_TS),
        )
        conn.execute(
            "INSERT INTO availability_evidence (evidence_id, unit_id, evidence_level, source_type,"
            " source_ref, captured_at, valid_until, result, occupancy_ranges, can_quote, can_hold,"
            " can_confirm, notes, created_at) VALUES (%s, %s, 'A', 'manager_written', 'probe', %s,"
            " %s, 'AVAILABLE', '[]', 1, 1, 1, NULL, %s)",
            ("evid_" + unit, unit, STATE_TS, EXPIRES_TS, STATE_TS),
        )
    return unit


def make_insert(kind: str, unit: str, check_in: str, check_out: str, *, status: Optional[str] = None,
                row_id: Optional[str] = None, idem: Optional[str] = None) -> tuple[str, dict, str]:
    spec = KINDS[kind]
    status = status or spec["live"]
    row_id = row_id or f"{kind[:2]}_{secrets.token_hex(5)}"
    params: dict[str, Any] = {"id": row_id, "unit": unit, "ci": check_in, "co": check_out,
                              "status": status, "idem": idem or f"idem_{row_id}", "ts": STATE_TS}
    if kind == "booking":
        params["fp"] = FINGERPRINT
        return _BOOKING_SQL, params, row_id
    closed = status not in ("REQUESTED", "AWAITING_SUPPLIER", "CONFIRMED", "EXTENSION_REQUESTED",
                            "RELEASE_REQUESTED")
    params.update(evid="evid_" + unit, exp=EXPIRES_TS,
                  closed=STATE_TS if closed else None, reason="probe" if closed else None)
    return _HOLD_SQL, params, row_id


def occupying(kind: str) -> tuple[str, ...]:
    if kind == "booking":
        import reservation_kernel as RK
        return tuple(RK.OCCUPYING_BOOKING_STATES)
    import hold_states as HS
    return tuple(HS.OCCUPYING_HOLD_STATES)


def overlapping_pairs(conn: Any, kind: str, units: list[str]) -> int:
    """Independent invariant: ISO text compares like dates, so no range type, no cast, no
    constraint machinery is involved — it cannot be fooled by the thing under test."""
    spec = KINDS[kind]
    table, pk = spec["table"], spec["pk"]
    row = conn.execute(
        f"SELECT count(*) FROM {table} a JOIN {table} b ON a.unit_id = b.unit_id AND a.{pk} < b.{pk}"
        " WHERE a.status = ANY(%s) AND b.status = ANY(%s) AND a.unit_id = ANY(%s)"
        " AND a.check_in < b.check_out AND b.check_in < a.check_out",
        (list(occupying(kind)), list(occupying(kind)), units),
    ).fetchone()
    return int(row[0])


def count_rows(conn: Any, kind: str, unit: str, *, only_occupying: bool = False) -> int:
    spec = KINDS[kind]
    sql = f"SELECT count(*) FROM {spec['table']} WHERE unit_id = %s"
    args: list[Any] = [unit]
    if only_occupying:
        sql += " AND status = ANY(%s)"
        args.append(list(occupying(kind)))
    return int(conn.execute(sql, args).fetchone()[0])


# ============================================================ concurrency machinery ==
@dataclass
class Outcome:
    committed: bool
    sqlstate: Optional[str] = None
    constraint: Optional[str] = None
    error: Optional[str] = None


class Attempt(threading.Thread):
    """One writer on its OWN connection, in its own thread, committing or rolling back."""

    def __init__(self, dsn: str, statements: list[tuple[str, Any]], *,
                 barrier: Optional[threading.Barrier] = None,
                 lock_timeout_ms: Optional[int] = None, then: str = "commit") -> None:
        super().__init__(daemon=True)
        self.dsn, self.statements, self.barrier = dsn, statements, barrier
        self.lock_timeout_ms, self.then = lock_timeout_ms, then
        self.connected = threading.Event()
        self.pid: Optional[int] = None
        self.outcome: Optional[Outcome] = None

    def run(self) -> None:
        psycopg = _driver()
        options = "-c statement_timeout=15000"
        if self.lock_timeout_ms:
            options += f" -c lock_timeout={self.lock_timeout_ms}"
        conn = None
        try:
            conn = psycopg.connect(self.dsn, options=options, connect_timeout=10)
            self.pid = conn.info.backend_pid
            self.connected.set()
            if self.barrier is not None:
                self.barrier.wait(10)
            for sql, params in self.statements:
                conn.execute(sql, params)
            conn.commit() if self.then == "commit" else conn.rollback()
            self.outcome = Outcome(True)
        except psycopg.Error as exc:
            self.outcome = Outcome(False, exc.sqlstate, exc.diag.constraint_name)
        except Exception as exc:  # barrier timeout etc.: surfaced, never swallowed
            self.outcome = Outcome(False, error=repr(exc))
        finally:
            self.connected.set()
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()


def wait_until_blocked(observer: Any, attempt: Attempt, timeout: float = 10.0) -> bool:
    """True once the observer sees the attempt's backend waiting on a lock held by another txn."""
    assert attempt.connected.wait(timeout), "writer never connected"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = observer.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (attempt.pid,)
        ).fetchone()
        if row and row[0] == "Lock":
            return True
        time.sleep(0.05)
    return False


def finish(*attempts: Attempt, timeout: float = 20.0) -> list[Outcome]:
    for attempt in attempts:
        attempt.join(timeout)
        assert not attempt.is_alive(), "writer thread did not finish within its time box"
        assert attempt.outcome is not None and attempt.outcome.error is None, attempt.outcome
    return [a.outcome for a in attempts]  # type: ignore[misc]


def race(dsn: str, statements_per_writer: list[list[tuple[str, Any]]]) -> list[Outcome]:
    barrier = threading.Barrier(len(statements_per_writer))
    writers = [Attempt(dsn, stmts, barrier=barrier) for stmts in statements_per_writer]
    for writer in writers:
        writer.start()
    return finish(*writers)


# ================================================================= tests: guard only ==
GOOD_ENV = {
    "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
    "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "VILLA_PG_DISPOSABLE_RUN": "123-2",
}


class TestDisposableGuard:
    """Run with or without PostgreSQL: these prove the guard itself refuses."""

    def test_accepts_only_the_exact_hosted_run(self):
        check_ci_environment(GOOD_ENV)

    @pytest.mark.parametrize("override", [
        {"GITHUB_ACTIONS": "false"},
        {"GITHUB_ACTIONS": ""},
        {"RUNNER_ENVIRONMENT": "self-hosted"},
        {"RUNNER_ENVIRONMENT": ""},
        {"VILLA_PG_DISPOSABLE_RUN": "123-1"},
        {"VILLA_PG_DISPOSABLE_RUN": ""},
        {"GITHUB_RUN_ID": ""},
        {"GITHUB_RUN_ATTEMPT": ""},
    ])
    def test_refuses_anything_that_is_not_this_hosted_run(self, override):
        with pytest.raises(DisposableRefused):
            check_ci_environment({**GOOD_ENV, **override})

    def test_refuses_an_empty_environment(self):
        with pytest.raises(DisposableRefused):
            check_ci_environment({})

    def test_refuses_a_cluster_that_is_old_shared_or_privileged(self):
        check_server_evidence(30.0, ["postgres"], False)
        check_server_evidence(30.0, ["postgres", DB_PREFIX + "0" * 12], False)
        for age, names, superuser in (
            (MAX_SERVER_AGE_SECONDS + 1, ["postgres"], False),   # long-lived server
            (None, ["postgres"], False),                          # unknown age
            (30.0, ["postgres", "anantara"], False),              # someone else's database
            (30.0, ["postgres"], True),                           # superuser
        ):
            with pytest.raises(DisposableRefused):
                check_server_evidence(age, names, superuser)

    @pytest.mark.parametrize("name", ["postgres", "public", "villa", DB_PREFIX + "0" * 12, "villa_t_XYZ"])
    def test_never_drops_a_database_it_did_not_create(self, name):
        # Raises before any connection is attempted, so no server is needed.
        with pytest.raises(DisposableRefused):
            _drop_owned("host=unreachable.invalid", name)

    def test_disposable_database_refuses_before_connecting(self):
        with pytest.raises(DisposableRefused):
            with disposable_database("host=unreachable.invalid", env={}):
                pytest.fail("body must not run")
        assert not _CREATED

    def test_required_mode_without_a_dsn_fails_instead_of_skipping(self, monkeypatch):
        monkeypatch.delenv("VILLA_PG_DSN", raising=False)
        monkeypatch.setenv("VILLA_PG_REQUIRED", "1")
        with pytest.raises(RuntimeError, match="VILLA_PG_REQUIRED=1"):
            _require_dsn()
        monkeypatch.delenv("VILLA_PG_REQUIRED")
        with pytest.raises(pytest.skip.Exception):
            _require_dsn()


# ============================================================ tests: live PostgreSQL ==
@pytest.fixture(scope="module")
def world():
    dsn = _require_dsn()
    with disposable_database(dsn) as test_dsn:
        apply_twin(test_dsn)
        seed_world(test_dsn)
        yield test_dsn


@pytest.fixture()
def observer(world):
    psycopg = _driver()
    with psycopg.connect(world, autocommit=True, connect_timeout=10) as conn:
        yield conn


@pytest.fixture(params=["booking", "hold"])
def kind(request):
    return request.param


PARTIAL_A = ("2027-03-01", "2027-03-04")
PARTIAL_B = ("2027-03-02", "2027-03-05")   # overlaps A, different check_in: only the EXCLUDE can fire


class TestConstraintsInstalled:
    def test_exclusions_idempotency_and_slot_guards_exist_with_baseline_predicates(self, world, observer):
        defs = dict(observer.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype = 'x'"
        ).fetchall())
        for kind_name, spec in KINDS.items():
            definition = defs.get(spec["exclusion"])
            assert definition is not None, f"{spec['exclusion']} is not installed"
            assert "gist" in definition and "daterange" in definition and "'[)'" in definition
            assert set(re.findall(r"'([A-Z_]+)'", definition)) == set(occupying(kind_name)), kind_name
        unique = {r[0] for r in observer.execute(
            "SELECT conname FROM pg_constraint WHERE contype = 'u'").fetchall()}
        indexes = {r[0] for r in observer.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('holds', 'bookings')").fetchall()}
        assert {"uq_holds_idempotency", "uq_bookings_idempotency"} <= unique
        assert {"uq_holds_active_slot", "uq_bookings_active_slot"} <= indexes
        assert observer.execute("SELECT count(*) FROM pg_extension WHERE extname = 'btree_gist'").fetchone()[0] == 1


class TestOverlapUnderConcurrency:
    def test_second_writer_waits_on_the_first_then_is_rejected(self, world, observer, kind):
        """A and B are two real connections. B is observed WAITING (not failing instantly), so
        the exclusion is evaluated against an in-flight, uncommitted row — the case a
        SELECT-then-INSERT guard loses."""
        psycopg = _driver()
        unit = new_unit(world)
        sql_a, par_a, _ = make_insert(kind, unit, *PARTIAL_A)
        sql_b, par_b, _ = make_insert(kind, unit, *PARTIAL_B)
        conn_a = psycopg.connect(world, connect_timeout=10)
        try:
            conn_a.execute(sql_a, par_a)                       # A: uncommitted
            writer_b = Attempt(world, [(sql_b, par_b)])
            writer_b.start()
            assert wait_until_blocked(observer, writer_b), "B was never observed waiting on A's transaction"
            assert writer_b.is_alive()
            conn_a.commit()                                    # A wins
        finally:
            conn_a.close()
        (outcome_b,) = finish(writer_b)
        assert not outcome_b.committed
        assert outcome_b.sqlstate == "23P01"                   # exclusion_violation, nothing else
        assert outcome_b.constraint == KINDS[kind]["exclusion"]
        assert count_rows(observer, kind, unit, only_occupying=True) == 1
        assert overlapping_pairs(observer, kind, [unit]) == 0

    def test_barrier_race_many_rounds_commits_exactly_one(self, world, observer, kind):
        units, winners = [], 0
        for _ in range(12):
            unit = new_unit(world)
            units.append(unit)
            a = make_insert(kind, unit, *PARTIAL_A)
            b = make_insert(kind, unit, *PARTIAL_B)
            first, second = race(world, [[(a[0], a[1])], [(b[0], b[1])]])
            assert [first.committed, second.committed].count(True) == 1, (first, second)
            loser = second if first.committed else first
            assert loser.sqlstate == "23P01" and loser.constraint == KINDS[kind]["exclusion"]
            winners += 1
            assert count_rows(observer, kind, unit, only_occupying=True) == 1
        assert winners == 12
        assert overlapping_pairs(observer, kind, units) == 0

    def test_identical_window_is_stopped_by_one_of_the_slot_guards(self, world, observer, kind):
        unit = new_unit(world)
        a = make_insert(kind, unit, *PARTIAL_A)
        b = make_insert(kind, unit, *PARTIAL_A)
        outcomes = race(world, [[(a[0], a[1])], [(b[0], b[1])]])
        assert sorted(o.committed for o in outcomes) == [False, True]
        loser = next(o for o in outcomes if not o.committed)
        assert loser.sqlstate in {"23505", "23P01"}            # unique slot index or exclusion, whichever wins the race
        assert count_rows(observer, kind, unit, only_occupying=True) == 1
        assert overlapping_pairs(observer, kind, [unit]) == 0

    def test_different_units_on_the_same_dates_both_commit(self, world, observer, kind):
        unit_1, unit_2 = new_unit(world), new_unit(world)
        a = make_insert(kind, unit_1, *PARTIAL_A)
        b = make_insert(kind, unit_2, *PARTIAL_A)
        outcomes = race(world, [[(a[0], a[1])], [(b[0], b[1])]])
        assert all(o.committed for o in outcomes), outcomes
        assert overlapping_pairs(observer, kind, [unit_1, unit_2]) == 0

    def test_back_to_back_stays_do_not_overlap_and_do_not_wait(self, world, observer, kind):
        """[1,3) then [3,5): half-open ranges. B must finish while A's transaction is still open."""
        psycopg = _driver()
        unit = new_unit(world)
        first = make_insert(kind, unit, "2027-04-01", "2027-04-03")
        second = make_insert(kind, unit, "2027-04-03", "2027-04-05")
        conn_a = psycopg.connect(world, connect_timeout=10)
        try:
            conn_a.execute(first[0], first[1])                 # A still uncommitted
            writer_b = Attempt(world, [(second[0], second[1])])
            writer_b.start()
            (outcome_b,) = finish(writer_b, timeout=10)        # would hang/timeout if B waited on A
            assert outcome_b.committed, outcome_b
            conn_a.commit()
        finally:
            conn_a.close()
        assert count_rows(observer, kind, unit, only_occupying=True) == 2
        assert overlapping_pairs(observer, kind, [unit]) == 0


class TestIdempotencyAndRetry:
    def test_same_idempotency_key_never_yields_two_rows(self, world, observer, kind):
        unit_1, unit_2 = new_unit(world), new_unit(world)
        key = "idem_cc_" + secrets.token_hex(4)
        a = make_insert(kind, unit_1, "2027-05-01", "2027-05-03", idem=key)
        b = make_insert(kind, unit_2, "2027-06-01", "2027-06-03", idem=key)  # no date conflict: only the key can stop it
        outcomes = race(world, [[(a[0], a[1])], [(b[0], b[1])]])
        assert sorted(o.committed for o in outcomes) == [False, True]
        loser = next(o for o in outcomes if not o.committed)
        assert loser.sqlstate == "23505" and loser.constraint == KINDS[kind]["idem"]
        table = KINDS[kind]["table"]
        assert observer.execute(f"SELECT count(*) FROM {table} WHERE idempotency_key = %s", (key,)).fetchone()[0] == 1

    def test_lock_timeout_then_retry_never_duplicates_a_commit(self, world, observer, kind):
        psycopg = _driver()
        unit = new_unit(world)
        a = make_insert(kind, unit, *PARTIAL_A)
        b = make_insert(kind, unit, *PARTIAL_B)
        conn_a = psycopg.connect(world, connect_timeout=10)
        try:
            conn_a.execute(a[0], a[1])
            timed_out = Attempt(world, [(b[0], b[1])], lock_timeout_ms=300)
            timed_out.start()
            (first_try,) = finish(timed_out)
            assert (first_try.committed, first_try.sqlstate) == (False, "55P03")   # lock_not_available
            conn_a.commit()
        finally:
            conn_a.close()
        retry = Attempt(world, [(b[0], b[1])])
        retry.start()
        (second_try,) = finish(retry)
        assert (second_try.committed, second_try.sqlstate) == (False, "23P01")     # the slot is genuinely taken
        assert count_rows(observer, kind, unit, only_occupying=True) == 1

    def test_retry_after_a_rollback_commits_exactly_once(self, world, observer, kind):
        psycopg = _driver()
        unit = new_unit(world)
        a = make_insert(kind, unit, *PARTIAL_A)
        b = make_insert(kind, unit, *PARTIAL_B)
        conn_a = psycopg.connect(world, connect_timeout=10)
        try:
            conn_a.execute(a[0], a[1])
            writer_b = Attempt(world, [(b[0], b[1])])
            writer_b.start()
            assert wait_until_blocked(observer, writer_b)
            conn_a.rollback()                                  # A gives up: B must now succeed
        finally:
            conn_a.close()
        (outcome_b,) = finish(writer_b)
        assert outcome_b.committed
        # The client lost B's acknowledgement and retries the very same request:
        replay = Attempt(world, [(b[0], b[1])])
        replay.start()
        (replayed,) = finish(replay)
        assert (replayed.committed, replayed.sqlstate) == (False, "23505")          # same key / same id
        assert count_rows(observer, kind, unit, only_occupying=True) == 1


class TestRollbackAndInterruption:
    def test_a_failed_second_statement_leaves_no_partial_row(self, world, observer, kind):
        unit = new_unit(world)
        a = make_insert(kind, unit, *PARTIAL_A)
        b = make_insert(kind, unit, *PARTIAL_B)
        writer = Attempt(world, [(a[0], a[1]), (b[0], b[1])])  # one transaction: A then conflicting B
        writer.start()
        (outcome,) = finish(writer)
        assert (outcome.committed, outcome.sqlstate) == (False, "23P01")
        assert count_rows(observer, kind, unit) == 0           # not even A survived

    def test_a_killed_session_rolls_back_and_frees_the_slot(self, world, observer, kind):
        """Connection interruption on a session this harness owns (same db, same role, not ours-to-guess)."""
        psycopg = _driver()
        unit = new_unit(world)
        a = make_insert(kind, unit, *PARTIAL_A)
        b = make_insert(kind, unit, *PARTIAL_B)
        victim = psycopg.connect(world, connect_timeout=10)
        try:
            victim.execute(a[0], a[1])
            pid = victim.info.backend_pid
            killed = observer.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE pid = %s AND datname = current_database() AND usename = current_user"
                " AND pid <> pg_backend_pid()", (pid,),
            ).fetchone()
            assert killed and killed[0] is True
            with pytest.raises(psycopg.OperationalError):
                victim.commit()
        finally:
            victim.close()
        assert count_rows(observer, kind, unit) == 0
        survivor = Attempt(world, [(b[0], b[1])])
        survivor.start()
        (outcome,) = finish(survivor)
        assert outcome.committed
        assert overlapping_pairs(observer, kind, [unit]) == 0


class TestConcurrentStatusChange:
    def test_update_into_an_occupying_state_still_obeys_the_exclusion(self, world, observer, kind):
        """Two rows overlap but are not occupying yet, so both exist legally. Promoting both at
        once must let exactly one through."""
        spec = KINDS[kind]
        psycopg = _driver()
        for mode in ("deterministic", "barrier"):
            unit = new_unit(world)
            rows = []
            for window in (PARTIAL_A, PARTIAL_B):
                sql, params, row_id = make_insert(kind, unit, *window, status=spec["free"])
                with psycopg.connect(world, autocommit=True, connect_timeout=10) as setup:
                    setup.execute(sql, params)
                rows.append(row_id)
            promote = [(f"UPDATE {spec['table']} SET status = %s WHERE {spec['pk']} = %s", (spec["live"], rid))
                       for rid in rows]
            assert overlapping_pairs(observer, kind, [unit]) == 0   # free rows do not count
            if mode == "deterministic":
                conn_a = psycopg.connect(world, connect_timeout=10)
                try:
                    conn_a.execute(*promote[0])
                    writer_b = Attempt(world, [promote[1]])
                    writer_b.start()
                    assert wait_until_blocked(observer, writer_b), "second UPDATE never waited on the first"
                    conn_a.commit()
                finally:
                    conn_a.close()
                (outcome_b,) = finish(writer_b)
                assert (outcome_b.committed, outcome_b.sqlstate) == (False, "23P01")
            else:
                first, second = race(world, [[promote[0]], [promote[1]]])
                assert [first.committed, second.committed].count(True) == 1
                loser = second if first.committed else first
                assert loser.sqlstate == "23P01"
            assert count_rows(observer, kind, unit, only_occupying=True) == 1
            assert overlapping_pairs(observer, kind, [unit]) == 0


class TestNegativeControl:
    def test_the_harness_detects_a_weakened_constraint_only_in_its_own_database(self, world, observer, kind):
        """Positive vs negative control. The weakened database is a second harness-owned
        database; the one under test (``world``) must be untouched afterwards."""
        spec = KINDS[kind]
        psycopg = _driver()
        with disposable_database(os.environ["VILLA_PG_DSN"]) as weak_dsn:
            apply_twin(weak_dsn)
            seed_world(weak_dsn)
            with psycopg.connect(weak_dsn, autocommit=True, connect_timeout=10) as weak:
                weak.execute(f"ALTER TABLE {spec['table']} DROP CONSTRAINT {spec['exclusion']}")
                unit = new_unit(weak_dsn)
                a = make_insert(kind, unit, *PARTIAL_A)
                b = make_insert(kind, unit, *PARTIAL_B)
                outcomes = race(weak_dsn, [[(a[0], a[1])], [(b[0], b[1])]])
                assert all(o.committed for o in outcomes), "weakened database should have accepted both"
                assert overlapping_pairs(weak, kind, [unit]) == 1, "the invariant check failed to see the overlap"
            matched = re.search(r"dbname=(\S+)", weak_dsn)
            assert matched is not None
            weak_name = matched.group(1)
        # nothing left behind, and the intact database still carries the constraint
        assert observer.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (weak_name,)).fetchone()[0] == 0
        assert observer.execute(
            "SELECT count(*) FROM pg_constraint WHERE conname = %s", (spec["exclusion"],)).fetchone()[0] == 1
        assert weak_name not in _CREATED  # the registry was drained by the context manager


class TestKnownGapCrossTable:
    @pytest.mark.xfail(strict=True, raises=pytest.fail.Exception, reason=(
        "UNRESOLVED RISK for ADR / kernel pack: holds and bookings carry separate exclusion "
        "constraints, so the database does not stop an occupying hold and an occupying booking "
        "from overlapping on one unit. Not a PASS; not claimed as verified."))
    def test_hold_and_booking_on_one_unit_are_rejected_across_tables(self, world, observer):
        unit = new_unit(world)
        psycopg = _driver()
        hold = make_insert("hold", unit, *PARTIAL_A)
        booking = make_insert("booking", unit, *PARTIAL_B)
        with psycopg.connect(world, autocommit=True, connect_timeout=10) as conn:
            conn.execute(hold[0], hold[1])
            with pytest.raises(psycopg.errors.Error):
                conn.execute(booking[0], booking[1])
