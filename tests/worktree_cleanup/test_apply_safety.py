"""Tests for worktree-cleanup.sh's `--apply` argv-safety and drift-guard
behavior (Task 14).

Behavioural contract under test (see "Apply flow" -> cmd_apply /
wtc_wt_remove / wtc_is_safe_category / wtc_print_apply_summary in
worktree-cleanup.sh):

1. Argv safety: every `wt remove` invocation includes `--no-delete-branch`;
   none ever includes `-D`/`--force-delete`/`-f`/`--force`. Only entries
   whose category is in the requested `--categories` list are ever passed
   to `wt remove`; a `dirty_skipped` entry is never removed even when not
   explicitly excluded via `--categories` (it can never appear in the safe
   category list anyway, but the exclusion is also explicit in the
   script). `--apply` never calls `wt list` and never calls `gh` at all —
   it loads a plan from disk and only re-verifies local git state.

2. Per-entry drift guard: immediately before removing each entry,
   `cmd_apply` re-checks `git -C <path> rev-parse HEAD` against the
   recorded `sha`, then `git -C <path> status --porcelain`, entirely
   independent of `wt`/`gh`. A mismatch on either check yields a
   `skipped_drift` outcome (reasons "sha changed since scan" /
   "became dirty since scan" respectively) and the entry is never handed
   to `wt remove` — but the guard is per-entry: a drifted entry does not
   abort entries after it.

3. `skipped_drift` entries are reported in the final summary printed to
   stdout, never silently dropped.

4. Exit code: `cmd_apply` exits non-zero iff at least one entry's outcome
   is `failed`; `skipped_drift` and `deleted` outcomes alone exit zero.

Uses real throwaway git worktrees on disk (via plain `git worktree add`,
following the pattern in test_categorize.py/test_plan_cache.py) because
the drift guard shells out to real `git -C <path> ...` against the exact
path/sha a hand-authored plan.json records — this cannot be faked via
`fake_bins` (which only fakes `wt`/`gh`). `wt remove` itself is faked via
`fake_bins` so no real `wt` binary is required.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

# A generic canned `wt remove` success response: exit 0, stderr contains
# "Branch integrated" so wtc_wt_remove's exit-code+stderr-text derivation
# (never wt's own --format=json branch_outcome, which is always
# "not_attempted" under --no-delete-branch) reports "deleted".
GENERIC_REMOVE_DELETED = {
    "argv_prefix": ["remove", "--no-delete-branch", "--foreground", "--format=json"],
    "stdout": "{}\n",
    "stderr": "Branch integrated (merged into main); retained with --no-delete-branch\n",
    "exit_code": 0,
}


def _rev_parse(path: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_main_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return _rev_parse(path)


def _add_worktree(main_repo: Path, wt_path: Path, branch: str) -> str:
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", "-q", "-b", branch, str(wt_path)],
        check=True,
    )
    return _rev_parse(wt_path)


def _commit_in_worktree(wt_path: Path, message: str = "more work") -> str:
    subprocess.run(
        ["git", "-C", str(wt_path), "commit", "-q", "--allow-empty", "-m", message],
        check=True,
    )
    return _rev_parse(wt_path)


def _make_dirty(wt_path: Path) -> None:
    (wt_path / "untracked.txt").write_text("dirty\n")


def _clean_entry(main_repo: Path, base_dir: Path, branch: str, category: str) -> dict:
    """Builds a plan entry for a branch/worktree whose recorded sha matches
    its real current HEAD and whose worktree is clean -- passes both
    drift-guard checks.
    """
    wt_path = base_dir / branch
    sha = _add_worktree(main_repo, wt_path, branch)
    return {"branch": branch, "path": str(wt_path), "sha": sha, "category": category}


def _sha_drift_entry(main_repo: Path, base_dir: Path, branch: str, category: str) -> dict:
    """Builds a plan entry whose recorded sha is stale: the worktree is
    advanced with an extra commit after recording the (now-stale) sha, so
    the real current HEAD no longer matches what the plan recorded.
    """
    wt_path = base_dir / branch
    recorded_sha = _add_worktree(main_repo, wt_path, branch)
    _commit_in_worktree(wt_path)  # advances real HEAD past recorded_sha
    return {"branch": branch, "path": str(wt_path), "sha": recorded_sha, "category": category}


def _dirty_drift_entry(main_repo: Path, base_dir: Path, branch: str, category: str) -> dict:
    """Builds a plan entry whose recorded sha is correct but whose
    worktree has been made dirty (untracked file) after the plan was
    written.
    """
    wt_path = base_dir / branch
    sha = _add_worktree(main_repo, wt_path, branch)
    _make_dirty(wt_path)
    return {"branch": branch, "path": str(wt_path), "sha": sha, "category": category}


def _write_plan(path: Path, entries: list[dict]) -> Path:
    plan = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": "example/repo",
        "default_branch": "main",
        "entries": entries,
    }
    path.write_text(json.dumps(plan))
    return path


def _wt_calls(fake_bins) -> list[dict]:
    return [c for c in fake_bins.calls() if c["bin"] == "wt"]


def _gh_calls(fake_bins) -> list[dict]:
    return [c for c in fake_bins.calls() if c["bin"] == "gh"]


def _remove_calls(fake_bins) -> list[dict]:
    return [c for c in _wt_calls(fake_bins) if c["argv"] and c["argv"][0] == "remove"]


def _removed_branches(fake_bins) -> list[str]:
    """The branch name (last argv element) of every `wt remove` call."""
    return [c["argv"][-1] for c in _remove_calls(fake_bins)]


# ---------------------------------------------------------------------------
# Argv-safety tests
# ---------------------------------------------------------------------------


def test_wt_remove_always_includes_no_delete_branch_and_never_force_flags(
    tmp_path, run_script, fake_bins
):
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    entries = [
        _clean_entry(main_repo, wt_dir, "merged-a", "merged"),
        _clean_entry(main_repo, wt_dir, "closed-b", "closed"),
    ]
    plan_path = _write_plan(tmp_path / "plan.json", entries)

    fake_bins.set_responses("wt", [GENERIC_REMOVE_DELETED])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    remove_calls = _remove_calls(fake_bins)
    assert len(remove_calls) == 2, remove_calls

    forbidden = {"-D", "--force-delete", "-f", "--force"}
    for call in remove_calls:
        argv = call["argv"]
        assert "--no-delete-branch" in argv, argv
        assert not (forbidden & set(argv)), argv


def test_only_selected_safe_categories_are_removed(tmp_path, run_script, fake_bins):
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    entries = [
        _clean_entry(main_repo, wt_dir, "merged-a", "merged"),
        _clean_entry(main_repo, wt_dir, "closed-b", "closed"),
        _clean_entry(main_repo, wt_dir, "empty-c", "empty"),
    ]
    plan_path = _write_plan(tmp_path / "plan.json", entries)

    fake_bins.set_responses("wt", [GENERIC_REMOVE_DELETED])

    # Only request merged,closed -- "empty" is a valid safe category but
    # not selected for this apply, so empty-c must never be touched.
    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert set(_removed_branches(fake_bins)) == {"merged-a", "closed-b"}


def test_dirty_skipped_entry_never_removed_even_with_default_categories(
    tmp_path, run_script, fake_bins
):
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    entries = [
        _clean_entry(main_repo, wt_dir, "merged-a", "merged"),
        _clean_entry(main_repo, wt_dir, "dirty-d", "dirty_skipped"),
    ]
    plan_path = _write_plan(tmp_path / "plan.json", entries)

    fake_bins.set_responses("wt", [GENERIC_REMOVE_DELETED])

    # No --categories flag -- cmd_apply defaults to all four safe
    # categories (merged,closed,empty,duplicate). dirty_skipped is not
    # among them, so it must be excluded regardless.
    result = run_script(["--apply", f"--plan={plan_path}"], env=fake_bins.env)

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    removed = _removed_branches(fake_bins)
    assert "dirty-d" not in removed
    assert "merged-a" in removed


def test_apply_never_calls_wt_list_or_gh(tmp_path, run_script, fake_bins):
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    entries = [
        _clean_entry(main_repo, wt_dir, "merged-a", "merged"),
        _clean_entry(main_repo, wt_dir, "closed-b", "closed"),
        _clean_entry(main_repo, wt_dir, "empty-c", "empty"),
    ]
    plan_path = _write_plan(tmp_path / "plan.json", entries)

    fake_bins.set_responses("wt", [GENERIC_REMOVE_DELETED])

    result = run_script(["--apply", f"--plan={plan_path}"], env=fake_bins.env)

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    assert _gh_calls(fake_bins) == [], "cmd_apply must never call gh"
    list_calls = [c for c in _wt_calls(fake_bins) if c["argv"] and c["argv"][0] == "list"]
    assert list_calls == [], "cmd_apply must never call `wt list` (no re-scan)"


# ---------------------------------------------------------------------------
# Drift-guard tests
# ---------------------------------------------------------------------------


def test_sha_drift_skips_only_the_drifted_entry(tmp_path, run_script, fake_bins):
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    drifted = _sha_drift_entry(main_repo, wt_dir, "drifted-a", "merged")
    safe = _clean_entry(main_repo, wt_dir, "safe-b", "closed")
    plan_path = _write_plan(tmp_path / "plan.json", [drifted, safe])

    fake_bins.set_responses("wt", [GENERIC_REMOVE_DELETED])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
    )

    # Drift guard is a per-entry, non-fatal outcome -- the run overall
    # still exits zero since nothing "failed".
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # The drifted branch must never reach `wt remove`.
    assert "drifted-a" not in _removed_branches(fake_bins)
    # The guard is per-entry, not run-aborting -- the other entry is
    # still removed.
    assert "safe-b" in _removed_branches(fake_bins)

    # Not silently dropped: reported in the printed summary with the
    # exact reason string.
    assert "drifted-a" in result.stdout
    assert "skipped_drift" in result.stdout
    assert "sha changed since scan" in result.stdout


def test_dirty_drift_skips_only_the_drifted_entry(tmp_path, run_script, fake_bins):
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    drifted = _dirty_drift_entry(main_repo, wt_dir, "drifted-a", "empty")
    safe = _clean_entry(main_repo, wt_dir, "safe-b", "merged")
    plan_path = _write_plan(tmp_path / "plan.json", [drifted, safe])

    fake_bins.set_responses("wt", [GENERIC_REMOVE_DELETED])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,empty"],
        env=fake_bins.env,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    assert "drifted-a" not in _removed_branches(fake_bins)
    assert "safe-b" in _removed_branches(fake_bins)

    assert "drifted-a" in result.stdout
    assert "skipped_drift" in result.stdout
    assert "became dirty since scan" in result.stdout


# ---------------------------------------------------------------------------
# Exit-code tests
# ---------------------------------------------------------------------------


def test_exit_zero_when_only_deleted_and_skipped_drift(tmp_path, run_script, fake_bins):
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    deleted = _clean_entry(main_repo, wt_dir, "safe-a", "merged")
    drifted = _sha_drift_entry(main_repo, wt_dir, "drifted-b", "closed")
    plan_path = _write_plan(tmp_path / "plan.json", [deleted, drifted])

    fake_bins.set_responses("wt", [GENERIC_REMOVE_DELETED])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "safe-a" in _removed_branches(fake_bins)
    assert "drifted-b" not in _removed_branches(fake_bins)


def test_exit_nonzero_when_any_entry_fails(tmp_path, run_script, fake_bins):
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    failing = _clean_entry(main_repo, wt_dir, "failing-a", "merged")
    succeeding = _clean_entry(main_repo, wt_dir, "safe-b", "closed")
    plan_path = _write_plan(tmp_path / "plan.json", [failing, succeeding])

    # A branch-specific failure rule must be checked before the generic
    # success rule (rules are matched in order, first-prefix-match wins).
    failing_rule = {
        "argv_prefix": [
            "remove",
            "--no-delete-branch",
            "--foreground",
            "--format=json",
            "failing-a",
        ],
        "stdout": "",
        "stderr": "error: worktree is locked\n",
        "exit_code": 1,
    }
    fake_bins.set_responses("wt", [failing_rule, GENERIC_REMOVE_DELETED])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
    )

    assert result.returncode != 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # wt remove is still invoked for the failing entry (the drift guard
    # passed; the failure is wt remove's own exit code) and for the
    # succeeding one -- a failure doesn't abort the run either.
    assert "failing-a" in _removed_branches(fake_bins)
    assert "safe-b" in _removed_branches(fake_bins)
    assert "failing-a" in result.stdout
    assert "failed" in result.stdout
