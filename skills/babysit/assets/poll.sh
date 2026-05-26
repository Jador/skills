#!/usr/bin/env bash
set -euo pipefail

# poll.sh — Persistent polling loop for babysit skill.
# Launched via the Monitor tool. Event collector + dispatcher launcher:
#   - Fetches comments + failed builds via gh/bk on each cycle.
#   - Inserts new events into the `pending_events` SQLite table (idempotent
#     via INSERT OR IGNORE on the PRIMARY KEY).
#   - After a 30s quiet window per PR (no new pending events), spawns one
#     `claude -p` headless dispatcher per burst (A2 architecture). The
#     dispatcher (assets/dispatch-prompt.md) owns clustering, worker
#     dispatch, and all subsequent DB writes for that burst. Single-flight
#     per PR is enforced via acquire_dispatch_lock (Guard 1).
#
# The dispatcher owns reads/deletes of `pending_events` and writes to
# `seen_events` / `clusters` / `worker_reports`. poll.sh never writes
# those tables.
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

# Sanitize "owner/repo" → "owner__repo" for safe inclusion in filesystem
# artifact names. PR numbers are not unique across repos, so every per-PR
# path (lockdir, pid file, log file) must also be scoped by repo.
REPO_SAFE="${REPO//\//__}"

###############################################################################
# State directory and database
###############################################################################

STATE_DIR="${CLAUDE_PLUGIN_DATA}/babysit"
STATE_DB="${STATE_DIR}/state.db"
ASSETS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA_FILE="${ASSETS_DIR}/schema.sql"
DB_PY="${ASSETS_DIR}/db.py"

# CLAUDE_SKILL_DIR is normally exported by the skill harness. If not, derive
# it from ASSETS_DIR (one level up). The dispatcher prompt references
# ${CLAUDE_SKILL_DIR}/assets/db.py, so it must be exported to the spawned
# `claude -p` session below.
: "${CLAUDE_SKILL_DIR:=$(cd -- "${ASSETS_DIR}/.." && pwd)}"

# Dispatch prompt template — A2 architecture (Task 5). On debounce expiry
# with pending events, poll.sh spawns a fresh `claude -p` dispatcher per
# burst, interpolating this file's <PLACEHOLDER>s with the current PR's
# context. Replaces the deleted cluster_ready JSON emission.
DISPATCH_PROMPT_FILE="${ASSETS_DIR}/dispatch-prompt.md"

# Freeform instructions are set by SKILL.md when the user supplies extra
# guidance at babysit-start. Default to "None" so the prompt is always
# well-formed even if the harness doesn't export it.
FREEFORM="${FREEFORM_INSTRUCTIONS:-None}"

mkdir -p "$STATE_DIR"

# Self-record poller PID under a repo+PR-scoped name so Stop mode can find
# and terminate this process. The file holds our real numeric $$ — never a
# harness shell-id — so `kill -TERM` always works. Cleared on exit.
POLLER_PID_FILE="${STATE_DIR}/babysit-pid-${REPO_SAFE}-${PR}.pid"
echo "$$" > "$POLLER_PID_FILE"
trap 'rm -f "$POLLER_PID_FILE"' EXIT

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

  # Insert each event into pending_events via the db.py CLI. The CLI
  # uses bound parameters so payloads with any characters (quotes,
  # backslashes, embedded JSON) are stored verbatim. --json-stdin reads
  # the raw payload from stdin and validates it parses as JSON.
  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  while IFS= read -r evt; do
    [[ -z "$evt" ]] && continue
    local root_id
    root_id=$(echo "$evt" | jq -r '.thread_root_id')
    [[ -z "$root_id" || "$root_id" == "null" ]] && continue
    printf '%s' "$evt" | python3 "$DB_PY" insert_pending \
      --db "$STATE_DB" \
      --pr "$PR" \
      --kind "comment_thread" \
      --event-id "$root_id" \
      --received-ts "$now" \
      --json-stdin >/dev/null
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

  # Insert each event into pending_events via the db.py CLI.
  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  while IFS= read -r evt; do
    [[ -z "$evt" ]] && continue
    local build_num
    build_num=$(echo "$evt" | jq -r '.build_number')
    [[ -z "$build_num" || "$build_num" == "null" ]] && continue
    printf '%s' "$evt" | python3 "$DB_PY" insert_pending \
      --db "$STATE_DB" \
      --pr "$PR" \
      --kind "build_failure" \
      --event-id "$build_num" \
      --received-ts "$now" \
      --json-stdin >/dev/null
  done <<< "$events"
}

###############################################################################
# Debounce + dispatcher spawn.
#
# A2 dispatcher spawn: on debounce expiry, if no dispatcher is live for this
# PR (Guard 1 single-flight via acquire_dispatch_lock), spawn a fresh
# `claude -p` headless session as the dispatcher for this burst. Dispatcher
# reads pending_events, claims clusters via deterministic cluster_id (Guard
# 2 via db.py compute_cluster_id), spawns sub-agent workers via Agent tool
# (one nesting level only — validated), and commits worker_reports + drains
# pending_events. Lock is released by a background watcher on dispatcher
# exit. Replaces the previous cluster_ready JSON emission — under A2 nothing
# consumes those notifications because there is no long-lived parent
# session reading <task-notification> blocks anymore; the dispatcher IS the
# consumer.
###############################################################################

maybe_spawn_dispatcher() {
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
  (( age > DEBOUNCE_SECONDS )) || return 0

  # Debounce window elapsed and events are pending. Try to acquire the
  # per-PR dispatch lock (Guard 1). If another dispatcher is already live
  # for this PR, return silently — the pending events are durable in
  # pending_events and will be picked up by the current dispatcher's claim
  # pass, or by the next burst's dispatcher.
  if ! acquire_dispatch_lock "$PR"; then
    return 0
  fi

  # Interpolate the dispatch prompt with the current burst's context.
  # dispatch-prompt.md uses <PLACEHOLDER> style (angle brackets); bash
  # parameter expansion (${var//pat/repl}) handles that portably with no
  # new dependencies.
  #
  # <FREEFORM_INSTRUCTIONS> is substituted LAST so any user-supplied text
  # containing the literal placeholder names of later passes is not
  # rewritten by subsequent expansions.
  # Stable per-burst dispatcher id (used in log filenames + summary text).
  # Generated by the parent so the prompt placeholder is interpolable BEFORE
  # spawn. We previously used $BASHPID inside a subshell, but $BASHPID is
  # bash 4+ and macOS ships bash 3.2 — `set -u` made every dispatch crash.
  local burst_epoch
  burst_epoch=$(date -u +%s)
  local dispatcher_id="${REPO_SAFE}-${PR}-${burst_epoch}-$$"

  local prompt
  prompt="$(cat "$DISPATCH_PROMPT_FILE")"
  prompt="${prompt//<REPO>/$REPO}"
  prompt="${prompt//<PR_NUMBER>/$PR}"
  prompt="${prompt//<BRANCH_NAME>/$BRANCH}"
  prompt="${prompt//<PIPELINE>/$PIPELINE}"
  prompt="${prompt//<EVENT_COUNT>/$count}"
  prompt="${prompt//<DISPATCHER_ID>/$dispatcher_id}"
  prompt="${prompt//<FREEFORM_INSTRUCTIONS>/$FREEFORM}"

  # Log file for the dispatcher's output — useful for crash diagnosis.
  # Scoped by repo+PR so cross-repo runs do not stomp on each other.
  local log_file
  log_file="${STATE_DIR}/dispatch-${REPO_SAFE}-${PR}-${burst_epoch}.log"

  # Spawn dispatcher as a background `claude -p` headless session. Export
  # the env vars the dispatcher prompt expects (BABYSIT_STATE_DB,
  # CLAUDE_SKILL_DIR) into its environment. Dispatcher inherits cwd from
  # poll.sh (the repo root, set when the user started babysit).
  #
  # `--add-dir` is required: a headless `claude -p` session restricts
  # tool access to its cwd, but the dispatcher must Read worker prompts
  # from ${CLAUDE_SKILL_DIR}/assets/ and the SQLite DB under
  # ${CLAUDE_PLUGIN_DATA}/babysit/ — both outside the user's repo.
  # `--max-budget-usd` caps spend per dispatch since each burst is a
  # fresh session with no inherited limits.
  # Feed the (18+ KB) dispatch prompt via stdin, NOT as a positional arg.
  # `claude -p` silently exits with no output when handed a large prompt as
  # argv — every dispatcher log was 0 bytes until this switch. Manual repro
  # in v2.0.1 debug session: same 18 KB prompt via positional arg → 0-byte
  # log + no exit code observable; via `printf | claude -p` → valid JSON
  # in 30s. Cause unconfirmed (argv limit math says we are well below
  # ARG_MAX=1MB on macOS; suspect special-character handling in claude's
  # CLI parser for large positional prompts). Stdin sidesteps it entirely.
  printf '%s' "$prompt" | BABYSIT_STATE_DB="$STATE_DB" \
    CLAUDE_SKILL_DIR="$CLAUDE_SKILL_DIR" \
    claude -p \
      --permission-mode acceptEdits \
      --output-format json \
      --max-budget-usd 50 \
      --add-dir "${CLAUDE_SKILL_DIR}" \
      --add-dir "${CLAUDE_PLUGIN_DATA}" \
      > "$log_file" 2>&1 &
  local dispatcher_pid=$!

  # Atomically rewrite the placeholder PID written by acquire_dispatch_lock
  # with the real dispatcher pid so stale-lock checks (and `/babysit stop
  # --force`) see the correct process.
  write_dispatch_lock_pid "$PR" "$dispatcher_pid"

  # Background watcher: when the dispatcher exits, release the lock. Use a
  # disowned subshell so it survives poll.sh exit and does not block the
  # main poll loop. Pass the expected pid to release_dispatch_lock so the
  # watcher cannot clobber a lockdir that has been re-acquired by a
  # different dispatcher in the meantime.
  (
    while _dispatcher_alive "$dispatcher_pid"; do
      sleep 2
    done
    release_dispatch_lock "$PR" "$dispatcher_pid"
  ) &
  disown
}

###############################################################################
# Per-PR dispatch lock helpers (Guard 1 for A2 dispatch architecture).
#
# Used by Task 5 dispatcher spawn. Lock is per-PR — cross-PR concurrency
# is desired. Atomicity is provided by mkdir, which is POSIX-atomic on both
# macOS and Linux: two concurrent mkdir calls for the same path result in
# exactly one success and one failure.
###############################################################################

# Per-PR dispatch lock — Guard 1 for A2 dispatch architecture.
# Returns 0 if lock acquired (caller proceeds), 1 if another dispatcher live.
# Atomic via mkdir; stale recovery via atomic rename.
#
# Lockdir name includes the sanitized repo segment so two repos can run
# babysit on a PR with the same number without colliding.
DISPATCH_LOCK_DIR() {
  printf '%s/babysit/dispatch-lock-%s-%s.d' "${CLAUDE_PLUGIN_DATA}" "$REPO_SAFE" "$1"
}

# Liveness probe with PID-reuse defense — true iff $pid exists AND its
# command name still looks like a claude process. Plain `ps -p $pid`
# trusts PID identity, which the kernel may have recycled to an unrelated
# long-lived process while the dispatcher was dying.
_dispatcher_alive() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  ps -p "$pid" > /dev/null 2>&1 || return 1
  local comm
  comm="$(ps -p "$pid" -o comm= 2>/dev/null | tr -d '[:space:]')"
  [[ "$comm" == *claude* ]] || return 1
  return 0
}

acquire_dispatch_lock() {
  local pr="$1"
  local lockdir
  lockdir="$(DISPATCH_LOCK_DIR "$pr")"

  # Stale-lock recovery loop. We cannot simply `rm -rf` + `mkdir` because
  # two concurrent callers can both observe the same stale pid and race
  # each other through the `mkdir`. Instead, atomically rename the stale
  # lockdir aside with `mv -n` (POSIX rename is atomic; `-n` prevents
  # clobber on race). Exactly one rename wins; losers see the lockdir
  # already gone and loop back to retry `mkdir`.
  while [[ -d "$lockdir" ]]; do
    local pid
    pid="$(cat "${lockdir}/pid" 2>/dev/null || echo '')"
    if [[ -z "$pid" ]]; then
      # No pid file yet — either a concurrent acquisition in another shell
      # (microseconds-wide window between mkdir and pid write), or an
      # orphan from a poll.sh SIGKILL / OOM / disk-full crash between the
      # two ops. Disambiguate via the lockdir's mtime: if older than 60s,
      # treat as orphan and fall through to the mv-n reap. Otherwise treat
      # as in-flight acquisition and return as loser.
      local lockdir_age
      lockdir_age=$(($(date -u +%s) - $(stat -c %Y "$lockdir" 2>/dev/null \
        || stat -f %m "$lockdir" 2>/dev/null \
        || echo 0)))
      if (( lockdir_age <= 60 )); then
        return 1
      fi
      echo "[poll] orphan dispatch lock for PR ${pr} (empty pid, age ${lockdir_age}s); reaping" >&2
    elif _dispatcher_alive "$pid"; then
      return 1
    fi
    local stale="${lockdir}.stale.$$.$(date -u +%s)"
    if mv -n "$lockdir" "$stale" 2>/dev/null; then
      echo "[poll] stale dispatch lock for PR ${pr} (pid '${pid}' not a live claude proc); reaping" >&2
      rm -rf "$stale"
    fi
  done

  if mkdir "$lockdir" 2>/dev/null; then
    # Write a placeholder pid (our own $$) immediately so any concurrent
    # `acquire_dispatch_lock` sees a non-empty file and treats us as live.
    # `maybe_spawn_dispatcher` overwrites this atomically once the real
    # dispatcher pid is known via `write_dispatch_lock_pid`.
    printf '%s\n' "$$" > "${lockdir}/pid"
    return 0
  fi
  return 1
}

release_dispatch_lock() {
  # Ownership-checked release. If the lockdir's pid no longer matches the
  # caller's expected dispatcher pid, another dispatcher has acquired the
  # lock since the caller's watcher started — do not remove it.
  local pr="$1"
  local expected_pid="${2:-}"
  local lockdir
  lockdir="$(DISPATCH_LOCK_DIR "$pr")"
  if [[ -n "$expected_pid" ]]; then
    local pid
    pid="$(cat "${lockdir}/pid" 2>/dev/null || echo '')"
    if [[ "$pid" != "$expected_pid" ]]; then
      echo "[poll] release_dispatch_lock PR ${pr}: pid mismatch (have='${pid}', expected='${expected_pid}'); leaving lock alone" >&2
      return 0
    fi
  fi
  rm -rf "$lockdir"
}

write_dispatch_lock_pid() {
  local pr="$1"
  local pid="$2"
  local lockdir
  lockdir="$(DISPATCH_LOCK_DIR "$pr")"
  # Atomic overwrite via rename. Reader (`/babysit stop --force`, watcher
  # ownership check) either sees the old placeholder or the new value,
  # never a truncated file.
  local tmp="${lockdir}/pid.tmp.$$"
  printf '%s\n' "$pid" > "$tmp"
  mv -f "$tmp" "${lockdir}/pid"
}

###############################################################################
# Main loop
###############################################################################

while true; do
  poll_comments
  poll_builds
  maybe_spawn_dispatcher
  sleep "$INTERVAL"
done
