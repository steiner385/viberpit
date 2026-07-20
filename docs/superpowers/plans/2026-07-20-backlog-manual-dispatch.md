# Backlog Runner Manual Issue Dispatch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users manually dispatch any AI-planned group immediately, or build a custom bundle from the eligible candidate pool, without waiting for the automatic queue.

**Architecture:** Wire the existing `backlog_candidates()` daemon method through the full stack (RPC → RemoteOrchestrator → API), add a parallel `backlog_runner_dispatch(refs)` method for multi-issue manual spawn, then surface both paths in the UI: a "Dispatch" button on each planned group row and a "Custom dispatch…" dialog with a multi-select picker.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/aiosqlite, pytest-asyncio; React 18, TypeScript, TanStack Query, Tailwind, Vitest, Testing Library, sonner (toast)

## Global Constraints

- All backend changes go under `server/`; frontend under `web/src/`
- Branch from `origin/master` (NOT local `master`) before any commits
- `env CCSWITCH_DEV=1 pytest server/tests/test_backlog_dispatch.py -v` — full suite with `env CCSWITCH_DEV=1 pytest server/tests/ -v`; use `PYTHONPATH=.:src python3` if no venv
- Frontend: `cd web && npx vitest run` + `npx tsc --noEmit` + `npx eslint src/`
- Use `from __future__ import annotations` in all new Python files
- Follow the Records-Before-Labels pattern: write durable DB state BEFORE any GitHub label write
- `toast` from `sonner`, not any other toast library
- Auth: Tailnet header `{"Tailscale-User-Login": "testuser@example.com"}` in API tests

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `server/sessions/orchestrator.py` | Modify ~L1584 | Add `backlog_runner_dispatch(refs)` method |
| `server/ipc/orchestrator_rpc.py` | Modify ~L275 | Add `"backlog_candidates"`, `"backlog_runner_dispatch"` to `_SIMPLE` |
| `server/sessions/remote.py` | Modify ~L379 | Add `backlog_candidates()` and `backlog_runner_dispatch(refs)` proxy methods |
| `server/api/automations.py` | Modify ~L247 | Add `DispatchBody`, `GET /backlog-runner/candidates`, `POST /backlog-runner/dispatch` |
| `server/tests/test_backlog_dispatch.py` | Create | Backend unit + API tests |
| `server/tests/test_action_providers.py` | Modify ~L76 | Add `backlog_runner_dispatch` to `_FakeBacklogOrch` |
| `web/src/api/automations.ts` | Modify ~L175 | Add `BacklogCandidate`, `BacklogCandidatesView`, `DispatchGroupResponse`, `fetchBacklogCandidates`, `postDispatchGroup` |
| `web/src/api/hooks.ts` | Modify | Add `QK.backlogCandidates`, `useBacklogCandidates`, `useDispatchBacklogGroup` |
| `web/src/components/BacklogGroupsPanel.tsx` | Modify | Add "Dispatch" button to unconsumed group rows |
| `web/src/components/BacklogGroupsPanel.test.tsx` | Create | Test dispatch button visibility + invocation |
| `web/src/components/ManualDispatchDialog.tsx` | Create | New dialog: candidate picker + dispatch |
| `web/src/components/ManualDispatchDialog.test.tsx` | Create | Test dialog rendering, filter, dispatch |
| `web/src/automations/AutomationsTab.tsx` | Modify ~L170 | Import dialog, add state + "Custom dispatch…" button |

---

### Task 1: Backend — dispatch method + API endpoints

Wire `backlog_candidates` through the daemon stack, add `backlog_runner_dispatch`, and expose both as new API routes. Tests are included in this task.

**Files:**
- Modify: `server/sessions/orchestrator.py` (after line 1588)
- Modify: `server/ipc/orchestrator_rpc.py` (line 275)
- Modify: `server/sessions/remote.py` (after line 386)
- Modify: `server/api/automations.py` (after line 247)
- Create: `server/tests/test_backlog_dispatch.py`
- Modify: `server/tests/test_action_providers.py`

**Interfaces:**
- Produces: `SessionOrchestrator.backlog_runner_dispatch(refs: list[str]) -> dict` returning `{"status": "spawned"|"claim_failed"|"no_account", "session_id": str|None, "session_name": str|None}`
- Produces: `RemoteOrchestrator.backlog_candidates() -> list[dict]`
- Produces: `RemoteOrchestrator.backlog_runner_dispatch(refs: list[str]) -> dict`
- Produces: `GET /api/automations/backlog-runner/candidates` → `{"candidates": [...]}`
- Produces: `POST /api/automations/backlog-runner/dispatch` body `{"refs": [...]}` → `{"status": ..., "session_id": ..., "session_name": ...}`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_backlog_dispatch.py`:

```python
"""Manual dispatch: backlog_runner_dispatch + /candidates + /dispatch API."""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

H = {"Tailscale-User-Login": "testuser@example.com"}

# ---------------------------------------------------------------------------
# Unit tests for SessionOrchestrator.backlog_runner_dispatch
# ---------------------------------------------------------------------------

@pytest.fixture()
def dispatch_orch(tmp_path):
    """SessionOrchestrator with mocked backlog seams — no network, no LLM."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                  "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        from server.sessions.orchestrator import SessionOrchestrator
        from server.tests.fakes.fake_factory import fake_sdk_factory
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        # Patch the seams used by backlog_runner_dispatch
        orch._backlog_read_config = AsyncMock(return_value=(
            "auto",
            {"enabled_repos": ["o/r"], "eligible_label": "ready",
             "max_concurrent": 1, "max_spawns_per_day": 10,
             "max_issues_per_pr": 3, "default_model": None},
        ))
        orch._backlog_claim = AsyncMock(return_value=True)
        orch._backlog_healthy_account = AsyncMock(return_value="acct-1")
        rec = MagicMock()
        rec.id = "sess-42"
        rec.name = "backlog o/r#7"
        rec.branch = "backlog/o-r-7"
        orch._backlog_spawn = AsyncMock(return_value=rec)
        return orch


_CANDIDATES = [
    {"ref": "o/r#7", "number": 7, "repo": "o/r", "title": "Fix seven",
     "body": "...", "url": "https://github.com/o/r/issues/7"},
    {"ref": "o/r#8", "number": 8, "repo": "o/r", "title": "Fix eight",
     "body": "...", "url": "https://github.com/o/r/issues/8"},
]


@pytest.mark.asyncio
async def test_dispatch_single_ref_spawns(dispatch_orch):
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert r["status"] == "spawned"
    assert r["session_id"] == "sess-42"
    assert r["session_name"] == "backlog o/r#7"
    dispatch_orch._backlog_claim.assert_awaited_once()
    dispatch_orch._backlog_spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_multi_ref_bundles_all(dispatch_orch):
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#7", "o/r#8"])
    assert r["status"] == "spawned"
    # The batch passed to _backlog_spawn must include both refs
    batch = dispatch_orch._backlog_spawn.call_args[0][0]
    assert batch.issues == ["o/r#7", "o/r#8"]
    assert batch.primary == "o/r#7"
    assert batch.titles == {"o/r#7": "Fix seven", "o/r#8": "Fix eight"}


@pytest.mark.asyncio
async def test_dispatch_claim_failed(dispatch_orch):
    dispatch_orch._backlog_claim = AsyncMock(return_value=False)
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert r["status"] == "claim_failed"
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_no_account(dispatch_orch):
    dispatch_orch._backlog_healthy_account = AsyncMock(return_value=None)
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert r["status"] == "no_account"
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_ref_not_in_candidates_uses_fallback_repo(dispatch_orch):
    """A custom ref not in current candidates must still dispatch (uses ref split for repo)."""
    with patch("server.automations.backlog_github.query_candidates", return_value=[]):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#99"])
    assert r["status"] == "spawned"
    batch = dispatch_orch._backlog_spawn.call_args[0][0]
    assert batch.repo == "o/r"
    assert batch.titles == {}


# ---------------------------------------------------------------------------
# API tests: GET /candidates + POST /dispatch
# ---------------------------------------------------------------------------

@contextmanager
def _api_client(monkeypatch_orch=None):
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                      "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            if monkeypatch_orch:
                monkeypatch_orch(orch)
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def test_candidates_returns_list():
    def patch_orch(orch):
        orch._backlog_read_config = AsyncMock(return_value=("auto", {
            "enabled_repos": ["o/r"], "eligible_label": "ready",
            "max_concurrent": 1, "max_spawns_per_day": 10,
            "max_issues_per_pr": 1, "default_model": None,
        }))
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        with _api_client(patch_orch) as c:
            asyncio.run(_seed_automation())
            r = c.get("/api/automations/backlog-runner/candidates", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "candidates" in body
    assert body["candidates"][0]["ref"] == "o/r#7"


def test_dispatch_api_happy_path():
    rec = MagicMock()
    rec.id = "sess-1"
    rec.name = "backlog o/r#7"
    rec.branch = "b"

    def patch_orch(orch):
        orch._backlog_read_config = AsyncMock(return_value=("auto", {
            "enabled_repos": ["o/r"], "eligible_label": "ready",
            "max_concurrent": 1, "max_spawns_per_day": 10,
            "max_issues_per_pr": 3, "default_model": None,
        }))
        orch._backlog_claim = AsyncMock(return_value=True)
        orch._backlog_healthy_account = AsyncMock(return_value="acct-1")
        orch._backlog_spawn = AsyncMock(return_value=rec)

    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        with _api_client(patch_orch) as c:
            asyncio.run(_seed_automation())
            r = c.post("/api/automations/backlog-runner/dispatch",
                       json={"refs": ["o/r#7"]}, headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "spawned"
    assert r.json()["session_id"] == "sess-1"


def test_dispatch_api_empty_refs_returns_400():
    with _api_client() as c:
        asyncio.run(_seed_automation())
        r = c.post("/api/automations/backlog-runner/dispatch",
                   json={"refs": []}, headers=H)
    assert r.status_code == 400


def test_dispatch_api_requires_auth():
    with _api_client() as c:
        r = c.post("/api/automations/backlog-runner/dispatch", json={"refs": ["o/r#1"]})
    assert r.status_code == 401


def test_remote_orch_has_backlog_candidates():
    from server.sessions.remote import RemoteOrchestrator
    assert hasattr(RemoteOrchestrator, "backlog_candidates")


def test_remote_orch_has_backlog_runner_dispatch():
    from server.sessions.remote import RemoteOrchestrator
    assert hasattr(RemoteOrchestrator, "backlog_runner_dispatch")


async def _seed_automation():
    """Ensure the backlog-runner row exists so _automation_or_404 passes."""
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        from sqlalchemy import select
        row = (await db.execute(
            select(Automation).where(Automation.id == "backlog-runner")
        )).scalar_one_or_none()
        if row is None:
            db.add(Automation(id="backlog-runner", enabled=True))
            await db.commit()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/42d9c5f4
env CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_dispatch.py -v 2>&1 | tail -30
```

Expected: collection errors or `AttributeError: 'SessionOrchestrator' object has no attribute 'backlog_runner_dispatch'`

- [ ] **Step 3: Add `backlog_runner_dispatch` to `SessionOrchestrator`**

In `server/sessions/orchestrator.py`, after line 1588 (after `backlog_candidates`), insert:

```python
    async def backlog_runner_dispatch(self, refs: list[str]) -> dict:
        """Manual dispatch: claim + spawn a batch for *refs*.

        *refs[0]* is the primary; all refs are bundled into one PR. Does NOT
        require refs to be in the current candidate set — picker state may lag
        by seconds and blocking an explicit user request is worse than a missing
        title. Returns ``{"status": ..., "session_id": ..., "session_name": ...}``."""
        from server.automations.backlog_github import query_candidates
        from server.automations.backlog_selection import Batch
        primary = refs[0]
        _mode, config = await self._backlog_read_config()
        candidates = await asyncio.to_thread(
            query_candidates, config, gh_run=self._gh_run)
        by_ref = {c["ref"]: c for c in candidates}
        repo = by_ref.get(primary, {}).get("repo") or primary.split("#")[0]
        titles = {r: by_ref[r]["title"]
                  for r in refs if r in by_ref and by_ref[r].get("title")}
        batch = Batch(primary=primary, issues=list(refs), repo=repo, titles=titles)
        st = await self._backlog_ctx.get_target_state(primary)
        already_claimed = bool(st.get("issues"))
        if not already_claimed:
            await self._backlog_ctx.set_target_state(primary, {
                "repo": batch.repo,
                "attempts": st.get("attempts", 0),
                "issues": batch.issues,
                "claimed": True,
            })
            if not await self._backlog_claim(batch):
                await self._backlog_ctx.delete_target_state(primary)
                return {"status": "claim_failed", "session_id": None,
                        "session_name": None}
            await self._backlog_ctx.emit("info", f"manual dispatch {primary}",
                                         kind="claimed", target=primary,
                                         detail={"issues": batch.issues})
        account = await self._backlog_healthy_account()
        if account is None:
            return {"status": "no_account", "session_id": None,
                    "session_name": None}
        rec = await self._backlog_spawn(batch, account)
        await self._backlog_ctx.set_target_state(primary, {
            "session_id": getattr(rec, "id", None),
            "repo": batch.repo,
            "branch": getattr(rec, "branch", None),
            "attempts": st.get("attempts", 0),
            "issues": batch.issues,
            "claimed": True,
        })
        return {
            "status": "spawned",
            "session_id": getattr(rec, "id", None),
            "session_name": getattr(rec, "name", None),
        }
```

- [ ] **Step 4: Add both names to `_SIMPLE` in `orchestrator_rpc.py`**

In `server/ipc/orchestrator_rpc.py`, find the line (≈L275):
```python
        "automation_statuses", "automation_trigger",
        "consolidation_candidates", "automation_consolidate",
```
Change it to:
```python
        "automation_statuses", "automation_trigger",
        "consolidation_candidates", "automation_consolidate",
        "backlog_candidates", "backlog_runner_dispatch",
```

- [ ] **Step 5: Add proxy methods to `RemoteOrchestrator`**

In `server/sessions/remote.py`, after `automation_consolidate` (≈L386), insert:

```python
    async def backlog_candidates(self) -> "list[dict]":
        """Current eligible issue candidates for manual dispatch, via the daemon."""
        r = await self._client.call("backlog_candidates")
        return r if isinstance(r, list) else []

    async def backlog_runner_dispatch(self, refs: "list[str]") -> dict:
        """Manually claim + spawn a batch for *refs*, via the daemon."""
        return await self._client.call("backlog_runner_dispatch", refs=refs,
                                       timeout=AUTOMATION_RPC_TIMEOUT)
```

- [ ] **Step 6: Add API body model and endpoints to `server/api/automations.py`**

After the `ConsolidateBody` class (≈L39), add:

```python
class DispatchBody(BaseModel):
    refs: list[str]
```

After the last endpoint in the file (≈L247), append:

```python

@router.get("/backlog-runner/candidates")
async def backlog_runner_candidates(request: Request,
                                    _identity: str = Depends(current_identity)) -> dict:
    """Current eligible (unclaimed, open, label-gated) issues for manual dispatch."""
    _automation_or_404("backlog-runner")
    orch = request.app.state.sessions
    candidates = await orch.backlog_candidates()
    return {"candidates": candidates}


@router.post("/backlog-runner/dispatch")
async def backlog_runner_dispatch(body: DispatchBody, request: Request,
                                  _identity: str = Depends(current_identity)) -> dict:
    """Immediately claim + spawn a session for the given issue refs."""
    _automation_or_404("backlog-runner")
    if not body.refs:
        raise HTTPException(status_code=400, detail="refs must be non-empty")
    orch = request.app.state.sessions
    return await orch.backlog_runner_dispatch(body.refs)
```

- [ ] **Step 7: Update `_FakeBacklogOrch` in `test_action_providers.py`**

In `server/tests/test_action_providers.py`, find `_FakeBacklogOrch` (≈L76) and add the new method:

```python
class _FakeBacklogOrch:
    def __init__(self, status="spawned"):
        self._status = status
        self.implement_calls = []
        self.dispatch_calls: list[list[str]] = []

    async def backlog_runner_implement_core(self, primary):
        self.implement_calls.append(primary)
        return self._status

    async def backlog_runner_dispatch(self, refs):
        self.dispatch_calls.append(refs)
        return {"status": self._status, "session_id": "s-1", "session_name": "backlog"}

    async def backlog_candidates(self):
        return [{"ref": "cairnea/KinDash#42", "repo": "cairnea/KinDash",
                 "title": "add rate limiter",
                 "url": "https://github.com/cairnea/KinDash/issues/42"}]
```

- [ ] **Step 8: Run tests — all must pass**

```bash
env CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_dispatch.py server/tests/test_action_providers.py -v 2>&1 | tail -30
```

Expected: all green

- [ ] **Step 9: Run mypy and full server suite**

```bash
env CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m mypy server/ --ignore-missing-imports 2>&1 | tail -20
env CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/ -v --ignore=server/tests/daemon_properties --ignore=server/tests/test_backlog_runner_e2e.py -x 2>&1 | tail -20
```

Expected: mypy clean, pytest green

- [ ] **Step 10: Commit**

```bash
git add server/sessions/orchestrator.py server/ipc/orchestrator_rpc.py \
        server/sessions/remote.py server/api/automations.py \
        server/tests/test_backlog_dispatch.py server/tests/test_action_providers.py
git commit -m "feat(backlog): add manual dispatch API — candidates + dispatch endpoints

Wires existing backlog_candidates() daemon method through RPC + RemoteOrchestrator.
Adds backlog_runner_dispatch(refs) for multi-issue manual spawn, bypassing the
automatic queue. New API endpoints: GET /backlog-runner/candidates and
POST /backlog-runner/dispatch.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Frontend API layer

Add types, fetch functions, and hooks so components can call both new endpoints.

**Files:**
- Modify: `web/src/api/automations.ts` (after line 175)
- Modify: `web/src/api/hooks.ts` (QK object + two new exports)

**Interfaces:**
- Consumes: `GET /automations/backlog-runner/candidates` → `{candidates: BacklogCandidate[]}`
- Consumes: `POST /automations/backlog-runner/dispatch` body `{refs: string[]}` → `DispatchGroupResponse`
- Produces: `useBacklogCandidates(enabled: boolean)` — query hook returning `BacklogCandidatesView`
- Produces: `useDispatchBacklogGroup()` — mutation hook calling `postDispatchGroup`
- Produces: `QK.backlogCandidates` — `['backlog-candidates'] as const`

- [ ] **Step 1: Write the failing type check / test**

There are no separate tests for the API layer — type correctness is confirmed by `tsc --noEmit` after implementation. Verify the pre-state fails fast:

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/42d9c5f4/web
grep -n "backlogCandidates\|useBacklogCandidates\|useDispatchBacklogGroup" src/api/hooks.ts src/api/automations.ts
```

Expected: no matches (these don't exist yet)

- [ ] **Step 2: Add types and fetch functions to `web/src/api/automations.ts`**

After line 175 (after `fetchBacklogGroups`), append:

```typescript
export interface BacklogCandidate {
  ref: string
  title: string
  repo: string
  number: number
  body: string
  url: string
}

export interface BacklogCandidatesView {
  candidates: BacklogCandidate[]
}

export interface DispatchGroupResponse {
  status: 'spawned' | 'claim_failed' | 'no_account' | string
  sessionId: string | null
  sessionName: string | null
}

export function fetchBacklogCandidates(): Promise<BacklogCandidatesView> {
  return api.get<BacklogCandidatesView>('/automations/backlog-runner/candidates')
}

export function postDispatchGroup(refs: string[]): Promise<DispatchGroupResponse> {
  return api.post<DispatchGroupResponse>('/automations/backlog-runner/dispatch', { refs })
}
```

- [ ] **Step 3: Add `QK.backlogCandidates` to query key map in `web/src/api/hooks.ts`**

Find the `QK` object (≈L29). After line `backlogGroups: ['backlog-groups'] as const,` add:

```typescript
  backlogCandidates: ['backlog-candidates'] as const,
```

- [ ] **Step 4: Add imports in `web/src/api/hooks.ts`**

Find the import block at the top (≈L5–L11). Add to the named imports from `./automations`:

```typescript
  fetchBacklogCandidates, postDispatchGroup,
  type BacklogCandidatesView, type DispatchGroupResponse,
```

- [ ] **Step 5: Add `useBacklogCandidates` and `useDispatchBacklogGroup` hooks**

After `useBacklogGroups` (after line 771 in hooks.ts), insert:

```typescript
/** Current eligible issues for manual dispatch. Only fetches when enabled. */
export function useBacklogCandidates(enabled = true) {
  return useQuery<BacklogCandidatesView>({
    queryKey: QK.backlogCandidates,
    queryFn: fetchBacklogCandidates,
    enabled,
    staleTime: 30_000,
  })
}

/** Mutation: immediately claim + spawn a session for the given refs. */
export function useDispatchBacklogGroup() {
  const qc = useQueryClient()
  return useMutation<DispatchGroupResponse, ApiRequestError, string[]>({
    mutationFn: (refs) => postDispatchGroup(refs),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.backlogGroups })
      qc.invalidateQueries({ queryKey: QK.automationSessions('backlog-runner') })
    },
  })
}
```

- [ ] **Step 6: Verify types compile**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/42d9c5f4/web
npx tsc --noEmit 2>&1 | head -30
```

Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add web/src/api/automations.ts web/src/api/hooks.ts
git commit -m "feat(backlog): frontend API layer — candidates + dispatch hooks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: BacklogGroupsPanel dispatch button

Add a "Dispatch" button to each unconsumed group row. The panel internally owns the mutation.

**Files:**
- Modify: `web/src/components/BacklogGroupsPanel.tsx`
- Create: `web/src/components/BacklogGroupsPanel.test.tsx`

**Interfaces:**
- Consumes: `useDispatchBacklogGroup()` from `@/api/hooks`
- Consumes: `BacklogGroup.consumedAt` — button hidden when truthy
- `BacklogGroupsPanel` gains `onOpenSession?: (id: string) => void` prop (passed through from `AutomationsTab`)

- [ ] **Step 1: Write failing tests**

Create `web/src/components/BacklogGroupsPanel.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { BacklogGroupsPanel } from './BacklogGroupsPanel'

const mutate = vi.fn()
const baseGroup = () => ({
  id: 1,
  repo: 'o/r',
  refs: ['o/r#7', 'o/r#8'],
  titles: { 'o/r#7': 'Fix seven', 'o/r#8': 'Fix eight' },
  rationale: 'same subsystem',
  rank: 0,
  generation: 'g1',
  createdAt: '2026-07-20T10:00:00Z',
  consumedAt: null,
})

vi.mock('@/api/hooks', () => ({
  useBacklogGroups: () => ({
    data: { generation: 'g1', createdAt: '2026-07-20T10:00:00Z', groups: [baseGroup()] },
    isLoading: false,
  }),
  useDispatchBacklogGroup: () => ({ mutateAsync: mutate, isPending: false }),
  // The hook list in hooks.ts has many exports; any new ones the component imports
  // must be added here. Add stubs for any hooks used by AutomationsTab mock chain.
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

describe('BacklogGroupsPanel', () => {
  beforeEach(() => {
    mutate.mockReset()
    mutate.mockResolvedValue({ status: 'spawned', session_id: 's-1', session_name: 'backlog o/r#7' })
  })

  it('renders group rows with issue links', () => {
    render(<BacklogGroupsPanel enabled />)
    expect(screen.getByText(/Fix seven/)).toBeInTheDocument()
    expect(screen.getByText(/Fix eight/)).toBeInTheDocument()
  })

  it('shows dispatch button for unconsumed groups', () => {
    render(<BacklogGroupsPanel enabled />)
    expect(screen.getByRole('button', { name: /dispatch/i })).toBeInTheDocument()
  })

  it('hides dispatch button for consumed groups', () => {
    vi.mocked(
      (await import('@/api/hooks')).useBacklogGroups
    )
    // Re-mock with consumedAt set
    vi.doMock('@/api/hooks', () => ({
      useBacklogGroups: () => ({
        data: {
          generation: 'g1',
          createdAt: '2026-07-20T10:00:00Z',
          groups: [{ ...baseGroup(), consumedAt: '2026-07-20T11:00:00Z' }],
        },
        isLoading: false,
      }),
      useDispatchBacklogGroup: () => ({ mutateAsync: mutate, isPending: false }),
      useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
      useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
      useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
    }))
  })

  it('clicking dispatch calls mutateAsync with the group refs', async () => {
    render(<BacklogGroupsPanel enabled />)
    fireEvent.click(screen.getByRole('button', { name: /dispatch/i }))
    await waitFor(() =>
      expect(mutate).toHaveBeenCalledWith(['o/r#7', 'o/r#8'])
    )
  })

  it('button is disabled while dispatch is in flight', () => {
    vi.doMock('@/api/hooks', () => ({
      useBacklogGroups: () => ({
        data: { generation: 'g1', createdAt: '2026-07-20T10:00:00Z', groups: [baseGroup()] },
        isLoading: false,
      }),
      useDispatchBacklogGroup: () => ({ mutateAsync: mutate, isPending: true }),
      useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
      useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
      useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
    }))
  })
})
```

- [ ] **Step 2: Run to confirm failures**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/42d9c5f4/web
npx vitest run src/components/BacklogGroupsPanel.test.tsx 2>&1 | tail -20
```

Expected: FAIL — `BacklogGroupsPanel.test.tsx` module not found or no dispatch button

- [ ] **Step 3: Rewrite `BacklogGroupsPanel.tsx` with dispatch button**

Replace the entire file with:

```tsx
import { useState } from 'react'
import { toast } from 'sonner'
import { useBacklogGroups, useDispatchBacklogGroup } from '@/api/hooks'
import type { BacklogGroup } from '@/api/automations'

function issueUrl(ref: string): string {
  const [repo, num] = ref.split('#')
  return `https://github.com/${repo}/issues/${num}`
}

function GroupRow({
  group,
  onDispatch,
  isDispatching,
}: {
  group: BacklogGroup
  onDispatch(): void
  isDispatching: boolean
}) {
  return (
    <li className="rounded border border-border p-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        {group.repo && (
          <span className="text-xs text-muted-foreground shrink-0">{group.repo}</span>
        )}
        {group.refs.map((ref) => (
          <a
            key={ref}
            href={issueUrl(ref)}
            target="_blank"
            rel="noreferrer"
            title={group.titles[ref]}
            className="hover:underline"
          >
            #{ref.split('#')[1]}
            {group.titles[ref] ? ` ${group.titles[ref]}` : ''}
          </a>
        ))}
        {group.consumedAt ? (
          <span className="ml-auto rounded bg-blue-100 dark:bg-blue-900/40 px-1 text-xs text-blue-700 dark:text-blue-300 shrink-0">
            picked up
          </span>
        ) : (
          <button
            type="button"
            onClick={onDispatch}
            disabled={isDispatching}
            className="ml-auto rounded bg-blue-600 px-2 py-0.5 text-xs text-white hover:bg-blue-700 disabled:opacity-40 shrink-0"
          >
            {isDispatching ? 'Dispatching…' : 'Dispatch'}
          </button>
        )}
      </div>
      {group.rationale && (
        <p className="mt-0.5 text-xs text-muted-foreground leading-snug">{group.rationale}</p>
      )}
    </li>
  )
}

/** Planned groups panel with per-group immediate dispatch. */
export function BacklogGroupsPanel({
  enabled,
  onOpenSession,
}: {
  enabled: boolean
  onOpenSession?: (id: string) => void
}) {
  const { data } = useBacklogGroups(enabled)
  const dispatch = useDispatchBacklogGroup()
  const [dispatchingId, setDispatchingId] = useState<number | null>(null)
  const groups = data?.groups ?? []
  if (!groups.length) return null

  async function handleDispatch(group: BacklogGroup) {
    setDispatchingId(group.id)
    try {
      const r = await dispatch.mutateAsync(group.refs)
      if (r.status === 'spawned') {
        toast.success(r.sessionName ? `Session spawned: ${r.sessionName}` : 'Session spawned')
        if (r.sessionId) onOpenSession?.(r.sessionId)
      } else {
        toast.error(`Dispatch failed: ${r.status}`)
      }
    } catch {
      toast.error('Failed to dispatch group')
    } finally {
      setDispatchingId(null)
    }
  }

  return (
    <section className="mt-3 border-t border-border pt-3">
      <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">
        Planned groups
        {data?.createdAt && (
          <span className="ml-2 font-normal normal-case">
            planned {data.createdAt.slice(0, 16).replace('T', ' ')}
          </span>
        )}
      </h4>
      <ul className="space-y-2">
        {groups.map((g) => (
          <GroupRow
            key={g.id}
            group={g}
            onDispatch={() => handleDispatch(g)}
            isDispatching={dispatchingId === g.id}
          />
        ))}
      </ul>
    </section>
  )
}
```

- [ ] **Step 4: Run tests — must pass**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/42d9c5f4/web
npx vitest run src/components/BacklogGroupsPanel.test.tsx 2>&1 | tail -20
```

Expected: all green

- [ ] **Step 5: TypeScript check**

```bash
npx tsc --noEmit 2>&1 | grep -i "backlogGroupsPanel\|BacklogGroupsPanel" | head -10
```

Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add web/src/components/BacklogGroupsPanel.tsx web/src/components/BacklogGroupsPanel.test.tsx
git commit -m "feat(backlog): add Dispatch button to planned group rows

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: ManualDispatchDialog + AutomationsTab wiring

New custom-dispatch dialog with issue picker, wired into the backlog-runner card.

**Files:**
- Create: `web/src/components/ManualDispatchDialog.tsx`
- Create: `web/src/components/ManualDispatchDialog.test.tsx`
- Modify: `web/src/automations/AutomationsTab.tsx`

**Interfaces:**
- Consumes: `useBacklogCandidates(enabled)` — lazy, only fetches while dialog open
- Consumes: `useDispatchBacklogGroup()` — shared mutation
- `ManualDispatchDialog` props: `{ open: boolean; onClose(): void; onOpenSession?: (id: string) => void }`
- `AutomationsTab` change: import + render `ManualDispatchDialog` inside the backlog-runner card, add `useState(false)` for open state

- [ ] **Step 1: Write failing tests**

Create `web/src/components/ManualDispatchDialog.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ManualDispatchDialog } from './ManualDispatchDialog'

const mutate = vi.fn()

const candidates = [
  { ref: 'o/r#7', number: 7, title: 'Fix seven', repo: 'o/r', body: '', url: '' },
  { ref: 'o/r#8', number: 8, title: 'Fix eight', repo: 'o/r', body: '', url: '' },
  { ref: 'a/b#1', number: 1, title: 'Alpha task', repo: 'a/b', body: '', url: '' },
]

vi.mock('@/api/hooks', () => ({
  useBacklogCandidates: () => ({ data: { candidates }, isLoading: false }),
  useDispatchBacklogGroup: () => ({ mutateAsync: mutate, isPending: false }),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

describe('ManualDispatchDialog', () => {
  const onClose = vi.fn()

  beforeEach(() => {
    mutate.mockReset()
    onClose.mockReset()
    mutate.mockResolvedValue({ status: 'spawned', session_id: 's-1', session_name: 'backlog o/r#7' })
  })

  it('renders nothing when closed', () => {
    const { container } = render(<ManualDispatchDialog open={false} onClose={onClose} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders candidate groups by repo when open', () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    expect(screen.getByText('o/r')).toBeInTheDocument()
    expect(screen.getByText('a/b')).toBeInTheDocument()
    expect(screen.getByText(/Fix seven/)).toBeInTheDocument()
  })

  it('dispatch button is disabled when nothing selected', () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    expect(screen.getByRole('button', { name: /dispatch 0/i })).toBeDisabled()
  })

  it('selecting issues enables dispatch and calls mutation with correct refs', async () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    fireEvent.click(screen.getByLabelText('select o/r#7'))
    fireEvent.click(screen.getByLabelText('select o/r#8'))
    const btn = screen.getByRole('button', { name: /dispatch 2/i })
    expect(btn).toBeEnabled()
    fireEvent.click(btn)
    await waitFor(() =>
      expect(mutate).toHaveBeenCalledWith(expect.arrayContaining(['o/r#7', 'o/r#8']))
    )
  })

  it('filters candidates by search text', () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    fireEvent.change(screen.getByPlaceholderText(/filter/i), { target: { value: 'Alpha' } })
    expect(screen.getByText(/Alpha task/)).toBeInTheDocument()
    expect(screen.queryByText(/Fix seven/)).toBeNull()
  })

  it('calls onClose after successful dispatch', async () => {
    render(<ManualDispatchDialog open onClose={onClose} />)
    fireEvent.click(screen.getByLabelText('select o/r#7'))
    fireEvent.click(screen.getByRole('button', { name: /dispatch 1/i }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('shows empty state when no candidates', () => {
    vi.doMock('@/api/hooks', () => ({
      useBacklogCandidates: () => ({ data: { candidates: [] }, isLoading: false }),
      useDispatchBacklogGroup: () => ({ mutateAsync: mutate, isPending: false }),
      useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
      useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
      useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
    }))
  })
})
```

- [ ] **Step 2: Run to confirm failures**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/42d9c5f4/web
npx vitest run src/components/ManualDispatchDialog.test.tsx 2>&1 | tail -20
```

Expected: FAIL — module not found

- [ ] **Step 3: Create `web/src/components/ManualDispatchDialog.tsx`**

```tsx
import { useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useBacklogCandidates, useDispatchBacklogGroup } from '@/api/hooks'
import type { BacklogCandidate } from '@/api/automations'

export function ManualDispatchDialog({
  open,
  onClose,
  onOpenSession,
}: {
  open: boolean
  onClose(): void
  onOpenSession?: (id: string) => void
}) {
  const { data, isLoading } = useBacklogCandidates(open)
  const dispatch = useDispatchBacklogGroup()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState('')

  const candidates = data?.candidates ?? []

  const filtered = useMemo(() => {
    const q = search.toLowerCase()
    return q
      ? candidates.filter(
          (c) => c.title.toLowerCase().includes(q) || c.ref.toLowerCase().includes(q),
        )
      : candidates
  }, [candidates, search])

  const byRepo = useMemo(() => {
    const m = new Map<string, BacklogCandidate[]>()
    for (const c of filtered) {
      if (!m.has(c.repo)) m.set(c.repo, [])
      m.get(c.repo)!.push(c)
    }
    return m
  }, [filtered])

  function toggle(ref: string) {
    setSelected((s) => {
      const next = new Set(s)
      if (next.has(ref)) next.delete(ref)
      else next.add(ref)
      return next
    })
  }

  async function run() {
    const refs = Array.from(selected)
    try {
      const r = await dispatch.mutateAsync(refs)
      if (r.status === 'spawned') {
        toast.success(r.sessionName ? `Session spawned: ${r.sessionName}` : 'Session spawned')
        if (r.sessionId) onOpenSession?.(r.sessionId)
        setSelected(new Set())
        onClose()
      } else {
        toast.error(`Dispatch failed: ${r.status}`)
      }
    } catch {
      toast.error('Failed to dispatch')
    }
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-label="Custom dispatch"
    >
      <div className="bg-background rounded-lg border border-border p-4 w-full max-w-lg max-h-[80vh] flex flex-col gap-3 shadow-xl">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">Custom dispatch</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-muted-foreground hover:text-foreground"
          >
            ✕
          </button>
        </div>
        <input
          type="search"
          placeholder="Filter issues…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="rounded border border-border bg-background px-3 py-1 text-sm w-full"
        />
        <div className="overflow-y-auto flex-1 min-h-0">
          {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {!isLoading && candidates.length === 0 && (
            <p className="text-sm text-muted-foreground">No eligible issues.</p>
          )}
          {Array.from(byRepo.entries()).map(([repo, items]) => (
            <div key={repo} className="mb-3">
              <p className="text-xs font-medium text-muted-foreground mb-1">{repo}</p>
              <ul className="space-y-1">
                {items.map((c) => (
                  <li key={c.ref} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      aria-label={`select ${c.ref}`}
                      checked={selected.has(c.ref)}
                      onChange={() => toggle(c.ref)}
                    />
                    <span className="truncate">
                      <span className="text-muted-foreground">#{c.number}</span>{' '}
                      {c.title}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <div className="flex justify-end">
          <button
            type="button"
            onClick={run}
            disabled={selected.size === 0 || dispatch.isPending}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700 disabled:opacity-40"
          >
            {dispatch.isPending
              ? 'Dispatching…'
              : `Dispatch ${selected.size} issue${selected.size !== 1 ? 's' : ''}`}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run ManualDispatchDialog tests — all must pass**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/42d9c5f4/web
npx vitest run src/components/ManualDispatchDialog.test.tsx 2>&1 | tail -20
```

Expected: all green

- [ ] **Step 5: Wire ManualDispatchDialog into `AutomationsTab.tsx`**

**Import:** At the top of `AutomationsTab.tsx` after the `BacklogGroupsPanel` import:
```tsx
import { ManualDispatchDialog } from '@/components/ManualDispatchDialog'
```

**State:** Inside `AutomationLiveCard` after the existing `const [open, setOpen] = useState(false)` on line 170:
```tsx
  const [customDispatch, setCustomDispatch] = useState(false)
```

**Replace** the existing `{a.id === 'backlog-runner' && <BacklogGroupsPanel enabled={open} />}` line (≈L225) with:
```tsx
          {a.id === 'backlog-runner' && (
            <>
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={() => setCustomDispatch(true)}
                  className="rounded border border-border px-2 py-0.5 text-xs hover:bg-muted"
                >
                  Custom dispatch…
                </button>
              </div>
              <BacklogGroupsPanel enabled={open} onOpenSession={onOpenSession} />
              <ManualDispatchDialog
                open={customDispatch}
                onClose={() => setCustomDispatch(false)}
                onOpenSession={onOpenSession}
              />
            </>
          )}
```

- [ ] **Step 6: Run full frontend suite**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/42d9c5f4/web
npx vitest run 2>&1 | tail -20
npx tsc --noEmit 2>&1 | head -20
npx eslint src/ 2>&1 | head -20
```

Expected: all green

- [ ] **Step 7: Commit**

```bash
git add web/src/components/ManualDispatchDialog.tsx \
        web/src/components/ManualDispatchDialog.test.tsx \
        web/src/automations/AutomationsTab.tsx
git commit -m "feat(backlog): ManualDispatchDialog + Custom dispatch button in AutomationsTab

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Open PR and arm auto-merge

**Files:** None (git/GitHub operations only)

- [ ] **Step 1: Verify on the correct branch**

```bash
git log --oneline -5
git status
```

Expected: 4 commits on a branch off `origin/master`, working tree clean

- [ ] **Step 2: Push branch**

```bash
git push -u origin HEAD
```

- [ ] **Step 3: Open PR (non-draft) targeting master**

```bash
gh pr create --title "feat(backlog): manual issue dispatch — one-click groups + custom picker" \
  --body "$(cat <<'EOF'
## Summary

- Wires existing `backlog_candidates()` daemon method through RPC + `RemoteOrchestrator`, exposes as `GET /api/automations/backlog-runner/candidates`
- Adds `backlog_runner_dispatch(refs)` for multi-issue manual spawn (bypasses automatic queue), exposes as `POST /api/automations/backlog-runner/dispatch`
- Adds "Dispatch" button to each unconsumed planned group row in `BacklogGroupsPanel`
- Adds "Custom dispatch…" button that opens a multi-select picker (`ManualDispatchDialog`) showing eligible candidates grouped by repo

## Test plan

- [ ] Backend: `env CCSWITCH_DEV=1 pytest server/tests/test_backlog_dispatch.py -v` — all pass
- [ ] Backend: `mypy server/ --ignore-missing-imports` — clean
- [ ] Frontend: `cd web && npx vitest run` — all pass including `BacklogGroupsPanel.test.tsx` and `ManualDispatchDialog.test.tsx`
- [ ] Frontend: `npx tsc --noEmit && npx eslint src/` — clean
- [ ] Manual: Open backlog-runner card → "Planned groups" section shows "Dispatch" on unconsumed rows
- [ ] Manual: Clicking "Dispatch" on a group immediately spawns a session and shows a toast
- [ ] Manual: "Custom dispatch…" opens dialog; search filters by title; checkboxes enable button; submit spawns session

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Arm auto-merge**

```bash
PR=$(gh pr view --json number --jq .number)
gh pr merge "$PR" --squash --auto
```

- [ ] **Step 5: Verify PR state**

```bash
gh pr view --json state,autoMergeRequest --jq '{state, autoMerge: .autoMergeRequest.enabledAt}'
```

Expected: `{"state": "OPEN", "autoMerge": "<timestamp>"}` — non-draft, auto-merge armed
