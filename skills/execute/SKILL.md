---
name: execute
description: Execute a plan produced by /jador:plan. Reads plan files from ~/plans/, parses the task dependency graph, and orchestrates execution through waves of parallel sub-agents. Each agent implements a task, runs verification, self-heals on failure, and commits atomically. Use when the user wants to execute, run, or carry out a plan.
argument-hint: "<plan-slug> [--step]"
disable-model-invocation: true
---

# Execute Skill

You are a plan executor. Your job is to take a plan file (produced by `/jador:plan`) and orchestrate its execution using sub-agents, running independent tasks in parallel where possible.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** This includes confirmations (yes/no), selecting from a list, and choosing between approaches.
- **Never execute task work in the parent agent.** When a task needs to be retried (sub-agent failure, retasking, connectivity loss), always spawn a new sub-agent. Do not attempt the task inline. This preserves the parallelism and worktree isolation that the execute skill is designed around.

## Process

### 1. Load the Plan

Read `$ARGUMENTS` and parse it:
- Extract the plan slug (everything before `--step` if present).
- Detect the `--step` flag (enables pause-between-waves mode).

Find the matching plan file in `~/plans/<slug>.md`. If the slug is empty or no file matches, list available plan files in that directory and use AskUserQuestion to ask the user to pick one.

Read the full plan file. Also read the idea file referenced in the plan's frontmatter `idea:` field for additional context.

### 2. Create Plan Worktree (git repos only)

> **Skip this step entirely if the current working directory is not inside a git repository.** When not in a git repo, all sub-agents run directly in the current working directory with no worktree isolation.

Use the `EnterWorktree` tool to create an isolated worktree for the entire plan execution. Pass the plan slug as the `name` parameter (e.g., `name: "remove-use-turn-manager"`). This creates the worktree at `.claude/worktrees/<slug>/` with its own branch and switches the session into it automatically.

All sub-agents and file operations for the rest of this execution run inside this worktree. The auto-detect logic for sub-agent isolation (step 5a) still applies — if parallel tasks within a wave have overlapping files, those sub-agents get their own nested worktrees via `isolation: "worktree"` on the Agent tool.

### 3. Parse Tasks

Extract all tasks from the plan. For each task, parse:
- **Number** and **Title** (from the `### Task N: <title>` heading)
- **Status** (`pending`, `in_progress`, `complete`, or `failed`)
- **Description**
- **Files** (list of files to create or modify)
- **Blocked by** (list of task numbers, or `none`)
- **Parallel group** (letter label)
- **Verification** (command, test, or condition)

Also extract the plan's **Assumptions** and **Notes** sections for context to pass to sub-agents.

### 4. Build Waves

Group tasks into execution waves:

1. Find all tasks with Status `pending` whose `blocked_by` dependencies are all `complete` (or `none`).
2. Group these ready tasks by their `parallel_group` label — each group forms a wave.
3. If multiple parallel groups are ready simultaneously, execute them as a single combined wave (they are independent by definition).

Tasks with Status `complete` are skipped (this enables resume support — a partially-executed plan can be re-run and it picks up where it left off).

### 5. Execute Waves

For each wave, repeat the following loop until all tasks are complete or execution is halted:

#### a. Detect File Overlap

Collect the `Files` lists from all tasks in the current wave. Check for any intersection — if two or more tasks list the same file, those tasks have overlap.

- **No overlap**: All agents run in the shared workspace (no isolation).
- **Overlap detected (git repo only)**: Agents whose tasks have overlapping files run with `isolation: "worktree"`. Agents with no overlap run in the shared workspace.
- **Overlap detected (no git repo)**: Worktree isolation is unavailable. Run overlapping tasks **sequentially** instead of in parallel to avoid conflicts. Non-overlapping tasks can still run in parallel.

#### b. Launch Sub-Agents in Parallel

For each task in the wave, launch a sub-agent using the Agent tool. **Launch all agents in the wave in a single response** so they run in parallel.

Construct each agent's prompt by filling in the template from [assets/agent-prompt.md](assets/agent-prompt.md) with:
- The task's number, title, description, files, and verification step
- The plan's Assumptions and Notes sections
- The idea document's Summary section
- Whether the agent should use worktree isolation

If a task requires worktree isolation, set `isolation: "worktree"` on the Agent tool call.

#### c. Collect Results

After all agents in the wave complete, collect their results. Each agent reports:
- **Status**: `complete` or `failed`
- **Summary**: What was done
- **Verification output**: The result of running the verification step
- **Issues**: Any problems encountered

#### d. Merge Worktree Branches (git repos only)

> **Skip this step entirely if not in a git repository.** Worktree isolation is not used outside git repos, so there are no branches to merge.

If any agents ran in worktrees and produced changes (indicated by the agent result containing a worktree path and branch name):

1. For each worktree branch, merge it into the current working branch.
2. Use `git merge <branch-name>` for each branch sequentially.
3. If a merge conflict occurs, attempt to resolve it. If auto-resolution fails, ask the user for help using AskUserQuestion.
4. After successful merge, the worktree is cleaned up automatically.

See [assets/merge-prompt.md](assets/merge-prompt.md) for detailed merge instructions.

#### e. Update the Plan File

For each completed task:
1. In the **Checklist** section, change `- [ ] Task N: ...` to `- [x] Task N: ...`
2. In the **Tasks** section, change `- **Status**: pending` to `- **Status**: complete`

For each failed task:
1. In the **Tasks** section, change `- **Status**: pending` to `- **Status**: failed`
2. Do NOT check off the checklist item.

Use the Edit tool to update the plan file in-place.

#### f. Print Wave Summary

Print a brief summary to the conversation:

```
## Wave [group label(s)] Complete

**Completed:**
- Task N: <title> ✓
- Task M: <title> ✓

**Failed:**
- Task K: <title> ✗ — <reason>

**Next up:** Tasks X, Y, Z (group <label>)
```

#### g. Handle Failures

If any task in the wave failed (agent reported `failed` after exhausting retries):

1. Identify all tasks that are blocked by the failed task (direct and transitive dependencies).
2. Use AskUserQuestion to ask the user how to proceed:
   - **Fix manually**: Pause execution. The user fixes the issue, then re-runs `/jador:execute <slug>` to resume (resume support picks up from the failed task).
   - **Skip**: Mark the task as `skipped`, and also skip all tasks transitively blocked by it. Continue with remaining independent tasks.
   - **Abort**: Stop execution entirely. The plan file reflects current progress.

#### h. Step Mode Gate

If the `--step` flag is active and there are more waves remaining:

Use AskUserQuestion to ask the user:
- **Continue**: Proceed to the next wave.
- **Abort**: Stop execution. The plan file reflects current progress.

### 6. Completion

When all tasks are complete (or skipped/failed with no remaining executable tasks):

1. Update the plan frontmatter: change `status: pending` to `status: complete` (or `status: partial` if any tasks were skipped/failed).
2. Print a final summary:

```
## Plan Complete

**Results:** N/M tasks completed successfully
**Status:** complete | partial

**Completed tasks:**
- Task 1: <title> ✓
- Task 2: <title> ✓
...

**Skipped/Failed tasks:** (if any)
- Task K: <title> — <reason>
```

### 7. Offer to Open a PR (git repos only)

> **Skip this step entirely if not in a git repository.** There is no worktree to exit and no branch to PR from.

After printing the final summary, ask the user if they want to open a pull request for the work:

Use AskUserQuestion with options:
- **Open PR**: Open a pull request from the plan worktree branch.
- **No thanks**: Keep the worktree and branch for the user to handle manually.

If the user chooses to open a PR:
1. Create the PR: `gh pr create --title "<Plan Title>" --fill`
2. Print the PR URL to the conversation.
3. Use `ExitWorktree` with `action: "keep"` to return to the original working directory while preserving the branch.

If the user declines:
1. Use `ExitWorktree` with `action: "keep"` to return to the original working directory while preserving the worktree and branch.
