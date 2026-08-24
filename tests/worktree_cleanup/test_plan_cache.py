"""Tests for worktree-cleanup.sh's plan cache write (Task 9).

Behavioural contract under test (see "Plan cache write" ->
wtc_write_plan_cache in worktree-cleanup.sh):

1. Every `cmd_scan` invocation writes the plan cache to the Task 4 cache
   path (`<git-common-dir>/worktree-cleanup-plan.json`), regardless of
   --format.
2. The written file is valid JSON containing `generated_at`, `repo`,
   `default_branch`, and `entries` (the categorized entries array).
3. The write is atomic: after a scan, no leftover temp file
   (`.worktree-cleanup-plan.*`) remains next to the cache file.
4. `generated_at` honors the `WTC_NOW` injectable override, so tests don't
   have to depend on wall-clock time.

Unlike test_categorize.py/test_no_pr_categorize.py (which use the
`--debug-categorize` hook and therefore never touch the cache path), these
tests drive the real `cmd_scan` entry point (no debug flags) with an
explicit `cwd` pointed at a throwaway git repo -- so the cache file lands
under that repo's `.git/`, never under this project's own `.git/`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def _init_git_repo(path: Path) -> str:
    """Inits a repo with one commit and returns its sha."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _wt_entry(branch: str, path: Path, sha: str) -> dict:
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
            "untracked": False,
            "renamed": False,
            "deleted": False,
        },
    }


def _run_scan(
    script_path: Path, fake_bins, repo: Path, args: list[str] | None = None, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(fake_bins.env)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script_path), *(args or [])],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def scan_setup(tmp_path, fake_bins):
    """Sets up a throwaway repo with one no-PR worktree branch and fakes
    wt/gh so a scan can run end-to-end without a real network/gh/wt.
    """
    repo = tmp_path / "repo"
    sha = _init_git_repo(repo)

    wt_path = tmp_path / "wt-feature"
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "work"], check=True
    )
    feature_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(wt_path), "feature"],
        check=True,
    )

    fake_bins.set_responses(
        "wt",
        [
            {
                "argv_prefix": ["list", "--format=json"],
                "stdout": json.dumps([_wt_entry("feature", wt_path, feature_sha)]),
            }
        ],
    )
    fake_bins.set_responses(
        "gh",
        [
            {
                "argv_prefix": [
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner",
                    "--jq",
                    ".nameWithOwner",
                ],
                "stdout": "example/repo\n",
            },
            {"argv_prefix": ["pr", "list"], "stdout": "[]"},
        ],
    )

    cache_path = repo / ".git" / "worktree-cleanup-plan.json"
    return repo, cache_path


def test_scan_writes_valid_cache_with_generated_at(script_path, fake_bins, scan_setup):
    repo, cache_path = scan_setup

    result = _run_scan(
        script_path, fake_bins, repo, extra_env={"WTC_NOW": "2026-08-24T00:00:00Z"}
    )

    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert cache_path.exists(), "plan cache file was not written"

    plan = json.loads(cache_path.read_text())
    assert plan["generated_at"] == "2026-08-24T00:00:00Z"
    assert plan["repo"] == "example/repo"
    assert plan["default_branch"] == "main"
    assert isinstance(plan["entries"], list)
    assert len(plan["entries"]) == 1
    assert plan["entries"][0]["branch"] == "feature"


def test_scan_writes_cache_regardless_of_format(script_path, fake_bins, scan_setup):
    repo, cache_path = scan_setup

    result = _run_scan(
        script_path,
        fake_bins,
        repo,
        args=["--format=json"],
        extra_env={"WTC_NOW": "2026-08-24T00:00:00Z"},
    )

    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert cache_path.exists()
    plan = json.loads(cache_path.read_text())
    assert plan["generated_at"] == "2026-08-24T00:00:00Z"


def test_scan_leaves_no_temp_file_behind(script_path, fake_bins, scan_setup):
    repo, cache_path = scan_setup

    result = _run_scan(script_path, fake_bins, repo)

    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert cache_path.exists()

    leftovers = list(cache_path.parent.glob(".worktree-cleanup-plan.*"))
    assert leftovers == [], f"leftover temp file(s) found: {leftovers}"


def test_scan_default_generated_at_is_iso8601_when_wtc_now_unset(
    script_path, fake_bins, scan_setup
):
    repo, cache_path = scan_setup

    result = _run_scan(script_path, fake_bins, repo)

    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    plan = json.loads(cache_path.read_text())
    # No strict datetime parsing dependency: just check the ISO 8601
    # UTC shape (`YYYY-MM-DDTHH:MM:SSZ`) produced by `date -u
    # +%Y-%m-%dT%H:%M:%SZ` when WTC_NOW isn't set.
    import re

    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", plan["generated_at"])
