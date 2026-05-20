#!/usr/bin/env bash
set -euo pipefail

# poll.sh — Persistent polling loop for babysit skill.
# Launched via the Monitor tool. Pure event collector:
#   - Fetches comments + failed builds via gh/bk on each cycle.
#   - Inserts new events into the `pending_events` SQLite table (idempotent
#     via INSERT OR IGNORE on the PRIMARY KEY).
#   - After a 30s quiet window per PR (no new pending events), emits one
#     `cluster_ready` JSON line on stdout so the coordinator can pick up
#     the buffered cluster.
#
# The coordinator (separate process) owns reads/deletes of `pending_events`
# and writes to `seen_events` / `clusters` / `worker_reports`.
#
# Usage: poll.sh [<pipeline-slug>] [--no-comments] [--no-builds] [--interval N]

###############################################################################
# Argument parsing
###############################################################################

PIPELINE=""
NO_COMMENTS=false
NO_BUILDS=false
INTERVAL=120
DEBOUNCE_SECONDS=30

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

###############################################################################
# State directory and database
###############################################################################

STATE_DIR="${CLAUDE_PLUGIN_DATA}/babysit"
STATE_DB="${STATE_DIR}/state.db"
SCHEMA_FILE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/schema.sql"

mkdir -p "$STATE_DIR"

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

# Run a SQL statement against state.db. Stdin = SQL.
db_exec() {
  sqlite3 "$STATE_DB"
}

# Run a SQL query and print result. Stdin = SQL.
db_query() {
  sqlite3 -noheader "$STATE_DB"
}

# Escape a string for safe inclusion in a single-quoted SQL literal.
sql_escape() {
  local s="$1"
  printf "%s" "${s//\'/\'\'}"
}

# Insert one pending event row. Args: kind, event_id, payload_json.
# event_id should be unique per (pr, kind). Payload is the raw JSON.
# INSERT OR IGNORE collapses duplicates on the primary key.
insert_pending_event() {
  local kind="$1"
  local event_id="$2"
  local payload="$3"
  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  local kind_esc event_id_esc payload_esc
  kind_esc=$(sql_escape "$kind")
  event_id_esc=$(sql_escape "$event_id")
  payload_esc=$(sql_escape "$payload")

  printf "INSERT OR IGNORE INTO pending_events (pr, kind, event_id, payload, received_ts) VALUES (%s, '%s', '%s', '%s', '%s');\n" \
    "$PR" "$kind_esc" "$event_id_esc" "$payload_esc" "$now" \
    | db_exec
}

###############################################################################
# Comment polling — write new threads into pending_events
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

  # Pull the set of already-seen comment-thread event_ids from seen_events
  # AND already-buffered ones from pending_events.
  local seen_json
  seen_json=$(db_query <<SQL
SELECT event_id FROM seen_events WHERE pr = $PR AND kind = 'comment_thread'
UNION
SELECT event_id FROM pending_events WHERE pr = $PR AND kind = 'comment_thread';
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

  # Insert each event into pending_events.
  while IFS= read -r evt; do
    [[ -z "$evt" ]] && continue
    local root_id
    root_id=$(echo "$evt" | jq -r '.thread_root_id')
    [[ -z "$root_id" || "$root_id" == "null" ]] && continue
    insert_pending_event "comment_thread" "$root_id" "$evt"
  done <<< "$events"
}

###############################################################################
# Build polling — write new build failures into pending_events
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

  # event_id = build_number (as string). Coordinator decides retry behaviour;
  # the poller just buffers each failed build observation once.
  local events
  events=$(echo "$raw_builds" | jq -c '
    .[] |
    select(.state == "failed") |
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

  while IFS= read -r evt; do
    [[ -z "$evt" ]] && continue
    local build_num
    build_num=$(echo "$evt" | jq -r '.build_number')
    [[ -z "$build_num" || "$build_num" == "null" ]] && continue
    insert_pending_event "build_failure" "$build_num" "$evt"
  done <<< "$events"
}

###############################################################################
# Debounce: emit cluster_ready when this PR has buffered events
# and no new arrivals within DEBOUNCE_SECONDS.
###############################################################################

emit_cluster_ready() {
  # For this PR, find max(received_ts) and count of rows in pending_events.
  local row
  row=$(db_query <<SQL
SELECT COUNT(*), COALESCE(MAX(received_ts), '')
  FROM pending_events
 WHERE pr = $PR;
SQL
)
  # sqlite3 separates columns with '|' by default.
  local count max_ts
  count=$(echo "$row" | awk -F'|' '{print $1}')
  max_ts=$(echo "$row" | awk -F'|' '{print $2}')

  [[ -z "$count" || "$count" == "0" ]] && return 0
  [[ -z "$max_ts" ]] && return 0

  # Compute age in seconds.
  local now_epoch max_epoch age
  now_epoch=$(date -u +%s)
  # macOS `date` and GNU `date` differ; try GNU form first, fall back to BSD.
  max_epoch=$(date -u -d "$max_ts" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$max_ts" +%s 2>/dev/null || echo "")
  [[ -z "$max_epoch" ]] && return 0

  age=$(( now_epoch - max_epoch ))
  if (( age > DEBOUNCE_SECONDS )); then
    printf '{"type":"cluster_ready","pr":%s,"event_count":%s}\n' "$PR" "$count"
  fi
}

###############################################################################
# Main loop
###############################################################################

while true; do
  poll_comments
  poll_builds
  emit_cluster_ready
  sleep "$INTERVAL"
done
