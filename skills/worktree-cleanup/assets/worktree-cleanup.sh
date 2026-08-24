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
DEBUG_LOOKUP_PR=""
DEBUG_CATEGORIZE=false

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
    --debug-lookup-pr=*)
      # Internal/debug hook, intentionally undocumented in `--help`: runs
      # wtc_lookup_pr for each comma-separated branch in order (repo_slug
      # resolved via detect_repo_slug) and prints one JSON result line per
      # branch, then exits. A branch repeated in the list exercises the
      # per-run cache (Task 6). Used to unit-test the PR lookup layer
      # independently of scan/apply/categorization.
      DEBUG_LOOKUP_PR="${1#--debug-lookup-pr=}"
      shift
      ;;
    --debug-categorize)
      # Internal/debug hook, intentionally undocumented in `--help`: runs
      # the full inventory + categorization ladder (Task 7) against the
      # current repo context and prints one categorized-entry JSON line
      # per worktree, then exits. Used to unit-test the ladder
      # (open/merged/closed/needs_review/dirty-override) independently of
      # cmd_scan's text/json report formatting.
      DEBUG_CATEGORIZE=true
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
# PR lookup layer — per-branch `gh pr list` lookup with selection and
# caching. Later categorization tasks (7, 8) build the removability ladder
# on top of this; this layer only answers "what is THE pull request for
# this head branch, if any" — it never decides what to do with the answer.
#
# wtc_lookup_pr <branch> <repo_slug> prints exactly one line of JSON to
# stdout, one of:
#   {"category":"error"}                                   gh exited non-zero
#   {"category":"no_pr"}                                    gh succeeded, 0 PRs match
#   {"category":"pr","state":...,"number":...,"title":...,
#    "url":...,"headRefOid":...}                            gh succeeded, PR selected
#
# A non-zero `gh` exit always yields "error" — it never falls through to
# "no_pr" (a real API/auth/network failure must not be silently treated as
# "nothing to clean up here").
#
# Selection precedence (this layer owns it; Task 7's ladder never sees more
# than one PR per branch and never re-derives this): a head branch can
# carry more than one PR after being reused (e.g. an earlier PR was closed,
# then the same branch was pushed again and opened a new PR). Among all
# PRs matching the head branch:
#   - any OPEN PR wins outright, regardless of the other PRs' states;
#   - otherwise the most recently MERGED PR wins;
#   - otherwise the most recently CLOSED PR wins.
# `gh` reports state in uppercase (OPEN/MERGED/CLOSED); comparison here is
# case-insensitive regardless.
#
# Caching: results are cached per-branch for the lifetime of the current
# script invocation (module-level indexed arrays — bash 3.2 has no
# associative arrays) so repeated lookups of the same branch within one run
# never re-invoke `gh`.
# ---------------------------------------------------------------------------
WTC_PR_CACHE_BRANCHES=()
WTC_PR_CACHE_RESULTS=()

# wtc_pr_cache_get <branch>
#
# Prints the cached result for <branch> and returns 0 on a cache hit;
# returns 1 (prints nothing) on a miss.
wtc_pr_cache_get() {
  local branch="$1" i
  for (( i=0; i<${#WTC_PR_CACHE_BRANCHES[@]}; i++ )); do
    if [[ "${WTC_PR_CACHE_BRANCHES[$i]}" == "$branch" ]]; then
      printf '%s\n' "${WTC_PR_CACHE_RESULTS[$i]}"
      return 0
    fi
  done
  return 1
}

# wtc_pr_cache_set <branch> <result-json>
wtc_pr_cache_set() {
  WTC_PR_CACHE_BRANCHES+=("$1")
  WTC_PR_CACHE_RESULTS+=("$2")
}

# wtc_select_pr <raw-json-array-on-stdin>
#
# Applies the open > merged > closed precedence (ties broken by most
# recent mergedAt/closedAt/updatedAt) to a `gh pr list --json ...` array and
# prints the single selected-PR result object described above. Split out
# from wtc_lookup_pr so the selection logic itself is a pure, independently
# testable jq transform.
wtc_select_pr() {
  jq -c '
    def rank:
      (.state | ascii_downcase) as $s
      | if $s == "open" then 0
        elif $s == "merged" then 1
        else 2
        end;
    def recency: (.mergedAt // .closedAt // .updatedAt // "");
    if length == 0 then
      {category: "no_pr"}
    else
      (map(rank) | min) as $minrank
      | (map(select(rank == $minrank)) | sort_by(recency) | .[-1]) as $winner
      | {
          category: "pr",
          state: $winner.state,
          number: $winner.number,
          title: $winner.title,
          url: $winner.url,
          headRefOid: $winner.headRefOid
        }
    end
  '
}

# wtc_lookup_pr <branch> <repo_slug>
#
# See section header above for the returned JSON shapes and precedence
# contract. Caches its result per-branch (see wtc_pr_cache_get/set).
wtc_lookup_pr() {
  local branch="$1" repo_slug="$2" cached raw result

  if cached="$(wtc_pr_cache_get "$branch")"; then
    printf '%s\n' "$cached"
    return 0
  fi

  if raw="$("$GH_BIN" pr list --repo "$repo_slug" --head "$branch" --state all \
      --json state,number,title,url,headRefOid,mergedAt,closedAt,updatedAt \
      2>/dev/null)"; then
    result="$(printf '%s' "$raw" | wtc_select_pr)"
  else
    result='{"category":"error"}'
  fi

  wtc_pr_cache_set "$branch" "$result"
  printf '%s\n' "$result"
}

# ---------------------------------------------------------------------------
# Categorization ladder (Task 7) — for each inventoried worktree, decides
# safety-to-remove by combining wtc_lookup_pr's single selected PR (above)
# with a local-vs-remote tip check, then applies the dirty override on top.
#
# Ladder, in order:
#   error     -- wtc_lookup_pr's `gh` call failed. Informational; not safe.
#   open      -- an open PR exists for this branch. Informational, NEVER
#                removable, regardless of anything else about the branch.
#   merged    -- the selected PR is merged AND the local branch tip is the
#                merged PR's headRefOid, or an ancestor of it (i.e. local
#                has nothing beyond what was merged). Safe to remove.
#   closed    -- same tip-check, against a closed (not merged) PR. Safe to
#                remove.
#   needs_review -- merged/closed PR selected, but the tip-check found the
#                local branch has commits beyond the PR's headRefOid (an
#                "N commits ahead of the {merged,closed} PR." reason is
#                attached). Not safe -- a human should look before removing.
#   (no_pr)   -- wtc_lookup_pr found no PR at all, ever, for this head
#                branch. This ladder does not decide anything for that
#                case itself -- see wtc_categorize_no_pr and
#                wtc_compute_no_pr_categories below (Task 8):
#     empty      -- the branch's HEAD sha is an ancestor of (or equal to)
#                   the default branch -- zero unique commits. Safe.
#     duplicate  -- among no-PR/non-empty branches, >1 share the exact
#                   same HEAD sha; all but the most-recently-touched one
#                   in that group. Safe.
#     needs_review -- either the most-recently-touched branch in a
#                   duplicate-sha group, or a singleton (unique sha,
#                   not an ancestor of default). Never auto-removed.
#
# Dirty override (applied last, unconditionally): if the entry's `dirty`
# flag is true, the category above is discarded and replaced with
# "dirty_skipped", no matter what the ladder produced -- including
# overriding "open" and "merged". This exists as defense-in-depth per
# Task 1's empirical finding that `wt remove --no-delete-branch --foreground`
# (never passed `-f`/`--force` by this script) already refuses any dirty
# worktree on its own -- but the categorization layer still needs to
# surface "dirty_skipped" as its own reported category (not just rely on
# apply-time failure), and later apply logic (Task 12) must independently
# re-check dirtiness before removing rather than trusting a stale plan.
# ---------------------------------------------------------------------------

# wtc_check_tip <path> <local_sha> <remote_sha>
#
# Prints one of:
#   safe          -- local_sha == remote_sha, or local_sha is an ancestor
#                    of remote_sha (local has nothing beyond remote).
#   ahead:<N>     -- local_sha has diverged/advanced past remote_sha by N
#                    commits (`git rev-list --count remote_sha..local_sha`).
#   unknown       -- the ahead-count itself could not be determined (e.g.
#                    remote_sha isn't a known object in this worktree's
#                    local object database -- possible if the remote ref
#                    was never fetched). Never fails the caller.
wtc_check_tip() {
  local path="$1" local_sha="$2" remote_sha="$3" count

  if [[ "$local_sha" == "$remote_sha" ]]; then
    echo "safe"
    return 0
  fi

  if git -C "$path" merge-base --is-ancestor "$local_sha" "$remote_sha" 2>/dev/null; then
    echo "safe"
    return 0
  fi

  if count="$(git -C "$path" rev-list --count "${remote_sha}..${local_sha}" 2>/dev/null)" \
      && [[ -n "$count" ]]; then
    echo "ahead:${count}"
  else
    echo "unknown"
  fi
}

# wtc_ladder_pr_category <path> <local_sha> <remote_sha> <state>
#
# <state> is "merged" or "closed" (lowercase) -- doubles as both the safe
# category name and the label used in the needs_review reason string.
# Prints a compact JSON object: {"category":"merged"|"closed"} when safe,
# or {"category":"needs_review","reason":"..."} otherwise.
wtc_ladder_pr_category() {
  local path="$1" local_sha="$2" remote_sha="$3" state="$4" check n

  check="$(wtc_check_tip "$path" "$local_sha" "$remote_sha")"

  case "$check" in
    safe)
      jq -c -n --arg cat "$state" '{category: $cat}'
      ;;
    unknown)
      jq -c -n --arg reason \
        "unable to determine commit position relative to the ${state} PR's head ref (${remote_sha:0:8} not found in local history)." \
        '{category: "needs_review", reason: $reason}'
      ;;
    *)
      n="${check#ahead:}"
      jq -c -n --arg reason "${n} commits ahead of the ${state} PR." \
        '{category: "needs_review", reason: $reason}'
      ;;
  esac
}

# wtc_dir_mtime <path>
#
# "Most-recently-touched" signal for duplicate-sha grouping below: prints
# the worktree directory's own mtime (epoch seconds). Deliberately NOT the
# commit's timestamp -- by construction every branch in a duplicate-sha
# group shares the exact same HEAD commit, so the commit's author/committer
# date is identical across the whole group and can't distinguish them.
# Directory mtime is a cheap, good-enough proxy for "which of these
# worktrees was created/touched most recently" (git bumps a directory's
# mtime on checkout/file changes within it); it's not perfect (e.g. an
# unrelated `touch` inside the worktree would perturb it) but this whole
# pattern is based on a single observed occurrence (a 17-branch fanned-out
# agent run collapsing to 2 distinct commits) -- see the plan notes -- so
# it isn't worth a more elaborate signal. Never fails the caller: a
# missing/unreadable path yields 0 (bash 3.2 / macOS system `stat`, hence
# `-f %m` rather than GNU `-c %Y`; a `-c %Y` fallback is included in case
# this ever runs under a Linux `stat`).
wtc_dir_mtime() {
  local path="$1" mtime
  mtime="$(stat -f %m "$path" 2>/dev/null || stat -c %Y "$path" 2>/dev/null || true)"
  echo "${mtime:-0}"
}

# wtc_compute_no_pr_categories <no_pr_entries_json_array> <default_branch>
#
# The multi-entry half of Task 8's detection: decides empty/duplicate/
# needs_review for a *batch* of no-PR entries at once (duplicate-sha
# grouping is inherently cross-entry -- it needs to see every no-PR
# branch's HEAD sha together to find the groups), then hands each entry's
# per-branch verdict to wtc_categorize_no_pr as a precomputed map so that
# function can stay a simple per-entry lookup (see its header comment for
# why this split, rather than doing the grouping inside the per-entry
# function itself).
#
# For each input entry (each has at least branch/path/sha):
#   1. empty: `git -C <path> merge-base --is-ancestor <sha> <default_branch>`
#      succeeds -- the branch's HEAD has zero commits beyond the default
#      branch's tip.
#   2. Otherwise, group the remaining entries by HEAD sha. A group of 1
#      (a "singleton") -> needs_review, no reason. A group of >1 (a
#      "duplicate-sha group") -> the entry with the greatest
#      wtc_dir_mtime (ties broken alphabetically by branch, for
#      determinism) -> needs_review with a reason citing the group; every
#      other entry in the group -> duplicate with a reason naming the kept
#      branch.
#
# Prints a single compact JSON object mapping branch -> {"category":...,
# "reason":...?} for every input entry. Prints "{}" for an empty input
# array.
wtc_compute_no_pr_categories() {
  local entries_json="$1" default_branch="$2"
  local line branch path sha is_ancestor mtime row
  local -a rows

  rows=()
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    branch="$(jq -r '.branch' <<<"$line")"
    path="$(jq -r '.path' <<<"$line")"
    sha="$(jq -r '.sha' <<<"$line")"

    if git -C "$path" merge-base --is-ancestor "$sha" "$default_branch" 2>/dev/null; then
      is_ancestor=true
    else
      is_ancestor=false
    fi
    mtime="$(wtc_dir_mtime "$path")"

    row="$(jq -c -n --arg branch "$branch" --arg sha "$sha" \
      --argjson is_ancestor "$is_ancestor" --argjson mtime "$mtime" \
      '{branch: $branch, sha: $sha, is_ancestor: $is_ancestor, mtime: $mtime}')"
    rows+=("$row")
  done <<<"$(jq -c '.[]' <<<"$entries_json")"

  if (( ${#rows[@]} == 0 )); then
    echo '{}'
    return 0
  fi

  printf '%s\n' "${rows[@]}" | jq -s -c '
    (map(select(.is_ancestor)) | reduce .[] as $e ({}; . + {($e.branch): {category: "empty"}})) as $empty_map
    | (map(select(.is_ancestor | not))) as $candidates
    | ($candidates
        | group_by(.sha)
        | reduce .[] as $group ({};
            if ($group | length) == 1 then
              . + {($group[0].branch): {category: "needs_review"}}
            else
              ($group | sort_by([-.mtime, .branch])) as $sorted
              | ($sorted[0].branch) as $kept
              | ($sorted[1:] | map(.branch)) as $dups
              | ($sorted | length) as $n
              | . + {($kept): {category: "needs_review",
                      reason: ("kept as most-recently-touched of " + ($n|tostring)
                        + " branches sharing this HEAD commit (others: " + ($dups | join(", ")) + ").")}}
                + (reduce $dups[] as $d ({}; . + {($d): {category: "duplicate",
                      reason: ("duplicate HEAD commit shared with " + (($n - 1)|tostring)
                        + " other branch(es); " + $kept + " kept as most-recently-touched.")}}))
            end
          )
      ) as $dup_map
    | $empty_map + $dup_map
  '
}

# wtc_categorize_no_pr <entry_json> <no_pr_map_json>
#
# Called whenever wtc_lookup_pr returns {"category":"no_pr"} for a branch
# -- i.e. no PR, of any state, has ever matched this head branch. This
# ladder (Task 7) deliberately does not decide empty/duplicate/etc.
# itself, because duplicate-sha grouping is inherently a multi-entry
# operation (see wtc_compute_no_pr_categories above) while this function
# is invoked per-entry from wtc_categorize_entry. Rather than re-deriving
# the group from scratch per entry, the caller (wtc_categorize_all) runs
# wtc_compute_no_pr_categories once over every no-PR entry up front and
# threads the resulting branch -> verdict map through wtc_categorize_entry
# into here -- this function only has to do the lookup.
#
# $1 is the full normalized inventory entry (branch, path, sha, dirty,
# staged/modified/untracked/renamed/deleted, ignored_count) as a single
# JSON object, matching wtc_categorize_entry's other callees. $2 is the
# precomputed map produced by wtc_compute_no_pr_categories. Prints exactly
# one compact JSON object on stdout carrying at least a "category" key
# (e.g. {"category":"empty"} or {"category":"duplicate","reason":"..."}),
# consistent with the shapes wtc_categorize_entry merges onto the entry.
# Falls back to a bare "needs_review" (never "safe" to apply) if the
# branch is somehow missing from the map -- defensive only, should be
# unreachable since wtc_categorize_all builds the map from the exact same
# no-PR entries it calls this function for.
wtc_categorize_no_pr() {
  local entry_json="$1" no_pr_map_json="$2" branch
  branch="$(jq -r '.branch' <<<"$entry_json")"
  jq -c --arg branch "$branch" '.[$branch] // {category: "needs_review"}' <<<"$no_pr_map_json"
}

# wtc_categorize_entry <entry_json> <repo_slug> <no_pr_map_json>
#
# Runs the full ladder (including the dirty override) for one normalized
# inventory entry (as produced by wtc_inventory) and prints the entry with
# categorization fields merged on top: at minimum "category", plus
# "reason" (needs_review, dirty_skipped) and/or "pr_number"/"pr_title"/
# "pr_url" (whenever a PR was selected, so reports can cite it) when
# applicable.
#
# <no_pr_map_json> is the precomputed branch -> verdict map from
# wtc_compute_no_pr_categories (see wtc_categorize_all below), forwarded
# unchanged to wtc_categorize_no_pr whenever this entry has no PR.
wtc_categorize_entry() {
  local entry_json="$1" repo_slug="$2" no_pr_map_json="$3"
  local branch path sha dirty lookup lookup_category state remote_sha ladder

  branch="$(jq -r '.branch' <<<"$entry_json")"
  path="$(jq -r '.path' <<<"$entry_json")"
  sha="$(jq -r '.sha' <<<"$entry_json")"
  dirty="$(jq -r '.dirty' <<<"$entry_json")"

  lookup="$(wtc_lookup_pr "$branch" "$repo_slug")"
  lookup_category="$(jq -r '.category' <<<"$lookup")"

  if [[ "$lookup_category" == "error" ]]; then
    ladder='{"category":"error"}'
  elif [[ "$lookup_category" == "no_pr" ]]; then
    ladder="$(wtc_categorize_no_pr "$entry_json" "$no_pr_map_json")"
  else
    state="$(jq -r '.state' <<<"$lookup" | tr '[:upper:]' '[:lower:]')"
    case "$state" in
      open)
        ladder="$(jq -c '{category: "open", pr_number: .number, pr_title: .title, pr_url: .url}' <<<"$lookup")"
        ;;
      merged|closed)
        remote_sha="$(jq -r '.headRefOid' <<<"$lookup")"
        ladder="$(wtc_ladder_pr_category "$path" "$sha" "$remote_sha" "$state")"
        ladder="$(jq -c --argjson base "$ladder" \
          '$base + {pr_number: .number, pr_title: .title, pr_url: .url}' <<<"$lookup")"
        ;;
      *)
        # Defensive only: `gh` only ever reports OPEN/MERGED/CLOSED for
        # pull requests, so this should be unreachable in practice.
        ladder="$(jq -c -n --arg state "$state" \
          '{category: "needs_review", reason: ("unrecognized PR state: " + $state)}')"
        ;;
    esac
  fi

  # Dirty override: applied last, unconditionally, discarding whatever
  # the ladder produced above -- see the section header comment.
  if [[ "$dirty" == "true" ]]; then
    ladder='{"category":"dirty_skipped","reason":"worktree has uncommitted changes (dirty override)"}'
  fi

  jq -c --argjson base "$ladder" '. + $base' <<<"$entry_json"
}

# wtc_categorize_all <inventory_json_array> <repo_slug> <default_branch>
#
# Runs wtc_categorize_entry over every entry in an inventory array (as
# produced by wtc_inventory) and prints the categorized result as a single
# JSON array. A bash loop (not a pure jq map) because wtc_categorize_entry
# calls wtc_lookup_pr, which mutates the module-level PR cache and shells
# out to git/gh per entry.
#
# Before that per-entry pass, runs a first pass over the same inventory to
# find every entry whose wtc_lookup_pr result is "no_pr" (a cheap,
# cache-only re-check -- wtc_lookup_pr already memoized the branch ->
# lookup result on the first call in this run, so this never triggers a
# second `gh` invocation per branch) and feeds that batch to
# wtc_compute_no_pr_categories once, up front. This is the resolution to
# Task 8's core wrinkle: duplicate-sha grouping needs to see every no-PR
# entry at once, but wtc_categorize_entry's loop below processes entries
# one at a time -- so the multi-entry grouping decision is made here, in a
# single pass, and handed into the per-entry pass as a precomputed map
# (see wtc_categorize_no_pr's header comment) rather than trying to make
# wtc_categorize_no_pr re-derive its sibling group per call.
wtc_categorize_all() {
  local inventory_json="$1" repo_slug="$2" default_branch="$3"
  local line branch lookup lookup_category result no_pr_map_json
  local -a no_pr_entries results

  no_pr_entries=()
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    branch="$(jq -r '.branch' <<<"$line")"
    lookup="$(wtc_lookup_pr "$branch" "$repo_slug")"
    lookup_category="$(jq -r '.category' <<<"$lookup")"
    if [[ "$lookup_category" == "no_pr" ]]; then
      no_pr_entries+=("$line")
    fi
  done <<<"$(jq -c '.[]' <<<"$inventory_json")"

  if (( ${#no_pr_entries[@]} > 0 )); then
    no_pr_map_json="$(wtc_compute_no_pr_categories \
      "$(printf '%s\n' "${no_pr_entries[@]}" | jq -s -c '.')" \
      "$default_branch")"
  else
    no_pr_map_json="{}"
  fi

  results=()
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    result="$(wtc_categorize_entry "$line" "$repo_slug" "$no_pr_map_json")"
    results+=("$result")
  done <<<"$(jq -c '.[]' <<<"$inventory_json")"

  if (( ${#results[@]} > 0 )); then
    printf '%s\n' "${results[@]}" | jq -s -c '.'
  else
    echo "[]"
  fi
}

# ---------------------------------------------------------------------------
# Plan cache write (Task 9) — assembles the final plan JSON (metadata plus
# categorized entries[]) and writes it atomically to the Task 4 cache path:
# write to a sibling temp file, then `mv` it into place, so a reader (the
# later --apply path, or a concurrent scan) never observes a partially
# written cache file.
# ---------------------------------------------------------------------------

# wtc_now
#
# Prints the plan's `generated_at` timestamp: honors WTC_NOW (an injectable
# override, e.g. for deterministic tests) when set and non-empty; otherwise
# the current UTC time as ISO 8601 (`date -u +%Y-%m-%dT%H:%M:%SZ`).
wtc_now() {
  if [[ -n "${WTC_NOW:-}" ]]; then
    printf '%s' "$WTC_NOW"
  else
    date -u +%Y-%m-%dT%H:%M:%SZ
  fi
}

# wtc_build_plan_json <categorized_entries_json_array> <repo_slug> <default_branch>
#
# Assembles and prints the plan JSON object --
#   {"generated_at":..., "repo":..., "default_branch":..., "entries":[...]}
# -- the single source of truth for the plan's shape. Both the cache
# writer (wtc_write_plan_cache, below) and cmd_scan's `--format=json`
# stdout printer call this exact function, so the object written to disk
# and the object printed to stdout are always byte-for-byte identical --
# never built twice and never allowed to drift apart.
wtc_build_plan_json() {
  local categorized_json="$1" repo_slug="$2" default_branch="$3"
  jq -c -n \
    --arg generated_at "$(wtc_now)" \
    --arg repo "$repo_slug" \
    --arg default_branch "$default_branch" \
    --argjson entries "$categorized_json" \
    '{generated_at: $generated_at, repo: $repo, default_branch: $default_branch, entries: $entries}'
}

# wtc_write_plan_cache <plan_json> <cache_path>
#
# Writes the already-assembled plan JSON (see wtc_build_plan_json)
# atomically to <cache_path>: first to a temp file created via `mktemp` in
# the *same directory* as <cache_path> (atomic `mv` requires same
# filesystem), then `mv`'d over the final path. The temp file is cleaned
# up if the write to it fails, so no stray temp file is ever left behind
# on either the success or failure path.
wtc_write_plan_cache() {
  local plan_json="$1" cache_path="$2"
  local cache_dir tmp_file

  cache_dir="$(dirname "$cache_path")"
  tmp_file="$(mktemp "${cache_dir}/.worktree-cleanup-plan.XXXXXX")"

  if ! printf '%s\n' "$plan_json" > "$tmp_file"; then
    rm -f "$tmp_file"
    return 1
  fi

  mv "$tmp_file" "$cache_path"
}

# ---------------------------------------------------------------------------
# Commands — cmd_scan wires up repo-context detection (Task 4), the
# inventory (Task 5), the categorization ladder (Tasks 7-8), and the plan
# cache write (Task 9, above). cmd_apply's load-and-remove path lands in a
# later task.
# ---------------------------------------------------------------------------

# wtc_emit_scan_warnings <categorized_entries_json_array>
#
# Task 11: stdout/stderr discipline. With --format=json, cmd_scan's stdout
# must carry the plan JSON and nothing else — any diagnostic/warning
# chatter about the scan has to go to stderr instead, so a consumer (e.g.
# the skill invoking this script with --format=json) can parse stdout
# directly without stripping anything out.
#
# This prints one warning line per "error" category entry (a `gh` call
# failed for that branch's PR lookup — see wtc_lookup_pr) to stderr, naming
# the branch. It is called unconditionally from cmd_scan (both formats),
# never touches stdout, and is a no-op when there are no "error" entries.
wtc_emit_scan_warnings() {
  local categorized_json="$1" line branch
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    branch="$(jq -r '.branch' <<<"$line")"
    echo "WARNING: PR lookup failed for branch '${branch}' (gh error); categorized as 'error' -- verify manually." >&2
  done <<<"$(jq -c '.[] | select(.category == "error")' <<<"$categorized_json")"
}

cmd_scan() {
  local repo_slug default_branch cache_path inventory categorized plan_json
  repo_slug="$(detect_repo_slug)"
  default_branch="$(detect_default_branch)"
  cache_path="$(plan_cache_path)"
  inventory="$(wtc_inventory)"
  categorized="$(wtc_categorize_all "$inventory" "$repo_slug" "$default_branch")"

  # Built once (wtc_build_plan_json) and reused for both the cache write
  # and the --format=json stdout print below, so the two are always
  # identical -- see that function's header comment.
  plan_json="$(wtc_build_plan_json "$categorized" "$repo_slug" "$default_branch")"

  wtc_write_plan_cache "$plan_json" "$cache_path"

  wtc_emit_scan_warnings "$categorized"

  if [[ "$FORMAT" == "json" ]]; then
    # Task 11: stdout carries ONLY the plan JSON here -- no progress or
    # diagnostic text before or after it. Any such chatter (see
    # wtc_emit_scan_warnings above) goes to stderr instead, so a consumer
    # (e.g. the worktree-cleanup skill) can parse this line directly.
    printf '%s\n' "$plan_json"
  else
    printf '%s\n' "$categorized" | jq -r '
      if length == 0 then
        "No worktrees to report (only the main/current worktree exists)."
      else
        .[] | "\(.branch)\t\(.path)\tcategory=\(.category)"
          + (if .reason then "\treason=\(.reason)" else "" end)
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

cmd_debug_lookup_pr() {
  # Internal/debug hook for --debug-lookup-pr=<comma-list>: prints one
  # wtc_lookup_pr JSON result per listed branch, in order (see: Task 6
  # verification step).
  local repo_slug branch
  repo_slug="$(detect_repo_slug)"
  local IFS=','
  for branch in $DEBUG_LOOKUP_PR; do
    wtc_lookup_pr "$branch" "$repo_slug"
  done
}

cmd_debug_categorize() {
  # Internal/debug hook for --debug-categorize: runs the inventory +
  # categorization ladder against the current repo context and prints one
  # categorized-entry JSON line per worktree (see: Task 7 verification
  # step). Unlike cmd_scan, this bypasses --format entirely -- always one
  # compact JSON object per line, never an array -- to keep ad-hoc/test
  # assertions on individual entries simple.
  local repo_slug default_branch categorized
  repo_slug="$(detect_repo_slug)"
  default_branch="$(detect_default_branch)"
  categorized="$(wtc_categorize_all "$(wtc_inventory)" "$repo_slug" "$default_branch")"
  printf '%s\n' "$categorized" | jq -c '.[]'
}

main() {
  if [[ "$DEBUG_CONTEXT" == "true" ]]; then
    cmd_debug_context
  elif [[ -n "$DEBUG_LOOKUP_PR" ]]; then
    cmd_debug_lookup_pr
  elif [[ "$DEBUG_CATEGORIZE" == "true" ]]; then
    cmd_debug_categorize
  elif [[ "$APPLY" == "true" ]]; then
    cmd_apply
  else
    cmd_scan
  fi
}

main
