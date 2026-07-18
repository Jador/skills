---
name: backlog
description: Surface idle work from the discuss/plan/execute workflow. Scans ~/ideas/ for unplanned ideas, ~/plans/ for unexecuted plans, and ~/notes.md for notes without matching ideas, then offers to route into /plan, /execute, or /discuss.
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
2. Extract the date from the filename prefix (`YYYY-MM-DD`) — keep the raw `YYYY-MM-DD` for sorting, and format a `Mon DD` (e.g., `Mar 24`) for display.
3. Read the file and extract the title from the first H1 heading (`# ...`) and the `**Project:**` value from the idea's Context section (for ranking; treat a missing project as "other").
4. Check whether `~/plans/<slug>.md` exists. If no matching plan exists, the idea is **unplanned**.

Collect all unplanned ideas into a list.

### 2. Scan Plans

Use Glob to find all files matching `~/plans/*.md`. For each plan file:

1. Read the file and parse the YAML frontmatter.
2. If the `status` field is `pending`, the plan is **unexecuted**.
3. Extract the title from the first H1 heading (`# ...`).
4. Extract the `created` date from frontmatter — keep the raw `YYYY-MM-DD` for sorting and format a `Mon DD` for display.
5. Extract the `project:` frontmatter field for ranking. For legacy plans lacking `project:`, resolve it per the Shared Ranking Spec (trace the `idea:` path to the source idea's `**Project:**`); treat an unresolvable project as "other".

Collect all unexecuted plans into a list.

### 3. Scan Notepad

Read `~/notes.md`. If the file exists, parse each entry — entries are separated by `## <ID>. <Title>` headings. For each entry:

1. Extract the title from the heading.
2. Normalize the title into a slug (lowercase, hyphens, no special characters).
3. Check whether any file in `~/ideas/` ends with `-<slug>.md` (approximate match). If a matching idea exists, skip this entry.
4. If no matching idea exists, the note is **undiscussed**.
5. Extract the note's per-entry `**Project:**` field (for ranking; missing project is "other") and its `**Added:**` date (raw `YYYY-MM-DD`, for recency sorting).

Collect all undiscussed notes into a list. If `~/notes.md` doesn't exist or is empty, the list is empty.

### 4. Display Results

If all three lists are empty, print a congratulatory message:

```
Nothing idle — all ideas are planned, all plans are executed, and no stray notes. Nice work.
```

Otherwise, print the results under an `## Idle Work` heading with the following subsections (only include a subsection if it has items).

**Order within each subsection** using the **Shared Ranking Spec** (the canonical copy lives in the plan skill at `skills/plan/SKILL.md`, delimited by `SHARED-RANKING-SPEC:BEGIN`/`:END`): current-repo docs first, then recency descending, filename tiebreak. The current-repo identity is `basename "$(git rev-parse --show-toplevel 2>/dev/null)"` (falling back to the working directory's basename). Match each doc against it using the project captured during the scan (notes → per-entry `**Project:**`, ideas → Context `**Project:**`, plans → `project:` frontmatter). Recency is the note's `**Added:**` date, the idea's filename date, and the plan's `created:` date respectively.

Prefix each current-repo doc's line with a leading **★ ** glyph; other-repo docs get no marker. These are full listings (not pick UIs), so show every item — pagination does not apply.

```
## Idle Work

### Notes without ideas (N)
- ★ **<Title>** (note #<ID>)
- **<Title>** (note #<ID>)

### Ideas without plans (N)
- ★ **<Title>** (<Mon DD>)
- **<Title>** (<Mon DD>)

### Unexecuted plans (N)
- ★ **<Title>** (planned <Mon DD>)
- **<Title>** (planned <Mon DD>)
```

### 5. Offer Routing

After displaying results, use AskUserQuestion to offer next steps. Only include options that are applicable:

- **Discuss a note** — only if there are undiscussed notes. If selected, use a follow-up AskUserQuestion to let the user pick which note, then invoke `/jador:discuss note <id>` via the Skill tool.
- **Plan an idea** — only if there are unplanned ideas. If selected, use a follow-up AskUserQuestion to let the user pick which idea, then invoke `/jador:plan <slug>` via the Skill tool.
- **Execute a plan** — only if there are unexecuted plans. If selected, use a follow-up AskUserQuestion to let the user pick which plan, then invoke `/jador:execute <slug>` via the Skill tool.
- **Just reviewing** — always available. Ends the skill.

Each follow-up "pick which" AskUserQuestion is a **pick UI**, so order its options by the same **Shared Ranking Spec** used in Display Results (current-repo docs first, then recency descending), mark each current-repo option with a leading **★**, and apply the spec's **Overflow** rule: if that doc type has more than 4 items, show the **top 3 ranked** as options plus a 4th **"Show more…"** option that re-prompts (AskUserQuestion again) with the next page of 3 + "Show more…", continuing until every item has been reachable.
