# Concurrency model — design decision record

> Reference doc. Not loaded into the session context (SKILL.md only carries
> the terse operational rules). Read this when reconsidering how parallel
> workers share the git worktree.

## Decision

Dispatch workers **in parallel** (one sub-agent per event), but make the
shared git worktree safe by:

1. Running analysis, file edits, and verification **unlocked, in parallel** —
   that is where the time goes, and distinct review threads almost always
   touch distinct files.
2. **Serializing + scoping each commit** with a single atomic command:
   ```
   flock "$(git rev-parse --git-dir)/babysit-commit.lock" -c \
     'git add -- <files> && git commit -- <files> -m "…"'
   ```
   `flock` serializes commits so they never race on `.git/index.lock`
   (the command is short-lived, which fits flock's fd/process-lifetime
   model); the `-- <files>` pathspec means a worker commits only its own
   changes even though the index is shared. This is why `flock` is a
   prerequisite (`brew install flock`).
3. A **single session-owned push** after the batch drains. Workers never
   push — that would race — so the session pushes once for the whole batch.
   AGREE comment workers also defer their confirmation reply: they return a
   `pending_reply` and the session posts it only after the push succeeds, so
   a `Fixed in <sha>` reference never points at an unpushed (or never-pushed,
   on push failure) commit. The commit SHA is captured *inside* the flock'd
   commit command, because a `git rev-parse` after the lock releases could
   read a sibling worker's commit as HEAD. We deliberately do **not**
   `git pull --rebase` before the push: in this single-user, one-poller-per-PR
   model the remote rarely advances mid-session, and an unattended rebase can
   leave a conflicted worktree — a plain push with a "resolve manually"
   fallback is safer than auto-rebasing blind. (v1.10.3 pushed + rebased
   per-worker; that was both racy and rebase-unsafe under parallel workers.)

## Why not the alternatives

- **Serialize dispatch entirely.** Simplest and lock-free, but a burst of
  5+ comments (common) would then be handled strictly one at a time,
  including the expensive analyze/edit/test phase. Rejected: throws away
  the parallelism that is the point of the model.
- **Worktree per worker.** True isolation of edits *and* test runs, but the
  PR branch can't be checked out in multiple worktrees, so it needs
  detached-HEAD temp worktrees + cherry-pick back onto the branch +
  conflict handling + lifecycle teardown. Judged disproportionate to the
  residual risk below.
- **Claude Code Agent Teams** (https://code.claude.com/docs/en/agent-teams).
  Evaluated and declined: more coordination machinery than this workload
  warrants.
- **`flock` around the whole edit→test→commit span.** Doesn't fit: that
  span is many separate tool calls with no single owning process, and
  `flock` is tied to an fd / process lifetime. Only the single-command
  commit can be flocked.

## Accepted residual risk

Two workers editing the **same file**, or one worker's test run observing
another's mid-edit, in the shared worktree. For distinct review threads
this is rare (different threads → different code). Not mitigated by the
current design.

## Revisit this decision if

- Parallel workers frequently land on the same file.
- Test suites interfere when run concurrently in one tree.
- Commit-lock contention becomes a visible bottleneck.

At that point worktree-per-worker isolation (or Agent Teams) earns its
complexity.
