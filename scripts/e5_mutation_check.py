"""E5 mutation evidence: each mutant must make the named test fail, and be restored byte-perfect."""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path.home() / "villa-agency-os"
PY = "/usr/bin/python3"

MUTANTS = [
    {
        "name": "M1 the confirmation gate stops asking for an attributed answer",
        "file": "src/reservation_kernel.py",
        "old": "            if self._assertion_enforced(conn):\n                self._require_attributed_evidence(",
        "new": "            if False:  # MUTANT M1\n                self._require_attributed_evidence(",
        "tests": [
            "tests/test_supplier_assertion.py::TestConfirmationIsEnforced::"
            "test_generic_availability_cannot_become_a_proof_once_enforced",
            "tests/test_supplier_assertion.py::TestConfirmationIsEnforced::"
            "test_legacy_unattributed_evidence_cannot_authorise_a_new_confirmation",
        ],
    },
    {
        "name": "M2 the role-binding row is no longer required",
        "file": "src/supplier_assertion.py",
        "old": "        if bound is None:",
        "new": "        if False and bound is None:  # MUTANT M2",
        "tests": [
            "tests/test_supplier_assertion.py::TestRoleBindingAndStepUp::"
            "test_a_role_held_by_someone_else_is_not_mine",
            "tests/test_supplier_assertion.py::TestRoleBindingAndStepUp::"
            "test_a_deactivated_holder_loses_authority_at_once",
        ],
    },
    {
        "name": "M3 stale supplier confirmations are accepted (comparison inverted)",
        "file": "src/supplier_assertion.py",
        "old": '            if now_moment - parse_iso(row["supplier_confirmed_at"]) > self._assertion_max_age(conn):',
        "new": '            if now_moment - parse_iso(row["supplier_confirmed_at"]) < self._assertion_max_age(conn):  # MUTANT M3',
        "tests": [
            "tests/test_supplier_assertion.py::TestConfirmationIsEnforced::"
            "test_the_supplier_confirmation_is_rechecked_for_age",
        ],
    },
    {
        "name": "M4 a suspended contact reads as verified",
        "file": "src/supplier_assertion.py",
        "old": '        return ("SUSPENDED" if row["kind"] == "SUSPENDED" else "VERIFIED"), str(row["event_id"])',
        "new": '        return "VERIFIED", str(row["event_id"])  # MUTANT M4',
        "tests": [
            "tests/test_supplier_assertion.py::TestAssertionContent::test_a_suspended_contact_is_refused",
            "tests/test_supplier_assertion.py::TestConfirmationIsEnforced::"
            "test_the_contact_is_rechecked_at_confirmation",
        ],
    },
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(tests) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", "-q", "--no-header", "-p", "no:randomly", *tests],
        cwd=REPO, capture_output=True, text=True,
    )
    tail = [line for line in proc.stdout.strip().splitlines() if line.strip()][-1] if proc.stdout else ""
    return proc.returncode, tail


def main() -> int:
    failures = 0
    for mutant in MUTANTS:
        path = REPO / mutant["file"]
        backup = Path("/tmp") / (path.name + ".e5-backup")
        shutil.copy2(path, backup)
        original = sha(path)
        source = path.read_text(encoding="utf-8")
        if source.count(mutant["old"]) != 1:
            print(f"SKIP {mutant['name']}: target string appears {source.count(mutant['old'])}x")
            failures += 1
            continue
        try:
            path.write_text(source.replace(mutant["old"], mutant["new"]), encoding="utf-8")
            rc_mutant, summary_mutant = run(mutant["tests"])
        finally:
            shutil.copy2(backup, path)
            backup.unlink()
        restored = sha(path)
        rc_clean, summary_clean = run(mutant["tests"])
        verdict = (
            "KILLED" if rc_mutant != 0 and restored == original and rc_clean == 0 else "SURVIVED"
        )
        if verdict != "KILLED":
            failures += 1
        print(f"[{verdict}] {mutant['name']}")
        print(f"    mutant   rc={rc_mutant} :: {summary_mutant}")
        print(f"    restored sha match={restored == original} rc={rc_clean} :: {summary_clean}")
    print("MUTATION_SUITE", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
