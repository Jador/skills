-- Babysit SQLite schema
-- Idempotent: safe to apply repeatedly against the same database.
-- Applied to: ${CLAUDE_PLUGIN_DATA}/babysit/state.db

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS seen_events (
    pr INT,
    kind TEXT,
    event_id TEXT,
    ts TEXT,
    PRIMARY KEY (pr, kind, event_id)
);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT PRIMARY KEY,
    pr INT,
    created_ts TEXT,
    status TEXT CHECK (status IN ('pending', 'running', 'done', 'abandoned')),
    files_touched TEXT
);

CREATE TABLE IF NOT EXISTS worker_reports (
    cluster_id TEXT PRIMARY KEY,
    resolved_ids TEXT,
    unresolved_ids TEXT,
    files_touched TEXT,
    commit_sha TEXT,
    summary TEXT,
    ts TEXT
);

CREATE TABLE IF NOT EXISTS pending_events (
    pr INT,
    kind TEXT,
    event_id TEXT,
    payload TEXT,
    received_ts TEXT,
    PRIMARY KEY (pr, kind, event_id)
);

CREATE TABLE IF NOT EXISTS pipelines (
    repo TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    ts TEXT
);

CREATE INDEX IF NOT EXISTS idx_clusters_pr ON clusters (pr);
CREATE INDEX IF NOT EXISTS idx_clusters_status ON clusters (status);
CREATE INDEX IF NOT EXISTS idx_pending_events_pr ON pending_events (pr);
