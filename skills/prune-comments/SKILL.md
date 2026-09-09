---
name: prune-comments
description: Prunes unjustified comments (narration, dead workaround sermons, thin "do not remove" excuses) from a changeset or a given file list, using the read-only `jador:comment-auditor` subagent to find them. Clear-cut findings are deleted automatically; only genuine judgment calls are put to the user. Also fixes symbols the auditor flags as `MUST KILL` at root-cause scope rather than papering over them locally. Supports `--report <path>` to consume an already-produced audit report instead of spawning the auditor, and `--non-interactive` to run without any `AskUserQuestion` prompts.
argument-hint: "[<paths>] [--report <path>] [--non-interactive]"
disable-model-invocation: false
---

# Prune Comments Skill

You are the entry point that turns the comment auditor's read-only report into actual edits. You resolve the scope to review, spawn `jador:comment-auditor` to hunt for unjustified comments within it, then act on what it finds — deleting the clear-cut cases outright, negotiating the judgment calls with the user, and fixing any `MUST KILL` symbol at its root cause rather than just at the comment site.

## General Rules

- **In interactive mode, always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** Under `--non-interactive`, no `AskUserQuestion` call is ever made — see step 5.
- **Clear-cut deletions are never gated behind a question.** Only genuine judgment calls go to the user.
- **Never edit outside the resolved scope.** Findings on files not in scope are reported, not acted on.

## Process

### 1. Resolve Invocation Mode

Parse `$ARGUMENTS` for `--report <path>` and `--non-interactive` before doing anything else; strip them out so the remainder, if any, is the explicit path list consumed in step 2.

- **`--report <path>`**: the caller supplies an already-produced four-section audit report instead of asking this skill to gather one. Read the report from `<path>` and validate it has the four sections — `Files touched`, `Deletions`, `MUST KILL`, `Skipped` — in order. If it doesn't parse, stop and report that rather than guessing. In this mode, **skip step 3 (Run the Comment Auditor) entirely** — do not spawn `jador:comment-auditor`, do not await a completion notification. Treat the parsed report as step 3's output and continue from step 4.
- **`--non-interactive`**: no `AskUserQuestion` call may occur anywhere in this run. This step only parses and carries the flag forward; what replaces interactive negotiation in step 5 under this flag is defined elsewhere, not here.
- **Neither flag**: today's behavior — resolve scope (step 2), spawn the auditor (step 3), and negotiate interactively (step 5).

These two flags are what `/jador:execute`'s per-task checkpoint uses to drive this skill non-interactively against a pre-built report; see `skills/execute/assets/pairing-protocol.md` for how that checkpoint produces and passes them.

### 2. Resolve Scope

If in `--report` mode, the scope is the report's `Files touched` list, intersected with any explicit paths given in `$ARGUMENTS`; with no explicit paths given, the scope is the full `Files touched` list. Granularity is **whole-file**. The existing rule "never edit outside the resolved scope" still governs.

Otherwise, if `$ARGUMENTS` names one or more paths, that file list is the scope, and the granularity is **whole-file** — the caller named these files explicitly, so review every comment in them.

Otherwise, derive the changeset scope using the **Derive the diff scope** substep of `skills/critique/SKILL.md`'s "### 2. Gather Inputs" → **Changeset mode** (the pure derivation only — repo-root resolution and diff construction). Do not run that section's item 2 (intent-gathering and handoff-deferral); this skill has no intent to gather and nothing to reconcile against a handoff. The granularity here is **hunk-adjacent** — only comments touching the changed hunks are in scope; a pre-existing comment elsewhere in a touched file is not, even though the file itself is part of the diff.

### 3. Run the Comment Auditor

Spawn the `jador:comment-auditor` subagent (Agent tool, `subagent_type: jador:comment-auditor`). Give it a task message containing only the resolved scope and its granularity — the file list (whole-file), or the diff plus the file paths so it can read context itself (hunk-adjacent).

The agent works **report-and-stop**: it returns its findings and ends its turn, delivering the result asynchronously via a completion notification. **Await that completion notification.** Do not treat the spawn/SendMessage return as the result, and do not proactively ping the agent for its findings — the completion arrives on its own.

The report you receive follows the **Report schema**: four sections in order — `Files touched`, `Deletions`, `MUST KILL`, `Skipped` — each heading carrying a count. Steps 4–8 below consume it section by section.

### 4. Auto-Delete the Clear-Cut Findings

For every `Deletions` entry labeled `clear-cut`: first check whether a `MUST KILL` entry lists it under `patches`. If so, skip it here — it's handled in step 6, which fixes the symbol before this comment is deleted. If not linked, remove the comment with no approval gate — this is what "clear-cut deletions are never gated behind a question" means in practice.

For each unlinked entry, use its `file` and verbatim comment text to locate the exact lines in the current file — the `line range` is a starting point, but earlier deletions in this same pass may have already shifted line numbers, so confirm by matching the verbatim text before editing. Delete the comment (and, if it was the only content on its line, the line itself).

Report the total count of clear-cut deletions applied (unlinked ones now; linked ones are counted when step 6 completes them).

### 5. Negotiate the Ambiguous Ones

**Non-interactive mode:** every `Deletions` entry labeled `ambiguous` is auto-kept — never deleted, never negotiated, regardless of `patches` linkage (there is nothing to defer, since nothing is being deleted). For each one, record an open item carrying its `file`, line location, the verbatim comment text, and the auditor's `reason` — these feed the step 8 `### Audit open items` block. No `AskUserQuestion` call is made. No encoding is offered or applied — encoding a constraint durably is itself a judgment call that needs a live user. The `jador:handoff update` path described below is interactive-only: in non-interactive mode the caller that invoked this skill owns durable recording of open items itself, and the step 8 report is the handoff — do not invoke it. Skip the rest of this step and go to step 6.

**Interactive mode** (`--non-interactive` not set): proceed as follows.

Before negotiating, check the same `patches` linkage as step 4: if a `Deletions` entry labeled `ambiguous` is linked to a `MUST KILL` entry, defer it — it's handled once step 6 lands (or, if step 6's fix is itself design-blocked, this entry is now also blocked; report it open rather than negotiating a deletion whose defect is still live).

For every unlinked `Deletions` entry labeled `ambiguous`, surface it to the user via AskUserQuestion — one question per entry (or batched sensibly if several are trivially related), each showing the verbatim comment text and its location.

An `ambiguous` entry is worth asking about because it's a genuine judgment call, typically one of two shapes:

- **A keep claim that is plausible but unverifiable** — the auditor couldn't confirm or refute it from history alone, and it's the user's call whether it still holds.
- **A constraint comment** ("do not remove", "talk to X first") where the real choice isn't keep-vs-delete but *how* to preserve the constraint: delete the comment outright, or encode the constraint more durably as a type, a lint rule, or a test.

For the second shape, always offer the cheapest in-scope encoding as one of the options — don't just offer a binary keep/delete.

On approval of an encoding: apply the encoding **first**, then delete the comment. On decline (or a plain "delete"): delete the comment and record the constraint as an open item in the report — do not silently drop it. Give it a durable home beyond this session's scrollback: if a handoff exists for this branch (`<root>/.claude/handoffs/<branch>.md`), invoke `jador:handoff update` (Skill tool) to append the declined constraint as a delta. If no handoff exists — the common case when this skill runs standalone — the step 8 report is the only record; make its entry specific enough (file, location, the constraint's text) that the user can act on it later without re-deriving it.

### 6. Fix the MUST KILL Symbols

This step runs identically in interactive and non-interactive mode — there is no mode branch here. That mode-independence is precisely why findings are applied *in this skill* rather than left for the worker prompt that invoked it: a worker prompt could tell the executor a symbol is broken, but only this step can fix it at root cause, verify the fix (step 7), and revert it on failure — all without a live user present.

For each `MUST KILL` entry, apply its reported `fix shape` (rename, extract, type, test, lint, or fix) at the smallest scope that addresses the root cause the `reason` describes — not just at the comment site.

If the entry carries a `patches` link, apply the fix **first**, then delete the linked comment (the `clear-cut` or `ambiguous`-but-deferred-from-step-5 entry) immediately after — this is the ordering finding 2 exists to enforce: never delete a suppression or a MUST KILL-patched comment before its underlying defect is fixed. Fold the linked comment into the deletion count from whichever step it originated in.

If a fix genuinely requires a design decision you can't make unilaterally (e.g. the rename is ambiguous between two reasonable names, or the extraction implies an API change), don't guess — leave it as an open item in the report instead, and leave its linked comment (if any) in place undeleted — a comment protecting a still-unfixed defect must not be removed.

### 7. Verify Code Changes

This step, like step 6, runs identically regardless of invocation mode — what needs verification depends only on whether step 6 touched code, not on how step 5's findings got resolved.

Comment-only edits (steps 4–5) don't change runtime or type behavior, so they need no verification of their own, in either mode — this includes step 4's clear-cut deletions and, in interactive mode, any ambiguous deletion or encoding step 5 applied. But step 6's `MUST KILL` fixes do touch actual code — renames, extractions, type changes, new tests, lint fixes, or direct bug fixes — so if step 6 applied at least one fix, run the project's lint, typecheck, and test commands on the touched files (check for existing scripts — e.g. `package.json` scripts, a `Makefile`, or a CI config — before guessing a command). If step 6 made no fixes, skip this step entirely.

If verification fails, the fix that touched the failing area is suspect: revert that specific fix (and restore any comment it had authorized deleting) and report it as an open item rather than leaving the tree broken. Do not revert fixes unrelated to the failure.

### 8. Report

Close with a report covering:

- **Deletion count** — clear-cut plus ambiguous-approved-for-deletion, total.
- **Encodings offered vs. applied** — for each ambiguous constraint entry, whether an encoding was offered and whether the user approved it.
- **`MUST KILL` fixes made** — symbol, location, and fix shape applied.
- **Verification result** — whether step 7 ran, and its outcome (skipped because no code fix landed, passed, or which fix was reverted after a failure).
- **Everything left open** — in interactive mode, declined-encoding constraints recorded per step 5; in non-interactive mode, every auto-kept `ambiguous` entry from step 5; in either mode, design-blocked `MUST KILL` entries from step 6, and any deletion left in place because its linked fix is still open.

Never report on or touch `Skipped` entries — they survived the audit and are out of scope for this skill entirely.

Always close the report with this exact trailing block, machine-consumable so a calling worker (e.g. a `/jador:execute` task worker) can lift it verbatim into its own `Issues` field:

```
### Audit open items (N)
- <file>:<line> — auto-kept (ambiguous): "<verbatim comment>" — <reason>
- <symbol> at <file>:<line> — MUST KILL unfixed (design-blocked): <reason>
```

`N` is the total line count that follows. The first line shape is for step 5's non-interactive auto-kept `ambiguous` entries; the second is for step 6's design-blocked `MUST KILL` entries, in either mode. Emit the header as `### Audit open items (0)` with no lines under it when there is nothing open — never omit the block — so the caller never has to distinguish "no open items" from "block missing".

## No Looping

If a re-reviewed or rejected finding fails a second pass — the user declines the same negotiation twice, a fix attempt doesn't resolve a `MUST KILL` entry on retry, or a verification failure recurs after one re-attempt of the fix — report it as an open item rather than re-prompting or re-attempting indefinitely.

In non-interactive mode there is no re-prompt and no second negotiation pass at all: since step 5 never negotiates in the first place, a finding that can't be applied cleanly on the first attempt (an unresolvable `MUST KILL` fix, a verification failure after one re-attempt) goes straight to an open item, same as above — there is simply no interactive retry loop to exhaust first.
