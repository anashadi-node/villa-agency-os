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


#: PACK-003A: a green parity job used to be all an auditor got. `pytest -q -rs` prints the names
#: of failing tests only, so "the back-to-back proofs passed" was an inference from an aggregate
#: count. The job now writes JUnit XML from the very command that writes the log, and uploads
#: both under the existing artifact name. These checks read the step blocks — never a comment,
#: because a filename in prose is exactly the evidence-free coverage this file exists to catch.
JUNIT_EVIDENCE = "pg-parity.xml"
PARITY_LOG = "pg-parity.log"


def _steps(job: str) -> list[str]:
    """The job's step blocks, each beginning at its ``- name:`` line."""
    return re.split(r"^      - ", job, flags=re.MULTILINE)[1:]


def _junit_argument(job: str) -> str | None:
    """The value a real ``python -m pytest`` command passes to ``--junitxml``, if any."""
    for step in _steps(job):
        for line in step.splitlines():
            command = line.strip()
            if not command.startswith("python -m pytest"):
                continue
            for token in command.split():
                if token.startswith("--junitxml="):
                    return token.split("=", 1)[1]
    return None


def _artifact_paths(job: str) -> list[str]:
    """Paths named by the job's artifact-upload step."""
    for step in _steps(job):
        if "actions/upload-artifact" not in step:
            continue
        lines = step.splitlines()
        for index, line in enumerate(lines):
            if not line.strip().startswith("path:"):
                continue
            inline = line.split("path:", 1)[1].strip()
            if inline and inline != "|":
                return [inline]
            indent = len(line) - len(line.lstrip())
            collected = []
            for follower in lines[index + 1 :]:
                if not follower.strip():
                    continue
                if len(follower) - len(follower.lstrip()) <= indent:
                    break
                collected.append(follower.strip())
            return collected
    return []


def _evidence_is_published(job: str) -> bool:
    upload = next((step for step in _steps(job) if "actions/upload-artifact" in step), "")
    paths = _artifact_paths(job)
    return (
        _junit_argument(job) == JUNIT_EVIDENCE
        and JUNIT_EVIDENCE in paths
        and PARITY_LOG in paths
        and "if: always()" in upload
        and "if-no-files-found: ignore" in upload
    )


def test_parity_job_publishes_per_test_junit_evidence_with_the_log():
    """The XML must come from the SQL-backed run itself, not from a second, friendlier one."""
    job = _parity_job(WORKFLOW.read_text(encoding="utf-8"))
    assert _evidence_is_published(job), (
        "the parity job must write pg-parity.xml with --junitxml and upload it beside pg-parity.log"
    )
    commands = [
        line.strip()
        for step in _steps(job)
        for line in step.splitlines()
        if line.strip().startswith("python -m pytest")
    ]
    assert len(commands) == 1, "one pytest run, so the XML cannot come from a different command"
    assert f"--junitxml={JUNIT_EVIDENCE}" in commands[0]
    assert f"tee {PARITY_LOG}" in commands[0]
    assert "continue-on-error" not in job, "evidence must not be published by a job that may not fail"


def test_parity_job_still_runs_every_mandatory_module_and_refuses_skips():
    """Adding evidence must not cost a test module or the skip refusal."""
    job = _parity_job(WORKFLOW.read_text(encoding="utf-8"))
    executed = _pytest_arguments(job)
    assert set(REQUIRED_PARITY_MODULES) <= executed
    assert "tests/test_schema.py" in executed and "tests/test_workforce_gate_g1.py" in executed
    assert "VILLA_PG_REQUIRED" in job
    assert re.search(r"grep -Eq .*skipped.*pg-parity\.log", job), "the skip refusal must stay"


def test_the_gate_fails_when_the_junit_evidence_is_disconnected():
    """Four ways to lose the evidence; each must turn the check red."""
    job = _parity_job(WORKFLOW.read_text(encoding="utf-8"))
    mutations = {
        "flag dropped": job.replace(f" --junitxml={JUNIT_EVIDENCE}", ""),
        "flag renamed": job.replace(f"--junitxml={JUNIT_EVIDENCE}", "--junitxml=other.xml"),
        "path dropped": job.replace(f"\n            {JUNIT_EVIDENCE}", ""),
        "upload no longer always runs": job.replace(
            "        if: always()\n        uses: actions/upload-artifact",
            "        uses: actions/upload-artifact",
        ),
        "log no longer uploaded": job.replace(f"\n            {PARITY_LOG}", ""),
    }
    for label, doctored in mutations.items():
        assert doctored != job, f"mutation '{label}' did not apply — the guard's anchor drifted"
        assert not _evidence_is_published(doctored), f"'{label}' still looked like published evidence"


#: PACK-003D: the approved fixture is what the runner certifies function bodies against, so it
#: must be shown to be what the approved SQL still produces — on every run, on the same
#: disposable server. These checks read the step block, never a comment: a filename or a command
#: in prose is exactly the evidence-free coverage this file exists to catch.
CAPTURE_SCRIPT = "scripts/capture_guard_definitions.py"
CAPTURE_LOG = "pg-capture.log"
CAPTURE_DSN_VARIABLE = "VILLA_PG_DSN"


def _capture_step(job: str) -> str:
    """The step that *checks* the fixture — not the proof step, which names the same script."""
    return next((step for step in _steps(job) if CAPTURE_SCRIPT in step and "--check" in step), "")


def _prove_step(job: str) -> str:
    return next((s for s in _steps(job) if "--prove-target" in s), "")


def _capture_is_enforced(job: str) -> bool:
    """True only when ownership is proven and the fixture check runs, both mandatory, in order."""
    step = _capture_step(job)
    prove = _prove_step(job)
    if not step or not prove:
        return False
    if "continue-on-error" in prove or "|| true" in prove:
        return False
    if "--check" not in step or f"--dsn-env {CAPTURE_DSN_VARIABLE}" not in step:
        return False
    if "--write" in step or "continue-on-error" in step or "|| true" in step:
        return False
    if "set -o pipefail" not in step or f"tee {CAPTURE_LOG}" not in step:
        return False
    steps = _steps(job)
    pytest_steps = [i for i, block in enumerate(steps) if "python -m pytest" in block]
    ordered = steps.index(prove) < steps.index(step)
    return (
        bool(pytest_steps)
        and ordered
        and steps.index(step) < min(pytest_steps)
        and CAPTURE_LOG in _artifact_paths(job)
    )


def _script_guards_its_target(text: str) -> bool:
    """The capture itself must refuse a non-disposable target before CREATE/DROP."""
    return (
        "def prove_disposable_target(" in text
        and "prove_disposable_target(dsn)" in text
        and "LOOPBACK_HOSTS" in text
        and "prove_disposable_server" in text
    )


def test_parity_job_verifies_the_approved_fixture_before_the_parity_suite():
    job = _parity_job(WORKFLOW.read_text(encoding="utf-8"))
    assert _capture_is_enforced(job), (
        "the parity job must run `capture_guard_definitions.py --check` with the DSN taken from"
        " the environment, mandatory, before pytest: the fixture is what the runner certifies"
        " against, and a capture that only ever runs by hand is not a gate"
    )
    assert "pg-parity.xml" in _artifact_paths(job) and PARITY_LOG in _artifact_paths(job), (
        "the capture step must not disturb the existing JUnit and parity-log evidence"
    )


def test_the_capture_script_proves_its_target_is_disposable():
    text = (REPO / CAPTURE_SCRIPT).read_text(encoding="utf-8")
    assert _script_guards_its_target(text), (
        "the capture creates and drops a database, so it must reuse the disposable-server proof"
        " and refuse a remote host before any DDL"
    )


def test_the_gate_fails_when_the_capture_check_is_removed_made_optional_or_downgraded():
    """Five ways to weaken the gate; each must turn the check red. A mutation that does not
    apply proves the guard's anchor drifted, so that is a failure too."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = _parity_job(workflow)
    step = _capture_step(job)
    assert step, "no capture step to mutate"
    mutations = {
        "step removed": job.replace(step, ""),
        "ownership proof removed": job.replace(_prove_step(job), ""),
        "downgraded to --write": job.replace("--check --dsn-env", "--write --dsn-env"),
        "made optional with continue-on-error": step.replace(
            "name: Verify the approved fixture against the disposable server",
            "name: Verify the approved fixture against the disposable server"
            "\n        continue-on-error: true",
        ),
        "failure swallowed with || true": step.replace(
            "2>&1 | tee pg-capture.log", "2>&1 | tee pg-capture.log || true"
        ),
        "proof dropped from the command": step.replace("set -o pipefail\n", ""),
    }
    for label, doctored in mutations.items():
        assert doctored != step and doctored != job, f"mutation '{label}' did not apply"
        assert not _capture_is_enforced(doctored), f"'{label}' still looked like enforcement"
    # Disconnected: the same block, moved out of the parity job, is not enforcement.
    others = [block for block in re.split(r"^  (?=[a-z0-9_-]+:\s*$)", workflow, flags=re.MULTILINE)
              if block and block != job and "steps:" in block]
    assert others, "no second job to move the step into"
    moved = workflow.replace(step, "").replace(
        others[0], others[0].replace("    steps:", "    steps:\n" + step, 1), 1
    )
    assert step not in _parity_job(moved), "the step was not moved"
    assert not _capture_is_enforced(_parity_job(moved)), "a detached capture step still counted"


def test_the_gate_fails_when_the_capture_stops_proving_a_disposable_target():
    text = (REPO / CAPTURE_SCRIPT).read_text(encoding="utf-8")
    assert _script_guards_its_target(text)
    dropped = text.replace("    prove_disposable_target(dsn)\n", "")
    assert dropped != text, "mutation did not apply — the guard's anchor drifted"
    assert not _script_guards_its_target(dropped), "removing the proof still looked guarded"


def test_TC_003D_15_the_capture_step_leaves_the_junit_evidence_and_modules_intact():
    """TC-003D-15: adding a gate must not cost the evidence it sits next to."""
    job = _parity_job(WORKFLOW.read_text(encoding="utf-8"))
    assert _evidence_is_published(job)
    assert set(REQUIRED_PARITY_MODULES) <= _pytest_arguments(job)
    paths = _artifact_paths(job)
    assert "pg-capture.log" in paths and JUNIT_EVIDENCE in paths and PARITY_LOG in paths
