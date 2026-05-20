---
name: babysit
description: Monitor a PR for review comments and build failures
argument-hint: "[stop | clean] [--no-comments] [--no-builds] [\"instructions\"]"
disable-model-invocation: true
---

# Babysit Skill

You monitor an open PR for review comments and build failures, automatically addressing feedback and fixing broken builds. Uses the Monitor tool to run a background polling script that emits JSON events.

## Prerequisites

Before doing anything, verify the environment:

1. **Check `gh` CLI is available:** Run `which gh`. If it fails, tell the user: "The `gh` CLI is required but not found on your PATH. Install it from https://cli.github.com/ and try again." Then stop.
2. **Check this is a git repo:** Run `git rev-parse --is-inside-work-tree`. If it fails, tell the user: "This command must be run from inside a git repository." Then stop.
3. **Check `jq` is available:** Run `which jq`. If it fails, tell the user: "The `jq` CLI is required but not found on your PATH. Install it via your package manager and try again." Then stop.
4. **Check `sqlite3` is available:** Run `which sqlite3`. If it fails, tell the user: "The `sqlite3` CLI is required but not found on your PATH. Install it via your package manager and try again." Then stop.

### First-time setup after upgrade

If you are upgrading from a pre-SQLite version of this skill, you must run the one-time migration script before first use:

```
bash "${CLAUDE_SKILL_DIR}/assets/migrate.sh"
```

This converts any legacy filesystem-based state into the new SQLite database at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. The script is idempotent — it is safe to run again, but only needs to succeed once per machine. If you have never used this skill before, you can skip the migration; the schema bootstrap in Start mode will create a fresh database for you.

## Argument Parsing

Parse `$ARGUMENTS` to determine the mode of operation. Before mode detection, extract any flags from `$ARGUMENTS`:

- `--no-comments` — disables comment monitoring
- `--no-builds` — disables build monitoring
- `--dry-run` — (Clean mode only) print intended deletions and stale-cluster reaps without modifying the database

Strip these flags from `$ARGUMENTS` before proceeding with mode detection below. The remaining text (after flag removal and trimming whitespace) is used for mode selection.

If both `--no-comments` and `--no-builds` are specified, tell the user: "Both checks are disabled — nothing to monitor." Then stop.

The `--dry-run` flag is only meaningful for Clean mode. If `--dry-run` is supplied alongside `stop` or Start mode, ignore it silently.

### Mode: Stop

If the remaining text is `stop` (case-insensitive):

1. **List running tasks:** Use `TaskList` to retrieve all currently running tasks.
2. **Filter for babysit monitors:** Examine each task's description for matches beginning with `babysit-monitor`. If no matching tasks are found, print: "No babysit monitors are currently running." Then stop.
3. **Stop each match:** Use `TaskStop` on each matching task by its ID.
4. **Print confirmation:** Print: "Stopped N babysit monitor(s)." (where N is the count of stopped tasks).

Then stop — do not continue to Start mode.

### Mode: Clean

If the remaining text is `clean` (case-insensitive):

If `--dry-run` was specified, print a banner first:

```
=== DRY RUN — no changes will be written ===
```

1. **Locate the state database:** The database lives at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. If the file does not exist, print: "No babysit state found." Then stop.

2. **Collect tracked PRs:** Connect to the database via `sqlite3` and run:
   ```
   sqlite3 "${CLAUDE_PLUGIN_DATA}/babysit/state.db" "SELECT DISTINCT pr FROM seen_events;"
   ```
   This yields the set of PR numbers tracked across all events. If the result is empty, print: "No babysit state found." Then stop.

3. **Check each PR's GitHub state:** For each unique PR number, run:
   ```
   gh pr view <PR_NUMBER> --json state --jq .state 2>/tmp/babysit-gh-err.$$
   ```
   Capture both the stdout (the state string) and the exit code (`$?`).

   Classify each PR as follows:
   - Exit code `0` and stdout is `OPEN`: **preserve** (still open).
   - Exit code `0` and stdout is `MERGED` or `CLOSED`: **mark for purge**.
   - Non-zero exit code AND the stderr file mentions "Could not resolve" or "no pull requests found" (i.e., the PR no longer exists / 404): **mark for purge**.
   - Non-zero exit code with any other stderr (network blip, auth error, rate limit): print "could not determine state for PR <PR_NUMBER>; skipping" and **do not purge** this PR.

   Clean up `/tmp/babysit-gh-err.$$` after each check.

4. **Purge marked PRs:** For each PR marked for purge:
   - Print the intended deletes, e.g.:
     ```
     Would purge PR #<PR_NUMBER> (<state>): rows from seen_events, worker_reports, clusters, pending_events
     ```
   - If `--dry-run` was NOT specified, execute the deletion in a single transaction. Bind `<PR_NUMBER>` to the `?` placeholders:
     ```
     sqlite3 "${CLAUDE_PLUGIN_DATA}/babysit/state.db" <<SQL
     BEGIN;
     DELETE FROM seen_events WHERE pr=<PR_NUMBER>;
     DELETE FROM worker_reports WHERE cluster_id IN (SELECT cluster_id FROM clusters WHERE pr=<PR_NUMBER>);
     DELETE FROM clusters WHERE pr=<PR_NUMBER>;
     DELETE FROM pending_events WHERE pr=<PR_NUMBER>;
     COMMIT;
     SQL
     ```
     Then report: "Purged PR #<PR_NUMBER> (<state>)."
   - For preserved PRs, report: "Preserved PR #<PR_NUMBER> (still open)."

5. **Stale-cluster reap:** Filesystem lockfiles are gone — abandoned cluster rows in the database are the new safety net for crashed coordinators/workers. Run:
   ```
   sqlite3 "${CLAUDE_PLUGIN_DATA}/babysit/state.db" "SELECT cluster_id, pr FROM clusters WHERE status='running';"
   ```
   For each running cluster, cross-check against the live agent task list using the `TaskList` tool. A cluster is considered **live** if any running task's description matches one of:
   - `coordinator PR #<pr>` (where `<pr>` is the cluster's `pr` column)
   - any description beginning with `worker ` (worker tasks are tied to clusters via `cluster_id` in the DB, so any live worker task is treated as evidence that some cluster work is in flight)

   If no live task matches for a given running cluster, mark it as a stale-cluster candidate. Print the intended reap, e.g.:
   ```
   Would mark cluster <cluster_id> (PR #<pr>) as abandoned — no live coordinator/worker task
   ```
   If `--dry-run` was NOT specified, run:
   ```
   sqlite3 "${CLAUDE_PLUGIN_DATA}/babysit/state.db" "UPDATE clusters SET status='abandoned' WHERE cluster_id='<cluster_id>';"
   ```
   Then report: "Reaped stale cluster <cluster_id> (PR #<pr>) — marked abandoned."

6. **Vacuum:** If `--dry-run` was NOT specified, reclaim space:
   ```
   sqlite3 "${CLAUDE_PLUGIN_DATA}/babysit/state.db" "VACUUM;"
   ```
   Skip this step entirely in dry-run mode.

7. **Print summary:** Print a final summary block. If `--dry-run` was specified, prefix the summary with the dry-run banner repeated:
   ```
   === DRY RUN — no changes were written ===
   ```
   The summary must list:
   - Count of PRs purged (or that would be purged in dry-run)
   - Count of PRs preserved (still open)
   - Count of PRs skipped due to transient errors (if any)
   - Count of stale clusters reaped (or that would be reaped in dry-run)

   Example:
   ```
   Babysit clean summary:
   - PRs purged:    3
   - PRs preserved: 2
   - PRs skipped:   0
   - Clusters reaped: 1
   ```

Then stop — do not continue to Start mode.

### Mode: Start (default)

If the remaining text is neither `stop` nor `clean` (case-insensitive), enter Start mode. Any remaining text after flag removal is the **freeform instructions** string. Store it for later use (it will be passed to sub-agents as `<FREEFORM_INSTRUCTIONS>`). If the remaining text is empty or blank, the freeform instructions value is `"None"`.

## Architecture

All persistent state lives in a single SQLite database at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. All writes are atomic — SQLite transactions replace the filesystem lockfiles used by earlier versions of this skill. There are no `.lock` files, no `flock` calls, and no directory-based mutexes anywhere in the pipeline.

The poll script writes raw GitHub and Buildkite events into the `pending_events` table and emits a `cluster_ready` notification. The coordinator sub-agent reads those pending rows, clusters them via LLM reasoning, and dispatches workers only when the file sets touched by each cluster are disjoint. The coordinator owns all writes to `seen_events` — workers and the poll script never touch that table directly.

Workers return strict JSON to the coordinator and do not write to the state database themselves. This keeps the write path single-threaded per cluster and avoids any need for worker-side locking.

## Start Mode — Detection

### PR Detection

Auto-detect the current branch's PR. Run:

```
gh pr view --json number,headRefName
```

If the command fails (no PR exists for the current branch), tell the user: "No open PR found for the current branch." Then stop.

Parse the `number` and `headRefName` fields from the JSON output. Store these as **PR_NUMBER** and **BRANCH_NAME**.

### Repository Detection

Detect the repository owner and name:

```
gh repo view --json nameWithOwner --jq .nameWithOwner
```

Store the result (e.g., `owner/repo-name`) as **REPO**.

### Pipeline Detection

**Skip this section entirely if `--no-builds` was specified.**

**Check `bk` CLI is available:** Run `which bk`. If it fails, tell the user: "The `bk` CLI is required for build monitoring but not found on your PATH. Install it or use `--no-builds` to skip build monitoring." Then stop.

1. **Check for saved pipeline:** Read the saved pipeline slug for the current **REPO** from `${CLAUDE_PLUGIN_DATA}/babysit/state.db` if it exists. If an entry exists for the current **REPO**, use that slug as **PIPELINE**. Skip to Branch Divergence Check.

2. **Detect pipeline:** If no saved pipeline was found, run:
   ```
   bk pipeline list --json | jq -r '.[].slug'
   ```
   Filter out secondary pipelines — those whose slug contains `publish`, `deploy`, `release`, or `rosetta`.

3. **Select pipeline:**
   - If exactly one pipeline remains after filtering, use it as **PIPELINE**.
   - If zero or multiple pipelines remain after filtering, print the list and ask the user to choose:
     ```
     Multiple Buildkite pipelines found for this repo:
     - <slug-1>
     - <slug-2>
     Which pipeline should I monitor?
     ```
     Then stop and wait for the user's response.

4. **Save pipeline:** Save the selected pipeline to `${CLAUDE_PLUGIN_DATA}/babysit/state.db`, keyed by `owner/repo`. If an entry already exists for the current **REPO**, replace it; otherwise insert a new entry.

### Branch Divergence Check

Run the following commands:

```
git fetch origin
git merge-base --is-ancestor origin/main HEAD
```

If the `merge-base` command exits with a non-zero status, the branch has diverged from `origin/main`. Print a warning:

```
WARNING: Branch <BRANCH_NAME> has diverged from origin/main. There may be merge conflicts. Proceeding anyway.
```

Continue regardless — this is a warning only, not a blocker.

### Create Data Directory

Run:

```
mkdir -p "${CLAUDE_PLUGIN_DATA}/babysit"
sqlite3 "${CLAUDE_PLUGIN_DATA}/babysit/state.db" < "${CLAUDE_SKILL_DIR}/assets/schema.sql"
```

This ensures the state directory exists and bootstraps the SQLite schema at `${CLAUDE_PLUGIN_DATA}/babysit/state.db` for the polling script and sub-agents to read and write.

## Start Mode — Launch Monitor

Use the `Monitor` tool to start the background polling process with:

- **command**: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" BABYSIT_OWNER_PID="$$" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "<PIPELINE>" --interval 120 --no-comments` (see construction rules below)
- **description**: `babysit-monitor PR #<PR_NUMBER>` (with actual PR number)
- **persistent**: `true`

**Command construction rules:**
- Always include the env prefix: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}"`
- Always include `BABYSIT_OWNER_PID="$$"` in the env prefix
- Always include: `bash "${CLAUDE_SKILL_DIR}/assets/poll.sh"`
- If builds are enabled (no `--no-builds` flag): include the **PIPELINE** slug as the first positional argument. If builds are disabled (`--no-builds`): omit the pipeline argument entirely.
- Always include: `--interval 120`
- If `--no-comments` was specified: include `--no-comments`. Otherwise omit it.

Examples:
- Both enabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" BABYSIT_OWNER_PID="$$" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "my-pipeline" --interval 120`
- Comments disabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" BABYSIT_OWNER_PID="$$" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "my-pipeline" --interval 120 --no-comments`
- Builds disabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" BABYSIT_OWNER_PID="$$" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" --interval 120`

## Start Mode — Dispatch Instructions

When a `<task-notification>` arrives from the monitor, parse each line of the notification body as a JSON object. Each JSON object has a `type` field. Handle each line according to its type:

### Event type: `"cluster_ready"`

The polling script emits this event when pending events have been queued and are ready for clustering and dispatch. The coordinator sub-agent owns clustering, atomic claims, file-disjoint wave packing, worker dispatch, and all state-database writes.

1. Read the file `${CLAUDE_SKILL_DIR}/assets/coordinator-prompt.md`.
2. In its contents, replace:
   - `<REPO>` with the detected **REPO** value
   - `<PR_NUMBER>` with the detected **PR_NUMBER** value
   - `<BRANCH_NAME>` with the detected **BRANCH_NAME** value
   - `<PIPELINE>` with the detected **PIPELINE** value (or `"None"` if `--no-builds` was specified)
   - `<FREEFORM_INSTRUCTIONS>` with the stored freeform instructions string (or `"None"` if none were provided)
   - `<EVENT_COUNT>` with the `event_count` field from the JSON line (the number of pending events queued for this PR)
3. Pass the fully interpolated prompt to the **Agent** tool with description `"coordinator PR #<PR_NUMBER>"` (with actual PR number).
4. Print the coordinator's returned summary.

### Event type: `"error"`

Print a warning message: "Polling degraded: <message>. Will retry next cycle." (where `<message>` is the value of the `message` field from the JSON event). Do **not** dispatch a sub-agent for error events.

### Multiple events in one notification

If a single notification contains multiple JSON lines, handle each line according to its type. Multiple `cluster_ready` lines for the same PR are rare but possible; each one spawns its own coordinator invocation. Error lines are handled inline (print warning only).

## Confirmation Message

After the Monitor is launched, print the following confirmation message (replacing placeholders with actual values):

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Monitoring: every 2 minutes
- Review comments: enabled/disabled
- Build status: enabled/disabled (pipeline: <PIPELINE>)
```

For the "Review comments" line: print `enabled` if comment monitoring is active, `disabled` if `--no-comments` was specified.

For the "Build status" line: print `enabled (pipeline: <PIPELINE>)` if build monitoring is active (with actual pipeline slug), or `disabled` if `--no-builds` was specified.

Then stop.
