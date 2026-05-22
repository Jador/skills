"""Pytest fixtures for babysit DB helper tests.

Provides two fixtures:

- ``conn``: an in-memory ``sqlite3.Connection`` with ``schema.sql`` applied
  and ``row_factory`` set to ``sqlite3.Row`` for dict-like access.
- ``coordinator_db``: a file-backed DB path under pytest's ``tmp_path``,
  with schema applied. Use this when tests need WAL behavior or multiple
  simultaneous connections.

These fixtures intentionally depend only on the stdlib ``sqlite3`` module
and the on-disk ``schema.sql`` — they do not import the (not-yet-written)
``db.py`` helper, so they remain usable from the very first test onward.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# tests/babysit/conftest.py -> tests/babysit -> tests -> <repo root>
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = REPO_ROOT / "skills" / "babysit" / "assets" / "schema.sql"


def _apply_schema(connection: sqlite3.Connection) -> None:
    """Apply schema.sql to the given connection."""
    sql = SCHEMA_PATH.read_text()
    connection.executescript(sql)
    connection.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    """In-memory sqlite3 connection with schema applied and Row factory set."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _apply_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def coordinator_db(tmp_path: Path) -> Path:
    """File-backed sqlite DB path under tmp_path, with schema applied.

    Returns the path so tests can open their own connections (e.g. to
    exercise WAL semantics or multi-connection coordination).
    """
    db_path = tmp_path / "state.db"
    connection = sqlite3.connect(str(db_path))
    try:
        _apply_schema(connection)
    finally:
        connection.close()
    return db_path
