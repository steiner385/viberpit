# Per-Session PR Indicators — Design

**Date:** 2026-06-22
**Feature:** Visual indicators on the dashboard showing which PR each session has open
and that PR's live GitHub status (state + CI checks), in the style of Claude Code web.

## Problem

A ccswitch session frequently opens a pull request, but the dashboard gives no signal
that it did, nor how that PR is doing on GitHub. Users must leave the app and check
GitHub to know whether a session's PR is open, merged, draft, or failing CI. We want a
compact, always-current PR pill on each session card.

## Key insight: the worktree branch is the join key

Every repo-backed session runs in its **own git worktree** on a per-session branch
(`ccswitch/<branch>-<short>`, created in `src/ccswitch/worktrees.py::create_worktree`).
A GitHub PR's `headRefName` is exactly that branch, so the mapping session → PR is a
clean 1:1 join on the branch name. No transcript parsing is needed, and PRs opened
out-of-band (or by a later `gh pr create`) are still picked up.

## Decisions (from brainstorming)

- **Detail level:** match Claude Code web — PR number, color-coded state
  (open / draft / merged / closed), and a CI-checks rollup (passing / failing / pending).
  Review decision and mergeability are out of scope.
- **Freshness:** a background poller (mirrors `server/usage/poller.py`) batch-queries
  GitHub via GraphQL on an interval and caches results in memory.
- **Linkage:** branch-derived. The session's worktree branch is stored on the session
  record and matched against PR `headRefName`.
- **Empty state:** no pill at all until a PR exists — the pill's appearance is itself
  the "a PR was opened" signal.

## Architecture & data flow

```
worktree branch ──stored on Session.branch──►  PrStatusPoller (every ~75s, jittered)
   (1:1 join)                                     │ batched GraphQL, grouped by repo
                                                  ▼
                                       in-memory cache  { (repo, branch) → PullRequestInfo }
                                                  │
                              list_sessions route attaches session["pr"]
                                                  ▼
                              SessionCard renders <PrPill> beside <StatusPill>
```

The poller is a **cache producer**; the HTTP/serialization path only ever *reads* the
cache, so a slow or rate-limited GitHub never blocks a dashboard load — pills just go
slightly stale. PR data rides the existing session-list payload (no new frontend hook),
which avoids the `dashboard-hooks-partial-mocks` test-fragility trap.

## Components

### Server

- **`server/github/pr_status.py`** — pure, I/O-free functions:
  - `build_pr_query(pairs: list[tuple[repo, branch]]) -> str` — one `gh api graphql`
    query that aliases a `repository(owner,name){ pullRequests(headRefName:…, first:5) }`
    block per repo, requesting `number, url, state, isDraft, merged, updatedAt` and the
    head commit's `statusCheckRollup { state }`.
  - `parse_pr_response(json, pairs) -> dict[(repo,branch), PullRequestInfo]` — maps the
    response, applying the "most relevant PR" rule (open/draft > merged > most-recent
    closed) and the checks rollup → `passing|failing|pending|none`.
- **`server/github/pr_poller.py`** — `PrStatusPoller`, modeled on `UsagePoller`
  (configurable interval, jitter, adaptive backoff on error, in-memory cache, injectable
  `query_fn` for tests). `collect()` takes the active `(repo, branch)` pairs each sweep;
  `cached(repo, branch)` serves the HTTP path. Runs `gh` via `gitrepos._run_gh`.
- **Branch storage:**
  - `Session.branch` — new **nullable** column (init_db backfills it on dev SQLite per
    the repo's no-ALTER rule), plus `branch` on the in-memory `SessionRecord`.
  - Set from `WorktreeInfo.branch` at session creation in `orchestrator.create_session`.
  - For pre-existing sessions with a null branch, derive once from
    `git -C <cwd> branch --show-current`.
- **Wiring:** the orchestrator owns the `PrStatusPoller` alongside `UsagePoller`
  (start/stop in the same lifespan hooks). `list_sessions` injects `pr` from the cache.

### Frontend

- **`PullRequestInfo`** added to `web/src/api/types.ts`; `Session.pr?: PullRequestInfo | null`.
- **`web/src/components/PrPill.tsx`** — the indicator: a small pill showing `#<number>`,
  colored by state (green=open, gray=draft, purple=merged, red=closed), with a checks
  icon (✓ pass / ✕ fail / • pending; none → no icon). Renders as a link to `pr.url`
  opening in a new tab; `e.stopPropagation()` so clicking it doesn't open the session.
- **`SessionCard`** renders `<PrPill pr={session.pr} />` beside `<StatusPill>` when
  `session.pr` is set; renders nothing otherwise.

## Data model

```ts
interface PullRequestInfo {
  number: number
  url: string
  state: 'open' | 'draft' | 'merged' | 'closed'
  checks: 'passing' | 'failing' | 'pending' | 'none'
  title: string   // pill tooltip
}
```

Server-side equivalent: a dataclass / TypedDict serialized as the `pr` key.

## Error handling

- `gh` absent / unauthenticated / HTTP 502 / rate-limited → the poller logs, applies
  exponential backoff, and leaves the cache untouched. Cards show the last-known pill or
  none. PR status is decorative, never load-bearing — it must never raise into a
  dashboard request.
- A branch with no matching PR → no cache entry → no pill.
- A detached worktree (no branch) → `branch` is null → never queried.

## Testing (TDD)

**Server (pytest):**
- `pr_status` mapping: each state, `isDraft`, the checks rollup → `checks` mapping, the
  "most relevant PR" selection rule, and a branch with zero PRs.
- `PrStatusPoller`: caching, interval sweep with an injected `query_fn`, backoff on
  failure (mirrors `test_poller_backoff`/`test_poller_cached`).
- Branch persistence: `create_session` stores the worktree branch; null-branch backfill.
- `list_sessions` attaches `pr` for a cached `(repo, branch)` and omits it otherwise.

**Web (vitest):**
- `PrPill`: renders `#<number>`, the right color class per state, the right checks icon,
  and an `href`/`target="_blank"` link; clicking does not bubble to the card's open
  handler.
- `SessionCard`: shows the pill when `session.pr` is present, hides it when null.
- No new Dashboard hook is introduced, so existing factory mocks remain valid.

## Out of scope (YAGNI)

- Review-decision / mergeability signals.
- Multiple PRs per session (we surface one; the branch is 1:1 with the session).
- Persisting PR status across restarts (the cache rebuilds within one poll interval).
- Triggering merges or other PR actions from the pill (link-out only).
