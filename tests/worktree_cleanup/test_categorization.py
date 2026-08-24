"""Formal, comprehensive end-to-end tests for worktree-cleanup.sh's full
categorization pipeline (Task 13) -- covering wtc_inventory (Task 5),
wtc_lookup_pr (Task 6), the categorization ladder + dirty override
(Task 7), and the no-PR empty/duplicate-sha detection (Task 8), all wired
together through the script's CLI via the `--debug-categorize` hook.

This suite is the one named explicitly by the plan's verification step.
It overlaps intentionally with the more granular, per-task test files
(test_pr_lookup.py, test_categorize.py, test_no_pr_categorize.py) -- this
file exists to stand on its own with one case per ladder outcome, per the
plan's exact case list, plus the reused-branch precedence case that proves
Task 6's open > merged > closed PR-selection precedence is honored all the
way through the full pipeline (not just the lookup layer in isolation).

Behavioural contract under test (see the "Categorization ladder" section
in worktree-cleanup.sh):
  - error: a `gh` failure during PR lookup.
  - open: an open PR always wins, never removable.
  - merged: safe when local HEAD == or is an ancestor of the merged PR's
    headRefOid; needs_review (with an "N commits ahead" reason) otherwise.
  - closed: same tip-check, against a closed PR.
  - empty: no PR, HEAD is an ancestor of the default branch.
  - duplicate: no PR, HEAD sha shared with other no-PR branches -- all but
    the most-recently-touched become "duplicate"; that one becomes
    "needs_review".
  - dirty override: a dirty worktree is always "dirty_skipped", regardless
    of what the ladder above it would have produced.
  - is_main/is_current worktrees are excluded from the inventory (and
    therefore from the categorized output) entirely.
  - reused-branch precedence: a single head branch carrying both a CLOSED
    and a later OPEN PR resolves to "open", not "closed" -- proving a
    reused branch with live work on it can never land in the safe/
    removable set (merged, closed, empty, duplicate).

Tip checks and ancestor/duplicate-sha detection run against real,
temporary git repos (only `wt`/`gh` are faked via `fake_bins`), per the
established pattern in test_categorize.py and test_no_pr_categorize.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SAFE_CATEGORIES = {"merged", "closed", "empty", "duplicate"}


def _run_git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_git_repo(path: Path, branch: str = "wtc-test-initial") -> str:
    """Inits a repo with one commit and returns its sha.

    Defaults to a branch name that is never "main" (or any other plausible
    default), so tests that don't care about ancestry-against-default
    never accidentally pass just because this repo's own initial branch
    happens to share the default branch's name.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
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


def _commit_new_branch_from(repo: Path, branch: str, from_ref: str, message: str) -> str:
    """Creates `branch` off `from_ref`, adds one empty commit, returns to
    `from_ref`, and returns the new commit's sha."""
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


def _wt_entry(
    branch: str,
    path: Path,
    sha: str,
    dirty: bool = False,
    is_main: bool = False,
    is_current: bool = False,
) -> dict:
    """Builds one schema-1 `wt list --format=json` entry."""
    return {
        "branch": branch,
        "path": str(path),
        "kind": "worktree",
        "commit": {"sha": sha, "short_sha": sha[:7], "message": "msg", "timestamp": 0},
        "is_main": is_main,
        "is_current": is_current,
        "working_tree": {
            "staged": False,
            "modified": False,
            "untracked": dirty,
            "renamed": False,
            "deleted": False,
        },
    }


def _pr(state: str, head_ref_oid: str, number: int = 1, updated_at: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "state": state,
        "number": number,
        "title": "some pr",
        "url": f"https://github.com/example/repo/pull/{number}",
        "headRefOid": head_ref_oid,
        "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None,
        "closedAt": "2026-01-01T00:00:00Z" if state in ("MERGED", "CLOSED") else None,
        "updatedAt": updated_at,
    }


def _gh_rules(slug: str, *pr_list_rules: dict) -> list[dict]:
    return [
        {
            "argv_prefix": ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            "stdout": f"{slug}\n",
        },
        *pr_list_rules,
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


# ---------------------------------------------------------------------------
# One case per ladder outcome.
# ---------------------------------------------------------------------------


def test_error_category_on_gh_failure(tmp_path, run_script, fake_bins):
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

    by_branch = _run_categorize(run_script, fake_bins, [_wt_entry("feature", repo, sha)])

    assert by_branch["feature"]["category"] == "error"


def test_open_category(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("OPEN", "f" * 40, number=42)])},
        ),
    )

    by_branch = _run_categorize(run_script, fake_bins, [_wt_entry("feature", repo, sha)])

    assert by_branch["feature"]["category"] == "open"
    assert by_branch["feature"]["pr_number"] == 42


def test_merged_category_safe(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("MERGED", sha)])},
        ),
    )

    by_branch = _run_categorize(run_script, fake_bins, [_wt_entry("feature", repo, sha)])

    assert by_branch["feature"]["category"] == "merged"
    assert "reason" not in by_branch["feature"]


def test_merged_with_unpushed_commits_is_needs_review(tmp_path, run_script, fake_bins):
    """A merged PR whose local worktree has commits beyond the PR's
    headRefOid is "needs_review", with a reason string naming the exact
    commit count."""
    repo = tmp_path / "wt"
    c1 = _init_git_repo(repo)
    _commit(repo, "second")
    c3 = _commit(repo, "third")

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("MERGED", c1)])},
        ),
    )

    by_branch = _run_categorize(run_script, fake_bins, [_wt_entry("feature", repo, c3)])

    assert by_branch["feature"]["category"] == "needs_review"
    assert by_branch["feature"]["reason"] == "2 commits ahead of the merged PR."


def test_closed_category_safe(tmp_path, run_script, fake_bins):
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("CLOSED", sha)])},
        ),
    )

    by_branch = _run_categorize(run_script, fake_bins, [_wt_entry("feature", repo, sha)])

    assert by_branch["feature"]["category"] == "closed"


def test_empty_category(tmp_path, run_script, fake_bins):
    """A no-PR branch whose HEAD sha is an ancestor of the default branch
    (main) is "empty" -- zero unique commits."""
    repo = tmp_path / "repo"
    c0 = _init_git_repo(repo, branch="main")

    _branch_at(repo, "feature-empty", c0)
    wt_path = tmp_path / "wt-empty"
    _worktree_add(repo, wt_path, "feature-empty")

    fake_bins.set_responses(
        "gh",
        _gh_rules("example/repo", {"argv_prefix": ["pr", "list"], "stdout": "[]"}),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature-empty", wt_path, c0)]
    )

    assert by_branch["feature-empty"]["category"] == "empty"


def test_duplicate_category(tmp_path, run_script, fake_bins):
    """Among no-PR branches sharing the exact same HEAD sha, exactly one
    becomes "needs_review" (the most-recently-touched); the rest become
    "duplicate"."""
    repo = tmp_path / "repo"
    _init_git_repo(repo, branch="main")

    shared_sha = _commit_new_branch_from(repo, "shared-base", "main", "shared work")
    _branch_at(repo, "fanout-1", shared_sha)
    _branch_at(repo, "fanout-2", shared_sha)

    wt1, wt2 = (tmp_path / f"wt-{i}" for i in (1, 2))
    _worktree_add(repo, wt1, "fanout-1")
    _worktree_add(repo, wt2, "fanout-2")

    now = time.time()
    _set_mtime(wt1, now - 200)
    _set_mtime(wt2, now - 100)  # fanout-2 is more recently touched.

    fake_bins.set_responses(
        "gh",
        _gh_rules("example/repo", {"argv_prefix": ["pr", "list"], "stdout": "[]"}),
    )

    by_branch = _run_categorize(
        run_script,
        fake_bins,
        [
            _wt_entry("fanout-1", wt1, shared_sha),
            _wt_entry("fanout-2", wt2, shared_sha),
        ],
    )

    assert by_branch["fanout-2"]["category"] == "needs_review"
    assert by_branch["fanout-1"]["category"] == "duplicate"
    assert "fanout-2" in by_branch["fanout-1"]["reason"]


# ---------------------------------------------------------------------------
# Dirty override beats the ladder.
# ---------------------------------------------------------------------------


def test_dirty_override_beats_ladder(tmp_path, run_script, fake_bins):
    """A worktree that would otherwise be "open" -- never removed even
    when clean -- is forced to "dirty_skipped" when dirty."""
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("OPEN", "f" * 40)])},
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("feature", repo, sha, dirty=True)]
    )

    assert by_branch["feature"]["category"] == "dirty_skipped"


def test_dirty_override_beats_safe_merged(tmp_path, run_script, fake_bins):
    """A worktree that would otherwise be safely "merged" is forced to
    "dirty_skipped" when dirty."""
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


# ---------------------------------------------------------------------------
# is_main / is_current exclusion.
# ---------------------------------------------------------------------------


def test_is_main_and_is_current_excluded_from_categorized_output(tmp_path, run_script, fake_bins):
    main_repo = tmp_path / "main-wt"
    main_sha = _init_git_repo(main_repo)
    current_repo = tmp_path / "current-wt"
    current_sha = _init_git_repo(current_repo)
    other_repo = tmp_path / "other-wt"
    other_sha = _init_git_repo(other_repo)

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps([_pr("OPEN", "f" * 40)])},
        ),
    )

    by_branch = _run_categorize(
        run_script,
        fake_bins,
        [
            _wt_entry("main", main_repo, main_sha, is_main=True),
            _wt_entry("current-feature", current_repo, current_sha, is_current=True),
            _wt_entry("other-feature", other_repo, other_sha),
        ],
    )

    assert "main" not in by_branch
    assert "current-feature" not in by_branch
    assert "other-feature" in by_branch


# ---------------------------------------------------------------------------
# Reused-branch precedence, end-to-end through the full pipeline.
# ---------------------------------------------------------------------------


def test_reused_branch_open_wins_over_closed_and_is_never_safe(tmp_path, run_script, fake_bins):
    """A single head branch carrying two PRs -- one CLOSED, one separate
    and later OPEN -- must categorize as "open" (informational, never
    removed), never "closed". This proves Task 6's open > merged > closed
    precedence is honored end-to-end through the full categorization
    pipeline, and that a reused branch with live work on it can never land
    in the safe/removable set."""
    repo = tmp_path / "wt"
    sha = _init_git_repo(repo)

    closed_pr = _pr("CLOSED", "c" * 40, number=10, updated_at="2026-01-01T00:00:00Z")
    open_pr = _pr("OPEN", "o" * 40, number=11, updated_at="2026-06-01T00:00:00Z")

    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {
                "argv_prefix": ["pr", "list"],
                "stdout": json.dumps([closed_pr, open_pr]),
            },
        ),
    )

    by_branch = _run_categorize(
        run_script, fake_bins, [_wt_entry("reused-branch", repo, sha)]
    )

    entry = by_branch["reused-branch"]
    assert entry["category"] == "open"
    assert entry["category"] != "closed"
    assert entry["pr_number"] == 11
    assert entry["category"] not in SAFE_CATEGORIES
