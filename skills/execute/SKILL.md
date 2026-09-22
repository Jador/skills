---
name: execute
description: Execute a plan produced by /jador:plan. Reads plan files from ~/plans/, parses the task dependency graph, and orchestrates execution through waves of parallel sub-agents. Each agent implements a task, runs verification, self-heals on failure, and commits atomically. Each task worker is paired with a live comment-auditor sub-agent that gates its commit on a comment audit. Use when the user wants to execute, run, or carry out a plan.
argument-hint: "<plan-slug> [--step]"
disable-model-invocation: true
---

# Execute Skill

You are a plan executor. Your job is to take a plan file (produced by `/jador:plan`) and orchestrate its execution using sub-agents, running independent tasks in parallel where possible.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** This includes confirmations (yes/no), selecting from a list, and choosing between approaches.
- **Never execute task work in the parent agent.** When a task needs to be retried (sub-agent failure, retasking, connectivity loss), always spawn a new sub-agent. Do not attempt the task inline. This preserves the parallelism and worktree isolation that the execute skill is designed around. Spawn that **retry** with `model: opus`, rather than the `model: sonnet` used for first attempts (see step 5b).
- **Every retry gets a fresh auditor.** A retry spawns a fresh worker named `worker-task-<N>-retry<k>` **and** a fresh auditor named `auditor-task-<N>-retry<k>` (see [assets/pairing-protocol.md](assets/pairing-protocol.md)), sharing the same `<k>`; the retry auditor's `{{WORKER_NAME}}` is the retry worker's name. The prior auditor for that task is stood down. The audit gates every real commit attempt, so each retry that reaches a commit gets its own audit — never reuse or resume a prior auditor across a retry.

## Process

### 1. Load the Plan

Read `$ARGUMENTS` and parse it:
- Extract the plan slug (everything before `--step` if present).
- Detect the `--step` flag (enables pause-between-waves mode).

Find the matching plan file in `~/plans/<slug>.md`. If the slug is empty or no file matches, list the available plan files in that directory and use AskUserQuestion to ask the user to pick one. Order that pick list using the shared ranking spec at [`../plan/assets/ranking-spec.md`](../plan/assets/ranking-spec.md): current-repo plans first, then recency-desc (`created:` frontmatter), filename ascending on ties.

- **Project match.** For each plan, take its project from the `project:` frontmatter field. For **legacy plans that lack `project:`**, resolve it per the **Matching a doc to its project** rule in the ranking spec (unresolvable → "other").
- **Presentation.** Mark each current-repo plan with a leading **★** glyph; other-repo plans get no marker. Each option still shows enough (slug/title + date) that its origin is clear.
- **Overflow.** This is a pick UI, so the AskUserQuestion 4-option cap applies: when more than 4 plans are available, show the **top 3 ranked plans** plus a 4th **"Show more…"** option that re-prompts with the next page of 3 + "Show more…", continuing until the plans are exhausted.

Read the full plan file. Also read the idea file referenced in the plan's frontmatter `idea:` field for additional context.

### 2. Create Plan Worktree (git repos only)

> **Skip this step entirely if the current working directory is not inside a git repository.** When not in a git repo, all sub-agents run directly in the current working directory with no worktree isolation.

Use the `EnterWorktree` tool to create an isolated worktree for the entire plan execution. Pass the plan slug as the `name` parameter (e.g., `name: "remove-use-turn-manager"`). This creates the worktree at `.claude/worktrees/<slug>/` with its own branch and switches the session into it automatically.

**Preflight: verify the worktree switch actually took before spawning any sub-agents.** Sub-agents inherit the session's cwd and commit wherever it points — if `EnterWorktree` did not move the session (step skipped, no-op'd, or refused because already in a worktree session), sub-agents would commit onto whatever branch you started on. The authoritative check is whether the branch *changed*, which needs no remote at all:

1. **Before** calling `EnterWorktree`, record the starting branch: `before=$(git rev-parse --abbrev-ref HEAD)`. **After** it returns, record `after=$(git rev-parse --abbrev-ref HEAD)`.
2. If `after` != `before`, the switch took — **proceed**. This is the primary signal: it needs no `origin`, no `origin/HEAD`, and no default-branch resolution, and it survives nested per-task worktrees (each lands on its own branch).
3. If `after` == `before`, the switch may have failed — *or* you legitimately re-entered an already-active worktree (idempotent resume-in-place). Disambiguate: resolve the default branch (`git rev-parse --abbrev-ref origin/HEAD`, strip the leading `origin/`) and compare. If `after` equals the default branch, the switch did **not** take — **stop and re-establish the worktree** (re-run `EnterWorktree` / resolve the failure) before spawning any sub-agents. If `after` is some other (non-default) branch, you are already on a worktree branch (resume-in-place) — proceed.
4. If the default branch cannot be resolved (no `origin` remote, unset `origin/HEAD`, or a fork workflow whose canonical remote isn't named `origin`), do **not** halt — **warn** that the switch could not be positively confirmed and proceed. The before/after comparison in step 2 is the real guard; an unresolvable default only weakens the step-3 resume-in-place disambiguation, and halting a core workflow over a benign, common git configuration would be worse than the rare risk it guards against. Compare branch names, not paths.

This preflight is git-only, matching the "skip if not in a git repo" guard above — non-git runs have no worktree and are unaffected.

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

This detection, and any resulting `isolation: "worktree"`, applies to **workers only**. Auditors never receive isolation of any kind — giving an auditor its own worktree would land it in a different, unrelated tree than the worker it is meant to audit. An auditor needs no path at spawn time either: it learns nothing about scope until the worker sends it absolute paths at the pre-commit checkpoint (see [assets/pairing-protocol.md](assets/pairing-protocol.md)).

- **No overlap**: All worker agents run in the shared workspace (no isolation).
- **Overlap detected (git repo only)**: Worker agents whose tasks have overlapping files run with `isolation: "worktree"`. Workers with no overlap run in the shared workspace.
- **Overlap detected (no git repo)**: Worktree isolation is unavailable. Run overlapping tasks **sequentially** instead of in parallel to avoid conflicts. Non-overlapping tasks can still run in parallel.

#### b. Launch Sub-Agents in Parallel

For each task in the wave, launch **two** agents: the worker and its paired auditor. **Launch every agent for the wave — every worker and every auditor — in a single response** so worker and auditor are concurrent from the start of the wave.

1. **The worker**, named `worker-task-<N>` for task `<N>` via the Agent tool's `name` parameter. Construct its prompt by filling in the template from [assets/agent-prompt.md](assets/agent-prompt.md) with:
   - The task's number, title, description, files, and verification step
   - The plan's Assumptions and Notes sections
   - The idea document's Summary section
   - Whether the agent should use worktree isolation
   - `{{AUDITOR_NAME}}`, computed as `auditor-task-<N>` for task `<N>`

   If a task requires worktree isolation, set `isolation: "worktree"` on the worker's Agent tool call.

   **Model selection.** Pin each first-attempt worker spawn to `model: sonnet`; spawn the **retry** of a failed/under-specified task (see the General Rules retry rule and step g) with `model: opus`. (First-attempt effort is carried as a soft constraint in the worker template, [assets/agent-prompt.md](assets/agent-prompt.md).)

2. **Its auditor**, named `auditor-task-<N>` (matching the name filled into the worker's prompt). Spawn it with `subagent_type: jador:comment-auditor`, `model: sonnet`, no isolation. Construct its prompt by filling in the template from [assets/auditor-prompt.md](assets/auditor-prompt.md) with the task's number, title, files, its own `{{AUDITOR_NAME}}`, and `{{WORKER_NAME}}`, computed as `worker-task-<N>` (the name filled into the worker's prompt above).

Auditors are support agents, not task agents — this holds throughout the rest of execution:
- Their returns are not task results (step 5c does not collect from them).
- They hold no worktree and contribute no branch (step 5d never merges or expects one from an auditor).
- Between messages, an auditor is idle with its turn ended — that is expected, not unfinished work. Once its worker finishes, stand the auditor down per [assets/pairing-protocol.md](assets/pairing-protocol.md) rather than treating it as unfinished work.

See [assets/pairing-protocol.md](assets/pairing-protocol.md) for the full spawn/addressing/checkpoint protocol this implements.

#### c. Collect Results

After all agents in the wave complete, collect their results. Each agent reports:
- **Status**: `complete` or `failed`
- **Summary**: What was done
- **Verification output**: The result of running the verification step
- **Issues**: Any problems encountered

A worker's `Issues` field now carries a `Comment audit:` line and, when the checkpoint surfaced anything unresolved, an `### Audit open items (N)` block lifted verbatim from `prune-comments` (see [assets/agent-prompt.md](assets/agent-prompt.md) step 6). Capture both **verbatim** — do not summarize, paraphrase, or drop them — they are what step 5f and step 6 draw on to roll ambiguous auto-kept items and unfixed `MUST KILL` symbols up to the wave and plan summaries.

**A waiting worker is not a result.** A worker's result is only the turn that ends with the step-6 structured report (`**Status:**` …). Identify a waiting worker by a turn that contains the line `Awaiting audit from auditor-task-<N>.` (its paired auditor's literal name, so `auditor-task-<N>-retry<k>` for a retry) and has no `**Status:**` block — match on the line's presence together with the missing report, not on the line being the literal last thing the turn printed, since a worker's turn can echo trailing text after it. Such a worker has sent its checkpoint and is waiting for its audit, not finished. Do not collect that turn, do not re-task the worker, and do not treat it as failed. It resumes when the auditor's report arrives.

**Stand down auditors.** Once this wave's worker results are collected, send a one-line stand-down message to every paired auditor in the wave per [assets/pairing-protocol.md](assets/pairing-protocol.md). The auditor is idle between turns; the stand-down message resumes it, and it ends its turn with no output. Do this for the whole wave now, rather than leaving a lingering auditor to be caught later.

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

**Comment audit open items:**
- Task N: <item>
- Task M: <item>

**Next up:** Tasks X, Y, Z (group <label>)
```

Build the **Comment audit open items** section from the `### Audit open items (N)` blocks captured in step 5c for every task in this wave: for each item line in a task's block, emit one `Task N: <item>` line, dropping the block's own leading `- ` and prefixing with that task's number instead. Print `_none_` in place of the list when every task in the wave reported an open-items count of 0 (or reported `Comment audit: unavailable`, which carries no items to roll up either way).

#### g. Handle Failures

If any task in the wave failed (agent reported `failed` after exhausting retries):

1. Identify all tasks that are blocked by the failed task (direct and transitive dependencies).
2. Use AskUserQuestion to ask the user how to proceed:
   - **Fix manually**: Pause execution. The user fixes the issue, then re-runs `/jador:execute <slug>` to resume (resume support picks up from the failed task).
   - **Skip**: Mark the task as `skipped`, and also skip all tasks transitively blocked by it. Continue with remaining independent tasks.
   - **Abort**: Stop execution entirely. The plan file reflects current progress.

If the chosen path re-executes the task (a retry), spawn a fresh worker named `worker-task-<N>-retry<k>` **and** a fresh auditor named `auditor-task-<N>-retry<k>` together, sharing the same `<k>`, per [assets/pairing-protocol.md](assets/pairing-protocol.md). Stand down the prior auditor for that task first — never reuse or resume it. This follows the General Rules retry rule and applies to every retry.

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

**Comment audit open items:**
- Task 1: <item>
- Task 4: <item>
```

Build the **Comment audit open items** section by aggregating the per-wave lists from every step 5f summary printed over the whole run — not just the final wave — so an item raised in an early wave (e.g. wave A) is still visible here after later waves (e.g. wave F) have completed. Keep the same `Task N: <item>` line shape. Print `_none_` in place of the list when no wave in the run reported any open items.

3. **Synthesize the handoff (git repos only).** Invoke the handoff skill so whoever picks up the PR inherits full context — what shipped, deviations from the plan, decisions made, gotchas, and open threads. Use the Skill tool to run `/jador:handoff synthesize`; it writes the branch-keyed `.claude/handoffs/<branch>.md` in the worktree (uncommitted). Draw the decisions/deviations/gotchas from the sub-agent return summaries and the narrative of this run, not just from the diff — that unstated rationale is the part the next agent can't reconstruct on its own. Pass the aggregated **Comment audit open items** from step 2 in as open threads: an auto-kept ambiguous comment or an unfixed `MUST KILL` symbol is exactly the kind of unstated rationale the next agent can't reconstruct from the diff alone, so it belongs in the handoff, not just in this run's scrollback.

### 7. Offer a Design Critique (git repos only)

> **Skip this step entirely if not in a git repository.**

Before offering a PR, offer an adversarial design review of the changeset — cheap insurance against shipping a design that merely works. Use AskUserQuestion:
- **Run critique**: invoke `/jador:critique changeset` via the Skill tool. It reviews the diff at architecture altitude (not nits/bugs) and writes the branch-keyed `.claude/critiques/<branch>.md`. It is advisory — it never blocks or auto-fixes. Surface the findings so the user can decide what to address before the PR.
- **Skip**: proceed without a critique.

### 8. Offer to Open a PR (git repos only)

> **Skip this step entirely if not in a git repository.** There is no branch to PR from.

After the summary (and any handoff/critique), ask the user if they want to open a pull request:

Use AskUserQuestion with options:
- **Open PR**: Open a pull request from the plan worktree branch.
- **No thanks**: Leave the branch for the user to handle.

If the user chooses to open a PR:
1. Create the PR: `gh pr create --title "<Plan Title>" --fill`
2. Print the PR URL to the conversation.

**Stay in the worktree by default — do not call `ExitWorktree` automatically.** The worktree, branch, and the uncommitted branch-keyed `.claude/handoffs/<branch>.md` must persist so the user (or `/jador:babysit`) can keep working from here; exiting would discard the handoff. Only if the user explicitly asks to leave, use `ExitWorktree` with `action: "keep"` (preserves the branch). When you finish, remind the user they're in the plan worktree and can run `/jador:babysit` here once the PR is open.
