"""Snapshot-style tests for worktree-cleanup.sh's human-readable text
report renderer -- ``wtc_render_text_report``, wired into ``cmd_scan``
for the default/``--format=text`` path.

Behavioural contract under test (see the "Human-readable text report"
section in worktree-cleanup.sh):

1. Safe categories (merged, closed, empty, duplicate) are listed before
   the non-safe/informational categories (open, needs_review,
   dirty_skipped, error).
2. A category with zero entries in the scan is omitted entirely -- no
   "Empty (0):" (or any other empty category's) header ever prints.
3. needs_review / dirty_skipped / error entries show their `reason`
   string alongside the branch name; safe entries don't carry one.
4. An entry's ignored-file count is appended to its line only when it is
   non-zero.
5. The report ends with a line naming the plan cache path and a
   copy-pasteable next `--apply --categories=...` command restricted to
   the safe categories that actually have entries in this scan.

This drives the real `cmd_scan` entry point (no debug flags, default
--format=text) against a throwaway git repo with a worktree per ladder
outcome, following the same `cwd`-scoped-execution pattern established in
test_plan_cache.py -- so the plan cache (and repo-context detection) is
scoped to the throwaway repo, never this project's own.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_SLUG = "example/repo"


def _run_git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_git_repo(path: Path) -> str:
    """Inits a repo on "main" with an initial commit (README + a
    .gitignore ignoring "ignored-file*", inherited by every branch created
    off it) and returns the initial commit's sha."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    (path / ".gitignore").write_text("ignored-file*\n")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return _run_git(path, "rev-parse", "HEAD")


def _commit(path: Path, message: str) -> str:
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message], cwd=path, check=True
    )
    return _run_git(path, "rev-parse", "HEAD")


def _branch_with_commit(repo: Path, branch: str, from_ref: str, message: str) -> str:
    """Creates `branch` off `from_ref`, adds one commit, returns to
    `from_ref`, and returns the new commit's sha."""
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", branch, from_ref], check=True)
    sha = _commit(repo, message)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", from_ref], check=True)
    return sha


def _worktree_add(repo: Path, wt_path: Path, branch: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(wt_path), branch], check=True
    )


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


def _pr(state: str, head_ref_oid: str, number: int) -> dict:
    return {
        "state": state,
        "number": number,
        "title": "some pr",
        "url": f"https://github.com/{REPO_SLUG}/pull/{number}",
        "headRefOid": head_ref_oid,
        "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None,
        "closedAt": "2026-01-01T00:00:00Z" if state in ("MERGED", "CLOSED") else None,
        "updatedAt": "2026-01-01T00:00:00Z",
    }


def _pr_list_rule(branch: str, prs: list[dict] | None = None, *, error: bool = False) -> dict:
    """A gh-response rule matched specifically to `pr list ... --head
    <branch> ...`, so a multi-branch scan can give each branch a distinct
    canned PR-list response (or failure) in a single scan."""
    rule = {
        "argv_prefix": ["pr", "list", "--repo", REPO_SLUG, "--head", branch],
    }
    if error:
        rule.update(stdout="", stderr="gh: some API failure\n", exit_code=1)
    else:
        rule.update(stdout=json.dumps(prs or []))
    return rule


@pytest.fixture
def report_setup(tmp_path, fake_bins):
    """Builds a throwaway repo with one worktree per ladder outcome
    (merged, closed w/ an ignored file, open, needs_review, dirty_skipped,
    error) and wires up wt/gh fakes so a plain `cmd_scan` (no --debug-*
    flags, no --format) runs the full pipeline end-to-end.

    Deliberately has zero "empty" and zero "duplicate" entries (no no-PR
    branches at all) so the report's omission-of-empty-sections behavior
    is exercised by two categories at once.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    sha_merged = _branch_with_commit(repo, "feature-merged", "main", "merged work")
    sha_closed = _branch_with_commit(repo, "feature-closed", "main", "closed work")
    sha_open = _branch_with_commit(repo, "feature-open", "main", "open work")
    review_pr_head = _branch_with_commit(repo, "feature-review", "main", "review work")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "feature-review"], check=True)
    _commit(repo, "review work 2")
    sha_review = _commit(repo, "review work 3")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    sha_dirty = _branch_with_commit(repo, "feature-dirty", "main", "dirty work")
    sha_error = _branch_with_commit(repo, "feature-error", "main", "error work")

    wt_paths = {}
    for branch, sha in (
        ("feature-merged", sha_merged),
        ("feature-closed", sha_closed),
        ("feature-open", sha_open),
        ("feature-review", sha_review),
        ("feature-dirty", sha_dirty),
        ("feature-error", sha_error),
    ):
        wt_path = tmp_path / f"wt-{branch}"
        _worktree_add(repo, wt_path, branch)
        wt_paths[branch] = wt_path

    # An ignored (not tracked, not staged) file inside feature-closed's
    # worktree, matched by the .gitignore inherited from `main` -- this
    # is what makes feature-closed's ignored_count non-zero.
    (wt_paths["feature-closed"] / "ignored-file.log").write_text("noise\n")

    wt_entries = [
        _wt_entry("feature-merged", wt_paths["feature-merged"], sha_merged),
        _wt_entry("feature-closed", wt_paths["feature-closed"], sha_closed),
        _wt_entry("feature-open", wt_paths["feature-open"], sha_open),
        _wt_entry("feature-review", wt_paths["feature-review"], sha_review),
        _wt_entry("feature-dirty", wt_paths["feature-dirty"], sha_dirty, dirty=True),
        _wt_entry("feature-error", wt_paths["feature-error"], sha_error),
    ]
    fake_bins.set_responses(
        "wt", [{"argv_prefix": ["list", "--format=json"], "stdout": json.dumps(wt_entries)}]
    )

    gh_rules = [
        {
            "argv_prefix": ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            "stdout": f"{REPO_SLUG}\n",
        },
        _pr_list_rule("feature-merged", [_pr("MERGED", sha_merged, 1)]),
        _pr_list_rule("feature-closed", [_pr("CLOSED", sha_closed, 2)]),
        _pr_list_rule("feature-open", [_pr("OPEN", sha_open, 3)]),
        _pr_list_rule("feature-review", [_pr("MERGED", review_pr_head, 4)]),
        # feature-dirty's PR state is irrelevant -- the dirty override
        # discards it regardless -- but it still needs a matching rule so
        # the lookup doesn't fall through to the "no rule matched -> exit
        # 0, empty stdout -> no_pr" default and skew the no-PR batch.
        _pr_list_rule("feature-dirty", [_pr("OPEN", "f" * 40, 5)]),
        _pr_list_rule("feature-error", error=True),
    ]
    fake_bins.set_responses("gh", gh_rules)

    cache_path = repo / ".git" / "worktree-cleanup-plan.json"
    return repo, cache_path


def _run_scan(script_path: Path, fake_bins, repo: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(fake_bins.env)
    return subprocess.run(
        ["bash", str(script_path)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_text_report_snapshot(script_path, fake_bins, report_setup):
    repo, cache_path = report_setup

    result = _run_scan(script_path, fake_bins, repo)

    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    out = result.stdout

    # --- Safe categories appear before non-safe ones. ---
    idx_merged = out.index("Merged (")
    idx_closed = out.index("Closed (")
    idx_open = out.index("Open (")
    idx_needs_review = out.index("Needs Review (")
    idx_dirty = out.index("Dirty (skipped) (")
    idx_error = out.index("Error (")

    last_safe_idx = max(idx_merged, idx_closed)
    first_nonsafe_idx = min(idx_open, idx_needs_review, idx_dirty, idx_error)
    assert last_safe_idx < first_nonsafe_idx, (
        "expected all safe-category sections before all non-safe sections:\n" + out
    )

    # --- Empty sections (no entries this scan) are omitted entirely. ---
    assert "Empty (" not in out, f"'Empty' section header should be omitted:\n{out}"
    assert "Duplicate (" not in out, f"'Duplicate' section header should be omitted:\n{out}"

    # --- Reasons appear for needs_review / dirty_skipped / error. ---
    review_line = next(line for line in out.splitlines() if "feature-review" in line)
    assert "2 commits ahead of the merged PR." in review_line

    dirty_line = next(line for line in out.splitlines() if "feature-dirty" in line)
    assert "worktree has uncommitted changes (dirty override)" in dirty_line
    # error's reason (added alongside the renderer so the report has
    # something to show for the one ladder outcome the spec calls out
    # that previously carried no reason at all).
    error_line = next(line for line in out.splitlines() if "feature-error" in line)
    assert "--" in error_line, f"expected a reason on the error entry's line: {error_line!r}"

    # Safe entries do NOT carry a reason suffix.
    merged_line = next(line for line in out.splitlines() if "feature-merged" in line)
    assert "--" not in merged_line

    # --- Ignored-file count: only feature-closed (count=1) shows one. ---
    closed_line = next(line for line in out.splitlines() if "feature-closed" in line)
    assert "(1 ignored file)" in closed_line
    for branch in (
        "feature-merged",
        "feature-open",
        "feature-review",
        "feature-dirty",
        "feature-error",
    ):
        line = next(l for l in out.splitlines() if branch in l)
        assert "ignored file" not in line, f"unexpected ignored-file count on: {line!r}"
    assert "(0 ignored" not in out

    # --- Closing line: cache path + a copy-pasteable --apply command,
    # restricted to the safe categories actually present (merged, closed
    # -- not empty/duplicate, which have zero entries this scan). ---
    assert str(cache_path) in out
    assert "--apply" in out
    assert "--categories=merged,closed" in out
