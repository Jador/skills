"""Tests for ``mechanical_precluster`` — Option C pre-clustering helper.

Behavioural contract:

1. Empty input → empty output.
2. Single event → one cluster of one.
3. Two events with overlapping ``predicted_files`` (extracted from payload
   JSON) → one cluster of two.
4. Two events with disjoint files, different ``kind``, far-apart times →
   two singleton clusters.
5. Two events with disjoint files, SAME ``kind``, ``received_ts`` within
   ``window_seconds`` → one cluster of two (kind-window rule).
6. Transitive grouping: A↔B share file X, B↔C share file Y, A and C share
   nothing directly → all three end up in one cluster.
7. CLI smoke: pre-seed DB with 3 events via ``insert_pending``, then call
   ``precluster_events --db <path> --pr <N>`` and verify response shape
   ``{"ok":true,"clusters":[[...],...]}``.

Event shape mirrors ``read_pending_events`` output: dict with keys ``pr``,
``kind``, ``event_id``, ``payload`` (JSON string), ``received_ts``
(ISO 8601 UTC string). The helper parses ``payload`` for a ``file``
(string) or ``files`` (list) field to derive per-event file lists.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from skills.babysit.assets.db import mechanical_precluster

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PY = REPO_ROOT / "skills" / "babysit" / "assets" / "db.py"
SCHEMA_PATH = REPO_ROOT / "skills" / "babysit" / "assets" / "schema.sql"


def _event(
    *,
    pr: int = 7,
    kind: str = "comment_thread",
    event_id: str,
    files: list[str] | None = None,
    file: str | None = None,
    received_ts: str = "2026-05-22T18:30:00Z",
    extra_payload: dict | None = None,
) -> dict:
    """Build an event dict in the shape ``read_pending_events`` returns."""
    payload: dict = {}
    if file is not None:
        payload["file"] = file
    if files is not None:
        payload["files"] = files
    if extra_payload:
        payload.update(extra_payload)
    return {
        "pr": pr,
        "kind": kind,
        "event_id": event_id,
        "payload": json.dumps(payload),
        "received_ts": received_ts,
    }


# ---------- function-level ----------

def test_empty_input_returns_empty_list():
    assert mechanical_precluster([]) == []


def test_single_event_returns_one_singleton_cluster():
    e = _event(event_id="evt-1", file="src/a.py")
    out = mechanical_precluster([e])
    assert len(out) == 1
    assert len(out[0]) == 1
    assert out[0][0]["event_id"] == "evt-1"


def test_overlapping_files_join_into_one_cluster():
    e1 = _event(
        event_id="evt-1",
        files=["src/a.py", "src/b.py"],
        received_ts="2026-05-22T18:30:00Z",
    )
    e2 = _event(
        event_id="evt-2",
        # Different kind + far-apart time so ONLY file-overlap can group.
        kind="build_failure",
        files=["src/b.py", "src/c.py"],
        received_ts="2026-05-22T20:00:00Z",
    )
    out = mechanical_precluster([e1, e2])
    assert len(out) == 1
    ids = sorted(ev["event_id"] for ev in out[0])
    assert ids == ["evt-1", "evt-2"]


def test_disjoint_files_different_kind_far_apart_times_stay_separate():
    e1 = _event(
        event_id="evt-1",
        kind="comment_thread",
        file="src/a.py",
        received_ts="2026-05-22T18:30:00Z",
    )
    e2 = _event(
        event_id="evt-2",
        kind="build_failure",
        file="src/z.py",
        received_ts="2026-05-22T20:00:00Z",
    )
    out = mechanical_precluster([e1, e2], window_seconds=30)
    assert len(out) == 2
    # Each cluster has exactly one event.
    assert all(len(c) == 1 for c in out)
    ids = sorted(c[0]["event_id"] for c in out)
    assert ids == ["evt-1", "evt-2"]


def test_same_kind_within_window_join_even_without_file_overlap():
    e1 = _event(
        event_id="evt-1",
        kind="build_failure",
        file="src/a.py",
        received_ts="2026-05-22T18:30:00Z",
    )
    e2 = _event(
        event_id="evt-2",
        kind="build_failure",
        file="src/z.py",  # no file overlap
        received_ts="2026-05-22T18:30:20Z",  # 20s later, within 30s window
    )
    out = mechanical_precluster([e1, e2], window_seconds=30)
    assert len(out) == 1
    ids = sorted(ev["event_id"] for ev in out[0])
    assert ids == ["evt-1", "evt-2"]


def test_transitive_file_overlap_merges_three_events():
    # A↔B share X; B↔C share Y; A and C share nothing directly.
    # Different kinds + far-apart times block the kind-window rule, so
    # the merge MUST come from file-overlap transitive closure.
    a = _event(
        event_id="A",
        kind="comment_thread",
        files=["X"],
        received_ts="2026-05-22T18:30:00Z",
    )
    b = _event(
        event_id="B",
        kind="build_failure",
        files=["X", "Y"],
        received_ts="2026-05-22T19:30:00Z",
    )
    c = _event(
        event_id="C",
        kind="review",
        files=["Y"],
        received_ts="2026-05-22T20:30:00Z",
    )
    out = mechanical_precluster([a, b, c], window_seconds=30)
    assert len(out) == 1
    ids = sorted(ev["event_id"] for ev in out[0])
    assert ids == ["A", "B", "C"]


# ---------- CLI smoke ----------

def _bootstrap_db(db_path: Path) -> None:
    """Apply schema.sql to a fresh sqlite DB file."""
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(SCHEMA_PATH.read_text())
        connection.commit()
    finally:
        connection.close()


def _run(args, *, stdin: str | None = None):
    env = os.environ.copy()
    env.pop("BABYSIT_STATE_DB", None)
    return subprocess.run(
        [sys.executable, str(DB_PY), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    _bootstrap_db(path)
    return path


def test_cli_precluster_events_returns_clusters(db_file: Path):
    # Seed 3 events: two share file X (must cluster); third stands alone.
    e1_payload = json.dumps({"files": ["src/a.py", "src/X.py"]})
    e2_payload = json.dumps({"files": ["src/X.py", "src/b.py"]})
    e3_payload = json.dumps({"files": ["src/z.py"]})

    for evid, payload, ts in [
        ("evt-1", e1_payload, "2026-05-22T18:30:00Z"),
        ("evt-2", e2_payload, "2026-05-22T19:30:00Z"),
        ("evt-3", e3_payload, "2026-05-22T22:00:00Z"),
    ]:
        result = _run([
            "insert_pending",
            "--db", str(db_file),
            "--pr", "42",
            # Mix kinds + space them out so file-overlap is the only join path
            # for evt-1 and evt-2, and evt-3 has no path to either.
            "--kind", "comment_thread" if evid != "evt-2" else "build_failure",
            "--event-id", evid,
            "--received-ts", ts,
            "--payload", payload,
        ])
        assert result.returncode == 0, result.stderr

    result = _run([
        "precluster_events",
        "--db", str(db_file),
        "--pr", "42",
    ])
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert "clusters" in out
    clusters = out["clusters"]
    assert isinstance(clusters, list)
    # Each cluster must be a list of event dicts.
    for c in clusters:
        assert isinstance(c, list)
        for ev in c:
            assert "event_id" in ev
            assert "kind" in ev
            assert "payload" in ev
            assert "received_ts" in ev

    # evt-1 and evt-2 share file X → same cluster; evt-3 alone.
    cluster_by_id = {
        ev["event_id"]: idx
        for idx, c in enumerate(clusters)
        for ev in c
    }
    assert cluster_by_id["evt-1"] == cluster_by_id["evt-2"]
    assert cluster_by_id["evt-3"] != cluster_by_id["evt-1"]
    assert len(clusters) == 2
