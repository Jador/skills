"""babysit DB helper — single source of truth for SQL writes.

All operations take an open sqlite3.Connection. The CLI in __main__ (added
later) wraps these for shell callers.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3


def compute_cluster_id(pr: int, event_ids: list[tuple[str, str]]) -> str:
    """Deterministic cluster_id from (pr, sorted (kind, event_id) tuples).

    Stable across invocations and insertion orders. Used as Guard 2 against
    concurrent-dispatcher claim races.
    """
    canonical = sorted(event_ids)
    h = hashlib.sha256()
    h.update(str(pr).encode("utf-8"))
    h.update(b"|")
    for kind, eid in canonical:
        h.update(kind.encode("utf-8"))
        h.update(b":")
        h.update(eid.encode("utf-8"))
        h.update(b";")
    return h.hexdigest()[:16]  # 16 hex chars = 64-bit collision resistance, plenty


def insert_pending_event(
    conn: sqlite3.Connection,
    pr: int,
    kind: str,
    event_id: str,
    payload: str,
    received_ts: str,
) -> int:
    """INSERT OR IGNORE one row into pending_events. Returns rows affected."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO pending_events "
        "(pr, kind, event_id, payload, received_ts) VALUES (?, ?, ?, ?, ?)",
        (pr, kind, event_id, payload, received_ts),
    )
    conn.commit()
    return cur.rowcount


def read_pending_events(conn: sqlite3.Connection, pr: int) -> list[dict]:
    """Return pending_events rows for one PR, oldest first."""
    cur = conn.execute(
        "SELECT pr, kind, event_id, payload, received_ts "
        "FROM pending_events WHERE pr = ? ORDER BY received_ts ASC",
        (pr,),
    )
    return [dict(row) for row in cur.fetchall()]


def claim_cluster(
    conn: sqlite3.Connection,
    cluster_id: str,
    pr: int,
    predicted_files: list[str],
    created_ts: str,
) -> bool:
    """Atomic single-winner cluster claim.

    Two-step: INSERT OR IGNORE the cluster row with status='pending', then
    UPDATE to 'running'. The UPDATE matches rows in {'pending', 'abandoned',
    'done'} so that:
      - First-time claims succeed via the freshly-inserted 'pending' row.
      - Recovery claims (prior dispatcher crashed → 'abandoned' by Clean
        mode) succeed and re-run the worker.
      - Re-entry claims (event re-arrived in pending_events after prior
        resolution, e.g. a still-failing build) succeed and re-run the
        worker — bounded by the dispatcher's per-cluster retry-cap logic.

    The cursor's rowcount is the single-winner oracle (==1 means we won
    the race; concurrent dispatchers collide here).
    """
    files_json = json.dumps(predicted_files)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO clusters "
            "(cluster_id, pr, created_ts, status, files_touched) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (cluster_id, pr, created_ts, files_json),
        )
        cur = conn.execute(
            "UPDATE clusters SET status='running', created_ts=?, "
            "files_touched=? "
            "WHERE cluster_id = ? "
            "AND status IN ('pending', 'abandoned', 'done')",
            (created_ts, files_json, cluster_id),
        )
    return cur.rowcount == 1


def commit_worker_report(
    conn: sqlite3.Connection,
    cluster_id: str,
    pr: int,
    resolved_event_ids: list[dict],
    unresolved_event_ids: list[dict],
    files_touched: list[str],
    commit_sha: str,
    summary: str,
    now_ts: str,
) -> dict:
    """Atomically persist a worker's result.

    Inside one transaction:
      - INSERT OR IGNORE every resolved tuple into seen_events.
      - INSERT OR REPLACE one worker_reports row (overwrites prior row on
        duplicate cluster_id, supporting re-claim of an already-resolved
        cluster).
      - UPDATE clusters.status='done', clusters.files_touched=JSON.
      - DELETE matching (pr, kind, event_id) from pending_events.

    Returns {"seen_inserted": N, "pending_deleted": M}.
    """
    seen_inserted = 0
    pending_deleted = 0
    files_json = json.dumps(files_touched)
    resolved_json = json.dumps(resolved_event_ids)
    unresolved_json = json.dumps(unresolved_event_ids)
    with conn:
        for ev in resolved_event_ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO seen_events (pr, kind, event_id, ts) "
                "VALUES (?, ?, ?, ?)",
                (pr, ev["kind"], ev["event_id"], now_ts),
            )
            seen_inserted += cur.rowcount
        # INSERT OR REPLACE so a re-claim of a previously-done cluster
        # (event re-entered pending_events; cluster_id is deterministic)
        # overwrites the prior worker outcome rather than UNIQUE-failing.
        conn.execute(
            "INSERT OR REPLACE INTO worker_reports "
            "(cluster_id, resolved_ids, unresolved_ids, files_touched, "
            "commit_sha, summary, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cluster_id, resolved_json, unresolved_json, files_json,
             commit_sha, summary, now_ts),
        )
        conn.execute(
            "UPDATE clusters SET status='done', files_touched=? "
            "WHERE cluster_id = ?",
            (files_json, cluster_id),
        )
        for ev in resolved_event_ids:
            cur = conn.execute(
                "DELETE FROM pending_events "
                "WHERE pr = ? AND kind = ? AND event_id = ?",
                (pr, ev["kind"], ev["event_id"]),
            )
            pending_deleted += cur.rowcount
    return {"seen_inserted": seen_inserted, "pending_deleted": pending_deleted}


def purge_pr(conn: sqlite3.Connection, pr: int) -> dict:
    """Delete every trace of one PR. Single transaction across 4 tables.

    Returns per-table delete counts.
    """
    counts = {}
    with conn:
        # Delete worker_reports first via clusters join (clusters has the pr column)
        cur = conn.execute(
            "DELETE FROM worker_reports "
            "WHERE cluster_id IN (SELECT cluster_id FROM clusters WHERE pr = ?)",
            (pr,),
        )
        counts["worker_reports"] = cur.rowcount
        cur = conn.execute("DELETE FROM clusters WHERE pr = ?", (pr,))
        counts["clusters"] = cur.rowcount
        cur = conn.execute("DELETE FROM seen_events WHERE pr = ?", (pr,))
        counts["seen_events"] = cur.rowcount
        cur = conn.execute("DELETE FROM pending_events WHERE pr = ?", (pr,))
        counts["pending_events"] = cur.rowcount
    return counts


def reap_stale_clusters(
    conn: sqlite3.Connection,
    pr: int | None = None,
    live_cluster_ids: list[str] | None = None,
) -> int:
    """Mark running clusters as abandoned. Returns reap count.

    pr=None scopes to all PRs (used by Clean mode).
    live_cluster_ids is a whitelist of cluster_ids to preserve.
    """
    where = ["status = 'running'"]
    params: list = []
    if pr is not None:
        where.append("pr = ?")
        params.append(pr)
    if live_cluster_ids:
        placeholders = ",".join("?" * len(live_cluster_ids))
        where.append(f"cluster_id NOT IN ({placeholders})")
        params.extend(live_cluster_ids)
    sql = "UPDATE clusters SET status='abandoned' WHERE " + " AND ".join(where)
    with conn:
        cur = conn.execute(sql, params)
    return cur.rowcount


def list_distinct_prs(conn: sqlite3.Connection) -> list[int]:
    """Distinct PR numbers ever recorded in seen_events, sorted asc."""
    cur = conn.execute("SELECT DISTINCT pr FROM seen_events ORDER BY pr ASC")
    return [row["pr"] if hasattr(row, "keys") else row[0] for row in cur.fetchall()]


def vacuum(conn: sqlite3.Connection) -> None:
    """Reclaim space. VACUUM cannot run inside a transaction."""
    conn.commit()  # Close any open transaction
    conn.execute("VACUUM")


# ---------------------------------------------------------------------------
# CLI dispatcher
# ---------------------------------------------------------------------------

def _emit(obj: dict) -> None:
    """Print one JSON object on a single line to stdout."""
    import sys as _sys
    _sys.stdout.write(json.dumps(obj) + "\n")
    _sys.stdout.flush()


def _build_parser():
    import argparse
    p = argparse.ArgumentParser(prog="db.py", description="babysit DB CLI")
    # --db can appear before OR after the subcommand. Both top-level and
    # per-subparser register the same flag with a shared dest.
    p.add_argument("--db", default=None, help="sqlite DB file path")

    def _add_db(parser):
        parser.add_argument("--db", default=None, dest="db",
                            help="sqlite DB file path")

    sub = p.add_subparsers(dest="op", required=True)

    sp = sub.add_parser("insert_pending")
    _add_db(sp)
    sp.add_argument("--pr", type=int, required=True)
    sp.add_argument("--kind", required=True)
    sp.add_argument("--event-id", required=True)
    sp.add_argument("--received-ts", required=True)
    sp.add_argument("--payload", default=None)
    sp.add_argument("--json-stdin", action="store_true")

    sp = sub.add_parser("read_pending")
    _add_db(sp)
    sp.add_argument("--pr", type=int, required=True)

    sp = sub.add_parser("claim_cluster")
    _add_db(sp)
    sp.add_argument("--cluster-id", required=True)
    sp.add_argument("--pr", type=int, required=True)
    sp.add_argument("--created-ts", required=True)
    sp.add_argument("--predicted-files", default=None,
                    help="JSON list of predicted file paths")
    sp.add_argument("--json-stdin", action="store_true")

    sp = sub.add_parser("commit_worker_report")
    _add_db(sp)
    sp.add_argument("--cluster-id", required=True)
    sp.add_argument("--pr", type=int, required=True)
    sp.add_argument("--commit-sha", required=True)
    sp.add_argument("--summary", required=True)
    sp.add_argument("--now-ts", required=True)
    # stdin JSON is implicit for this op; no flag needed.

    sp = sub.add_parser("purge_pr")
    _add_db(sp)
    sp.add_argument("--pr", type=int, required=True)

    sp = sub.add_parser("list_distinct_prs")
    _add_db(sp)

    sp = sub.add_parser("reap_stale_clusters")
    _add_db(sp)
    sp.add_argument("--pr", type=int, default=None)
    # Optional live cluster ids via stdin (JSON list).

    sp = sub.add_parser("vacuum")
    _add_db(sp)

    sp = sub.add_parser("compute_cluster_id")
    _add_db(sp)
    sp.add_argument("--pr", type=int, required=True)
    sp.add_argument("--json-stdin", action="store_true",
                    help="Read {\"event_ids\":[{\"kind\":...,\"event_id\":...}]} from stdin")

    return p


def _dispatch(args, conn):
    """Route parsed args to the underlying op and return a result dict."""
    import sys as _sys
    op = args.op

    if op == "insert_pending":
        if args.json_stdin:
            payload = _sys.stdin.read()
            # Validate it's parseable JSON; we store the raw text.
            json.loads(payload)
        else:
            payload = args.payload if args.payload is not None else ""
        n = insert_pending_event(
            conn,
            pr=args.pr,
            kind=args.kind,
            event_id=args.event_id,
            payload=payload,
            received_ts=args.received_ts,
        )
        return {"ok": True, "rows_affected": n}

    if op == "read_pending":
        rows = read_pending_events(conn, pr=args.pr)
        return {"ok": True, "rows": rows}

    if op == "claim_cluster":
        if args.json_stdin:
            predicted = json.loads(_sys.stdin.read())
        elif args.predicted_files is not None:
            predicted = json.loads(args.predicted_files)
        else:
            predicted = []
        claimed = claim_cluster(
            conn,
            cluster_id=args.cluster_id,
            pr=args.pr,
            predicted_files=predicted,
            created_ts=args.created_ts,
        )
        return {"ok": True, "claimed": bool(claimed)}

    if op == "commit_worker_report":
        body = _sys.stdin.read()
        parsed = json.loads(body)
        result = commit_worker_report(
            conn,
            cluster_id=args.cluster_id,
            pr=args.pr,
            resolved_event_ids=parsed.get("resolved_event_ids", []),
            unresolved_event_ids=parsed.get("unresolved_event_ids", []),
            files_touched=parsed.get("files_touched", []),
            commit_sha=args.commit_sha,
            summary=args.summary,
            now_ts=args.now_ts,
        )
        return {
            "ok": True,
            "seen_inserted": result["seen_inserted"],
            "pending_deleted": result["pending_deleted"],
        }

    if op == "purge_pr":
        counts = purge_pr(conn, pr=args.pr)
        return {"ok": True, "counts": counts}

    if op == "list_distinct_prs":
        prs = list_distinct_prs(conn)
        return {"ok": True, "prs": prs}

    if op == "reap_stale_clusters":
        # Optional list of live cluster ids on stdin (JSON array). If
        # stdin is a tty or empty, treat as no whitelist.
        live = None
        if not _sys.stdin.isatty():
            raw = _sys.stdin.read()
            if raw.strip():
                live = json.loads(raw)
        n = reap_stale_clusters(conn, pr=args.pr, live_cluster_ids=live)
        return {"ok": True, "reaped": n}

    if op == "vacuum":
        vacuum(conn)
        return {"ok": True}

    if op == "compute_cluster_id":
        if args.json_stdin:
            parsed = json.loads(_sys.stdin.read())
        else:
            parsed = {"event_ids": []}
        tuples = [
            (ev["kind"], ev["event_id"]) for ev in parsed.get("event_ids", [])
        ]
        cid = compute_cluster_id(pr=args.pr, event_ids=tuples)
        return {"ok": True, "cluster_id": cid}

    raise ValueError(f"unknown op: {op}")


_NO_DB_OPS = {"compute_cluster_id"}


def main(argv=None) -> int:
    """CLI entrypoint. Returns process exit code."""
    import os
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Pure-function ops do not need a DB connection.
    if args.op in _NO_DB_OPS:
        try:
            result = _dispatch(args, conn=None)
        except Exception as e:
            _emit({"ok": False, "error": str(e), "exit_code": 2})
            return 2
        _emit(result)
        return 0

    db_path = args.db or os.environ.get("BABYSIT_STATE_DB")
    if not db_path:
        _emit({"ok": False, "error": "DB path not provided"})
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            result = _dispatch(args, conn)
        except Exception as e:
            _emit({"ok": False, "error": str(e), "exit_code": 2})
            return 2
        _emit(result)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.exit(main())
