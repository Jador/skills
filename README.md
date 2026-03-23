# jador

Personal productivity skills for Claude Code.

## Skills

| Skill | Command | Description |
|-------|---------|-------------|
| **Discuss** | `/jador:discuss` | Flesh out an idea through structured Q&A, producing a polished idea document in `~/ideas/` |
| **Plan** | `/jador:plan` | Break down an idea into a detailed execution plan with parallel task groups |
| **Execute** | `/jador:execute` | Run a plan using parallel sub-agents with worktree isolation and auto-retry |
| **Babysit** | `/jador:babysit` | Monitor a PR for review comments and build failures, auto-fixing issues |
| **Notepad** | `/jador:notepad` | Quick scratch pad for capturing, listing, and managing ideas |
| **Skill Builder** | `/jador:skill-builder` | Scaffold new Claude Code skills through guided conversation |

## Workflow

The core workflow chains three skills together:

1. **Discuss** an idea to flesh it out → produces `~/ideas/<slug>.md`
2. **Plan** the idea into tasks → produces a plan with dependency graph
3. **Execute** the plan → parallel agents implement, verify, and commit each task

## Installation

```bash
claude plugin marketplace add Jador/skills
claude plugin install jador@skills
```

## Requirements

- **gh CLI** — required by babysit skill ([install](https://cli.github.com/))
- **bk CLI** — required by babysit skill for Buildkite integration
- **jq** — used by babysit cron agents for JSON parsing
