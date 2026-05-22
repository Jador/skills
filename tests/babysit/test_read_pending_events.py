"""Tests for ``skills.babysit.assets.db.read_pending_events``.

Behavioural contract under test:

1. Returns rows for a given PR, ordered by ``received_ts`` ascending.
2. Returns an empty list ``[]`` when no rows exist for that PR.
3. Does not return rows belonging to other PRs (isolation).
4. Each returned item is a dict (or dict-like) exposing keys
   ``pr``, ``kind``, ``event_id``, ``payload``, ``received_ts``.

Rows are seeded via the existing ``insert_pending_event`` function, so these
tests double as an integration check between the two DB ops.
"""

from __future__ import annotations

from skills.babysit.assets.db import insert_pending_event, read_pending_events


def test_returns_rows_for_pr_ordered_by_received_ts_asc(conn):
    # Insert out of chronological order to prove ORDER BY is doing the work.
    insert_pending_event(
        conn,
        pr=10,
        kind="comment",
        event_id="evt-b",
        payload="second",
        received_ts="2026-05-22T11:00:00Z",
    )
    insert_pending_event(
        conn,
        pr=10,
        kind="comment",
        event_id="evt-a",
        payload="first",
        received_ts="2026-05-22T10:00:00Z",
    )
    insert_pending_event(
        conn,
        pr=10,
        kind="review",
        event_id="evt-c",
        payload="third",
        received_ts="2026-05-22T12:00:00Z",
    )

    rows = read_pending_events(conn, 10)

    assert len(rows) == 3
    assert [r["event_id"] for r in rows] == ["evt-a", "evt-b", "evt-c"]
    assert [r["received_ts"] for r in rows] == [
        "2026-05-22T10:00:00Z",
        "2026-05-22T11:00:00Z",
        "2026-05-22T12:00:00Z",
    ]


def test_returns_empty_list_when_no_rows_for_pr(conn):
    # DB is empty for PR 999.
    assert read_pending_events(conn, 999) == []


def test_does_not_return_rows_for_other_prs(conn):
    insert_pending_event(
        conn,
        pr=1,
        kind="comment",
        event_id="evt-1",
        payload="for-1",
        received_ts="2026-05-22T10:00:00Z",
    )
    insert_pending_event(
        conn,
        pr=2,
        kind="comment",
        event_id="evt-2",
        payload="for-2",
        received_ts="2026-05-22T10:00:00Z",
    )
    insert_pending_event(
        conn,
        pr=2,
        kind="review",
        event_id="evt-3",
        payload="also-for-2",
        received_ts="2026-05-22T11:00:00Z",
    )

    rows_for_1 = read_pending_events(conn, 1)
    assert len(rows_for_1) == 1
    assert rows_for_1[0]["pr"] == 1
    assert rows_for_1[0]["event_id"] == "evt-1"

    rows_for_2 = read_pending_events(conn, 2)
    assert len(rows_for_2) == 2
    assert {r["event_id"] for r in rows_for_2} == {"evt-2", "evt-3"}
    assert all(r["pr"] == 2 for r in rows_for_2)


def test_each_item_has_expected_keys(conn):
    insert_pending_event(
        conn,
        pr=55,
        kind="comment",
        event_id="evt-x",
        payload='{"body": "hi"}',
        received_ts="2026-05-22T09:00:00Z",
    )

    rows = read_pending_events(conn, 55)

    assert len(rows) == 1
    row = rows[0]
    expected_keys = {"pr", "kind", "event_id", "payload", "received_ts"}
    # Dict-like access on every required key.
    for key in expected_keys:
        assert key in row, f"missing key: {key}"
    assert row["pr"] == 55
    assert row["kind"] == "comment"
    assert row["event_id"] == "evt-x"
    assert row["payload"] == '{"body": "hi"}'
    assert row["received_ts"] == "2026-05-22T09:00:00Z"
