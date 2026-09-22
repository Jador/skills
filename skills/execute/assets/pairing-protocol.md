# Executor/Auditor Pairing Protocol

Pair every task worker 1:1 with a live `jador:comment-auditor` teammate for the duration of the task, and gate the task's commit on a comment audit requested at a pre-commit checkpoint. The orchestrator spawns both the worker and its auditor together, named, in the same batch — they are siblings, and either can message the other directly by name via `SendMessage`. This is the only pairing mechanism; there is no orchestrator-mediated fallback and no other spawn topology.

## Topology

Spawn exactly one auditor per worker, per task. Keep both alive concurrently for the entire duration of the task, from the worker's spawn until its results are collected.

Do not treat auditors as task agents. Do not collect an auditor's output as a task result. Do not give an auditor a worktree of its own. An auditor produces no branch and nothing to merge — it only reads and replies.

## Naming and Addressing

Compute each worker's name before the batch, as `worker-task-<N>` where `<N>` is the task number, and each auditor's name as `auditor-task-<N>`. For a retry, compute `worker-task-<N>-retry<k>` and `auditor-task-<N>-retry<k>` where `<k>` is the retry attempt number. Set both names with the `Agent` tool's `name` parameter at spawn time.

Spawn the worker and its auditor together in the same `Agent` call batch, so each computed name can be interpolated into the other's spawn prompt in that same response. Give the auditor the worker's literal name as `{{WORKER_NAME}}`, and give the worker the auditor's literal name as `{{AUDITOR_NAME}}`. Each addresses the other with a literal-name `SendMessage`: the worker sends `SendMessage({to: "{{AUDITOR_NAME}}", ...})`, and the auditor replies with `SendMessage({to: "{{WORKER_NAME}}", ...})`. Nothing more elaborate than that — no discovery step, no `ListAgents` lookup.

`SendMessage` is a deferred tool. Both the worker and the auditor load it with `ToolSearch("select:SendMessage")` before their first send.

## Turn Model

Agents receive messages only between turns, never mid-turn. After each `SendMessage` send, and after spawn for the auditor, the sending agent ends its turn: use the exact sentence "end your turn now; the next message resumes you". The next message — a reply, a checkpoint, or a stand-down — resumes the agent from where it left off.

Polling, `sleep`, and re-reading files are forbidden as ways to wait for a message. There is no other way to observe a reply arriving; an agent that tries to wait within a turn will simply never see one.

There is no spawn-time ack. On being spawned, the auditor does not acknowledge the task, audit anything, or speculate about scope — it ends its turn immediately and waits to be resumed.

After sending its checkpoint, the worker ends its turn with the exact final line `Awaiting audit from {{AUDITOR_NAME}}.` (for example `Awaiting audit from auditor-task-<N>.`). A worker whose turn ends with that line is waiting for its audit, not done — the orchestrator collects only the worker's later step-6 `**Status:**` report, not this line.

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

Treat the auditor's reply as the unmodified four-section `jador:comment-auditor` report: `Files touched`, `Deletions`, `MUST KILL`, `Skipped`. The auditor delivers this report to `{{WORKER_NAME}}` with `SendMessage`, not as plain output — plain output goes to the orchestrator, not the worker. Do not have the worker reinterpret, summarize, or restate this report — pass it through as-is to whatever applies it (`prune-comments` in report-input mode).

## Stand-Down

Once the orchestrator has collected a wave's worker results, send every paired auditor in that wave a one-line stand-down message. Under the end-turn model every auditor is idle between turns, waiting to be resumed; the stand-down resumes it. Have the auditor end its turn on receiving it, with no output. Do not wait for any acknowledgment beyond the auditor ending its turn.

## Failure Handling

If `ToolSearch` fails to load `SendMessage`, or a `SendMessage` send returns an error, have the worker retry it exactly once. If it still fails, do not block the commit: have the worker proceed to commit and record `Comment audit: unavailable — <reason>` in the task's `Issues`. A messaging failure must never discard already-verified work.

## Retry Pairing

When a task is retried, spawn a fresh worker and a fresh auditor together, named `worker-task-<N>-retry<k>` and `auditor-task-<N>-retry<k>` per the naming rule above. Stand down the previous auditor for that task (per the Stand-Down section) rather than reusing or resuming it — a retry is a new commit attempt by a new agent, and it gets a new audit from a new auditor.
