"""Tests for ``skills.babysit.assets.db.insert_seen_event``.

Behavioural contract under test:

1. A fresh ``(pr, kind, event_id)`` row inserts cleanly and returns
   ``rowcount == 1``.
2. A second insert with the same primary key tuple is silently ignored
   via ``INSERT OR IGNORE`` and returns ``rowcount == 0``.
3. Rows differing on any PK component (pr, kind, or event_id) coexist.
4. ``purge_pr`` clears every ``seen_events`` row for the target PR while
   leaving other PRs intact.
"""

from __future__ import annotations

from skills.babysit.assets.db import insert_seen_event, purge_pr


def _count_seen(conn, **where) -> int:
    if not where:
        return conn.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0]
    clauses = " AND ".join(f"{col} = ?" for col in where)
    return conn.execute(
        f"SELECT COUNT(*) FROM seen_events WHERE {clauses}",
        tuple(where.values()),
    ).fetchone()[0]


# ---------- normal insert ----------

def test_insert_new_event_returns_rowcount_one(conn):
    n = insert_seen_event(
        conn,
        pr=42,
        kind="comment_thread",
        event_id="evt-1",
        ts="2026-05-22T09:00:00Z",
    )
    assert n == 1
    assert _count_seen(conn, pr=42, kind="comment_thread", event_id="evt-1") == 1


def test_insert_distinct_rows_all_persist(conn):
    # Three distinct PK tuples — all should land.
    assert insert_seen_event(conn, pr=1, kind="comment_thread",
                             event_id="e1", ts="t") == 1
    assert insert_seen_event(conn, pr=1, kind="comment_thread",
                             event_id="e2", ts="t") == 1
    assert insert_seen_event(conn, pr=1, kind="ci_failure",
                             event_id="e1", ts="t") == 1
    assert insert_seen_event(conn, pr=2, kind="comment_thread",
                             event_id="e1", ts="t") == 1
    assert _count_seen(conn) == 4


# ---------- INSERT OR IGNORE dedup ----------

def test_duplicate_insert_returns_rowcount_zero(conn):
    first = insert_seen_event(
        conn, pr=42, kind="comment_thread", event_id="evt-1",
        ts="2026-05-22T09:00:00Z",
    )
    assert first == 1

    # Same PK tuple — different ts (ts is NOT part of PK).
    second = insert_seen_event(
        conn, pr=42, kind="comment_thread", event_id="evt-1",
        ts="2026-05-22T10:00:00Z",
    )
    assert second == 0

    # Still only one row, and the original ts is preserved (INSERT OR IGNORE
    # does not overwrite).
    rows = conn.execute(
        "SELECT pr, kind, event_id, ts FROM seen_events"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-05-22T09:00:00Z"


def test_dedup_is_per_pk_tuple_not_per_event_id(conn):
    # Same event_id under different (pr, kind) tuples is NOT a dup.
    assert insert_seen_event(conn, pr=1, kind="comment_thread",
                             event_id="shared", ts="t") == 1
    assert insert_seen_event(conn, pr=2, kind="comment_thread",
                             event_id="shared", ts="t") == 1
    assert insert_seen_event(conn, pr=1, kind="ci_failure",
                             event_id="shared", ts="t") == 1
    assert _count_seen(conn) == 3


# ---------- purge_pr clears seen_events ----------

def test_purge_pr_clears_seen_events_for_target_pr(conn):
    insert_seen_event(conn, pr=42, kind="comment_thread",
                      event_id="e1", ts="t")
    insert_seen_event(conn, pr=42, kind="ci_failure",
                      event_id="e2", ts="t")
    insert_seen_event(conn, pr=99, kind="comment_thread",
                      event_id="e3", ts="t")
    conn.commit()

    counts = purge_pr(conn, 42)

    assert counts == {"seen_events": 2}
    assert _count_seen(conn, pr=42) == 0
    assert _count_seen(conn, pr=99) == 1
