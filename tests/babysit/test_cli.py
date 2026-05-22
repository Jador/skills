"""Tests for the ``skills.babysit.assets.db`` CLI dispatcher.

Behavioural contract under test:

1. Each subcommand is reachable and maps to its underlying op
   (smoke: ``insert_pending`` + ``read_pending`` round trip).
2. ``--json-stdin`` parses payload (and other structured args) from stdin.
3. Malformed JSON on stdin → exit 2 with an error JSON on stdout.
4. Missing ``--db`` and no ``BABYSIT_STATE_DB`` env var → exit 1
   with a ``DB path not provided`` error JSON.
5. ``--db`` overrides ``BABYSIT_STATE_DB`` when both are set.
6. ``purge_pr`` returns the per-table delete counts dict.
7. ``vacuum`` returns ``{"ok": true}``.

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


# ---------- smoke: insert then read ----------

def test_insert_pending_then_read_pending_round_trips(db_file: Path):
    payload = '{"body": "hello"}'
    result = _run([
        "insert_pending",
        "--db", str(db_file),
        "--pr", "123",
        "--kind", "comment_thread",
        "--event-id", "evt-1",
        "--received-ts", "2026-05-22T10:00:00Z",
        "--payload", payload,
    ])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True, "rows_affected": 1}

    # Now read it back.
    result = _run([
        "read_pending",
        "--db", str(db_file),
        "--pr", "123",
    ])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["pr"] == 123
    assert row["kind"] == "comment_thread"
    assert row["event_id"] == "evt-1"
    assert row["payload"] == payload
    assert row["received_ts"] == "2026-05-22T10:00:00Z"


# ---------- --json-stdin parsing ----------

def test_insert_pending_with_json_stdin_payload(db_file: Path):
    payload = '{"body": "from stdin"}'
    result = _run(
        [
            "insert_pending",
            "--db", str(db_file),
            "--pr", "7",
            "--kind", "comment_thread",
            "--event-id", "evt-stdin",
            "--received-ts", "2026-05-22T11:00:00Z",
            "--json-stdin",
        ],
        stdin=payload,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True, "rows_affected": 1}

    # Verify the payload made it in unchanged.
    connection = sqlite3.connect(str(db_file))
    try:
        row = connection.execute(
            "SELECT payload FROM pending_events WHERE pr = 7"
        ).fetchone()
    finally:
        connection.close()
    assert row[0] == payload


# ---------- malformed stdin JSON ----------

def test_malformed_json_stdin_exits_2_with_error_json(db_file: Path):
    # commit_worker_report always reads structured JSON from stdin, so
    # use it as the canonical "stdin must be valid JSON" target.
    result = _run(
        [
            "commit_worker_report",
            "--db", str(db_file),
            "--cluster-id", "c1",
            "--pr", "1",
            "--commit-sha", "deadbeef",
            "--summary", "x",
            "--now-ts", "2026-05-22T12:00:00Z",
        ],
        stdin="{not valid json",
    )
    assert result.returncode == 2
    out = json.loads(result.stdout)
    assert out["ok"] is False
    assert "error" in out


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
            "insert_pending",
            "--db", str(flag_db),
            "--pr", "1",
            "--kind", "comment_thread",
            "--event-id", "e1",
            "--received-ts", "2026-05-22T10:00:00Z",
            "--payload", "x",
        ],
        env_extra={"BABYSIT_STATE_DB": str(env_db)},
    )
    assert result.returncode == 0, result.stderr

    # flag_db got the row.
    c = sqlite3.connect(str(flag_db))
    try:
        assert c.execute("SELECT COUNT(*) FROM pending_events").fetchone()[0] == 1
    finally:
        c.close()
    # env_db is untouched.
    c = sqlite3.connect(str(env_db))
    try:
        assert c.execute("SELECT COUNT(*) FROM pending_events").fetchone()[0] == 0
    finally:
        c.close()


# ---------- BABYSIT_STATE_DB used when --db absent ----------

def test_env_var_used_when_db_flag_absent(tmp_path: Path):
    env_db = tmp_path / "env.db"
    _bootstrap_db(env_db)

    result = _run(
        [
            "insert_pending",
            "--pr", "2",
            "--kind", "comment_thread",
            "--event-id", "e2",
            "--received-ts", "2026-05-22T10:00:00Z",
            "--payload", "x",
        ],
        env_extra={"BABYSIT_STATE_DB": str(env_db)},
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True, "rows_affected": 1}


# ---------- purge_pr ----------

def test_purge_pr_returns_per_table_counts(db_file: Path):
    # Seed one pending row, then purge.
    _run([
        "insert_pending",
        "--db", str(db_file),
        "--pr", "55",
        "--kind", "comment_thread",
        "--event-id", "e1",
        "--received-ts", "2026-05-22T10:00:00Z",
        "--payload", "x",
    ])

    result = _run(["purge_pr", "--db", str(db_file), "--pr", "55"])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True
    counts = out["counts"]
    # All four tables must be reported.
    assert set(counts.keys()) == {
        "seen_events",
        "pending_events",
        "clusters",
        "worker_reports",
    }
    assert counts["pending_events"] == 1
    assert counts["seen_events"] == 0
    assert counts["clusters"] == 0
    assert counts["worker_reports"] == 0


# ---------- vacuum ----------

def test_vacuum_returns_ok(db_file: Path):
    result = _run(["vacuum", "--db", str(db_file)])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True}


# ---------- list_distinct_prs ----------

def test_list_distinct_prs_returns_prs_array(db_file: Path):
    # Seed seen_events directly via sqlite — no CLI op for that yet.
    c = sqlite3.connect(str(db_file))
    try:
        c.execute(
            "INSERT INTO seen_events (pr, kind, event_id, ts) "
            "VALUES (?, ?, ?, ?)",
            (42, "comment_thread", "e1", "2026-05-22T10:00:00Z"),
        )
        c.execute(
            "INSERT INTO seen_events (pr, kind, event_id, ts) "
            "VALUES (?, ?, ?, ?)",
            (7, "comment_thread", "e2", "2026-05-22T10:00:00Z"),
        )
        c.commit()
    finally:
        c.close()

    result = _run(["list_distinct_prs", "--db", str(db_file)])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"ok": True, "prs": [7, 42]}
