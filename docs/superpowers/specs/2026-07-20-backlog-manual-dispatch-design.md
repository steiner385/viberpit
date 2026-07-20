# Backlog Runner Manual Issue Dispatch

**Date:** 2026-07-20  
**Status:** Approved — proceeding to implementation

## Problem

The backlog runner operates fully automatically: it consumes AI-generated groups in rank order with no way to jump the queue or bundle custom sets of issues. Users need to dispatch a specific group immediately (bypassing the wait) or compose their own bundle from the eligible candidate pool.

## Solution

Two dispatch paths surfaced in the Automations UI:

1. **One-click dispatch** — "Dispatch" button on each unconsumed planned group row in `BacklogGroupsPanel`.
2. **Custom dispatch** — "Custom dispatch…" button opens a dialog that lists the current eligible candidates with multi-select checkboxes.

Both paths converge on `POST /api/automations/backlog-runner/dispatch`.

## Architecture

### Pattern reference

Mirrors the PR-Doctor flow: `GET /pr-doctor/consolidation` (candidates) + `POST /pr-doctor/consolidate` (spawn). The backlog runner equivalent:
- `GET /backlog-runner/candidates` → `RemoteOrchestrator.backlog_candidates()` → daemon `backlog_candidates()`
- `POST /backlog-runner/dispatch` → `RemoteOrchestrator.backlog_runner_dispatch(refs)` → daemon `backlog_runner_dispatch(refs)`

### Daemon/edge wiring

Each method needs three layers:
1. `SessionOrchestrator` method (daemon, `server/sessions/orchestrator.py`)
2. `_SIMPLE` entry in `orchestrator_rpc.py` (auto-registers RPC handler)
3. Explicit proxy method on `RemoteOrchestrator` (`server/sessions/remote.py`)

## Backend

### `SessionOrchestrator.backlog_candidates()` (already exists — needs wiring)

`server/sessions/orchestrator.py` lines 1584–1588: calls `query_candidates(config)` via `asyncio.to_thread`. Returns `list[dict]` with keys: `ref`, `repo`, `title`, `number`, `body`, `url`.

**Wire it:**
- Add `"backlog_candidates"` to `_SIMPLE` in `orchestrator_rpc.py`
- Add `RemoteOrchestrator.backlog_candidates()` in `remote.py`

### `SessionOrchestrator.backlog_runner_dispatch(refs: list[str]) -> dict` (new)

Similar to `backlog_runner_implement_core` but:
- Accepts `refs: list[str]`; `refs[0]` is the primary
- Builds `Batch(primary=refs[0], issues=refs, repo=..., titles={ref: title})`
- Writes durable state row for the primary ref (RECORDS-BEFORE-LABELS pattern)
- Claims all issues (`_backlog_claim`)
- Resolves healthy account
- Spawns session via `_backlog_spawn`
- Returns `{"status": "spawned"|"claim_failed"|"no_account", "session_id": str, "session_name": str}`

Titles are fetched by querying candidates; refs not in the candidate set get empty titles (do not block dispatch).

**Wire it:**
- Add `"backlog_runner_dispatch"` to `_SIMPLE` in `orchestrator_rpc.py`
- Add `RemoteOrchestrator.backlog_runner_dispatch(refs)` in `remote.py`

### API endpoints (`server/api/automations.py`)

```python
GET  /api/automations/backlog-runner/candidates
     → {candidates: [{ref, title, repo, number, body, url}]}
     Calls orch.backlog_candidates()

POST /api/automations/backlog-runner/dispatch
     Body: {refs: list[str]}  (validated: non-empty, refs[0] is primary)
     → {status: str, sessionId?: str, sessionName?: str}
     Calls orch.backlog_runner_dispatch(body.refs)
     400 if refs is empty
```

Both require Tailnet auth (`current_identity` dependency, same as all other endpoints).

## Frontend

### New types (`web/src/api/automations.ts`)

```typescript
export interface BacklogCandidate {
  ref: string      // "owner/repo#123"
  title: string
  repo: string
  number: number
  body: string     // 800-char excerpt
  url: string
}

export interface DispatchGroupResponse {
  status: 'spawned' | 'claim_failed' | 'no_account' | string
  sessionId?: string
  sessionName?: string
}
```

### New hooks (`web/src/api/hooks.ts`)

- `useBacklogCandidates(enabled: boolean)` — `GET /automations/backlog-runner/candidates`, 30 s stale time; only fetches when `enabled`
- `useDispatchBacklogGroup()` — `useMutation` posting to `/automations/backlog-runner/dispatch`; invalidates `automations` and `backlogGroups` query keys on success

### `BacklogGroupsPanel.tsx` changes

- `GroupRow` gains an `onDispatch?: () => void` prop
- "Dispatch" button rendered on unconsumed rows (`!group.consumedAt`): small ghost button, top-right of row
- Loading/disabled state while mutation in flight
- On success: toast "Session spawned: <name>" (shadcn `useToast`); button label reverts

`BacklogGroupsPanel` receives `onDispatch?: (refs: string[]) => void` from parent and wires it into each `GroupRow`.

### New `ManualDispatchDialog.tsx` (`web/src/components/`)

Props:
```typescript
{ open: boolean; onClose(): void; onOpenSession?(id: string): void }
```

Behaviour:
- Fetches `useBacklogCandidates(open)` — lazy, only when dialog is open
- Search input (client-side filter on `title` + `ref`)
- Issues grouped by `repo`; each row has a checkbox
- Dispatch button disabled when nothing selected
- On success: shows inline "Session spawned: <name>" with link; closes after 1.5 s
- On error: shows error message inline

### `AutomationsTab.tsx` changes

In `AutomationLiveCard`, inside the backlog-runner section (alongside `BacklogGroupsPanel`):
- Add state: `const [customDispatch, setCustomDispatch] = useState(false)`
- Add "Custom dispatch…" button (small, ghost, above or below `BacklogGroupsPanel`)
- Render `<ManualDispatchDialog open={customDispatch} onClose={...} onOpenSession={onOpenSession} />`
- Pass `onDispatch` down to `BacklogGroupsPanel` (wraps the `useDispatchBacklogGroup` mutation)

## Tests

### Backend (`server/tests/test_backlog_dispatch.py`)

- `test_backlog_runner_dispatch_spawned` — happy path, single ref
- `test_backlog_runner_dispatch_multi_ref` — two refs bundled; batch.issues has both
- `test_backlog_runner_dispatch_claim_failed` — `_backlog_claim` returns False → `"claim_failed"`
- `test_backlog_runner_dispatch_no_account` — no healthy account → `"no_account"`
- `test_backlog_candidates_api` — `GET /api/automations/backlog-runner/candidates` returns list with expected shape
- `test_backlog_dispatch_api_empty_refs` — 400 when refs is empty
- `test_remote_orch_has_backlog_dispatch` — parity test: `hasattr(RemoteOrchestrator, "backlog_runner_dispatch")`
- `test_remote_orch_has_backlog_candidates` — `hasattr(RemoteOrchestrator, "backlog_candidates")`

### Frontend (`web/src/components/BacklogGroupsPanel.test.tsx`, `ManualDispatchDialog.test.tsx`)

- Dispatch button visible on unconsumed group, absent when `consumedAt` is set
- Click dispatch → mutation called with group's refs
- Loading state disables button while in flight
- `ManualDispatchDialog`: renders grouped candidates, search filters by title, dispatch button disabled when nothing selected, calls mutation with selected refs

## Out of scope

- Editing AI group membership (add/remove issues from a planned group)
- Reordering the plan
- Multi-group parallel dispatch
- Dispatching issues outside `enabled_repos`
