---
name: safe-git-operations
description: Guard destructive or history-changing Git operations such as rebase, reset, force-push, branch deletion, or direct changes to a protected branch. Use before executing the operation.
---

# Safe Git Operations

1. Resolve the exact repository, worktree, branch, `HEAD`, upstream, and working-tree
   status before changing history.
2. Fetch the relevant remote. Before rebasing a shared branch, inspect both directions
   of divergence and preserve remote-only commits.
3. Never use `git push --force` or `git push -f`. Use `--force-with-lease` only after
   confirming the expected remote tip.
4. Never push feature work directly to `master`. In ccswitch, branch from a freshly
   fetched `origin/master`; the deploy worktree owns local `master` and local refs may
   be stale.
5. Before deleting a branch, verify it is merged or otherwise recoverable and resolve
   every worktree that still uses it.
6. Do not run `git reset --hard`, discard local changes, or delete untracked files
   without explicit user authorization and an exact target.

Prefer an explicit SHA-to-ref push when worktree context is ambiguous.
