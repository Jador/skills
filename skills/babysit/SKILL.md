---
name: babysit
description: Monitor a PR for review comments and build failures
argument-hint: "[<pr-number> | stop] [--no-comments] [--no-builds]"
disable-model-invocation: true
---

# Babysit Skill

You monitor an open PR for review comments and build failures, automatically addressing feedback and fixing broken builds.

## Prerequisites

Before doing anything, verify the environment:

1. **Check `gh` CLI is available:** Run `which gh`. If it fails, tell the user: "The `gh` CLI is required but not found on your PATH. Install it from https://cli.github.com/ and try again." Then stop.
2. **Check this is a git repo:** Run `git rev-parse --is-inside-work-tree`. If it fails, tell the user: "This command must be run from inside a git repository." Then stop.

## Argument Parsing

Parse `$ARGUMENTS` to determine the mode of operation. Before mode detection, extract any flags from `$ARGUMENTS`:

- `--no-comments` — disables the comment-check cron job
- `--no-builds` — disables the build-check cron job

Strip these flags from `$ARGUMENTS` before proceeding with mode detection below. The remaining text (after flag removal and trimming whitespace) is used for mode selection.

If both `--no-comments` and `--no-builds` are specified, tell the user: "Both checks are disabled — nothing to monitor." Then stop.

### Mode 1: No arguments (auto-detect PR)

If the remaining `$ARGUMENTS` is empty or blank:

1. Detect the current branch's PR number by running:
   ```
   gh pr view --json number,headRefName --jq .number
   ```
2. If the command fails (no PR exists for the current branch), tell the user: "No open PR found for the current branch. Push your branch and open a PR first, or pass a PR number explicitly." Then stop.
3. Store the PR number from the output.
4. Capture the branch name by running:
   ```
   gh pr view --json headRefName --jq .headRefName
   ```

### Mode 2: Numeric argument (explicit PR number)

If the remaining `$ARGUMENTS` is a number (matches `^[0-9]+$`):

1. Use that number as the PR number.
2. Verify the PR exists by running:
   ```
   gh pr view $ARGUMENTS --json number,headRefName --jq .headRefName
   ```
3. If the command fails, tell the user: "PR #$ARGUMENTS not found in this repository." Then stop.
4. Store the PR number and the branch name from the output.

### Mode 3: Stop

If the remaining `$ARGUMENTS` is `stop` (case-insensitive):

1. **List all cron jobs:** Use `CronList` to retrieve all currently active cron jobs.

2. **Filter for babysitter jobs:** Examine each job's prompt for the tag `[babysit:`. Any job whose prompt contains this tag is a babysitter job. If no matching jobs are found, print: "No babysitter jobs are currently running." Then stop.

3. **Extract PR numbers:** For each matching job, extract the PR number from the tag. The tag format is `[babysit:<PR_NUMBER>]` — parse the number between the colon and closing bracket. Collect the unique set of PR numbers across all matching jobs.

4. **Delete each matching job:** Use `CronDelete` to remove each matching cron job by its ID.

5. **Clean up state files:** For each unique PR number found in step 3, remove the corresponding state files from the plugin data directory at `${CLAUDE_PLUGIN_DATA}/babysit/`:
   - `${CLAUDE_PLUGIN_DATA}/babysit/<PR_NUMBER>-seen-comments.json`
   - `${CLAUDE_PLUGIN_DATA}/babysit/<PR_NUMBER>-seen-builds.json`

   Use `rm -f` so that missing files do not cause errors.

6. **Print confirmation:** Print a summary message listing how many cron jobs were stopped and which PR number(s) they were for. For example: "Stopped 4 babysitter jobs for PR(s): #123, #456."

Then stop — do not continue to Repository Detection or Next Steps.

## Repository Detection

After determining the PR number and branch name, detect the repository owner and name dynamically:

```
gh repo view --json nameWithOwner --jq .nameWithOwner
```

Store the result (e.g., `owner/repo-name`) alongside the PR number and branch name. These three values — **repo** (`owner/name`), **PR number**, and **branch name** — are used by downstream cron job setup.

## Pipeline Detection

**Skip this section entirely if `--no-builds` was specified.** Pipeline detection is only needed for the build-check cron job.

After determining the repo, detect the Buildkite pipeline for this repository:

1. Run:
   ```
   bk pipeline list --json | jq -r '.[].slug'
   ```
2. If exactly one pipeline is returned, use it.
3. If multiple pipelines are returned, filter out secondary pipelines — those whose slug contains `publish`, `deploy`, `release`, or `rosetta`. If exactly one remains after filtering, use it.
4. If zero or multiple pipelines remain after filtering, print the list and ask the user to choose:
   ```
   Multiple Buildkite pipelines found for this repo:
   - <slug-1>
   - <slug-2>
   Which pipeline should I monitor?
   ```
   Then stop and wait for the user's response.
5. Store the selected pipeline slug as the **pipeline** value. This — along with **repo**, **PR number**, and **branch name** — is used by downstream cron job setup.

## Start Monitoring

Once argument parsing, repo detection, and pipeline detection are complete, set up the two independent cron-based polling loops.

### Step 1: Check for Branch Divergence

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

### Step 2: (Comment-Check Prompt)

No pre-interpolation needed — the comment-check cron wrapper (Step 5) reads and interpolates the asset file at execution time, then delegates to a sub-agent.

### Step 3: (Build-Check Prompt)

No pre-interpolation needed — the build-check cron wrapper (Step 6) reads and interpolates the asset file at execution time, then delegates to a sub-agent.

### Step 4: Create Data Directory

Run:

```
mkdir -p ${CLAUDE_PLUGIN_DATA}/babysit
```

This ensures the state directory exists for the cron agents to write to.

### Step 5: Create Comment-Check Cron Job

**Skip this step if `--no-comments` was specified.**

Use `CronCreate` with:
- **schedule**: `*/5 * * * *`
- **prompt**: a thin delegation wrapper that reads the asset file, interpolates variables, and delegates to a sub-agent. The prompt should be (with `<REPO>`, `<PR_NUMBER>`, and `<BRANCH_NAME>` replaced by their actual detected values):

```
[babysit:<PR_NUMBER>] Read the file `${CLAUDE_SKILL_DIR}/assets/comment-check-prompt.md`. In its contents, replace `<REPO>` with `<REPO>`, `<PR_NUMBER>` with `<PR_NUMBER>`, and `<BRANCH_NAME>` with `<BRANCH_NAME>`. Then pass the fully interpolated prompt to the Agent tool with description "comment-check PR #<PR_NUMBER>". Print the sub-agent's returned summary.
```

In the prompt text above, all template variables (`<REPO>`, `<PR_NUMBER>`, `<BRANCH_NAME>`) must be replaced with the actual values detected earlier — the cron prompt is stored with those values baked in. At cron execution time, the cron agent will read the asset file, perform the replacements, and delegate to a sub-agent via the Agent tool.

### Step 6: Create Build-Check Cron Job

**Skip this step if `--no-builds` was specified.**

Use `CronCreate` with:
- **schedule**: `*/2 * * * *`
- **prompt**: a thin delegation wrapper that reads the asset file, interpolates variables, and delegates to a sub-agent. The prompt should be (with `<REPO>`, `<PR_NUMBER>`, `<BRANCH_NAME>`, and `<PIPELINE>` replaced by their actual detected values):

```
[babysit:<PR_NUMBER>] Read the file `${CLAUDE_SKILL_DIR}/assets/build-check-prompt.md`. In its contents, replace `<REPO>` with `<REPO>`, `<PR_NUMBER>` with `<PR_NUMBER>`, `<BRANCH_NAME>` with `<BRANCH_NAME>`, and `<PIPELINE>` with `<PIPELINE>`. Then pass the fully interpolated prompt to the Agent tool with description "build-check PR #<PR_NUMBER>". Print the sub-agent's returned summary.
```

In the prompt text above, all template variables (`<REPO>`, `<PR_NUMBER>`, `<BRANCH_NAME>`, `<PIPELINE>`) must be replaced with the actual values detected earlier — the cron prompt is stored with those values baked in. At cron execution time, the cron agent will read the asset file, perform the replacements, and delegate to a sub-agent via the Agent tool.

### Step 6.5: Run Immediate Comment Check

**Skip this step if `--no-comments` was specified.**

Before printing the confirmation message, run an immediate comment check so the user gets instant feedback on any existing review comments without waiting for the first cron tick.

1. Read the file `${CLAUDE_SKILL_DIR}/assets/comment-check-prompt.md`.
2. In its contents, replace `<REPO>` with the detected repo value, `<PR_NUMBER>` with the detected PR number, and `<BRANCH_NAME>` with the detected branch name.
3. Pass the fully interpolated prompt to the **Agent** tool with description `"comment-check PR #<PR_NUMBER>"` (with `<PR_NUMBER>` replaced by the actual PR number).
4. Print the sub-agent's returned summary.

> **Note:** This step should run in parallel with the immediate build-check step (if present). Both immediate checks can be dispatched as simultaneous Agent tool calls.

### Step 7: Print Confirmation

Print a confirmation message listing only the checks that were enabled. Replace `<REPO>`, `<PR_NUMBER>`, and `<BRANCH_NAME>` with the actual values.

If both checks are enabled (default):

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Review comments: checking every 5 minutes
- Build status: checking every 2 minutes
```

If only comments are enabled (`--no-builds`):

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Review comments: checking every 5 minutes
- Build status: disabled
```

If only builds are enabled (`--no-comments`):

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Review comments: disabled
- Build status: checking every 2 minutes
```

Then stop.
