# Multiple PRs per Session — Design

**Date:** 2026-06-23
**Status:** Approved (design); implementation pending
**Feature area:** Session PR badges (`PrPill` / new `PrList`) · PR poller · daemon↔edge RPC

## Goal

A session authors more than one PR over its life (e.g. this session authored #181
`feat/pr-deployed-indicator` and #186 `fix/remote-pr-for-badges`). Today only one PR
can be shown per session, and only when the session happens to be checked out on the
PR's head branch. This feature tracks **every** PR a session has authored and surfaces
them all on the session, scaling the UI to an arbitrary count.

## Background / why the current behavior is insufficient

- Session worktrees share one clone's `.git`, so `git branch` cannot attribute a branch
  to a specific session. The only per-session git signals are the worktree's current
  HEAD and its **HEAD reflog** (checkout history is per-worktree).
- A session works on auto-created scratch branch `ccswitch/<base>-<hash>` but opens PRs
  from separate feature branches it creates (e.g. `feat/foo`). The stored
  `session.branch` (the scratch branch) never matches a PR head ref, so no badge shows.
- We never wrap the `claude`/`git` binary (constitution I), so there is no push hook to
  observe "the branch was pushed". Branches must be **inferred from the worktree and
  persisted**.

## Decisions (locked during brainstorming)

1. **Track all PRs ever authored** (not just open / recent).
2. **Persist** the accumulated branch set in a new nullable `authored_branches` JSON
   column, seeded from the reflog and appended each cycle — survives reflog GC (~90d)
   and daemon restarts.
3. **UI placement:** 1 PR → the existing inline `PrPill`; 2+ PRs → a compact count chip
   (`🔀 N PRs · M 🚀`) that opens a popover listing every PR (state, checks, deployed).
4. The API session shape changes from a single `pr` to a **`prs` list** (front + back
   deploy together, so no transition shim needed).

## Branch discovery

Per session, the daemon (which has filesystem access to the worktree cwd) computes the
session's authored branches:

- **Reflog source:** parse `git -C <cwd> reflog HEAD` (or read the worktree's
  `logs/HEAD`) for `checkout: moving from … to <ref>` targets that are branch names —
  the full set of branches this worktree has checked out.
- **Live source:** `worktrees.current_branch(cwd)` — the in-progress branch.
- **Union** the two, then **filter out**: the session's own scratch branch
  (`record.branch`) and the repo's default branch (`master` / `main`). Detached-HEAD /
  unresolvable states contribute nothing.
- **Persist** the union into `session.authored_branches` (accumulate; never prune). The
  GitHub PR query downstream naturally keeps only branches that actually have a PR, so
  feature branches that never opened a PR simply contribute no badge.

A new helper owns this so the poller and the per-session resolver share one
implementation:

```
worktrees.authored_branches(cwd, scratch_branch, default_branch) -> list[str]
```

## Component 1 — persistence

- New `DbSession.authored_branches` column: `JSON`, nullable, default `None`
  (per CLAUDE.md: nullable so `init_db` backfills without a migration).
- `SessionRecord.authored_branches: list[str]` field (default `[]`), included in
  `to_dict` / `from_dict` so it crosses the daemon→edge boundary.
- The daemon's poll cycle resolves each live session's authored branches, unions them
  into the record + DB row (write-back like the existing branch-backfill in
  `rehydrate`), so the set is durable.

## Component 2 — poller (multi-branch query)

- `_pr_pairs()` becomes the **union of `(repo, branch)`** over every session's
  `authored_branches` (deduped). Still one batched `gh api graphql` call — `build_pr_query`
  already aliases per pair; the only change is more pairs.
- Poller cache stays keyed by `(repo, branch)` — unchanged.

## Component 3 — per-session PR resolution

- New orchestrator method:
  ```
  prs_for(session_id: str) -> list[PullRequestInfo]
  ```
  Gathers `pr_poller.cached(repo, b)` for each branch in the session's
  `authored_branches`, drops `None`, **dedupes by PR number**, and orders
  open/draft first, then merged/closed, newest `updatedAt` first. (Reuses the existing
  `_STATE_RANK` ordering from `pr_status`.)
- New daemon RPC `prs_for` + `RemoteOrchestrator.prs_for` (mirrors the existing `pr_for`
  RPC pattern), so it works in remote mode. The edge rebuilds each entry via
  `PullRequestInfo.from_dict`.

## Component 4 — API

- `server/api/sessions.py`: replace the single-PR resolution with
  `prs = await _resolve_prs(orch, record)` (await-aware, like the existing `_resolve_pr`),
  and emit **`"prs": [info.to_dict() for info in prs]`** in `_record_to_dict` (drop the
  `"pr"` key). Update `test_sessions_pr_field.py` accordingly.

## Component 5 — frontend

- `web/src/api/types.ts`: `Session.pr?` → **`prs: PullRequestInfo[]`** (default `[]`).
- New `web/src/components/PrList.tsx`:
  - `prs.length === 0` → render nothing.
  - `prs.length === 1` → render today's `<PrPill>` (unchanged single-PR look).
  - `prs.length >= 2` → a count chip `🔀 {n} PRs · {deployedCount} 🚀` that toggles a
    popover; the popover renders one `<PrPill>` (or a richer row) per PR. Reuses `PrPill`
    for each entry — no parallel pill implementation.
- `SessionCard.tsx` (and the mobile Dashboard inline variant — see memory
  `accounts-list-dual-impl` / `archived-list-dual-impl` for the dual-render gotcha)
  swap `<PrPill pr=…>` for `<PrList prs=…>`.

## Testing

- **Branch discovery** (`worktrees.authored_branches`): reflog + live union; scratch and
  default branches filtered; detached/empty → `[]`. (Drive with a temp git repo + a
  worktree, or a parsed-reflog fixture for the pure part.)
- **`prs_for`**: dedup by number across branches; ordering (open before merged); empty
  when no cached PRs.
- **`prs_for` over the real UDS daemon RPC** (extends `test_remote_orchestrator.py`):
  seed the daemon poller cache for two branches of one session, assert the edge returns
  both rebuilt `PullRequestInfo`s.
- **API**: `_record_to_dict` emits `prs`; `_resolve_prs` is await-aware (sync in-process
  / async remote / missing → `[]`).
- **Frontend** (`PrList.test.tsx`): 0 → nothing; 1 → a single pill; 2+ → chip with
  correct count + deployed tally, popover lists all with deployed markers.

## Out of scope (v1)

- Pruning / archiving old merged PRs from the set (we keep "all ever").
- A push hook or any binary wrapping for exact "pushed" detection (reflog inference only).
- Per-PR branch labels in the popover beyond what `PullRequestInfo` already carries.
- Changes to how a single PR renders (kept identical to today).
