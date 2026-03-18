---
name: hg:babysit
description: Monitor a PR for review comments and build failures
argument-hint: [<pr-number> | stop]
disable-model-invocation: true
---

# Babysit Skill

You monitor an open PR for review comments and build failures, automatically addressing feedback and fixing broken builds.

## Prerequisites

Before doing anything, verify the environment:

1. **Check `gh` CLI is available:** Run `which gh`. If it fails, tell the user: "The `gh` CLI is required but not found on your PATH. Install it from https://cli.github.com/ and try again." Then stop.
2. **Check this is a git repo:** Run `git rev-parse --is-inside-work-tree`. If it fails, tell the user: "This command must be run from inside a git repository." Then stop.

## Argument Parsing

Parse `$ARGUMENTS` to determine the mode of operation:

### Mode 1: No arguments (auto-detect PR)

If `$ARGUMENTS` is empty or blank:

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

If `$ARGUMENTS` is a number (matches `^[0-9]+$`):

1. Use that number as the PR number.
2. Verify the PR exists by running:
   ```
   gh pr view $ARGUMENTS --json number,headRefName --jq .headRefName
   ```
3. If the command fails, tell the user: "PR #$ARGUMENTS not found in this repository." Then stop.
4. Store the PR number and the branch name from the output.

### Mode 3: Stop

If `$ARGUMENTS` is `stop` (case-insensitive):

1. **List all cron jobs:** Use `CronList` to retrieve all currently active cron jobs.

2. **Filter for babysitter jobs:** Examine each job's prompt for the tag `[babysit:`. Any job whose prompt contains this tag is a babysitter job. If no matching jobs are found, print: "No babysitter jobs are currently running." Then stop.

3. **Extract PR numbers:** For each matching job, extract the PR number from the tag. The tag format is `[babysit:<PR_NUMBER>]` — parse the number between the colon and closing bracket. Collect the unique set of PR numbers across all matching jobs.

4. **Delete each matching job:** Use `CronDelete` to remove each matching cron job by its ID.

5. **Clean up state files:** For each unique PR number found in step 3, remove the corresponding state files from the `data/` directory (relative to this skill's directory):
   - `data/<PR_NUMBER>-seen-comments.json`
   - `data/<PR_NUMBER>-seen-builds.json`

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

### Step 2: Read and Interpolate the Comment-Check Prompt

Use the `Read` tool to read the file `assets/comment-check-prompt.md` (relative to this skill's directory).

In the file contents, replace all occurrences of:
- `<REPO>` with the detected repo name (e.g., `owner/repo-name`)
- `<PR_NUMBER>` with the PR number
- `<BRANCH_NAME>` with the branch name

Store the interpolated result as `comment_check_prompt`.

### Step 3: Read and Interpolate the Build-Check Prompt

Use the `Read` tool to read the file `assets/build-check-prompt.md` (relative to this skill's directory).

In the file contents, replace all occurrences of:
- `<REPO>` with the detected repo name
- `<PR_NUMBER>` with the PR number
- `<BRANCH_NAME>` with the branch name
- `<PIPELINE>` with the detected pipeline slug

Store the interpolated result as `build_check_prompt`.

### Step 4: Create Data Directory

Run:

```
mkdir -p <skill-directory>/data
```

This ensures the state directory exists for the cron agents to write to.

### Step 5: Create Comment-Check Cron Job

Use `CronCreate` with:
- **schedule**: `*/5 * * * *`
- **prompt**: the interpolated `comment_check_prompt` from Step 2

### Step 6: Create Build-Check Cron Job

Use `CronCreate` with:
- **schedule**: `*/2 * * * *`
- **prompt**: the interpolated `build_check_prompt` from Step 3

### Step 7: Print Confirmation

Print the following confirmation message:

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Review comments: checking every 5 minutes
- Build status: checking every 2 minutes
```

Replace `<REPO>`, `<PR_NUMBER>`, and `<BRANCH_NAME>` with the actual values. Then stop.
