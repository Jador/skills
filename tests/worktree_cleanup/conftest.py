"""Shared pytest fixtures for worktree-cleanup.sh tests.

worktree-cleanup.sh resolves the ``wt`` and ``gh`` binaries through the
``WTC_WT_BIN`` / ``WTC_GH_BIN`` env vars (default: plain ``wt``/``gh`` on
PATH), specifically so tests can inject fakes without touching PATH. This
module provides that harness:

- ``script_path``: Path to the script under test.
- ``fake_bins``: writes executable fake ``wt``/``gh`` binaries into a tmp
  bin dir. Each fake logs its full argv to a shared call-log file (JSON
  lines) and echoes a canned response looked up from a JSON "response
  rules" fixture file, selected via the ``WTC_WT_RESPONSES`` /
  ``WTC_GH_RESPONSES`` env vars. Tests point those env vars at whatever
  canned fixture the scenario needs via ``FakeBins.set_responses()``,
  without ever having to rewrite the fake binaries themselves. The call
  log (``FakeBins.calls()``) lets tests assert on exactly what argv each
  invocation received — needed by later apply/removal tests.
- ``run_script``: helper that runs worktree-cleanup.sh via ``subprocess``,
  merging a given env dict onto the real process environment.

This is the shared harness other worktree-cleanup test modules (scan
categorization, plan caching, apply) build on top of, per the precedent at
``tests/babysit/test_migrate_schema_upgrade.py`` and
``tests/babysit/test_poll_init_errors.py`` for driving a bash script via
subprocess under this repo's pytest config.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT / "skills" / "worktree-cleanup" / "assets" / "worktree-cleanup.sh"
)

# Fake binary source, rendered per-binary via string.Template substitution
# (not str.format) so the JSON/dict literals in the generated body don't
# need brace-escaping. $python is the interpreter to shebang with (so the
# fake runs under the same Python as pytest, regardless of what "python3"
# resolves to on PATH); $bin_name/$responses_env are JSON-encoded string
# literals substituted directly into the generated source.
_FAKE_BIN_TEMPLATE = Template(
    '''#!$python
import json
import os
import sys

BIN_NAME = $bin_name
RESPONSES_ENV = $responses_env


def main():
    argv = sys.argv[1:]

    log_path = os.environ.get("WTC_CALL_LOG")
    if log_path:
        with open(log_path, "a") as fh:
            fh.write(json.dumps({"bin": BIN_NAME, "argv": argv}) + "\\n")

    responses_path = os.environ.get(RESPONSES_ENV)
    rules = []
    if responses_path and os.path.exists(responses_path):
        with open(responses_path) as fh:
            rules = json.load(fh)

    for rule in rules:
        prefix = rule.get("argv_prefix", [])
        if argv[: len(prefix)] == prefix:
            sys.stdout.write(rule.get("stdout", ""))
            stderr = rule.get("stderr", "")
            if stderr:
                sys.stderr.write(stderr)
            sys.exit(rule.get("exit_code", 0))

    # No matching rule: succeed silently. Lets tests that only care about
    # one subcommand (e.g. prereq checks) ignore calls they don't stub.
    sys.exit(0)


main()
'''
)


@dataclass
class FakeBins:
    """Handles for the fake wt/gh binaries written by the fake_bins fixture."""

    bin_dir: Path
    wt_bin: Path
    gh_bin: Path
    call_log: Path
    responses_dir: Path
    env: dict = field(default_factory=dict)

    def calls(self) -> list[dict]:
        """Parse the call log into a list of ``{"bin": ..., "argv": [...]}``.

        Returns an empty list if nothing has been logged yet (e.g. the
        script failed before ever invoking wt/gh).
        """
        if not self.call_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.call_log.read_text().splitlines()
            if line.strip()
        ]

    def set_responses(self, binary: str, rules: Sequence[dict]) -> Path:
        """Write a response-rules JSON fixture for "wt" or "gh" and point
        the matching env var (in ``self.env``) at it.

        Each rule is a dict like
        ``{"argv_prefix": [...], "stdout": "...", "exit_code": 0}``.
        Rules are matched in order against the invocation's argv by
        prefix; the first match wins. Returns the fixture file path.
        """
        if binary not in ("wt", "gh"):
            raise ValueError(f"unknown binary: {binary!r}")
        path = self.responses_dir / f"{binary}-responses.json"
        path.write_text(json.dumps(list(rules)))
        env_var = "WTC_WT_RESPONSES" if binary == "wt" else "WTC_GH_RESPONSES"
        self.env[env_var] = str(path)
        return path


def _write_fake_bin(path: Path, *, bin_name: str, responses_env: str) -> None:
    source = _FAKE_BIN_TEMPLATE.substitute(
        python=sys.executable,
        bin_name=json.dumps(bin_name),
        responses_env=json.dumps(responses_env),
    )
    path.write_text(source)
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def script_path() -> Path:
    """Path to worktree-cleanup.sh."""
    return SCRIPT_PATH


@pytest.fixture
def fake_bins(tmp_path: Path) -> FakeBins:
    """Write fake wt/gh binaries plus a shared call log into a tmp bin dir.

    Merge the returned ``FakeBins.env`` onto the environment passed to
    ``run_script`` to point worktree-cleanup.sh at the fakes instead of
    real wt/gh. Use ``.set_responses()`` beforehand to control what each
    fake echoes for a given subcommand, and ``.calls()`` afterward to
    inspect exactly what argv each invocation received.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    responses_dir = tmp_path / "responses"
    responses_dir.mkdir()

    wt_bin = bin_dir / "wt"
    gh_bin = bin_dir / "gh"
    _write_fake_bin(wt_bin, bin_name="wt", responses_env="WTC_WT_RESPONSES")
    _write_fake_bin(gh_bin, bin_name="gh", responses_env="WTC_GH_RESPONSES")

    call_log = tmp_path / "calls.log"

    return FakeBins(
        bin_dir=bin_dir,
        wt_bin=wt_bin,
        gh_bin=gh_bin,
        call_log=call_log,
        responses_dir=responses_dir,
        env={
            "WTC_WT_BIN": str(wt_bin),
            "WTC_GH_BIN": str(gh_bin),
            "WTC_CALL_LOG": str(call_log),
        },
    )


@pytest.fixture
def run_script(script_path: Path):
    """Returns a callable that runs worktree-cleanup.sh via subprocess.

    Usage: ``run_script(["--format=json"], env={...}, cwd=some_path)``.
    ``env`` (if given) is merged onto a copy of the real process
    environment — so PATH, HOME, etc. stay intact — rather than replacing
    it outright. ``cwd`` (if given) sets the subprocess's working
    directory — needed for ``--apply`` tests, since the drift guard's
    provenance/self-targeting checks (``git worktree list``,
    ``git rev-parse --show-toplevel``) resolve relative to the script's
    own cwd, not any path recorded in the plan.
    """

    def _run(
        args: Sequence[str], env: dict | None = None, cwd: Path | None = None
    ) -> subprocess.CompletedProcess:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        return subprocess.run(
            ["bash", str(script_path), *args],
            env=full_env,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
        )

    return _run
