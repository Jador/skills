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

## Argument Parsing

Parse `$ARGUMENTS` to determine the mode of operation. Before mode detection, extract any flags from `$ARGUMENTS`:

- `--no-comments` — disables comment monitoring
- `--no-builds` — disables build monitoring

Strip these flags from `$ARGUMENTS` before proceeding with mode detection below. The remaining text (after flag removal and trimming whitespace) is used for mode selection.

If both `--no-comments` and `--no-builds` are specified, tell the user: "Both checks are disabled — nothing to monitor." Then stop.

### Mode: Stop

If the remaining text is `stop` (case-insensitive):

1. **List running tasks:** Use `TaskList` to retrieve all currently running tasks.
2. **Filter for babysit monitors:** Examine each task's description for matches beginning with `babysit-monitor`. If no matching tasks are found, print: "No babysit monitors are currently running." Then stop.
3. **Stop each match:** Use `TaskStop` on each matching task by its ID.
4. **Remove poll lockfile:** Remove the poll lockfile if it exists: `rm -f ${CLAUDE_PLUGIN_DATA}/babysit/poll.lock`.
5. **Print confirmation:** Print: "Stopped N babysit monitor(s)." (where N is the count of stopped tasks).

Then stop — do not continue to Start mode.

### Mode: Clean

If the remaining text is `clean` (case-insensitive):

1. **Scan for state files:** List all files in `${CLAUDE_PLUGIN_DATA}/babysit/` matching the patterns `*-seen-comments.json` and `*-seen-builds.json`. If no matching files exist, print: "No babysit state files found." Then stop.

2. **Extract PR numbers:** From the matching filenames, extract the PR number portion. Filenames follow the pattern `<PR_NUMBER>-seen-comments.json` and `<PR_NUMBER>-seen-builds.json`. Collect the unique set of PR numbers.

3. **Check PR status:** For each unique PR number, run:
   ```
   gh pr view <PR_NUMBER> --json state --jq .state
   ```

4. **Clean or preserve:** For each PR number:
   - If the state is `MERGED` or `CLOSED`: delete both `${CLAUDE_PLUGIN_DATA}/babysit/<PR_NUMBER>-seen-comments.json` and `${CLAUDE_PLUGIN_DATA}/babysit/<PR_NUMBER>-seen-builds.json` using `rm -f`. Report: "Cleaned state for PR #<PR_NUMBER> (<state>)."
   - If the state is `OPEN`: skip deletion. Report: "Preserved state for PR #<PR_NUMBER> (still open)."

5. **Remove poll lockfile:** Remove the poll lockfile if it exists: `rm -f ${CLAUDE_PLUGIN_DATA}/babysit/poll.lock`.
6. **Print summary:** Print a summary listing all PRs that were cleaned and all that were preserved.

Then stop — do not continue to Start mode.

### Mode: Start (default)

If the remaining text is neither `stop` nor `clean` (case-insensitive), enter Start mode. Any remaining text after flag removal is the **freeform instructions** string. Store it for later use (it will be passed to sub-agents as `<FREEFORM_INSTRUCTIONS>`). If the remaining text is empty or blank, the freeform instructions value is `"None"`.

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

1. **Check for saved pipeline:** Read `${CLAUDE_PLUGIN_DATA}/babysit/pipelines.json` if it exists. Parse it as a JSON object mapping `owner/repo` to pipeline slug. If an entry exists for the current **REPO**, use that slug as **PIPELINE**. Skip to Branch Divergence Check.

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

4. **Save pipeline:** Save the selected pipeline to `${CLAUDE_PLUGIN_DATA}/babysit/pipelines.json`. The file is a JSON object mapping `owner/repo` to slug. If the file already exists, merge the new entry into the existing object. If it does not exist, create it with the single entry.

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
mkdir -p ${CLAUDE_PLUGIN_DATA}/babysit
```

This ensures the state directory exists for the polling script to write to.

## Start Mode — Launch Monitor

Use the `Monitor` tool to start the background polling process with:

- **command**: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "<PIPELINE>" --interval 30 --no-comments` (see construction rules below)
- **description**: `babysit-monitor PR #<PR_NUMBER>` (with actual PR number)
- **persistent**: `true`

**Command construction rules:**
- Always include the env prefix: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}"`
- Always include: `bash "${CLAUDE_SKILL_DIR}/assets/poll.sh"`
- If builds are enabled (no `--no-builds` flag): include the **PIPELINE** slug as the first positional argument. If builds are disabled (`--no-builds`): omit the pipeline argument entirely.
- Always include: `--interval 30`
- If `--no-comments` was specified: include `--no-comments`. Otherwise omit it.

Examples:
- Both enabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "my-pipeline" --interval 30`
- Comments disabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" "my-pipeline" --interval 30 --no-comments`
- Builds disabled: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" bash "${CLAUDE_SKILL_DIR}/assets/poll.sh" --interval 30`

## Start Mode — Dispatch Instructions

When a `<task-notification>` arrives from the monitor, parse each line of the notification body as a JSON object. Each JSON object has a `type` field.

### Lock acquisition

Before dispatching any sub-agents for a notification, acquire a lockfile to suppress polling during processing:

```
touch ${CLAUDE_PLUGIN_DATA}/babysit/poll-$$.lock
```

This lock wraps the entire notification batch — acquire it once before handling any events from the notification.

Handle each event according to its type:

### Event type: `"comment"`

1. Read the file `${CLAUDE_SKILL_DIR}/assets/comment-check-prompt.md`.
2. In its contents, replace:
   - `<REPO>` with the detected **REPO** value
   - `<PR_NUMBER>` with the detected **PR_NUMBER** value
   - `<BRANCH_NAME>` with the detected **BRANCH_NAME** value
   - `<EVENT_JSON>` with the full JSON line from the notification
   - `<FREEFORM_INSTRUCTIONS>` with the stored freeform instructions string (or `"None"` if none were provided)
3. Pass the fully interpolated prompt to the **Agent** tool with description `"comment-check PR #<PR_NUMBER>"` (with actual PR number).
4. Print the sub-agent's returned summary.

### Event type: `"build_failure"`

1. Read the file `${CLAUDE_SKILL_DIR}/assets/build-check-prompt.md`.
2. In its contents, replace:
   - `<REPO>` with the detected **REPO** value
   - `<PR_NUMBER>` with the detected **PR_NUMBER** value
   - `<BRANCH_NAME>` with the detected **BRANCH_NAME** value
   - `<PIPELINE>` with the detected **PIPELINE** value
   - `<EVENT_JSON>` with the full JSON line from the notification
   - `<FREEFORM_INSTRUCTIONS>` with the stored freeform instructions string (or `"None"` if none were provided)
3. Pass the fully interpolated prompt to the **Agent** tool with description `"build-check PR #<PR_NUMBER>"` (with actual PR number).
4. Print the sub-agent's returned summary.

### Event type: `"error"`

Print a warning message: "Polling degraded: <message>. Will retry next cycle." (where `<message>` is the value of the `message` field from the JSON event). Do **not** dispatch a sub-agent for error events.

### Multiple events in one notification

If a single notification contains multiple JSON lines, dispatch a **separate sub-agent** for each `"comment"` or `"build_failure"` event. Error events are handled inline (print warning only). All sub-agent dispatches for a single notification may be launched in parallel.

### Lock release

After all sub-agents for a notification have returned (or if there were only error events and no agents were dispatched), release the lockfile:

```
rm -f ${CLAUDE_PLUGIN_DATA}/babysit/poll-$$.lock
```

This ensures the lock is held for the entire notification batch and released only once all processing is complete.

## Confirmation Message

After the Monitor is launched, print the following confirmation message (replacing placeholders with actual values):

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Monitoring: every 30 seconds
- Review comments: enabled/disabled
- Build status: enabled/disabled (pipeline: <PIPELINE>)
```

For the "Review comments" line: print `enabled` if comment monitoring is active, `disabled` if `--no-comments` was specified.

For the "Build status" line: print `enabled (pipeline: <PIPELINE>)` if build monitoring is active (with actual pipeline slug), or `disabled` if `--no-builds` was specified.

Then stop.
