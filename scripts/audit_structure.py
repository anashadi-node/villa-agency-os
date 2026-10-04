#!/usr/bin/env python3
"""Whole-tree structural audit for the villa platform: the checks `make check` cannot see.

`make check` proves the tree is *runnable* (syntax, lint, tests, secret/unsafe scans, PG twin
parity). It cannot see whether the tree is *tidy*: whether a newcomer can find the test for a
module, whether a documented command still exists, whether a dashboard read forgot its tenant
filter, or whether a money aggregate mixes currencies.

This script is read-only. It never opens a database, never writes outside stdout, and takes no
arguments other than the optional ``--json`` sink.

    python3 scripts/audit_structure.py
    python3 scripts/audit_structure.py --json /tmp/audit.json

Exit code is 0 when nothing FAILED, 1 when at least one check failed. WARN and INFO never
affect the exit code — they are things a human decides on.

The failure levels are deliberately different from each other:

* **FAIL** — a claim the tree makes about itself that is not true (a documented command that
  does not exist, a tenant-scoped read without its tenant filter, a wall-clock call in a test,
  an underscore numeric literal in SQL that only one interpreter's SQLite accepts).
* **WARN** — debt that is already recorded and ratcheted (Indonesian strings, TODO markers,
  a script nothing references).
* **INFO** — counts worth quoting in a report.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCHEMA_SQLITE = REPO / "src" / "schema" / "001_initial.sql"

DOC_SOURCES = ("README.md", "Makefile")
DOC_DIRS = ("docs", "reports")

READ_HELPERS = {"_rows", "_count", "_one", "_scalar", "_fetch"}


@dataclass
class Report:
    """Collected findings, grouped by check id."""

    results: list[dict[str, str]] = field(default_factory=list)

    def add(self, level: str, check: str, detail: str) -> None:
        self.results.append({"level": level, "check": check, "detail": detail})

    def failed(self) -> bool:
        return any(item["level"] == "FAIL" for item in self.results)

    def counts(self) -> dict[str, int]:
        out = {"FAIL": 0, "WARN": 0, "INFO": 0}
        for item in self.results:
            out[item["level"]] = out.get(item["level"], 0) + 1
        return out


def _python_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.py") if p.name != "__init__.py")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _doc_files() -> list[Path]:
    files = [REPO / name for name in DOC_SOURCES if (REPO / name).is_file()]
    for name in DOC_DIRS:
        directory = REPO / name
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.md")))
    return files


# --------------------------------------------------------------------------------------- checks


def check_module_test_map(report: Report) -> None:
    """Every src/ module should be reachable from a test module.

    Traceability is by *import*, not by filename: a module covered inside a differently named test
    file is covered, and saying otherwise would be a false accusation.
    """
    test_files = [p for p in sorted((REPO / "tests").rglob("*.py")) if p.name != "__init__.py"]
    corpus = "\n".join(_read(path) for path in test_files)
    orphans = []
    for module in _python_files(REPO / "src"):
        pattern = re.compile(rf"(?:from|import)\s+(?:\w+\.)?{re.escape(module.stem)}\b")
        if not pattern.search(corpus):
            orphans.append(module.stem)
    if orphans:
        report.add(
            "WARN",
            "module-test-map",
            f"{len(orphans)} modul tidak pernah diimpor satu tes pun: {', '.join(orphans)}",
        )
    else:
        report.add(
            "INFO",
            "module-test-map",
            f"setiap modul src/ diimpor oleh minimal satu berkas tes ({len(test_files)} berkas tes)",
        )


def check_scripts_are_referenced(report: Report) -> None:
    """A script nobody's documentation mentions is either dead or undocumented."""
    corpus = "\n".join(_read(path) for path in _doc_files())
    makefile = _read(REPO / "Makefile") if (REPO / "Makefile").is_file() else ""
    corpus += "\n" + makefile
    unreferenced = []
    for script in sorted((REPO / "scripts").glob("*")):
        if script.is_dir() or script.name.startswith("__"):
            continue
        if script.name not in corpus:
            unreferenced.append(script.name)
    if unreferenced:
        report.add(
            "WARN",
            "scripts-referenced",
            f"{len(unreferenced)} skrip tidak disebut di docs/ atau Makefile: {', '.join(unreferenced)}",
        )
    else:
        report.add("INFO", "scripts-referenced", "setiap skrip disebut di docs/ atau Makefile")


def check_documented_commands_exist(report: Report) -> None:
    """Detect documentation rot: a command naming a file or Make target that is gone."""
    missing = []
    make_targets = set(re.findall(r"^([a-z][a-z0-9-]*):", _read(REPO / "Makefile"), re.M))
    for path in _doc_files():
        text = _read(path)
        for hit in re.findall(r"(?:python3|python)\s+(scripts/[\w./-]+\.(?:py|sh))", text):
            if not (REPO / hit).is_file():
                missing.append(f"{path.relative_to(REPO)} → {hit}")
        for hit in re.findall(r"(?:python3|python)\s+(src/[\w./-]+\.py)", text):
            if not (REPO / hit).is_file():
                missing.append(f"{path.relative_to(REPO)} → {hit}")
        for hit in re.findall(r"^make\s+([a-z][a-z0-9-]*)", text, re.M):
            if hit not in make_targets:
                missing.append(f"{path.relative_to(REPO)} → make {hit}")
    if missing:
        report.add("FAIL", "documented-commands", "; ".join(sorted(set(missing))))
    else:
        report.add(
            "INFO",
            "documented-commands",
            f"setiap perintah di dokumentasi menunjuk berkas/target yang ada ({len(make_targets)} target make)",
        )


def check_module_docstrings(report: Report) -> None:
    """A module with no docstring is a module whose purpose lives only in someone's head."""
    silent = []
    for module in _python_files(REPO / "src"):
        tree = ast.parse(_read(module))
        if not ast.get_docstring(tree):
            silent.append(module.name)
    if silent:
        report.add("FAIL", "module-docstring", f"modul tanpa docstring: {', '.join(silent)}")
    else:
        report.add("INFO", "module-docstring", "semua modul src/ punya docstring")


def _docstring_lines(tree: ast.AST) -> set[int]:
    """Line numbers occupied by module/class/function docstrings."""
    covered: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                start = first.lineno
                covered.update(range(start, getattr(first, "end_lineno", start) + 1))
    return covered


def check_todo_markers(report: Report) -> None:
    """A marker in a comment is a promise; the same word inside a docstring is prose.

    Scanning raw lines would call a malformed test value such as ``XXX-2026-00001`` a marker and
    would flag this very docstring. Comments come from the tokenizer; strings are read through the
    AST so docstrings can be skipped, and a string only counts when it is written like a marker
    (``TODO:`` / ``FIXME -``).
    """
    in_string = re.compile(r"\b(TODO|FIXME)\b\s*[:\-]")
    marker = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
    hits = 0
    for directory, level in (("src", "FAIL"), ("scripts", "WARN"), ("tests", "WARN")):
        for path in sorted((REPO / directory).rglob("*.py")):
            source = _read(path)
            tree = ast.parse(source)
            doc_lines = _docstring_lines(tree)
            comments: list[tuple[int, str]] = []
            try:
                with path.open("rb") as handle:
                    comments = [
                        (token.start[0], token.string)
                        for token in tokenize.tokenize(handle.readline)
                        if token.type == tokenize.COMMENT
                    ]
            except (tokenize.TokenError, SyntaxError, IndentationError):
                comments = []
            for number, text in comments:
                found = marker.search(text)
                if found:
                    hits += 1
                    report.add(level, "todo-markers", f"{path.relative_to(REPO)}:{number} ({found.group(0)})")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if node.lineno in doc_lines:
                    continue
                found = in_string.search(node.value)
                if found:
                    hits += 1
                    report.add(level, "todo-markers", f"{path.relative_to(REPO)}:{node.lineno} ({found.group(0)})")
    if not hits:
        report.add("INFO", "todo-markers", "tidak ada TODO/FIXME/XXX/HACK di src/, scripts/, tests/")


def check_swallowed_errors(report: Report) -> None:
    """A bare except, or `except Exception: pass`, is an error that leaves no trace."""
    bad = []
    for path in sorted((REPO / "src").rglob("*.py")):
        lines = _read(path).splitlines()
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped in ("except:", "except Exception:"):
                following = lines[number] if number < len(lines) else ""
                if following.strip() == "pass":
                    bad.append(f"{path.relative_to(REPO)}:{number}")
            elif re.fullmatch(r"except\s*:", stripped):
                bad.append(f"{path.relative_to(REPO)}:{number}")
    if bad:
        report.add("FAIL", "swallowed-errors", ", ".join(bad))
    else:
        report.add("INFO", "swallowed-errors", "tidak ada `except:` telanjang / `except: pass` di src/")


PATTERN_DEFINING = (
    "scripts/unsafe_config_scan.sh",
    "scripts/secret_scan.sh",
    "scripts/audit_structure.py",
)


def check_shuffled_ledgers(report: Report) -> None:
    """Time-series safety: nothing in this tree may shuffle an ordered series.

    The check reads the AST for Python (a keyword argument really named ``shuffle`` set to True),
    so prose in a docstring or a scanner's own pattern cannot trip it, and falls back to a line
    scan for `.sh`/`.sql` — skipping the files whose job is to *name* the forbidden pattern.
    """
    hits = []
    for directory in ("src", "scripts", "tests"):
        for path in sorted((REPO / directory).rglob("*")):
            relative = str(path.relative_to(REPO))
            if path.suffix == ".py":
                for node in ast.walk(ast.parse(_read(path))):
                    if isinstance(node, ast.keyword) and node.arg == "shuffle":
                        if isinstance(node.value, ast.Constant) and node.value.value is True:
                            hits.append(f"{relative}:{node.lineno}")
                    elif isinstance(node, ast.Dict):
                        for key, value in zip(node.keys, node.values):
                            if isinstance(key, ast.Constant) and key.value == "shuffle":
                                if isinstance(value, ast.Constant) and value.value is True:
                                    hits.append(f"{relative}:{node.lineno}")
            elif path.suffix in (".sh", ".sql") and relative not in PATTERN_DEFINING:
                for number, line in enumerate(_read(path).splitlines(), start=1):
                    if re.search(r"shuffle\s*[=:]\s*(True|true)", line):
                        hits.append(f"{relative}:{number}")
    if hits:
        report.add("FAIL", "no-shuffle", ", ".join(sorted(set(hits))))
    else:
        report.add("INFO", "no-shuffle", "tidak ada shuffle=True di seluruh pohon")


def _page_names() -> list[str]:
    """Page names straight from the module's own registry, not from a copied route table.

    The registry maps a page name to the roles entitled to it (a tuple, or a shared constant), so it
    is the module's own answer to "which pages exist" — including pages routed by a template
    pattern such as ``case_detail``. The dict may sit behind a mapping proxy, so the search walks
    the whole tree instead of reading a single assignment.
    """
    tree = ast.parse(_read(REPO / "src" / "dashboard.py"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if len(keys) < 5 or len(keys) != len(node.values):
            continue
        if all(isinstance(value, (ast.Tuple, ast.Name)) for value in node.values):
            return [str(key) for key in keys]
    return []


def check_dashboard_templates(report: Report) -> None:
    """Every routable page needs a template; every template should be reachable."""
    names = _page_names()
    if not names:
        report.add("WARN", "dashboard-templates", "registry halaman tidak ditemukan: pemeriksaan dilewati")
        return
    templates = {p.stem for p in (REPO / "templates").glob("*.html")}
    missing = sorted(set(names) - templates)
    unreachable = sorted(templates - set(names) - {"base", "error"})
    if missing:
        report.add("FAIL", "dashboard-templates", f"halaman tanpa template: {', '.join(missing)}")
    else:
        report.add(
            "INFO",
            "dashboard-templates",
            f"{len(names)} halaman terdaftar, semuanya punya template ({len(templates)} berkas)",
        )
    if unreachable:
        report.add("WARN", "dashboard-templates", f"template tidak terpakai: {', '.join(unreachable)}")


def agency_scoped_tables() -> set[str]:
    """Tables that carry agency_id in the SQLite DDL — a read of one must filter by tenant."""
    if not SCHEMA_SQLITE.is_file():
        return set()
    text = _read(SCHEMA_SQLITE)
    scoped = set()
    for match in re.finditer(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\)\s*;", text, re.S):
        if re.search(r"^\s*agency_id\b", match.group(2), re.M):
            scoped.add(match.group(1))
    return scoped


def _sql_from_node(node: ast.AST) -> str | None:
    """Join an implicitly-concatenated SQL literal, rendering f-string holes as `?`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("?")
        return "".join(parts)
    return None


def check_tenant_scoped_reads(report: Report) -> None:
    """A read of a tenant table that forgets its filter is a cross-tenant read."""
    scoped = agency_scoped_tables()
    if not scoped:
        report.add("WARN", "tenant-scoped-reads", "DDL tidak terbaca: pemeriksaan dilewati")
        return
    findings = []
    for path in sorted((REPO / "src").rglob("*.py")):
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in READ_HELPERS:
                continue
            if not node.args:
                continue
            sql = _sql_from_node(node.args[0])
            if not sql:
                continue
            for table in re.findall(r"(?:FROM|JOIN)\s+(\w+)", sql, re.I):
                if table in scoped and "agency_id" not in sql:
                    findings.append(f"{path.relative_to(REPO)}:{node.lineno} → {table}")
    if findings:
        report.add("FAIL", "tenant-scoped-reads", "; ".join(sorted(set(findings))))
    else:
        report.add(
            "INFO",
            "tenant-scoped-reads",
            f"setiap baca tabel ber-agency_id di src/ menyertakan filter tenant ({len(scoped)} tabel)",
        )


PINNED_TO_ONE_ENTITY = re.compile(
    r"\b(?:payment_id|invoice_id|booking_id|resolution_id|case_id|settlement_id|entry_id)\s*=\s*\?"
)


def check_money_aggregates(report: Report) -> None:
    """A SUM over one currency column must say which currency it summed.

    A sum pinned to a single row (``WHERE payment_id = ?``) is safe by construction: one row has one
    currency, so it is not reported. What is reported is an open sum across many rows that neither
    states a currency nor groups by one — the figure a human would end up adding rupiah to dollars.
    """
    findings = []
    for path in sorted((REPO / "src").rglob("*.py")):
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            sql = _sql_from_node(node.args[0]) if node.args else None
            if not sql:
                continue
            if not re.search(r"SUM\s*\(", sql, re.I) or "_minor" not in sql:
                continue
            if "currency" in sql.lower() or "GROUP BY" in sql.upper():
                continue
            if PINNED_TO_ONE_ENTITY.search(sql):
                continue
            findings.append(f"{path.relative_to(REPO)}:{node.lineno}")
    if findings:
        report.add(
            "WARN",
            "money-aggregates",
            "agregat lintas mata uang, perlu ditinjau manusia: " + ", ".join(sorted(findings)),
        )
    else:
        report.add("INFO", "money-aggregates", "setiap SUM(…_minor) menyebut mata uangnya atau dikelompokkan")


def check_wall_clock_in_tests(report: Report) -> None:
    """A test that reads the wall clock expires by itself (or flips at midnight)."""
    hits = []
    for path in sorted((REPO / "tests").rglob("*.py")):
        source = _read(path)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("today", "now", "utcnow"):
                target = node.value
                if isinstance(target, ast.Name) and target.id in ("date", "datetime"):
                    hits.append(f"{path.relative_to(REPO)}:{node.lineno}")
    if hits:
        report.add("FAIL", "wall-clock-in-tests", ", ".join(hits))
    else:
        report.add("INFO", "wall-clock-in-tests", "tidak ada date.today()/datetime.now() di tests/")


def check_sql_numeric_literals(report: Report) -> None:
    """`2_250_000` is only valid from SQLite 3.46 — the deployment interpreter is older."""
    hits = []
    for path in sorted((REPO / "src").rglob("*.sql")):
        for number, line in enumerate(_read(path).splitlines(), start=1):
            for literal in re.findall(r"\b\d+(?:_\d+)+\b", line):
                hits.append(f"{path.relative_to(REPO)}:{number} ({literal})")
    if hits:
        report.add("FAIL", "sql-numeric-literals", ", ".join(hits))
    else:
        report.add("INFO", "sql-numeric-literals", "tidak ada literal angka bergaris bawah di .sql")


def check_external_assets(report: Report) -> None:
    """The demo and the dashboard must work with no internet: no third-party asset URLs."""
    hits = []
    for directory in ("templates", "demo"):
        for path in sorted((REPO / directory).rglob("*.html")):
            for number, line in enumerate(_read(path).splitlines(), start=1):
                for url in re.findall(r"(?:src|href)\s*=\s*[\"'](https?://[^\"']+)", line):
                    hits.append(f"{path.relative_to(REPO)}:{number} ({url[:60]})")
    if hits:
        report.add("WARN", "offline-assets", ", ".join(hits))
    else:
        report.add("INFO", "offline-assets", "templates/ dan demo/ tidak memuat aset pihak ketiga")


def check_language_ratchet(report: Report) -> None:
    """ADR-0011 ratchet: report the current Indonesian-string load so the count cannot grow unseen."""
    ratchet = {}
    for path in sorted((REPO / "src").rglob("*.py")):
        hits = re.findall(r"[\"'][^\"']*[a-z]{2,}[\"']", _read(path))
        count = sum(1 for hit in hits if re.search(r"\b(dan|atau|tidak|sudah|belum|dengan|untuk|hari|kasus)\b", hit))
        if count:
            ratchet[path.name] = count
    total = sum(ratchet.values())
    report.add(
        "INFO",
        "language-ratchet",
        f"{total} string Indonesia di {len(ratchet)} modul src/ (ADR-0011: daftar hanya boleh menyusut)",
    )


CHECKS = (
    check_module_test_map,
    check_scripts_are_referenced,
    check_documented_commands_exist,
    check_module_docstrings,
    check_todo_markers,
    check_swallowed_errors,
    check_shuffled_ledgers,
    check_dashboard_templates,
    check_tenant_scoped_reads,
    check_money_aggregates,
    check_wall_clock_in_tests,
    check_sql_numeric_literals,
    check_external_assets,
    check_language_ratchet,
)


def main(argv: list[str] | None = None) -> int:
    """Run every check, print a human report, optionally dump JSON."""
    parser = argparse.ArgumentParser(description="Struktural audit seluruh pohon villa-agency-os")
    parser.add_argument("--json", dest="json_path", default=None, help="tulis hasil ke berkas JSON")
    args = parser.parse_args(argv)

    report = Report()
    for check in CHECKS:
        check(report)

    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    results = sorted(report.results, key=lambda item: (order[item["level"]], item["check"]))
    for item in results:
        print(f"[{item['level']:4}] {item['check']}: {item['detail']}")

    counts = report.counts()
    print(f"\nRINGKASAN: {counts['FAIL']} FAIL, {counts['WARN']} WARN, {counts['INFO']} INFO")

    if args.json_path:
        payload = {
            "repo": str(REPO),
            "counts": counts,
            "results": results,
            "verdict": "FAIL" if report.failed() else "PASS",
        }
        Path(args.json_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"JSON: {args.json_path}")

    return 1 if report.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
