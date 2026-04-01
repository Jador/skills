---
name: mq
description: Monitor merge queue and auto-retry failed Buildkite jobs
argument-hint: "[stop]"
disable-model-invocation: true
---

# Merge Queue Monitor Skill

You monitor the GitHub merge queue for the current branch's PR, detect Buildkite CI failures, and automatically retry failed jobs. After 3 failed retry attempts, you notify the user.

## Prerequisites

Before doing anything, verify the environment:

1. **Check `gh` CLI is available:** Run `which gh`. If it fails, tell the user: "The `gh` CLI is required but not found on your PATH. Install it from https://cli.github.com/ and try again." Then stop.
2. **Check `bk` CLI is available:** Run `which bk`. If it fails, tell the user: "The `bk` CLI is required but not found on your PATH. Install it from https://github.com/buildkite/cli and try again." Then stop.
3. **Check this is a git repo:** Run `git rev-parse --is-inside-work-tree`. If it fails, tell the user: "This command must be run from inside a git repository." Then stop.

## Argument Parsing

Parse `$ARGUMENTS` to determine the mode of operation.

### Mode 1: Stop

If `$ARGUMENTS` is `stop` (case-insensitive):

1. **List all cron jobs:** Use `CronList` to retrieve all currently active cron jobs.

2. **Filter for mq jobs:** Examine each job's prompt for the tag `[mq:`. Any job whose prompt contains this tag is an mq monitor job. If no matching jobs are found, print: "No merge queue monitor jobs are currently running." Then stop.

3. **Extract PR numbers:** For each matching job, extract the PR number from the tag. The tag format is `[mq:<PR_NUMBER>]` — parse the number between the colon and closing bracket. Collect the unique set of PR numbers across all matching jobs.

4. **Delete each matching job:** Use `CronDelete` to remove each matching cron job by its ID.

5. **Clean up state files:** For each unique PR number found in step 3, remove the corresponding state files from the plugin data directory at `${CLAUDE_PLUGIN_DATA}/mq/`:
   - `${CLAUDE_PLUGIN_DATA}/mq/<PR_NUMBER>-retry-state.json`

   Use `rm -f` so that missing files do not cause errors.

6. **Print confirmation:** Print a summary message listing how many cron jobs were stopped and which PR number(s) they were for. For example: "Stopped 1 merge queue monitor job for PR(s): #123."

Then stop — do not continue to Repository Detection or any other steps.

### Mode 2: No arguments (auto-detect PR)

If `$ARGUMENTS` is empty or blank:

1. Detect the current branch's PR number by running:
   ```
   gh pr view --json number,headRefName
   ```
2. If the command fails (no PR exists for the current branch), tell the user: "No open PR found for the current branch. Push your branch and open a PR first." Then stop.
3. Store the PR number and branch name from the output.

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

Once argument parsing, repo detection, and pipeline detection are complete, set up the cron-based polling loop.

### Step 1: Create Data Directory

Run:

```
mkdir -p ${CLAUDE_PLUGIN_DATA}/mq
```

This ensures the state directory exists for the cron agent to write to.

### Step 2: Create Queue-Check Cron Job

Use `CronCreate` with:
- **schedule**: `*/2 * * * *`
- **prompt**: a thin delegation wrapper that reads the asset file, interpolates variables, and delegates to a sub-agent. The prompt should be (with `<REPO>`, `<PR_NUMBER>`, `<BRANCH_NAME>`, and `<PIPELINE>` replaced by their actual detected values):

```
[mq:<PR_NUMBER>] Read the file `${CLAUDE_SKILL_DIR}/assets/queue-check-prompt.md`. In its contents, replace `<REPO>` with `<REPO>`, `<PR_NUMBER>` with `<PR_NUMBER>`, `<BRANCH_NAME>` with `<BRANCH_NAME>`, and `<PIPELINE>` with `<PIPELINE>`. Then pass the fully interpolated prompt to the Agent tool with description "mq-check PR #<PR_NUMBER>". Print the sub-agent's returned summary.
```

In the prompt text above, all template variables (`<REPO>`, `<PR_NUMBER>`, `<BRANCH_NAME>`, `<PIPELINE>`) must be replaced with the actual values detected earlier — the cron prompt is stored with those values baked in. At cron execution time, the cron agent will read the asset file, perform the replacements, and delegate to a sub-agent via the Agent tool.

### Step 3: Print Confirmation

Print a confirmation message. Replace `<REPO>`, `<PR_NUMBER>`, `<BRANCH_NAME>`, and `<PIPELINE>` with the actual values:

```
Merge queue monitor started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Pipeline: <PIPELINE>
- Queue check: every 2 minutes
```

Then stop.
