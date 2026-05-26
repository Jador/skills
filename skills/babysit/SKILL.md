---
name: babysit
description: Monitor a PR for review comments and build failures
argument-hint: "[stop [--force] | clean [--dry-run]] [--no-comments] [--no-builds] [\"instructions\"]"
disable-model-invocation: true
---

# Babysit Skill

You monitor an open PR for review comments and build failures, automatically addressing feedback and fixing broken builds. The skill launches a background polling script that ingests events into SQLite and spawns its own headless `claude -p` dispatcher per debounced burst — your session is not involved in dispatch after Start mode returns.

## Prerequisites

Before doing anything, verify the environment:

1. **Check `gh` CLI is available:** Run `which gh`. If it fails, tell the user: "The `gh` CLI is required but not found on your PATH. Install it from https://cli.github.com/ and try again." Then stop.
2. **Check this is a git repo:** Run `git rev-parse --is-inside-work-tree`. If it fails, tell the user: "This command must be run from inside a git repository." Then stop.
3. **Check `jq` is available:** Run `which jq`. If it fails, tell the user: "The `jq` CLI is required but not found on your PATH. Install it via your package manager and try again." Then stop.
4. **Check `sqlite3` is available:** Run `which sqlite3`. If it fails, tell the user: "The `sqlite3` CLI is required but not found on your PATH. Install it via your package manager and try again." Then stop.
5. **Check `python3` is available:** Run `which python3`. If it fails, tell the user: "`python3` is required but not found on your PATH. Install it via your package manager and try again." Then stop. `python3` runs `assets/db.py`, the bound-parameter SQLite helper that owns all DB writes.

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
- `--dry-run` — (Clean mode only) print intended deletions and stale-cluster reaps without modifying the filesystem or database
- `--force` — (Stop mode only) also terminate any in-flight dispatcher processes for the targeted PR(s) in addition to the poller

Strip these flags from `$ARGUMENTS` before proceeding with mode detection below. The remaining text (after flag removal and trimming whitespace) is used for mode selection.

If both `--no-comments` and `--no-builds` are specified, tell the user: "Both checks are disabled — nothing to monitor." Then stop.

The `--dry-run` flag is only meaningful for Clean mode. If `--dry-run` is supplied alongside `stop` or Start mode, ignore it silently.

The `--force` flag is only meaningful for Stop mode. If `--force` is supplied alongside `clean` or Start mode, ignore it silently.

## Architecture

Single SQLite DB at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. All writes atomic via `db.py` (Python sqlite3 with `?` bound params and `with conn:` transactions). No filesystem locks for DB state.

The poll script (`assets/poll.sh`) runs as a background process per monitored PR (launched via the Bash tool's `run_in_background: true` parameter — a backgrounded shell, **not** a `TaskCreate` Task; the harness's `TaskList` cannot see it). On each cycle it ingests new comments + build failures into `pending_events`. After a 30s debounce window of no new events for a PR, the poller spawns a fresh headless `claude -p` dispatcher (acquiring a per-`(repo,PR)` `mkdir`-based lock — Guard 1). The dispatcher reads pending events, runs an LLM clustering pass, atomically claims clusters via `db.py claim_cluster` with deterministic cluster_id (Guard 2), spawns sub-agent workers via the `Agent` tool, collects their JSON returns, and commits `worker_reports` + drains `pending_events`. Workers stay sub-agents because `claude -p` main thread → `Agent` tool sub-agent is one level of nesting only (validated).

Workers return strict JSON — they do NOT write state. The dispatcher is the sole DB writer for `seen_events`, `clusters`, and `worker_reports`.

**This means your session is not involved in dispatch.** Start mode launches `poll.sh` and exits. To observe what the background pipeline is doing, `tail -f ${CLAUDE_PLUGIN_DATA}/babysit/dispatch-<REPO_SAFE>-<PR>-*.log` (where `<REPO_SAFE>` is the current `owner/repo` with `/` replaced by `__`).

### Mode: Stop

If the remaining text is `stop` (case-insensitive):

PID files and lockdirs are scoped by both repository and PR number. The convention is `babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid` and `dispatch-lock-<REPO_SAFE>-<PR_NUMBER>.d`, where `<REPO_SAFE>` is `owner/repo` with every `/` replaced by `__`. This lets babysit run concurrently against PRs with the same number across different repositories.

1. **Locate poller PID file(s):**
   - If a repo + PR pair was discovered (see PR Detection below — Stop mode does the same auto-detect as Start mode), look at `${CLAUDE_PLUGIN_DATA}/babysit/babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid`.
   - If neither a repo nor a PR can be detected (e.g., the user ran `babysit stop` from outside a repo), scan `${CLAUDE_PLUGIN_DATA}/babysit/` for every `babysit-pid-*.pid` file and operate on each in turn.
   - If no PID files exist at all, print: "No babysit pollers are currently running." Then stop.

2. **Terminate the poller(s):** For each PID file:
   - Read the PID with `cat`. The contents are written by `poll.sh` itself as its first action (using its own numeric `$$`), so this is always a real process id — never a harness shell-id — and `kill -TERM` will always succeed against a live poller.
   - `kill -TERM <pid>` (ignore errors — the process may already have exited).
   - Remove the PID file. (`poll.sh` also removes it on graceful exit; the explicit `rm` here covers the SIGTERM case.)

3. **Default behaviour leaves in-flight dispatchers alone.** Active dispatcher processes continue to completion — they are independent of `poll.sh` and may be mid-work writing `worker_reports`. Killing them mid-cluster can leave `clusters.status='running'` rows behind that Clean mode will eventually reap.

4. **`--force` mode also terminates dispatchers:** If `--force` was specified, for each affected `(REPO_SAFE, PR)` pair:
   - Read the dispatcher PID from `${CLAUDE_PLUGIN_DATA}/babysit/dispatch-lock-<REPO_SAFE>-<PR>.d/pid` if it exists.
   - If the pid file is missing or its contents are non-numeric (e.g., a placeholder written by `acquire_dispatch_lock` in the brief window before the real dispatcher pid is recorded), **skip the kill and leave the lockdir alone** — removing it would let a second dispatcher spawn against the same `(repo, PR)` on the next debounce.
   - Otherwise, `kill -TERM <pid>` (ignore errors).
   - Remove the lockdir with `rm -rf "${CLAUDE_PLUGIN_DATA}/babysit/dispatch-lock-<REPO_SAFE>-<PR>.d"`.

5. **Print confirmation:**
   - Without `--force`: "Stopped N babysit poller(s). In-flight dispatchers (if any) will continue to completion."
   - With `--force`: "Stopped N babysit poller(s) and M dispatcher(s)."

Then stop — do not continue to Start mode.

### Mode: Clean

If the remaining text is `clean` (case-insensitive):

Clean mode delegates DB writes to `db.py` (the CLI at `${CLAUDE_SKILL_DIR}/assets/db.py`) and sweeps stale filesystem artifacts from this skill and from the agent-teams harness. The `--dry-run` flag skips every destructive call — read-only queries and `ls`/`stat` checks still run so the dry-run summary is accurate. Ensure `BABYSIT_STATE_DB` is set or pass `--db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"` to each call (the examples below pass `--db` explicitly).

If `--dry-run` was specified, print a banner first:

```
=== DRY RUN — no changes will be written ===
```

1. **Locate the state database:** The database lives at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. If the file does not exist, skip steps 2–6 (DB cleanup) but still run steps 7–9 (filesystem sweeps). If both the DB is missing AND no stale filesystem artifacts are found, print: "No babysit state found." Then stop.

2. **Collect tracked PRs:** Ask the CLI for every PR ever recorded in `seen_events`:
   ```
   python3 "${CLAUDE_SKILL_DIR}/assets/db.py" list_distinct_prs \
       --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"
   ```
   The CLI prints one JSON object on stdout of the shape `{"ok": true, "prs": [<pr>, ...]}`. Extract the PR numbers with `jq -r '.prs[]'`. If the array is empty, skip to step 7.

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
   - If `--dry-run` was specified, **skip the CLI call** — the line above is the only output for this PR.
   - If `--dry-run` was NOT specified, delegate the transactional 4-table purge to `db.py`:
     ```
     python3 "${CLAUDE_SKILL_DIR}/assets/db.py" purge_pr \
         --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db" \
         --pr <PR_NUMBER>
     ```
     The CLI runs all four `DELETE`s in a single transaction and prints `{"ok": true, "counts": {...}}`. Then report: "Purged PR #<PR_NUMBER> (<state>)."
   - For preserved PRs, report: "Preserved PR #<PR_NUMBER> (still open)."

5. **Stale-cluster reap (cross-PR safety net):** Under A2, abandoned `clusters.status='running'` rows arise only when a dispatcher crashes mid-claim — the per-burst dispatch model means there is no per-PR reap step in the dispatcher itself. Clean mode keeps a cross-PR sweep as the safety net.

   **Live-cluster enumeration (NOT TaskList).** Workers run inside the dispatcher's headless `claude -p` session, not your user session — `TaskList` cannot see them and would always return an empty whitelist, causing this step to abandon every running cluster across every PR. Derive liveness from the dispatch lockdirs instead. Run this snippet:

   ```bash
   live_ids=()
   shopt -s nullglob
   for lockdir in "${CLAUDE_PLUGIN_DATA}/babysit/"dispatch-lock-*.d; do
     pid_file="${lockdir}/pid"
     [[ -f "$pid_file" ]] || continue
     pid=$(cat "$pid_file" 2>/dev/null || true)
     [[ "$pid" =~ ^[0-9]+$ ]] || continue
     ps -p "$pid" >/dev/null 2>&1 || continue
     comm=$(ps -p "$pid" -o comm= 2>/dev/null | tr -d '[:space:]')
     [[ "$comm" == *claude* ]] || continue
     # Lockdir name: dispatch-lock-<REPO_SAFE>-<PR>.d. Pull trailing -<num>.
     base=$(basename "$lockdir" .d)
     pr="${base##*-}"
     [[ "$pr" =~ ^[0-9]+$ ]] || continue
     while IFS= read -r cid; do
       [[ -n "$cid" ]] && live_ids+=("$cid")
     done < <(sqlite3 -noheader "${CLAUDE_PLUGIN_DATA}/babysit/state.db" \
       "SELECT cluster_id FROM clusters WHERE pr = $pr AND status = 'running';" 2>/dev/null)
   done
   shopt -u nullglob
   if [[ ${#live_ids[@]} -eq 0 ]]; then
     live_ids_json="[]"
   else
     live_ids_json=$(printf '%s\n' "${live_ids[@]}" | jq -Rsn '[inputs|select(length>0)]')
   fi
   ```

   `$live_ids_json` is the JSON array to pipe into `reap_stale_clusters`. Note that an empty array (`[]`) here genuinely means "no live dispatchers found" — `db.py` will then mark every `clusters.status='running'` row as `abandoned`. That is the intended safety-net behaviour: if no dispatcher process is alive, no cluster can legitimately still be running. If the lockdir scan failed to discover dispatchers you expected, abort Clean mode and investigate before re-running.

   Print the intended reaps for any running cluster that is *not* in the live whitelist (for the dry-run banner, pre-print "Would mark cluster <cluster_id> (PR #<pr>) as abandoned — no live dispatcher" lines based on a separate read of `clusters WHERE status='running'` that filters out the whitelist).

   If `--dry-run` was specified, **skip the CLI call**.

   If `--dry-run` was NOT specified, pipe the live cluster_ids to the CLI to perform the cross-PR sweep in one shot:
   ```
   printf '%s' "$live_ids_json" | python3 "${CLAUDE_SKILL_DIR}/assets/db.py" reap_stale_clusters \
       --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"
   ```
   Omit `--pr` for a cross-PR sweep (the CLI scopes to all PRs when `--pr` is absent). The CLI prints `{"ok": true, "reaped": N}`. Then report: "Reaped N stale cluster(s) — marked abandoned."

6. **Vacuum:** If `--dry-run` was specified, **skip this step entirely**. Otherwise reclaim space:
   ```
   python3 "${CLAUDE_SKILL_DIR}/assets/db.py" vacuum \
       --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"
   ```

7. **Sweep stale dispatch lockdirs:** For each `${CLAUDE_PLUGIN_DATA}/babysit/dispatch-lock-*.d/` directory:
   - Read the dispatcher pid from `<lockdir>/pid` (if it exists).
   - Check liveness with `ps -p <pid>` — if the process is **alive**, skip this lockdir.
   - Check lockdir mtime — if it was modified within the last hour (3600 seconds), skip (safety: don't reap a lock that just got acquired by a dispatcher that hasn't written its pid yet).
   - Otherwise the lockdir is stale. Print: "Would remove stale dispatch lockdir <path> (pid <pid> not alive, mtime > 1h)."
   - If `--dry-run` was NOT specified, `rm -rf "<lockdir>"`.

8. **Sweep old dispatcher logs:** List `${CLAUDE_PLUGIN_DATA}/babysit/dispatch-*.log` files older than 30 days (use `find "${CLAUDE_PLUGIN_DATA}/babysit" -maxdepth 1 -type f -name 'dispatch-*.log' -mtime +30`). For each match:
   - Print: "Would remove old dispatcher log <path>."
   - If `--dry-run` was NOT specified, `rm -f <path>`.

9. **Sweep stale agent-teams orphans:** The agent-teams harness writes scratch directories under `~/.claude/teams/` and `~/.claude/tasks/` that are not cleaned up by every code path. Sweep entries with mtime older than 7 days:
   - `find ~/.claude/teams -mindepth 1 -maxdepth 1 -mtime +7` and `find ~/.claude/tasks -mindepth 1 -maxdepth 1 -mtime +7` (the directories may not exist — skip silently if `find` reports them missing).
   - For each match, print: "Would remove stale agent-teams orphan <path>."
   - If `--dry-run` was NOT specified, `rm -rf <path>`.

10. **Print summary:** Print a final summary block. If `--dry-run` was specified, prefix the summary with the dry-run banner repeated:
    ```
    === DRY RUN — no changes were written ===
    ```
    The summary must list:
    - Count of PRs purged (or that would be purged in dry-run)
    - Count of PRs preserved (still open)
    - Count of PRs skipped due to transient errors (if any)
    - Count of stale clusters reaped (or that would be reaped in dry-run)
    - Count of stale dispatch lockdirs removed
    - Count of old dispatcher logs removed
    - Count of stale agent-teams orphans removed

    Example:
    ```
    Babysit clean summary:
    - PRs purged:        3
    - PRs preserved:     2
    - PRs skipped:       0
    - Clusters reaped:   1
    - Lockdirs removed:  2
    - Logs removed:      14
    - Orphans removed:   5
    ```

Then stop — do not continue to Start mode.

### Mode: Start (default)

If the remaining text is neither `stop` nor `clean` (case-insensitive), enter Start mode. Any remaining text after flag removal is the **freeform instructions** string. Store it for later use (it will be passed to the dispatcher as `<FREEFORM_INSTRUCTIONS>` via the `FREEFORM_INSTRUCTIONS` env var that `poll.sh` reads). If the remaining text is empty or blank, the freeform instructions value is `"None"`.

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

1. **Check for saved pipeline:** Query the `pipelines` table in `${CLAUDE_PLUGIN_DATA}/babysit/state.db` for the current **REPO**:
   ```
   sqlite3 "${CLAUDE_PLUGIN_DATA}/babysit/state.db" "SELECT slug FROM pipelines WHERE repo = '<REPO>';"
   ```
   If a row exists, use that slug as **PIPELINE**. Skip to Branch Divergence Check.

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

4. **Save pipeline:** UPSERT the selected pipeline into the `pipelines` table keyed by `owner/repo`:
   ```
   sqlite3 "${CLAUDE_PLUGIN_DATA}/babysit/state.db" "INSERT INTO pipelines(repo, slug, ts) VALUES('<REPO>', '<PIPELINE>', datetime('now')) ON CONFLICT(repo) DO UPDATE SET slug=excluded.slug, ts=excluded.ts;"
   ```

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

### Create Data Directory and Bootstrap Schema

Run:

```
mkdir -p "${CLAUDE_PLUGIN_DATA}/babysit"
sqlite3 "${CLAUDE_PLUGIN_DATA}/babysit/state.db" < "${CLAUDE_SKILL_DIR}/assets/schema.sql"
```

This ensures the state directory exists and bootstraps the SQLite schema at `${CLAUDE_PLUGIN_DATA}/babysit/state.db` for the polling script and dispatcher to read and write.

## Start Mode — Launch Poller

Launch `poll.sh` as a background process via the Bash tool's `run_in_background: true` parameter. The polling script runs detached from your session, ingests events into `pending_events`, and spawns its own headless `claude -p` dispatchers on debounce — your session is not involved in dispatch.

**Bash tool call:**

- **command:** see construction rules below.
- **run_in_background:** `true`
- **description:** `babysit poll PR #<PR_NUMBER>` (with actual PR number)

**Command construction rules:**
- Always include the env prefix: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" CLAUDE_SKILL_DIR="${CLAUDE_SKILL_DIR}" FREEFORM_INSTRUCTIONS=<quoted-instructions>`.
  - `<quoted-instructions>` is the freeform instructions string from Argument Parsing, single-quoted (escape any embedded single quotes by closing-the-quote / backslash-quote / reopen). If empty, pass `None`.
- Always include: `bash "${CLAUDE_SKILL_DIR}/assets/poll.sh"`.
- If builds are enabled (no `--no-builds` flag): include the **PIPELINE** slug as the first positional argument. If builds are disabled (`--no-builds`): omit the pipeline argument entirely.
- Always include: `--interval 120`.
- If `--no-comments` was specified: include `--no-comments`. Otherwise omit it.
- If `--no-builds` was specified: include `--no-builds`. Otherwise omit it.

Examples:
- Both enabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" CLAUDE_SKILL_DIR="${CLAUDE_SKILL_DIR}" FREEFORM_INSTRUCTIONS='None' bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "my-pipeline" --interval 120`
- Comments disabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" CLAUDE_SKILL_DIR="${CLAUDE_SKILL_DIR}" FREEFORM_INSTRUCTIONS='None' bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "my-pipeline" --interval 120 --no-comments`
- Builds disabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" CLAUDE_SKILL_DIR="${CLAUDE_SKILL_DIR}" FREEFORM_INSTRUCTIONS='None' bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" --interval 120 --no-builds`

### Poller PID file

You do **not** need to record the poller's PID from the Bash tool's return value. The Bash tool's `run_in_background: true` mode often returns a harness shell-id (e.g. `bash_abc123`) rather than a real OS PID, and `kill -TERM` cannot signal a shell-id. To avoid that failure mode, `poll.sh` writes its own numeric `$$` to `${CLAUDE_PLUGIN_DATA}/babysit/babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid` as its first action and clears the file on graceful exit. Stop mode reads that file directly.

`<REPO_SAFE>` is `owner/repo` with each `/` replaced by `__` (e.g. `myorg/myrepo` → `myorg__myrepo`). The same scoping is used for the dispatch lockdir and dispatcher logs so multiple repos with PRs of the same number can run concurrently without colliding.

## Confirmation Message

After the poller is launched, print the following confirmation message (replacing placeholders with actual values). Note that the PID file is self-recorded by `poll.sh`, so you do not need to capture or print the PID itself.

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Monitoring:       every 2 minutes
- Review comments:  enabled/disabled
- Build status:     enabled/disabled (pipeline: <PIPELINE>)
- PID file:         ${CLAUDE_PLUGIN_DATA}/babysit/babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid (self-recorded by poll.sh)

Babysit no longer dispatches workers from your session. The background
poller spawns its own headless `claude -p` dispatchers on debounce. To
follow what is happening live:

    tail -f "${CLAUDE_PLUGIN_DATA}/babysit/dispatch-<REPO_SAFE>-<PR_NUMBER>-"*.log

To stop the poller: `/babysit stop` (active dispatchers continue to
completion) or `/babysit stop --force` (also kills in-flight dispatchers).
```

For the "Review comments" line: print `enabled` if comment monitoring is active, `disabled` if `--no-comments` was specified.

For the "Build status" line: print `enabled (pipeline: <PIPELINE>)` if build monitoring is active (with actual pipeline slug), or `disabled` if `--no-builds` was specified.

Then stop. Your session has nothing more to do — the background poller owns the lifecycle from here.
