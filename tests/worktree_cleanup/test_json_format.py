"""Tests for worktree-cleanup.sh's `--format=json` stdout/stderr discipline
(Task 11).

Behavioural contract under test:

1. With `--format=json`, `cmd_scan` writes EXACTLY the plan JSON (the same
   object `wtc_write_plan_cache` writes to the cache file --
   `generated_at`/`repo`/`default_branch`/`entries[]`) to stdout, and
   nothing else -- no progress/diagnostic chatter.
2. Any warning condition surfaced during a scan (e.g. a `gh` PR-lookup
   failure, categorized as `"error"`) is written to stderr, never stdout.
3. Absent such a condition, stderr stays empty (nothing currently prints
   unconditional progress chatter during a scan).

Follows the same throwaway-git-repo + fake wt/gh harness as
test_plan_cache.py, since `--format=json` drives the real `cmd_scan` entry
point (not a `--debug-*` hook).
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


def _add_feature_worktree(repo: Path, tmp_path: Path) -> tuple[Path, str]:
    """Creates a `feature` branch (one commit ahead of main) as a real git
    worktree under `tmp_path`, and returns (worktree_path, head_sha).
    """
    wt_path = tmp_path / "wt-feature"
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True)
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
    return wt_path, feature_sha


def _run_scan(
    script_path: Path,
    fake_bins,
    repo: Path,
    args: list[str] | None = None,
    extra_env: dict | None = None,
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
def scan_setup_with_gh_error(tmp_path, fake_bins):
    """One worktree branch whose `gh pr list` call fails (non-zero exit),
    so categorization yields a `"category":"error"` entry -- the warning
    condition `wtc_emit_scan_warnings` watches for.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    wt_path, feature_sha = _add_feature_worktree(repo, tmp_path)

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
            {
                "argv_prefix": ["pr", "list"],
                "stdout": "",
                "stderr": "gh: some API error\n",
                "exit_code": 1,
            },
        ],
    )

    return repo


@pytest.fixture
def scan_setup_clean(tmp_path, fake_bins):
    """One worktree branch with a healthy (empty) `gh pr list` result --
    no warning condition should fire.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    wt_path, feature_sha = _add_feature_worktree(repo, tmp_path)

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

    return repo


def test_format_json_stdout_is_pure_json_matching_schema(
    script_path, fake_bins, scan_setup_with_gh_error
):
    repo = scan_setup_with_gh_error

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

    # Bonus/sanity: stdout must parse as JSON with no stripping needed. If
    # any diagnostic/progress chatter leaked onto stdout alongside the
    # plan JSON, this raises.
    plan = json.loads(result.stdout)

    assert plan["generated_at"] == "2026-08-24T00:00:00Z"
    assert plan["repo"] == "example/repo"
    assert plan["default_branch"] == "main"
    assert isinstance(plan["entries"], list)
    assert len(plan["entries"]) == 1
    assert plan["entries"][0]["branch"] == "feature"
    assert plan["entries"][0]["category"] == "error"


def test_format_json_stderr_nonempty_on_warning_condition(
    script_path, fake_bins, scan_setup_with_gh_error
):
    repo = scan_setup_with_gh_error

    result = _run_scan(script_path, fake_bins, repo, args=["--format=json"])

    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stderr.strip() != "", (
        "expected a warning on stderr for the gh-failure/error category entry"
    )
    assert "feature" in result.stderr
    assert "error" in result.stderr.lower()

    # The warning is chatter, not data -- stdout must still parse cleanly
    # on its own, with nothing needing to be stripped out first.
    json.loads(result.stdout)


def test_format_json_no_stderr_chatter_without_warning_condition(
    script_path, fake_bins, scan_setup_clean
):
    repo = scan_setup_clean

    result = _run_scan(script_path, fake_bins, repo, args=["--format=json"])

    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    plan = json.loads(result.stdout)
    assert plan["entries"][0]["category"] != "error"
    assert result.stderr == ""
