"""Tests for ``skills.babysit.assets.db.purge_pr``.

Behavioural contract under test:

1. Deletes all rows for a single PR across four tables in one transaction:
   - ``seen_events`` (rows with matching ``pr``)
   - ``pending_events`` (rows with matching ``pr``)
   - ``clusters`` (rows with matching ``pr``)
   - ``worker_reports`` (rows whose ``cluster_id`` belongs to PR's clusters)
2. Other PRs' rows are untouched.
3. Returns a dict ``{"seen_events": N, "pending_events": N,
   "worker_reports": N, "clusters": N}`` with per-table delete counts.
4. Idempotent: a second call returns all zeros and leaves the DB untouched.
"""

from __future__ import annotations

import json

from skills.babysit.assets.db import purge_pr


def _seed_seen(conn, pr, kind, event_id, ts="2026-05-22T09:00:00Z"):
    conn.execute(
        "INSERT INTO seen_events (pr, kind, event_id, ts) VALUES (?, ?, ?, ?)",
        (pr, kind, event_id, ts),
    )


def _seed_pending(conn, pr, kind, event_id, payload="{}",
                  received_ts="2026-05-22T09:00:00Z"):
    conn.execute(
        "INSERT INTO pending_events (pr, kind, event_id, payload, received_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (pr, kind, event_id, payload, received_ts),
    )


def _seed_cluster(conn, cluster_id, pr, status="running", files=None,
                  created_ts="2026-05-22T09:00:00Z"):
    conn.execute(
        "INSERT INTO clusters (cluster_id, pr, created_ts, status, files_touched) "
        "VALUES (?, ?, ?, ?, ?)",
        (cluster_id, pr, created_ts, status, json.dumps(files or [])),
    )


def _seed_worker_report(conn, cluster_id, ts="2026-05-22T10:00:00Z"):
    conn.execute(
        "INSERT INTO worker_reports "
        "(cluster_id, resolved_ids, unresolved_ids, files_touched, "
        "commit_sha, summary, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cluster_id, "[]", "[]", "[]", "sha", "summary", ts),
    )


def _seed_full_pr(conn, pr, cluster_ids):
    """Seed all four tables for a PR. Returns the cluster_ids list."""
    for cid in cluster_ids:
        _seed_cluster(conn, cid, pr=pr)
        _seed_worker_report(conn, cid)
    _seed_seen(conn, pr, "comment_thread", "e1")
    _seed_seen(conn, pr, "ci_failure", "e2")
    _seed_pending(conn, pr, "comment_thread", "p1")
    _seed_pending(conn, pr, "ci_failure", "p2")
    conn.commit()
    return cluster_ids


def test_purges_all_rows_for_target_pr(conn):
    _seed_full_pr(conn, pr=42, cluster_ids=["cluster-A1", "cluster-A2"])
    _seed_full_pr(conn, pr=99, cluster_ids=["cluster-B1"])

    purge_pr(conn, 42)

    # PR 42 rows are gone everywhere.
    assert conn.execute(
        "SELECT COUNT(*) FROM seen_events WHERE pr = ?", (42,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM pending_events WHERE pr = ?", (42,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM clusters WHERE pr = ?", (42,)
    ).fetchone()[0] == 0
    # worker_reports for PR 42's clusters are gone.
    assert conn.execute(
        "SELECT COUNT(*) FROM worker_reports WHERE cluster_id IN (?, ?)",
        ("cluster-A1", "cluster-A2"),
    ).fetchone()[0] == 0


def test_other_prs_untouched(conn):
    _seed_full_pr(conn, pr=42, cluster_ids=["cluster-A1"])
    _seed_full_pr(conn, pr=99, cluster_ids=["cluster-B1"])

    purge_pr(conn, 42)

    # PR 99 is fully intact.
    assert conn.execute(
        "SELECT COUNT(*) FROM seen_events WHERE pr = ?", (99,)
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM pending_events WHERE pr = ?", (99,)
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM clusters WHERE pr = ?", (99,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM worker_reports WHERE cluster_id = ?",
        ("cluster-B1",),
    ).fetchone()[0] == 1


def test_worker_reports_for_pr_clusters_deleted(conn):
    """worker_reports has cluster_id PK; deletion is via clusters join."""
    _seed_cluster(conn, "cluster-X", pr=7)
    _seed_worker_report(conn, "cluster-X")
    # Sanity: row exists.
    assert conn.execute(
        "SELECT 1 FROM worker_reports WHERE cluster_id = ?", ("cluster-X",)
    ).fetchone() is not None

    purge_pr(conn, 7)

    assert conn.execute(
        "SELECT 1 FROM worker_reports WHERE cluster_id = ?", ("cluster-X",)
    ).fetchone() is None


def test_returns_per_table_counts_dict(conn):
    _seed_full_pr(conn, pr=42, cluster_ids=["cluster-A1", "cluster-A2"])

    result = purge_pr(conn, 42)

    assert result == {
        "seen_events": 2,
        "pending_events": 2,
        "worker_reports": 2,
        "clusters": 2,
    }


def test_idempotent_second_call_returns_zeros(conn):
    _seed_full_pr(conn, pr=42, cluster_ids=["cluster-A1"])
    _seed_full_pr(conn, pr=99, cluster_ids=["cluster-B1"])

    first = purge_pr(conn, 42)
    assert any(v > 0 for v in first.values())

    second = purge_pr(conn, 42)
    assert second == {
        "seen_events": 0,
        "pending_events": 0,
        "worker_reports": 0,
        "clusters": 0,
    }

    # PR 99 still fully intact after both calls.
    assert conn.execute(
        "SELECT COUNT(*) FROM seen_events WHERE pr = ?", (99,)
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM clusters WHERE pr = ?", (99,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM worker_reports WHERE cluster_id = ?",
        ("cluster-B1",),
    ).fetchone()[0] == 1
