"""Tests for ``skills.babysit.assets.db.list_distinct_prs`` and ``vacuum``.

Behavioural contract under test:

``list_distinct_prs(conn) -> list[dict]``:
1. Empty DB returns ``[]``.
2. Returns distinct ``(repo, pr)`` pairs from ``seen_events`` as dicts
   with keys ``repo`` and ``pr``, sorted ascending by (repo, pr).
3. Duplicates collapsed (a pair with multiple seen rows shows once).
4. Same PR number across two repos surfaces as two distinct entries.

``vacuum(conn) -> None``:
1. Callable on a fresh schema-only DB without error.
2. Callable on a DB with rows previously written by other ops.

Note: SQLite ``VACUUM`` cannot run inside an open transaction.
"""

from __future__ import annotations

from skills.babysit.assets.db import (
    insert_seen_event,
    list_distinct_prs,
    vacuum,
)


def _seed_seen(conn, repo, pr, kind, event_id, ts="2026-05-22T09:00:00Z"):
    conn.execute(
        "INSERT INTO seen_events (repo, pr, kind, event_id, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (repo, pr, kind, event_id, ts),
    )


# ---------- list_distinct_prs ----------

def test_list_distinct_prs_empty_db_returns_empty_list(conn):
    assert list_distinct_prs(conn) == []


def test_list_distinct_prs_returns_sorted_ascending(conn):
    _seed_seen(conn, "org-a/foo", 99, "comment_thread", "e1")
    _seed_seen(conn, "org-a/foo", 7, "ci_failure", "e2")
    _seed_seen(conn, "org-a/foo", 42, "comment_thread", "e3")
    conn.commit()

    assert list_distinct_prs(conn) == [
        {"repo": "org-a/foo", "pr": 7},
        {"repo": "org-a/foo", "pr": 42},
        {"repo": "org-a/foo", "pr": 99},
    ]


def test_list_distinct_prs_collapses_duplicates(conn):
    # Same (repo, pr) appears across multiple kinds/event_ids — must
    # appear once.
    _seed_seen(conn, "org-a/foo", 42, "comment_thread", "e1")
    _seed_seen(conn, "org-a/foo", 42, "comment_thread", "e2")
    _seed_seen(conn, "org-a/foo", 42, "ci_failure", "e3")
    _seed_seen(conn, "org-a/foo", 7, "comment_thread", "e4")
    _seed_seen(conn, "org-a/foo", 7, "comment_thread", "e5")
    conn.commit()

    assert list_distinct_prs(conn) == [
        {"repo": "org-a/foo", "pr": 7},
        {"repo": "org-a/foo", "pr": 42},
    ]


def test_list_distinct_prs_separates_same_pr_across_repos(conn):
    # Same PR number in two repos must produce two distinct entries.
    _seed_seen(conn, "org-a/foo", 42, "comment", "e1")
    _seed_seen(conn, "org-b/bar", 42, "comment", "e2")
    conn.commit()

    assert list_distinct_prs(conn) == [
        {"repo": "org-a/foo", "pr": 42},
        {"repo": "org-b/bar", "pr": 42},
    ]


# ---------- vacuum ----------

def test_vacuum_on_fresh_schema_only_db(conn):
    # Should not raise on an empty (schema-only) DB.
    vacuum(conn)


def test_vacuum_on_db_with_rows(conn):
    # Use the seen_events op to write rows, then VACUUM.
    insert_seen_event(
        conn,
        repo="org-a/foo",
        pr=42,
        kind="comment_thread",
        event_id="e1",
        ts="2026-05-22T09:00:00Z",
    )
    insert_seen_event(
        conn,
        repo="org-a/foo",
        pr=7,
        kind="ci_failure",
        event_id="e2",
        ts="2026-05-22T09:01:00Z",
    )

    # Should not raise even though we just committed transactions.
    vacuum(conn)

    # DB still usable afterwards.
    rows = conn.execute(
        "SELECT pr FROM seen_events ORDER BY pr ASC"
    ).fetchall()
    assert [r[0] for r in rows] == [7, 42]
