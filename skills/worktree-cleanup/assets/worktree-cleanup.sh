#!/usr/bin/env bash
#
# worktree-cleanup.sh — inventory, categorize, and clean up git worktrees.
#
# Scans all git worktrees for the current repo, categorizes each by
# safety-to-remove (cross-checking GitHub PR status against local commit
# position and duplicate/ancestor checks), caches the result as a JSON plan,
# and removes worktrees in selected categories on request.
#
# Usage:
#   worktree-cleanup.sh [--format=text|json]
#   worktree-cleanup.sh --apply [--categories=<comma-list>] [--plan=<path>]
#
# With no --apply: scan, write the plan to a cache path, print the
# human-readable report (or raw JSON with --format=json). Nothing is
# removed.
#
# With --apply: does not re-scan. Loads the most recently cached plan (or
# the one at --plan=<path>) and removes worktree branches in the selected
# --categories via `wt remove --no-delete-branch`.
#
# Target: macOS system bash 3.2. No associative arrays, no `mapfile`.
set -euo pipefail

# ---------------------------------------------------------------------------
# Tool resolution
#
# wt and gh are resolved through env var indirection (rather than calling
# `wt`/`gh` directly everywhere) so tests can inject fake binaries by
# setting WTC_WT_BIN / WTC_GH_BIN without touching PATH.
# ---------------------------------------------------------------------------
WT_BIN="${WTC_WT_BIN:-wt}"
GH_BIN="${WTC_GH_BIN:-gh}"

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"

usage() {
  cat <<EOF
Usage:
  ${SCRIPT_NAME} [--format=text|json]
  ${SCRIPT_NAME} --apply [--categories=<comma-list>] [--plan=<path>]

Scan all git worktrees for this repo, categorize each by safety-to-remove
(cross-checking GitHub PR status against local commit position), and either
print a report (default) or apply a previously cached plan (--apply).

Options:
  --format=text|json    Output format for the scan report. Default: text.
                         Ignored with --apply.
  --apply                Apply a previously cached plan: remove worktrees in
                          the selected categories via
                          'wt remove --no-delete-branch'. Does not re-scan.
  --categories=<list>    Comma-separated list of categories to apply.
                         Only valid with --apply.
  --plan=<path>          Path to a plan file to apply, overriding the most
                         recently cached plan. Only valid with --apply.
  -h, --help             Show this help message and exit.

Environment:
  WTC_WT_BIN   Override the 'wt' binary used (default: wt).
  WTC_GH_BIN   Override the 'gh' binary used (default: gh).
EOF
}

fail_usage() {
  # Prints an error to stderr followed by usage, then exits non-zero.
  echo "ERROR: $1" >&2
  echo >&2
  usage >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Flag parsing — case/shift (no getopts: need --flag=value support that
# getopts does not handle cleanly).
# ---------------------------------------------------------------------------
FORMAT="text"
APPLY=false
CATEGORIES=""
PLAN_PATH=""
DEBUG_CONTEXT=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --format=*)
      FORMAT="${1#--format=}"
      shift
      ;;
    --apply)
      APPLY=true
      shift
      ;;
    --categories=*)
      CATEGORIES="${1#--categories=}"
      shift
      ;;
    --plan=*)
      PLAN_PATH="${1#--plan=}"
      shift
      ;;
    --debug-context)
      # Internal/debug hook, intentionally undocumented in `--help`: prints
      # the resolved owner/repo, default branch, and plan cache path, then
      # exits. Used to verify repo-context detection independently of the
      # scan/apply flows (e.g. across worktrees of the same repo).
      DEBUG_CONTEXT=true
      shift
      ;;
    *)
      fail_usage "Unknown flag: $1"
      ;;
  esac
done

if [[ "$FORMAT" != "text" && "$FORMAT" != "json" ]]; then
  fail_usage "Invalid --format value: '${FORMAT}' (expected 'text' or 'json')"
fi

if [[ -n "$CATEGORIES" && "$APPLY" != "true" ]]; then
  fail_usage "--categories is only valid with --apply"
fi

if [[ -n "$PLAN_PATH" && "$APPLY" != "true" ]]; then
  fail_usage "--plan is only valid with --apply"
fi

# ---------------------------------------------------------------------------
# Prerequisite checks — fail fast, before doing anything else. Checked in
# order: gh, wt, jq, then confirm we're inside a git repo. Each failure
# names the missing tool and its install URL, then exits 1.
# ---------------------------------------------------------------------------
require_tool() {
  local bin="$1" label="$2" url="$3"
  if ! which "$bin" >/dev/null 2>&1; then
    echo "ERROR: required tool '${label}' not found on PATH (looked for '${bin}')." >&2
    echo "Install it from: ${url}" >&2
    exit 1
  fi
}

check_prereqs() {
  require_tool "$GH_BIN" "gh" "https://cli.github.com/"
  require_tool "$WT_BIN" "wt (worktrunk)" "https://github.com/max-sixty/worktrunk"
  require_tool "jq" "jq" "https://jqlang.org/download/"

  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "ERROR: not inside a git repository (git rev-parse --is-inside-work-tree failed)." >&2
    exit 1
  fi
}

check_prereqs

# ---------------------------------------------------------------------------
# Repo context detection — owner/repo, default branch, and the plan cache
# path. Operates on the repo containing the current working directory.
# ---------------------------------------------------------------------------

detect_repo_slug() {
  # Detects "owner/repo" via `gh repo view`. On failure (not a GitHub repo,
  # gh not authenticated, no network, etc.), fails with a clear message
  # naming the command that failed and gh's own error output.
  local slug
  if ! slug="$("$GH_BIN" repo view --json nameWithOwner --jq .nameWithOwner 2>&1)"; then
    echo "ERROR: failed to detect GitHub repo via '${GH_BIN} repo view --json nameWithOwner --jq .nameWithOwner':" >&2
    echo "  ${slug}" >&2
    exit 1
  fi
  echo "$slug"
}

detect_default_branch() {
  # Parses `refs/remotes/origin/HEAD` for the default branch name, falling
  # back to "main" when the symbolic ref isn't set (e.g. a fresh clone with
  # `git clone --single-branch`, or no `origin` remote at all).
  local ref
  if ref="$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null)"; then
    echo "${ref#refs/remotes/origin/}"
  else
    echo "main"
  fi
}

plan_cache_path() {
  # Resolves the plan cache path from `--git-common-dir`, NOT `--git-dir`.
  # `--git-dir` resolves per-worktree (`<repo>/.git/worktrees/<name>`), which
  # would break scan-here/apply-there across worktrees of the same repo.
  # `--git-common-dir` resolves to the same absolute path from every
  # worktree of a given repo (verified empirically in Task 4).
  local common_dir
  common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
  echo "${common_dir}/worktree-cleanup-plan.json"
}

# ---------------------------------------------------------------------------
# Command stubs — inventory/categorization logic and the apply path land in
# later tasks. This task only wires repo-context detection up ahead of
# those stubs so those tasks have resolved values to hook in.
# ---------------------------------------------------------------------------

cmd_scan() {
  local repo_slug default_branch cache_path
  repo_slug="$(detect_repo_slug)"
  default_branch="$(detect_default_branch)"
  cache_path="$(plan_cache_path)"

  # TODO(later task): inventory worktrees via `${WT_BIN} list --format=json`,
  # cross-check GitHub PR status via `${GH_BIN}`, categorize each worktree,
  # write the plan to ${cache_path}, then print the report in $FORMAT.
  echo "TODO: scan not yet implemented (format=${FORMAT}, repo=${repo_slug}, default_branch=${default_branch}, cache=${cache_path})" >&2
  return 1
}

cmd_apply() {
  local cache_path
  cache_path="${PLAN_PATH:-$(plan_cache_path)}"

  # TODO(later task): load the cached plan from ${cache_path}, filter to
  # $CATEGORIES, and remove each via
  # `${WT_BIN} remove --no-delete-branch <worktree>`.
  echo "TODO: apply not yet implemented (categories=${CATEGORIES:-<all>}, plan=${cache_path})" >&2
  return 1
}

cmd_debug_context() {
  # Internal/debug hook for --debug-context: prints resolved repo context
  # so it can be verified directly (see: Task 4 verification step).
  echo "repo_slug=$(detect_repo_slug)"
  echo "default_branch=$(detect_default_branch)"
  echo "plan_cache_path=$(plan_cache_path)"
}

main() {
  if [[ "$DEBUG_CONTEXT" == "true" ]]; then
    cmd_debug_context
  elif [[ "$APPLY" == "true" ]]; then
    cmd_apply
  else
    cmd_scan
  fi
}

main
