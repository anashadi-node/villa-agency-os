"""Dependency versions live in exactly one file, and CI reads that file.

Why this exists: the gate used to install its tools with an unpinned
``pip install --upgrade pytest pyflakes markdown`` while ``requirements-dev.txt`` pinned older
versions. Both were green, so nothing looked broken — but "green" meant two different toolchains,
and Dependabot opened a PR bumping a pin that CI never used (markdown 3.5.2 → 3.8.1 while CI was
already running 3.11.3). A gate whose toolchain is not recorded cannot be reproduced.

These tests fail when that drift returns, not when a version changes: bumping a pin in the manifest
is always allowed and needs no test edit.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = REPO / "requirements-dev.txt"
WORKFLOW = REPO / ".github" / "workflows" / "check.yml"

# `name`, `name[extra]`, optionally `; env_marker` — then `==version` (a pin) or `==version.*`
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)(\[[^\]]+\])?\s*==\s*(?P<version>[0-9][^\s;#]*)$")
_PACKAGE_WORDS = ("pytest", "pyflakes", "markdown")


def _manifest_entries() -> list[tuple[int, str]]:
    """(line number, text) for every non-comment, non-blank line in the manifest."""
    entries: list[tuple[int, str]] = []
    for number, raw in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), start=1):
        text = raw.split("#", 1)[0].strip()
        if text:
            entries.append((number, text))
    return entries


def test_the_manifest_exists_and_pins_every_entry():
    assert MANIFEST.is_file(), "requirements-dev.txt is the single source of tool versions"
    entries = _manifest_entries()
    assert entries, "requirements-dev.txt declares nothing"
    loose = [f"line {n}: {text}" for n, text in entries if not _PIN.match(text)]
    assert loose == [], f"every entry must be an exact `==` pin, not a range: {loose}"


def test_the_manifest_covers_every_tool_the_gate_uses():
    names = set()
    for _, text in _manifest_entries():
        match = _PIN.match(text)
        if match:
            names.add(match.group("name").lower())
    missing = [tool for tool in _PACKAGE_WORDS if tool not in names]
    assert missing == [], f"the gate uses these tools but the manifest does not pin them: {missing}"


def test_ci_installs_the_manifest_and_names_no_package_itself():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "requirements-dev.txt" in workflow, (
        "CI must install its tools from requirements-dev.txt; an inline package list makes the "
        "gate's toolchain unrecorded"
    )
    # Every `pip install ...` line must either upgrade pip itself or read the manifest.
    offenders = []
    for line in workflow.splitlines():
        stripped = line.strip()
        if "pip install" not in stripped or stripped.startswith("#"):
            continue
        if "-r requirements-dev.txt" in stripped or stripped.endswith("--upgrade pip"):
            continue
        offenders.append(stripped)
    assert offenders == [], f"CI installs packages outside the manifest: {offenders}"


def test_ci_proves_the_postgresql_twin_against_a_real_server():
    """The twin must be applied to a server in CI, not merely diffed as text.

    Measured before this guard existed: a clean checkout reported ``71 passed, 4 skipped``
    and CI reported green, so nothing in the pipeline ever proved that
    ``001_initial.pg.sql`` applies to PostgreSQL. ``tests/test_schema.py`` skips those four
    tests when ``VILLA_PG_DSN`` is unset — which is exactly CI's default state. The job
    named here must keep its service container and must keep fail-closed on.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    jobs = re.split(r"^  (?=[a-z0-9_-]+:\s*$)", workflow, flags=re.MULTILINE)
    parity = [job for job in jobs if "postgres" in job and "VILLA_PG_REQUIRED" in job]
    assert parity, (
        "CI has no job that runs the PostgreSQL twin against a real server; without one the "
        "live-parity tests skip and the gate is green over an unproven schema"
    )
    job = parity[0]
    assert "services:" in job, "the parity job must declare a PostgreSQL service container"
    assert "image: postgres:" in job, "the parity job must pin a PostgreSQL image tag"
    assert 'VILLA_PG_REQUIRED: "1"' in job, (
        "VILLA_PG_REQUIRED must be 1 so a missing server fails the job instead of skipping it"
    )
    assert "VILLA_PG_DSN" in job, "the parity job must hand the tests a DSN"
    assert "tests/test_schema.py" in job, "the parity job must run the live-parity tests"


def test_parity_job_runs_the_concurrency_proof_with_a_least_privilege_role():
    """PACK-04A: the destructive PostgreSQL tests may only run as a throw-away, non-superuser role.

    The DSN is no longer a literal in the workflow (it used to hand the tests the
    ``postgres`` superuser and a database they then dropped and recreated). It is minted
    per run, and the run token is what the test-side guard checks. Losing any of these
    lines silently turns the destructive tests back into "trust the environment".
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    jobs = re.split(r"^  (?=[a-z0-9_-]+:\s*$)", workflow, flags=re.MULTILINE)
    job = next(j for j in jobs if "postgres" in j and "VILLA_PG_REQUIRED" in j)
    assert "tests/test_pg_overlap_concurrency.py" in job, "the concurrency proof must run in CI"
    assert not re.search(r"^\s+VILLA_PG_DSN:\s*postgres", job, flags=re.MULTILINE), (
        "VILLA_PG_DSN must not be a literal superuser DSN in the job env"
    )
    assert "NOSUPERUSER" in job and "CREATEDB" in job, "tests must run as a least-privilege role"
    assert "GITHUB_ENV" in job and "VILLA_PG_DISPOSABLE_RUN=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in job, (
        "the per-run disposable token must be exported by the job, not hard-coded"
    )
    assert "add-mask" in job, "the generated role password must be masked in the log"
    assert re.search(r"grep -Eq .*skipped.*pg-parity\.log", job), (
        "a skipped PostgreSQL test must fail the job, not pass it"
    )


#: Modules the parity job must actually hand to pytest. Naming a file in a comment does not
#: count: the check reads the arguments of the `python -m pytest` command, because coverage
#: that exists only in prose is the exact failure this file exists to catch.
REQUIRED_PARITY_MODULES = (
    "tests/test_pg_overlap_concurrency.py",
    "tests/test_pg_migrations.py",
    "tests/test_pg_cross_table_occupancy.py",
)


def _parity_job(workflow: str) -> str:
    jobs = re.split(r"^  (?=[a-z0-9_-]+:\s*$)", workflow, flags=re.MULTILINE)
    return next(job for job in jobs if "postgres" in job and "VILLA_PG_REQUIRED" in job)


def _pytest_arguments(job: str) -> set[str]:
    """Every argument the job's pytest invocations name, one token per space-separated word."""
    arguments: set[str] = set()
    for line in job.splitlines():
        if "pytest" not in line or line.strip().startswith("#"):
            continue
        tokens = line.replace("\\", " ").replace('"', " ").split()
        start = next((i for i, token in enumerate(tokens) if token.endswith("pytest")), None)
        if start is None:
            continue
        arguments.update(token for token in tokens[start + 1 :] if token.endswith(".py"))
    return arguments


def test_parity_job_executes_every_mandatory_postgresql_module():
    """PACK-002A: 021's cross-table proof was absent from the job while the job looked complete.

    Measured before this guard: ``tests/test_pg_cross_table_occupancy.py`` existed and passed
    locally, but the parity job never named it, so the 26 tests only reached CI through
    ``make check`` in a matrix job with no database — where they skip and nothing complains.
    """
    job = _parity_job(WORKFLOW.read_text(encoding="utf-8"))
    executed = _pytest_arguments(job)
    missing = [module for module in REQUIRED_PARITY_MODULES if module not in executed]
    assert missing == [], (
        "the parity job must execute these PostgreSQL modules, not merely contain the file: "
        f"{missing}"
    )


def test_the_gate_fails_when_a_mandatory_module_is_dropped_from_the_job():
    """The guard has to be able to go red, or it is decoration."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = _parity_job(workflow)
    for module in REQUIRED_PARITY_MODULES:
        doctored = job.replace(module, "")
        executed = _pytest_arguments(doctored)
        assert module not in executed, f"removing {module} still looked like coverage"
