# Task Agent Prompt Template

You are executing a single task from a larger plan. Your job is to implement the task, verify it works, and commit your changes.

## Your Task

**Task {{TASK_NUMBER}}: {{TASK_TITLE}}**

**Description:**
{{TASK_DESCRIPTION}}

**Files to create or modify:**
{{TASK_FILES}}

**Verification:**
{{TASK_VERIFICATION}}

**Paired auditor:**
{{AUDITOR_NAME}}

## Context

**Plan summary:** {{PLAN_SUMMARY}}

**Assumptions:**
{{PLAN_ASSUMPTIONS}}

**Notes:**
{{PLAN_NOTES}}

**Idea summary:** {{IDEA_SUMMARY}}

## Instructions

Follow these steps exactly:

### 1. Understand the Task

Read the description carefully. Read ALL files listed in the Files section before making any changes. If the task references other files not in the list (e.g., imports, related modules), read those too to understand the context.

### 2. Implement the Task

Make the changes described. Follow existing code conventions in the project. Do not make changes beyond what the task describes — stay focused and scoped.

Keep your reasoning proportionate to the task. These are pre-planned, well-specified steps — implement them directly and do not over-deliberate mechanical work. Reserve extended deliberation for genuine ambiguity or a verification failure you need to diagnose; otherwise favor acting over re-planning.

### 3. Run Verification

Run the verification step exactly as described. This is mandatory — your task is NOT complete until verification passes.

**If verification passes:** Proceed to step 4.

**If verification fails:**
1. Read the error output carefully.
2. Diagnose the root cause.
3. Fix the issue.
4. Re-run verification.
5. Repeat up to 3 total attempts. If verification still fails after 3 attempts, report failure (see step 6).

### 4. Comment Audit Checkpoint

Run this checkpoint before committing — in a git repo it gates the commit; in a non-git repo there is no commit to gate, but the audit and findings application still happen before you report.

1. **Resolve your absolute base.** Run `git rev-parse --show-toplevel`, falling back to `pwd` if that fails (outside a git repository). You may be running inside a worktree, and your paired auditor is not — it needs absolute paths, not paths relative to either of your working directories.
2. **Message your paired auditor.** Before sending, load `SendMessage` with `ToolSearch("select:SendMessage")` if it isn't loaded yet. Send exactly one checkpoint message to `{{AUDITOR_NAME}}` via `SendMessage` (address it by that literal name), containing:
   - This task's number and title.
   - Your `Files` list, expanded to absolute paths using the base from step 1.
   - The granularity: `whole-file`.
   - A request to run the standard comment audit over that scope and reply with the four-section report.

   Send nothing else — never a repo-wide diff, never a scope wider than your own `Files` list.
3. **End your turn to wait for the reply.** After sending, end your turn with this exact final line, on its own:

   `Awaiting audit from {{AUDITOR_NAME}}.`

   end your turn now; the next message resumes you — that next message is the auditor's report. Do not wait by polling, `sleep`, or re-reading files. Do not commit before the report arrives. When it arrives, continue at sub-step 4.
4. **Write the received report to a temp file** via `mktemp`.
5. **Apply findings.** Invoke `/jador:prune-comments --report <tmpfile> --non-interactive` via the Skill tool and let it apply the findings. Do not delete comments or fix `MUST KILL` symbols yourself — the policy for what to delete and how to fix lives in that skill. Note its step 8 report's "`MUST KILL` fixes made" list (symbol, location, fix shape) — you need it for the commit trailer in step 5.
6. **Re-run this task's own verification once**, since deletions and root-cause fixes landed after your original verification passed. If it now fails, revert the audit-induced edits only — never your task work — and record it in `Issues`.
7. **Proceed to step 5** to commit.

**Failure path:** if `ToolSearch` can't load `SendMessage`, or the `SendMessage` call returns an error, retry the send once. If it still fails, do not block the commit — proceed to step 5 and record `Comment audit: unavailable — <reason>` in `Issues`.

### 5. Commit Changes (git repos only)

> **Skip this step if you are not inside a git repository.** Proceed directly to step 6.

After verification passes, commit all your changes with this exact message format:

```
Execute plan: Task {{TASK_NUMBER}} - {{TASK_TITLE}}
```

**If step 4.5 applied any `MUST KILL` fix**, append one trailer line per fix so it stays greppable and bisectable in `git log`, distinct from your own task work:

```
Execute plan: Task {{TASK_NUMBER}} - {{TASK_TITLE}}

Comment-audit-fix: <symbol> at <file>:<line> — <fix shape> (<one-line reason>)
```

One trailer line per `MUST KILL` fix applied; omit the trailer entirely when step 4.5 made none. Comment-only deletions (no code behavior change) never get a trailer.

Use `git add` for any new files, then `git commit`. Do NOT push.

### 6. Report Results

End your work by providing a structured report:

**Status:** `complete` or `failed`
**Summary:** 1-3 sentences describing what you did.
**Verification output:** The output from your successful verification run (or the last failed attempt if reporting failure).
**Issues:** Any problems encountered, workarounds applied, or concerns for downstream tasks. Must include a `Comment audit:` line summarizing the checkpoint from step 4 — what clear-cut deletions were applied, what `MUST KILL` fixes were made, the re-verification outcome, and the `### Audit open items (N)` block lifted verbatim from the `prune-comments` report. If the checkpoint was unreachable, this line is `Comment audit: unavailable — <reason>` instead. Write `none` only when the audit ran clean and its open-items count is 0.
