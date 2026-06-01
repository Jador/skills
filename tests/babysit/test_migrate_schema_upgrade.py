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
        # The first v3 poll.sh wrote kind='comment_thread'; current v3
        # queries kind='comment'. Seed with the real v2 value so the
        # test catches the comment_thread -> comment rewrite during
        # upgrade.
        conn.execute(
            "INSERT INTO seen_events (pr, kind, event_id, ts) "
            "VALUES (?, ?, ?, ?)",
            (42, "comment_thread", "999", "2026-05-22T09:00:00Z"),
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
    # Note: kind for the comment row was 'comment_thread' in v2 and must
    # be rewritten to 'comment' so v3 poll.sh's SELECT finds it.
    assert rows == [
        ("legacy/unknown", 7, "build_failure", "100", "2026-05-22T09:01:00Z"),
        ("legacy/unknown", 42, "comment", "999", "2026-05-22T09:00:00Z"),
    ]


def test_v2_comment_thread_kind_rewritten_to_comment(plugin_data: Path):
    # Dedicated regression for the kind-rewrite step: without it every
    # previously-handled review thread would re-dispatch on first v3
    # poll and post a duplicate babysit-agent reply.
    db = plugin_data / "babysit" / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(V2_SCHEMA)
        for cid in ("100", "101", "102"):
            conn.execute(
                "INSERT INTO seen_events (pr, kind, event_id, ts) "
                "VALUES (?, ?, ?, ?)",
                (42, "comment_thread", cid, "2026-05-22T09:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()

    result = _run_migrate(plugin_data)
    assert result.returncode == 0, result.stderr

    # No row may carry the v2 kind after upgrade.
    conn = sqlite3.connect(str(db))
    try:
        residual = conn.execute(
            "SELECT COUNT(*) FROM seen_events WHERE kind = 'comment_thread'"
        ).fetchone()[0]
        migrated = conn.execute(
            "SELECT event_id FROM seen_events "
            "WHERE kind = 'comment' AND pr = 42 ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()
    assert residual == 0
    assert [r[0] for r in migrated] == ["100", "101", "102"]


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


def test_legacy_build_files_migrate_with_kind_build_failure(plugin_data: Path):
    # Drop a legacy <pr>-seen-builds.json into the live babysit dir
    # (which is one of LEGACY_DIRS). After migrate.sh runs, rows must
    # land with kind='build_failure' so runtime poll.sh queries find
    # them — kind='build' would break dedup forever.
    babysit_dir = plugin_data / "babysit"
    legacy_file = babysit_dir / "5-seen-builds.json"
    legacy_file.write_text('{"100": true, "101": true}')

    result = _run_migrate(plugin_data)
    assert result.returncode == 0, result.stderr

    db = babysit_dir / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT repo, pr, kind, event_id FROM seen_events "
            "WHERE pr = 5 ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()

    assert rows == [
        ("legacy/unknown", 5, "build_failure", "100"),
        ("legacy/unknown", 5, "build_failure", "101"),
    ]


def test_migrate_requires_shasum_in_precheck():
    # shasum is invoked to disambiguate legacy-dir basenames. It must be
    # in the precheck tool loop so a missing shasum fails fast with an
    # actionable message, not mid-run after the schema rebuild has
    # already dropped tables.
    src = MIGRATE_SH.read_text()
    assert "for tool in sqlite3 jq shasum" in src, (
        "shasum must be in the precondition tool check"
    )


def test_legacy_comment_ids_numeric_guard_skips_corrupt(plugin_data: Path):
    # A legacy seen-comments file with a non-numeric / multi-line id must
    # not break the generated SQL. The numeric guard drops the corrupt
    # entry and imports the valid ones.
    babysit_dir = plugin_data / "babysit"
    legacy_file = babysit_dir / "9-seen-comments.json"
    # "200" and "201" are valid; the middle entry has an embedded newline
    # that would split the INSERT across statements without the guard.
    legacy_file.write_text('["200", "bad\\nid", "201"]')

    result = _run_migrate(plugin_data)
    assert result.returncode == 0, result.stderr

    db = babysit_dir / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT event_id FROM seen_events WHERE pr = 9 ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()
    # Only the two clean numeric ids survive; the corrupt one is dropped.
    assert [r[0] for r in rows] == ["200", "201"]


def test_migrate_sh_uses_bail_on_for_multistatement_blocks():
    # sqlite3 CLI keeps executing after a per-statement error by default,
    # which silently breaks the atomicity of BEGIN/COMMIT heredocs and
    # the bare DROP block. `.bail on` makes the CLI exit on first
    # error so partial migrations never land. Regression-guard: assert
    # the directive appears in migrate.sh.
    src = MIGRATE_SH.read_text()
    # The dispatcher-cleanup block, the column-upgrade transaction, and
    # the legacy-import sql_tmp construction must all carry `.bail on`.
    occurrences = src.count(".bail on")
    assert occurrences >= 3, (
        f"expected .bail on in dispatcher drop block, v2->v3 rebuild, "
        f"and legacy import; found {occurrences}"
    )


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
