"""Tests for worktree-cleanup.sh's no-PR empty/duplicate-sha detection
(Task 8).

Behavioural contract under test (see "Categorization ladder" ->
wtc_compute_no_pr_categories / wtc_categorize_no_pr in
worktree-cleanup.sh):

1. A no-PR branch whose HEAD sha is an ancestor of (or equal to) the
   default branch's tip -- zero unique commits -- is categorized "empty".
2. Among no-PR branches that are NOT ancestors of the default branch,
   branches sharing the exact same HEAD sha are grouped: the one whose
   worktree directory has the greatest mtime ("most-recently-touched") is
   "needs_review"; every other branch in that group is "duplicate".
3. A no-PR branch with a unique sha (not shared with any other no-PR
   branch, and not an ancestor of default) is a "singleton" and is
   "needs_review" -- covered by
   test_categorize.py::test_no_pr_singleton_is_needs_review, since it's
   really just a size-1 case of the same ladder extension point exercised
   here; not duplicated in this file.

All scenarios here use one real shared git repository with real
`git worktree add` worktrees (rather than several unrelated repos), same
as a real fanned-out-agent-run duplicate would look like on disk: several
branches/worktrees backed by one shared object database, some pointing at
the exact same commit. Directory mtimes are pinned explicitly via
`os.utime` after worktree creation so the "most-recently-touched" pick is
deterministic rather than depending on real-time ordering/filesystem
timestamp granularity.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


def _run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_main_repo(path: Path) -> str:
    """Inits a repo with branch "main" and one commit; returns its sha."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return _run_git(path, "rev-parse", "HEAD")


def _commit_new_branch_from(repo: Path, branch: str, from_ref: str, message: str) -> str:
    """Creates `branch` off `from_ref`, adds one empty commit on it, then
    returns to `from_ref` (leaving the repo's checked-out branch
    unchanged) and returns the new commit's sha.
    """
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", branch, from_ref], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message], check=True)
    sha = _run_git(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", from_ref], check=True)
    return sha


def _branch_at(repo: Path, branch: str, sha: str) -> None:
    subprocess.run(["git", "-C", str(repo), "branch", branch, sha], check=True)


def _worktree_add(repo: Path, wt_path: Path, branch: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(wt_path), branch], check=True
    )


def _set_mtime(path: Path, epoch: float) -> None:
    os.utime(path, (epoch, epoch))


def _wt_entry(branch: str, path: Path, sha: str, dirty: bool = False) -> dict:
    """Builds one schema-1 `wt list --format=json` entry."""
    return {
        "branch": branch,
        "path": str(path),
        "kind": "worktree",
        "commit": {"sha": sha, "short_sha": sha[:7], "message": "msg", "timestamp": 0},
        "is_main": False,
        "is_current": False,
        "working_tree": {
            "staged": False,
            "modified": False,
            "untracked": dirty,
            "renamed": False,
            "deleted": False,
        },
    }


def _gh_rules_no_pr(slug: str) -> list[dict]:
    return [
        {
            "argv_prefix": ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            "stdout": f"{slug}\n",
        },
        {"argv_prefix": ["pr", "list"], "stdout": "[]"},
    ]


def _run_categorize(run_script, fake_bins, entries: list[dict]) -> dict[str, dict]:
    fake_bins.set_responses(
        "wt",
        [{"argv_prefix": ["list", "--format=json"], "stdout": json.dumps(entries)}],
    )

    result = run_script(["--debug-categorize"], env=fake_bins.env)

    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    return {entry["branch"]: entry for entry in parsed}


def test_no_pr_ancestor_of_default_is_empty(tmp_path, run_script, fake_bins):
    repo = tmp_path / "repo"
    c0 = _init_main_repo(repo)

    # feature-empty has zero commits beyond main's tip -- its HEAD sha is
    # literally main's tip, hence trivially an ancestor of it.
    _branch_at(repo, "feature-empty", c0)
    wt_path = tmp_path / "wt-empty"
    _worktree_add(repo, wt_path, "feature-empty")

    fake_bins.set_responses("gh", _gh_rules_no_pr("example/repo"))

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature-empty", wt_path, c0)]
    )

    assert by_branch["feature-empty"]["category"] == "empty"


def test_no_pr_duplicate_sha_group_keeps_most_recently_touched(
    tmp_path, run_script, fake_bins
):
    repo = tmp_path / "repo"
    _init_main_repo(repo)

    # A commit that diverges from main (not an ancestor of it) shared by
    # three separate branches/worktrees -- the fanned-out-agent-run
    # pattern this detection targets.
    shared_sha = _commit_new_branch_from(repo, "shared-base", "main", "shared work")
    _branch_at(repo, "fanout-1", shared_sha)
    _branch_at(repo, "fanout-2", shared_sha)
    _branch_at(repo, "fanout-3", shared_sha)

    wt1, wt2, wt3 = (tmp_path / f"wt-{i}" for i in (1, 2, 3))
    _worktree_add(repo, wt1, "fanout-1")
    _worktree_add(repo, wt2, "fanout-2")
    _worktree_add(repo, wt3, "fanout-3")

    # Pin mtimes explicitly (not relying on real creation-order timing):
    # fanout-2 is the most-recently-touched of the three.
    now = time.time()
    _set_mtime(wt1, now - 300)
    _set_mtime(wt3, now - 200)
    _set_mtime(wt2, now - 100)

    fake_bins.set_responses("gh", _gh_rules_no_pr("example/repo"))

    by_branch = _run_categorize(
        run_script,
        fake_bins,
        [
            _wt_entry("fanout-1", wt1, shared_sha),
            _wt_entry("fanout-2", wt2, shared_sha),
            _wt_entry("fanout-3", wt3, shared_sha),
        ],
    )

    assert by_branch["fanout-2"]["category"] == "needs_review"
    kept_reason = by_branch["fanout-2"].get("reason", "")
    assert "3 branches" in kept_reason
    assert "fanout-1" in kept_reason and "fanout-3" in kept_reason

    for dup_branch in ("fanout-1", "fanout-3"):
        assert by_branch[dup_branch]["category"] == "duplicate"
        assert "fanout-2" in by_branch[dup_branch]["reason"]
