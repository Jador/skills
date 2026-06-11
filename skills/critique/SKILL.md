---
name: critique
description: Adversarial design review of a plan or a changeset, run in a dedicated skeptical subagent (jador:adversary). Plan mode stress-tests soundness, assumptions, and alternatives before execution; changeset mode reviews the diff for design quality, maintainability, and refactor opportunities at architecture altitude — explicitly NOT style, nits, or minor bugs. Use before committing to a plan, or before opening a PR, to bring an implementation back to soundness.
argument-hint: [plan <slug> | changeset]
disable-model-invocation: true
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

If the mode is ambiguous, ask.

### 2. Gather Inputs

**Plan mode:**
1. Read `~/plans/<slug>.md` and the idea file referenced in its frontmatter.
2. Intent = the idea; artifact = the plan.

**Changeset mode:**
1. Resolve the repo root (`git rev-parse --show-toplevel`).
2. Build the diff: `git diff <base>..HEAD` plus uncommitted changes, where `<base>` is the PR base branch if a PR exists, else the default branch.
3. Intent = the originating idea/plan (find via branch/slug if present); artifact = the diff. Do **not** read `.claude/agent-handoff.md` yet — that is the cross-check pass.

### 3. Run the Adversary — Cold Pass

Spawn the `jador:adversary` subagent (Agent tool, `subagent_type: jador:adversary`). Give it a task message containing: the **mode**, the **stated intent**, and the **artifact** (the plan text, or the diff plus key file paths so it can read context itself). Do not include the builder's rationale. The adversary returns severity-ranked findings per its own format.

### 4. Changeset Cross-Check Pass

> Plan mode skips this step.

Continue the *same* adversary subagent with SendMessage, now providing `<root>/.claude/agent-handoff.md` if it exists. Ask it to reconcile: for each cold finding, is it **defused** by the builder's stated rationale, or does it **stand**? Also surface any choice whose rationale doesn't hold up. If no handoff exists, say so and keep the cold findings.

### 5. Output the Findings

**Changeset mode:** render `assets/critique-template.md` from the reconciled findings and write it to `<root>/.claude/critique.md`. Keep it out of git the way the handoff is — if `.claude/critique.md` is not already in `<root>/.git/info/exclude`, append it (guarded so it's never duplicated):
```bash
grep -qxF '.claude/critique.md' "$(git rev-parse --show-toplevel)/.git/info/exclude" \
  || echo '.claude/critique.md' >> "$(git rev-parse --show-toplevel)/.git/info/exclude"
```
This doc feeds the handoff's "Open threads" — mention that to the user.

**Plan mode:** return the findings conversationally so they fold into the plan-revision loop. Don't write a file unless asked.

### 6. Report

Present the severity-ranked findings (Critical first, then Worth-discussing). For changeset mode, confirm the path written. Remind the user the critique is advisory — they choose what to address.
