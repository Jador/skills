"""Tests for ``skills.babysit.assets.db.list_distinct_prs`` and ``vacuum``.

Behavioural contract under test:

``list_distinct_prs(conn) -> list[int]``:
1. Empty DB returns ``[]``.
2. Returns distinct PR numbers from ``seen_events``, sorted ascending.
3. Duplicates collapsed (a PR with multiple seen rows shows once).

``vacuum(conn) -> None``:
1. Callable on a fresh schema-only DB without error.
2. Callable on a DB with rows previously written by other ops.

Note: SQLite ``VACUUM`` cannot run inside an open transaction.
"""

from __future__ import annotations

from skills.babysit.assets.db import (
    insert_pending_event,
    list_distinct_prs,
    vacuum,
)


def _seed_seen(conn, pr, kind, event_id, ts="2026-05-22T09:00:00Z"):
    conn.execute(
        "INSERT INTO seen_events (pr, kind, event_id, ts) VALUES (?, ?, ?, ?)",
        (pr, kind, event_id, ts),
    )


# ---------- list_distinct_prs ----------

def test_list_distinct_prs_empty_db_returns_empty_list(conn):
    assert list_distinct_prs(conn) == []


def test_list_distinct_prs_returns_sorted_ascending(conn):
    _seed_seen(conn, 99, "comment_thread", "e1")
    _seed_seen(conn, 7, "ci_failure", "e2")
    _seed_seen(conn, 42, "comment_thread", "e3")
    conn.commit()

    assert list_distinct_prs(conn) == [7, 42, 99]


def test_list_distinct_prs_collapses_duplicates(conn):
    # Same PR appears across multiple kinds/event_ids — must appear once.
    _seed_seen(conn, 42, "comment_thread", "e1")
    _seed_seen(conn, 42, "comment_thread", "e2")
    _seed_seen(conn, 42, "ci_failure", "e3")
    _seed_seen(conn, 7, "comment_thread", "e4")
    _seed_seen(conn, 7, "comment_thread", "e5")
    conn.commit()

    assert list_distinct_prs(conn) == [7, 42]


# ---------- vacuum ----------

def test_vacuum_on_fresh_schema_only_db(conn):
    # Should not raise on an empty (schema-only) DB.
    vacuum(conn)


def test_vacuum_on_db_with_rows(conn):
    # Use an existing op to write rows, then VACUUM.
    insert_pending_event(
        conn,
        pr=42,
        kind="comment_thread",
        event_id="e1",
        payload="{}",
        received_ts="2026-05-22T09:00:00Z",
    )
    insert_pending_event(
        conn,
        pr=7,
        kind="ci_failure",
        event_id="e2",
        payload="{}",
        received_ts="2026-05-22T09:01:00Z",
    )

    # Should not raise even though we just committed transactions.
    vacuum(conn)

    # DB still usable afterwards.
    rows = conn.execute(
        "SELECT pr FROM pending_events ORDER BY pr ASC"
    ).fetchall()
    assert [r[0] for r in rows] == [7, 42]
