"""Tests for worktree-cleanup.sh's prerequisite checks.

``check_prereqs()`` (Task 2) runs before any scan/apply logic and verifies,
in order, that gh, wt, and jq are on PATH (via ``which``, resolved through
the ``WTC_GH_BIN``/``WTC_WT_BIN`` indirection), then that we're inside a
git repo. On any miss it prints an actionable message naming the missing
tool plus an install URL to stderr and exits 1 — before touching wt or gh.
"""

from __future__ import annotations

from pathlib import Path


def test_missing_gh_binary_fails_fast_with_actionable_message(
    fake_bins, run_script, tmp_path: Path
):
    # gh is checked first. Point WTC_GH_BIN at a path that does not exist
    # so `which` fails on the very first prereq check, while WTC_WT_BIN
    # stays pointed at a real (fake) executable — so a failure here can
    # only be about gh, not wt.
    env = dict(fake_bins.env)
    env["WTC_GH_BIN"] = str(tmp_path / "no-such-gh")

    result = run_script(["--format=json"], env=env)

    assert result.returncode == 1
    assert "required tool 'gh' not found on PATH" in result.stderr
    assert "https://cli.github.com/" in result.stderr
    assert result.stdout == ""
    # The script must fail before ever invoking wt or gh.
    assert fake_bins.calls() == []
