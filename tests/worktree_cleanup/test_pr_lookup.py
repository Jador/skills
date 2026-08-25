"""Tests for worktree-cleanup.sh's PR lookup layer (Task 6).

Behavioural contract under test (see the "PR lookup layer" section in
worktree-cleanup.sh):

1. `gh pr list --repo <slug> --head <branch> --state all --json ...` is the
   query; `gh`'s states come back uppercase and must be compared
   case-insensitively.
2. A non-zero `gh` exit always yields category "error" -- it never falls
   through to "no_pr".
3. Repeated lookups of the same branch within a single script invocation
   hit the per-run cache: exactly one `gh` invocation is logged even when
   the branch is looked up more than once.
4. Multi-PR heads (a reused branch carrying more than one PR) resolve to
   exactly one selected PR per the open > merged > closed precedence,
   ties broken by most recent mergedAt/closedAt.

These use the internal `--debug-lookup-pr=<comma-list>` hook (undocumented
in --help, same pattern as --debug-context) to exercise wtc_lookup_pr
through the script's CLI, since the test harness only drives the script
as a subprocess rather than sourcing its functions directly. `gh repo
view` is always stubbed too, since detect_repo_slug runs first.
"""

from __future__ import annotations

import json

import pytest


def _gh_rules(slug: str, pr_list_rule: dict) -> list[dict]:
    return [
        {
            "argv_prefix": ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            "stdout": f"{slug}\n",
        },
        pr_list_rule,
    ]


def _pr_list_calls(fake_bins) -> list[dict]:
    return [
        c
        for c in fake_bins.calls()
        if c["bin"] == "gh" and c["argv"][:2] == ["pr", "list"]
    ]


def _run_lookup(run_script, fake_bins, branches: list[str]):
    result = run_script(
        [f"--debug-lookup-pr={','.join(branches)}"], env=fake_bins.env
    )
    assert result.returncode == 0, (
        f"script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == len(branches), (
        f"expected {len(branches)} result line(s), got {lines!r}"
    )
    return [json.loads(line) for line in lines]


def test_uppercase_open_state_matches_case_insensitively(run_script, fake_bins):
    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {
                "argv_prefix": ["pr", "list"],
                "stdout": json.dumps(
                    [
                        {
                            "state": "OPEN",
                            "number": 42,
                            "title": "Add widget",
                            "url": "https://github.com/example/repo/pull/42",
                            "headRefOid": "a" * 40,
                            "mergedAt": None,
                            "closedAt": None,
                            "updatedAt": "2026-01-01T00:00:00Z",
                        }
                    ]
                ),
            },
        ),
    )

    (result,) = _run_lookup(run_script, fake_bins, ["feature-branch"])

    assert result == {
        "category": "pr",
        "state": "OPEN",
        "number": 42,
        "title": "Add widget",
        "url": "https://github.com/example/repo/pull/42",
        "headRefOid": "a" * 40,
    }


def test_nonzero_gh_exit_yields_error_not_no_pr(run_script, fake_bins):
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

    (result,) = _run_lookup(run_script, fake_bins, ["broken-branch"])

    # The error result carries gh's own stderr text as "reason" (rather
    # than discarding it) so a caller can report *why* the lookup failed,
    # not just that it did.
    assert result["category"] == "error"
    assert result["reason"] == "gh: some API failure"


def test_empty_pr_list_yields_no_pr(run_script, fake_bins):
    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": "[]"},
        ),
    )

    (result,) = _run_lookup(run_script, fake_bins, ["untouched-branch"])

    assert result == {"category": "no_pr"}


def test_repeated_branch_hits_cache_exactly_one_gh_invocation(run_script, fake_bins):
    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {
                "argv_prefix": ["pr", "list"],
                "stdout": json.dumps(
                    [
                        {
                            "state": "OPEN",
                            "number": 7,
                            "title": "Repeated lookup",
                            "url": "https://github.com/example/repo/pull/7",
                            "headRefOid": "b" * 40,
                            "mergedAt": None,
                            "closedAt": None,
                            "updatedAt": "2026-01-01T00:00:00Z",
                        }
                    ]
                ),
            },
        ),
    )

    results = _run_lookup(
        run_script, fake_bins, ["same-branch", "same-branch", "same-branch"]
    )

    assert results[0] == results[1] == results[2]
    assert results[0]["category"] == "pr"
    assert results[0]["number"] == 7

    pr_list_calls = _pr_list_calls(fake_bins)
    assert len(pr_list_calls) == 1, (
        f"expected exactly one `gh pr list` invocation, got {pr_list_calls!r}"
    )


@pytest.mark.parametrize(
    "prs,expected_number",
    [
        # closed + open -> open wins regardless of recency.
        (
            [
                {
                    "state": "CLOSED",
                    "number": 1,
                    "title": "old closed",
                    "url": "https://github.com/example/repo/pull/1",
                    "headRefOid": "a" * 40,
                    "mergedAt": None,
                    "closedAt": "2026-06-01T00:00:00Z",
                    "updatedAt": "2026-06-01T00:00:00Z",
                },
                {
                    "state": "OPEN",
                    "number": 2,
                    "title": "new open",
                    "url": "https://github.com/example/repo/pull/2",
                    "headRefOid": "b" * 40,
                    "mergedAt": None,
                    "closedAt": None,
                    "updatedAt": "2026-01-01T00:00:00Z",
                },
            ],
            2,
        ),
        # closed + merged -> merged wins.
        (
            [
                {
                    "state": "CLOSED",
                    "number": 3,
                    "title": "old closed",
                    "url": "https://github.com/example/repo/pull/3",
                    "headRefOid": "c" * 40,
                    "mergedAt": None,
                    "closedAt": "2026-06-01T00:00:00Z",
                    "updatedAt": "2026-06-01T00:00:00Z",
                },
                {
                    "state": "MERGED",
                    "number": 4,
                    "title": "merged one",
                    "url": "https://github.com/example/repo/pull/4",
                    "headRefOid": "d" * 40,
                    "mergedAt": "2026-01-01T00:00:00Z",
                    "closedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                },
            ],
            4,
        ),
        # two closed PRs -> most recently closed wins.
        (
            [
                {
                    "state": "CLOSED",
                    "number": 5,
                    "title": "earlier closed",
                    "url": "https://github.com/example/repo/pull/5",
                    "headRefOid": "e" * 40,
                    "mergedAt": None,
                    "closedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                },
                {
                    "state": "CLOSED",
                    "number": 6,
                    "title": "later closed",
                    "url": "https://github.com/example/repo/pull/6",
                    "headRefOid": "f" * 40,
                    "mergedAt": None,
                    "closedAt": "2026-06-01T00:00:00Z",
                    "updatedAt": "2026-06-01T00:00:00Z",
                },
            ],
            6,
        ),
    ],
    ids=["closed+open->open", "closed+merged->merged", "two-closed->most-recent"],
)
def test_multi_pr_head_precedence(run_script, fake_bins, prs, expected_number):
    fake_bins.set_responses(
        "gh",
        _gh_rules(
            "example/repo",
            {"argv_prefix": ["pr", "list"], "stdout": json.dumps(prs)},
        ),
    )

    (result,) = _run_lookup(run_script, fake_bins, ["reused-branch"])

    assert result["category"] == "pr"
    assert result["number"] == expected_number

    # Exactly one PR selected -- never multiple.
    assert isinstance(result.get("state"), str)
