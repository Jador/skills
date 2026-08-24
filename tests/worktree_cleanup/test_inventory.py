"""Tests for worktree-cleanup.sh's worktree inventory (Task 5).

Behavioural contract under test:

1. Both `wt list --format=json` schema shapes -- the bare-array "schema 1"
   (default on `wt` v0.74.0) and the enveloped "schema 2"
   (`{schema, repo, collected, items: [...]}`, which a future `wt` release
   switches to by default) -- normalize to the same inventory.
2. Entries flagged as the main worktree or the current worktree are
   excluded from the inventory, regardless of schema shape.
3. A worktree that is clean per git except for an *ignored* file is NOT
   classified dirty, and its ignored-file count reflects that file.
4. A worktree with only untracked (non-ignored) changes IS classified
   dirty (per `wt remove`'s refusal to remove untracked-only worktrees
   without `-f`; see Task 1's spike notes), and its ignored-file count is 0.
5. `wt`'s stderr (a schema-deprecation warning on real `wt` v0.74.0) never
   leaks into the JSON the script parses -- the fake `wt` always emits one,
   and every assertion here depends on `stdout` being clean, valid JSON.

Fixture JSON payloads mirror the real shapes captured from an actual
installed `wt v0.74.0` (schema 1 default; schema 2 obtained by forcing
`--config-set list.json-schema=2`) -- see the Task 5 report for the raw
samples this was modeled on.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _make_clean_with_ignored_repo(path: Path) -> None:
    """A repo that is clean per `git status` but has one ignored file."""
    _init_git_repo(path)
    (path / ".gitignore").write_text("ignored.log\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add gitignore"], cwd=path, check=True)
    (path / "ignored.log").write_text("noise\n")  # untracked, but ignored


def _make_untracked_repo(path: Path) -> None:
    """A repo with a single untracked (non-ignored) file, nothing else."""
    _init_git_repo(path)
    (path / "scratch.txt").write_text("wip\n")  # untracked, not ignored


def _entry_by_branch(inventory: list[dict], branch: str) -> dict:
    matches = [e for e in inventory if e["branch"] == branch]
    assert len(matches) == 1, f"expected exactly one entry for {branch!r}, got {matches}"
    return matches[0]


def _schema1_fixture(entries: list[dict]) -> list[dict]:
    """Builds a bare-array ("schema 1") `wt list --format=json` payload."""
    out = []
    for e in entries:
        out.append(
            {
                "branch": e["branch"],
                "path": e["path"],
                "kind": "worktree",
                "commit": {
                    "sha": e["sha"],
                    "short_sha": e["sha"][:7],
                    "message": "some commit",
                    "timestamp": 0,
                },
                "working_tree": {
                    "staged": e["staged"],
                    "modified": e["modified"],
                    "untracked": e["untracked"],
                    "renamed": e["renamed"],
                    "deleted": e["deleted"],
                    "diff": {"added": 0, "deleted": 0},
                },
                "main_state": "same_commit",
                "is_main": e["is_main"],
                "is_current": e["is_current"],
                "is_previous": False,
                "repo_url": "https://github.com/example/repo",
                "repo": {
                    "url": "https://github.com/example/repo",
                    "provider": "github",
                    "host": "github.com",
                    "owner": "example",
                    "name": "repo",
                    "remote": "origin",
                },
                "statusline": e["branch"],
                "symbols": "",
            }
        )
    return out


def _schema2_fixture(entries: list[dict]) -> dict:
    """Builds an enveloped ("schema 2") `wt list --format=json` payload."""
    items = []
    for e in entries:
        items.append(
            {
                "branch": e["branch"],
                "head": {
                    "sha": e["sha"],
                    "short_sha": e["sha"][:7],
                    "subject": "some commit",
                    "committed_at": "2026-01-01T00:00:00Z",
                },
                "worktree": {
                    "path": e["path"],
                    "main": e["is_main"],
                    "current": e["is_current"],
                    "previous": False,
                    "detached": False,
                    "branch_mismatch": False,
                    "duplicate_branch": False,
                    "changes": {
                        "staged": e["staged"],
                        "modified": e["modified"],
                        "untracked": e["untracked"],
                        "renamed": e["renamed"],
                        "deleted": e["deleted"],
                        "conflicted": False,
                        "diff": {"added": 0, "deleted": 0},
                    },
                },
                "display": {
                    "state": "same_commit",
                    "symbols": "",
                    "statusline": e["branch"],
                },
            }
        )
    return {
        "schema": 2,
        "repo": {
            "default_branch": "main",
            "forge": {
                "url": "https://github.com/example/repo",
                "provider": "github",
                "host": "github.com",
                "owner": "example",
                "name": "repo",
                "remote": "origin",
            },
        },
        "collected": {"ci": False, "summary": False},
        "items": items,
    }


@pytest.fixture
def worktree_paths(tmp_path: Path) -> dict[str, Path]:
    """Real git repos backing each fixture entry's `path` field.

    `main`/`current` never get a filesystem probe (they're filtered out by
    the script's jq stage before any `git status` call), so plain
    directories are enough for those two; `clean`/`untracked` get real
    repos so the ignored-file-count logic has something real to measure.
    """
    paths = {
        "main": tmp_path / "main-wt",
        "current": tmp_path / "current-wt",
        "clean": tmp_path / "clean-wt",
        "untracked": tmp_path / "untracked-wt",
    }
    paths["main"].mkdir()
    paths["current"].mkdir()
    _make_clean_with_ignored_repo(paths["clean"])
    _make_untracked_repo(paths["untracked"])
    return paths


def _entries(paths: dict[str, Path]) -> list[dict]:
    return [
        {
            "branch": "main",
            "path": str(paths["main"]),
            "sha": "a" * 40,
            "is_main": True,
            "is_current": False,
            "staged": False,
            "modified": False,
            "untracked": False,
            "renamed": False,
            "deleted": False,
        },
        {
            "branch": "current-branch",
            "path": str(paths["current"]),
            "sha": "b" * 40,
            "is_main": False,
            "is_current": True,
            # Deliberately dirty -- must still be excluded because it's
            # current, regardless of dirtiness.
            "staged": False,
            "modified": False,
            "untracked": True,
            "renamed": False,
            "deleted": False,
        },
        {
            "branch": "clean-branch",
            "path": str(paths["clean"]),
            "sha": "c" * 40,
            "is_main": False,
            "is_current": False,
            "staged": False,
            "modified": False,
            "untracked": False,
            "renamed": False,
            "deleted": False,
        },
        {
            "branch": "untracked-branch",
            "path": str(paths["untracked"]),
            "sha": "d" * 40,
            "is_main": False,
            "is_current": False,
            "staged": False,
            "modified": False,
            "untracked": True,
            "renamed": False,
            "deleted": False,
        },
    ]


@pytest.mark.parametrize("schema", ["schema1", "schema2"])
def test_inventory_normalizes_both_schemas(
    schema, tmp_path, worktree_paths, run_script, fake_bins
):
    entries = _entries(worktree_paths)
    payload = _schema1_fixture(entries) if schema == "schema1" else _schema2_fixture(entries)

    # Fake `wt` responds to `list --format=json` with the fixture payload on
    # stdout and a fabricated schema-deprecation warning on stderr, mirroring
    # real `wt` v0.74.0 -- the script must discard that stderr (2>/dev/null,
    # never 2>&1) rather than let it corrupt the JSON it parses.
    fake_bins.set_responses(
        "wt",
        [
            {
                "argv_prefix": ["list", "--format=json"],
                "stdout": json.dumps(payload),
                "stderr": "▲ JSON output is schema 1; a future release switches the default to schema 2\n",
            }
        ],
    )

    result = run_script(["--format=json"], env=fake_bins.env)

    assert result.returncode == 0, (
        f"script failed (schema={schema}): stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # The fake wt always writes a fabricated schema-deprecation warning to
    # its own stderr (mirroring real `wt` v0.74.0). The script discards
    # wt's stderr (`2>/dev/null`, never `2>&1`) rather than surfacing it,
    # so the warning must not appear anywhere here, and -- more
    # importantly -- stdout must still parse as clean JSON below rather
    # than being corrupted by it.
    assert "schema" not in result.stdout
    assert "schema" not in result.stderr

    inventory = json.loads(result.stdout)

    # --- main/current exclusion ---
    branches = {e["branch"] for e in inventory}
    assert branches == {"clean-branch", "untracked-branch"}, (
        f"schema={schema}: expected main/current excluded, got branches={branches}"
    )

    # --- clean-except-ignored worktree: NOT dirty, ignored_count == 1 ---
    clean = _entry_by_branch(inventory, "clean-branch")
    assert clean["dirty"] is False, f"schema={schema}: clean-branch should not be dirty"
    assert clean["ignored_count"] == 1, (
        f"schema={schema}: expected 1 ignored file, got {clean['ignored_count']}"
    )

    # --- untracked-only worktree: classified dirty, ignored_count == 0 ---
    untracked = _entry_by_branch(inventory, "untracked-branch")
    assert untracked["dirty"] is True, (
        f"schema={schema}: untracked-only worktree must be classified dirty"
    )
    assert untracked["untracked"] is True
    assert untracked["staged"] is False
    assert untracked["modified"] is False
    assert untracked["ignored_count"] == 0

    # --- path/sha carried through correctly ---
    assert clean["path"] == str(worktree_paths["clean"])
    assert clean["sha"] == "c" * 40
    assert untracked["path"] == str(worktree_paths["untracked"])
    assert untracked["sha"] == "d" * 40


def test_schema1_and_schema2_produce_identical_inventory(
    tmp_path, worktree_paths, run_script, fake_bins
):
    """Same input worktree state, both schemas -> byte-identical normalized inventory."""
    entries = _entries(worktree_paths)

    fake_bins.set_responses(
        "wt",
        [{"argv_prefix": ["list", "--format=json"], "stdout": json.dumps(_schema1_fixture(entries))}],
    )
    result1 = run_script(["--format=json"], env=fake_bins.env)

    fake_bins.set_responses(
        "wt",
        [{"argv_prefix": ["list", "--format=json"], "stdout": json.dumps(_schema2_fixture(entries))}],
    )
    result2 = run_script(["--format=json"], env=fake_bins.env)

    assert result1.returncode == 0
    assert result2.returncode == 0

    inventory1 = json.loads(result1.stdout)
    inventory2 = json.loads(result2.stdout)

    key = lambda e: e["branch"]  # noqa: E731
    assert sorted(inventory1, key=key) == sorted(inventory2, key=key)


def test_text_format_reports_category_and_dirty_override(
    tmp_path, worktree_paths, run_script, fake_bins
):
    """cmd_scan's text report now surfaces the categorization ladder's
    output (Task 7) rather than the raw dirty/ignored fields directly:
    a no-PR, clean worktree reports the Task 8 extension-point placeholder
    category, and a no-PR but dirty worktree is forced to "dirty_skipped"
    by the dirty override -- regardless of what the (no-PR) ladder would
    otherwise have produced.
    """
    entries = _entries(worktree_paths)
    fake_bins.set_responses(
        "wt",
        [{"argv_prefix": ["list", "--format=json"], "stdout": json.dumps(_schema1_fixture(entries))}],
    )
    fake_bins.set_responses(
        "gh",
        [
            {
                "argv_prefix": ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                "stdout": "example/repo\n",
            },
            {"argv_prefix": ["pr", "list"], "stdout": "[]"},
        ],
    )

    result = run_script([], env=fake_bins.env)

    assert result.returncode == 0
    assert "current-branch" not in result.stdout
    assert "untracked-branch" in result.stdout
    assert "category=dirty_skipped" in result.stdout
    assert "clean-branch" in result.stdout
    assert "category=no_pr_pending" in result.stdout
