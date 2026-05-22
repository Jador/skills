"""babysit DB helper — single source of truth for SQL writes.

All operations take an open sqlite3.Connection. The CLI in __main__ (added
later) wraps these for shell callers.
"""
from __future__ import annotations
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
