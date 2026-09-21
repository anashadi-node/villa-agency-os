"""Shared pytest fixtures.

The repository keeps importable modules directly under ``src/`` (matching the
implementation plan file list), so ``src/`` is added to ``sys.path`` here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from primitives import FrozenClock, IdFactory  # noqa: E402
from schema_loader import connect_sqlite, load_statements  # noqa: E402


@pytest.fixture()
def clock() -> FrozenClock:
    """Deterministic clock: 2026-09-21T08:00:00Z."""
    return FrozenClock.at("2026-09-21T08:00:00Z")


@pytest.fixture()
def ids() -> IdFactory:
    """Deterministic identifier factory so assertions stay stable."""
    return IdFactory(seed="test")


@pytest.fixture()
def conn():
    """Fresh in-memory SQLite database with the full schema applied."""
    connection = connect_sqlite(":memory:")
    yield connection
    connection.close()


@pytest.fixture()
def schema_statements() -> list[str]:
    return load_statements("sqlite")
