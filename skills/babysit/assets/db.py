"""babysit DB helper — single source of truth for SQL writes.

All operations take an open sqlite3.Connection. The CLI in __main__
wraps these for shell callers.
"""
from __future__ import annotations
import json
import sqlite3


def insert_seen_event(
    conn: sqlite3.Connection,
    repo: str,
    pr: int,
    kind: str,
    event_id: str,
    ts: str,
) -> int:
    """INSERT OR IGNORE one row into seen_events. Returns rows affected.

    Used by poll.sh to dedupe events: a rowcount of 1 means this is a new
    event the session has not yet seen; 0 means it was already recorded.
    Repo-scoped so the same PR number across different repos cannot collide.
    """
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_events (repo, pr, kind, event_id, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (repo, pr, kind, event_id, ts),
        )
    return cur.rowcount


def purge_pr(conn: sqlite3.Connection, repo: str, pr: int) -> dict:
    """Delete every trace of one (repo, PR) pair from seen_events.

    Returns per-table delete counts. Scoped by repo so purging PR #42 in
    org-a/foo never touches PR #42 in org-b/bar.
    """
    counts = {}
    with conn:
        cur = conn.execute(
            "DELETE FROM seen_events WHERE repo = ? AND pr = ?",
            (repo, pr),
        )
        counts["seen_events"] = cur.rowcount
    return counts


def list_distinct_prs(conn: sqlite3.Connection) -> list[dict]:
    """Distinct (repo, pr) pairs ever recorded in seen_events.

    Sorted ascending by (repo, pr). Returns list of dicts so the CLI JSON
    output retains both fields.
    """
    cur = conn.execute(
        "SELECT DISTINCT repo, pr FROM seen_events ORDER BY repo ASC, pr ASC"
    )
    return [{"repo": row[0], "pr": row[1]} for row in cur.fetchall()]


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

    sp = sub.add_parser("insert_seen")
    _add_db(sp)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--pr", type=int, required=True)
    sp.add_argument("--kind", required=True)
    sp.add_argument("--event-id", required=True)
    sp.add_argument("--ts", required=True)

    sp = sub.add_parser("purge_pr")
    _add_db(sp)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--pr", type=int, required=True)

    sp = sub.add_parser("list_distinct_prs")
    _add_db(sp)

    sp = sub.add_parser("vacuum")
    _add_db(sp)

    return p


def _dispatch(args, conn):
    """Route parsed args to the underlying op and return a result dict."""
    op = args.op

    if op == "insert_seen":
        n = insert_seen_event(
            conn,
            repo=args.repo,
            pr=args.pr,
            kind=args.kind,
            event_id=args.event_id,
            ts=args.ts,
        )
        return {"ok": True, "rows_affected": n}

    if op == "purge_pr":
        counts = purge_pr(conn, repo=args.repo, pr=args.pr)
        return {"ok": True, "counts": counts}

    if op == "list_distinct_prs":
        prs = list_distinct_prs(conn)
        return {"ok": True, "prs": prs}

    if op == "vacuum":
        vacuum(conn)
        return {"ok": True}

    raise ValueError(f"unknown op: {op}")


def main(argv=None) -> int:
    """CLI entrypoint. Returns process exit code."""
    import os
    parser = _build_parser()
    args = parser.parse_args(argv)

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
