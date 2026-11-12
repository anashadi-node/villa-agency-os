"""ADR citations inside schema comments must point at an ADR that exists (ADR-0068).

A schema comment is documentation that survives every refactor: it is read by whoever
next opens the file, long after the commit that wrote it. `001_initial.sql` said
``(ADR-0061)`` next to the geo columns for weeks after ADR-0068 took that rule over —
a citation that resolves to the wrong decision is worse than no citation, because it
looks authoritative. This test makes that drift fail instead of travelling silently:
every ``ADR-NNNN`` mentioned in ``src/schema/*.sql`` must name a real ADR file, and the
geo columns specifically must cite the geo ADR, not the one it replaced.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO / "src" / "schema"
ADR_DIR = REPO / "docs" / "adr"

CITATION_RE = re.compile(r"ADR-(\d{4})")

#: The decision that owns WHERE the villa is and how much of it the public may see.
GEO_ADR = "0068"
#: The decision ADR-0068 replaced for that subject. Naming it next to a geo column is drift.
STALE_GEO_ADR = "0061"


def _citations(text: str) -> set[str]:
    return set(CITATION_RE.findall(text))


def _unresolved(text: str) -> list[str]:
    """Numbers cited in ``text`` that have no ``docs/adr/NNNN-*.md`` behind them."""
    return sorted(number for number in _citations(text) if not _adr_exists(number))


def _adr_exists(number: str) -> bool:
    return any(ADR_DIR.glob(f"{number}-*.md"))


def _schema_texts() -> dict[str, str]:
    files = sorted(SCHEMA_DIR.glob("*.sql"))
    assert files, f"no schema files under {SCHEMA_DIR}"
    return {path.name: path.read_text(encoding="utf-8") for path in files}


def test_negative_control_unresolved_citation_is_reported() -> None:
    """The scanner must be able to fail: a fabricated ADR number is unresolved."""
    assert _unresolved("-- see ADR-9999 and ADR-0068") == ["9999"]


def test_every_schema_adr_citation_resolves_to_a_file() -> None:
    unresolved = {
        name: _unresolved(text) for name, text in _schema_texts().items() if _unresolved(text)
    }
    assert not unresolved, f"schema cites ADRs with no file in docs/adr: {unresolved}"


def test_geo_columns_cite_the_geo_adr() -> None:
    offenders: dict[str, list[str]] = {}
    for name, text in _schema_texts().items():
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "geo_precision" not in line and "public may see" not in line:
                continue
            if STALE_GEO_ADR in _citations(line):
                offenders.setdefault(name, []).append(f"{line_number}: {line.strip()[:100]}")
    assert not offenders, f"geo columns still cite the superseded ADR-{STALE_GEO_ADR}: {offenders}"

    geo_schema = SCHEMA_DIR / "017_property_geo.sql"
    assert geo_schema.exists(), "017_property_geo.sql is missing"
    assert GEO_ADR in _citations(geo_schema.read_text(encoding="utf-8")), (
        "017_property_geo.sql must cite ADR-0068, the decision that owns the rule"
    )
