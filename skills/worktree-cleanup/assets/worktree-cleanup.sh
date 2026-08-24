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
# Command stubs — inventory/categorization/plan-cache logic and the apply
# path land in later tasks. This task only wires up the skeleton so those
# tasks have a place to hook in.
# ---------------------------------------------------------------------------

cmd_scan() {
  # TODO(later task): inventory worktrees via `${WT_BIN} list --format=json`,
  # cross-check GitHub PR status via `${GH_BIN}`, categorize each worktree,
  # write the plan to the cache path, then print the report in $FORMAT.
  echo "TODO: scan not yet implemented (format=${FORMAT})" >&2
  return 1
}

cmd_apply() {
  # TODO(later task): load the cached plan (or $PLAN_PATH if set), filter to
  # $CATEGORIES, and remove each via
  # `${WT_BIN} remove --no-delete-branch <worktree>`.
  echo "TODO: apply not yet implemented (categories=${CATEGORIES:-<all>}, plan=${PLAN_PATH:-<cached>})" >&2
  return 1
}

main() {
  if [[ "$APPLY" == "true" ]]; then
    cmd_apply
  else
    cmd_scan
  fi
}

main
