---
name: babysit
description: Monitor a PR for review comments and build failures
argument-hint: "[stop | clean [--dry-run]] [--no-comments] [--no-builds] [\"instructions\"]"
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
- `--dry-run` — (Clean mode only) print intended deletions without modifying the filesystem or database

Strip these flags from `$ARGUMENTS` before proceeding with mode detection below. The remaining text (after flag removal and trimming whitespace) is used for mode selection.

If both `--no-comments` and `--no-builds` are specified, tell the user: "Both checks are disabled — nothing to monitor." Then stop.

The `--dry-run` flag is only meaningful for Clean mode. If `--dry-run` is supplied alongside `stop` or Start mode, ignore it silently.

## Architecture

Babysit is a hybrid observer + session pattern with three roles:

1. **`assets/poll.sh` — read-only observer.** Runs as a background process per monitored PR (launched via the Bash tool's `run_in_background: true` parameter — a backgrounded shell, **not** a `TaskCreate` Task; the harness's `TaskList` cannot see it). On each cycle it polls GitHub for new review comments and Buildkite for failed builds, dedupes against the `seen_events` table, and emits one JSON event per unseen item to its stdout. It is the **single writer** to `seen_events`; nothing else touches that table. It holds no locks and spawns no workers.

2. **Your user session — event reader.** After Start mode launches `poll.sh`, you use the Monitor tool to stream stdout from the backgrounded shell. Each stdout line is one of these JSON event shapes:
   - `{"type":"comment_thread","pr":...,"thread_root_id":...,"comments":[...],"file":...,"line":...,"diff_hunk":...}`
   - `{"type":"build_failure","pr":...,"build_number":...,"state":"failed","pipeline":...,"branch":...,"jobs":[...]}`
   - `{"type":"error","kind":...,"pr":...,"message":...}`

   For each event, spawn one sub-agent via the `Agent` tool, using `assets/comment-check-prompt.md` for `comment_thread` events and `assets/build-check-prompt.md` for `build_failure` events. Surface `error` events to the user and continue. The sub-agents do the actual work (replying to comments, pushing build fixes).

3. **Sub-agent workers.** Invoked via the `Agent` tool with the worker prompts at `assets/comment-check-prompt.md` and `assets/build-check-prompt.md`. They read the event payload from their prompt, take action against the PR, and return free-form text. They do **not** write to `seen_events` or any other DB table.

**State.** Single SQLite DB at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. All writes go through `db.py` (Python sqlite3 with `?` bound params and `with conn:` transactions). The schema has two tables:
- `seen_events` — dedup ledger keyed by event identity (comment id, build number). Written only by `poll.sh`.
- `pipelines` — Buildkite pipeline slug per `owner/repo`. Written once during Start mode pipeline detection; read on subsequent runs.

There are no per-PR locks and no second long-running LLM process spawned by `poll.sh`. Everything that requires the LLM happens inside your session or inside the worker sub-agents you spawn.

### Mode: Stop

If the remaining text is `stop` (case-insensitive):

PID files are scoped by both repository and PR number. The convention is `babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid`, where `<REPO_SAFE>` is `owner/repo` with every `/` replaced by `__`. This lets babysit run concurrently against PRs with the same number across different repositories.

1. **Locate poller PID file(s):** Scan `${CLAUDE_PLUGIN_DATA}/babysit/` for every `babysit-pid-*.pid` file. If no PID files exist, print: "No babysit pollers are currently running." Then stop.

2. **Terminate the poller(s):** For each PID file:
   - Read the PID with `cat`.
   - `kill -TERM <pid>` (ignore errors — the process may already have exited).
   - Remove the PID file.

3. **Print confirmation:**
   - "Stopped N babysit poller(s)."

Then stop — do not continue to Start mode.

### Mode: Clean

If the remaining text is `clean` (case-insensitive):

Clean mode delegates DB writes to `db.py` (the CLI at `${CLAUDE_SKILL_DIR}/assets/db.py`) and sweeps stale filesystem artifacts from this skill and from the agent-teams harness. The `--dry-run` flag skips every destructive call — read-only queries and `ls`/`stat` checks still run so the dry-run summary is accurate. Ensure `BABYSIT_STATE_DB` is set or pass `--db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"` to each call (the examples below pass `--db` explicitly).

If `--dry-run` was specified, print a banner first:

```
=== DRY RUN — no changes will be written ===
```

1. **Locate the state database:** The database lives at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. If the file does not exist, skip steps 2–5 (DB cleanup) but still run steps 6–7 (filesystem sweeps). If both the DB is missing AND no stale filesystem artifacts are found, print: "No babysit state found." Then stop.

2. **Collect tracked PRs:** Ask the CLI for every PR ever recorded in `seen_events`:
   ```
   python3 "${CLAUDE_SKILL_DIR}/assets/db.py" list_distinct_prs \
       --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"
   ```
   The CLI prints one JSON object on stdout of the shape `{"ok": true, "prs": [<pr>, ...]}`. Extract the PR numbers with `jq -r '.prs[]'`. If the array is empty, skip to step 6.

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
     Would purge PR #<PR_NUMBER> (<state>): rows from seen_events
     ```
   - If `--dry-run` was specified, **skip the CLI call** — the line above is the only output for this PR.
   - If `--dry-run` was NOT specified, delegate the purge to `db.py`:
     ```
     python3 "${CLAUDE_SKILL_DIR}/assets/db.py" purge_pr \
         --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db" \
         --pr <PR_NUMBER>
     ```
     The CLI deletes the PR's rows from `seen_events` in a single transaction and prints `{"ok": true, "counts": {...}}`. Then report: "Purged PR #<PR_NUMBER> (<state>)."
   - For preserved PRs, report: "Preserved PR #<PR_NUMBER> (still open)."

5. **Vacuum:** If `--dry-run` was specified, **skip this step entirely**. Otherwise reclaim space:
   ```
   python3 "${CLAUDE_SKILL_DIR}/assets/db.py" vacuum \
       --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"
   ```

6. **Sweep old poll logs:** Remove stale `poll-*.log` files under `${CLAUDE_PLUGIN_DATA}/babysit/` with mtime older than 7 days:
   - `find "${CLAUDE_PLUGIN_DATA}/babysit" -maxdepth 1 -name 'poll-*.log' -mtime +7` (the directory may not exist — skip silently if `find` reports it missing).
   - For each match, print: "Would remove old poll log <path>."
   - If `--dry-run` was NOT specified, `rm -f <path>`.

7. **Sweep stale agent-teams orphans:** The agent-teams harness writes scratch directories under `~/.claude/teams/` and `~/.claude/tasks/` that are not cleaned up by every code path. Sweep entries with mtime older than 7 days:
   - `find ~/.claude/teams -mindepth 1 -maxdepth 1 -mtime +7` and `find ~/.claude/tasks -mindepth 1 -maxdepth 1 -mtime +7` (the directories may not exist — skip silently if `find` reports them missing).
   - For each match, print: "Would remove stale agent-teams orphan <path>."
   - If `--dry-run` was NOT specified, `rm -rf <path>`.

8. **Print summary:** Print a final summary block. If `--dry-run` was specified, prefix the summary with the dry-run banner repeated:
    ```
    === DRY RUN — no changes were written ===
    ```
    The summary must list:
    - Count of PRs purged (or that would be purged in dry-run)
    - Count of PRs preserved (still open)
    - Count of PRs skipped due to transient errors (if any)
    - Count of old poll logs removed
    - Count of stale agent-teams orphans removed

    Example:
    ```
    Babysit clean summary:
    - PRs purged:        3
    - PRs preserved:     2
    - PRs skipped:       0
    - Logs removed:      14
    - Orphans removed:   5
    ```

Then stop — do not continue to Start mode.

### Mode: Start (default)

If the remaining text is neither `stop` nor `clean` (case-insensitive), enter Start mode. Any remaining text after flag removal is treated as **freeform instructions** for the worker sub-agents — keep that string in memory and prepend it verbatim to each worker prompt you assemble in the Monitor loop below. If the remaining text is empty or blank, there are no freeform instructions and you can skip the prepend.

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

Launch `poll.sh` as a background process via the Bash tool's `run_in_background: true` parameter. The polling script runs detached as a read-only observer — it emits one JSON event per line to stdout for each unseen comment thread or build failure, and never spawns workers itself. Your session reads those stdout lines via the Monitor tool and dispatches one sub-agent worker per event.

**Bash tool call:**

- **command:** see construction rules below.
- **run_in_background:** `true`
- **description:** `babysit poll PR #<PR_NUMBER>` (with actual PR number)

**Capture the returned shell-id.** When the Bash tool returns, it gives you a `bash_xxxxxxxx` shell-id for the backgrounded process. Store this as **SHELL_ID** — you will pass it to the Monitor tool in the next step.

**Command construction rules:**
- Always include the env prefix: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" CLAUDE_SKILL_DIR="${CLAUDE_SKILL_DIR}"`. Do **not** pass any other environment variables — `poll.sh` reads only `CLAUDE_PLUGIN_DATA`, `CLAUDE_SKILL_DIR`, and its own positional arguments / flags.
- Always include: `bash "${CLAUDE_SKILL_DIR}/assets/poll.sh"`.
- If builds are enabled (no `--no-builds` flag): include the **PIPELINE** slug as the first positional argument. If builds are disabled (`--no-builds`): omit the pipeline argument entirely.
- Always include: `--interval 120`.
- If `--no-comments` was specified: include `--no-comments`. Otherwise omit it.
- If `--no-builds` was specified: include `--no-builds`. Otherwise omit it.

Examples:
- Both enabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" CLAUDE_SKILL_DIR="${CLAUDE_SKILL_DIR}" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "my-pipeline" --interval 120`
- Comments disabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" CLAUDE_SKILL_DIR="${CLAUDE_SKILL_DIR}" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "my-pipeline" --interval 120 --no-comments`
- Builds disabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" CLAUDE_SKILL_DIR="${CLAUDE_SKILL_DIR}" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" --interval 120 --no-builds`

### Poller PID file

You do **not** need to record the poller's PID from the Bash tool's return value. The Bash tool's `run_in_background: true` mode returns a harness shell-id (e.g. `bash_abc123`) rather than a real OS PID, and `kill -TERM` cannot signal a shell-id. The shell-id is what you pass to the Monitor tool below, but it is **not** what Stop mode kills. To avoid that mismatch, `poll.sh` writes its own numeric `$$` to `${CLAUDE_PLUGIN_DATA}/babysit/babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid` as its first action and clears the file on graceful exit. Stop mode reads that file directly.

`<REPO_SAFE>` is `owner/repo` with each `/` replaced by `__` (e.g. `myorg/myrepo` → `myorg__myrepo`). The same scoping convention applies to any per-PR artifact so multiple repos with PRs of the same number can run concurrently without colliding.

## Confirmation Message

Immediately after launching `poll.sh` (and before entering the Monitor loop in the next section), print exactly:

```
Babysitting PR #<PR_NUMBER>. Listening for events…
```

Substitute `<PR_NUMBER>` with the detected PR number. Keep this line short — it is the only output the user expects before events start arriving. Do not print a multi-line status block, do not print the PID file path, do not print the shell-id.

## Start Mode — Monitor Loop

Once the confirmation line is printed, your session's job is to keep reading new stdout lines from the backgrounded `poll.sh` shell and turning each one into a sub-agent invocation. This loop continues for the lifetime of your session (or until the user issues `/babysit stop`).

### Reading events

Invoke the **Monitor** tool against the **SHELL_ID** captured when you launched `poll.sh`. Each delivery from Monitor is one or more new stdout lines from the poller. Treat every non-empty line as a single JSON event — `poll.sh` guarantees one event per line and never emits partial lines.

For each line you receive:

1. Parse it as JSON (use `jq` if you need to, but inline JSON parsing inside this session is fine since the lines are small and well-formed).
2. Read the `.type` field to decide how to route it.
3. Dispatch as described under "Routing events" below.

If a line fails to parse as JSON, surface it to the user verbatim (prefix with `babysit: malformed event:` so they can tell it apart from real worker output) and continue. Do not stop the loop on parse errors.

### Routing events

There are exactly three event shapes, all emitted by `poll.sh`:

- **`{"type":"comment_thread","pr":...,"thread_root_id":...,"comments":[{id,user.login,body,created_at,in_reply_to_id},...],"file":...,"line":...,"diff_hunk":...}`** — a new (or newly-extended) review-comment thread on the PR.
- **`{"type":"build_failure","pr":...,"build_number":...,"state":"failed","pipeline":...,"branch":...,"jobs":[{id,name,state},...]}`** — a Buildkite build for this branch that has finished in a failed state.
- **`{"type":"error","kind":...,"pr":...,"message":...}`** — `poll.sh` itself hit a recoverable problem (API failure, missing field, etc.) and wants to surface it.

Handle each `.type` as follows:

**`comment_thread` → spawn a comment-check sub-agent.** Use the Agent (sub-agent) tool. Load the worker prompt from `${CLAUDE_SKILL_DIR}/assets/comment-check-prompt.md` and use it as the sub-agent's system prompt / instructions. Pass the event payload as the sub-agent's user message — the JSON line you just parsed, verbatim, inside a fenced ```json block. If freeform instructions were captured during Argument Parsing, prepend them to the user message above the JSON block under a `Freeform instructions:` header so the sub-agent sees them in context.

**`build_failure` → spawn a build-check sub-agent.** Same shape as above, but load the worker prompt from `${CLAUDE_SKILL_DIR}/assets/build-check-prompt.md` instead. Pass the event payload as the user message in a fenced ```json block, with any freeform instructions prepended exactly as for `comment_thread`.

**`error` → surface to the user.** Print a short line of the form `babysit poller error (<kind>): <message>` and then continue reading new events. Do not spawn a sub-agent and do not stop the loop. If `error` events arrive repeatedly with the same `kind`, the user can decide whether to `/babysit stop`.

### Parallelism

If a single Monitor delivery contains multiple new event lines (because two or more events arrived inside the same poll cycle), spawn **all** of the resulting sub-agents in a **single assistant message** with one Agent tool call per event. Do not serialize them. Sub-agents are independent — comment-check on thread A and build-check on build #123 have no shared state to contend for, and running them in parallel is the whole point of the hybrid model.

If the same Monitor delivery includes an `error` event alongside `comment_thread` / `build_failure` events, print the error line in the same assistant message as the sub-agent calls — the user-visible text and the tool calls can coexist.

### Stopping the loop

The loop ends when one of the following happens:

- The user issues `/babysit stop`, which terminates `poll.sh` and (by extension) closes its stdout. Monitor will deliver no more events.
- The user kills the session.
- `poll.sh` exits on its own (graceful shutdown — schema upgrade, repeated fatal errors, etc.). Monitor will report that the backgrounded shell has exited; surface that to the user as `babysit poller exited (status=<N>). To restart, run /babysit again.` and stop reading.

Do not attempt to restart `poll.sh` automatically — if the poller dies, the user gets to decide whether to relaunch.
