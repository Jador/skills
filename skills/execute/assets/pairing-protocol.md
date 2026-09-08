# Executor/Auditor Pairing Protocol

Pair every task worker 1:1 with a live `jador:comment-auditor` teammate for the duration of the task, and gate the task's commit on a comment audit requested at a pre-commit checkpoint. The orchestrator spawns both the worker and its auditor together, named, in the same batch — they are siblings, and either can message the other directly by name via `SendMessage`. This is the only pairing mechanism; there is no orchestrator-mediated fallback and no other spawn topology.

## Topology

Spawn exactly one auditor per worker, per task. Keep both alive concurrently for the entire duration of the task, from the worker's spawn until its results are collected.

Do not treat auditors as task agents. Do not collect an auditor's output as a task result. Do not give an auditor a worktree of its own. An auditor produces no branch and nothing to merge — it only reads and replies.

## Naming and Addressing

Compute each auditor's name before the batch, as `auditor-task-<N>` where `<N>` is the task number. For a retry, compute `auditor-task-<N>-retry<k>` where `<k>` is the retry attempt number.

Spawn the worker and its auditor together in the same `Agent` call batch, so the computed name can be interpolated into the worker's spawn prompt in that same response. Give the worker the auditor's literal name as a plain string; the worker addresses it with `SendMessage({to: "auditor-task-<N>", ...})` and nothing more elaborate — no discovery step, no `ListAgents` lookup.

## Path Resolution

Have the worker resolve its own paths — never the orchestrator. A worktree created by `isolation: "worktree"` does not exist until the spawn happens, so the orchestrator cannot know its path in advance and must not attempt to compute or pass one.

At checkpoint time, have the worker resolve its own absolute base:

```bash
git rev-parse --show-toplevel
```

Fall back to `pwd` if that command fails (outside a git repository). Send absolute paths built from this base in the checkpoint message. Because the worker resolves and sends its own absolute paths, the auditor needs no isolation of its own and no path of any kind in its spawn prompt.

## Checkpoint Message

Before committing, have the worker send its auditor exactly one checkpoint message containing:

- The task number and title.
- The task's `Files` list, as absolute paths resolved per the previous section.
- The granularity: `whole-file`.
- The standing instruction to run the standard comment audit over that scope and reply with the four-section report.

Include nothing else. In particular, never send a repo-wide diff and never widen the scope beyond the task's own `Files` list — in the no-overlap case, all wave workers share one workspace, and an unscoped or diff-based audit would pick up sibling workers' half-written, uncommitted comments as if they belonged to this task.

## Reply Contract

Treat the auditor's reply as the unmodified four-section `jador:comment-auditor` report: `Files touched`, `Deletions`, `MUST KILL`, `Skipped`. Do not have the worker reinterpret, summarize, or restate this report — pass it through as-is to whatever applies it (`prune-comments` in report-input mode).

## Stand-Down

Once the orchestrator has collected a worker's results, send that worker's auditor a one-line stand-down message if it is still waiting on a reply. Have the auditor end its turn on receiving it. Do not wait for any acknowledgment beyond the auditor ending its turn.

## Failure Handling

If the checkpoint request goes unanswered or errors, have the worker retry it exactly once. If the auditor is still unreachable or still errors after that retry, do not block the commit: have the worker proceed to commit and record `Comment audit: unavailable — <reason>` in the task's `Issues`. A messaging failure must never discard already-verified work.

## Retry Pairing

When a task is retried, spawn a fresh worker and a fresh auditor together, named `auditor-task-<N>-retry<k>` per the naming rule above. Stand down the previous auditor for that task (per the Stand-Down section) rather than reusing or resuming it — a retry is a new commit attempt by a new agent, and it gets a new audit from a new auditor.
