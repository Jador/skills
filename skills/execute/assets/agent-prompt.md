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

### 3. Run Verification

Run the verification step exactly as described. This is mandatory — your task is NOT complete until verification passes.

**If verification passes:** Proceed to step 4.

**If verification fails:**
1. Read the error output carefully.
2. Diagnose the root cause.
3. Fix the issue.
4. Re-run verification.
5. Repeat up to 3 total attempts. If verification still fails after 3 attempts, report failure (see step 5).

### 4. Commit Changes (git repos only)

> **Skip this step if you are not inside a git repository.** Proceed directly to step 5.

After verification passes, commit all your changes with this exact message format:

```
Execute plan: Task {{TASK_NUMBER}} - {{TASK_TITLE}}
```

Use `git add` for any new files, then `git commit`. Do NOT push.

### 5. Report Results

End your work by providing a structured report:

**Status:** `complete` or `failed`
**Summary:** 1-3 sentences describing what you did.
**Verification output:** The output from your successful verification run (or the last failed attempt if reporting failure).
**Issues:** Any problems encountered, workarounds applied, or concerns for downstream tasks. Write `none` if everything went smoothly.
