"""babysit DB helper — single source of truth for SQL writes.

All operations take an open sqlite3.Connection. The CLI in __main__ (added
later) wraps these for shell callers.
"""
from __future__ import annotations
import json
import sqlite3


def insert_pending_event(
    conn: sqlite3.Connection,
    pr: int,
    kind: str,
    event_id: str,
    payload: str,
    received_ts: str,
) -> int:
    """INSERT OR IGNORE one row into pending_events. Returns rows affected."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO pending_events "
        "(pr, kind, event_id, payload, received_ts) VALUES (?, ?, ?, ?, ?)",
        (pr, kind, event_id, payload, received_ts),
    )
    conn.commit()
    return cur.rowcount


def read_pending_events(conn: sqlite3.Connection, pr: int) -> list[dict]:
    """Return pending_events rows for one PR, oldest first."""
    cur = conn.execute(
        "SELECT pr, kind, event_id, payload, received_ts "
        "FROM pending_events WHERE pr = ? ORDER BY received_ts ASC",
        (pr,),
    )
    return [dict(row) for row in cur.fetchall()]


def claim_cluster(
    conn: sqlite3.Connection,
    cluster_id: str,
    pr: int,
    predicted_files: list[str],
    created_ts: str,
) -> bool:
    """Atomic single-winner cluster claim.

    Two-step: INSERT OR IGNORE the cluster row with status='pending', then
    UPDATE to 'running' only if it is still 'pending'. The UPDATE's
    rowcount is the single-winner oracle (==1 means we won the race).
    """
    files_json = json.dumps(predicted_files)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO clusters "
            "(cluster_id, pr, created_ts, status, files_touched) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (cluster_id, pr, created_ts, files_json),
        )
        cur = conn.execute(
            "UPDATE clusters SET status='running' "
            "WHERE cluster_id = ? AND status = 'pending'",
            (cluster_id,),
        )
    return cur.rowcount == 1


def commit_worker_report(
    conn: sqlite3.Connection,
    cluster_id: str,
    pr: int,
    resolved_event_ids: list[dict],
    unresolved_event_ids: list[dict],
    files_touched: list[str],
    commit_sha: str,
    summary: str,
    now_ts: str,
) -> dict:
    """Atomically persist a worker's result.

    Inside one transaction:
      - INSERT OR IGNORE every resolved tuple into seen_events.
      - INSERT a worker_reports row (raises on duplicate cluster_id).
      - UPDATE clusters.status='done', clusters.files_touched=JSON.
      - DELETE matching (pr, kind, event_id) from pending_events.

    Returns {"seen_inserted": N, "pending_deleted": M}.
    """
    seen_inserted = 0
    pending_deleted = 0
    files_json = json.dumps(files_touched)
    resolved_json = json.dumps(resolved_event_ids)
    unresolved_json = json.dumps(unresolved_event_ids)
    with conn:
        for ev in resolved_event_ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO seen_events (pr, kind, event_id, ts) "
                "VALUES (?, ?, ?, ?)",
                (pr, ev["kind"], ev["event_id"], now_ts),
            )
            seen_inserted += cur.rowcount
        conn.execute(
            "INSERT INTO worker_reports "
            "(cluster_id, resolved_ids, unresolved_ids, files_touched, "
            "commit_sha, summary, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cluster_id, resolved_json, unresolved_json, files_json,
             commit_sha, summary, now_ts),
        )
        conn.execute(
            "UPDATE clusters SET status='done', files_touched=? "
            "WHERE cluster_id = ?",
            (files_json, cluster_id),
        )
        for ev in resolved_event_ids:
            cur = conn.execute(
                "DELETE FROM pending_events "
                "WHERE pr = ? AND kind = ? AND event_id = ?",
                (pr, ev["kind"], ev["event_id"]),
            )
            pending_deleted += cur.rowcount
    return {"seen_inserted": seen_inserted, "pending_deleted": pending_deleted}
