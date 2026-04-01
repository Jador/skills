# jador

Personal productivity skills for Claude Code.

## Skills

| Skill | Command | Description |
|-------|---------|-------------|
| **Discuss** | `/jador:discuss` | Flesh out an idea through structured Q&A, producing a polished idea document in `~/ideas/` |
| **Plan** | `/jador:plan` | Break down an idea into a detailed execution plan with parallel task groups. Plans live in `~/plans/` |
| **Execute** | `/jador:execute` | Run a plan using parallel sub-agents with worktree isolation and auto-retry |
| **Babysit** | `/jador:babysit` | Monitor a PR for review comments and build failures, auto-fixing issues |
| **MQ** | `/jador:mq` | Monitor merge queue and auto-retry failed Buildkite jobs (checks every 2 min) |
| **Backlog** | `/jador:backlog` | Surface idle work — notes without ideas, ideas without plans, plans without execution |
| **Notepad** | `/jador:notepad` | Quick scratch pad for capturing, listing, and managing ideas |
| **Skill Builder** | `/jador:skill-builder` | Scaffold new Claude Code skills through guided conversation |

## Workflow

The core workflow chains skills together:

1. **Notepad** — capture a quick thought → `~/notes.md`
2. **Discuss** a note or topic to flesh it out → `~/ideas/<slug>.md`
3. **Plan** the idea into tasks → `~/plans/<slug>.md`
4. **Execute** the plan → parallel agents implement, verify, and commit each task

Use **Backlog** to see what's idle at any stage and route into the next step.

## Installation

```bash
claude plugin marketplace add Jador/skills
claude plugin install jador@skills
```

## Requirements

- **gh CLI** — required by babysit and mq skills ([install](https://cli.github.com/))
- **bk CLI** — required by babysit and mq skills for Buildkite integration
- **jq** — used by babysit and mq cron agents for JSON parsing

