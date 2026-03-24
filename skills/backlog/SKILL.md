---
name: backlog
description: Surface idle work from the discuss/plan/execute workflow. Scans ~/ideas/ for unplanned ideas and plans/ for unexecuted plans, then offers to route into /plan or /execute.
disable-model-invocation: true
---

# Backlog Skill

You are a backlog scanner. Your job is to surface idle work — ideas without plans and plans without execution — then offer to route the user into action.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** This includes confirmations (yes/no), selecting from a list, and choosing between approaches.

## Process

### 1. Scan Ideas

Use Glob to find all files matching `~/ideas/*.md`. For each idea file:

1. Extract the slug from the filename: strip the `YYYY-MM-DD-` date prefix and `.md` suffix (e.g., `2026-03-24-backlog.md` becomes `backlog`).
2. Extract the date from the filename prefix (`YYYY-MM-DD`) and format it as `Mon DD` (e.g., `Mar 24`).
3. Read the file and extract the title from the first H1 heading (`# ...`).
4. Check whether `CLAUDE_PLUGIN_DATA/plans/<slug>.md` exists. If no matching plan exists, the idea is **unplanned**.

Collect all unplanned ideas into a list.

### 2. Scan Plans

Use Glob to find all files matching `CLAUDE_PLUGIN_DATA/plans/*.md`. For each plan file:

1. Read the file and parse the YAML frontmatter.
2. If the `status` field is `pending`, the plan is **unexecuted**.
3. Extract the title from the first H1 heading (`# ...`).
4. Extract the `created` date from frontmatter and format it as `Mon DD`.

Collect all unexecuted plans into a list.

### 3. Display Results

If both lists are empty, print a congratulatory message:

```
Nothing idle — all ideas are planned and all plans are executed. Nice work.
```

Otherwise, print the results under an `## Idle Work` heading with the following subsections (only include a subsection if it has items):

```
## Idle Work

### Ideas without plans (N)
- **<Title>** (<Mon DD>)
- **<Title>** (<Mon DD>)

### Unexecuted plans (N)
- **<Title>** (planned <Mon DD>)
- **<Title>** (planned <Mon DD>)
```

### 4. Offer Routing

After displaying results, use AskUserQuestion to offer next steps. Only include options that are applicable:

- **Plan an idea** — only if there are unplanned ideas. If selected, use a follow-up AskUserQuestion to let the user pick which idea, then invoke `/jador:plan <slug>` via the Skill tool.
- **Execute a plan** — only if there are unexecuted plans. If selected, use a follow-up AskUserQuestion to let the user pick which plan, then invoke `/jador:execute <slug>` via the Skill tool.
- **Just reviewing** — always available. Ends the skill.
