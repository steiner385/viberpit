# Sunset Adoption Capability (flag-gated) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Disable the unmanaged-session adoption capability behind a single env flag (`CCSWITCH_ENABLE_ADOPTION`), off by default, keeping all code and tests intact for later revival.

**Architecture:** Add the flag to the central `Settings` (both Pydantic and dataclass-fallback branches). Guard the two adoption routes so they return 404 when the flag is off. Expose the flag through the existing `/api/config` endpoint so the frontend hides `AdoptPanel` without polling `/adoptable`. No adoption logic is removed.

**Tech Stack:** Python FastAPI + Pydantic settings · React + TypeScript + React Query · pytest · vitest.

## Global Constraints

- Flag name: `CCSWITCH_ENABLE_ADOPTION`, bool, **default `False`**. Fallback truthy parse must match `enable_xvfb`: value `.lower() in ("1","true","yes","on")`.
- API when off: **404 Not Found** (not 403, not empty).
- Frontend when off: `AdoptPanel` **never mounts**.
- **Keep** `server/sessions/adopt.py`, `orchestrator.adopt_session()`, the `adopted_in_place` column, and `AdoptPanel.tsx` unchanged.
- Single source of truth: read the flag only via `server.config.get_settings()`.
- Run server tests with the prebuilt env per CLAUDE.md (fresh worktree has no built env): `<main-checkout>/.venv/bin/python -m pytest …`. Web checks run in `web/` with `node_modules` symlinked from the main checkout.

---

### Task 1: Add the `enable_adoption` flag and expose it via `/api/config`

**Files:**
- Modify: `server/config.py` (Pydantic `Settings` ~line 39; dataclass fallback ~line 100)
- Modify: `server/api/config.py:9-12`
- Test: `server/tests/test_config_api.py` (create)

**Interfaces:**
- Produces: `Settings.enable_adoption: bool` (default `False`); `GET /api/config` JSON gains key `adoptionEnabled: bool`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_config_api.py`:

```python
"""GET /api/config exposes adoptionEnabled, off by default."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

H = {"Tailscale-User-Login": "testuser@example.com"}


def _client():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)


def test_config_adoption_disabled_by_default():
    client = _client()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CCSWITCH_ENABLE_ADOPTION", None)
        r = client.get("/api/config", headers=H)
    assert r.status_code == 200
    assert r.json()["adoptionEnabled"] is False


def test_config_adoption_enabled_when_flag_set():
    client = _client()
    with patch.dict(os.environ, {"CCSWITCH_ENABLE_ADOPTION": "1"}):
        r = client.get("/api/config", headers=H)
    assert r.json()["adoptionEnabled"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_config_api.py -v`
Expected: FAIL — `KeyError: 'adoptionEnabled'`.

- [ ] **Step 3: Add the flag to `server/config.py`**

In the Pydantic `Settings` branch, after the `enable_xvfb` line (~39):

```python
        enable_adoption: bool = Field(default=False, alias="CCSWITCH_ENABLE_ADOPTION")
```

In the dataclass fallback branch, after the `enable_xvfb` field (~100):

```python
        enable_adoption: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ENABLE_ADOPTION", "false").lower() in ("1","true","yes","on")
        )
```

- [ ] **Step 4: Expose it in `server/api/config.py`**

Replace the return in `get_config` (line 12):

```python
    return {"repoRoot": settings.repo_root, "adoptionEnabled": settings.enable_adoption}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_config_api.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add server/config.py server/api/config.py server/tests/test_config_api.py
git commit -m "feat: add CCSWITCH_ENABLE_ADOPTION flag, expose via /api/config"
```

---

### Task 2: Guard the adoption routes (404 when off); pin existing adopt tests on

**Files:**
- Modify: `server/api/sessions.py` (add `get_settings` import; add `_require_adoption_enabled()` helper; call it at the top of `list_adoptable` ~289 and `adopt_session_route` ~307)
- Modify: `server/tests/test_adopt_api.py:15-24` (pin flag on in `_client()`)
- Test: `server/tests/test_adopt_disabled.py` (create)

**Interfaces:**
- Consumes: `Settings.enable_adoption` (Task 1).
- Produces: `_require_adoption_enabled()` raising `HTTPException(404)` when the flag is off; both adoption routes call it first.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_adopt_disabled.py`:

```python
"""When CCSWITCH_ENABLE_ADOPTION is off (default), adoption routes 404."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

H = {"Tailscale-User-Login": "testuser@example.com"}


@pytest.fixture(autouse=True)
def _disable_adoption(monkeypatch):
    # Guarantee the flag is off at request time regardless of ambient shell env.
    monkeypatch.delenv("CCSWITCH_ENABLE_ADOPTION", raising=False)


def _client():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    env = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
           "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}
    with patch.dict(os.environ, env):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)


def test_adoptable_404_when_disabled():
    client = _client()
    r = client.get("/api/sessions/adoptable", headers=H)
    assert r.status_code == 404


def test_adopt_404_when_disabled():
    client = _client()
    r = client.post("/api/sessions/adopt", json={"claudeSessionId": "x"}, headers=H)
    assert r.status_code == 404
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_adopt_disabled.py -v`
Expected: FAIL — routes currently return 200/409/422, not 404.

- [ ] **Step 3: Add the guard in `server/api/sessions.py`**

Ensure the module imports `get_settings` (add to the existing `from server.config import …` line, or add `from server.config import get_settings`). Add a helper near the other module-level helpers:

```python
def _require_adoption_enabled() -> None:
    """Sunset gate: adoption is off unless CCSWITCH_ENABLE_ADOPTION is set."""
    if not get_settings().enable_adoption:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
```

As the **first line** of `list_adoptable` (after the docstring):

```python
    _require_adoption_enabled()
```

As the **first line** of `adopt_session_route` (after the docstring, before the `try`):

```python
    _require_adoption_enabled()
```

- [ ] **Step 4: Pin the flag ON in the existing adopt API tests**

The route guard reads `get_settings()` at **request time**, which is *after* `_client()`'s
`patch.dict` block has exited — so pinning inside `_client()` would not survive to the request.
Add an autouse fixture so the flag is set for the whole duration of each test body (including
the HTTP call). In `server/tests/test_adopt_api.py`, add `import pytest` and, just below the
`H = {...}` line:

```python
@pytest.fixture(autouse=True)
def _enable_adoption():
    with patch.dict(os.environ, {"CCSWITCH_ENABLE_ADOPTION": "1"}):
        yield
```

Leave `_client()` unchanged.

- [ ] **Step 5: Run the affected suites to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_adopt_disabled.py server/tests/test_adopt_api.py -v`
Expected: PASS — new 404 tests pass; the four pinned adopt-API tests still pass.

- [ ] **Step 6: Commit**

```bash
git add server/api/sessions.py server/tests/test_adopt_api.py server/tests/test_adopt_disabled.py
git commit -m "feat: 404 adoption routes when CCSWITCH_ENABLE_ADOPTION is off"
```

---

### Task 3: Hide `AdoptPanel` on the dashboard when adoption is off

**Files:**
- Modify: `web/src/api/hooks.ts:55-57` (`ConfigInfo` interface)
- Modify: `web/src/components/Dashboard.tsx` (the `<AdoptPanel />` mount, ~line 1493)

**Interfaces:**
- Consumes: `GET /api/config` → `{ repoRoot, adoptionEnabled }` (Task 1).
- Produces: `AdoptPanel` mounts only when `useConfig().data?.adoptionEnabled` is true.

- [ ] **Step 1: Extend the `ConfigInfo` interface**

In `web/src/api/hooks.ts`, update the interface (lines 55-57):

```typescript
export interface ConfigInfo {
  repoRoot: string
  adoptionEnabled: boolean
}
```

- [ ] **Step 2: Gate the `AdoptPanel` mount in `Dashboard.tsx`**

Locate `<AdoptPanel />` (~line 1493). If `useConfig()` is not already called in this component scope, add near the other hook calls at the top of the component:

```tsx
  const { data: config } = useConfig()
```

(ensure `useConfig` is in the import from `@/api/hooks`). Then replace the bare mount:

```tsx
{config?.adoptionEnabled && <AdoptPanel />}
```

- [ ] **Step 3: Type-check, lint, and run the web tests**

Run in `web/`:
```bash
npx tsc --noEmit && npx eslint src/components/Dashboard.tsx src/api/hooks.ts && npx vitest run src/components/__tests__/AdoptPanel.test.tsx
```
Expected: tsc clean, eslint clean, `AdoptPanel.test.tsx` PASS (panel component unchanged).

- [ ] **Step 4: Commit**

```bash
git add web/src/api/hooks.ts web/src/components/Dashboard.tsx
git commit -m "feat: hide AdoptPanel when adoption is disabled"
```

---

### Task 4: Full verification sweep

**Files:** none (verification only).

- [ ] **Step 1: Run the full server test suite**

Run: `env -u CCSWITCH_ENABLE_ADOPTION -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/`
Expected: all pass (note: `CCSWITCH_ENABLE_XVFB` must be unset to match CI per memory; unset it if present).

- [ ] **Step 2: Run the full web checks**

Run in `web/`: `npx tsc --noEmit && npx eslint . && npx vitest run`
Expected: all clean / pass.

- [ ] **Step 3: Confirm the kept code is untouched**

Run: `git diff --name-only origin/master -- server/sessions/adopt.py server/sessions/orchestrator.py server/db/models.py web/src/components/AdoptPanel.tsx`
Expected: **empty** — none of the kept-code files changed.
