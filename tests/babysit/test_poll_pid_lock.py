"""Tests for poll.sh PID single-flight lock (finding #7).

A second poller for the same (repo, PR) must not be allowed to clobber
the first poller's PID file. Otherwise Stop mode can never reach the
first poller, and the two pollers race on inserts/emits.

Contract under test:

1. With a live PID in the existing PID file, poll.sh refuses to start
   and emits a kind=init error pointing at the existing pid.
2. With a stale PID (process no longer alive), poll.sh reclaims the
   slot and proceeds.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
POLL_SH = REPO_ROOT / "skills" / "babysit" / "assets" / "poll.sh"


def _write_fake_gh(bin_dir: Path, *, repo: str = "org-a/foo",
                   pr: int = 42, branch: str = "feat/x") -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f'''#!/usr/bin/env bash
case "$1 $2" in
    "repo view") echo "{repo}" ;;
    "pr view")   printf "%s\\n%s\\n" "{pr}" "{branch}" ;;
    *) exit 1 ;;
esac
''')
    gh.chmod(0o755)


def _pid_file(plugin_data: Path) -> Path:
    return plugin_data / "babysit" / "babysit-pid-org-a__foo-42.pid"


def _base_env(plugin_data: Path, bin_dir: Path) -> dict:
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(plugin_data)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


@pytest.fixture
def plugin_data(tmp_path: Path) -> Path:
    (tmp_path / "babysit").mkdir()
    return tmp_path


def test_refuses_to_start_with_live_pid_already_in_file(plugin_data: Path,
                                                        tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir)

    # Seed the PID file with our own PID — guaranteed alive.
    pid_file = _pid_file(plugin_data)
    pid_file.write_text(f"{os.getpid()}\n")

    result = subprocess.run(
        # Only --no-builds: --no-comments too would trip the both-flags
        # guard before the PID check we are exercising here.
        ["bash", str(POLL_SH), "--no-builds", "--interval", "30"],
        env=_base_env(plugin_data, bin_dir),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    line = result.stdout.strip().splitlines()[0]
    payload = json.loads(line)
    assert payload["type"] == "error"
    assert payload["kind"] == "init"
    assert payload["pr"] == 42
    assert "already running" in payload["message"]
    # PID file must still hold the original PID — second poller did not
    # clobber it.
    assert pid_file.read_text().strip() == str(os.getpid())


def test_reclaims_slot_when_pid_in_file_is_stale(plugin_data: Path,
                                                 tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir)

    # Use a PID guaranteed not alive AND not reusable by the poll.sh
    # process we are about to spawn. Reaping a real short-lived PID is
    # flaky: the OS can reassign that exact number to poll.sh itself.
    # A value above the platform pid_max can never name a live process.
    dead_pid = 2_147_483_646

    pid_file = _pid_file(plugin_data)
    pid_file.write_text(f"{dead_pid}\n")

    # Run poll.sh briefly; it should reclaim the slot, write its own PID,
    # then enter the sleep loop. Kill it via timeout.
    try:
        subprocess.run(
            # Only --no-builds: both --no-* flags would trip the
            # both-flags guard before the PID reclaim we are testing.
            ["bash", str(POLL_SH), "--no-builds", "--interval", "30"],
            env=_base_env(plugin_data, bin_dir),
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except subprocess.TimeoutExpired:
        pass

    # The PID file must now hold a PID different from the dead one
    # (poll.sh's own PID, which may or may not still be in the file
    # depending on whether the EXIT trap fired). Either:
    #   - file still exists with poll.sh's PID, OR
    #   - file was removed by the EXIT trap on SIGTERM.
    # Both prove the stale slot was reclaimed.
    if pid_file.exists():
        assert pid_file.read_text().strip() != str(dead_pid)
