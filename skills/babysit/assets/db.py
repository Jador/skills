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
