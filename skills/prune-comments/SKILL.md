---
name: prune-comments
description: Prunes unjustified comments (narration, dead workaround sermons, thin "do not remove" excuses) from a changeset or a given file list, using the read-only `jador:comment-auditor` subagent to find them. Clear-cut findings are deleted automatically; only genuine judgment calls are put to the user. Also fixes symbols the auditor flags as `MUST KILL` at root-cause scope rather than papering over them locally.
argument-hint: [<paths>]
disable-model-invocation: true
---

# Prune Comments Skill

You are the entry point that turns the comment auditor's read-only report into actual edits. You resolve the scope to review, spawn `jador:comment-auditor` to hunt for unjustified comments within it, then act on what it finds — deleting the clear-cut cases outright, negotiating the judgment calls with the user, and fixing any `MUST KILL` symbol at its root cause rather than just at the comment site.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.**
- **Clear-cut deletions are never gated behind a question.** Only genuine judgment calls go to the user.
- **Never edit outside the resolved scope.** Findings on files not in scope are reported, not acted on.

## Process

### 1. Resolve Scope

If `$ARGUMENTS` names one or more paths, that file list is the scope.

Otherwise, derive the changeset scope using items 1–2 of `skills/critique/SKILL.md`'s "### 2. Gather Inputs" → **Changeset mode** substep (repo-root resolution and diff construction) — the pure derivation only. Do not run that substep's item 3 (intent-gathering and handoff-deferral); this skill has no intent to gather and nothing to reconcile against a handoff.

### 2. Run the Comment Auditor

Spawn the `jador:comment-auditor` subagent (Agent tool, `subagent_type: jador:comment-auditor`). Give it a task message containing only the resolved scope — the file list, or the diff plus the file paths so it can read context itself.

The agent works **report-and-stop**: it returns its findings and ends its turn, delivering the result asynchronously via a completion notification. **Await that completion notification.** Do not treat the spawn/SendMessage return as the result, and do not proactively ping the agent for its findings — the completion arrives on its own.
