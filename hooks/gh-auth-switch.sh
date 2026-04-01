#!/usr/bin/env bash
# PreToolUse hook: auto-switch gh CLI auth based on repo org.
# Reads tool input JSON from stdin. If the Bash command involves `gh`,
# checks repo org against active gh account and switches if mismatched.

set -euo pipefail

# Read hook input from stdin
INPUT=$(cat)

# Only act on Bash tool calls
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
if [[ "$TOOL_NAME" != "Bash" ]]; then
  exit 0
fi

# Extract the command; exit if it doesn't involve gh
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
if [[ -z "$COMMAND" ]] || ! echo "$COMMAND" | grep -qw 'gh'; then
  exit 0
fi

# Get repo org from git remote
REMOTE_URL=$(git remote get-url origin 2>/dev/null || true)
if [[ -z "$REMOTE_URL" ]]; then
  exit 0
fi

# Extract org/owner from remote URL
# Handles: git@github.com:Org/repo.git, git@github.com-alias:Org/repo.git,
#           https://github.com/Org/repo.git
REPO_ORG=$(echo "$REMOTE_URL" | sed -n 's|.*github\.com[^:]*:\([^/]*\)/.*|\1|p')
if [[ -z "$REPO_ORG" ]]; then
  # HTTPS format
  REPO_ORG=$(echo "$REMOTE_URL" | sed -n 's|.*github\.com/\([^/]*\)/.*|\1|p')
fi

if [[ -z "$REPO_ORG" ]]; then
  exit 0
fi

# Get currently active gh account
ACTIVE_USER=$(gh auth status 2>&1 | grep "Active account: true" -B3 | grep "Logged in" | sed 's/.*account //' | sed 's/ .*//')
if [[ -z "$ACTIVE_USER" ]]; then
  exit 0
fi

# Compare (case-insensitive) and switch if needed
REPO_ORG_LOWER=$(echo "$REPO_ORG" | tr '[:upper:]' '[:lower:]')
ACTIVE_USER_LOWER=$(echo "$ACTIVE_USER" | tr '[:upper:]' '[:lower:]')
if [[ "$REPO_ORG_LOWER" != "$ACTIVE_USER_LOWER" ]]; then
  gh auth switch --user "$REPO_ORG" 2>/dev/null || true
fi

exit 0
