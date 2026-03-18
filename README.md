# hg-skills

Personal productivity skills for Claude Code.

## Skills

| Skill | Command | Description |
|-------|---------|-------------|
| **Discuss** | `/hg:discuss` | Flesh out an idea through structured Q&A, producing a polished idea document in `~/ideas/` |
| **Plan** | `/hg:plan` | Break down an idea into a detailed execution plan with parallel task groups |
| **Execute** | `/hg:execute` | Run a plan using parallel sub-agents with worktree isolation and auto-retry |
| **Babysit** | `/hg:babysit` | Monitor a PR for review comments and build failures, auto-fixing issues |
| **Notepad** | `/hg:notepad` | Quick scratch pad for capturing, listing, and managing ideas |
| **Skill Builder** | `/hg:skill-builder` | Scaffold new Claude Code skills through guided conversation |

## Workflow

The core workflow chains three skills together:

1. **Discuss** an idea to flesh it out → produces `~/ideas/<slug>.md`
2. **Plan** the idea into tasks → produces a plan with dependency graph
3. **Execute** the plan → parallel agents implement, verify, and commit each task

## Installation

Add this plugin to your Claude Code configuration:

```bash
claude plugin add /path/to/skills
```

Or add it via settings:

```json
{
  "plugins": ["/path/to/skills"]
}
```

## Requirements

- **gh CLI** — required by babysit skill ([install](https://cli.github.com/))
- **bk CLI** — required by babysit skill for Buildkite integration
- **jq** — used by babysit cron agents for JSON parsing
