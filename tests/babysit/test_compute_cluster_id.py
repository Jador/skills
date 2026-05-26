"""Tests for ``compute_cluster_id`` — deterministic Guard 2 hash.

Behavioural contract:

1. Determinism: same input twice → same hex output.
2. Order independence: shuffling the event_ids list does not change the hash.
3. PR sensitivity: same event_ids, different PR → different hash.
4. Event-id sensitivity: same PR, different event_ids → different hash.
5. Empty event_ids list yields a stable hash (no crash).
6. CLI: ``compute_cluster_id --pr N --json-stdin`` reads
   ``{"event_ids":[{"kind":"...","event_id":"..."}]}`` and emits
   ``{"ok":true,"cluster_id":"<hex>"}`` on stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from skills.babysit.assets.db import compute_cluster_id

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PY = REPO_ROOT / "skills" / "babysit" / "assets" / "db.py"


# ---------- function-level ----------

def test_same_input_twice_returns_same_hash():
    event_ids = [("comment_thread", "42"), ("build_failure", "99")]
    a = compute_cluster_id(42, event_ids)
    b = compute_cluster_id(42, event_ids)
    assert a == b
    assert isinstance(a, str)
    assert len(a) > 0
    # hex chars only
    int(a, 16)


def test_order_independence():
    a = compute_cluster_id(
        42, [("comment_thread", "42"), ("build_failure", "99")]
    )
    b = compute_cluster_id(
        42, [("build_failure", "99"), ("comment_thread", "42")]
    )
    assert a == b


def test_different_pr_different_hash_same_event_ids():
    event_ids = [("comment_thread", "42"), ("build_failure", "99")]
    a = compute_cluster_id(42, event_ids)
    b = compute_cluster_id(43, event_ids)
    assert a != b


def test_different_event_ids_different_hash_same_pr():
    a = compute_cluster_id(42, [("comment_thread", "42")])
    b = compute_cluster_id(42, [("comment_thread", "43")])
    assert a != b


def test_empty_event_ids_list_returns_stable_hash():
    a = compute_cluster_id(42, [])
    b = compute_cluster_id(42, [])
    assert a == b
    assert isinstance(a, str)
    assert len(a) > 0
    int(a, 16)


# ---------- CLI ----------

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


def test_cli_compute_cluster_id_json_stdin():
    body = json.dumps(
        {"event_ids": [{"kind": "comment_thread", "event_id": "42"}]}
    )
    result = _run(
        ["compute_cluster_id", "--pr", "42", "--json-stdin"],
        stdin=body,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert "cluster_id" in out
    cid = out["cluster_id"]
    assert isinstance(cid, str)
    assert len(cid) > 0
    int(cid, 16)
    # CLI result matches function-level result.
    assert cid == compute_cluster_id(42, [("comment_thread", "42")])
