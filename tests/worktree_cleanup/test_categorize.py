"""Tests for worktree-cleanup.sh's categorization ladder + dirty override
(Task 7).

Behavioural contract under test (see the "Categorization ladder" section
in worktree-cleanup.sh):

1. An open PR always yields category "open", regardless of local commit
   position relative to the PR's head.
2. A merged PR whose selected `headRefOid` equals the local worktree's
   HEAD, or of which the local HEAD is an ancestor, yields the safe
   category "merged".
3. A merged PR where local HEAD has advanced beyond `headRefOid` yields
   "needs_review" with a reason string naming the exact commit count
   ("N commits ahead of the merged PR.").
4. The same two outcomes apply to a closed (not merged) PR, with the
   reason string naming "the closed PR" instead.
5. A branch with no PR at all hands off to the Task 8 extension point
   (wtc_categorize_no_pr), surfaced for now as the placeholder category
   "no_pr_pending".
6. A `gh` failure during PR lookup yields category "error".
7. The dirty override applies last and unconditionally: a dirty worktree
   is always categorized "dirty_skipped", regardless of what the ladder
   above produced -- including overriding "open" and a safe "merged".

Uses the internal `--debug-categorize` hook (undocumented in --help, same
pattern as --debug-context/--debug-lookup-pr) to exercise
wtc_categorize_all/wtc_categorize_entry through the script's CLI. Tip
checks (`git merge-base --is-ancestor`, `git rev-list --count`) run
against real, temporary git repos so the ladder's git plumbing is
exercised for real rather than mocked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _run_git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_git_repo(path: Path) -> str:
    """Inits a repo with one commit and returns its sha."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return _run_git(path, "rev-parse", "HEAD")


def _commit(path: Path, message: str) -> str:
    """Adds an empty commit to an existing repo and returns its sha."""
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message], cwd=path, check=True
    )
    return _run_git(path, "rev-parse", "HEAD")


def _touch_untracked(path: Path) -> None:
    (path / "scratch.txt").write_text("wip\n")


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


def _gh_rules(slug: str, pr_list_rule: dict) -> list[dict]:
    return [
        {
            "argv_prefix": ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            "stdout": f"{slug}\n",
        },
        pr_list_rule,
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


def _pr(state: str, head_ref_oid: str, number: int = 1) -> dict:
    return {
        "state": state,
        "number": number,
        "title": "some pr",
        "url": f"https://github.com/example/repo/pull/{number}",
        "headRefOid": head_ref_oid,
        "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None,
        "closedAt": "2026-01-01T00:00:00Z" if state in ("MERGED", "CLOSED") else None,
        "updatedAt": "2026-01-01T00:00:00Z",
    }


def test_open_pr_is_open_regardless_of_tip(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {
                "argv_prefix": ["pr", "list"],
                "stdout": json.dumps([_pr("OPEN", "f" * 40, number=42)]),
            },
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, sha)]
    )

    assert by_branch["feature"]["category"] == "open"
    assert by_branch["feature"]["pr_number"] == 42


def test_merged_pr_equal_tip_is_safe(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("MERGED", sha)])},
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, sha)]
    )

    assert by_branch["feature"]["category"] == "merged"
    assert "reason" not in by_branch["feature"]


def test_merged_pr_local_ancestor_of_head_is_safe(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    c1 = _init_git_repo(repo)
    c2 = _commit(repo, "second")
    # local worktree's recorded sha (c1) is an ancestor of the PR's
    # headRefOid (c2) -- local hasn't advanced beyond what merged.
    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("MERGED", c2)])},
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, c1)]
    )

    assert by_branch["feature"]["category"] == "merged"


def test_merged_pr_local_ahead_is_needs_review_with_count(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    c1 = _init_git_repo(repo)
    _commit(repo, "second")
    c3 = _commit(repo, "third")
    # local worktree's recorded sha (c3) is two commits ahead of the PR's
    # merged headRefOid (c1).
    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("MERGED", c1)])},
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, c3)]
    )

    assert by_branch["feature"]["category"] == "needs_review"
    assert by_branch["feature"]["reason"] == "2 commits ahead of the merged PR."


def test_closed_pr_local_ahead_is_needs_review_with_count(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    c1 = _init_git_repo(repo)
    c2 = _commit(repo, "second")
    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("CLOSED", c1)])},
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, c2)]
    )

    assert by_branch["feature"]["category"] == "needs_review"
    assert by_branch["feature"]["reason"] == "1 commits ahead of the closed PR."


def test_closed_pr_equal_tip_is_safe(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("CLOSED", sha)])},
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, sha)]
    )

    assert by_branch["feature"]["category"] == "closed"


def test_no_pr_hands_off_to_task8_placeholder(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules("example/repo", {"argv_prefix": ["pr", "list"], "stdout": "[]"}),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, sha)]
    )

    assert by_branch["feature"]["category"] == "no_pr_pending"


def test_gh_failure_is_error_category(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {
                "argv_prefix": ["pr", "list"],
                "stdout": "",
                "stderr": "gh: some API failure\n",
                "exit_code": 1,
            },
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, sha)]
    )

    assert by_branch["feature"]["category"] == "error"


def test_dirty_override_beats_open(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {
                "argv_prefix": ["pr", "list"],
                "stdout": json.dumps([_pr("OPEN", "f" * 40)]),
            },
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, sha, dirty=True)]
    )

    assert by_branch["feature"]["category"] == "dirty_skipped"


def test_dirty_override_beats_safe_merged(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("MERGED", sha)])},
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, sha, dirty=True)]
    )

    assert by_branch["feature"]["category"] == "dirty_skipped"


def test_dirty_override_beats_needs_review(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    c1 = _init_git_repo(repo)
    c2 = _commit(repo, "second")
    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("MERGED", c1)])},
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, c2, dirty=True)]
    )

    assert by_branch["feature"]["category"] == "dirty_skipped"
