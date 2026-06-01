-- Babysit SQLite schema
-- Idempotent: safe to apply repeatedly against the same database.
-- Applied to: ${CLAUDE_PLUGIN_DATA}/babysit/state.db

PRAGMA journal_mode=WAL;

-- repo is NOT NULL: it is part of the PRIMARY KEY, and SQLite (unlike
-- the SQL standard) treats every NULL in a PK column as distinct, so a
-- NULL-repo row would never dedupe against another and the ledger would
-- silently re-emit. The sentinel 'legacy/unknown' is used for migrated
-- rows whose origin repo is unknown — never NULL.
CREATE TABLE IF NOT EXISTS seen_events (
    repo TEXT NOT NULL,
    pr INT,
    kind TEXT,
    event_id TEXT,
    ts TEXT,
    PRIMARY KEY (repo, pr, kind, event_id)
);

CREATE TABLE IF NOT EXISTS pipelines (
    repo TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    ts TEXT
);
