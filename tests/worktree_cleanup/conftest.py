"""Pytest fixtures for worktree-cleanup.sh tests.

NOTE for future reconciliation: this is a minimal, self-contained fixture
harness built for Task 5 (worktree inventory) because the shared harness
described in the plan (fake `wt`/`gh` binaries, a `run_script` helper, and
`tests/worktree_cleanup/conftest.py`) was being built concurrently by
another task and had not landed in this worktree yet. If that shared
harness lands with overlapping fixtures (e.g. its own `fake_wt`/`run_script`
under the same module path), the two should be merged rather than left to
diverge — this file's fixtures are intentionally narrow (just enough to
drive `worktree-cleanup.sh`'s scan path end to end) and can likely be
subsumed by the richer shared version.

Provides:

- ``script_path``: path to ``skills/worktree-cleanup/assets/worktree-cleanup.sh``.
- ``fake_bin``: a factory fixture that writes an executable fake ``wt`` (and
  a no-op fake ``gh``) into a per-test temp dir, returning their absolute
  paths. The fake ``wt`` responds to ``list --format=json`` by printing the
  contents of a caller-supplied fixture file to stdout and a fake
  schema-deprecation warning to stderr (mirroring real `wt` v0.74.0
  behavior), so tests can confirm the script never merges that stderr
  output into the JSON it parses (i.e. never uses ``2>&1``).
- ``run_script``: a helper that invokes the script via
  ``subprocess.run(["bash", script_path, *args], env=...)`` with
  ``WTC_WT_BIN``/``WTC_GH_BIN`` pointed at the fakes.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# tests/worktree_cleanup/conftest.py -> tests/worktree_cleanup -> tests -> <repo root>
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "skills" / "worktree-cleanup" / "assets" / "worktree-cleanup.sh"

_FAKE_WT_SCRIPT = """#!/usr/bin/env bash
# Fake `wt` binary for tests. Responds to `list --format=json` by printing
# the fixture named by $FAKE_WT_JSON_FILE to stdout, after writing a fake
# schema-deprecation warning to stderr -- mirroring the real `wt` v0.74.0
# behavior this task's script must tolerate without corrupting the JSON it
# parses (i.e. it must never be captured via `2>&1`).
set -euo pipefail
if [[ "${1:-}" == "list" ]]; then
  echo "▲ JSON output is schema 1; a future release switches the default to schema 2" >&2
  cat "$FAKE_WT_JSON_FILE"
  exit 0
fi
echo "fake wt: unsupported invocation: $*" >&2
exit 1
"""

_FAKE_GH_SCRIPT = """#!/usr/bin/env bash
# Fake `gh` binary for tests. worktree-cleanup.sh only needs it to exist on
# PATH (via WTC_GH_BIN) to pass its prerequisite check; this task's scan
# path does not shell out to gh yet.
exit 0
"""


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_bin(tmp_path: Path) -> dict[str, Path]:
    """Writes fake `wt` and `gh` executables under tmp_path and returns their paths."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    wt_path = _write_executable(bin_dir / "fake-wt", _FAKE_WT_SCRIPT)
    gh_path = _write_executable(bin_dir / "fake-gh", _FAKE_GH_SCRIPT)
    return {"wt": wt_path, "gh": gh_path}


@pytest.fixture
def run_script():
    """Returns a callable that invokes worktree-cleanup.sh as a subprocess.

    Usage: run_script(["--format=json"], wt_json_file=<path>, cwd=<path>)
    Returns the completed subprocess.CompletedProcess.
    """

    def _run(
        args: list[str],
        *,
        wt_bin: Path,
        gh_bin: Path,
        wt_json_file: Path | None = None,
        cwd: Path | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WTC_WT_BIN"] = str(wt_bin)
        env["WTC_GH_BIN"] = str(gh_bin)
        if wt_json_file is not None:
            env["FAKE_WT_JSON_FILE"] = str(wt_json_file)
        return subprocess.run(
            ["bash", str(SCRIPT_PATH), *args],
            env=env,
            cwd=str(cwd) if cwd is not None else str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    return _run
