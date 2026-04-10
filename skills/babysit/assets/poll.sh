#!/usr/bin/env bash
set -euo pipefail

# poll.sh — Persistent polling loop for babysit skill.
# Launched via the Monitor tool. Emits JSON lines to stdout for new events.
# This script is a READ-ONLY observer: it reads state files but never writes them.
#
# Usage: poll.sh [<pipeline-slug>] [--no-comments] [--interval N]

###############################################################################
# Argument parsing
###############################################################################

PIPELINE=""
NO_COMMENTS=false
INTERVAL=120

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-comments)
      NO_COMMENTS=true
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
  echo '{"type":"error","source":"init","message":"Failed to detect repository via gh repo view"}'
  exit 1
fi

PR_INFO=$(gh pr view --json number,headRefName --jq '.number,.headRefName' 2>/dev/null) || true
if [[ -z "$PR_INFO" ]]; then
  echo '{"type":"error","source":"init","message":"Failed to detect PR number and branch via gh pr view"}'
  exit 1
fi

PR=$(echo "$PR_INFO" | head -n1)
BRANCH=$(echo "$PR_INFO" | tail -n1)

if [[ -z "$PR" || -z "$BRANCH" ]]; then
  echo '{"type":"error","source":"init","message":"Failed to parse PR number or branch name"}'
  exit 1
fi

# State directory
STATE_DIR="${CLAUDE_PLUGIN_DATA}/babysit"
LOCK_FILE="${STATE_DIR}/poll.lock"

###############################################################################
# Comment polling
###############################################################################

poll_comments() {
  if [[ "$NO_COMMENTS" == "true" ]]; then
    return
  fi

  local seen_file="${STATE_DIR}/${PR}-seen-comments.json"
  local seen_ids="[]"
  if [[ -f "$seen_file" ]]; then
    seen_ids=$(cat "$seen_file")
  fi

  local raw_comments
  raw_comments=$(gh api "repos/${REPO}/pulls/${PR}/comments" --paginate 2>/dev/null) || {
    echo '{"type":"error","source":"comments","message":"GitHub API request failed"}'
    return
  }

  # Process each comment: skip if already seen or if body contains babysit-agent marker
  echo "$raw_comments" | jq -c --argjson seen "$seen_ids" '
    .[] |
    select(.id as $id | ($seen | map(. == $id) | any | not)) |
    select(.body | contains("<!-- babysit-agent -->") | not) |
    {
      type: "comment",
      pr: '"$PR"',
      id: .id,
      reviewer: .user.login,
      file: .path,
      line: (.line // .original_line // null),
      body: .body,
      diff_hunk: .diff_hunk,
      created_at: .created_at
    }
  ' 2>/dev/null || true
}

###############################################################################
# Build polling
###############################################################################

poll_builds() {
  if [[ -z "$PIPELINE" ]]; then
    return
  fi

  local seen_file="${STATE_DIR}/${PR}-seen-builds.json"
  local seen_builds="{}"
  if [[ -f "$seen_file" ]]; then
    seen_builds=$(cat "$seen_file")
  fi

  local raw_builds
  raw_builds=$(bk build list --pipeline "$PIPELINE" --branch "$BRANCH" --json 2>/dev/null) || {
    echo '{"type":"error","source":"builds","message":"Buildkite CLI request failed"}'
    return
  }

  # Process each failed build: emit if not seen, or if seen with attempts < 3
  echo "$raw_builds" | jq -c --argjson seen "$seen_builds" '
    .[] |
    select(.state == "failed") |
    .number as $num |
    select(
      ($seen[($num | tostring)] == null) or
      (($seen[($num | tostring)].status == "failed") and (($seen[($num | tostring)].attempts // 0) < 3))
    ) |
    {
      type: "build_failure",
      pr: '"$PR"',
      build_number: .number,
      state: "failed",
      pipeline: "'"$PIPELINE"'",
      branch: "'"$BRANCH"'",
      jobs: [.jobs[]? | select(.state != "passed") | {id: .id, name: .name, state: .state}]
    }
  ' 2>/dev/null || true
}

###############################################################################
# Main loop
###############################################################################

while true; do
  # Lock check: skip this cycle if another agent holds the lock
  if [[ -f "$LOCK_FILE" ]]; then
    lock_mtime=$(stat -f %m "$LOCK_FILE" 2>/dev/null) || { sleep "$INTERVAL"; continue; }
    now=$(date +%s)
    age=$(( now - lock_mtime ))
    if (( age > 600 )); then
      rm -f "$LOCK_FILE"
      echo '{"type":"error","source":"lock","message":"Removed stale poll lock (>10 min)"}'
    else
      sleep "$INTERVAL"
      continue
    fi
  fi

  poll_comments
  poll_builds
  sleep "$INTERVAL"
done
