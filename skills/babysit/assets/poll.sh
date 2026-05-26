#!/usr/bin/env bash
set -euo pipefail

# poll.sh — Persistent polling loop for babysit skill.
# Launched via the Monitor tool. Read-only event producer:
#   - Fetches comments + failed builds via gh/bk on each cycle.
#   - Dedupes against the `seen_events` SQLite table.
#   - For every unseen event, prints one JSON line to stdout (consumed by
#     the user session via Monitor) and records the event in `seen_events`
#     so it is not re-emitted on the next cycle.
#
# poll.sh is the only writer to `seen_events`. It does not spawn any
# downstream processes; the user session is responsible for reacting to
# emitted JSON lines (e.g. spawning a sub-agent per event).
#
# Usage: poll.sh [<pipeline-slug>] [--no-comments] [--no-builds] [--interval N]

###############################################################################
# Argument parsing
###############################################################################

PIPELINE=""
NO_COMMENTS=false
NO_BUILDS=false
INTERVAL=120

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-comments)
      NO_COMMENTS=true
      shift
      ;;
    --no-builds)
      NO_BUILDS=true
      shift
      ;;
    --interval)
      INTERVAL="${2:?--interval requires a value}"
      shift 2
      ;;
    -*)
      echo "Unknown flag: $1" >&2
      exit 1
      ;;
    *)
      # Positional: pipeline slug
      PIPELINE="$1"
      shift
      ;;
  esac
done

###############################################################################
# Startup: auto-detect repo, PR, branch from cwd
###############################################################################

REPO=""
PR=""
BRANCH=""

REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) || true
if [[ -z "$REPO" ]]; then
  echo '{"type":"error","kind":"init","message":"Failed to detect repository via gh repo view"}'
  exit 1
fi

PR_INFO=$(gh pr view --json number,headRefName --jq '.number,.headRefName' 2>/dev/null) || true
if [[ -z "$PR_INFO" ]]; then
  echo '{"type":"error","kind":"init","message":"Failed to detect PR number and branch via gh pr view"}'
  exit 1
fi

PR=$(echo "$PR_INFO" | head -n1)
BRANCH=$(echo "$PR_INFO" | tail -n1)

if [[ -z "$PR" || -z "$BRANCH" ]]; then
  echo '{"type":"error","kind":"init","message":"Failed to parse PR number or branch name"}'
  exit 1
fi

# Sanitize "owner/repo" → "owner__repo" for safe inclusion in filesystem
# artifact names. PR numbers are not unique across repos, so every per-PR
# path (pid file, log file) must also be scoped by repo.
REPO_SAFE="${REPO//\//__}"

###############################################################################
# State directory and database
###############################################################################

STATE_DIR="${CLAUDE_PLUGIN_DATA}/babysit"
STATE_DB="${STATE_DIR}/state.db"
ASSETS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA_FILE="${ASSETS_DIR}/schema.sql"
DB_PY="${ASSETS_DIR}/db.py"

mkdir -p "$STATE_DIR"

###############################################################################
# Self-log: tee stdout+stderr to a per-PR poll log for observability.
#
# poll.sh runs as a backgrounded shell when launched via `bash run_in_background`
# — the harness captures output to an ephemeral task buffer that the user can't
# tail. Without this, every cycle was invisible. Now: live progress at
# ${STATE_DIR}/poll-${REPO_SAFE}-${PR}.log, follow with `tail -f`.
#
# Process substitution + exec is bash 3.2-compatible.
###############################################################################
POLL_LOG="${STATE_DIR}/poll-${REPO_SAFE}-${PR}.log"
exec > >(tee -a "$POLL_LOG") 2>&1

# Timestamped log line. Used throughout the poll loop for visibility.
log() {
  printf '[%s] [poll] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

log "starting babysit poll loop"
log "repo=$REPO pr=$PR branch=$BRANCH pipeline=${PIPELINE:-<none>}"
log "interval=${INTERVAL}s"
log "state_db=$STATE_DB"
log "poll_log=$POLL_LOG"

# Self-record poller PID under a repo+PR-scoped name so Stop mode can find
# and terminate this process. The file holds our real numeric $$ — never a
# harness shell-id — so `kill -TERM` always works. Cleared on exit.
POLLER_PID_FILE="${STATE_DIR}/babysit-pid-${REPO_SAFE}-${PR}.pid"
echo "$$" > "$POLLER_PID_FILE"
trap 'log "poll loop exiting"; rm -f "$POLLER_PID_FILE"' EXIT

# Ensure schema is applied. Idempotent: schema.sql uses CREATE IF NOT EXISTS.
if [[ -f "$SCHEMA_FILE" ]]; then
  sqlite3 "$STATE_DB" < "$SCHEMA_FILE" 2>/dev/null || {
    echo '{"type":"error","kind":"init","message":"Failed to apply schema.sql to state.db"}'
    exit 1
  }
fi

###############################################################################
# sqlite helpers
###############################################################################
#
# All writes go through python3 db.py (bound parameters). Inline reads via
# sqlite3 are only used for queries that take integer PR values and string
# literals — never untrusted text — so they cannot be SQL-injected.

# Run a SQL query and print result. Stdin = SQL.
db_query() {
  sqlite3 -noheader "$STATE_DB"
}

###############################################################################
# Comment polling — emit JSON per new thread + record in seen_events
###############################################################################

poll_comments() {
  if [[ "$NO_COMMENTS" == "true" ]]; then
    return
  fi

  local raw_comments
  raw_comments=$(gh api "repos/${REPO}/pulls/${PR}/comments" --paginate 2>/dev/null) || {
    echo '{"type":"error","kind":"comments","pr":'"$PR"',"message":"GitHub API request failed"}'
    return
  }

  # Pull the set of already-seen comment-thread event_ids from seen_events.
  local seen_json
  seen_json=$(db_query <<SQL
SELECT event_id FROM seen_events WHERE pr = $PR AND kind = 'comment_thread';
SQL
)
  # Convert newline-separated ids into a JSON array.
  local seen_array
  if [[ -z "$seen_json" ]]; then
    seen_array="[]"
  else
    seen_array=$(printf "%s\n" "$seen_json" | jq -R . | jq -s 'map(tonumber? // .)')
  fi

  # Build one event per thread. Thread root id is the event_id.
  # Filter out babysit-agent self-authored comments. A thread is "new" if its
  # thread_root_id is not in $seen_array.
  local events
  events=$(echo "$raw_comments" | jq -c --argjson seen "$seen_array" '
    [.[]] as $all |

    # Threads keyed by root id
    ($all | map({ key: ((.in_reply_to_id // .id) | tostring), value: . }) | group_by(.key) |
      map({ thread_root_id: (.[0].key | tonumber), comments: [.[].value] })
    ) as $threads |

    $threads[] |
    .thread_root_id as $root_id |
    select(($seen | map(. == $root_id) | any) | not) |
    .comments |= sort_by(.created_at) |

    # Skip threads where every comment is babysit-agent self-authored
    select(
      any(.comments[]; (.body // "") | contains("<!-- babysit-agent -->") | not)
    ) |

    ( [ .comments[] | select(.id == $root_id) ] | first // .comments[0] ) as $root |

    {
      type: "comment_thread",
      pr: '"$PR"',
      thread_root_id: $root_id,
      comments: [ .comments[] | {
        id: .id,
        user: { login: .user.login },
        body: .body,
        created_at: .created_at,
        in_reply_to_id: .in_reply_to_id
      }],
      file: ($root.path // null),
      line: (($root.line // $root.original_line) // null),
      diff_hunk: ($root.diff_hunk // null)
    }
  ' 2>/dev/null) || return 0

  # For each unseen event: print the JSON payload to stdout (the user
  # session reads these via Monitor), then record the (pr, kind, event_id)
  # in seen_events so we never re-emit it.
  local now emitted=0
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  while IFS= read -r evt; do
    [[ -z "$evt" ]] && continue
    local root_id
    root_id=$(echo "$evt" | jq -r '.thread_root_id')
    [[ -z "$root_id" || "$root_id" == "null" ]] && continue
    printf '%s\n' "$evt"
    python3 "$DB_PY" insert_seen \
      --db "$STATE_DB" \
      --pr "$PR" \
      --kind "comment_thread" \
      --event-id "$root_id" \
      --ts "$now" >/dev/null
    emitted=$((emitted + 1))
    log "emitted comment_thread event_id=$root_id"
  done <<< "$events"
  if (( emitted > 0 )); then log "poll_comments: $emitted new comment thread(s) emitted"; fi
}

###############################################################################
# Build polling — emit JSON per new build failure + record in seen_events
###############################################################################

poll_builds() {
  if [[ "$NO_BUILDS" == "true" ]]; then
    return
  fi
  if [[ -z "$PIPELINE" ]]; then
    return
  fi

  local raw_builds
  raw_builds=$(bk build list --pipeline "$PIPELINE" --branch "$BRANCH" --json 2>/dev/null) || {
    echo '{"type":"error","kind":"builds","pr":'"$PR"',"message":"Buildkite CLI request failed"}'
    return
  }

  # Pull the set of already-seen build-failure event_ids from seen_events.
  # event_id = build_number (stored as TEXT in seen_events).
  local seen_json
  seen_json=$(db_query <<SQL
SELECT event_id FROM seen_events WHERE pr = $PR AND kind = 'build_failure';
SQL
)
  local seen_array
  if [[ -z "$seen_json" ]]; then
    seen_array="[]"
  else
    seen_array=$(printf "%s\n" "$seen_json" | jq -R . | jq -s '.')
  fi

  # event_id = build_number (as string). The session decides retry behaviour;
  # the poller just emits each failed build observation once.
  local events
  events=$(echo "$raw_builds" | jq -c --argjson seen "$seen_array" '
    .[] |
    select(.state == "failed") |
    . as $build |
    select(($seen | map(. == ($build.number | tostring)) | any) | not) |
    {
      type: "build_failure",
      pr: '"$PR"',
      build_number: .number,
      state: "failed",
      pipeline: "'"$PIPELINE"'",
      branch: "'"$BRANCH"'",
      jobs: [.jobs[]? | select(.state != "passed") | {id: .id, name: .name, state: .state}]
    }
  ' 2>/dev/null) || return 0

  # For each unseen event: print JSON to stdout, then record in seen_events.
  local now emitted=0
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  while IFS= read -r evt; do
    [[ -z "$evt" ]] && continue
    local build_num
    build_num=$(echo "$evt" | jq -r '.build_number')
    [[ -z "$build_num" || "$build_num" == "null" ]] && continue
    printf '%s\n' "$evt"
    python3 "$DB_PY" insert_seen \
      --db "$STATE_DB" \
      --pr "$PR" \
      --kind "build_failure" \
      --event-id "$build_num" \
      --ts "$now" >/dev/null
    emitted=$((emitted + 1))
    log "emitted build_failure build=$build_num"
  done <<< "$events"
  if (( emitted > 0 )); then log "poll_builds: $emitted new build failure(s) emitted"; fi
}

###############################################################################
# Main loop
###############################################################################

CYCLE=0
while true; do
  CYCLE=$((CYCLE + 1))
  log "cycle $CYCLE start"
  poll_comments
  poll_builds
  # Cheap status snapshot once per cycle.
  seen_count=$(sqlite3 "$STATE_DB" "SELECT COUNT(*) FROM seen_events WHERE pr=$PR" 2>/dev/null || echo 0)
  log "cycle $CYCLE end — seen=$seen_count sleep=${INTERVAL}s"
  sleep "$INTERVAL"
done
