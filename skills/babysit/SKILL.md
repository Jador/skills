---
name: babysit
description: Monitor a PR for review comments and build failures
argument-hint: "[stop | clean [--dry-run]] [--no-comments] [--no-builds] [\"instructions\"]"
disable-model-invocation: true
---

# Babysit Skill

You monitor an open PR for review comments and build failures, automatically addressing feedback and fixing broken builds. The skill launches a background polling script via the Monitor tool. The script is a read-only observer that emits one JSON event per line to stdout; Monitor delivers each line to your session as a notification, and you spawn one sub-agent worker per event to handle it.

## Prerequisites

Before doing anything, verify the environment:

1. **Check `gh` CLI is available:** Run `which gh`. If it fails, tell the user: "The `gh` CLI is required but not found on your PATH. Install it from https://cli.github.com/ and try again." Then stop.
2. **Check this is a git repo:** Run `git rev-parse --is-inside-work-tree`. If it fails, tell the user: "This command must be run from inside a git repository." Then stop.
3. **Check `jq` is available:** Run `which jq`. If it fails, tell the user: "The `jq` CLI is required but not found on your PATH. Install it via your package manager and try again." Then stop.
4. **Check `sqlite3` is available:** Run `which sqlite3`. If it fails, tell the user: "The `sqlite3` CLI is required but not found on your PATH. Install it via your package manager and try again." Then stop.
5. **Check `python3` is available:** Run `which python3`. If it fails, tell the user: "`python3` is required but not found on your PATH. Install it via your package manager and try again." Then stop. `python3` runs `assets/db.py`, the bound-parameter SQLite helper that owns all DB writes.
6. **Check `flock` is available:** Run `which flock`. If it fails, tell the user: "The `flock` CLI is required so parallel comment/build workers can commit safely in the shared worktree. Install it with `brew install flock` and try again." Then stop.

### First-time setup after upgrade

If you are upgrading from **any** prior version of this skill (the early filesystem-JSON version *or* the SQLite dispatcher version), you must run the one-time migration script before first use:

```
bash "${CLAUDE_SKILL_DIR}/assets/migrate.sh"
```

The script does three things, each idempotent:
- Imports any legacy per-PR JSON state files into the SQLite database at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`.
- Drops obsolete dispatcher/clustering tables (`clusters`, `worker_reports`, `pending_events`) carried over from the previous SQLite schema.
- Removes orphan dispatcher filesystem artifacts (`dispatch-*.log`, `dispatch-lock-*.d/`) that the current architecture never touches.

It is safe to re-run, but only needs to succeed once per machine. If you have never used this skill before, you can skip the migration; the schema bootstrap in Start mode will create a fresh database for you.

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

1. **`assets/poll.sh` — read-only observer.** Runs as a background process per monitored PR, launched via the Monitor tool with `persistent: true`. Monitor owns the process lifecycle and streams every stdout line to your session as a notification. On each cycle poll.sh polls GitHub for new review comments and Buildkite for failed builds, dedupes against the `seen_events` table, and emits one JSON event per unseen item to its stdout. Log lines (timestamped progress, errors) go to stderr — Monitor does not surface stderr as notifications, so the event stream stays clean. poll.sh is the **single writer** to `seen_events`; nothing else touches that table. It holds no locks and spawns no workers.

2. **Your user session — event reader.** Monitor delivers each stdout line from `poll.sh` to your session as a notification. Each line is one of these JSON event shapes:
   - `{"type":"comment_thread","pr":...,"repo":...,"branch":...,"thread_root_id":...,"new_comment_ids":[...],"comments":[...],"file":...,"line":...,"diff_hunk":...}`
   - `{"type":"build_failure","pr":...,"repo":...,"branch":...,"pipeline":...,"build_number":...,"state":"failed","jobs":[...]}`
   - `{"type":"error","kind":...,"pr":...,"message":...}` — `pr` is `null` for `kind:"init"` errors emitted before PR detection has succeeded; otherwise it is the integer PR number.

   For each event, spawn one sub-agent via the `Agent` tool, using `assets/comment-check-prompt.md` for `comment_thread` events and `assets/build-check-prompt.md` for `build_failure` events. Surface `error` events to the user and continue. The sub-agents do the actual work (replying to comments, pushing build fixes).

3. **Sub-agent workers.** Invoked via the `Agent` tool with the worker prompts at `assets/comment-check-prompt.md` and `assets/build-check-prompt.md`. They read the event payload from their prompt, take action against the PR, and return free-form text. They do **not** write to `seen_events` or any other DB table.

**State.** Single SQLite DB at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. All writes go through `db.py` (Python sqlite3 with `?` bound params and `with conn:` transactions). The schema has two tables:
- `seen_events` — dedup ledger keyed by `(repo, pr, kind, event_id)`. The `repo` column scopes every row so the same PR number across two repos never collides. Written only by `poll.sh`.
- `pipelines` — Buildkite pipeline slug per `owner/repo`. Written once during Start mode pipeline detection; read on subsequent runs.

There are no per-PR locks and no second long-running LLM process spawned by `poll.sh`. Everything that requires the LLM happens inside your session or inside the worker sub-agents you spawn.

**Concurrency.** Workers are dispatched in parallel but share one git worktree, so each commits via a `flock`-serialized, pathspec-scoped command (see the worker prompts) and the session owns the single push. Rationale and the revisit criteria live in `assets/CONCURRENCY.md`.

### Mode: Stop

If the remaining text is `stop` (case-insensitive):

Babysit is scoped to the PR of the current working directory's branch — Start mode refuses to run without one, and Stop mode targets that same single (repo, PR). It never stops pollers for other repos or PRs; to stop a different poller, run `/babysit stop` from that worktree. PID files are named `babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid`, where `<REPO_SAFE>` is `owner/repo` with every `/` replaced by `__`.

1. **Resolve the current (repo, PR)** exactly as Start mode does:
   - Run `gh repo view --json nameWithOwner --jq .nameWithOwner` to get **REPO**.
   - Run `gh pr view --json number --jq .number` to get **PR_NUMBER**.

   If **either** command fails (run from outside a git repo, or the current branch has no PR), there is nothing this invocation can act on. Print: "No PR found for the current branch — nothing to stop. Run /babysit stop from the worktree whose PR you want to stop." Then stop. **Do not** scan for or kill other pollers.

2. **Target the single PID file:** Compute `<REPO_SAFE>` and the path `${CLAUDE_PLUGIN_DATA}/babysit/babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid`.
   - If it does not exist, print: "No babysit poller running for <REPO>#<PR_NUMBER>." Then stop.
   - If it exists, read the PID with `cat`, `kill -TERM <pid>` (ignore errors — the process may already have exited), and remove the PID file.

3. **Print confirmation:** "Stopped babysit poller for <REPO>#<PR_NUMBER>."

Then stop — do not continue to Start mode.

### Mode: Clean

If the remaining text is `clean` (case-insensitive):

Clean mode delegates DB writes to `db.py` (the CLI at `${CLAUDE_SKILL_DIR}/assets/db.py`) and sweeps this skill's own stale poll logs. The `--dry-run` flag skips every destructive call — read-only queries and `ls`/`stat` checks still run so the dry-run summary is accurate. Ensure `BABYSIT_STATE_DB` is set or pass `--db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"` to each call (the examples below pass `--db` explicitly).

If `--dry-run` was specified, print a banner first:

```
=== DRY RUN — no changes will be written ===
```

1. **Locate the state database:** The database lives at `${CLAUDE_PLUGIN_DATA}/babysit/state.db`. If the file does not exist, skip steps 2–5 (DB cleanup) but still run step 6 (the poll-log sweep). If both the DB is missing AND no stale poll logs are found, print: "No babysit state found." Then stop.

2. **Collect tracked (repo, PR) pairs:** Ask the CLI for every (repo, PR) pair ever recorded in `seen_events`:
   ```
   python3 "${CLAUDE_SKILL_DIR}/assets/db.py" list_distinct_prs \
       --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"
   ```
   The CLI prints one JSON object on stdout of the shape `{"ok": true, "prs": [{"repo": "<owner/repo>", "pr": <pr>}, ...]}`. Extract the pairs as one JSON object per line:
   ```
   pairs=$(python3 "${CLAUDE_SKILL_DIR}/assets/db.py" list_distinct_prs \
              --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db" \
              | jq -c '.prs[]')
   ```
   If `$pairs` is empty, skip to step 6.

   Rows with the sentinel repo `legacy/unknown` are pre-v3 imports whose origin repo was not preserved. They are surfaced like any other pair — the GitHub state check below will fail with "Could not resolve" and they will be purged in step 4.

3. **Check each pair's GitHub state:** Iterate over `$pairs`. For each line, **unpack the JSON object into separate `REPO` and `PR_NUMBER` shell variables before substituting them into any command**. Pasting the raw `{"repo":...,"pr":...}` object into the command template below would make `gh` and `db.py` see a literal JSON blob as their argument, fail with an arg-parse error, and fall into the "transient skip" bucket — Clean mode would silently no-op on every closed PR.

   ```
   while IFS= read -r pair; do
       REPO=$(echo "$pair" | jq -r '.repo')
       PR_NUMBER=$(echo "$pair" | jq -r '.pr')
       gh pr view "$PR_NUMBER" --repo "$REPO" --json state --jq .state \
           2>/tmp/babysit-gh-err.$$
       # ... classify based on exit code and stderr, see below ...
   done <<< "$pairs"
   ```
   `--repo` is mandatory here — without it, `gh` resolves against the current working directory and a wrong-cwd invocation would misclassify every pair.

   Capture both the stdout (the state string) and the exit code (`$?`).

   Classify each pair as follows:
   - Exit code `0` and stdout is `OPEN`: **preserve** (still open).
   - Exit code `0` and stdout is `MERGED` or `CLOSED`: **mark for purge**.
   - Non-zero exit code AND the stderr file mentions "Could not resolve" or "no pull requests found" (i.e., the PR no longer exists / 404 / sentinel repo): **mark for purge**.
   - Non-zero exit code with any other stderr (network blip, auth error, rate limit): print "could not determine state for <REPO>#<PR_NUMBER>; skipping" and **do not purge** this pair.

   Clean up `/tmp/babysit-gh-err.$$` after each check.

4. **Purge marked pairs:** For each pair marked for purge:
   - Print the intended deletes, e.g.:
     ```
     Would purge <REPO>#<PR_NUMBER> (<state>): rows from seen_events
     ```
   - If `--dry-run` was specified, **skip the CLI call** — the line above is the only output for this pair.
   - If `--dry-run` was NOT specified, delegate the purge to `db.py`:
     ```
     python3 "${CLAUDE_SKILL_DIR}/assets/db.py" purge_pr \
         --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db" \
         --repo <REPO> \
         --pr <PR_NUMBER>
     ```
     The CLI deletes that pair's rows from `seen_events` in a single transaction and prints `{"ok": true, "counts": {...}}`. Then report: "Purged <REPO>#<PR_NUMBER> (<state>)."
   - For preserved pairs, report: "Preserved <REPO>#<PR_NUMBER> (still open)."

5. **Vacuum:** If `--dry-run` was specified, **skip this step entirely**. Otherwise reclaim space:
   ```
   python3 "${CLAUDE_SKILL_DIR}/assets/db.py" vacuum \
       --db "${CLAUDE_PLUGIN_DATA}/babysit/state.db"
   ```

6. **Sweep old poll logs:** Remove stale `poll-*.log` files under `${CLAUDE_PLUGIN_DATA}/babysit/` with mtime older than 7 days:
   - `find "${CLAUDE_PLUGIN_DATA}/babysit" -maxdepth 1 -name 'poll-*.log' -mtime +7` (the directory may not exist — skip silently if `find` reports it missing).
   - For each match, print: "Would remove old poll log <path>."
   - If `--dry-run` was NOT specified, `rm -f <path>`.

7. **Print summary:** Print a final summary block. If `--dry-run` was specified, prefix the summary with the dry-run banner repeated:
    ```
    === DRY RUN — no changes were written ===
    ```
    The summary must list:
    - Count of PRs purged (or that would be purged in dry-run)
    - Count of PRs preserved (still open)
    - Count of PRs skipped due to transient errors (if any)
    - Count of old poll logs removed

    Example:
    ```
    Babysit clean summary:
    - PRs purged:        3
    - PRs preserved:     2
    - PRs skipped:       0
    - Logs removed:      14
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

Launch `poll.sh` as a background process via the **Monitor** tool. Monitor owns the process lifecycle and streams each stdout line from `poll.sh` to your session as a notification. The polling script runs detached as a read-only observer — it emits one JSON event per line to stdout for each unseen comment thread or build failure, and never spawns workers itself. You react to each notification by dispatching one sub-agent worker per event.

**Monitor tool call:**

- **command:** see construction rules below.
- **persistent:** `true` (session-length watch; Monitor's default 5-minute timeout is wrong for babysit).
- **description:** `babysit poll PR #<PR_NUMBER>` (with actual PR number).

You do **not** need to capture any handle. Monitor delivers events automatically as notifications; there is no shell-id to track. Stop mode terminates `poll.sh` via its own PID file (see below).

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

`poll.sh` writes its own numeric `$$` to `${CLAUDE_PLUGIN_DATA}/babysit/babysit-pid-<REPO_SAFE>-<PR_NUMBER>.pid` as its first action and clears the file on graceful exit. Stop mode reads that file directly to send `kill -TERM`.

`<REPO_SAFE>` is `owner/repo` with each `/` replaced by `__` (e.g. `myorg/myrepo` → `myorg__myrepo`). The same scoping convention applies to any per-PR artifact so multiple repos with PRs of the same number can run concurrently without colliding.

## Confirmation Message

Immediately after launching `poll.sh` (and before reacting to any events in the next section), print exactly:

```
Babysitting PR #<PR_NUMBER>. Listening for events…
```

Substitute `<PR_NUMBER>` with the detected PR number. Keep this line short — it is the only output the user expects before events start arriving. Do not print a multi-line status block, do not print the PID file path.

## Start Mode — Event Handling

Once the confirmation line is printed, your job is to react to each Monitor notification by dispatching one sub-agent worker per event. This continues for the lifetime of your session (or until the user issues `/babysit stop`).

### Reading events

Each Monitor notification carries one or more new stdout lines from `poll.sh`. Treat every non-empty line as a single JSON event — `poll.sh` guarantees one event per line and never emits partial lines. (Log output from `poll.sh` goes to stderr and never reaches you as a notification; Monitor only delivers stdout.)

For each line you receive:

1. Parse it as JSON (use `jq` if you need to, but inline JSON parsing inside this session is fine since the lines are small and well-formed).
2. Read the `.type` field to decide how to route it.
3. Dispatch as described under "Routing events" below.

If a line fails to parse as JSON, surface it to the user verbatim (prefix with `babysit: malformed event:` so they can tell it apart from real worker output) and continue. Do not stop the loop on parse errors.

### Routing events

There are exactly three event shapes, all emitted by `poll.sh`:

- **`{"type":"comment_thread","pr":...,"repo":...,"branch":...,"thread_root_id":...,"new_comment_ids":[...],"comments":[{id,user.login,body,created_at,in_reply_to_id},...],"file":...,"line":...,"diff_hunk":...}`** — a new (or newly-extended) review-comment thread on the PR. `new_comment_ids` is the subset of comment ids the worker must respond to; `comments` is the full thread (sorted by `created_at`) for context.
- **`{"type":"build_failure","pr":...,"repo":...,"branch":...,"pipeline":...,"build_number":...,"state":"failed","jobs":[{id,name,state},...]}`** — a Buildkite build for this branch that has finished in a failed state.
- **`{"type":"error","kind":...,"pr":...,"message":...}`** — `poll.sh` itself hit a recoverable problem (API failure, missing field, etc.) and wants to surface it.

Handle each `.type` as follows:

**`comment_thread` → spawn a comment-check sub-agent.** Use the Agent (sub-agent) tool. Load the worker prompt from `${CLAUDE_SKILL_DIR}/assets/comment-check-prompt.md` and use it as the sub-agent's system prompt / instructions. Pass the event payload as the sub-agent's user message — the JSON line you just parsed, verbatim, inside a fenced ```json block. If freeform instructions were captured during Argument Parsing, prepend them to the user message above the JSON block under a `Freeform instructions:` header so the sub-agent sees them in context.

**`build_failure` → spawn a build-check sub-agent.** Same shape as above, but load the worker prompt from `${CLAUDE_SKILL_DIR}/assets/build-check-prompt.md` instead. Pass the event payload as the user message in a fenced ```json block, with any freeform instructions prepended exactly as for `comment_thread`.

**`error` → surface to the user.** Print a short line of the form `babysit poller error (<kind>): <message>` and then continue reading new events. Do not spawn a sub-agent and do not stop the loop. If `error` events arrive repeatedly with the same `kind`, the user can decide whether to `/babysit stop`.

### Pushing fixes

Both `comment_thread` and `build_failure` workers **commit but do not push** — pushing is the orchestrating session's job. The worker prompts forbid the worker from pushing because parallel workers would race on `git push`; the session is the single pusher.

After every worker in a batch has returned, inspect their reports:

- If **any** worker reports a landed commit (a non-empty `commit_sha`, or its prose says it committed a fix), run `git push` from the session **once** for the whole batch — all commits are on the same branch, so one push carries them all.
- If no worker landed a commit (all DISAGREE / ESCALATE / retry / skip / branch mismatch), do **not** push.
- If `git push` fails (rejected, diverged), do not force-push. Surface the failure to the user verbatim: `babysit: push failed after fixes — resolve manually then re-run /babysit`. The commits stay local; the reviewer threads already have the workers' replies.

### Parallelism

If a single Monitor notification batches multiple new event lines (because two or more events arrived inside the same poll cycle — Monitor groups stdout lines within 200ms), spawn **all** of the resulting sub-agents in a **single assistant message** with one Agent tool call per event. Workers share one git worktree but commit safely via `flock` + pathspec (see worker prompts), and the session owns the single push.

If the same notification includes an `error` event alongside `comment_thread` / `build_failure` events, print the error line in the same assistant message as the sub-agent calls — the user-visible text and the tool calls can coexist.

### Stopping the loop

The loop ends when one of the following happens:

- The user issues `/babysit stop`, which sends `kill -TERM` to `poll.sh` via its PID file. Monitor reports the process exited; no further notifications arrive.
- The user kills the session.
- `poll.sh` exits on its own (repeated fatal errors, etc.). Monitor reports the process exited with status `<N>`; surface that to the user as `babysit poller exited (status=<N>). To restart, run /babysit again.` and stop reading.

Do not attempt to restart `poll.sh` automatically — if the poller dies, the user gets to decide whether to relaunch.
