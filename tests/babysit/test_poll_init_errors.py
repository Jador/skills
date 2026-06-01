"""Tests for poll.sh init-error JSON contract.

Behavioural contract under test:

1. Init errors (failure to detect repo/PR/branch, schema apply failure)
   emit JSON with kind="init" and an explicit `pr` field. Until PR
   detection succeeds, `pr` is the JSON literal `null`. Once PR is
   known, `pr` is the integer.
2. When --no-builds is omitted but no pipeline slug is supplied, poll.sh
   refuses to start and emits an init error rather than silently never
   checking CI.

The tests run poll.sh against a fake `gh` shimmed onto PATH so the
poller never touches a real GitHub account.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
POLL_SH = REPO_ROOT / "skills" / "babysit" / "assets" / "poll.sh"


def _write_fake_gh(bin_dir: Path, *, fail_repo: bool = False,
                   repo: str = "org-a/foo", pr: int = 42,
                   branch: str = "feat/x") -> None:
    """Write a fake `gh` to bin_dir that returns canned JSON.

    fail_repo=True makes `gh repo view` exit non-zero, simulating
    invocation from a non-git directory.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    if fail_repo:
        body = '#!/usr/bin/env bash\nexit 1\n'
    else:
        body = f'''#!/usr/bin/env bash
case "$1 $2" in
    "repo view")
        echo "{repo}"
        ;;
    "pr view")
        printf "%s\\n%s\\n" "{pr}" "{branch}"
        ;;
    *)
        exit 1
        ;;
esac
'''
    gh.write_text(body)
    gh.chmod(0o755)


def _run_poll(*args, env_extra: dict, timeout: float = 5.0):
    return subprocess.run(
        ["bash", str(POLL_SH), *args],
        env=env_extra,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
def plugin_data(tmp_path: Path) -> Path:
    (tmp_path / "babysit").mkdir()
    return tmp_path


def _base_env(plugin_data: Path, bin_dir: Path) -> dict:
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(plugin_data)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


def test_init_error_when_repo_undetected_carries_pr_null(plugin_data: Path,
                                                         tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir, fail_repo=True)

    result = _run_poll(env_extra=_base_env(plugin_data, bin_dir))
    assert result.returncode == 1

    # First stdout line is the init error JSON. Strict parse.
    line = result.stdout.strip().splitlines()[0]
    payload = json.loads(line)
    assert payload["type"] == "error"
    assert payload["kind"] == "init"
    assert payload["pr"] is None
    assert "Failed to detect repository" in payload["message"]


def test_init_error_when_pr_value_is_non_numeric(plugin_data: Path,
                                                 tmp_path: Path):
    # gh can return the literal "null" or another non-integer string
    # when its response shape changes. Without an explicit numeric
    # guard, that value flows into jq --argjson pr "$PR" later in the
    # poll cycle, jq exits non-zero, `|| return 0` swallows it, and
    # poll_comments/poll_builds silently emit nothing for the rest of
    # the session.
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir, pr="null")  # type: ignore[arg-type]

    result = _run_poll(env_extra=_base_env(plugin_data, bin_dir))
    assert result.returncode == 1
    line = result.stdout.strip().splitlines()[0]
    payload = json.loads(line)
    assert payload["type"] == "error"
    assert payload["kind"] == "init"
    assert payload["pr"] is None
    assert "not numeric" in payload["message"]


def test_init_error_when_pipeline_missing_carries_pr_int(plugin_data: Path,
                                                         tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir)  # gh succeeds, no --no-builds, no pipeline arg.

    result = _run_poll(env_extra=_base_env(plugin_data, bin_dir))
    assert result.returncode == 1

    line = result.stdout.strip().splitlines()[0]
    payload = json.loads(line)
    assert payload["type"] == "error"
    assert payload["kind"] == "init"
    # PR was successfully detected before the pipeline check fired.
    assert payload["pr"] == 42
    assert "no pipeline slug" in payload["message"].lower()


def test_no_builds_flag_skips_pipeline_requirement(plugin_data: Path,
                                                   tmp_path: Path):
    # With --no-builds, the missing pipeline is fine. Poller proceeds
    # past init and enters its sleep loop — kill it after a short
    # window via timeout.
    # Only --no-builds (comments stay enabled) — must NOT trip the
    # both-flags guard, and the missing pipeline is fine.
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir)

    try:
        result = _run_poll("--no-builds", "--interval", "30",
                           env_extra=_base_env(plugin_data, bin_dir),
                           timeout=2.0)
    except subprocess.TimeoutExpired as e:
        # Expected — poller is now in its sleep loop. The stdout we did
        # see must NOT contain an init error.
        stdout = e.stdout.decode() if e.stdout else ""
        assert '"kind":"init"' not in stdout
        return
    # If it exited cleanly within 2s, something is off.
    pytest.fail(f"Poller exited early: rc={result.returncode} "
                f"stdout={result.stdout!r} stderr={result.stderr!r}")


def test_both_no_flags_rejected(plugin_data: Path, tmp_path: Path):
    # --no-comments --no-builds together = nothing to poll. Must fail
    # fast, not spin up a silent no-op poller. The guard fires before
    # repo detection, so gh need not be reachable — but provide it for
    # realism.
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir)

    result = _run_poll("--no-comments", "--no-builds",
                       env_extra=_base_env(plugin_data, bin_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout.strip().splitlines()[0])
    assert payload["kind"] == "init"
    assert payload["pr"] is None
    assert "nothing to poll" in payload["message"].lower()


def test_init_error_when_branch_is_null(plugin_data: Path, tmp_path: Path):
    # gh returned a numeric PR but headRefName=null (deleted source ref).
    # BRANCH="null" must be rejected, not silently accepted.
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir, branch="null")

    result = _run_poll("--no-builds",
                       env_extra=_base_env(plugin_data, bin_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout.strip().splitlines()[0])
    assert payload["kind"] == "init"
    assert payload["pr"] == 42
    assert "branch is null" in payload["message"].lower()


def test_init_error_repo_bad_charset(plugin_data: Path, tmp_path: Path):
    # A repo slug outside [A-Za-z0-9._-]/[...] means the gh output shape
    # changed or something injected it — refuse rather than build an
    # unsafe SQL literal.
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir, repo="org/foo;DROP")

    result = _run_poll("--no-builds",
                       env_extra=_base_env(plugin_data, bin_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout.strip().splitlines()[0])
    assert payload["kind"] == "init"
    assert "unexpected characters" in payload["message"].lower()


def test_non_numeric_pr_error_is_valid_json_with_quotes(plugin_data: Path,
                                                        tmp_path: Path):
    # The non-numeric-PR error must stay valid JSON even when the bad
    # PR value itself contains a double-quote (built via jq, not string
    # interpolation). Use a literal heredoc in the fake gh so the quote
    # survives into poll.sh verbatim.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1 $2" in\n'
        '  "repo view") echo "org-a/foo" ;;\n'
        '  "pr view")\n'
        "    cat <<'PR_EOF'\n"
        'bad"value\n'
        "feat/x\n"
        "PR_EOF\n"
        "    ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)

    result = _run_poll(env_extra=_base_env(plugin_data, bin_dir))
    assert result.returncode == 1
    line = result.stdout.strip().splitlines()[0]
    # Must parse — the bug was invalid JSON here.
    payload = json.loads(line)
    assert payload["kind"] == "init"
    assert payload["pr"] is None
    assert 'bad"value' in payload["message"]
