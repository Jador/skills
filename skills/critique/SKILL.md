---
name: critique
description: Adversarial design review of a plan, a changeset, or a bare idea, run in a dedicated skeptical subagent (jador:adversary). Plan mode stress-tests soundness, assumptions, and alternatives before execution; changeset mode reviews the diff for design quality, maintainability, and refactor opportunities at architecture altitude — explicitly NOT style, nits, or minor bugs; idea mode reviews an unwritten or draft idea for soundness, assumptions, and alternatives before it becomes a plan. Use before committing to a plan, before opening a PR, or while shaping an idea, to bring the work back to soundness.
argument-hint: [plan <slug> | changeset | idea <slug>]
disable-model-invocation: false
---

# Critique Skill

You are the entry point for an adversarial reviewer. You gather the right inputs, run them past the `jador:adversary` subagent — which reviews with deliberate, anchored skepticism in its own read-only context — and surface the findings. The adversary exists to counter the way a single agent drifts toward confident self-approval. It works at design altitude: the issues a staff engineer raises in architecture review, not the line-level nits and bugs that linters, type-checkers, `/code-review`, and `caveman:cavecrew-reviewer` already own.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.**
- **The critique is advisory, never a gate.** Surface findings; the user decides what to act on. Never block or auto-fix.
- **Keep the adversary independent.** Feed it the artifact and the stated intent — not the builder's rationale — until the explicit cross-check pass. That independence is what stops it rubber-stamping.

## Process

### 1. Determine Mode and Target

Parse `$ARGUMENTS`:

- **`plan <slug>`** — review `~/plans/<slug>.md` (and its source idea) for soundness before execution. If no slug is given, ask which plan.
- **`changeset`** — review the branch's changes in the current repo for design quality before a PR.
- **`idea <slug>`** — review a bare idea for soundness, assumptions, and alternatives before it becomes a plan. The idea document is passed inline (e.g. by `jador:discuss` handing over a draft) or read from `~/ideas/<slug>.md` when invoked standalone. If invoked standalone with no slug and no inline draft, ask which idea.

If the mode is ambiguous, ask.

### 2. Gather Inputs

**Plan mode:**
1. Read `~/plans/<slug>.md` and the idea file referenced in its frontmatter.
2. Intent = the idea; artifact = the plan.

**Changeset mode:**
1. Resolve the repo root (`git rev-parse --show-toplevel`).
2. Build the diff: `git diff <base>..HEAD` plus uncommitted changes, where `<base>` is the PR base branch if a PR exists, else the default branch.
3. Intent = the originating idea/plan (find via branch/slug if present); artifact = the diff. Do **not** read the keyed handoff at `<root>/.claude/handoffs/<branch>.md` yet — that is the cross-check pass. Derive `<branch>` using the **Derive the branch path** substep of `skills/handoff/SKILL.md`'s "Locate the Handoff File" (the pure derivation only — do not run that step's `mkdir`/exclude side effects).

**Idea mode:**
1. If the idea document was passed inline (from `jador:discuss`), use it directly — do not read from disk. Otherwise read `~/ideas/<slug>.md`.
2. Intent = the topic/goal the idea addresses; artifact = the idea document.

### 3. Run the Adversary — Cold Pass

Spawn the `jador:adversary` subagent (Agent tool, `subagent_type: jador:adversary`). Give it a task message containing: the **mode**, the **stated intent**, and the **artifact** (the plan text, the idea document, or the diff plus key file paths so it can read context itself). Do not include the builder's rationale.

The adversary works **report-and-stop**: after its cold pass it returns severity-ranked findings and ends its turn, delivering the result asynchronously via a completion notification. **Await that completion notification.** Do not treat the spawn/SendMessage return as the result, and do not proactively ping the adversary asking for its findings — the completion arrives on its own. Once it lands, proceed with the returned findings.

### 4. Changeset Cross-Check Pass

> Plan and idea modes skip this step — there is no handoff or diff to reconcile against.

Continue the *same* adversary subagent with SendMessage, now providing the keyed handoff at `<root>/.claude/handoffs/<branch>.md` if it exists (derive `<branch>` via the **Derive the branch path** substep of `skills/handoff/SKILL.md`, and apply its **Branch-aware legacy fallback** from "Read Mode — Load and Summarize"). Ask it to reconcile: for each cold finding, is it **defused** by the builder's stated rationale, or does it **stand**? Also surface any choice whose rationale doesn't hold up. If no handoff exists, say so and keep the cold findings.

### 5. Output the Findings

**Changeset mode:** render `assets/critique-template.md` from the reconciled findings and write it to `<root>/.claude/critiques/<branch>.md` (derive `<branch>` via the **Derive the branch path** substep of `skills/handoff/SKILL.md`). Ensure the parent directory exists first with `mkdir -p "<root>/.claude/critiques/$(dirname "<branch>")"` — the `mkdir -p` handles nested branch names (the `feat/` in `feat/foo`). Keep it out of git the way the handoff is — add the **directory** entry `.claude/critiques/` to the exclude file if absent. Resolve the exclude path with `git rev-parse --git-path info/exclude` — **not** `<root>/.git/info/exclude`, which breaks in worktrees where `.git` is a file. Use a guarded write so it's never duplicated:
```bash
excl="$(git rev-parse --git-path info/exclude)"
grep -qxF '.claude/critiques/' "$excl" || echo '.claude/critiques/' >> "$excl"
```
This doc feeds the handoff's "Open threads" — mention that to the user.

**Plan mode:** return the findings conversationally so they fold into the plan-revision loop. Don't write a file unless asked.

**Idea mode:** return the findings conversationally — never write a file. When invoked inline by `jador:discuss`, the findings fold into its draft-revision loop; when invoked standalone on a `~/ideas/<slug>.md`, they surface for the user to reshape the idea. This is advisory only.

### 6. Report

Present the severity-ranked findings (Critical first, then Worth-discussing). For changeset mode, confirm the path written. Remind the user the critique is advisory — they choose what to address.
