"""Tests for ``skills.babysit.assets.db.insert_pending_event``.

Behavioural contract under test:

1. Inserts one row whose columns exactly match caller-provided values.
2. Re-inserting the same ``(pr, kind, event_id)`` is a no-op (idempotent).
   First call returns 1 row affected; second returns 0. Final row count = 1.
3. Adversarial payload bodies (single-quote-with-backslash, double quotes,
   embedded newlines) round-trip intact.
4. ``received_ts`` is stored exactly as the caller passed it — the function
   must NOT generate its own timestamp.
"""

from __future__ import annotations

from skills.babysit.assets.db import insert_pending_event


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM pending_events").fetchone()[0]


def test_insert_round_trips_all_fields(conn):
    affected = insert_pending_event(
        conn,
        pr=123,
        kind="comment",
        event_id="evt-abc",
        payload='{"body": "hello"}',
        received_ts="2026-05-22T10:00:00Z",
    )

    assert affected == 1
    assert _count(conn) == 1

    row = conn.execute("SELECT * FROM pending_events").fetchone()
    assert row["pr"] == 123
    assert row["kind"] == "comment"
    assert row["event_id"] == "evt-abc"
    assert row["payload"] == '{"body": "hello"}'
    assert row["received_ts"] == "2026-05-22T10:00:00Z"


def test_reinsert_same_key_is_noop(conn):
    first = insert_pending_event(
        conn,
        pr=7,
        kind="review",
        event_id="evt-1",
        payload="first",
        received_ts="2026-05-22T11:00:00Z",
    )
    second = insert_pending_event(
        conn,
        pr=7,
        kind="review",
        event_id="evt-1",
        # Different payload + ts on second call — must NOT overwrite.
        payload="second",
        received_ts="2026-05-22T12:00:00Z",
    )

    assert first == 1
    assert second == 0
    assert _count(conn) == 1

    row = conn.execute("SELECT * FROM pending_events").fetchone()
    # Original values preserved — INSERT OR IGNORE, not REPLACE.
    assert row["payload"] == "first"
    assert row["received_ts"] == "2026-05-22T11:00:00Z"


def test_adversarial_payload_round_trips(conn):
    # backslash-quote ("I\'m"), double quotes, embedded newlines.
    # NOTE: NUL bytes intentionally excluded — Python sqlite3 driver
    # rejects them in TEXT columns.
    adversarial = "I\\'m \"weird\"\nmulti\nline\tpayload"

    affected = insert_pending_event(
        conn,
        pr=42,
        kind="comment",
        event_id="evt-adv",
        payload=adversarial,
        received_ts="2026-05-22T13:00:00Z",
    )

    assert affected == 1
    row = conn.execute(
        "SELECT payload FROM pending_events WHERE pr = ?", (42,)
    ).fetchone()
    assert row["payload"] == adversarial


def test_received_ts_is_caller_provided_not_generated(conn):
    # Use a clearly synthetic timestamp that the function would never
    # generate on its own — proves the function is a pure pass-through.
    sentinel_ts = "1999-01-01T00:00:00Z"

    insert_pending_event(
        conn,
        pr=1,
        kind="comment",
        event_id="evt-ts",
        payload="x",
        received_ts=sentinel_ts,
    )

    row = conn.execute(
        "SELECT received_ts FROM pending_events WHERE pr = 1"
    ).fetchone()
    assert row["received_ts"] == sentinel_ts
