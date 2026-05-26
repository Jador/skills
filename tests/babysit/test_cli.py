"""Tests for the ``skills.babysit.assets.db`` CLI dispatcher.

Behavioural contract under test:

1. Each subcommand is reachable and maps to its underlying op
   (smoke: ``insert_seen`` round trip + dedup).
2. Missing ``--db`` and no ``BABYSIT_STATE_DB`` env var → exit 1
   with a ``DB path not provided`` error JSON.
3. ``--db`` overrides ``BABYSIT_STATE_DB`` when both are set.
4. ``BABYSIT_STATE_DB`` is honoured when ``--db`` is absent.
5. ``purge_pr`` returns the seen_events delete count dict.
6. ``vacuum`` returns ``{"ok": true}``.
7. ``list_distinct_prs`` returns the sorted distinct PRs array.

The CLI is invoked via ``subprocess.run([sys.executable, db_py, ...])`` so
the tests exercise the real ``if __name__ == "__main__"`` entrypoint, not
an in-process function call. The DB file lives under pytest's ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PY = REPO_ROOT / "skills" / "babysit" / "assets" / "db.py"
SCHEMA_PATH = REPO_ROOT / "skills" / "babysit" / "assets" / "schema.sql"


def _bootstrap_db(db_path: Path) -> None:
    """Apply schema.sql to a fresh sqlite DB file."""
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(SCHEMA_PATH.read_text())
        connection.commit()
    finally:
        connection.close()


def _run(args, *, stdin: str | None = None, env_extra: dict | None = None):
    """Invoke the CLI as a subprocess. Returns CompletedProcess.

    stdin is fed as text. env_extra is merged onto os.environ.
    """
    env = os.environ.copy()
    # Make sure BABYSIT_STATE_DB doesn't leak in from the outer shell.
    env.pop("BABYSIT_STATE_DB", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(DB_PY), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    """Path to a schema-bootstrapped sqlite DB under tmp_path."""
    path = tmp_path / "state.db"
    _bootstrap_db(path)
    return path


# ---------- smoke: insert_seen ----------

def test_insert_seen_returns_rowcount_one_on_new_row(db_file: Path):
    result = _run([
        "insert_seen",
        "--db", str(db_file),
        "--pr", "123",
        "--kind", "comment_thread",
        "--event-id", "evt-1",
        "--ts", "2026-05-22T10:00:00Z",
    ])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True, "rows_affected": 1}

    # Verify the row actually landed.
    connection = sqlite3.connect(str(db_file))
    try:
        row = connection.execute(
            "SELECT pr, kind, event_id, ts FROM seen_events WHERE pr = 123"
        ).fetchone()
    finally:
        connection.close()
    assert row == (123, "comment_thread", "evt-1", "2026-05-22T10:00:00Z")


def test_insert_seen_dedup_returns_rowcount_zero(db_file: Path):
    args = [
        "insert_seen",
        "--db", str(db_file),
        "--pr", "7",
        "--kind", "comment_thread",
        "--event-id", "evt-dup",
        "--ts", "2026-05-22T10:00:00Z",
    ]
    first = _run(args)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"ok": True, "rows_affected": 1}

    second = _run(args)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == {"ok": True, "rows_affected": 0}


# ---------- missing --db and no env var ----------

def test_missing_db_path_exits_1_with_error_json(tmp_path: Path):
    # Note: _run() clears BABYSIT_STATE_DB from env.
    result = _run(["list_distinct_prs"])
    assert result.returncode == 1
    out = json.loads(result.stdout)
    assert out["ok"] is False
    assert "DB path not provided" in out["error"]


# ---------- --db overrides BABYSIT_STATE_DB ----------

def test_db_flag_overrides_env_var(tmp_path: Path):
    # Two DB files: one referenced by env var (should be IGNORED), the
    # other by --db (should be USED). Insert a row via --db and confirm
    # the env-var DB stays empty.
    env_db = tmp_path / "env.db"
    flag_db = tmp_path / "flag.db"
    _bootstrap_db(env_db)
    _bootstrap_db(flag_db)

    result = _run(
        [
            "insert_seen",
            "--db", str(flag_db),
            "--pr", "1",
            "--kind", "comment_thread",
            "--event-id", "e1",
            "--ts", "2026-05-22T10:00:00Z",
        ],
        env_extra={"BABYSIT_STATE_DB": str(env_db)},
    )
    assert result.returncode == 0, result.stderr

    # flag_db got the row.
    c = sqlite3.connect(str(flag_db))
    try:
        assert c.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0] == 1
    finally:
        c.close()
    # env_db is untouched.
    c = sqlite3.connect(str(env_db))
    try:
        assert c.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0] == 0
    finally:
        c.close()


# ---------- BABYSIT_STATE_DB used when --db absent ----------

def test_env_var_used_when_db_flag_absent(tmp_path: Path):
    env_db = tmp_path / "env.db"
    _bootstrap_db(env_db)

    result = _run(
        [
            "insert_seen",
            "--pr", "2",
            "--kind", "comment_thread",
            "--event-id", "e2",
            "--ts", "2026-05-22T10:00:00Z",
        ],
        env_extra={"BABYSIT_STATE_DB": str(env_db)},
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True, "rows_affected": 1}


# ---------- purge_pr ----------

def test_purge_pr_returns_seen_events_count(db_file: Path):
    # Seed one row, then purge.
    _run([
        "insert_seen",
        "--db", str(db_file),
        "--pr", "55",
        "--kind", "comment_thread",
        "--event-id", "e1",
        "--ts", "2026-05-22T10:00:00Z",
    ])

    result = _run(["purge_pr", "--db", str(db_file), "--pr", "55"])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True
    counts = out["counts"]
    # Only seen_events is reported under the trimmed surface.
    assert counts == {"seen_events": 1}


# ---------- vacuum ----------

def test_vacuum_returns_ok(db_file: Path):
    result = _run(["vacuum", "--db", str(db_file)])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True}


# ---------- list_distinct_prs ----------

def test_list_distinct_prs_returns_prs_array(db_file: Path):
    # Seed via the CLI itself so we exercise the round trip.
    for pr, eid in [(42, "e1"), (7, "e2")]:
        _run([
            "insert_seen",
            "--db", str(db_file),
            "--pr", str(pr),
            "--kind", "comment_thread",
            "--event-id", eid,
            "--ts", "2026-05-22T10:00:00Z",
        ])

    result = _run(["list_distinct_prs", "--db", str(db_file)])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True, "prs": [7, 42]}


def test_list_distinct_prs_empty_db_returns_empty(db_file: Path):
    result = _run(["list_distinct_prs", "--db", str(db_file)])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True, "prs": []}
