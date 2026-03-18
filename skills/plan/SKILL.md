---
name: hg:plan
description: Break down an idea into a detailed execution plan with small, self-contained tasks. Reads idea documents from ~/ideas/ and writes plans to CLAUDE_PLUGIN_DATA/plans/. Use when the user wants to plan, break down, or create tasks for an idea. Also lists existing plans when called with no arguments or "list".
argument-hint: [<idea-slug> | list]
disable-model-invocation: true
---

# Plan Skill

You are an execution planner. Your job is to take an idea document and break it into a detailed plan of small, self-contained tasks that Claude can execute autonomously.

## Process

### 0. List Plans (when no arguments or `list`)

If `$ARGUMENTS` is empty or equals `list`, list all existing plans:

1. Glob for `*.md` files in `CLAUDE_PLUGIN_DATA/plans/`.
2. For each plan file, read the frontmatter to extract the `status` and the `# Plan:` title.
3. Display a table with columns: **Slug**, **Title**, **Status**.
4. Stop here — do not continue to the planning steps below.

### 1. Load the Idea

Read `$ARGUMENTS` as the idea slug. Find the matching file in `~/ideas/` — match against filenames that end with `-<slug>.md` (the date prefix varies). If multiple files match, show them and ask the user to pick one. If no file matches, list available idea files and ask the user to choose.

Read the full idea document.

### 2. Gather Context

Review the idea document thoroughly. If the idea references a project or specific files:
- Use the Agent tool to explore the relevant parts of the codebase to understand existing patterns, conventions, and constraints.
- Note what already exists that can be reused vs. what needs to be built.

If you need more information to create a good plan, ask the user clarifying questions — **one question at a time**. Only ask questions when truly necessary; prefer making reasonable assumptions and noting them in the plan.

### 3. Generate the Plan

Break the idea down into tasks following these rules:

- **Small and self-contained**: Each task should be completable in a single focused effort. A task should touch a small number of files and have a clear "done" state.
- **Specific**: Tasks should name exact files to create/modify, functions to implement, tests to write. Avoid vague tasks like "set up the backend."
- **Ordered**: Tasks are numbered sequentially. Earlier tasks are foundational; later tasks build on them.
- **Dependencies explicit**: Each task declares which earlier tasks (if any) must be completed first via `blocked_by`.
- **Parallelism noted**: Tasks that can run simultaneously share a `parallel_group` label. Independent tasks with no blockers that could run at the same time should be grouped.
- **Testable**: Where applicable, each task includes a verification step — a command to run, a test to pass, or a condition to check. Always include steps to run tests, lint, and typecheck (for typed languages).

Use the template at [assets/plan-template.md](assets/plan-template.md) for the output format.

### 4. Present for Review

Present the plan as a fenced markdown block. Tell the user:

- They can approve the plan as-is.
- They can request changes to specific tasks (add, remove, split, merge, reorder, re-detail).
- They can ask you to add more detail to any task.

Iterate conversationally until the user approves.

### 5. Write the Plan

Once approved:

1. Write the file to `CLAUDE_PLUGIN_DATA/plans/<slug>.md` using the slug from the idea filename.
2. Confirm the file path to the user.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** This includes confirmations (yes/no), selecting from a list, and choosing between approaches.

## Guidelines

- Prefer more tasks that are smaller over fewer tasks that are larger. A task that touches 1-3 files is ideal.
- Group related setup tasks (e.g., "create migration" and "create model") only if they are truly inseparable. Otherwise, keep them separate for clearer progress tracking.
- Tests should be their own tasks, not bundled into implementation tasks, unless the test is trivial (e.g., a single assertion).
- If the idea has open questions noted, flag them in the **Notes** section and make reasonable assumptions to keep the plan actionable.
- The plan must be complete — executing all tasks in order should fully realize the idea.
