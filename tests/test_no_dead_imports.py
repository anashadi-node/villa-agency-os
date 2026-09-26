"""No module may import a name it never uses (ADR-0024).

A dead import is not harmless. It keeps a dependency edge alive that no longer exists
in the code, it hides the fact that a rule moved to another module, and it tells the
next reader that this module still owns a capability it no longer touches. The
ADR-0024 refactor left three of them behind in ``reservation_kernel`` — ``AiService``,
``PolicyResolver`` and ``is_machine_actor`` — and ``make check`` did not notice,
because the gate runs no static analysis. This guard closes that hole with the standard
library instead of a new dependency.

It is deliberately conservative: a name counts as used when it appears as an
identifier anywhere in the module (including annotations), in ``__all__``, or as a
string literal that is exactly an identifier — which covers ``getattr``/``setattr`` and
type comments. An import that is genuinely only a re-export can opt out with
``# noqa: F401`` on its own line, so the escape hatch is explicit and greppable.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NOQA = re.compile(r"#\s*noqa(?::\s*F401)?")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _targets() -> list[Path]:
    return sorted((REPO_ROOT / "src").glob("*.py")) + sorted((REPO_ROOT / "scripts").glob("*.py"))


def _imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Every name an import statement binds, with the line it is bound on."""
    bound: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.append((alias.asname or alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                bound.append((alias.asname or alias.name, node.lineno))
    return bound


def _used_names(tree: ast.AST) -> set[str]:
    """Every name the module mentions as an identifier, a global, or a bare string."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            used.update(node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _IDENTIFIER.match(node.value):
                used.add(node.value)
    return used


def test_no_module_imports_a_name_it_never_uses():
    offenders: list[str] = []
    for path in _targets():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))
        used = _used_names(tree)
        for name, lineno in _imported_names(tree):
            if name in used:
                continue
            if NOQA.search(lines[lineno - 1]):
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} imports {name!r} and never uses it")

    assert not offenders, "dead imports found:\n  " + "\n  ".join(offenders)
