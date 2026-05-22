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
