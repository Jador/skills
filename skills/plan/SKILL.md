---
name: plan
description: Break down an idea into a detailed execution plan with small, self-contained tasks. Reads idea documents from ~/ideas/ and writes plans to ~/plans/. Use when the user wants to plan, break down, or create tasks for an idea — even if they just say "plan <slug>" without elaborating. Also lists existing plans when called with "list".
argument-hint: "[<idea-slug> | list]"
disable-model-invocation: true
---

# Plan Skill

You are an execution planner. Your job is to take an idea document and break it into a detailed plan of small, self-contained tasks that Claude can execute autonomously.

## Process

### 0. Route Based on Arguments

Check `$ARGUMENTS` and take exactly one of these three paths:

**Path A — Slug provided** (anything that is not empty and not `list`):
Jump directly to Step 1 with the slug from `$ARGUMENTS`.

**Path B — `list`** (argument is exactly `list`):
1. Glob for `*.md` files in `~/plans/`.
2. For each plan file, read the frontmatter to extract the `status`, the `# Plan:` title, the plan's project (see the ranking spec's **Matching a doc to its project** — for plans, use the `project:` field; legacy plans without it resolve via the `idea:` trace or fall to "other"), and its recency date (`created:`).
3. Order the plans using the **Shared Ranking Spec** below (current-repo docs first, then recency-desc, filename tiebreak).
4. Display a table with columns: **★** (current-repo marker), **Slug**, **Title**, **Status** — rows in the ranked order. This is a full listing, not a pick UI, so pagination does not apply; show every plan.
5. Stop here — do not continue to the planning steps below.

**Path C — No arguments** (`$ARGUMENTS` is empty):
1. Glob for all `*.md` files in `~/ideas/`.
2. For each idea, extract the slug from the filename (strip `YYYY-MM-DD-` prefix and `.md` suffix).
3. Check whether `~/plans/<slug>.md` exists — if it does, skip this idea (already planned).
4. If no unplanned ideas remain, tell the user everything is planned and stop.
5. Order the remaining unplanned ideas using the **Shared Ranking Spec** below. For each idea, its project comes from `**Project:**` in the idea's Context section and its recency from the `YYYY-MM-DD-` filename prefix.
6. Present the ranked ideas to the user with AskUserQuestion and let them pick one, following the spec's **Presentation** (mark current-repo ideas with ★) and **Overflow** (top 3 ideas + a "Show more…" option that re-prompts with the next page) rules.
7. Continue to Step 1 with the chosen slug.

### 1. Load the Idea

Read the idea slug (from Path A or Path C above). Find the matching file in `~/ideas/` — match against filenames that end with `-<slug>.md` (the date prefix varies). If multiple files match, show them and ask the user to pick one. If no file matches, list available idea files and ask the user to choose.

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
- **Verified**: Every task that modifies code must include a verification step — a command to run, a test to pass, or a condition to check. Include steps to run tests, lint, and typecheck (for typed languages). There is no "where applicable" — if the task touches code, it gets a verification step.

Use the template at [assets/plan-template.md](assets/plan-template.md) for the output format.

### 4. Present for Review

Present the plan as a fenced markdown block. Tell the user:

- They can approve the plan as-is.
- They can request changes to specific tasks (add, remove, split, merge, reorder, re-detail).
- They can ask you to add more detail to any task.

Iterate conversationally until the user approves.

### 5. Offer a Design Critique

Once the user is happy with the plan but before writing it, offer to stress-test it. Use AskUserQuestion:
- **Run critique**: invoke `/jador:critique plan <slug>` via the Skill tool. A dedicated adversary reviews the plan for soundness — it names the load-bearing assumptions, proposes at least one concrete alternative, and states what would make this the wrong approach. Findings come back conversationally; fold any the user accepts into the plan, then continue.
- **Skip**: write the plan as-is.

### 6. Write the Plan

Once approved (and after folding in any critique revisions):

1. Determine the `project:` value for the frontmatter: run `basename "$(git rev-parse --show-toplevel 2>/dev/null)"`, falling back to the current working directory's basename when not in a git repo (see the **Shared Ranking Spec** below for the exact identity rule). Write this into the plan frontmatter's `project:` field.
2. Write the file to `~/plans/<slug>.md` using the slug from the idea filename.
3. Confirm the file path to the user.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** This includes confirmations (yes/no), selecting from a list, and choosing between approaches.

## Guidelines

- Prefer more tasks that are smaller over fewer tasks that are larger. A task that touches 1-3 files is ideal.
- Group related setup tasks (e.g., "create migration" and "create model") only if they are truly inseparable. Otherwise, keep them separate for clearer progress tracking.
- Tests should be their own tasks, not bundled into implementation tasks, unless the test is trivial (e.g., a single assertion).
- If the idea has open questions noted, flag them in the **Notes** section and make reasonable assumptions to keep the plan actionable.
- The plan must be complete — executing all tasks in order should fully realize the idea.

<!-- SHARED-RANKING-SPEC:BEGIN — canonical copy; reused verbatim by other doc-listing skills (execute, backlog, notepad). Keep edits in sync. -->
## Shared Ranking Spec — current-repo-first ordering

When a skill presents a list of shared-directory docs (ideas, plans, notes) for the user to pick from or browse, order that list so docs belonging to the **current repo** come first, then by recency. Nothing is hidden — every doc stays reachable.

**Current-repo identity.** The current repo is `basename "$(git rev-parse --show-toplevel 2>/dev/null)"`. If that command produces nothing (not inside a git repo), fall back to the basename of the current working directory. This is the same convention notepad uses to set a doc's `**Project:**`.

**Matching a doc to its project.** Compare the current-repo identity (case-sensitive, exact string match) against the doc's stored project:
- **Ideas** — the `**Project:**` value in the idea's Context section.
- **Notes** — the `**Project:**` field on the individual note entry.
- **Plans** — the `project:` frontmatter field. For **legacy plans that lack `project:`**, resolve it by tracing the `idea:` frontmatter path to the source idea file and reading that idea's `**Project:**`. If the project still cannot be determined (missing field, unresolvable `idea:` path, or idea with no project), the doc falls into the **"other"** bucket.

A doc whose project cannot be determined is treated as "other" (not current-repo).

**Recency.** Order by each doc's **embedded date**, descending — NOT filesystem mtime (edits would reorder; the shared notes file has no per-note mtime). The embedded date is:
- **Ideas** — the `YYYY-MM-DD-` filename prefix.
- **Plans** — the `created:` frontmatter field.
- **Notes** — the note's `**Added:**` field.

Ties (equal dates) are broken by filename, ascending.

**Sort order (apply in full).**
1. Current-repo docs first, then "other" docs.
2. Within each bucket, most-recent embedded date first (descending).
3. Within equal dates, filename ascending.

**Presentation.** A single flat list in the sorted order above — no section headers. Mark each current-repo doc with a leading **★** glyph so its origin is legible; other-repo docs get no marker. Each row still shows enough (title + date) that its origin is clear.

**Overflow (pick UI only).** AskUserQuestion caps at 4 options. When the list is used as a *pick UI* and has more than 4 docs, show the **top 3 ranked docs** as options plus a 4th **"Show more…"** option that re-prompts (AskUserQuestion again) with the next page of 3 + "Show more…", continuing until the docs are exhausted (the final page needs no "Show more…" if it fits). This surfaces current-repo/recent docs first while keeping every doc reachable across pages. A plain **full listing** (e.g. a table, not a pick UI) is not subject to this cap — render all rows in ranked order.
<!-- SHARED-RANKING-SPEC:END -->
