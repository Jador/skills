"""Tests for ``skills.babysit.assets.db.insert_seen_event``.

Behavioural contract under test:

1. A fresh ``(repo, pr, kind, event_id)`` row inserts cleanly and returns
   ``rowcount == 1``.
2. A second insert with the same primary key tuple is silently ignored
   via ``INSERT OR IGNORE`` and returns ``rowcount == 0``.
3. Rows differing on any PK component (repo, pr, kind, or event_id) coexist.
4. The same (pr, kind, event_id) across two different repos never collides.
5. ``purge_pr`` clears every ``seen_events`` row for one (repo, pr) pair
   while leaving other pairs intact.
"""

from __future__ import annotations

from skills.babysit.assets.db import (
    insert_seen_event,
    insert_seen_event_batch,
    purge_pr,
)


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
        repo="org-a/foo",
        pr=42,
        kind="comment_thread",
        event_id="evt-1",
        ts="2026-05-22T09:00:00Z",
    )
    assert n == 1
    assert _count_seen(
        conn, repo="org-a/foo", pr=42, kind="comment_thread", event_id="evt-1"
    ) == 1


def test_insert_distinct_rows_all_persist(conn):
    # Four distinct PK tuples — all should land.
    assert insert_seen_event(conn, repo="org-a/foo", pr=1,
                             kind="comment_thread", event_id="e1",
                             ts="t") == 1
    assert insert_seen_event(conn, repo="org-a/foo", pr=1,
                             kind="comment_thread", event_id="e2",
                             ts="t") == 1
    assert insert_seen_event(conn, repo="org-a/foo", pr=1,
                             kind="ci_failure", event_id="e1",
                             ts="t") == 1
    assert insert_seen_event(conn, repo="org-a/foo", pr=2,
                             kind="comment_thread", event_id="e1",
                             ts="t") == 1
    assert _count_seen(conn) == 4


# ---------- INSERT OR IGNORE dedup ----------

def test_duplicate_insert_returns_rowcount_zero(conn):
    first = insert_seen_event(
        conn, repo="org-a/foo", pr=42, kind="comment_thread",
        event_id="evt-1", ts="2026-05-22T09:00:00Z",
    )
    assert first == 1

    # Same PK tuple — different ts (ts is NOT part of PK).
    second = insert_seen_event(
        conn, repo="org-a/foo", pr=42, kind="comment_thread",
        event_id="evt-1", ts="2026-05-22T10:00:00Z",
    )
    assert second == 0

    # Still only one row, and the original ts is preserved (INSERT OR IGNORE
    # does not overwrite).
    rows = conn.execute(
        "SELECT repo, pr, kind, event_id, ts FROM seen_events"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-05-22T09:00:00Z"


def test_dedup_is_per_pk_tuple_not_per_event_id(conn):
    # Same event_id under different (pr, kind) tuples is NOT a dup.
    assert insert_seen_event(conn, repo="org-a/foo", pr=1,
                             kind="comment_thread", event_id="shared",
                             ts="t") == 1
    assert insert_seen_event(conn, repo="org-a/foo", pr=2,
                             kind="comment_thread", event_id="shared",
                             ts="t") == 1
    assert insert_seen_event(conn, repo="org-a/foo", pr=1,
                             kind="ci_failure", event_id="shared",
                             ts="t") == 1
    assert _count_seen(conn) == 3


# ---------- cross-repo isolation ----------

def test_same_pr_different_repos_do_not_collide(conn):
    # The same (pr, kind, event_id) across two repos must coexist.
    assert insert_seen_event(conn, repo="org-a/foo", pr=42,
                             kind="comment", event_id="999",
                             ts="t") == 1
    assert insert_seen_event(conn, repo="org-b/bar", pr=42,
                             kind="comment", event_id="999",
                             ts="t") == 1
    assert _count_seen(conn) == 2
    assert _count_seen(conn, repo="org-a/foo", pr=42) == 1
    assert _count_seen(conn, repo="org-b/bar", pr=42) == 1


# ---------- insert_seen_event_batch ----------

def test_batch_insert_records_all_event_ids_atomically(conn):
    n = insert_seen_event_batch(
        conn,
        repo="org-a/foo",
        pr=42,
        kind="comment",
        event_ids=["100", "101", "102"],
        ts="t",
    )
    assert n == 3
    rows = conn.execute(
        "SELECT event_id FROM seen_events WHERE pr = 42 ORDER BY event_id"
    ).fetchall()
    assert [r[0] for r in rows] == ["100", "101", "102"]


def test_batch_insert_is_partial_dedup_safe(conn):
    # Pre-seed 100; batch contains 100, 101, 102. Only 101 and 102
    # should land — INSERT OR IGNORE skips the existing key.
    insert_seen_event(conn, repo="org-a/foo", pr=42,
                      kind="comment", event_id="100", ts="t")
    n = insert_seen_event_batch(
        conn,
        repo="org-a/foo",
        pr=42,
        kind="comment",
        event_ids=["100", "101", "102"],
        ts="t",
    )
    assert n == 2
    total = conn.execute(
        "SELECT COUNT(*) FROM seen_events WHERE pr = 42"
    ).fetchone()[0]
    assert total == 3


def test_batch_insert_empty_list_is_noop(conn):
    n = insert_seen_event_batch(
        conn,
        repo="org-a/foo",
        pr=42,
        kind="comment",
        event_ids=[],
        ts="t",
    )
    assert n == 0
    assert _count_seen(conn) == 0


# ---------- purge_pr clears seen_events ----------

def test_purge_pr_clears_seen_events_for_target_pair(conn):
    insert_seen_event(conn, repo="org-a/foo", pr=42,
                      kind="comment_thread", event_id="e1", ts="t")
    insert_seen_event(conn, repo="org-a/foo", pr=42,
                      kind="ci_failure", event_id="e2", ts="t")
    insert_seen_event(conn, repo="org-a/foo", pr=99,
                      kind="comment_thread", event_id="e3", ts="t")
    conn.commit()

    counts = purge_pr(conn, repo="org-a/foo", pr=42)

    assert counts == {"seen_events": 2}
    assert _count_seen(conn, repo="org-a/foo", pr=42) == 0
    assert _count_seen(conn, repo="org-a/foo", pr=99) == 1


def test_purge_pr_is_repo_scoped(conn):
    # Same PR number in two repos: purging one must not touch the other.
    insert_seen_event(conn, repo="org-a/foo", pr=42,
                      kind="comment", event_id="999", ts="t")
    insert_seen_event(conn, repo="org-b/bar", pr=42,
                      kind="comment", event_id="999", ts="t")

    counts = purge_pr(conn, repo="org-a/foo", pr=42)

    assert counts == {"seen_events": 1}
    assert _count_seen(conn, repo="org-a/foo", pr=42) == 0
    assert _count_seen(conn, repo="org-b/bar", pr=42) == 1
