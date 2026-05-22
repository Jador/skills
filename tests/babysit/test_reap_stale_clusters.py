"""Tests for ``skills.babysit.assets.db.reap_stale_clusters``.

Behavioural contract under test:

1. ``pr=N, live_cluster_ids=None`` marks every ``status='running'`` row for
   PR N as ``status='abandoned'``.
2. ``pr=N, live_cluster_ids=['c1']`` leaves ``c1`` running and reaps the
   other running rows for PR N.
3. ``pr=None, live_cluster_ids=['c1']`` reaps all running clusters across
   every PR except those in the whitelist.
4. Rows with ``status='done'``, ``status='pending'``, or
   ``status='abandoned'`` are never touched.
5. Returns the count of rows reaped.
6. Empty/zero case: no running clusters → returns 0 and raises no errors.
"""

from __future__ import annotations

import json

from skills.babysit.assets.db import reap_stale_clusters


def _seed_cluster(conn, cluster_id, pr, status="running", files=None,
                  created_ts="2026-05-22T09:00:00Z"):
    conn.execute(
        "INSERT INTO clusters (cluster_id, pr, created_ts, status, files_touched) "
        "VALUES (?, ?, ?, ?, ?)",
        (cluster_id, pr, created_ts, status, json.dumps(files or [])),
    )


def _status_of(conn, cluster_id):
    row = conn.execute(
        "SELECT status FROM clusters WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()
    return row[0] if row is not None else None


def test_pr_scoped_reaps_all_running_for_that_pr(conn):
    _seed_cluster(conn, "c-a1", pr=42, status="running")
    _seed_cluster(conn, "c-a2", pr=42, status="running")
    _seed_cluster(conn, "c-b1", pr=99, status="running")
    conn.commit()

    reaped = reap_stale_clusters(conn, pr=42, live_cluster_ids=None)

    assert reaped == 2
    assert _status_of(conn, "c-a1") == "abandoned"
    assert _status_of(conn, "c-a2") == "abandoned"
    # Other PR untouched.
    assert _status_of(conn, "c-b1") == "running"


def test_pr_scoped_with_whitelist_preserves_live_cluster(conn):
    _seed_cluster(conn, "c1", pr=42, status="running")
    _seed_cluster(conn, "c2", pr=42, status="running")
    _seed_cluster(conn, "c3", pr=42, status="running")
    conn.commit()

    reaped = reap_stale_clusters(conn, pr=42, live_cluster_ids=["c1"])

    assert reaped == 2
    assert _status_of(conn, "c1") == "running"
    assert _status_of(conn, "c2") == "abandoned"
    assert _status_of(conn, "c3") == "abandoned"


def test_cross_pr_reap_with_whitelist(conn):
    _seed_cluster(conn, "c1", pr=42, status="running")
    _seed_cluster(conn, "c2", pr=42, status="running")
    _seed_cluster(conn, "c3", pr=99, status="running")
    _seed_cluster(conn, "c4", pr=7, status="running")
    conn.commit()

    reaped = reap_stale_clusters(conn, pr=None, live_cluster_ids=["c1"])

    assert reaped == 3
    assert _status_of(conn, "c1") == "running"
    assert _status_of(conn, "c2") == "abandoned"
    assert _status_of(conn, "c3") == "abandoned"
    assert _status_of(conn, "c4") == "abandoned"


def test_does_not_touch_done_pending_or_abandoned(conn):
    _seed_cluster(conn, "c-running", pr=42, status="running")
    _seed_cluster(conn, "c-done", pr=42, status="done")
    _seed_cluster(conn, "c-pending", pr=42, status="pending")
    _seed_cluster(conn, "c-abandoned", pr=42, status="abandoned")
    conn.commit()

    reaped = reap_stale_clusters(conn, pr=42, live_cluster_ids=None)

    assert reaped == 1
    assert _status_of(conn, "c-running") == "abandoned"
    assert _status_of(conn, "c-done") == "done"
    assert _status_of(conn, "c-pending") == "pending"
    assert _status_of(conn, "c-abandoned") == "abandoned"


def test_returns_count_of_reaped_rows(conn):
    _seed_cluster(conn, "c1", pr=42, status="running")
    _seed_cluster(conn, "c2", pr=42, status="running")
    _seed_cluster(conn, "c3", pr=42, status="done")
    conn.commit()

    result = reap_stale_clusters(conn, pr=42, live_cluster_ids=None)

    assert result == 2


def test_empty_case_returns_zero(conn):
    # Seed only non-running clusters.
    _seed_cluster(conn, "c-done", pr=42, status="done")
    _seed_cluster(conn, "c-pending", pr=42, status="pending")
    _seed_cluster(conn, "c-abandoned", pr=42, status="abandoned")
    conn.commit()

    result_pr = reap_stale_clusters(conn, pr=42, live_cluster_ids=None)
    result_cross = reap_stale_clusters(conn, pr=None, live_cluster_ids=None)
    result_empty_db = reap_stale_clusters(
        conn, pr=12345, live_cluster_ids=["x"]
    )

    assert result_pr == 0
    assert result_cross == 0
    assert result_empty_db == 0
    # All untouched.
    assert _status_of(conn, "c-done") == "done"
    assert _status_of(conn, "c-pending") == "pending"
    assert _status_of(conn, "c-abandoned") == "abandoned"
