"""Tests for the v2->v3 schema column upgrade in migrate.sh.

When a pre-v3 seen_events table exists (no `repo` column), migrate.sh
must:

1. Rebuild the table with the new PK ``(repo, pr, kind, event_id)``.
2. Stamp every legacy row with the sentinel repo ``legacy/unknown``.
3. Preserve the (pr, kind, event_id, ts) data verbatim.
4. Be idempotent: re-running migrate.sh on the already-upgraded DB must
   leave the rebuilt rows intact.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATE_SH = REPO_ROOT / "skills" / "babysit" / "assets" / "migrate.sh"
SCHEMA_PATH = REPO_ROOT / "skills" / "babysit" / "assets" / "schema.sql"

V2_SCHEMA = """
CREATE TABLE seen_events (
    pr INT,
    kind TEXT,
    event_id TEXT,
    ts TEXT,
    PRIMARY KEY (pr, kind, event_id)
);
CREATE TABLE pipelines (
    repo TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    ts TEXT
);
"""


def _build_v2_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(V2_SCHEMA)
        conn.execute(
            "INSERT INTO seen_events (pr, kind, event_id, ts) "
            "VALUES (?, ?, ?, ?)",
            (42, "comment", "999", "2026-05-22T09:00:00Z"),
        )
        conn.execute(
            "INSERT INTO seen_events (pr, kind, event_id, ts) "
            "VALUES (?, ?, ?, ?)",
            (7, "build_failure", "100", "2026-05-22T09:01:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def _run_migrate(plugin_data: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(plugin_data)
    # Point HOME at a temp dir so the legacy-dir scan is empty — the
    # column upgrade is what we want to exercise here.
    env["HOME"] = str(plugin_data)
    return subprocess.run(
        ["bash", str(MIGRATE_SH)],
        env=env,
        capture_output=True,
        text=True,
    )


def _table_info(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("PRAGMA table_info('seen_events')")
        return [
            {"name": row[1], "type": row[2], "pk": row[5]}
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def _fetchall_rows(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT repo, pr, kind, event_id, ts FROM seen_events "
            "ORDER BY pr ASC"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def plugin_data(tmp_path: Path) -> Path:
    (tmp_path / "babysit").mkdir()
    return tmp_path


def test_v2_to_v3_adds_repo_column_with_sentinel(plugin_data: Path):
    db = plugin_data / "babysit" / "state.db"
    _build_v2_db(db)

    result = _run_migrate(plugin_data)
    assert result.returncode == 0, result.stderr

    info = {col["name"]: col for col in _table_info(db)}
    assert "repo" in info
    # PK columns: repo (pk=1), pr (pk=2), kind (pk=3), event_id (pk=4).
    assert info["repo"]["pk"] == 1
    assert info["pr"]["pk"] == 2
    assert info["kind"]["pk"] == 3
    assert info["event_id"]["pk"] == 4

    rows = _fetchall_rows(db)
    assert rows == [
        ("legacy/unknown", 7, "build_failure", "100", "2026-05-22T09:01:00Z"),
        ("legacy/unknown", 42, "comment", "999", "2026-05-22T09:00:00Z"),
    ]


def test_v2_to_v3_idempotent_second_run_is_noop(plugin_data: Path):
    db = plugin_data / "babysit" / "state.db"
    _build_v2_db(db)

    first = _run_migrate(plugin_data)
    assert first.returncode == 0, first.stderr
    rows_after_first = _fetchall_rows(db)

    second = _run_migrate(plugin_data)
    assert second.returncode == 0, second.stderr
    rows_after_second = _fetchall_rows(db)

    assert rows_after_first == rows_after_second


def test_fresh_v3_db_unchanged_by_migrate(plugin_data: Path):
    # Bootstrap a fresh v3 DB. Running migrate.sh against it must not
    # touch the rows.
    db = plugin_data / "babysit" / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.execute(
            "INSERT INTO seen_events (repo, pr, kind, event_id, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("org-a/foo", 42, "comment", "999", "2026-05-22T09:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    result = _run_migrate(plugin_data)
    assert result.returncode == 0, result.stderr

    rows = _fetchall_rows(db)
    assert rows == [
        ("org-a/foo", 42, "comment", "999", "2026-05-22T09:00:00Z"),
    ]
