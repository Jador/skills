"""Tests for ``skills.babysit.assets.db.commit_worker_report``.

Behavioural contract under test:

1. UPSERTs each ``resolved_event_ids`` tuple into ``seen_events`` with
   ``(pr, kind, event_id, ts=now_ts)``. Duplicate event_ids in seen_events
   do not error (INSERT OR IGNORE semantics).
2. Inserts a ``worker_reports`` row keyed by ``cluster_id`` with
   resolved/unresolved stored as JSON strings, ``files_touched`` as JSON
   string, ``commit_sha`` and ``summary`` stored as-is, ``ts=now_ts``.
3. UPDATEs ``clusters.status='done'`` and
   ``clusters.files_touched=json.dumps(files_touched)`` for ``cluster_id``.
4. DELETEs rows from ``pending_events`` matching the resolved
   ``(pr, kind, event_id)`` tuples. Unresolved rows remain. Other PRs'
   rows remain.
5. Returns ``{"seen_inserted": <count>, "pending_deleted": <count>}``.
6. The whole operation runs in a single transaction; a PK conflict on
   ``worker_reports.cluster_id`` rolls back any ``seen_events`` /
   ``pending_events`` writes made in that call.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from skills.babysit.assets.db import commit_worker_report


def _seed_cluster(conn, cluster_id, pr, status="running", files=None):
    conn.execute(
        "INSERT INTO clusters (cluster_id, pr, created_ts, status, files_touched) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            cluster_id,
            pr,
            "2026-05-22T09:00:00Z",
            status,
            json.dumps(files or []),
        ),
    )
    conn.commit()


def _seed_pending(conn, pr, kind, event_id, payload="{}"):
    conn.execute(
        "INSERT INTO pending_events (pr, kind, event_id, payload, received_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (pr, kind, event_id, payload, "2026-05-22T09:30:00Z"),
    )
    conn.commit()


def test_inserts_seen_events_for_each_resolved(conn):
    _seed_cluster(conn, "cluster-A", pr=42)
    resolved = [
        {"kind": "comment_thread", "event_id": "1"},
        {"kind": "comment_thread", "event_id": "2"},
        {"kind": "ci_failure", "event_id": "abc"},
    ]

    result = commit_worker_report(
        conn,
        cluster_id="cluster-A",
        pr=42,
        resolved_event_ids=resolved,
        unresolved_event_ids=[],
        files_touched=["a.py"],
        commit_sha="deadbeef",
        summary="ok",
        now_ts="2026-05-22T10:00:00Z",
    )

    rows = conn.execute(
        "SELECT pr, kind, event_id, ts FROM seen_events ORDER BY kind, event_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (42, "ci_failure", "abc", "2026-05-22T10:00:00Z"),
        (42, "comment_thread", "1", "2026-05-22T10:00:00Z"),
        (42, "comment_thread", "2", "2026-05-22T10:00:00Z"),
    ]
    assert result["seen_inserted"] == 3


def test_seen_events_insert_or_ignore_on_duplicate(conn):
    _seed_cluster(conn, "cluster-B", pr=7)
    # Pre-existing seen_events row should NOT cause an error and should NOT
    # be re-counted in seen_inserted.
    conn.execute(
        "INSERT INTO seen_events (pr, kind, event_id, ts) VALUES (?, ?, ?, ?)",
        (7, "comment_thread", "99", "2026-05-22T08:00:00Z"),
    )
    conn.commit()

    result = commit_worker_report(
        conn,
        cluster_id="cluster-B",
        pr=7,
        resolved_event_ids=[
            {"kind": "comment_thread", "event_id": "99"},  # duplicate
            {"kind": "comment_thread", "event_id": "100"},  # new
        ],
        unresolved_event_ids=[],
        files_touched=[],
        commit_sha="sha",
        summary="",
        now_ts="2026-05-22T10:00:00Z",
    )

    # Only the new one counts as inserted.
    assert result["seen_inserted"] == 1
    # Pre-existing row's ts is preserved (INSERT OR IGNORE, not REPLACE).
    pre = conn.execute(
        "SELECT ts FROM seen_events WHERE pr=? AND kind=? AND event_id=?",
        (7, "comment_thread", "99"),
    ).fetchone()
    assert pre["ts"] == "2026-05-22T08:00:00Z"


def test_inserts_worker_reports_row(conn):
    _seed_cluster(conn, "cluster-C", pr=11)
    resolved = [{"kind": "comment_thread", "event_id": "1"}]
    unresolved = [{"kind": "ci_failure", "event_id": "x"}]
    files = ["src/foo.py", "tests/test_foo.py"]

    commit_worker_report(
        conn,
        cluster_id="cluster-C",
        pr=11,
        resolved_event_ids=resolved,
        unresolved_event_ids=unresolved,
        files_touched=files,
        commit_sha="cafebabe",
        summary="fixed all the things",
        now_ts="2026-05-22T10:00:00Z",
    )

    row = conn.execute(
        "SELECT cluster_id, resolved_ids, unresolved_ids, files_touched, "
        "commit_sha, summary, ts FROM worker_reports WHERE cluster_id = ?",
        ("cluster-C",),
    ).fetchone()
    assert row is not None
    assert row["cluster_id"] == "cluster-C"
    assert json.loads(row["resolved_ids"]) == resolved
    assert json.loads(row["unresolved_ids"]) == unresolved
    assert json.loads(row["files_touched"]) == files
    assert row["commit_sha"] == "cafebabe"
    assert row["summary"] == "fixed all the things"
    assert row["ts"] == "2026-05-22T10:00:00Z"


def test_updates_cluster_status_and_files(conn):
    _seed_cluster(conn, "cluster-D", pr=5, files=["old.py"])

    new_files = ["new1.py", "new2.py"]
    commit_worker_report(
        conn,
        cluster_id="cluster-D",
        pr=5,
        resolved_event_ids=[],
        unresolved_event_ids=[],
        files_touched=new_files,
        commit_sha="sha",
        summary="",
        now_ts="2026-05-22T10:00:00Z",
    )

    row = conn.execute(
        "SELECT status, files_touched FROM clusters WHERE cluster_id = ?",
        ("cluster-D",),
    ).fetchone()
    assert row["status"] == "done"
    assert json.loads(row["files_touched"]) == new_files


def test_deletes_resolved_pending_events_only(conn):
    _seed_cluster(conn, "cluster-E", pr=3)
    # Resolved events on PR 3
    _seed_pending(conn, pr=3, kind="comment_thread", event_id="r1")
    _seed_pending(conn, pr=3, kind="comment_thread", event_id="r2")
    # Unresolved on PR 3 — must remain
    _seed_pending(conn, pr=3, kind="ci_failure", event_id="u1")
    # Same kind/event_id but DIFFERENT PR — must remain
    _seed_pending(conn, pr=999, kind="comment_thread", event_id="r1")

    result = commit_worker_report(
        conn,
        cluster_id="cluster-E",
        pr=3,
        resolved_event_ids=[
            {"kind": "comment_thread", "event_id": "r1"},
            {"kind": "comment_thread", "event_id": "r2"},
        ],
        unresolved_event_ids=[{"kind": "ci_failure", "event_id": "u1"}],
        files_touched=[],
        commit_sha="sha",
        summary="",
        now_ts="2026-05-22T10:00:00Z",
    )

    remaining = conn.execute(
        "SELECT pr, kind, event_id FROM pending_events "
        "ORDER BY pr, kind, event_id"
    ).fetchall()
    assert [tuple(r) for r in remaining] == [
        (3, "ci_failure", "u1"),
        (999, "comment_thread", "r1"),
    ]
    assert result["pending_deleted"] == 2


def test_returns_counts_dict(conn):
    _seed_cluster(conn, "cluster-F", pr=8)
    _seed_pending(conn, pr=8, kind="comment_thread", event_id="e1")
    # No pending row for e2 → only one DELETE actually removes a row.

    result = commit_worker_report(
        conn,
        cluster_id="cluster-F",
        pr=8,
        resolved_event_ids=[
            {"kind": "comment_thread", "event_id": "e1"},
            {"kind": "comment_thread", "event_id": "e2"},
        ],
        unresolved_event_ids=[],
        files_touched=[],
        commit_sha="sha",
        summary="",
        now_ts="2026-05-22T10:00:00Z",
    )

    assert result == {"seen_inserted": 2, "pending_deleted": 1}


def test_pk_conflict_rolls_back_seen_and_pending_writes(conn):
    """A duplicate worker_reports.cluster_id must abort the whole txn.

    First call succeeds. Second call (same cluster_id) attempts to insert
    new seen_events and delete more pending_events, but the
    worker_reports PK conflict raises IntegrityError — and those
    in-flight writes from the second attempt must NOT persist.
    """
    _seed_cluster(conn, "cluster-G", pr=4)
    _seed_pending(conn, pr=4, kind="comment_thread", event_id="first")
    _seed_pending(conn, pr=4, kind="comment_thread", event_id="second")

    # First call: writes the worker_reports row, deletes "first".
    commit_worker_report(
        conn,
        cluster_id="cluster-G",
        pr=4,
        resolved_event_ids=[{"kind": "comment_thread", "event_id": "first"}],
        unresolved_event_ids=[],
        files_touched=[],
        commit_sha="sha1",
        summary="first",
        now_ts="2026-05-22T10:00:00Z",
    )

    seen_before = conn.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0]
    pending_before = conn.execute(
        "SELECT COUNT(*) FROM pending_events"
    ).fetchone()[0]

    # Second call: same cluster_id triggers worker_reports PK violation.
    with pytest.raises(sqlite3.IntegrityError):
        commit_worker_report(
            conn,
            cluster_id="cluster-G",
            pr=4,
            resolved_event_ids=[
                {"kind": "comment_thread", "event_id": "second"}
            ],
            unresolved_event_ids=[],
            files_touched=[],
            commit_sha="sha2",
            summary="second",
            now_ts="2026-05-22T11:00:00Z",
        )

    # Counts must be unchanged from the rolled-back attempt.
    seen_after = conn.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0]
    pending_after = conn.execute(
        "SELECT COUNT(*) FROM pending_events"
    ).fetchone()[0]
    assert seen_after == seen_before
    assert pending_after == pending_before
    # And the "second" pending row in particular is still present.
    still_there = conn.execute(
        "SELECT 1 FROM pending_events "
        "WHERE pr=? AND kind=? AND event_id=?",
        (4, "comment_thread", "second"),
    ).fetchone()
    assert still_there is not None
