#!/usr/bin/env bash
# Babysit legacy-state migration.
#
# Imports per-PR JSON state files from historical babysit data directories into
# the single SQLite database at ${CLAUDE_PLUGIN_DATA}/babysit/state.db.
#
# Idempotent: re-running imports nothing new (UPSERT dedupes on (pr, kind, event_id)).
# Processed legacy files are moved into a dated backup folder so the source dir
# is left clean. Stale poll-*.lock files in legacy dirs are deleted.
#
# Inputs:  CLAUDE_PLUGIN_DATA env var (required)
# Outputs: ${CLAUDE_PLUGIN_DATA}/babysit/state.db populated, legacy files
#          relocated under ${CLAUDE_PLUGIN_DATA}/babysit/legacy-backup/<date>/...

set -euo pipefail

# --- Preconditions --------------------------------------------------------

if [[ -z "${CLAUDE_PLUGIN_DATA:-}" ]]; then
    echo "ERROR: CLAUDE_PLUGIN_DATA is not set. Export it before running migrate.sh." >&2
    exit 1
fi

for tool in sqlite3 jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: required tool '$tool' not found on PATH." >&2
        exit 1
    fi
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEMA_FILE="${SCRIPT_DIR}/schema.sql"
if [[ ! -f "$SCHEMA_FILE" ]]; then
    echo "ERROR: schema.sql not found at ${SCHEMA_FILE}." >&2
    exit 1
fi

BABYSIT_DIR="${CLAUDE_PLUGIN_DATA}/babysit"
DB_FILE="${BABYSIT_DIR}/state.db"
TODAY="$(date +%Y-%m-%d)"
BACKUP_ROOT="${BABYSIT_DIR}/legacy-backup/${TODAY}"

# --- (a) Ensure target dir; (b) init DB ----------------------------------

mkdir -p "$BABYSIT_DIR"

# Apply schema (CREATE IF NOT EXISTS makes this safe on every run).
# Redirect stdout to discard the "wal" echo from PRAGMA journal_mode.
sqlite3 "$DB_FILE" < "$SCHEMA_FILE" >/dev/null

# Silent v2->v3 migration: drop dispatcher/clustering tables and their indexes
# that existed in the v2 schema but are no longer used. DROP IF EXISTS is
# idempotent so this is a no-op on fresh v3 databases.
if [[ -f "$DB_FILE" ]]; then
    sqlite3 "$DB_FILE" <<'SQL' >/dev/null
DROP TABLE IF EXISTS clusters;
DROP TABLE IF EXISTS worker_reports;
DROP TABLE IF EXISTS pending_events;
DROP INDEX IF EXISTS idx_clusters_pr;
DROP INDEX IF EXISTS idx_clusters_status;
DROP INDEX IF EXISTS idx_pending_events_pr;
SQL
fi

# v2->v3: also remove orphan filesystem artifacts from the old dispatcher
# (per-burst dispatch logs, per-PR dispatch lockdirs). v3 never reads these;
# leaving them around just clutters the data dir. Globs are no-ops if nothing
# matches.
shopt -s nullglob
v2_logs=( "${BABYSIT_DIR}"/dispatch-*.log )
v2_lockdirs=( "${BABYSIT_DIR}"/dispatch-lock-*.d )
shopt -u nullglob
if (( ${#v2_logs[@]} > 0 )); then
    rm -f "${v2_logs[@]}"
fi
if (( ${#v2_lockdirs[@]} > 0 )); then
    rm -rf "${v2_lockdirs[@]}"
fi

NOW_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- (c)-(e) Walk legacy dirs --------------------------------------------

LEGACY_DIRS=(
    "${HOME}/.claude/plugins/data/jador-skills/babysit"
    "${CLAUDE_PLUGIN_DATA}/babysit"
    "${HOME}/.claude/plugin-data/babysit"
    "${HOME}/.claude/plugin_data/babysit"
    "${HOME}/.claude/data/babysit"
)

dirs_scanned=0
files_imported=0
events_imported=0
locks_removed=0

# Capture initial count of unique events so we can report only newly imported rows.
events_before=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM seen_events;")

for dir in "${LEGACY_DIRS[@]}"; do
    [[ -d "$dir" ]] || continue
    # Skip if this is the same dir as our backup root path (avoid recursive re-import).
    case "$dir" in
        "${BABYSIT_DIR}"|"${BABYSIT_DIR}/")
            # Allow scanning live dir, but skip the backup tree inside it.
            ;;
    esac
    dirs_scanned=$((dirs_scanned + 1))

    dir_basename="$(basename "$dir")"
    # Disambiguate identical basenames (all are "babysit") by hashing the path.
    safe_name="${dir_basename}-$(printf '%s' "$dir" | shasum | cut -c1-8)"
    backup_dir="${BACKUP_ROOT}/${safe_name}"

    # --- (e) Remove poll-*.lock files ---
    while IFS= read -r -d '' lock; do
        rm -f "$lock"
        locks_removed=$((locks_removed + 1))
    done < <(find "$dir" -maxdepth 1 -type f -name 'poll-*.lock' -print0 2>/dev/null)

    # --- (c) Import seen-comments and seen-builds JSON ---
    # Collect files first so we can batch them into a single sqlite transaction.
    comment_files=()
    while IFS= read -r -d '' f; do
        comment_files+=("$f")
    done < <(find "$dir" -maxdepth 1 -type f -name '*-seen-comments.json' -print0 2>/dev/null)

    build_files=()
    while IFS= read -r -d '' f; do
        build_files+=("$f")
    done < <(find "$dir" -maxdepth 1 -type f -name '*-seen-builds.json' -print0 2>/dev/null)

    total_files=$(( ${#comment_files[@]:-0} + ${#build_files[@]:-0} ))
    [[ "$total_files" -eq 0 ]] && continue

    # Build a single SQL transaction with all UPSERTs for this dir.
    sql_tmp="$(mktemp)"
    trap 'rm -f "$sql_tmp"' EXIT
    {
        echo "BEGIN;"
        for f in "${comment_files[@]:-}"; do
            [[ -z "$f" ]] && continue
            base="$(basename "$f")"
            # Strip "-seen-comments.json" suffix to recover the PR number.
            pr="${base%-seen-comments.json}"
            [[ "$pr" =~ ^[0-9]+$ ]] || continue
            # Extract event IDs as a stream of strings; tolerate empty/invalid JSON.
            # Comment files are arrays; if the file happens to be an object we take its keys.
            if ids=$(jq -er '
                if type == "array" then .[]
                elif type == "object" then keys[]
                else empty
                end
                | tostring
            ' "$f" 2>/dev/null); then
                while IFS= read -r id; do
                    [[ -z "$id" ]] && continue
                    # SQL-escape single quotes.
                    id_esc="${id//\'/\'\'}"
                    echo "INSERT OR IGNORE INTO seen_events (pr, kind, event_id, ts) VALUES (${pr}, 'comment', '${id_esc}', '${NOW_TS}');"
                done <<< "$ids"
            fi
        done
        for f in "${build_files[@]:-}"; do
            [[ -z "$f" ]] && continue
            base="$(basename "$f")"
            pr="${base%-seen-builds.json}"
            [[ "$pr" =~ ^[0-9]+$ ]] || continue
            # Build files are typically objects keyed by build number, but tolerate arrays too.
            if ids=$(jq -er '
                if type == "object" then keys[]
                elif type == "array" then .[]
                else empty
                end
                | tostring
            ' "$f" 2>/dev/null); then
                while IFS= read -r id; do
                    [[ -z "$id" ]] && continue
                    id_esc="${id//\'/\'\'}"
                    echo "INSERT OR IGNORE INTO seen_events (pr, kind, event_id, ts) VALUES (${pr}, 'build', '${id_esc}', '${NOW_TS}');"
                done <<< "$ids"
            fi
        done
        echo "COMMIT;"
    } > "$sql_tmp"

    sqlite3 "$DB_FILE" < "$sql_tmp"
    rm -f "$sql_tmp"
    trap - EXIT

    # --- (d) Move processed files into dated backup ---
    mkdir -p "$backup_dir"
    for f in "${comment_files[@]:-}" "${build_files[@]:-}"; do
        [[ -z "$f" ]] && continue
        [[ -f "$f" ]] || continue
        target="${backup_dir}/$(basename "$f")"
        # If target already exists from a prior run, append a numeric suffix.
        if [[ -e "$target" ]]; then
            n=1
            while [[ -e "${target}.${n}" ]]; do n=$((n + 1)); done
            target="${target}.${n}"
        fi
        mv "$f" "$target"
        files_imported=$((files_imported + 1))
    done
done

events_after=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM seen_events;")
events_imported=$((events_after - events_before))

# --- (f) Summary ----------------------------------------------------------

cat <<SUMMARY
Babysit legacy migration complete.
  Database:           ${DB_FILE}
  Dirs scanned:       ${dirs_scanned}
  Files imported:     ${files_imported}
  Unique events new:  ${events_imported}
  Locks removed:      ${locks_removed}
  Backup root:        ${BACKUP_ROOT}
SUMMARY
