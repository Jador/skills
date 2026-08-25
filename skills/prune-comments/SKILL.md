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

If `$ARGUMENTS` names one or more paths, that file list is the scope, and the granularity is **whole-file** — the caller named these files explicitly, so review every comment in them.

Otherwise, derive the changeset scope using items 1–2 of `skills/critique/SKILL.md`'s "### 2. Gather Inputs" → **Changeset mode** substep (repo-root resolution and diff construction) — the pure derivation only. Do not run that substep's item 3 (intent-gathering and handoff-deferral); this skill has no intent to gather and nothing to reconcile against a handoff. The granularity here is **hunk-adjacent** — only comments touching the changed hunks are in scope; a pre-existing comment elsewhere in a touched file is not, even though the file itself is part of the diff.

### 2. Run the Comment Auditor

Spawn the `jador:comment-auditor` subagent (Agent tool, `subagent_type: jador:comment-auditor`). Give it a task message containing only the resolved scope and its granularity — the file list (whole-file), or the diff plus the file paths so it can read context itself (hunk-adjacent).

The agent works **report-and-stop**: it returns its findings and ends its turn, delivering the result asynchronously via a completion notification. **Await that completion notification.** Do not treat the spawn/SendMessage return as the result, and do not proactively ping the agent for its findings — the completion arrives on its own.

The report you receive follows the **Report schema**: four sections in order — `Files touched`, `Deletions`, `MUST KILL`, `Skipped` — each heading carrying a count. Steps 3–7 below consume it section by section.

### 3. Auto-Delete the Clear-Cut Findings

For every `Deletions` entry labeled `clear-cut`: first check whether a `MUST KILL` entry lists it under `patches`. If so, skip it here — it's handled in step 5, which fixes the symbol before this comment is deleted. If not linked, remove the comment with no approval gate — this is what "clear-cut deletions are never gated behind a question" means in practice.

For each unlinked entry, use its `file` and verbatim comment text to locate the exact lines in the current file — the `line range` is a starting point, but earlier deletions in this same pass may have already shifted line numbers, so confirm by matching the verbatim text before editing. Delete the comment (and, if it was the only content on its line, the line itself).

Report the total count of clear-cut deletions applied (unlinked ones now; linked ones are counted when step 5 completes them).

### 4. Negotiate the Ambiguous Ones

Before negotiating, check the same `patches` linkage as step 3: if a `Deletions` entry labeled `ambiguous` is linked to a `MUST KILL` entry, defer it — it's handled once step 5 lands (or, if step 5's fix is itself design-blocked, this entry is now also blocked; report it open rather than negotiating a deletion whose defect is still live).

For every unlinked `Deletions` entry labeled `ambiguous`, surface it to the user via AskUserQuestion — one question per entry (or batched sensibly if several are trivially related), each showing the verbatim comment text and its location.

An `ambiguous` entry is worth asking about because it's a genuine judgment call, typically one of two shapes:

- **A keep claim that is plausible but unverifiable** — the auditor couldn't confirm or refute it from history alone, and it's the user's call whether it still holds.
- **A constraint comment** ("do not remove", "talk to X first") where the real choice isn't keep-vs-delete but *how* to preserve the constraint: delete the comment outright, or encode the constraint more durably as a type, a lint rule, or a test.

For the second shape, always offer the cheapest in-scope encoding as one of the options — don't just offer a binary keep/delete.

On approval of an encoding: apply the encoding **first**, then delete the comment. On decline (or a plain "delete"): delete the comment and record the constraint as an open item in the report — do not silently drop it.

### 5. Fix the MUST KILL Symbols

For each `MUST KILL` entry, apply its reported `fix shape` (rename, extract, type, test, lint, or fix) at the smallest scope that addresses the root cause the `reason` describes — not just at the comment site.

If the entry carries a `patches` link, apply the fix **first**, then delete the linked comment (the `clear-cut` or `ambiguous`-but-deferred-from-step-4 entry) immediately after — this is the ordering finding 2 exists to enforce: never delete a suppression or a MUST KILL-patched comment before its underlying defect is fixed. Fold the linked comment into the deletion count from whichever step it originated in.

If a fix genuinely requires a design decision you can't make unilaterally (e.g. the rename is ambiguous between two reasonable names, or the extraction implies an API change), don't guess — leave it as an open item in the report instead, and leave its linked comment (if any) in place undeleted — a comment protecting a still-unfixed defect must not be removed.

### 6. Verify Code Changes

Comment-only edits (steps 3–4, unlinked) don't change runtime or type behavior, so they need no verification. But step 5's `MUST KILL` fixes do touch actual code — renames, extractions, type changes, new tests, lint fixes, or direct bug fixes — so if step 5 applied at least one fix, run the project's lint, typecheck, and test commands on the touched files (check for existing scripts — e.g. `package.json` scripts, a `Makefile`, or a CI config — before guessing a command). If step 5 made no fixes, skip this step entirely.

If verification fails, the fix that touched the failing area is suspect: revert that specific fix (and restore any comment it had authorized deleting) and report it as an open item rather than leaving the tree broken. Do not revert fixes unrelated to the failure.

### 7. Report

Close with a report covering:

- **Deletion count** — clear-cut plus ambiguous-approved-for-deletion, total.
- **Encodings offered vs. applied** — for each ambiguous constraint entry, whether an encoding was offered and whether the user approved it.
- **`MUST KILL` fixes made** — symbol, location, and fix shape applied.
- **Verification result** — whether step 6 ran, and its outcome (skipped because no code fix landed, passed, or which fix was reverted after a failure).
- **Everything left open** — declined-encoding constraints recorded per step 4, design-blocked `MUST KILL` entries from step 5, and any deletion left in place because its linked fix is still open.

Never report on or touch `Skipped` entries — they survived the audit and are out of scope for this skill entirely.

## No Looping

If a re-reviewed or rejected finding fails a second pass — the user declines the same negotiation twice, a fix attempt doesn't resolve a `MUST KILL` entry on retry, or a verification failure recurs after one re-attempt of the fix — report it as an open item rather than re-prompting or re-attempting indefinitely.
