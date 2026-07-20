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
2. For each plan file, read the frontmatter to extract the `status`, the `# Plan:` title, the plan's project (resolve it per the **Matching a doc to its project** rule in [assets/ranking-spec.md](assets/ranking-spec.md) — for plans, use the `project:` field; legacy plans without it resolve per that rule, else fall to "other"), and its recency date (`created:`).
3. Order the plans using the ranking spec in [assets/ranking-spec.md](assets/ranking-spec.md) (current-repo docs first, then recency-desc, filename tiebreak).
4. Display a table with columns: **★** (current-repo marker), **Slug**, **Title**, **Status** — rows in the ranked order. This is a full listing, not a pick UI, so pagination does not apply; show every plan.
5. Stop here — do not continue to the planning steps below.

**Path C — No arguments** (`$ARGUMENTS` is empty):
1. Glob for all `*.md` files in `~/ideas/`.
2. For each idea, extract the slug from the filename (strip `YYYY-MM-DD-` prefix and `.md` suffix).
3. Check whether `~/plans/<slug>.md` exists — if it does, skip this idea (already planned).
4. If no unplanned ideas remain, tell the user everything is planned and stop.
5. Order the remaining unplanned ideas using the ranking spec in [assets/ranking-spec.md](assets/ranking-spec.md). For each idea, its project comes from `**Project:**` in the idea's Context section and its recency from the `YYYY-MM-DD-` filename prefix.
6. Present the ranked ideas to the user with AskUserQuestion and let them pick one, following the spec's **Presentation** (mark current-repo ideas with ★) and **Overflow** (top 3 ideas + a "Show more…" option that re-prompts with the next page) rules.
7. Continue to Step 1 with the chosen slug.

### 1. Load the Idea

Read the idea slug (from Path A or Path C above). Find the matching file in `~/ideas/` — match against filenames that end with `-<slug>.md` (the date prefix varies). If multiple files match, show them and ask the user to pick one. If no file matches, list available idea files and ask the user to choose.

Read the full idea document.

### 2. Gather Context

Review the idea document thoroughly. If the idea references a project or specific files:
- Use the Agent tool to explore the relevant parts of the codebase to understand existing patterns, conventions, and constraints. **Pin these codebase-exploration spawns to `model: sonnet`.**
- Note what already exists that can be reused vs. what needs to be built.

Carry the gathered context forward — you will hand it to the planner subagent in Step 3.

If you need more information to create a good plan, ask the user clarifying questions — **one question at a time**. Only ask questions when truly necessary; prefer making reasonable assumptions and noting them in the plan.

### 3. Generate the Plan

Delegate the decomposition reasoning to the pinned `jador:planner` subagent (Agent tool, `subagent_type: jador:planner`) — mirroring how `jador:critique` delegates to `jador:adversary`. This keeps the highest-leverage reasoning in the pipeline on a strong, pinned model regardless of the session model. Give it a task message containing:

- The **idea document** (the full text you loaded in Step 1).
- The **gathered codebase context** from Step 2 — existing patterns, conventions, constraints, and what can be reused vs. built new.
- The **absolute path to the plan template** ([assets/plan-template.md](assets/plan-template.md)) so it formats its output correctly.

The planner works **report-and-stop**: it does the decomposition, returns the plan draft as its result (delivered asynchronously via a completion notification), and ends its turn. **Await that completion notification** — do not treat the spawn return as the result, and do not proactively ping the planner. Once the draft lands, carry it into Step 4 for review. Every later revision (Steps 4 and 5) is a **fresh** `jador:planner` spawn given the current draft plus the changes — so all decomposition, not just the first draft, runs on the pinned model, with **no dependency on keeping a subagent alive** across your review loop or a nested critique run. (Re-spawning per pass rather than parking one live subagent is deliberate: the decomposition rules are single-sourced in the planner, so a fresh spawn handed the current draft gets the identical result without any liveness/resume fragility.)

The **decomposition rules** the planner enforces (small/self-contained, specific, ordered, dependencies explicit, parallelism noted, verified) live in [`agents/planner.md`](../../agents/planner.md) — the planner owns them. Do **not** restate them here; that is the single source of truth for how a plan is decomposed.

The planner formats its draft against the template at [assets/plan-template.md](assets/plan-template.md).

### 4. Present for Review

Present the plan (the planner's latest draft) as a fenced markdown block. Tell the user:

- They can approve the plan as-is.
- They can request changes to specific tasks (add, remove, split, merge, reorder, re-detail).
- They can ask you to add more detail to any task.

When the user requests changes, **do not edit the draft yourself** — restructuring the task graph in the parent would move decomposition off the planner's pinned model. Instead, **spawn a fresh `jador:planner`** (Agent tool, `subagent_type: jador:planner`) with the current draft plus the requested changes; it applies them against its decomposition rules, returns the revised draft, and stops. Await the completion notification, then re-present. Loop until the user approves. (A fresh spawn each pass — rather than reusing a parked subagent — is deliberate: it needs no liveness across your review turns and survives a resumed session, while still keeping every revision on the pinned model.)

### 5. Offer a Design Critique

Once the user is happy with the plan but before writing it, offer to stress-test it. Use AskUserQuestion:
- **Run critique**: invoke `/jador:critique plan <slug>` via the Skill tool. A dedicated adversary reviews the plan for soundness — it names the load-bearing assumptions, proposes at least one concrete alternative, and states what would make this the wrong approach. Findings come back conversationally; for any the user accepts, **spawn a fresh `jador:planner`** with the current draft plus the accepted findings to fold them in (keeping the critique-driven restructuring on the pinned model), await the revised draft, and re-present before continuing.
- **Skip**: write the plan as-is.

### 6. Write the Plan

Once approved (and after folding in any critique revisions):

1. Determine the `project:` value for the frontmatter: run `basename "$(git rev-parse --show-toplevel 2>/dev/null)"`, falling back to the current working directory's basename when not in a git repo (see the **Current-repo identity** rule in [assets/ranking-spec.md](assets/ranking-spec.md)). Write this into the plan frontmatter's `project:` field.
2. Write the file to `~/plans/<slug>.md` using the slug from the idea filename.
3. Confirm the file path to the user.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** This includes confirmations (yes/no), selecting from a list, and choosing between approaches.

## Guidelines

The decomposition judgment — task granularity (prefer smaller, 1-3 files), when to group vs. split, tests as their own tasks, flagging open questions with stated assumptions, and completeness — lives in the `jador:planner` subagent that produces the plan (see Step 3). Do not re-derive it here; the planner owns it. When the user requests review edits (Step 4) or accepts critique revisions (Step 5), you **forward** them to a fresh `jador:planner` spawn (per those steps) — you do not apply decomposition judgment yourself; the planner does, keeping it on the pinned model.

## Shared Ranking Spec — current-repo-first ordering

The single source of truth for current-repo-first ordering lives in a standalone asset: **[assets/ranking-spec.md](assets/ranking-spec.md)**. Read it whenever you present a list of shared-directory docs (ideas, plans, notes). It defines current-repo identity, project matching (including the legacy-plan `idea:`-trace resolution and the unresolvable → "other" bucket), the recency rule, sort order, ★ presentation, and pick-UI overflow/pagination. Do not restate those rules here or in any consumer skill — reference the asset.
