---
name: worktree-cleanup
description: Scan git worktrees for a repo, categorize each by safety-to-remove using GitHub PR status and local commit position, and remove the ones you approve
disable-model-invocation: false
---

# Worktree Cleanup Skill

You scan all git worktrees for the current repo, categorize each one by whether it's safe to remove — cross-checking GitHub PR status against local commit position, plus duplicate/ancestor detection — and remove the categories the user approves. All the actual scanning, categorization, and removal logic lives in the bundled script; you drive it and present its output conversationally.

## Prerequisites

Before doing anything, verify the environment (the bundled script checks these too and will fail with the same guidance, but check here first so a missing tool doesn't burn a scan):

1. **Check `gh` CLI is available:** Run `which gh`. If it fails, tell the user: "The `gh` CLI is required but not found on your PATH. Install it from https://cli.github.com/ and try again." Then stop.
2. **Check `wt` (worktrunk) CLI is available:** Run `which wt`. If it fails, tell the user: "The `wt` (worktrunk) CLI is required but not found on your PATH. Install it from https://github.com/max-sixty/worktrunk and try again." Then stop.
3. **Check `jq` is available:** Run `which jq`. If it fails, tell the user: "The `jq` CLI is required but not found on your PATH. Install it and try again." Then stop.
4. **Check this is a git repo:** Run `git rev-parse --is-inside-work-tree`. If it fails, tell the user: "This command must be run from inside a git repository." Then stop.

## Process

### 1. Scan

Run the bundled script with a fresh scan, capturing stdout and stderr separately (stdout carries only the plan JSON; stderr carries any warnings — never merge them with `2>&1`):

```
${CLAUDE_SKILL_DIR}/assets/worktree-cleanup.sh --format=json
```

This scans every worktree, categorizes each one, and atomically writes the result to a plan cache file on disk (the script's own concern — you don't need to do anything with the cache path except read it out of the JSON for later reference). Parse stdout as JSON with this shape:

```json
{
  "generated_at": "2026-08-24T18:00:00Z",
  "repo": "owner/repo",
  "default_branch": "main",
  "entries": [
    {
      "branch": "some-branch",
      "path": "/absolute/path/to/worktree",
      "sha": "abc123...",
      "dirty": false,
      "ignored_count": 0,
      "category": "merged",
      "reason": "...",
      "pr_number": 123,
      "pr_title": "...",
      "pr_url": "https://...",
      "original_category": "open",
      "original_reason": "..."
    }
  ]
}
```

`reason`, `pr_number`, `pr_title`, and `pr_url` are present only on entries where they apply (e.g. `reason` on `needs_review`/`dirty_skipped`/`error`; the `pr_*` fields on any entry a PR was matched to). `original_category`/`original_reason` appear only on a `dirty_skipped` entry whose dirty override actually superseded a more specific category — e.g. a dirty worktree with an open PR keeps its `pr_*` fields and reports `original_category: "open"`, and a dirty worktree whose PR lookup itself failed reports `original_category: "error"`. When presenting the report, mention this: a dirty worktree with `original_category: "open"` is active work that's also dirty, not just clutter. If stderr is non-empty, note its content — it names branches whose `gh pr list` lookup failed so you can call them out in the report (check both `category == "error"` and `original_category == "error"`, since a dirty+lookup-failure entry reports `category: "dirty_skipped"`).

The category set:
- **Safe to remove**: `merged`, `closed`, `empty`, `duplicate`
- **Never removed**: `open` (has a live PR), `needs_review` (ambiguous — unpushed commits beyond a merged/closed PR, or a no-PR branch that isn't a duplicate/empty), `dirty_skipped` (uncommitted changes present), `error` (the PR lookup itself failed — treat as unknown, not safe)

### 2. Present the Report

Summarize the scan conversationally, grouped the same way the categories break down above (safe categories first, then the rest). For each category with at least one entry, show the count and the branch names. For `needs_review`, `dirty_skipped`, and `error` entries, include the `reason` alongside each branch. For any entry with `ignored_count > 0`, mention it (e.g. "3 ignored files") — this is the one thing removal can't restore even though branches are never deleted, so it deserves visibility, not silent risk. Omit any category with zero entries — don't print empty headers.

If there are zero worktrees to report (only the main/current worktree exists), say so and stop — there's nothing to offer removing.

### 3. Offer Removal

Use `AskUserQuestion` to offer what to do with the safe categories (`merged`, `closed`, `empty`, `duplicate`) found in the scan. Only offer this if at least one safe-category entry exists — if none exist, skip straight to a closing note that there's nothing safe to remove this run. Options:

- **Remove all safe worktrees** — every entry in `merged`, `closed`, `empty`, `duplicate`.
- **Pick specific categories** — follow up with another `AskUserQuestion` (multi-select) listing only the safe categories that actually have entries this scan, so the user can choose a subset.
- **Skip** — don't remove anything. End here.

Never offer `open`, `needs_review`, `dirty_skipped`, or `error` as removable — they are not in the safe set and the script itself will reject them if named via `--categories`.

### 4. Apply and Report Outcomes

If the user chose to remove something, run:

```
${CLAUDE_SKILL_DIR}/assets/worktree-cleanup.sh --apply --categories=<comma-separated selected categories>
```

This does **not** re-scan — it operates on the plan cache written in Step 1, so what was shown to the user is exactly what gets acted on. Before touching anything, it re-validates that every requested category is in the safe set (rejecting otherwise) and hard-excludes any `dirty_skipped` entry regardless of what's requested. Immediately before removing each individual worktree, it re-checks five things against what the plan recorded, all via plain local `git` reads (no `gh`, no `wt list`, no re-categorization): that the path is still a worktree of the current repo (catches a stale/moved worktree, or a plan file from a different repo); that the path isn't the worktree the apply run is itself standing in; that the commit sha hasn't advanced; that the worktree hasn't become dirty; and that it hasn't gained more ignored files than the scan recorded (ignored files are the one thing this tool can still destroy, since branches are never deleted). Any mismatch skips just that entry rather than removing it — a multi-entry removal run can span minutes and state can drift mid-run. It never passes any branch-deletion flag to the underlying `wt remove` call — branches always survive; only the worktree directory is removed.

Parse the final summary output and report each branch's outcome to the user:
- **`removed`** — the worktree directory is gone. The branch itself always survives (`--no-delete-branch` is unconditional), so there's only this one success outcome — don't read anything into it about whether the branch was "actually" merged; that's what the scan's `category` already told you.
- **`failed`** — removal attempt errored; show the failure detail.
- **`skipped_drift`** — not removed because something changed since the scan (path no longer a worktree of this repo, is the current worktree, commit advanced, became dirty, or gained ignored files); show the reason. Tell the user to re-scan (re-run this skill) to pick these up on a future pass — the current plan cache is now stale for that entry.

If the plan the apply step consumed was written more than 60 minutes before this apply ran, the script emits a staleness warning rather than failing — surface that warning to the user if present, but the apply still proceeds.

If any entry's outcome is `failed`, the script exits non-zero — note that plainly in your summary, distinct from the non-fatal `skipped_drift` entries.
