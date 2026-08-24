# Task 1 findings: `wt remove --no-delete-branch` safety semantics (spike)

**wt version tested:** `wt v0.74.0` (installed and on PATH; confirmed via `wt --version` before starting)

**Method:** Built a scratch local repo (`main-repo`) cloned from a local bare "origin"
(`origin.git`), all outside this repo, under a scratchpad tmp path (not committed, not part of
this repo). Created three sibling worktrees with `wt switch --create <branch>`, one per test
case, then ran `wt remove --no-delete-branch --foreground <branch>` against each (using
`--foreground` per the plan's note that removal is async by default and `--foreground` makes it
deterministic for sequential testing/scripting). Captured exit code, stdout, stderr, and
post-condition (`git worktree list`, `git branch --list`) for each.

## Bottom line (answers the Task 7 / Task 12 question)

**`--no-delete-branch` is sufficient protection against branch deletion.** Across every case
tested — dirty, merged, unmerged, forced-dirty-removal — the branch was retained whenever
`--no-delete-branch` was passed, with no exceptions observed. The disputed anecdote ("a dirty
worktree was silently removed with its branch deleted") did **not** reproduce on v0.74.0. The
`--help` text's claim is accurate: **removal of a dirty worktree fails outright (exit 1, nothing
removed) unless `-f`/`--force` is explicitly also passed.**

**However, dirtiness protection and branch-deletion protection are two independent flags:**
- `-f`/`--force` protects against losing **uncommitted worktree changes** (default: refuse).
- `--no-delete-branch` protects against losing the **branch ref** (default: delete if merged).

These do not interact — passing `--no-delete-branch` alone does not make `wt remove` proceed on a
dirty worktree, and passing `-f` alone (without `--no-delete-branch`) will still delete the
branch if it's merged. **Recommendation for the script (Tasks 7/12): always pass
`--no-delete-branch` on every `wt remove` invocation (belt) AND never pass `-f`/`--force` —
instead pre-check dirtiness yourself (via `git status --porcelain` or the dirty flag already
surfaced by `wt list --format=json`) and skip/flag dirty worktrees before ever invoking `wt
remove` on them (suspenders). This gives the script two independent, non-overlapping safety
nets instead of relying on wt's default refusal alone**, and avoids a confusing failure path
(exit 1 mid-batch) when the script could have just excluded the dirty entry from the plan up
front.**

---

## Case A: dirty worktree (uncommitted changes), unmerged branch, no `-f`

**Setup:** `case-a-dirty` branch created from `main`; added one untracked file
(`dirty-file.txt`) inside the worktree, no commit. Branch has no commits beyond `main` (still
unmerged/no-op in wt's merge-check sense, but irrelevant here since removal never got that far).

**Command:** `wt remove --no-delete-branch --foreground case-a-dirty` (repo-level `-y` also set)

**Exit code:** `1`

**stdout:** *(empty)*

**stderr:**
```
✗ Cannot remove worktree: case-a-dirty has uncommitted changes
  ?? dirty-file.txt
↳ Commit or stash changes first, or to lose uncommitted changes, run wt remove --force case-a-dirty
```

**Post-condition:**
- `git worktree list`: `case-a-dirty` worktree **still present**, unchanged, at
  `.../main-repo.case-a-dirty` @ `3a787c3`.
- `git branch --list`: `case-a-dirty` **still present** (still shows `+`, i.e. still checked out).

**Conclusion:** Removal is refused outright. Nothing is deleted — neither the worktree nor the
branch. This directly disputes the anecdote as stated (at least for this version): a dirty
worktree is *not* silently removed by default.

---

## Case B: worktree whose branch is merged (clean, no `-f`)

**Setup:** `case-b-merged` branch created from `main`; committed one file
(`feature-b.txt`) on the branch; then fast-forward merged `case-b-merged` into `main` in the
main worktree (`git merge case-b-merged`), so branch HEAD == `main` HEAD (`f2f8b9e`). Worktree
itself is clean (no uncommitted changes).

**Command:** `wt remove --no-delete-branch --foreground case-b-merged`

**Exit code:** `0`

**stdout:** *(empty)*

**stderr:**
```
◎ Removing case-b-merged worktree...
✓ Removed case-b-merged worktree (2 files · 193 B)
↳ Branch integrated (same commit as main, _); retained with --no-delete-branch
```

**Post-condition:**
- `git worktree list`: `case-b-merged` worktree **gone** (directory removed, no longer listed).
- `git branch --list`: `case-b-merged` **still present**, now shown without the `+` prefix
  (no longer checked out anywhere) — i.e. it survives as a plain local branch pointing at
  `f2f8b9e`.

**Conclusion:** Worktree removed; branch explicitly retained and the tool tells you it would
otherwise have deleted it ("Branch integrated ... retained with --no-delete-branch"). Confirms
`--no-delete-branch` overrides the "delete if merged" default.

---

## Case C: worktree with untracked-but-ignored files, unmerged branch (clean per git, no `-f`)

**Setup:** `case-c-ignored` branch created from `main`; committed a `.gitignore` containing
`ignored.log`; then created an untracked file `ignored.log` matching that ignore pattern (so
`git status --short` shows nothing, `git status --short --ignored` shows `!! ignored.log`).
Branch has one commit ahead of `main` that `main` doesn't have (unmerged — `wt list` shows `↕`
ahead/behind, not `_`/`⊂`).

**Command:** `wt remove --no-delete-branch --foreground case-c-ignored`

**Exit code:** `0`

**stdout:** *(empty)*

**stderr:**
```
◎ Removing case-c-ignored worktree...
✓ Removed case-c-ignored worktree (3 files · 203 B)
```

**Post-condition:**
- `git worktree list`: `case-c-ignored` worktree **gone**.
- `git branch --list`: `case-c-ignored` **still present** (no longer checked out; `+` prefix
  gone). No "Branch integrated" message was printed for this case, since the branch was unmerged
  and not eligible for auto-deletion anyway — `--no-delete-branch` was a no-op safety net here,
  but branch was never at risk either way (confirms `-D`/force-delete would be needed to delete
  an unmerged branch, and `--no-delete-branch` doesn't need to "do" anything when there's no
  deletion pending).

**Conclusion:** Ignored files do **not** count as "uncommitted changes" — `wt remove` treats the
worktree as clean and proceeds without needing `-f`. Branch survives regardless (it wasn't
merged, so it wouldn't have been auto-deleted even without `--no-delete-branch`, and `-D` was not
passed).

---

## Bonus case: forced removal of the still-present dirty worktree (`case-a-dirty`), with `--no-delete-branch`

Run after Case A's refusal, to directly test whether forcing past dirtiness ever also destroys
the branch when `--no-delete-branch` is present (this is the exact combination that would
validate or kill the disputed anecdote).

**Command:** `wt remove --no-delete-branch --force --foreground case-a-dirty`

**Exit code:** `0`

**stdout:** *(empty)*

**stderr:**
```
◎ Removing case-a-dirty worktree...
✓ Removed case-a-dirty worktree (--force) (2 files · 193 B)
↳ Branch integrated (ancestor of main, ⊂); retained with --no-delete-branch
```

**Post-condition:**
- `git worktree list`: `case-a-dirty` worktree **gone** (including the uncommitted
  `dirty-file.txt`, which is lost — expected, that's what `-f` means).
- `git branch --list`: `case-a-dirty` **still present** (retained via `--no-delete-branch`,
  explicitly confirmed in the tool's own output).

**Conclusion:** Even when `-f` is used to force past a dirty-worktree refusal, `--no-delete-branch`
still fully protects the branch. The branch is never deleted as a side effect of `-f`; it is only
ever deleted when merged **and** `--no-delete-branch` is absent. No scenario was found in which
`--no-delete-branch` fails to protect the branch.

---

## Explicit answer for Tasks 7 and 12

1. **Is `--no-delete-branch` sufficient protection against destroying a branch?** Yes, in every
   case tested (dirty, merged, unmerged, forced) the branch survived whenever
   `--no-delete-branch` was passed. No counter-example was found; the anecdote did not reproduce
   on v0.74.0.
2. **Does the script still need to pre-check dirtiness itself before calling `wt remove`?**
   Recommended yes, as defense in depth — not because `--no-delete-branch` is unreliable, but
   because:
   - `wt remove` without `-f` refuses outright on a dirty worktree (exit 1); a batch script
     driving multiple removals should pre-filter these out of the plan rather than relying on
     per-call failure handling, since a stale plan entry could have gone dirty since caching.
   - The script (per plan assumptions) never passes `-f`/`--force` — so a dirty worktree is
     never at risk of being force-removed by this tool; it will just be skipped/flagged instead.
   - This matches the plan's existing note that per-entry sha/dirty drift must be enforced at
     apply time regardless of plan-cache staleness.
3. **Always pass `--no-delete-branch` on every `wt remove` call** the script makes, regardless of
   category (merged/unmerged/duplicate/ancestor) — it is the single flag responsible for branch
   preservation and was 100% reliable across all tested scenarios.
