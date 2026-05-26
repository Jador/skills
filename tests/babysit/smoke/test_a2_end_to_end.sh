#!/usr/bin/env bash
# Smoke test for A2 dispatch path: validates end-to-end that a headless
# `claude -p` dispatcher can read pending events, compute a deterministic
# cluster_id, claim the cluster, spawn a worker sub-agent via the Agent
# tool, parse the worker's JSON contract, and commit the worker report —
# all through the new db.py CLI.
#
# Scope (in):
#   - db.py compute_cluster_id, claim_cluster, commit_worker_report
#   - JSON contract round-trip dispatcher -> worker -> dispatcher -> db.py
#   - Agent tool invocation from within `claude -p` headless session
#
# Scope (out):
#   - poll.sh gh/bk fetching, debounce timer, real GH/BK APIs
#   - The full dispatch-prompt.md (we use a minimal inlined variant here)
#
# Cost budget: < $5 per run.
#
# Usage:
#   bash tests/babysit/smoke/test_a2_end_to_end.sh
# Exits 0 on PASS, non-zero on FAIL.

set -uo pipefail

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

REPO_ROOT="$(git rev-parse --show-toplevel)"
CLAUDE_SKILL_DIR="${REPO_ROOT}/skills/babysit"
export CLAUDE_SKILL_DIR

SMOKE_ROOT="/tmp/babysit-a2-smoke-$$"
export CLAUDE_PLUGIN_DATA="${SMOKE_ROOT}"
export BABYSIT_STATE_DB="${SMOKE_ROOT}/babysit/state.db"

cleanup() {
  local rc=$?
  echo
  echo "--- cleanup ---"
  if [ -n "${KEEP_SMOKE_ARTIFACTS:-}" ]; then
    echo "KEEP_SMOKE_ARTIFACTS set; preserving ${SMOKE_ROOT}"
  else
    rm -rf "${SMOKE_ROOT}"
    echo "removed ${SMOKE_ROOT}"
  fi
  exit "$rc"
}
trap cleanup EXIT

echo "--- setup ---"
echo "REPO_ROOT=${REPO_ROOT}"
echo "CLAUDE_SKILL_DIR=${CLAUDE_SKILL_DIR}"
echo "CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA}"
echo "BABYSIT_STATE_DB=${BABYSIT_STATE_DB}"

mkdir -p "${CLAUDE_PLUGIN_DATA}/babysit"
sqlite3 "${BABYSIT_STATE_DB}" < "${CLAUDE_SKILL_DIR}/assets/schema.sql"
echo "schema applied"

# ---------------------------------------------------------------------------
# Seed phase: insert one synthetic comment_thread event for PR 99999.
# received_ts is set 60s in the past so any wall-clock debounce is satisfied.
# ---------------------------------------------------------------------------

echo
echo "--- seed ---"
PR=99999
EVENT_KIND="comment_thread"
EVENT_ID="smoke-1"
# 60s in the past, UTC ISO-8601 with Z suffix.
if date -u -v -60S +%Y-%m-%dT%H:%M:%SZ >/dev/null 2>&1; then
  RECEIVED_TS="$(date -u -v -60S +%Y-%m-%dT%H:%M:%SZ)"
else
  # GNU date fallback (Linux).
  RECEIVED_TS="$(date -u -d '60 seconds ago' +%Y-%m-%dT%H:%M:%SZ)"
fi
NOW_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "RECEIVED_TS=${RECEIVED_TS}"
echo "NOW_TS=${NOW_TS}"

PAYLOAD_JSON='{"comment":"test comment for smoke","user":"smoke-test","file":"README.md","thread_root_id":"smoke-1"}'

printf '%s' "${PAYLOAD_JSON}" | python3 "${CLAUDE_SKILL_DIR}/assets/db.py" insert_pending \
  --db "${BABYSIT_STATE_DB}" \
  --pr "${PR}" \
  --kind "${EVENT_KIND}" \
  --event-id "${EVENT_ID}" \
  --received-ts "${RECEIVED_TS}" \
  --json-stdin

echo "inserted pending event"

PENDING_COUNT_BEFORE=$(python3 "${CLAUDE_SKILL_DIR}/assets/db.py" read_pending \
  --db "${BABYSIT_STATE_DB}" --pr "${PR}" | jq '.rows | length')
echo "pending_events for PR ${PR} before dispatch: ${PENDING_COUNT_BEFORE}"
if [ "${PENDING_COUNT_BEFORE}" != "1" ]; then
  echo "FAIL: expected 1 pending event, got ${PENDING_COUNT_BEFORE}"
  exit 1
fi

# Pre-compute the cluster_id the dispatcher should derive (for verification
# only — the dispatcher must compute its own via db.py compute_cluster_id).
EXPECTED_CLUSTER_ID=$(echo '{"event_ids":[{"kind":"comment_thread","event_id":"smoke-1"}]}' \
  | python3 "${CLAUDE_SKILL_DIR}/assets/db.py" compute_cluster_id --pr "${PR}" --json-stdin \
  | jq -r '.cluster_id')
echo "expected cluster_id: ${EXPECTED_CLUSTER_ID}"

# ---------------------------------------------------------------------------
# Dispatch phase: minimal inlined dispatcher prompt invoked via `claude -p`.
# The prompt walks the dispatcher through exactly the critical path that
# the real dispatch-prompt.md exercises, but with the LLM clustering pass
# replaced by a single-event cluster (deterministic for the smoke).
# ---------------------------------------------------------------------------

DISPATCH_LOG="${CLAUDE_PLUGIN_DATA}/babysit/dispatch-${PR}-$(date -u +%s).log"
RESULT_JSON="${CLAUDE_PLUGIN_DATA}/babysit/dispatch-${PR}-result.json"

# IMPORTANT: $CLAUDE_SKILL_DIR and $BABYSIT_STATE_DB are exported above, so
# the spawned `claude -p` inherits them and the prompt below references
# them via $-expansion at the time the dispatcher executes the bash.
read -r -d '' DISPATCH_PROMPT <<'EOF' || true
You are the babysit dispatcher for PR 99999, repo smoke/test, branch worktree-solar-gem-mint.

You have access to Bash and Agent tools. Follow these 6 steps in order. After ALL six steps complete successfully, print EXACTLY the line `SMOKE_PASS` (no prefix, no quotes) as your final assistant message. If any step fails, print `SMOKE_FAIL: <one-line reason>` instead. Do not print SMOKE_PASS unless every step actually succeeded.

Environment variables already exported and available to your Bash calls:
- $CLAUDE_SKILL_DIR — root of the babysit skill (contains assets/db.py)
- $BABYSIT_STATE_DB — sqlite database path

## Step 1: Read pending events
Run this bash command and capture the stdout JSON:
```
python3 "$CLAUDE_SKILL_DIR/assets/db.py" read_pending --db "$BABYSIT_STATE_DB" --pr 99999
```
Confirm the response is `{"ok":true,"rows":[...]}` with at least one row whose kind is `comment_thread` and event_id is `smoke-1`. If not, SMOKE_FAIL.

## Step 2: Compute deterministic cluster_id
Run this bash command and capture the cluster_id from stdout:
```
echo '{"event_ids":[{"kind":"comment_thread","event_id":"smoke-1"}]}' | python3 "$CLAUDE_SKILL_DIR/assets/db.py" compute_cluster_id --pr 99999 --json-stdin
```
The response is `{"ok":true,"cluster_id":"<16-hex-chars>"}`. Extract the cluster_id string and reuse it for steps 3 and 6.

## Step 3: Claim the cluster
Run this bash command (substituting <CID> with the cluster_id from step 2):
```
echo '["README.md"]' | python3 "$CLAUDE_SKILL_DIR/assets/db.py" claim_cluster --db "$BABYSIT_STATE_DB" --cluster-id <CID> --pr 99999 --created-ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --json-stdin
```
The response must be `{"ok":true,"claimed":true}`. If `claimed` is false, SMOKE_FAIL.

## Step 4: Spawn a worker sub-agent via the Agent tool
Use the Agent tool to spawn ONE sub-agent. The sub-agent's task is exactly this (no embellishment):

> You are a smoke-test worker. Do not modify any files. Your only task is to return a single fenced ```json block on its own line, containing EXACTLY this object (a literal — do not vary the values):
>
> ```json
> {"resolved_event_ids":[{"kind":"comment_thread","event_id":"smoke-1"}],"unresolved_event_ids":[],"files_touched":["README.md"],"commit_sha":"smoke-sha","summary":"smoke test worker - validated"}
> ```
>
> Return that fenced block as your final message and nothing else.

## Step 5: Parse the worker's JSON
Extract the JSON object from the sub-agent's final message (the content between ```json and ``` fences). Confirm it has exactly these top-level keys: resolved_event_ids, unresolved_event_ids, files_touched, commit_sha, summary. If parse fails or keys missing, SMOKE_FAIL.

## Step 6: Commit the worker report
Take the worker's JSON object verbatim and pipe it into commit_worker_report. The bash form is (substituting <CID> with the cluster_id from step 2 and <JSON> with the worker's JSON object):

```
echo '<JSON>' | python3 "$CLAUDE_SKILL_DIR/assets/db.py" commit_worker_report --db "$BABYSIT_STATE_DB" --cluster-id <CID> --pr 99999 --commit-sha smoke-sha --summary smoke --now-ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```
The response must be `{"ok":true,"seen_inserted":1,"pending_deleted":1}`. If either count is not 1, SMOKE_FAIL.

If all six steps passed, your FINAL assistant message must be the single token `SMOKE_PASS` on its own line, with nothing else. Do not summarize. Do not explain. Just `SMOKE_PASS`.
EOF

echo
echo "--- dispatch ---"
echo "spawning claude -p dispatcher; logging to ${DISPATCH_LOG}"
echo "spawning at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

set +e
claude -p \
  --permission-mode acceptEdits \
  --output-format json \
  --no-session-persistence \
  --max-budget-usd 5 \
  --add-dir "${CLAUDE_SKILL_DIR}" \
  --add-dir "${CLAUDE_PLUGIN_DATA}" \
  "${DISPATCH_PROMPT}" \
  > "${RESULT_JSON}" 2> "${DISPATCH_LOG}"
DISPATCH_EXIT=$?
set -e

echo "dispatcher exit code: ${DISPATCH_EXIT}"
echo "result JSON written to: ${RESULT_JSON}"
echo "stderr log written to:  ${DISPATCH_LOG}"

# Extract the assistant's final text from the JSON envelope. claude -p
# --output-format json produces an object with a `result` (final assistant
# text) and `total_cost_usd` field, among others.
if [ ! -s "${RESULT_JSON}" ]; then
  echo "FAIL: result JSON is empty"
  echo "--- dispatcher stderr (last 80 lines) ---"
  tail -80 "${DISPATCH_LOG}" || true
  exit 1
fi

FINAL_TEXT=$(jq -r '.result // .response // .text // empty' "${RESULT_JSON}" 2>/dev/null || true)
COST_USD=$(jq -r '.total_cost_usd // .cost_usd // .cost // "unknown"' "${RESULT_JSON}" 2>/dev/null || echo unknown)
NUM_TURNS=$(jq -r '.num_turns // empty' "${RESULT_JSON}" 2>/dev/null || true)
DURATION_MS=$(jq -r '.duration_ms // empty' "${RESULT_JSON}" 2>/dev/null || true)

echo
echo "--- dispatcher summary ---"
echo "num_turns:   ${NUM_TURNS:-<unknown>}"
echo "duration_ms: ${DURATION_MS:-<unknown>}"
echo "cost_usd:    ${COST_USD}"
echo "final assistant text (first 400 chars):"
printf '%.400s\n' "${FINAL_TEXT}"

# ---------------------------------------------------------------------------
# Verification phase
# ---------------------------------------------------------------------------

echo
echo "--- verification ---"

FAIL=0

# Check 1: SMOKE_PASS appears in the dispatcher's final assistant text.
if printf '%s' "${FINAL_TEXT}" | grep -q "SMOKE_PASS"; then
  echo "PASS: dispatcher emitted SMOKE_PASS"
else
  echo "FAIL: dispatcher did NOT emit SMOKE_PASS"
  FAIL=1
fi

# Check 2: pending_events for PR drained to 0.
PENDING_COUNT_AFTER=$(sqlite3 "${BABYSIT_STATE_DB}" \
  "SELECT COUNT(*) FROM pending_events WHERE pr = ${PR}")
if [ "${PENDING_COUNT_AFTER}" = "0" ]; then
  echo "PASS: pending_events for PR ${PR} = 0 (drained)"
else
  echo "FAIL: pending_events for PR ${PR} = ${PENDING_COUNT_AFTER} (expected 0)"
  FAIL=1
fi

# Check 3: seen_events for PR has exactly 1 row.
SEEN_COUNT=$(sqlite3 "${BABYSIT_STATE_DB}" \
  "SELECT COUNT(*) FROM seen_events WHERE pr = ${PR}")
if [ "${SEEN_COUNT}" = "1" ]; then
  echo "PASS: seen_events for PR ${PR} = 1"
else
  echo "FAIL: seen_events for PR ${PR} = ${SEEN_COUNT} (expected 1)"
  FAIL=1
fi

# Check 4: worker_reports has exactly 1 row.
WORKER_REPORTS_COUNT=$(sqlite3 "${BABYSIT_STATE_DB}" \
  "SELECT COUNT(*) FROM worker_reports")
if [ "${WORKER_REPORTS_COUNT}" = "1" ]; then
  echo "PASS: worker_reports = 1"
else
  echo "FAIL: worker_reports = ${WORKER_REPORTS_COUNT} (expected 1)"
  FAIL=1
fi

# Check 5: clusters for PR in status=done = 1.
CLUSTERS_DONE_COUNT=$(sqlite3 "${BABYSIT_STATE_DB}" \
  "SELECT COUNT(*) FROM clusters WHERE pr = ${PR} AND status = 'done'")
if [ "${CLUSTERS_DONE_COUNT}" = "1" ]; then
  echo "PASS: clusters for PR ${PR} in status=done = 1"
else
  echo "FAIL: clusters for PR ${PR} in status=done = ${CLUSTERS_DONE_COUNT} (expected 1)"
  FAIL=1
fi

# Check 6: a dispatcher log file exists in the expected location.
if ls "${CLAUDE_PLUGIN_DATA}/babysit/dispatch-${PR}-"*.log >/dev/null 2>&1; then
  echo "PASS: dispatcher log file exists at ${CLAUDE_PLUGIN_DATA}/babysit/dispatch-${PR}-*.log"
else
  echo "FAIL: no dispatcher log file matched ${CLAUDE_PLUGIN_DATA}/babysit/dispatch-${PR}-*.log"
  FAIL=1
fi

# Check 7: cost under $5 budget (if numeric).
if [ "${COST_USD}" != "unknown" ] && [ -n "${COST_USD}" ]; then
  # Compare numerically with awk to tolerate floats.
  UNDER_BUDGET=$(awk -v c="${COST_USD}" 'BEGIN{ print (c+0 < 5) ? 1 : 0 }')
  if [ "${UNDER_BUDGET}" = "1" ]; then
    echo "PASS: cost ${COST_USD} USD < 5 USD budget"
  else
    echo "FAIL: cost ${COST_USD} USD >= 5 USD budget"
    FAIL=1
  fi
else
  echo "WARN: cost not reported in result JSON; skipping budget check"
fi

echo
echo "--- final DB state ---"
sqlite3 "${BABYSIT_STATE_DB}" <<'SQL'
.headers on
.mode column
SELECT 'pending_events' AS table_name, COUNT(*) AS rows FROM pending_events
UNION ALL SELECT 'seen_events',     COUNT(*) FROM seen_events
UNION ALL SELECT 'clusters',        COUNT(*) FROM clusters
UNION ALL SELECT 'worker_reports',  COUNT(*) FROM worker_reports;
SELECT cluster_id, pr, status, files_touched FROM clusters;
SELECT cluster_id, summary, commit_sha FROM worker_reports;
SQL

echo
if [ "${FAIL}" = "0" ]; then
  echo "RESULT: PASS (all checks)"
  exit 0
else
  echo "RESULT: FAIL"
  echo "--- dispatcher stderr (last 60 lines) ---"
  tail -60 "${DISPATCH_LOG}" || true
  exit 1
fi
