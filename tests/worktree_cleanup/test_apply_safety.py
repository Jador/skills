"""Tests for worktree-cleanup.sh's `--apply` argv-safety and drift-guard
behavior.

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
   `cmd_apply` re-checks (all plain local `git`, no `wt`/`gh`): that the
   entry's `path` is still a worktree of the repo the script is standing
   in (`git worktree list`), that it isn't the worktree the apply run is
   itself standing in (`git rev-parse --show-toplevel`), `git -C <path>
   rev-parse HEAD` against the recorded `sha`, `git -C <path> status
   --porcelain`, and finally whether the worktree has gained MORE ignored
   files than the scan recorded. A mismatch on any check yields a
   `skipped_drift` outcome and the entry is never handed to `wt remove` —
   but the guard is per-entry: a drifted entry does not abort entries
   after it.

3. `skipped_drift` entries are reported in the final summary printed to
   stdout, never silently dropped.

4. Exit code: `cmd_apply` exits non-zero iff at least one entry's outcome
   is `failed`; `skipped_drift` and `removed` outcomes alone exit zero.

Uses real throwaway git worktrees on disk (via plain `git worktree add`,
following the pattern in test_categorize.py/test_plan_cache.py) because
the drift guard shells out to real `git -C <path> ...` against the exact
path/sha a hand-authored plan.json records — this cannot be faked via
`fake_bins` (which only fakes `wt`/`gh`). `wt remove` itself is faked via
`fake_bins` so no real `wt` binary is required.

Every `run_script(["--apply", ...])` call below passes `cwd=main_repo`:
the provenance/self-targeting checks resolve `git worktree list`/
`git rev-parse --show-toplevel` relative to the script's own cwd (that's
the whole point of those checks — they're asking "is this path really
part of the repo I'm standing in?"), so cwd must actually be inside the
repo the plan's entries belong to, matching how a user would really run
`--apply` (standing in their repo, not some unrelated directory).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

# A generic canned `wt remove` success response: exit 0, so
# wtc_wt_remove's exit-code-only derivation (never wt's own --format=json
# branch_outcome, which is always "not_attempted" under --no-delete-branch)
# reports "removed" -- the only success outcome; the branch's fate never
# varies (--no-delete-branch is unconditional), so it isn't encoded as a
# second outcome value.
GENERIC_REMOVE_OK = {
    "argv_prefix": ["remove", "--no-delete-branch", "--foreground", "--format=json"],
    "stdout": "{}\n",
    "stderr": "",
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
    _commit_in_worktree(wt_path)
    return {"branch": branch, "path": str(wt_path), "sha": recorded_sha, "category": category}


def _unrelated_repo_entry(base_dir: Path, branch: str, category: str) -> dict:
    """Builds a plan entry whose `path` is a real git repo, but NOT a
    worktree of `main_repo` at all -- simulates a stale/moved worktree, or
    a plan file that was generated for an entirely different repo.
    """
    repo_path = base_dir / branch
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, check=True)
    (repo_path / "README.md").write_text("unrelated\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_path, check=True)
    sha = _rev_parse(repo_path)
    return {"branch": branch, "path": str(repo_path), "sha": sha, "category": category}


def _self_target_entry(main_repo: Path, branch: str, category: str) -> dict:
    """Builds a plan entry whose `path` is `main_repo` itself -- simulates
    an apply run standing in the exact worktree a (mis-scoped) plan names.
    """
    sha = _rev_parse(main_repo)
    return {"branch": branch, "path": str(main_repo), "sha": sha, "category": category}


def _ignored_growth_entry(main_repo: Path, base_dir: Path, branch: str, category: str) -> dict:
    """Builds a plan entry recording `ignored_count: 0`, with the ignore
    rule itself committed (so the worktree is genuinely clean per `git
    status --porcelain`, matching its recorded sha), then drops a matching
    ignored-but-real file into the worktree afterward -- simulates a
    worktree gaining ignored-but-real state (e.g. a fresh .env) since the
    scan. `git status --porcelain` (no `--ignored`) can't see this file at
    all; only `wtc_ignored_count`'s `--ignored` check can.
    """
    wt_path = base_dir / branch
    _add_worktree(main_repo, wt_path, branch)
    (wt_path / ".gitignore").write_text("ignored-after-scan.txt\n")
    subprocess.run(["git", "-C", str(wt_path), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(wt_path), "commit", "-q", "-m", "add gitignore rule"],
        check=True,
    )
    sha = _rev_parse(wt_path)  # recorded AFTER the gitignore commit -- no sha drift
    (wt_path / "ignored-after-scan.txt").write_text("real local state\n")
    return {
        "branch": branch,
        "path": str(wt_path),
        "sha": sha,
        "category": category,
        "ignored_count": 0,
    }


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

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
        cwd=main_repo,
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

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    # Only request merged,closed -- "empty" is a valid safe category but
    # not selected for this apply, so empty-c must never be touched.
    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
        cwd=main_repo,
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

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    # No --categories flag -- cmd_apply defaults to all four safe
    # categories (merged,closed,empty,duplicate). dirty_skipped is not
    # among them, so it must be excluded regardless.
    result = run_script(
        ["--apply", f"--plan={plan_path}"], env=fake_bins.env, cwd=main_repo
    )

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

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    result = run_script(
        ["--apply", f"--plan={plan_path}"], env=fake_bins.env, cwd=main_repo
    )

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

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
        cwd=main_repo,
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

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,empty"],
        env=fake_bins.env,
        cwd=main_repo,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    assert "drifted-a" not in _removed_branches(fake_bins)
    assert "safe-b" in _removed_branches(fake_bins)

    assert "drifted-a" in result.stdout
    assert "skipped_drift" in result.stdout
    assert "became dirty since scan" in result.stdout


def test_provenance_drift_skips_entry_not_a_worktree_of_this_repo(
    tmp_path, run_script, fake_bins
):
    """A plan entry whose `path` is a real git repo, but never registered
    as a worktree of `main_repo` (e.g. a plan generated for a different
    repo, or a worktree that's since been moved/deregistered), must never
    reach `wt remove` -- `wt remove <branch>` resolves its target repo
    from cwd, not from the entry's path, so acting on it could remove the
    wrong repo's same-named branch.
    """
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    unrelated = _unrelated_repo_entry(wt_dir, "unrelated-a", "merged")
    safe = _clean_entry(main_repo, wt_dir, "safe-b", "closed")
    plan_path = _write_plan(tmp_path / "plan.json", [unrelated, safe])

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
        cwd=main_repo,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    assert "unrelated-a" not in _removed_branches(fake_bins)
    assert "safe-b" in _removed_branches(fake_bins)

    assert "unrelated-a" in result.stdout
    assert "skipped_drift" in result.stdout
    assert "not a worktree of the current repository" in result.stdout


def test_self_targeting_drift_skips_the_current_worktree(
    tmp_path, run_script, fake_bins
):
    """A plan entry whose `path` is the worktree the apply run is itself
    standing in must never be removed, even if its category/sha look
    perfectly safe.
    """
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    self_target = _self_target_entry(main_repo, "self-a", "empty")
    safe = _clean_entry(main_repo, wt_dir, "safe-b", "merged")
    plan_path = _write_plan(tmp_path / "plan.json", [self_target, safe])

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,empty"],
        env=fake_bins.env,
        cwd=main_repo,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    assert "self-a" not in _removed_branches(fake_bins)
    assert "safe-b" in _removed_branches(fake_bins)

    assert "self-a" in result.stdout
    assert "skipped_drift" in result.stdout
    assert "currently standing in" in result.stdout


def test_ignored_files_growth_drift_skips_only_the_drifted_entry(
    tmp_path, run_script, fake_bins
):
    """A worktree that gained MORE ignored files than the scan recorded
    must be skipped -- `git status --porcelain` (the existing dirty check)
    can't see ignored files at all, so this is the one drift the sha/dirty
    checks alone would miss (the design's own named residual-loss vector,
    since branches are never deleted but ignored-but-real state can still
    be destroyed).
    """
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    grown = _ignored_growth_entry(main_repo, wt_dir, "grown-a", "merged")
    safe = _clean_entry(main_repo, wt_dir, "safe-b", "closed")
    plan_path = _write_plan(tmp_path / "plan.json", [grown, safe])

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
        cwd=main_repo,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    assert "grown-a" not in _removed_branches(fake_bins)
    assert "safe-b" in _removed_branches(fake_bins)

    assert "grown-a" in result.stdout
    assert "skipped_drift" in result.stdout
    assert "ignored files increased since scan" in result.stdout


# ---------------------------------------------------------------------------
# Exit-code tests
# ---------------------------------------------------------------------------


def test_exit_zero_when_only_removed_and_skipped_drift(tmp_path, run_script, fake_bins):
    main_repo = tmp_path / "main-repo"
    _init_main_repo(main_repo)
    wt_dir = tmp_path / "worktrees"

    removed = _clean_entry(main_repo, wt_dir, "safe-a", "merged")
    drifted = _sha_drift_entry(main_repo, wt_dir, "drifted-b", "closed")
    plan_path = _write_plan(tmp_path / "plan.json", [removed, drifted])

    fake_bins.set_responses("wt", [GENERIC_REMOVE_OK])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
        cwd=main_repo,
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
    fake_bins.set_responses("wt", [failing_rule, GENERIC_REMOVE_OK])

    result = run_script(
        ["--apply", f"--plan={plan_path}", "--categories=merged,closed"],
        env=fake_bins.env,
        cwd=main_repo,
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
