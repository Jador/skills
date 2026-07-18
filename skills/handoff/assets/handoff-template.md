---
branch: <branch name>
pr: <PR number/url, or "none yet">
plan_ref: <path to plan file, or "none">
status: <in-progress | ready-for-review | handed-off>
last_updated: <ISO timestamp>
---

# Agent Handoff

> Ephemeral digest of what actually happened. The plan/spec remains the source of truth — this records only the delta. Read it, then proceed; it is safe to discard.

## What shipped

<One or two lines: the outcome, plus the PR/branch anchor.>

## Deviations from plan

<Where reality diverged from the plan and why. "None — followed plan as specified" if true.>

## Decisions made

<Decisions with reasoning and rejected alternatives, e.g. "options were X/Y, chose X because Z." So the next agent does not reverse them under reviewer pushback.>

## Gotchas / what to avoid

<Failed approaches, sharp edges, and anything that looks wrong but is intentional. Stops the next agent from "fixing" deliberate choices.>

## Open threads

<Deferred TODOs, known-incomplete work, and likely review-comment magnets. Includes unresolved items from .claude/critiques/<branch>.md if present.>

## Verification state

<Build/test/lint status at handoff. Which CI is expected to pass or fail, and why.>

## Key files touched

<path → what changed and why. Pointers, not diffs.>

## Anticipated review feedback

<Likely review comments, each with the intended fix or the defense for the current choice.>

## Changes Since Handoff (babysit)

<Additive delta log, appended by `handoff update` (not written at synthesize time). Each bullet ties a later change back to the decision it superseded or the open thread it resolved, anchored to a commit SHA — e.g. `Decision "use polling" → superseded by push-based fix in a1b2c3d (review thread #4)`. Never rewrites the sections above; omit or leave empty if nothing has superseded the original digest.>
