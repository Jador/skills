# Skills Improvement Tracker

Decisions and planned changes from review session (2026-04-01).

## Bugs

### #21 — Plan skill ignores slug argument
**Skill:** plan
**Status:** Decided
**Changes:**
- Rewrite Step 0 routing logic with three paths:
  1. `list` — show existing plans (explicit opt-in only)
  2. Slug provided — load that idea and plan it
  3. Empty/no args — scan `~/ideas/`, filter out ideas that already have a matching plan, present unplanned ideas for user to pick
- Put the slug path first so Claude defaults to planning, not listing
- Make the empty/list check explicit to avoid Claude misinterpreting bare slugs
- Do NOT show partial/failed plans in the no-args path; re-planning is a future skill

### #22 — Discuss skill overwrites existing ideas
**Skill:** discuss
**Status:** Decided
**Changes:**
- Add to Step 4: never modify or overwrite existing files in `~/ideas/` without user approval
- If same slug + date would collide, append a suffix (`-2`, `-3`)
- If a similar topic exists under a different date, suggest combining but wait for user approval before acting
- Always default to creating a new file

### #14 — gh commands fail with multiple GitHub accounts
**Skill:** all skills using `gh` (execute, babysit, mq)
**Status:** Decided
**Changes:**
- Add a plugin-level PreToolUse hook (`hooks/hooks.json` referenced from `plugin.json`)
- Hook fires on Bash tool calls, parses `tool_input.command` from stdin
- Early exit if command doesn't involve `gh`
- If `gh` detected: compare repo org (from `git remote get-url origin`) against active `gh auth status`
- If mismatched: run `gh auth switch --user <correct-user>` before allowing the command
- This solves it once for all skills and any future `gh` usage, no per-skill changes needed

### #25 — setup-worktree.sh fails outside ~/projects/handshake
**Status:** Skipped — personal dev script, not a plugin concern

## Improvements

### #26 — Verification before committing (babysit + execute/plan)
**Skills:** babysit, plan
**Status:** Decided
**Changes:**
- **Babysit (comment-check-prompt.md & build-check-prompt.md):** Add a step between making changes and committing: "run the project's test/lint/typecheck commands for the affected files before committing." No hardcoded commands — let the agent figure out what to run based on the project.
- **Plan skill:** Tighten verification language — change "where applicable" to require every code-modifying task to include a verification step. The execute skill itself doesn't need changes; better plans will produce better execution.

### #24 — Remove hardcoded terminal-notifier from mq
**Skill:** mq
**Status:** Decided
**Changes:**
- Replace all three `terminal-notifier` references (description, prerequisite check, queue-check-prompt.md command) with generic "notify the user" instructions
- Remove `which terminal-notifier` prerequisite check — don't gate the skill on a specific notification tool
- Update README to remove terminal-notifier as a requirement
- Drop `[mq]` prefix from log/output messages — unnecessary noise

### #23 — Move plan output to ~/plans/
**Skill:** plan
**Status:** Decided
**Changes:**
- Change write path in plan SKILL.md from `${CLAUDE_PLUGIN_DATA}/plans/` to `~/plans/`
- Update all skills that read plans (execute, backlog) to use `~/plans/`
- When implementing: backup existing plans from `${CLAUDE_PLUGIN_DATA}/plans/`, then copy to `~/plans/`

### #11 — Skill-builder should auto-detect plugin repos
**Skill:** skill-builder
**Status:** Decided
**Changes:**
- In Step 3, before asking about scope: check if the current working directory (or parent) contains `.claude-plugin/plugin.json`
- If detected, default to repo scope with the current directory as the target
- Confirm with the user rather than silently assuming ("Looks like you're in a plugin repo — want to add a skill here?")
- Skip Step 3b (target repo selection) when auto-detected — no need to pick from history or enter a path

## New observations

### Backlog should surface notepad entries
**Skill:** backlog
**Status:** Decided
**Changes:**
- Add a third section: "Notes without ideas" — scan ~/notes.md, filter out entries that already have a matching idea in ~/ideas/
- Show all notes regardless of project

### Babysit + MQ shared build-retry logic
**Skills:** babysit, mq
**Status:** Skipped — no pain yet, skills will likely diverge as mq gets tweaked. Revisit if same edit keeps being applied to both.

### Execute retries should re-dispatch to sub-agents, not inline
**Skill:** execute
**Status:** Decided
**Changes:**
- Add explicit instruction to execute skill: when a task needs to be retried (sub-agent failure, retasking, connectivity loss), always spawn a new sub-agent. Never execute task work directly in the parent.
- This preserves the parallelism and worktree isolation that the execute skill is designed around

### Discuss slug dedup
**Skill:** discuss
**Status:** Covered by #22 — the suffix appending (`-2`, `-3`) on collision handles this case

### Skill-builder should hand off to skill-creator for testing
**Skill:** skill-builder
**Status:** Decided
**Changes:**
- After skill-builder finishes writing files (Step 8), suggest running skill-creator for testing and iteration
- skill-builder owns scaffolding (scope detection, convention inference, doc updates); skill-creator owns validation (test cases, benchmarks, description optimization)
- Don't merge them — they're separate plugins with complementary strengths

## Completed (remove from ~/notes.md)

- #12 — Merge queue monitor (mq skill exists)
- #15 — Babysit immediate checks (shipped in 3db76ea)
- #16 — Babysit preserve state across restarts (shipped in 93b5a2d)
