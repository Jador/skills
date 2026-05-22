"""Tests for ``skills.babysit.assets.db.claim_cluster``.

Behavioural contract under test:

1. First call returns ``True`` and the row's status becomes ``'running'``.
2. Second call with the same ``cluster_id`` returns ``False`` and does NOT
   overwrite the originally stored ``files_touched``.
3. Contender simulation: if a row with that ``cluster_id`` already exists
   with ``status='running'`` (a different coordinator won), the call
   returns ``False``.
4. ``predicted_files`` is stored as a JSON-serialized string in the
   ``files_touched`` column.
5. ``pr`` and ``created_ts`` are stored exactly as passed — the function
   does not invent timestamps.
"""

from __future__ import annotations

import json

from skills.babysit.assets.db import claim_cluster


def _select_cluster(conn, cluster_id):
    return conn.execute(
        "SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()


def test_first_call_wins_and_marks_running(conn):
    won = claim_cluster(
        conn,
        cluster_id="cluster-1",
        pr=42,
        predicted_files=["a.py", "b.py"],
        created_ts="2026-05-22T10:00:00Z",
    )

    assert won is True

    status = conn.execute(
        "SELECT status FROM clusters WHERE cluster_id = ?", ("cluster-1",)
    ).fetchone()[0]
    assert status == "running"


def test_second_call_loses_and_preserves_files(conn):
    first_files = ["original.py"]
    second_files = ["should-not-overwrite.py"]

    first = claim_cluster(
        conn,
        cluster_id="cluster-2",
        pr=7,
        predicted_files=first_files,
        created_ts="2026-05-22T11:00:00Z",
    )
    second = claim_cluster(
        conn,
        cluster_id="cluster-2",
        pr=7,
        predicted_files=second_files,
        created_ts="2026-05-22T12:00:00Z",
    )

    assert first is True
    assert second is False

    row = _select_cluster(conn, "cluster-2")
    # files_touched from the first call must be intact.
    assert row["files_touched"] == json.dumps(first_files)


def test_contender_already_running_loses(conn):
    # Simulate another coordinator winning: row exists, status='running'.
    conn.execute(
        "INSERT INTO clusters (cluster_id, pr, created_ts, status, files_touched) "
        "VALUES (?, ?, ?, 'pending', ?)",
        ("cluster-3", 99, "2026-05-22T09:00:00Z", json.dumps(["other.py"])),
    )
    conn.execute(
        "UPDATE clusters SET status='running' WHERE cluster_id = ?",
        ("cluster-3",),
    )
    conn.commit()

    won = claim_cluster(
        conn,
        cluster_id="cluster-3",
        pr=99,
        predicted_files=["mine.py"],
        created_ts="2026-05-22T10:00:00Z",
    )

    assert won is False

    row = _select_cluster(conn, "cluster-3")
    # Contender's data preserved.
    assert row["status"] == "running"
    assert row["files_touched"] == json.dumps(["other.py"])


def test_predicted_files_stored_as_json_string(conn):
    predicted = ["src/foo.py", "src/bar.py", "tests/test_foo.py"]

    claim_cluster(
        conn,
        cluster_id="cluster-4",
        pr=1,
        predicted_files=predicted,
        created_ts="2026-05-22T10:00:00Z",
    )

    row = _select_cluster(conn, "cluster-4")
    stored = row["files_touched"]
    assert isinstance(stored, str)
    assert stored == json.dumps(predicted)
    # And it must round-trip back to the same list.
    assert json.loads(stored) == predicted


def test_pr_and_created_ts_are_caller_provided(conn):
    sentinel_ts = "1999-01-01T00:00:00Z"
    sentinel_pr = 31337

    claim_cluster(
        conn,
        cluster_id="cluster-5",
        pr=sentinel_pr,
        predicted_files=["x.py"],
        created_ts=sentinel_ts,
    )

    row = _select_cluster(conn, "cluster-5")
    assert row["pr"] == sentinel_pr
    assert row["created_ts"] == sentinel_ts
