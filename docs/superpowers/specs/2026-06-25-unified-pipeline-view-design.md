# Unified Pipeline View — Design

**Date:** 2026-06-25
**Status:** Approved (design); pending implementation plan
**Program:** ccswitch⨉Plotroom combined-app. Maps to roadmap **M5 / US-13/14** (Unified Pipeline view),
delivered on the already-parity-proven stage machine. See
`docs/superpowers/specs/2026-06-23-combined-app-roadmap.md`.

---

## 1. Goal

Port Plotroom's signature capability — a board that shows the **full PR population** for the repos you
work in, grouped by pipeline stage — into the ccswitch web app as a new top-level **Pipeline** tab.

Today ccswitch only knows **session-linked PRs** (PRs authored from a branch some session touched) and
only surfaces a PR's stage as a tiny `PrPill` on its session card. This feature:

1. Extends PR tracking to the **full population**: every open + recently-merged PR in watched repos,
   whether or not a ccswitch session exists for it.
2. Surfaces them in one place, grouped into **stacked, collapsible stage sections**.
3. Reuses the existing, parity-proven stage machine (`server/pipeline/stage.py`,
   `server/github/pr_status.py`) — no new stage classifier.

**Decisions locked during brainstorming:**
- PR scope = **full repo PR population** (not session-linked only).
- Watched repos = **auto-derived from sessions** (union of `record.repo`); zero config.
- Layout = **stacked stage sections** (collapsible), mobile- and desktop-identical.

---

## 2. Non-goals (explicitly out of scope)

- **No ETA / lead-time waterfall** — Plotroom's `PrTimeline` waterfall and learned ETA engine stay
  deferred (oracle-coverage blocked; roadmap M5 1c). The board shows stage, not predicted timings.
- **No owner-wide search** — we watch repos derived from sessions, not all repos under a GitHub owner.
- **No new config surface** — no `.pr-dashboard.yml`, no settings panel, no `CCSWITCH_PR_OWNERS`.
- **No closed-unmerged PRs** — only open + recently-merged (≤7d) appear, matching the stage machine's
  merged retention.
- **No queue/deploy parity claims** — queue/qa-deploy/awaiting-prod stages remain as the stage machine
  already computes them (parity-deferred); this feature only *displays* whatever stage is derived.

---

## 3. Architecture — Backend

### 3.1 New `PrPopulationPoller` (`server/github/pr_population.py`)

A background poller that mirrors `PrStatusPoller`'s lifecycle (jitter, exponential backoff, on-change
callback) but is keyed by **repo**, not `(repo, branch)`.

- **Watched-repo provider:** an async callable returning the **union of `record.repo`** across all live
  (non-archived) sessions — the repo-only analogue of `orchestrator._pr_pairs`. Set via a
  `set_repos_provider(...)` on the poller, instantiated in `SessionOrchestrator.__init__` alongside the
  existing `_pr_poller`.
- **Query:** one GitHub GraphQL `search` per repo, combining:
  - `repo:<owner/name> is:pr is:open archived:false`
  - `repo:<owner/name> is:pr is:merged merged:>=<now-7d>`
  Paginated (50/page) following Plotroom's sweep shape, scoped to a single repo per call.
- **Classification — REUSE, do not duplicate:** each PR node is mapped through the existing
  `_to_info(...)` in `server/github/pr_status.py`, which already calls `derive_open_stage` /
  `derive_merged_stage`. The poller adds **no** stage logic of its own (constitution VI).
- **Cache:** `repo -> tuple[PullRequestInfo, ...]`, served stale on transient failure.
- **Isolation:** per-repo try/except + backoff so one failing repo never blanks the whole board.

> Rationale: a *separate* poller (not a mode flag on `PrStatusPoller`) is correct because the data
> shapes genuinely differ — `(repo,branch) -> one PR` (feeds per-card pills) vs `repo -> list[PR]`
> (feeds the board). The shared **logic** is the stage classifier, which both call into. Duplicate the
> plumbing when shapes differ; never duplicate the logic.

### 3.2 `PullRequestInfo` gains a `repo` field

`PullRequestInfo` (`server/github/pr_status.py`) currently has no `repo` field (the repo is only
implicit in `url`). Add `repo: str = ""` so the board can group/label without URL-parsing.

- Update the single `to_dict` / `from_dict` pair.
- Default empty string keeps it backward-compatible with the existing session-PR path and historic
  wire payloads (additive, nullable-by-default — no migration).
- Frontend `PullRequestInfo` type (`web/src/api/types.ts`) gains the same optional `repo`.

### 3.3 New `all_prs()` orchestrator method (wired across daemon↔edge)

This is the integration point most prone to being half-wired (see memory:
remote-mode-path-asymmetry). All five layers must be touched:

1. **`SessionOrchestrator.all_prs()`** (in-process, `server/sessions/orchestrator.py`): reads the
   population cache for all watched repos, flattens, and **attaches a session link** to each PR by
   joining on `(repo, pr.number)` against every session's `authored_branches` + live `branch`. Returns
   a list of `(PullRequestInfo, session_id|None, session_name|None)` (or a small dataclass).
2. **`InProcessOrchestratorAdapter.all_prs()`** (`server/sessions/facade.py`): async passthrough.
3. **`RemoteOrchestrator.all_prs()`** (`server/sessions/remote.py`): `self._client.call("all_prs")`.
4. **RPC handler** in `server/ipc/orchestrator_rpc.py`: register `all_prs`, serialize result
   (follow the `prs_for` handler pattern; PRs via `to_dict`).
5. Confirm the daemon's session-serving layer dispatches it (same pattern as `prs_for`).

### 3.4 New route `GET /api/prs`

In `server/api/sessions.py` (or a small new `server/api/pipeline.py` router registered in
`server/app.py`), behind the standard `current_identity` auth dependency.

Response: a flat list, each element:

```json
{
  "number": 123,
  "url": "https://github.com/owner/name/pull/123",
  "repo": "owner/name",
  "state": "open",
  "checks": "failing",
  "title": "...",
  "deployed": false,
  "stage": "parked",
  "session": { "id": "abc", "name": "Fix the thing" }
}
```

`session` is `null` for PRs with no ccswitch session. (Fields beyond those shown — `head_sha`,
`is_draft`, `merged`, `updated_at`, `checks_detail` — come from `pr.to_dict()` and may be present.)

---

## 4. Architecture — Frontend

### 4.1 Data hook `useAllPrs()` (`web/src/api/hooks.ts`)

`useQuery` on `/api/prs`, `refetchInterval ~10s` (the endpoint reads an in-memory cache — cheap),
mirroring `useSessions()`. Returns the typed list above.

### 4.2 `PipelineView` component (`web/src/components/PipelineView.tsx`)

- Buckets PRs by `pr.stage` and renders **one collapsible section per non-empty stage**, in urgency
  order:

  | Order | Stage key(s) | Section label |
  |---|---|---|
  | 1 | `parked` | Parked |
  | 2 | `ci` | In CI |
  | 3 | `ready` | Ready |
  | 4 | `queue` (+ substates) | Queue |
  | 5 | `merged` | Merged |

  Empty sections are omitted. `''`/unknown stages are dropped (defensive).
- **Reuse existing patterns:** the collapsible-group + `GroupHeader` component and the established
  z-index conventions (see memory: zindex-scale-and-list-stacking) — do not invent a new collapsible.
- **PR row:** reuses `PrPill` for state/checks/stage, plus repo + PR number + title.
  - **Session-linked** (`session != null`): the row click-throughs to that session
    (`/session/:id`); shows the **Fix-PR** chip when the PR is failing + actionable
    (`isActionablePrState`) — reusing `SessionCard`'s existing chip + `request_pr_fix` path.
  - **Unsessioned** (`session == null`): the row opens the GitHub `url` in a new tab; **no** Fix chip
    (`request_pr_fix` requires a session).
- **Empty board:** friendly empty state ("No open PRs in your repos").

### 4.3 Navigation

- Extend `TabId` to `'sessions' | 'accounts' | 'archived' | 'pipeline'`.
- Add `/pipeline` route in `web/src/App.tsx` rendering `Dashboard` with `activeTab='pipeline'`.
- Surface the tab in the `Dashboard` (mobile) and `DesktopShell` tab strips; wire
  `onTabChange('pipeline')` → `navigate('/pipeline')`.

---

## 5. Data flow

```
live sessions ──(union of record.repo)──▶ PrPopulationPoller
                                              │  per-repo GraphQL search (open + merged≤7d)
                                              ▼
                                     repo -> [PullRequestInfo]  (stage via shared classifier)
                                              │
SessionOrchestrator.all_prs() ◀───────────────┘
   │  join (repo, number) against sessions' authored_branches/branch → attach session link
   ▼
RPC (remote) / direct ──▶ GET /api/prs ──▶ useAllPrs() ──▶ PipelineView
                                                              group by stage → collapsible sections
```

---

## 6. Error handling & edge cases

- **Transient GitHub failure (per repo):** isolated try/except + backoff; last good cache served; one
  bad repo never blanks the board.
- **No sessions / no repos:** empty watched-repo set → empty board → empty state (no error).
- **PR reachable via multiple session branches:** session join dedupes by PR number (as `prs_for`
  already does); first/most-relevant session wins for the link.
- **Merged retention:** bounded to 7 days by `derive_merged_stage` — the Merged section can't grow
  unbounded.
- **Stage `''` or unknown:** dropped from the board (defensive; should not occur for open/merged).
- **Remote vs direct mode:** `all_prs()` must behave identically in both; covered by tests.

---

## 7. Testing (TDD)

**Backend**
- Population query builder: correct per-repo `search` strings (open + merged window).
- Parse → stage bucketing: a GraphQL fixture maps to the right `PullRequestInfo` + `stage` via the
  shared classifier (no re-derivation).
- Session join: `(repo, number)` correctly attaches/omits the session link; multi-branch dedupe.
- `all_prs()` RPC round-trip in **both** direct and remote modes (asserts the boundary is fully wired).
- `GET /api/prs`: 401 without identity; 200 with; payload shape incl. `repo` and `session` — assert on
  a real GET response, not just a `to_dict` round-trip (see memory: session-record-dual-serializer).
- `PullRequestInfo` `to_dict`/`from_dict` round-trip including `repo`.

**Frontend (vitest)**
- `PipelineView`: bucketing + section ordering (Parked→CI→Ready→Queue→Merged); empty sections hidden;
  empty board state.
- Session-linked row → navigates to session + shows Fix chip when failing+actionable; unsessioned row
  → opens GitHub URL + no Fix chip.
- `useAllPrs` hook fetch/shape.
- Verify with `npx tsc -b --force` + `eslint . --max-warnings 0` + full `vitest run` (see memory:
  ccswitch-frontend-real-typecheck, ccswitch-tsc-noemit-false-green).

---

## 8. Files touched (summary)

**New**
- `server/github/pr_population.py` — `PrPopulationPoller`.
- `web/src/components/PipelineView.tsx` — board.
- (maybe) `server/api/pipeline.py` — route, if not folded into `sessions.py`.

**Modified**
- `server/github/pr_status.py` — add `repo` to `PullRequestInfo` (+ `to_dict`/`from_dict`).
- `server/sessions/orchestrator.py` — instantiate poller, repos provider, `all_prs()`.
- `server/sessions/facade.py`, `server/sessions/remote.py`, `server/ipc/orchestrator_rpc.py` — wire
  `all_prs()` across daemon↔edge.
- `server/app.py` — register route (if new router); wire poller into lifespan if needed.
- `web/src/api/types.ts` — `repo` on `PullRequestInfo`; pipeline response type.
- `web/src/api/hooks.ts` — `useAllPrs()`.
- `web/src/App.tsx`, `web/src/components/Dashboard.tsx`, `web/src/components/DesktopShell.tsx` —
  `'pipeline'` tab + route.

---

## 9. Risks

- **Half-wired daemon↔edge** (the recurring trap): mitigated by an explicit 5-layer checklist (§3.3)
  and a remote-mode round-trip test.
- **Population query cost / rate limit:** per-repo search every ~75s for a handful of repos is well
  within the 5,000-req/hr budget; backoff guards spikes.
- **Parallel-implementation drift:** the stage classifier is the single source of truth; the poller
  must call it, not re-derive. Enforced in review + tests.
