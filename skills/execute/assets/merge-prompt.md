# Worktree Branch Merge Guide

When sub-agents run with `isolation: "worktree"`, their changes land on temporary branches. Use this guide to merge them back into the working branch.

## Merge Process

For each worktree agent that reported changes (the Agent tool result includes a branch name):

1. **Ensure you're on the working branch:**
   ```bash
   git checkout <working-branch>
   ```

2. **Merge the worktree branch:**
   ```bash
   git merge <worktree-branch-name> --no-edit
   ```

3. **If the merge succeeds:** Move on to the next branch.

4. **If a merge conflict occurs:**
   - Run `git diff --name-only --diff-filter=U` to list conflicted files.
   - Read each conflicted file and attempt to resolve the conflict by keeping both sets of changes (since the tasks were designed to touch different parts of the code).
   - After resolving, run `git add <resolved-files>` and `git commit --no-edit`.
   - If the conflict is too complex to auto-resolve (e.g., both agents modified the same lines), ask the user for help using AskUserQuestion.

5. **Clean up the branch:**
   ```bash
   git branch -d <worktree-branch-name>
   ```

## Merge Order

Merge branches in the order the agents completed (or in task number order if they completed simultaneously). This is generally safe since parallel tasks are designed to be independent.

## Important Notes

- The Agent tool automatically cleans up worktrees that have no changes. You only need to merge branches that the agent result explicitly mentions.
- Always merge sequentially — do not attempt to merge multiple branches at once.
- After all merges are complete, run a quick sanity check (e.g., `git status`, `git log --oneline -5`) to confirm the working branch is clean.
