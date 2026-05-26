---
name: babysit
description: Monitor a PR for review comments and build failures
argument-hint: "[stop [--force] | clean [--dry-run]] [--no-comments] [--no-builds] [\"instructions\"]"
disable-model-invocation: true
---

# Babysit Skill

You monitor an open PR for review comments and build failures, automatically addressing feedback and fixing broken builds. The skill launches a background polling script as a read-only observer that streams JSON events to its stdout; your session reads those events with the Monitor tool and spawns one sub-agent worker per event to handle it.

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
- `--force` — (Stop mode only) reserved; in the hybrid model there is nothing besides `poll.sh` to force-terminate, so this flag is currently a no-op and is accepted only for backward compatibility

Strip these flags from `$ARGUMENTS` before proceeding with mode detection below. The remaining text (after flag removal and trimming whitespace) is used for mode selection.

If both `--no-comments` and `--no-builds` are specified, tell the user: "Both checks are disabled — nothing to monitor." Then stop.

The `--dry-run` flag is only meaningful for Clean mode. If `--dry-run` is supplied alongside `stop` or Start mode, ignore it silently.

The `--force` flag is only meaningful for Stop mode. If `--force` is supplied alongside `clean` or Start mode, ignore it silently.

## Architecture

Babysit is a hybrid observer + session pattern with three roles:

1. **`assets/poll.sh` — read-only observer.** Runs as a background process per monitored PR (launched via the Bash tool's `run_in_background: true` parameter — a backgrounded shell, **not** a `TaskCreate` Task; the harness's `TaskList` cannot see it). On each cycle it polls GitHub for new review comments and Buildkite for failed builds, dedupes against the `seen_events` table, and emits one JSON event per unseen item to its stdout. It is the **single writer** to `seen_events`; nothing else touches that table. It does no clustering, holds no locks, and spawns no workers.

2. **Your user session — event reader.** After Start mode launches `poll.sh`, you use the Monitor tool to stream stdout from the backgrounded shell. Each stdout line is one of these JSON event shapes:
   - `{"type":"comment_thread","pr":...,"thread_root_id":...,"comments":[...],"file":...,"line":...,"diff_hunk":...}`
   - `{"type":"build_failure","pr":...,"build_number":...,"state":"failed","pipeline":...,"branch":...,"jobs":[...]}`
   - `{"type":"error","kind":...,"pr":...,"message":...}`

   For each event, spawn one sub-agent via the `Agent` tool, using `assets/comment-check-prompt.md` for `comment_thread` events and `assets/build-check-prompt.md` for `build_failure` events. Surface `error` events to the user and continue. The sub-agents do the actual work (replying to comments, pushing build fixes).

3. **Sub-agent workers.** Invoked via the `Agent` tool with the worker prompts at `assets/comment-check-prompt.md` and `assets/build-check-prompt.md`. They read the event payload from their prompt, take action against the PR, and return free-form text. They do **not** write to `seen_events` or any other DB table.

**State.** Single SQLite DB at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. All writes go through `db.py` (Python sqlite3 with `?` bound params and `with conn:` transactions). The schema has two tables:
- `seen_events` — dedup ledger keyed by event identity (comment id, build number). Written only by `poll.sh`.
- `pipelines` — Buildkite pipeline slug per `owner/repo`. Written once during Start mode pipeline detection; read on subsequent runs.

There are no per-PR locks, no cluster claims, no clustering pass, and no second `claude -p` process. Everything that requires the LLM happens inside your session or inside the worker sub-agents you spawn.

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

3. **No other processes to terminate.** In the hybrid model `poll.sh` is the only long-running background process; there are no separate worker pools to clean up.

4. **`--force` is a no-op:** Accepted for backward compatibility only.

5. **Print confirmation:**
   - "Stopped N babysit poller(s)."

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

5. **(Removed in the hybrid model.)** This step previously reaped stale rows from a `clusters` table that no longer exists. Task 8 will renumber subsequent steps.

6. **Vacuum:** If `--dry-run` was specified, **skip this step entirely**. Otherwise reclaim space:
   ```
   python3 "${CLAUDE_SKILL_DIR}/assets/db.py" vacuum \
       --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"
   ```

7. **(Removed in the hybrid model.)** This step previously swept lockdirs that no longer exist (the hybrid model uses no per-PR locks).

8. **(Removed in the hybrid model.)** This step previously swept background-worker log files that no longer exist.

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
    - Count of old background-worker logs removed
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

If the remaining text is neither `stop` nor `clean` (case-insensitive), enter Start mode. Any remaining text after flag removal is the **freeform instructions** string. Store it for later use (it will be forwarded to sub-agent workers as `<FREEFORM_INSTRUCTIONS>` via the `FREEFORM_INSTRUCTIONS` env var that `poll.sh` reads and re-emits on each event). If the remaining text is empty or blank, the freeform instructions value is `"None"`.

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

This ensures the state directory exists and bootstraps the SQLite schema at `${CLAUDE_PLUGIN_DATA}/babysit/state.db` for the polling script and your session to read and write.

## Start Mode — Launch Poller

Launch `poll.sh` as a background process via the Bash tool's `run_in_background: true` parameter. The polling script runs detached as a read-only observer — it emits JSON events to stdout for each unseen comment thread or build failure. Your session reads those events via the Monitor tool and spawns sub-agent workers per event.

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

`<REPO_SAFE>` is `owner/repo` with each `/` replaced by `__` (e.g. `myorg/myrepo` → `myorg__myrepo`). The same scoping convention applies to any per-PR artifact so multiple repos with PRs of the same number can run concurrently without colliding.

## Confirmation Message

After the poller is launched, print the following confirmation message (replacing placeholders with actual values). Note that the PID file is self-recorded by `poll.sh`, so you do not need to capture or print the PID itself.

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Monitoring:       every 2 minutes
- Review comments:  enabled/disabled
- Build status:     enabled/disabled (pipeline: <PIPELINE>)
- PID file:         ${CLAUDE_PLUGIN_DATA}/babysit/babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid (self-recorded by poll.sh)

Your session will now read JSON events from the poller via the Monitor
tool and spawn one sub-agent worker per event (using
`comment-check-prompt.md` or `build-check-prompt.md`).

To stop the poller: `/babysit stop`.
```

For the "Review comments" line: print `enabled` if comment monitoring is active, `disabled` if `--no-comments` was specified.

For the "Build status" line: print `enabled (pipeline: <PIPELINE>)` if build monitoring is active (with actual pipeline slug), or `disabled` if `--no-builds` was specified.

Then stop. Your session has nothing more to do — the background poller owns the lifecycle from here.
