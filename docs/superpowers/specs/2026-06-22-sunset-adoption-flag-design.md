# Sunset the unmanaged-session adoption capability (flag-gated)

**Date:** 2026-06-22
**Status:** Approved
**Supersedes nothing — gates:** `2026-06-21-adopt-untracked-sessions-design.md`

## Problem

The session **adoption** feature (added 2026-06-21) discovers live `claude` processes
running outside ccswitch and force-takes them over. We want to **sunset** it: turn it
off everywhere by default, but **keep all code and tests** so it can be revived without
re-implementation. This is a disable, not a removal.

The adoption path also runs resumed sessions in their original cwd with no worktree
(`adopted_in_place=True`), a documented exception to the strict per-process isolation
norm — another reason to keep it off by default until revisited.

## Decisions (from brainstorming)

| Question | Decision |
|----------|----------|
| Gate mechanism | Single env-var flag, **off by default** |
| API when off | **404 Not Found** — routes behave as if the feature isn't present |
| Frontend when off | **Hide entirely** — `AdoptPanel` never mounts, never polls `/adoptable` |
| Code/tests | **Keep all** — `adopt.py`, `adopt_session()`, `adopted_in_place` column, `AdoptPanel.tsx` untouched |

## The flag

`CCSWITCH_ENABLE_ADOPTION` — bool, default `False`, added to `server/config.py`'s
`Settings` in **both** the Pydantic and the dataclass-fallback branches, mirroring the
existing `enable_xvfb` flag exactly (same alias style, same truthy parsing in the
fallback: `in ("1","true","yes","on")`). Read via `get_settings()` — single source of truth.

## Backend changes

1. **`server/api/sessions.py`** — guard both adoption routes. At the top of
   `list_adoptable` and `adopt_session_route`:
   ```python
   if not get_settings().enable_adoption:
       raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
   ```
   Extract a small module-level helper `_require_adoption_enabled()` so the two routes
   cannot drift. Routes stay registered (no router surgery); they just 404 when off.
   Add the `get_settings` import.

2. **`server/api/config.py`** — extend the returned dict:
   ```python
   return {"repoRoot": settings.repo_root, "adoptionEnabled": settings.enable_adoption}
   ```

3. **`server/sessions/orchestrator.py`** — **unchanged.** `adopt_session()` is reachable
   only through the now-guarded route. Kept code.

## Frontend changes

1. **`web/src/api/hooks.ts`** — add `adoptionEnabled: boolean` to the `ConfigInfo` interface.

2. **`web/src/components/Dashboard.tsx`** — gate the mount at the current `<AdoptPanel />`
   site (line ~1493):
   ```tsx
   {config?.adoptionEnabled && <AdoptPanel />}
   ```
   using the existing `useConfig()` hook. When off, the panel never mounts, so
   `useAdoptable`'s 5s poll never starts. `AdoptPanel.tsx` itself is **unchanged.**

## Testing

- **Pin existing suites on:** the six `test_adopt_*.py` suites and `AdoptPanel.test.tsx`
  assume the feature is on. Pin `CCSWITCH_ENABLE_ADOPTION=1` (env/fixture override, the
  same pattern used for `CCSWITCH_TAILNET_ALLOWLIST`) so kept coverage keeps passing.
- **New off-by-default tests:**
  - `GET /api/sessions/adoptable` → 404 when flag unset.
  - `POST /api/sessions/adopt` → 404 when flag unset.
  - `GET /api/config` → `adoptionEnabled: false` when flag unset.
- **Frontend:** `AdoptPanel.test.tsx` renders the panel directly with mocked hooks, so
  it is unaffected by the Dashboard gate and stays as kept coverage. The Dashboard mount
  gate (`config?.adoptionEnabled && <AdoptPanel/>`) is a one-line short-circuit verified
  by `tsc --noEmit` (the new `ConfigInfo.adoptionEnabled` field) + `eslint`; Dashboard is
  not otherwise unit-tested and a bespoke render harness is out of scope for a sunset.

## Out of scope / explicitly kept

`server/sessions/adopt.py`, `AdoptPanel.tsx`, `orchestrator.adopt_session()`, the
`adopted_in_place` DB column, and the 2026-06-21 design doc all remain. Reviving the
feature is `CCSWITCH_ENABLE_ADOPTION=1` plus a restart.

## Why a route-internal 404 (not conditional router registration)

Guarding inside the handler keeps route registration and the OpenAPI schema static and
flag-independent: flipping the env var needs only a restart, not a different app graph.
The route still appears in the schema — acceptable for a sunset, and far easier to test.
