"""Tests for ``skills.babysit.assets.db.purge_pr``.

Behavioural contract under test:

1. Deletes all ``seen_events`` rows for a single PR.
2. Other PRs' rows are untouched.
3. Returns a dict ``{"seen_events": N}`` with the delete count.
4. Idempotent: a second call returns ``{"seen_events": 0}`` and leaves
   other PRs intact.
"""

from __future__ import annotations

from skills.babysit.assets.db import purge_pr


def _seed_seen(conn, pr, kind, event_id, ts="2026-05-22T09:00:00Z"):
    conn.execute(
        "INSERT INTO seen_events (pr, kind, event_id, ts) VALUES (?, ?, ?, ?)",
        (pr, kind, event_id, ts),
    )


def _seed_pr_events(conn, pr, n=2):
    """Seed N seen_events rows for one PR. Returns row count seeded."""
    for i in range(n):
        _seed_seen(conn, pr, "comment_thread", f"e{pr}-{i}")
    conn.commit()
    return n


def test_purges_all_seen_rows_for_target_pr(conn):
    _seed_pr_events(conn, pr=42, n=2)
    _seed_pr_events(conn, pr=99, n=1)

    purge_pr(conn, 42)

    assert conn.execute(
        "SELECT COUNT(*) FROM seen_events WHERE pr = ?", (42,)
    ).fetchone()[0] == 0


def test_other_prs_untouched(conn):
    _seed_pr_events(conn, pr=42, n=2)
    _seed_pr_events(conn, pr=99, n=3)

    purge_pr(conn, 42)

    assert conn.execute(
        "SELECT COUNT(*) FROM seen_events WHERE pr = ?", (99,)
    ).fetchone()[0] == 3


def test_returns_seen_events_count_dict(conn):
    _seed_pr_events(conn, pr=42, n=2)

    result = purge_pr(conn, 42)

    assert result == {"seen_events": 2}


def test_idempotent_second_call_returns_zero(conn):
    _seed_pr_events(conn, pr=42, n=2)
    _seed_pr_events(conn, pr=99, n=1)

    first = purge_pr(conn, 42)
    assert first == {"seen_events": 2}

    second = purge_pr(conn, 42)
    assert second == {"seen_events": 0}

    # PR 99 still intact after both calls.
    assert conn.execute(
        "SELECT COUNT(*) FROM seen_events WHERE pr = ?", (99,)
    ).fetchone()[0] == 1
