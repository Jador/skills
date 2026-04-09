---
name: babysit
description: Monitor a PR for review comments and build failures
argument-hint: "[stop | clean] [--no-comments] [--no-builds] [\"instructions\"]"
disable-model-invocation: true
---

# Babysit Skill

You monitor an open PR for review comments and build failures, automatically addressing feedback and fixing broken builds. This skill is a thin launcher that delegates to the channel's MCP tools.

## Prerequisites

Before doing anything, verify the environment:

1. **Check `gh` CLI is available:** Run `which gh`. If it fails, tell the user: "The `gh` CLI is required but not found on your PATH. Install it from https://cli.github.com/ and try again." Then stop.
2. **Check this is a git repo:** Run `git rev-parse --is-inside-work-tree`. If it fails, tell the user: "This command must be run from inside a git repository." Then stop.

## Argument Parsing

Parse `$ARGUMENTS` to determine the mode of operation. Before mode detection, extract any flags from `$ARGUMENTS`:

- `--no-comments` — disables comment monitoring
- `--no-builds` — disables build monitoring

Strip these flags from `$ARGUMENTS` before proceeding with mode detection below. The remaining text (after flag removal and trimming whitespace) is used for mode selection.

If both `--no-comments` and `--no-builds` are specified, tell the user: "Both checks are disabled — nothing to monitor." Then stop.

### Mode Detection

After stripping flags, examine the remaining text:

- **Empty** — start mode, auto-detect PR
- **`stop`** (case-insensitive) — stop mode
- **`clean`** (case-insensitive) — clean mode
- **Any other text** (quoted or unquoted) — start mode with freeform instructions, auto-detect PR

## Stop Mode

If the remaining argument is `stop`:

1. Call `mcp__babysit__unwatch` with no arguments.
2. Print the result from the tool.

Then stop — do not continue to any other section.

## Clean Mode

If the remaining argument is `clean`:

1. Call `mcp__babysit__clean` with no arguments.
2. Print the returned summary.

Then stop — do not continue to any other section.

## Start Mode (Auto-Detect PR)

This mode handles both the no-argument case and the freeform-instructions case. If there was remaining text after flag extraction (and it was not `stop` or `clean`), store it as the **instructions** value. Otherwise, instructions is empty.

### Step 1: Detect PR

Detect the current branch's PR by running:

```
gh pr view --json number,headRefName
```

If the command fails (no PR exists for the current branch), tell the user: "No open PR found for the current branch. Push your branch and open a PR first." Then stop.

Parse the output to extract the **PR number** and **branch name**.

### Step 2: Detect Repository

Run:

```
gh repo view --json nameWithOwner --jq .nameWithOwner
```

Store the result (e.g., `owner/repo-name`) as the **repo** value.

### Step 3: Detect Pipeline

**Skip this step if `--no-builds` was specified.** Pipeline detection is only needed for build monitoring.

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
5. Store the selected pipeline slug as the **pipeline** value.

### Step 4: Call Watch

Call `mcp__babysit__watch` with the following arguments:

- `repo`: the detected repo value (e.g., `owner/repo-name`)
- `pr_number`: the detected PR number
- `branch`: the detected branch name
- `pipeline`: the detected pipeline slug (omit if `--no-builds` was specified)
- `instructions`: the freeform instructions string (omit if empty)
- `no_comments`: `true` if `--no-comments` was specified, otherwise omit or pass `false`
- `no_builds`: `true` if `--no-builds` was specified, otherwise omit or pass `false`

### Step 5: Print Confirmation

Print a confirmation message listing only the checks that were enabled. Replace placeholders with actual values.

If both checks are enabled (default):

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Review comments: watching
- Build status: watching
```

If only comments are enabled (`--no-builds`):

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Review comments: watching
- Build status: disabled
```

If only builds are enabled (`--no-comments`):

```
PR Babysitter started for <REPO> PR #<PR_NUMBER> (branch: <BRANCH_NAME>)
- Review comments: disabled
- Build status: watching
```

If freeform instructions were provided, append:

```
Instructions: "<INSTRUCTIONS>"
```

Then stop.
