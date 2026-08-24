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
# Worktree inventory — parse `wt list --format=json` into one normalized
# shape regardless of which on-disk schema `wt` emits.
#
# `wt` v0.74.0 defaults to "schema 1" (a bare JSON array; each element has
# top-level `is_main`/`is_current`/`path`/`working_tree{...}`) but warns on
# stderr that a future release switches the default to "schema 2" (an
# envelope `{schema, repo, collected, items: [...]}`; each item nests the
# same information under `worktree.main`/`worktree.current`/`worktree.path`/
# `worktree.changes{...}`). Both shapes were captured empirically from a
# real `wt v0.74.0` install (see Task 5 report) and are handled here so the
# script keeps working across the schema flip.
#
# The deprecation warning goes to stderr — it is intentionally discarded
# (never merged onto stdout with 2>&1, which would corrupt the JSON we
# parse).
# ---------------------------------------------------------------------------

# wtc_normalize_wt_json <raw-json-on-stdin>
#
# Reads a `wt list --format=json` document (either schema shape) on stdin
# and writes one compact JSON object per line on stdout, one per worktree
# *excluding* the main worktree and the current worktree (`is_main`/
# `is_current`, however schema 2 spells them). Each object carries:
#   branch, path, sha, staged, modified, untracked, renamed, deleted, dirty
# `dirty` is derived (not read from `wt`): true if any of
# staged/modified/untracked/renamed/deleted is true. Untracked counts as
# dirty because `wt remove` refuses an untracked-only worktree without `-f`
# (confirmed empirically in Task 1's spike).
wtc_normalize_wt_json() {
  jq -c '
    def entries: if type == "array" then . else .items end;
    entries
    | map(
        if has("is_main") then
          # schema 1: bare array, flat is_main/is_current/path, working_tree{}
          {
            branch: .branch,
            path: .path,
            sha: .commit.sha,
            is_main: .is_main,
            is_current: .is_current,
            staged: .working_tree.staged,
            modified: .working_tree.modified,
            untracked: .working_tree.untracked,
            renamed: .working_tree.renamed,
            deleted: .working_tree.deleted
          }
        else
          # schema 2: enveloped, is_main/is_current/path/changes nested
          # under .worktree, sha nested under .head
          {
            branch: .branch,
            path: .worktree.path,
            sha: .head.sha,
            is_main: .worktree.main,
            is_current: .worktree.current,
            staged: .worktree.changes.staged,
            modified: .worktree.changes.modified,
            untracked: .worktree.changes.untracked,
            renamed: .worktree.changes.renamed,
            deleted: .worktree.changes.deleted
          }
        end
      )
    | map(select((.is_main | not) and (.is_current | not)))
    | map(. + {dirty: (.staged or .modified or .untracked or .renamed or .deleted)})
    | map(del(.is_main, .is_current))
    | .[]
  '
}

# wtc_ignored_count <worktree-path>
#
# Counts ignored files in the given worktree via
# `git status --ignored --short`, matching lines that start with `!!`
# (git's porcelain marker for an ignored path). Never fails the caller: a
# missing/invalid path just yields 0, since `wt list` output could be
# momentarily stale relative to the filesystem.
wtc_ignored_count() {
  local path="$1" count
  count="$(git -C "$path" status --ignored --short 2>/dev/null | grep -c '^!!' || true)"
  echo "${count:-0}"
}

# wtc_inventory
#
# Fetches `wt list --format=json`, normalizes it (excluding main/current),
# and augments each entry with an `ignored_count` (files ignored per
# `.gitignore`, counted via `wtc_ignored_count`; not read from `wt`, which
# doesn't report it). Writes a single JSON array to stdout.
wtc_inventory() {
  local raw normalized line path count merged
  local -a results

  raw="$("$WT_BIN" list --format=json 2>/dev/null)"

  if [[ -z "$raw" ]]; then
    echo "[]"
    return 0
  fi

  normalized="$(printf '%s' "$raw" | wtc_normalize_wt_json)"

  results=()
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    path="$(jq -r '.path' <<<"$line")"
    count="$(wtc_ignored_count "$path")"
    merged="$(jq -c --argjson ignored "$count" '. + {ignored_count: $ignored}' <<<"$line")"
    results+=("$merged")
  done <<<"$normalized"

  if (( ${#results[@]} > 0 )); then
    printf '%s\n' "${results[@]}" | jq -s -c '.'
  else
    echo "[]"
  fi
}

# ---------------------------------------------------------------------------
# Command stubs — categorization (cross-checking GitHub PR status against
# local commit position) and the plan-cache/apply path land in later tasks.
# cmd_scan wires up repo-context detection (Task 4) together with the
# inventory above (Task 5); it does not yet categorize or cache a plan.
# ---------------------------------------------------------------------------

cmd_scan() {
  local repo_slug default_branch cache_path inventory
  repo_slug="$(detect_repo_slug)"
  default_branch="$(detect_default_branch)"
  cache_path="$(plan_cache_path)"
  inventory="$(wtc_inventory)"

  # TODO(later task): cross-check GitHub PR status via `${GH_BIN}` (using
  # repo_slug/default_branch), categorize each worktree, and write the plan
  # to ${cache_path}. Until then, --format=json surfaces the raw normalized
  # inventory, and the text report is a plain per-worktree listing.
  if [[ "$FORMAT" == "json" ]]; then
    printf '%s\n' "$inventory"
  else
    printf '%s\n' "$inventory" | jq -r '
      if length == 0 then
        "No worktrees to report (only the main/current worktree exists)."
      else
        .[] | "\(.branch)\t\(.path)\tdirty=\(.dirty)\tignored=\(.ignored_count)"
      end
    '
  fi
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
