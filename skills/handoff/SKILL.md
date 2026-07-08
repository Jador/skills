---
name: handoff
description: Synthesize or read an agent handoff doc — a living "what actually happened" digest (decisions, deviations, gotchas, open threads) at `.claude/handoffs/<branch>.md`, uncommitted. Use to package finished work for the next agent, or to load prior context. Invoked manually, and by execute (synthesize at completion) and babysit (read for context).
argument-hint: [synthesize|read]
disable-model-invocation: false
---

# Handoff Skill

You are a handoff synthesizer. Your job is to package what actually happened during a piece of work — decisions, deviations, gotchas, open threads — into a single uncommitted digest that a fresh agent (typically a PR babysitter) can read to pick up with full context. The digest is a complement to the plan, never a duplicate of it: the plan is the static spec, the handoff is the living record of how reality diverged from it.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** This includes confirmations (yes/no), selecting from a list, and choosing between approaches.
- **The handoff is a digest, not an archive.** Keep it under ~120 lines. Reference artifacts (PR, files, commits, plan), never paste diffs or logs. Capture *why*, not just *what*. Collapse empty sections to "None" rather than padding.
- **Never duplicate the plan or spec.** Record only the delta from it.

## Process

### 1. Determine Mode

Parse `$ARGUMENTS` for the mode. Two modes exist:

- **`synthesize`** (default when no mode is given) — build or rebuild the handoff from the current state of the work.
- **`read`** — load the existing handoff and summarize it for the caller.

If `$ARGUMENTS` contains neither word, default to `synthesize`.

### 2. Locate the Handoff File

The handoff is keyed by branch so every line of work — per worktree, per branch — keeps its own uncommitted digest. Switching branches never reads or overwrites another branch's handoff.

1. Resolve the repo/worktree root `<root>` with `git rev-parse --show-toplevel`. If not in a git repo, tell the user the handoff requires a git working tree and stop.
2. **Derive the branch path** (the canonical derivation, referenced by other skills):
   - Run `git branch --show-current`. If it returns a name, use that name **verbatim** as a relative path — keep any `/` as real subdirectories (e.g. `feat/foo` → `feat/foo`).
   - If it returns empty (detached HEAD), use `detached-<short-sha>` where `<short-sha>` is `git rev-parse --short HEAD`.
   - Call the result `<branch>`.
3. The handoff path is `<root>/.claude/handoffs/<branch>.md`. (The paired critique lives at `<root>/.claude/critiques/<branch>.md`.)
4. Ensure the parent directory exists with `mkdir -p "<root>/.claude/handoffs/$(dirname "<branch>")"` — the `mkdir -p` handles nested branch names (the `feat/` in `feat/foo`).
5. Ensure the handoff directory is locally ignored without touching the shared `.gitignore`: add the **directory** entry `.claude/handoffs/` to the exclude file if absent. Resolve the exclude path with `git rev-parse --git-path info/exclude` — **not** `<root>/.git/info/exclude`, which breaks in worktrees where `.git` is a file (the real exclude lives in the shared common git dir; one entry there covers every worktree and branch). Use a guarded write so the entry is never duplicated:
   ```bash
   excl="$(git rev-parse --git-path info/exclude)"
   grep -qxF '.claude/handoffs/' "$excl" || echo '.claude/handoffs/' >> "$excl"
   ```

### 3. Synthesize Mode — Gather Inputs

> Skip to Step 5 if the mode is `read`.

Collect the raw material for the digest from what is actually available — do not speculate:

1. **Branch & PR**: current branch (`git branch --show-current`) and, if a PR exists, `gh pr view --json number,url,title` for the anchor.
2. **Plan reference**: if a plan drove this work, find it (e.g. `~/plans/<slug>.md` matching the branch/idea) and record its path — do not copy its contents.
3. **Commits**: `git log --oneline <base>..HEAD` for the task→commit trail. Determine `<base>` from the PR base branch when available, else the default branch.
4. **Files touched**: `git diff --name-status <base>..HEAD` for the key-files list (paths only — pair each with a one-line "what changed and why" from session context).
5. **Session context**: the decisions, deviations, surprises, and open threads observed during this session's work. When invoked by execute at completion, draw these from the sub-agent return summaries and the execution narrative. When invoked manually, draw from the conversation.
6. **Verification state**: build/test/lint status as last observed; which CI is expected to pass or fail and why.
7. **Critique findings**: if `<root>/.claude/critiques/<branch>.md` exists (same `<branch>` derived in Step 2), fold its open items into the Open Threads section.

### 4. Synthesize Mode — Write the Handoff

Render `assets/handoff-template.md`, filling every section from the gathered inputs and obeying the digest rules in **General Rules**. Write the result to the keyed handoff path `<root>/.claude/handoffs/<branch>.md` (Step 2) — never to any legacy path — overwriting any existing file (synthesize always produces a fresh, coherent digest from current state — it does not append). Set the frontmatter `last_updated` to the current timestamp. Then go to Step 6.

If re-invoked later in the same session (e.g. the user asked for changes after the first synthesis), re-run Steps 3–4 to refresh the digest.

### 5. Read Mode — Load and Summarize

1. Read the keyed handoff path `<root>/.claude/handoffs/<branch>.md` (Step 2).
2. **Branch-aware legacy fallback.** If the keyed file is absent **and** the old `<root>/.claude/agent-handoff.md` exists, read that legacy file's `branch:` frontmatter field. Use it **only if** that value equals the current branch (`<branch>` from Step 2); otherwise treat it as no handoff — a legacy file from another branch must never be served here, or a stale handoff would silently leak across branches. Do not migrate or rewrite the legacy file.
3. If neither the keyed file nor a matching legacy file is found, report "No handoff doc present" and stop — the caller should proceed without it.
4. Otherwise, read the located file and return a tight summary for the caller: what shipped, the load-bearing decisions and deviations, the gotchas (so the caller does not "fix" deliberate choices), and the open threads. This summary is the context a babysitter uses to brief its workers. Do not modify the file in read mode.

### 6. Report

- **Synthesize**: confirm the path written and print the section headers with a one-line preview of each, so the user sees what the next agent will receive.
- **Read**: present the summary from Step 5.
