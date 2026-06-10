---
name: handoff
description: Synthesize or read an agent handoff doc — a living "what actually happened" digest (decisions, deviations, gotchas, open threads) at .claude/agent-handoff.md, uncommitted. Use to package finished work for the next agent, or to load prior context. Invoked manually, and by execute (synthesize at completion) and babysit (read for context).
argument-hint: [synthesize|read]
disable-model-invocation: true
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

1. Resolve the repo/worktree root with `git rev-parse --show-toplevel`. If not in a git repo, tell the user the handoff requires a git working tree and stop.
2. The handoff path is `<root>/.claude/agent-handoff.md`.
3. Ensure `<root>/.claude/` exists (create it if missing).
4. Ensure the file is locally ignored without touching the shared `.gitignore`: if `.claude/agent-handoff.md` is not already present in `<root>/.git/info/exclude`, append it. Use a guarded write so the entry is never duplicated:
   ```bash
   grep -qxF '.claude/agent-handoff.md' "$(git rev-parse --show-toplevel)/.git/info/exclude" \
     || echo '.claude/agent-handoff.md' >> "$(git rev-parse --show-toplevel)/.git/info/exclude"
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
7. **Critique findings**: if `<root>/.claude/critique.md` exists, fold its open items into the Open Threads section.

### 4. Synthesize Mode — Write the Handoff

Render `assets/handoff-template.md`, filling every section from the gathered inputs and obeying the digest rules in **General Rules**. Write the result to `<root>/.claude/agent-handoff.md`, overwriting any existing file (synthesize always produces a fresh, coherent digest from current state — it does not append). Set the frontmatter `last_updated` to the current timestamp. Then go to Step 6.

If re-invoked later in the same session (e.g. the user asked for changes after the first synthesis), re-run Steps 3–4 to refresh the digest.

### 5. Read Mode — Load and Summarize

1. If `<root>/.claude/agent-handoff.md` does not exist, report "No handoff doc present" and stop — the caller should proceed without it.
2. If it exists, read it and return a tight summary for the caller: what shipped, the load-bearing decisions and deviations, the gotchas (so the caller does not "fix" deliberate choices), and the open threads. This summary is the context a babysitter uses to brief its workers. Do not modify the file in read mode.

### 6. Report

- **Synthesize**: confirm the path written and print the section headers with a one-line preview of each, so the user sees what the next agent will receive.
- **Read**: present the summary from Step 5.
