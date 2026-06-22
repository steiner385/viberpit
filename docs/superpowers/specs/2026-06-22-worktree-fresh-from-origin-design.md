# Fresh-from-origin session worktrees

**Date:** 2026-06-22
**Status:** Implemented (PR #125), amended by PR #127
**Area:** `src/ccswitch/worktrees.py` (`create_worktree`)

> **Amendment (PR #127) — fetch failure is best-effort, not fatal.** The original
> Rule 2 below made a `git fetch` failure against a configured `origin` abort the
> session (`WorktreeError`). In practice `git fetch` fails for any origin that isn't
> fetchable non-interactively — private repos with no cached credentials, the
> credential-less deploy box, offline — and even the project's own test fixtures use
> placeholder origins. Hard-fail therefore broke session creation broadly (it turned
> CI red). **Revised behavior:** on fetch failure (or an unresolvable `origin/HEAD`),
> log a warning and fall back to the local-ref base instead of aborting. Remote
> anchoring still applies whenever the fetch succeeds. Read Rule 2/3 below with this
> override in mind.

## Problem

Auto-spawned per-session worktrees can be hundreds of commits behind `origin/master`.
Observed: every `ccswitch/master-<id>` worktree sat at `2143c9f` (PR #26) while
`origin/master` was at `439a6ab` (PR #120) — **399 commits behind**.

Root cause: `create_worktree` never touches the network. It bases the worktree on a
**local** ref:

- No branch → detached off the local current HEAD (`worktrees.py:110-114`).
- Branch given → checks out the local `<branch>`; if busy, makes a child branch
  `ccswitch/<branch>-<short>` based on the **local** `<branch>` (`worktrees.py:117-131`).

The shared local clone's `master` ref is itself stale (nothing fetches it), so every
session inherits that stale point. The deployed app runs `origin/master`, so sessions and
"what's actually live" diverge silently.

The single load-bearing bug: step 2 bases the child branch off the local `<branch>` rather
than `origin/<branch>`, and there is no `git fetch` anywhere in the path.

## Goal

Every new session starts from the **true remote tip**, without mutating the user's local
refs or working tree, and without silently falling back to a stale base.

## Contract

`create_worktree(repo_path, branch, session_id, repo_root)` becomes fetch-first and
remote-anchored. Resolution order:

1. **Detect `origin`.** If the repo has **no `origin` remote** configured, there is nothing
   to be stale against — skip the fetch and keep today's local-ref behavior verbatim
   (detached off local HEAD / direct local-branch checkout / local child branch). This keeps
   purely local repos working and offline-capable.

2. **Fetch.** If `origin` exists, run `git -C <repo_top> fetch origin`. **If the fetch fails
   (non-zero exit), raise `WorktreeError` — the session does not start.** Offline or a network
   blip is a loud failure, not a silent stale base. (Distinct from rule 1: *no remote* is
   fine; *fetch failed against a configured remote* is fatal.)

3. **Resolve the base commit-ish from the remote, never the local ref:**
   - **No branch given** → remote default branch. Run `git remote set-head origin --auto`,
     then resolve `origin/HEAD` (e.g. `origin/master`). If it cannot be resolved, raise
     `WorktreeError`.
   - **Branch given and it exists on origin** → `origin/<branch>`.
   - **Branch given but NOT on origin** (genuinely local-only branch) → local `<branch>`.
     This is not staleness — there is no remote tip to be fresh against — so it is the one
     allowed local base even when `origin` exists.

4. **Never mutate the user's local refs.** Do not fast-forward or reset the user's
   `master`/`<branch>`. Achieve isolation at the remote tip via:
   - **No branch** → detached worktree at the remote default tip.
     `git worktree add --detach <dest> <origin/HEAD-sha>`
   - **Branch given (remote tip)** → per-session child branch
     `ccswitch/<branch>-<short>` based at `origin/<branch>`.
     `git worktree add -b ccswitch/<branch>-<short> <dest> origin/<branch>`
     **This is now the normal path, not just the "branch busy" fallback** — the prior
     direct-checkout-of-local-branch step is removed for remote-backed branches. It is the
     only way to honor "fresh from origin" without moving the user's local branch ref.
   - **Branch given (local-only, rule 3 fallback)** → unchanged: direct local checkout, with
     the existing busy→child-branch fallback.

### `WorktreeInfo` semantics

- Remote-backed branch case: `branch = "ccswitch/<branch>-<short>"`, `is_child = True`.
- No-branch case: `branch = None`, `is_child = False` (detached), as today.
- Local-only / no-`origin` cases: unchanged from today.

### Unchanged behavior

- The `-`-prefixed branch-name injection guard (`worktrees.py:91-92`) stays.
- `worktree prune` of dead admin records before creating (`worktrees.py:105`) stays.
- `remove_worktree` is untouched; it already deletes `ccswitch/*` child branches and never
  destroys uncommitted work.

## Error handling

`WorktreeError` is raised on: fetch failure against a configured `origin`; unresolvable
`origin/HEAD` in the no-branch case; and the existing invalid-branch and final
`worktree add` failure cases. The orchestrator already treats `WorktreeError` as a failed
session start (`orchestrator.py:227-231`), so no new surfacing path is needed — the fetch
runs inside the existing `run_in_executor` call, off the event loop.

## Testing (TDD)

Extend `tests/test_worktrees.py`. The current `_make_repo` builds a remoteless local repo;
add a helper that wires a **bare `origin` remote ahead of local**: clone/init a bare repo,
push, then add a commit to `origin` that local does not have.

New tests:

1. **Remote-backed branch is fresh** — worktree HEAD == `origin/<branch>` tip (the commit
   local lacks), `is_child` True, branch name `ccswitch/<branch>-<short>`.
2. **No branch → remote default tip** — detached worktree HEAD == `origin/HEAD` tip, not
   local HEAD.
3. **User local refs unchanged** — after creation, local `master`/`<branch>` still point
   where they did (no fast-forward).
4. **Fetch failure is fatal** — `origin` set to a bogus URL → `WorktreeError`, and no
   worktree directory is left behind.
5. **No `origin` remote → today's behavior** — remoteless repo still creates a worktree off
   local refs (regression guard; covers all existing tests).
6. **Local-only branch with `origin` present** — a branch that exists only locally bases off
   the local ref, not the remote.

Existing tests that use remoteless repos must keep passing unchanged (rule 1 guarantees it).
Run: `<main-checkout>/.venv/bin/python -m pytest tests/test_worktrees.py
server/tests/test_orchestrator_worktree.py server/tests/test_security_hardening.py`.

## Out of scope

- Refreshing the shared local clone's refs on a timer (an alternative we rejected).
- Surfacing "N commits behind" in the session status strip (separate concern).
- Rebasing long-running sessions mid-flight; `origin/master` can still advance during a
  session (see the worktree-staleness note) — push-time rebase remains the user's call.
