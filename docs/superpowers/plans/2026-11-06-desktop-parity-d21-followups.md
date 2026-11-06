# Desktop-parity D-21: D-19 follow-ups — recovery refs on archived sessions, repo branches in ⌘K — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal.** Close two follow-ups from #3333 (owner choice on 2026-11-06: build "D-19 follow-ups"):
- **A. Recovery snapshots stay findable.** Today the refs and restore command from a Discard (and from close, delete, drain or a forced removal) appear only in a toast or a log. Show them on the archived session's page.
- **B. ⌘K "New session on repo@branch" offers the repo's real branches,** not only branches that a live session already has.

The pin/star item in #3333 is settled: the owner rejected it (it's in the benchmark's `REJECTED` set). The effort-ordering and ignored-files items stay open there.

## Facts (origin/master `0093a02b`)

**A. Recovery refs**
- The refs live in the clone's common git dir, under `refs/viberpit/recovery/<short-sid>/…`.
  - Helpers in `src/viberpit/worktrees.py`: `recovery_refs(repo_top, sid)` (~1173), `restore_command(ref)` (~1131), `_recovery_ns` (~1153). The reasons are at ~1137.
- Discard records the refs only in the `session.worktree_discard` audit row and the toast.
  - Toast: `web/src/features/sessions/rail/DiscardWorktreeDialog.tsx:101-117`.
  - Discard code: `orchestrator.py:7035-7127`.
- Other paths also write refs: close (`_cleanup_session_paths` ~7166), `permanent_delete` (~29189), drain (~22086), forced remove, move finalize (`worktree_transfer.py:374`), and the janitor. All of them only log.
- After the worktree is gone, the clone can still be found on the owning host with `gitrepos.clone_path(effective_repo_root, row.repo)`. `_ensure_session_cwd` does the same at `orchestrator.py:6814-6829`. A row with no `repo` (a scratch session) can't be resolved this way.
- The existing preflight (`_worktree_control_facts` ~6885) lists refs only while the worktree is present, and it needs a live record. So archived sessions get a 404 today.
- Routing for archived rows: `_PER_SESSION` methods route by `row.host` through `_host_for` (`fleet_orchestrator.py:222`, `:440-465`).
- Archived page: `web/src/features/sessions/ReviewPage.tsx:36-112` (`ArchivedReviewPage`). The banner slot is between `PageHeader` and `PageContent` (~92). The type is `ArchivedSession` (`web/src/api/types.ts:522`).

**B. Branches**
- `GET /api/github/branches?repo=owner/name` already returns `{default, branches[]}` (`server/api/repos.py:99-110`, built on `gitrepos.list_remote_branches`, which uses the GitHub API on the edge).
  - Today only the assistant tool uses it. The web never calls it, and the result isn't cached.
- The palette builds branches only from `host.live` (`web/src/features/palette/usePaletteEntries.ts:167-188`). The entry calls `host.startOnBranch(repo, branch)`; D-19 already wired that through.

## Global Constraints

**A (server):**
- **The per-session read.** Add `session_recovery_refs(session_id)`, returning `{refs:[{ref, reason, sha, createdAt}], restoreCommand}`. `restoreCommand` is the command for the newest ref, or `null`.
  - It is a READ that works for **live and archived** sessions.
  - It runs on the owning daemon, wired through `_PER_SESSION`, `orchestrator_rpc._SIMPLE`, remote, facade and fleet. Check that `_host_for` resolves an archived row's host. If it doesn't, fix it there, in that one routing function; don't add a parallel route.
  - It finds the repo top in this order: the live worktree's common dir if the worktree is present; otherwise `clone_path(root, row.repo)`; otherwise `{refs: [], restoreCommand: null}` (no repo, or no clone). Reuse `recovery_refs` and `restore_command`. Never shell out with a string.
  - `reason` and `createdAt` come from what the ref name or ref log already encodes. Read `_recovery_ns` and `snapshot_recovery_ref` to find out. If `createdAt` isn't recoverable cheaply, return `null`. Don't add DB state.
- **The route.** `GET /api/sessions/{id}/recovery-refs`.
  - It checks identity only and is read-only, so it writes no audit row.
  - Status codes: 404 for an unknown session, 409 `daemon_outdated` for an old daemon (the D-20 pattern).
  - Expected refusals log at INFO.
- **The refs never leak another session's refs.** The lookup is keyed by the session's own short id, using the same namespace function as the writers.

**A (web):**
- **A "Recovery snapshots" card on `ArchivedReviewPage`.** It is shown only when `refs` isn't empty.
  - It lists each ref with its reason, a short SHA and the date if known.
  - It has a copy button for `restoreCommand` (reuse the existing copy-to-clipboard helper).
  - It names the archived session's host when that isn't this host. Check whether `ArchivedSession` carries `host`; if it doesn't, leave this out.
- **The hook is lazy.** The card's module is lazy too, if the budget needs it.

**B (web):**
- **A hook `useRepoBranches(repo, enabled)`** that calls `/api/github/branches`. Use `staleTime` 60 s and `retry: 1`, and follow the pattern in `web/src/api/hooks.ts`.
- **Fetch lazily.** Fetch branches only for the repo the user is focusing in the palette: the repo drill, or a query that matches `repo@`. Read how drill entries work in `usePaletteEntries.ts` and pick the narrowest trigger that exists. Never fetch for every repo when the palette opens.
- **Merge the results** with the live-session branches, dedupe them, and put the default branch first. Each entry offers `New session on <repo>@<branch>` through the existing `host.startOnBranch`.
  - While loading, keep the live entries.
  - On an error, keep the live entries and show no error toast. A failed fetch is a quiet degradation.
- **Server (optional, small).** If the palette can call it repeatedly, add a short per-repo TTL cache to `/github/branches` (30–60 s) that reuses the stale-cache pattern in `repos.py:27-49`. Don't create a new cache helper.

**Gates:**
- **Server:**
  - ruff;
  - mypy;
  - focused pytest plus parity;
  - the full `server/tests -n 4` with `PYTHONPATH=$PWD/src` in a clean env;
  - core `tests/` if `src/` changes.
- **Web:**
  - typecheck;
  - lint;
  - the full vitest suite;
  - build;
  - budget (ceilings entryJs 111.6, initialJs 308.8, initialCss 15.6);
  - e2e.

## Review Focus

1. The refs lookup is confined to the session's own namespace and works for an archived row whose worktree is gone, on the right host (Task 1).
2. No new write, lock or audit on a read path (Task 1).
3. ⌘K never fetches branches for every repo, and a failed fetch degrades quietly (Task 2).
4. The restore command is shown as text and copied exactly (Task 2).

---

### Task 1: Server — `session_recovery_refs` RPC and route

**Files:** the orchestrator method, the RPC, remote, facade and fleet layers, `api/sessions.py`, and possibly `repos.py` (the cache).

**Tests:** `server/tests/test_recovery_refs_api.py`, against a real temporary git repo. Cover:
- a live session;
- an archived session whose worktree has been removed (the refs are still listed through `clone_path`);
- a scratch session (empty result);
- another session's refs not being listed;
- an unknown session (404);
- the remote and fleet routing of an archived row.

- [ ] **Step 1:** Write the failing tests.
- [ ] **Step 2–4:** Implement, then run the focused tests, parity, ruff and mypy.
- [ ] **Step 5:** Commit `feat(sessions): list a session's recovery refs, archived included (D-21)`.

### Task 2: Web — the archived recovery card and repo branches in ⌘K

**Files:** `ReviewPage.tsx`, a new lazy `RecoveryRefsCard.tsx`, the hooks and their mocks, and `usePaletteEntries.ts`.

**Tests:** unit tests, plus e2e `web/e2e/d21-followups.spec.ts` (route-mocked, at 1440 and 390).

- [ ] **Step 1:** Write failing tests for Review Focus 3–4 and the card's states.
- [ ] **Step 2–4:** Implement, then run the full web gates and e2e.
- [ ] **Step 5:** Commit `feat(web): recovery snapshots on archived sessions, repo branches in ⌘K (D-21)`.

### Task 3: Docs

- **Spec:** add the route to the API table and a D-21 row to §11.
- **Product docs:** describe the archived page and the palette changes.
- **Issue #3333:** tick the two items off; the PR body carries `Refs #3333`.
- **Benchmark:** no change, since these rows are already Y.
- **Ceilings:** measured + 0.3, only if they moved.
- [ ] Commit `docs: D-21 follow-ups`.
