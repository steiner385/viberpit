# Roadmap Phase 1: shell generalization and dead-end fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the shell's list column and open-item detection generic per nav section, fix every dead end on the existing Products, Initiatives and Triage pages, and put the three pages under the page contract, with no schema change.

**Architecture:** The onboarding engine gets two correctness fixes (a new product can reach "propose map"; a proposed map waits for its PR instead of re-offering itself) and the wizard gains the missing PR link and two advance buttons backed by one new endpoint. The web side gets a single vocabulary module that every label reads from, a `?projectSlug=` scope on the Triage inbox, sync feedback, and page-contract wrappers. The shell stops hardcoding Sessions as the only list section by routing through two small tables keyed by `NavId`.

**Tech Stack:** FastAPI + SQLAlchemy (server), React 19 + react-router + TanStack Query + Tailwind v4 tokens (web), vitest + testing-library, pytest.

**Spec:** `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` (§5 shell generalization, §7 wizard fixes, §13 phase 1). Read it first.

## Global Constraints

- User-facing words are **Product, Capability, Initiative, Epic, Issue**. Code and API identifiers keep `project`/`feature` (spec D11). Never write "project" or "feature" in new UI copy.
- No schema change in this phase. New columns, tables or migrations are Phase 2.
- New web files go under `web/src/features/<feature>/` or `web/src/shell/` or `web/src/ui/`, where `ccswitch/no-raw-palette` lint applies: no raw palette classes, no `dark:` variants, no `z-<n>` literals, no arbitrary px. Existing files under `web/src/projects/`, `web/src/initiatives/`, `web/src/triage/` keep their `.css` for now (Phase 3 deletes them); do not add new `.css` files.
- Every migrated page: exactly one `h1` via `ui/PageHeader`, `data-page-variant="standard"`, content inside `ui/PageContent`, a row in `web/src/features/settings/pageContract.test.tsx`.
- Paths come from `NAV_BY_ID.<id>.path`, never literals.
- Run the **full** `npm run test` in `web/` before every commit that touches `web/src/shell/` or `web/src/api/hooks.ts`; dozens of unrelated tests mock those.
- Run the four-step server gate before pushing: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/ -n auto --dist loadfile --timeout=300`.
- Bash hook: `.claude/settings.json` runs a `PreToolUse` hook resolved relative to the working directory. Never persist a `cd web`; use `(cd web && …)` subshells. Never put the words deploy/release/rollout/promote together with prod/production in a command's text; write commit messages to a file and use `-F`.
- Commit messages end with `Co-Authored-By: Claude <model> <noreply@anthropic.com>` for the running model.
- Sample data in tests and copy uses fictional names (`example-product`, `o/r`), never KinDash.

## File map

| File | Responsibility |
|------|----------------|
| `server/pm/onboarding.py` | Pure engine. Loses the two dead `map_pr_*` flags; `registered` behaves like `syncing`; `map_proposed` always awaits the PR. |
| `server/sessions/orchestrator.py` | `pm_onboarding_counts` stops passing the dead flags; new `pm_advance_live`. |
| `server/sessions/facade.py`, `server/sessions/remote.py`, `server/ipc/orchestrator_rpc.py` | Mirror `pm_advance_live` across the in-process facade, the remote proxy and the RPC registry. |
| `server/api/pm.py` | Onboarding view gains `mapPr`; new `POST /projects/{slug}/advance-live`. |
| `web/src/features/products/vocab.ts` | The one source of every user-facing label for products, capabilities, stages, statuses, onboarding states. |
| `web/src/api/pm.ts`, `web/src/api/hooks.ts` | `mapPr` type, `advanceMap`/`advanceLive` fetchers and hooks, sync toast. |
| `web/src/projects/OnboardingWizard.tsx` | PR link, advance buttons, scoped triage link, honest copy. |
| `web/src/projects/ProjectsTab.tsx` | Product vocabulary, friendly state chip, page contract. |
| `web/src/initiatives/InitiativesTab.tsx`, `InitiativeTrack.tsx` | Stage labels from vocab on the Advance button, page contract. |
| `web/src/triage/TriageInbox.tsx` | `?projectSlug=` scope, page contract. |
| `web/src/shell/listColumns.tsx` | `LIST_SECTIONS` + `listColumnFor(id, sources)`: which nav sections own a list column and how to render each. |
| `web/src/shell/openItems.ts` | `OPEN_ITEM_RESOLVERS`: per-section "what is open" from route params. |
| `web/src/shell/regions/ContextList.tsx`, `web/src/shell/openSessionId.ts`, `web/src/shell/AppShell.tsx` | Consume the two tables instead of hardcoding Sessions. |
| `web/src/shell/navDefs.ts` | Help text says purpose, not keystrokes. |

Deviations from spec §5, with reasons:
- `ui/Breadcrumb` is **not** built here. It would have no consumer until Phase 3 (the current pages are all top-level), and the repo forbids dead code. Phase 3 builds it with its first consumer.
- The wizard's `register` state gets honest copy, not an add-repo form. A product cannot be created without a repo (`POST /pm/projects` returns 422 `no_repos`), so the state is unreachable through the API; product editing, including repos, is Phase 3's product header.

---

### Task 1: Onboarding engine — reachable propose step, PR wait that sticks

**Files:**
- Modify: `server/pm/onboarding.py:28-41` (view), `:96-137` (engine)
- Modify: `server/sessions/orchestrator.py:6761-6773`
- Modify: `server/sessions/remote.py:1104-1116`
- Test: `server/tests/test_pm_onboarding_engine.py`

**Interfaces:**
- Produces: `ProjectOnboardingView(state, repo_count, backfill_done_count, feature_count, open_unclassified_count, pending_proposal_count)` — six fields, the two `map_pr_*` booleans are gone.
- Produces: `next_onboarding_action` returns `ProposeMap()` for `registered` when every repo is backfilled, and `AwaitMapPr()` for every `map_proposed` view.

Background: nothing writes `syncing` (grep `onboarding_state =` under `server/` excluding tests: only `map_proposed`, `map_merged`, `classifying`), so a new product sat in `registered` and the engine only offered `ProposeMap` from `syncing`. Separately `pm_onboarding_counts` hardcodes both PR flags `False`, so `map_proposed` always fell through to `ProposeMap` again and the wizard re-offered the proposal it had just spawned.

- [ ] **Step 1: Rewrite the engine tests for the new rules**

Replace the `_view` helper and the `registered`/`syncing`/`map_proposed` sections of `server/tests/test_pm_onboarding_engine.py` (lines 28-85) with:

```python
def _view(**kw) -> ProjectOnboardingView:
    base = dict(state="syncing", repo_count=1, backfill_done_count=0,
                feature_count=0, open_unclassified_count=0,
                pending_proposal_count=0)
    base.update(kw)
    return ProjectOnboardingView(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# registered / syncing: nothing attached → Register; backfill in flight → wait; #
# all repos done → propose the map. `syncing` is never written by any loop      #
# (only map_proposed/map_merged/classifying are), so `registered` MUST offer    #
# the same ladder or a new product never reaches the propose step.             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("state", ["registered", "syncing"])
def test_no_repos_is_register(state):
    v = _view(state=state, repo_count=0)
    assert next_onboarding_action(v) == Register()


@pytest.mark.parametrize("state", ["registered", "syncing"])
def test_partial_backfill_awaits(state):
    v = _view(state=state, repo_count=2, backfill_done_count=1)
    assert next_onboarding_action(v) == AwaitBackfill()


@pytest.mark.parametrize("state", ["registered", "syncing"])
def test_all_backfilled_proposes_map(state):
    v = _view(state=state, repo_count=2, backfill_done_count=2)
    assert next_onboarding_action(v) == ProposeMap()


# --------------------------------------------------------------------------- #
# map_proposed: the state itself means a synthesis session was spawned, so the  #
# wizard waits for the human to merge and advance. The wizard offers "propose  #
# again" beside the wait for the died-session case; the engine does not guess. #
# --------------------------------------------------------------------------- #

def test_map_proposed_awaits_pr():
    v = _view(state="map_proposed", backfill_done_count=1)
    assert next_onboarding_action(v) == AwaitMapPr()
```

Keep every other test in the file unchanged.

- [ ] **Step 2: Run the engine tests to see them fail**

Run: `.venv/bin/python -m pytest server/tests/test_pm_onboarding_engine.py -q`
Expected: FAIL — `TypeError` from `_view` (the dataclass still requires `map_pr_open`), then assertion failures on the `registered` cases.

- [ ] **Step 3: Change the view and the engine**

In `server/pm/onboarding.py` replace the `ProjectOnboardingView` dataclass with:

```python
@dataclass(frozen=True)
class ProjectOnboardingView:
    """The counts the engine decides over. ``backfill_done_count`` is how many
    of the project's ``repo_count`` repos have finished pm-sync backfill."""
    state: str
    repo_count: int
    backfill_done_count: int
    feature_count: int
    open_unclassified_count: int
    pending_proposal_count: int
```

and replace the `registered`, `syncing` and `map_proposed` branches of `next_onboarding_action` with:

```python
    if state in ("registered", "syncing"):
        # `syncing` is declared but no loop writes it (pm-sync backfills without
        # touching onboarding_state), so `registered` carries the same ladder.
        if view.repo_count <= 0:
            return Register()
        if view.backfill_done_count < view.repo_count:
            return AwaitBackfill()
        return ProposeMap()

    if state == "map_proposed":
        # The state is set only after a synthesis session was spawned. Whether
        # its PR is open, merged or never opened is not correlated here; the
        # wizard shows the PR search link and a "propose again" beside the wait.
        return AwaitMapPr()
```

Also update the module docstring line `The propose-only gate mirrors…` paragraph to drop the phrase "``AwaitMapPr``/``AwaitTriage`` while a gate is open" only if it mentions the PR flags; otherwise leave it.

In `server/sessions/orchestrator.py` change the return at the end of `pm_onboarding_counts` to:

```python
        return ProjectOnboardingView(
            state=proj.onboarding_state or "registered",
            repo_count=len(repos),
            backfill_done_count=sum(1 for r in repos if r.backfill_done),
            feature_count=int(feature_count),
            open_unclassified_count=int(open_unclassified),
            pending_proposal_count=int(pending))
```

and delete the four-line `# NOTE on map_pr_open / map_pr_merged:` comment that follows it.

- [ ] **Step 4: Check the remote proxy and every other reader**

Run: `grep -rn "map_pr" server/ web/src/`
Expected: no output. If `server/sessions/remote.py`'s `pm_onboarding_counts` names the fields explicitly when it rebuilds the dataclass, delete the two `map_pr_*` keyword arguments; if it uses `ProjectOnboardingView(**result)` nothing changes.

- [ ] **Step 5: Run the engine tests and the API tests that build views**

Run: `.venv/bin/python -m pytest server/tests/test_pm_onboarding_engine.py server/tests/test_pm_api.py server/tests/test_fleet_orchestrator.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
printf 'fix(pm): onboarding engine reaches propose-map from registered and holds at map_proposed\n\nNothing writes `syncing`, so a new product never reached ProposeMap; the\nmap-PR flags were hardcoded False, so map_proposed re-offered the proposal.\nDrop the dead flags and give registered the same ladder as syncing.\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t1.txt
git add server/pm/onboarding.py server/sessions/orchestrator.py server/sessions/remote.py server/tests/test_pm_onboarding_engine.py
git commit -F /tmp/p1-t1.txt
```

---

### Task 2: Onboarding API — `mapPr` in the view, `advance-live` endpoint

**Files:**
- Modify: `server/api/pm.py:554-566` (get_onboarding), append after `advance_map` (`:611-630`)
- Modify: `server/sessions/orchestrator.py` (after `pm_advance_map_merged`, `:6775-6787`)
- Modify: `server/sessions/facade.py:238`, `server/sessions/remote.py:1139-1141`, `server/ipc/orchestrator_rpc.py:345-347`
- Modify: `server/tests/test_fleet_orchestrator.py:500` and `:646`
- Test: `server/tests/test_pm_api.py`

**Interfaces:**
- Consumes: `map_branch_name(slug)` from `server/pm/classification.py` (`pm/feature-map-<slug>`).
- Produces: `GET /api/pm/projects/{slug}/onboarding` → `{state, action, counts, mapPr: {branch, url} | null}`.
- Produces: `POST /api/pm/projects/{slug}/advance-live` → `200 {onboardingState: "live"}`; `409 {error: "not_classifying"}` unless state is `map_merged` or `classifying`.
- Produces: orchestrator method `pm_advance_live(slug: str) -> None`, mirrored on facade, remote and RPC.

- [ ] **Step 1: Write the failing API tests**

Append to `server/tests/test_pm_api.py`:

```python
def test_onboarding_view_carries_the_map_pr_search_link():
    """The wizard's await-map-PR step needs somewhere to send the user. The PR
    number is not stored, but the branch is deterministic, so the view carries
    a GitHub PR search on that branch for the primary repo."""
    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")
        r = c.get("/api/pm/projects/ccswitch/onboarding", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        map_pr = r.json()["mapPr"]
        assert map_pr["branch"] == "pm/feature-map-ccswitch"
        assert map_pr["url"] == (
            "https://github.com/o/r/pulls?q=is%3Apr+head%3Apm%2Ffeature-map-ccswitch")


def test_advance_live_transitions_state():
    """advance-live: 200 from map_merged or classifying → live; 409 otherwise."""
    import asyncio
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import Project

    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")

        r = c.post("/api/pm/projects/ccswitch/advance-live", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "not_classifying"

        async def _set(state):
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
                p.onboarding_state = state
        asyncio.run(_set("classifying"))

        r = c.post("/api/pm/projects/ccswitch/advance-live", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["onboardingState"] == "live"

        async def _check():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
                assert p.onboarding_state == "live"
        asyncio.run(_check())

        # live is absorbing: a second tap is a 409, not a silent no-op
        r = c.post("/api/pm/projects/ccswitch/advance-live", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
```

`_client_with_stubbed_seams` and `_register` already exist in that file (used by `test_advance_map_transitions_state`).

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest server/tests/test_pm_api.py -k "map_pr_search or advance_live" -q`
Expected: FAIL — `KeyError: 'mapPr'` and `404` on the unknown route.

- [ ] **Step 3: Add the orchestrator method and its three mirrors**

In `server/sessions/orchestrator.py`, directly after `pm_advance_map_merged`:

```python
    async def pm_advance_live(self, slug: str) -> None:
        """Advance a project from ``map_merged``/``classifying`` to ``live`` once
        the human has drained the triage queue. The wizard's tap calls this; the
        pm-triage loop keeps classifying new items in the live loop regardless."""
        from sqlalchemy import select
        from server.db.models import Project
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.slug == slug))).scalar_one_or_none()
            if proj is not None and proj.onboarding_state in ("map_merged", "classifying"):
                proj.onboarding_state = "live"
                await db.commit()
```

In `server/sessions/facade.py`, after the `pm_advance_map_merged` line:

```python
    async def pm_advance_live(self, *a, **k): return await _maybe_await(self._orch.pm_advance_live(*a, **k))
```

In `server/sessions/remote.py`, after `pm_advance_map_merged`:

```python
    async def pm_advance_live(self, slug: str) -> None:
        """Advance map_merged/classifying → live, via the daemon."""
        await self._client.call("pm_advance_live", slug=slug)
```

In `server/ipc/orchestrator_rpc.py`, in the `_SIMPLE` list after `"pm_advance_map_merged",`:

```python
        # advance map_merged/classifying → live (returns None, JSON-safe)
        "pm_advance_live",
```

In `server/tests/test_fleet_orchestrator.py`, add `"pm_advance_live",` after each of the two `"pm_advance_map_merged",` entries (lines 500 and 646).

- [ ] **Step 4: Add the view field and the endpoint**

In `server/api/pm.py`, add near the other imports at the top of the phase-2 block (after `next_onboarding_action,` import group):

```python
from urllib.parse import quote_plus  # noqa: E402

from server.pm.classification import map_branch_name  # noqa: E402
```

Replace the body of `get_onboarding` with:

```python
    p = await _load_project(db, slug)
    orch = request.app.state.sessions
    view = await orch.pm_onboarding_counts(p.id)
    primary = (await db.execute(select(ProjectRepo).where(
        ProjectRepo.project_id == p.id,
        ProjectRepo.is_primary.is_(True)))).scalars().first()
    map_pr = None
    if primary is not None and primary.repo:
        branch = map_branch_name(slug)
        # The PR number is not stored (Feature.pr_number is never written); the
        # branch is deterministic, so a GitHub PR search on it is the link.
        map_pr = {
            "branch": branch,
            "url": f"https://github.com/{primary.repo}/pulls?q="
                   + quote_plus(f"is:pr head:{branch}"),
        }
    return {
        "state": view.state,
        "action": _onboarding_action_to_dict(next_onboarding_action(view)),
        "counts": _counts_to_dict(view),
        "mapPr": map_pr,
    }
```

(`select` and `ProjectRepo` are already imported at the top of `pm.py`; verify with `grep -n "^from\|^import" server/api/pm.py | head -30`.)

Append after `advance_map`:

```python
@router.post("/projects/{slug}/advance-live", status_code=status.HTTP_200_OK)
async def advance_live(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Advance the project to ``live`` once the human has drained the triage
    queue. Allowed from ``map_merged`` (nothing needed classifying) and
    ``classifying``; 409 otherwise, including from ``live`` itself."""
    p = await _load_project(db, slug)
    if p.onboarding_state not in ("map_merged", "classifying"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_classifying",
            "message": f"project is in state '{p.onboarding_state}', "
                       "not 'map_merged' or 'classifying'"})
    orch = request.app.state.sessions
    await orch.pm_advance_live(slug)
    await record_audit(db=db, type="pm.onboard.advance_live", identity=identity,
                       detail={"slug": slug})
    return {"onboardingState": "live"}
```

- [ ] **Step 5: Run the PM API, fleet and assistant tests**

Run: `.venv/bin/python -m pytest server/tests/test_pm_api.py server/tests/test_fleet_orchestrator.py server/tests/test_assistant_policy.py -q`
Expected: PASS. (`test_assistant_policy.py` enumerates PM routes; if it lists `advance-map` as a mutating route and asserts the set, add `advance-live` beside it.)

- [ ] **Step 6: Lint and type-check the server**

Run: `.venv/bin/ruff check src/ server/ && .venv/bin/mypy server/ --ignore-missing-imports`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
printf 'feat(pm): onboarding view carries the map-PR link; POST advance-live\n\nThe wizard told the user to "advance" with no control and no PR link.\nThe branch is deterministic, so the view carries a PR search URL; a new\nadvance-live endpoint (map_merged|classifying -> live) mirrors advance-map\nacross orchestrator, facade, remote proxy and RPC.\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t2.txt
git add server/api/pm.py server/sessions/orchestrator.py server/sessions/facade.py server/sessions/remote.py server/ipc/orchestrator_rpc.py server/tests/test_pm_api.py server/tests/test_fleet_orchestrator.py
git commit -F /tmp/p1-t2.txt
```

---

### Task 3: Web vocabulary module

**Files:**
- Create: `web/src/features/products/vocab.ts`
- Test: `web/src/features/products/vocab.test.ts`

**Interfaces:**
- Produces:
  - `PRODUCT = { singular: 'Product', plural: 'Products', new: 'New product', empty: 'No products yet. Add one to start mirroring its backlog.' }`
  - `CAPABILITY = { singular: 'Capability', plural: 'Capabilities' }`
  - `STAGE_LABEL: Record<InitiativeStage, string>` and `stageLabel(stage: string | null | undefined): string`
  - `INITIATIVE_STATUS_LABEL: Record<InitiativeStatus, string>` and `initiativeStatusLabel(status: string | null | undefined): string`
  - `ONBOARDING_STATE_LABEL: Record<OnboardingState, string>` and `onboardingStateLabel(state: string | null | undefined): string`
  - Each `*Label` function returns the friendly label, or the raw input, or `'—'` for null/undefined.

- [ ] **Step 1: Write the failing test**

`web/src/features/products/vocab.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { INITIATIVE_STAGES } from '@/api/sdlc'
import { PROJECT_ONBOARDING_STATES } from '@/api/pm'
import {
  PRODUCT, CAPABILITY, STAGE_LABEL, stageLabel,
  INITIATIVE_STATUS_LABEL, initiativeStatusLabel,
  ONBOARDING_STATE_LABEL, onboardingStateLabel,
} from './vocab'

describe('vocab', () => {
  it('names every initiative stage', () => {
    for (const s of INITIATIVE_STAGES) expect(STAGE_LABEL[s].length).toBeGreaterThan(0)
    expect(stageLabel('intake')).toBe('Intent')
    expect(stageLabel('design')).toBe('Spec')
  })

  it('names every onboarding state', () => {
    for (const s of PROJECT_ONBOARDING_STATES) expect(ONBOARDING_STATE_LABEL[s].length).toBeGreaterThan(0)
    expect(onboardingStateLabel('map_proposed')).toBe('Capability map proposed')
  })

  it('names every initiative status', () => {
    for (const s of ['proposed', 'active', 'awaiting_human', 'blocked', 'done'] as const) {
      expect(INITIATIVE_STATUS_LABEL[s].length).toBeGreaterThan(0)
    }
    expect(initiativeStatusLabel('awaiting_human')).toBe('Awaiting human')
  })

  it('falls back to the raw value, and to a dash for nothing', () => {
    expect(stageLabel('bogus')).toBe('bogus')
    expect(stageLabel(null)).toBe('—')
    expect(onboardingStateLabel(undefined)).toBe('—')
  })

  it('never says project or feature to the user', () => {
    const all = [
      ...Object.values(PRODUCT), ...Object.values(CAPABILITY),
      ...Object.values(STAGE_LABEL), ...Object.values(INITIATIVE_STATUS_LABEL),
      ...Object.values(ONBOARDING_STATE_LABEL),
    ].join(' ').toLowerCase()
    expect(all).not.toMatch(/\bproject\b|\bfeature\b/)
  })
})
```

- [ ] **Step 2: Run it to see it fail**

Run: `(cd web && npx vitest run src/features/products/vocab.test.ts)`
Expected: FAIL — cannot resolve `./vocab`.

- [ ] **Step 3: Write the module**

`web/src/features/products/vocab.ts`:

```ts
/**
 * The one source of every user-facing word in the roadmap area (spec D11).
 *
 * Users see Product / Capability / Initiative / Epic / Issue. Code and the API
 * keep `project` and `feature`; this module is where the renaming happens, so
 * copy cannot drift between the nav, a chip, a button and an empty state.
 */
import type { InitiativeStage, InitiativeStatus } from '@/api/sdlc'
import type { OnboardingState } from '@/api/pm'

export const PRODUCT = {
  singular: 'Product',
  plural: 'Products',
  new: 'New product',
  empty: 'No products yet. Add one to start mirroring its backlog.',
} as const

export const CAPABILITY = {
  singular: 'Capability',
  plural: 'Capabilities',
} as const

/** The first three stages surface the artifact they commit, so the track reads
 *  as the playbook's intent → spec → plan → build progression. */
export const STAGE_LABEL: Record<InitiativeStage, string> = {
  intake: 'Intent',
  design: 'Spec',
  plan: 'Plan',
  build: 'Build',
  deploy: 'Deploy',
  maintain: 'Maintain',
  done: 'Done',
}

export const INITIATIVE_STATUS_LABEL: Record<InitiativeStatus, string> = {
  proposed: 'Proposed',
  active: 'Active',
  awaiting_human: 'Awaiting human',
  blocked: 'Blocked',
  done: 'Done',
}

export const ONBOARDING_STATE_LABEL: Record<OnboardingState, string> = {
  registered: 'Registered',
  syncing: 'Syncing backlog',
  map_proposed: 'Capability map proposed',
  map_merged: 'Capability map merged',
  classifying: 'Classifying backlog',
  live: 'Live',
}

function lookup(table: Record<string, string>, key: string | null | undefined): string {
  if (key == null || key === '') return '—'
  return table[key] ?? key
}

export const stageLabel = (stage: string | null | undefined) => lookup(STAGE_LABEL, stage)
export const initiativeStatusLabel = (status: string | null | undefined) =>
  lookup(INITIATIVE_STATUS_LABEL, status)
export const onboardingStateLabel = (state: string | null | undefined) =>
  lookup(ONBOARDING_STATE_LABEL, state)
```

- [ ] **Step 4: Run the test and the type check**

Run: `(cd web && npx vitest run src/features/products/vocab.test.ts && npm run typecheck)`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
printf 'feat(web): one vocabulary module for product, capability, stage and state labels\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t3.txt
git add web/src/features/products/vocab.ts web/src/features/products/vocab.test.ts
git commit -F /tmp/p1-t3.txt
```

---

### Task 4: Route every existing label through the vocabulary

**Files:**
- Modify: `web/src/initiatives/InitiativeTrack.tsx:15-26,39`
- Modify: `web/src/initiatives/InitiativesTab.tsx:38-41,83-96`
- Modify: `web/src/projects/OnboardingWizard.tsx:11-15,73`
- Modify: `web/src/projects/ProjectsTab.tsx:38,53,66-68,129-137`
- Test: `web/src/initiatives/__tests__/InitiativesTab.test.tsx:108-110`, `web/src/projects/__tests__/ProjectsTab.test.tsx`

**Interfaces:**
- Consumes: everything from Task 3.

- [ ] **Step 1: Update the tests to expect the friendly labels**

In `web/src/initiatives/__tests__/InitiativesTab.test.tsx`, the `advances a gate via POST /advance` test: change

```ts
    // design → plan is the next gate.
    expect(btn).toHaveTextContent('plan')
```

to

```ts
    // design → plan is the next gate, and the button says what the track node
    // says ("Plan"), not the raw stage id.
    expect(btn).toHaveTextContent('Advance → Plan')
```

In `web/src/projects/__tests__/ProjectsTab.test.tsx`:
- In the fixture, rename `slug: 'kindash', name: 'KinDash'` to `slug: 'example-product', name: 'Example product'` and `repo: 'steiner385/KinDash'` to `repo: 'o/example-product'`; update the two assertions that look for `'KinDash'` / `'steiner385/KinDash'` accordingly.
- `getByText(/registered/i)` stays (label is "Registered").
- Change every `/new project/i` to `/new product/i`, `/no projects yet/i` to `/no products yet/i`, and every `getByPlaceholderText('KinDash')` to `getByPlaceholderText('My product')`.

- [ ] **Step 2: Run both test files to see them fail**

Run: `(cd web && npx vitest run src/initiatives/__tests__/InitiativesTab.test.tsx src/projects/__tests__/ProjectsTab.test.tsx)`
Expected: FAIL on the label assertions.

- [ ] **Step 3: Point the components at the vocabulary**

`InitiativeTrack.tsx`: delete the local `STAGE_LABEL` constant (lines 15-26) and add `import { STAGE_LABEL } from '@/features/products/vocab'`. Everything else unchanged.

`InitiativesTab.tsx`:
- Delete the local `STATUS_LABEL` (lines 38-41); add `import { initiativeStatusLabel, stageLabel } from '@/features/products/vocab'`.
- Status chip: `{initiativeStatusLabel(init.status)}`.
- Advance button:

```tsx
        <button
          type="button"
          className="btn-ghost init-advance"
          disabled={!nxt || advance.isPending}
          onClick={() => advance.mutate(init.slug)}
          title={nxt ? `Advance the gate to ${stageLabel(nxt)}` : 'Already at the final stage'}
        >
          {nxt ? `Advance → ${stageLabel(nxt)}` : 'Complete'}
        </button>
```

`OnboardingWizard.tsx`: delete the local `STATE_LABEL` (lines 11-15); add `import { onboardingStateLabel } from '@/features/products/vocab'`; render `{onboardingStateLabel(data.state)}`.

`ProjectsTab.tsx`:
- `import { PRODUCT, onboardingStateLabel } from '@/features/products/vocab'`.
- Form placeholder `"KinDash"` → `"My product"`; submit button text `'Add project'` → `'Add product'`; error fallback `'Could not create project'` → `'Could not create product'`.
- State chip content: `{onboardingStateLabel(project.onboardingState)}` (keep the `className` with the raw state for the CSS hook).
- `<h2>Products</h2>` → `<h2>{PRODUCT.plural}</h2>`; `New project` button → `{PRODUCT.new}`; empty paragraph → `{PRODUCT.empty}`.

- [ ] **Step 4: Run the two test files, then the full web suite**

Run: `(cd web && npx vitest run src/initiatives src/projects && npm run test && npm run typecheck && npm run lint)`
Expected: all PASS, lint clean with zero warnings.

- [ ] **Step 5: Commit**

```bash
printf 'fix(web): stage, status and onboarding labels read from the vocabulary module\n\nThe Advance button said "design" while the track node said "Spec"; the\nproduct card showed raw snake_case beside the wizard'"'"'s friendly label.\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t4.txt
git add web/src/initiatives web/src/projects
git commit -F /tmp/p1-t4.txt
```

---

### Task 5: Web API — `mapPr`, advance fetchers and hooks, sync toast

**Files:**
- Modify: `web/src/api/pm.ts:86-90` (OnboardingView), append fetchers
- Modify: `web/src/api/hooks.ts:1542-1549` (useSyncProject), append hooks after `useClassify`
- Test: `web/src/projects/__tests__/pmHooks.test.tsx`

**Interfaces:**
- Produces (`api/pm.ts`): `interface OnboardingMapPr { branch: string; url: string }`; `OnboardingView.mapPr: OnboardingMapPr | null`; `advanceMap(slug): Promise<{ onboardingState: string }>`; `advanceLive(slug): Promise<{ onboardingState: string }>`.
- Produces (`api/hooks.ts`): `useAdvanceMap()`, `useAdvanceLive()` — `useMutation<{ onboardingState: string }, ApiRequestError, string>` invalidating `QK.onboarding(slug)` and `QK.projects`; `useSyncProject` toasts `Sync requested for <slug>` on success.

- [ ] **Step 1: Write the failing hook tests**

Append to `web/src/projects/__tests__/pmHooks.test.tsx` inside the `describe('pm hooks')` block:

```ts
  it('useAdvanceMap posts advance-map and refreshes the wizard', async () => {
    const pm = await import('@/api/pm')
    vi.mocked(pm.advanceMap).mockResolvedValueOnce({ onboardingState: 'map_merged' })
    const { useAdvanceMap } = await import('@/api/hooks')
    const { result } = renderHook(() => useAdvanceMap(), { wrapper: wrap() })
    result.current.mutate('ccswitch')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(pm.advanceMap).toHaveBeenCalledWith('ccswitch')
  })

  it('useAdvanceLive posts advance-live', async () => {
    const pm = await import('@/api/pm')
    vi.mocked(pm.advanceLive).mockResolvedValueOnce({ onboardingState: 'live' })
    const { useAdvanceLive } = await import('@/api/hooks')
    const { result } = renderHook(() => useAdvanceLive(), { wrapper: wrap() })
    result.current.mutate('ccswitch')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(pm.advanceLive).toHaveBeenCalledWith('ccswitch')
  })

  it('useSyncProject toasts on success — a 202 with no feedback read as "nothing happened"', async () => {
    const pm = await import('@/api/pm')
    vi.mocked(pm.requestProjectSync).mockResolvedValueOnce({ requested: true })
    const { toast } = await import('sonner')
    const { useSyncProject } = await import('@/api/hooks')
    const { result } = renderHook(() => useSyncProject(), { wrapper: wrap() })
    result.current.mutate('ccswitch')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(toast.success).toHaveBeenCalledWith('Sync requested for ccswitch')
  })
```

Extend the file's existing `vi.mock('@/api/pm', …)` factory with three more entries beside `fetchOnboarding`:

```ts
  advanceMap: vi.fn(),
  advanceLive: vi.fn(),
  requestProjectSync: vi.fn(),
```

and add at the top, after the other mocks:

```ts
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
```

- [ ] **Step 2: Run to see them fail**

Run: `(cd web && npx vitest run src/projects/__tests__/pmHooks.test.tsx)`
Expected: FAIL — `advanceMap` is not exported / `useAdvanceMap` is not a function.

- [ ] **Step 3: Add the types and fetchers**

In `web/src/api/pm.ts` replace the `OnboardingView` interface with:

```ts
export interface OnboardingMapPr {
  /** The deterministic branch the map-synthesis session pushes to. */
  branch: string
  /** A GitHub PR search for that branch on the primary repo. */
  url: string
}

export interface OnboardingView {
  state: OnboardingState | string
  action: OnboardingAction
  counts: OnboardingCounts
  mapPr: OnboardingMapPr | null
}
```

and after `runClassify` add:

```ts
/** Human gate: the capability-map PR is merged → map_merged. */
export function advanceMap(slug: string): Promise<{ onboardingState: string }> {
  return api.post<{ onboardingState: string }>(
    `/pm/projects/${encodeURIComponent(slug)}/advance-map`)
}

/** Human gate: the triage queue is drained → live. */
export function advanceLive(slug: string): Promise<{ onboardingState: string }> {
  return api.post<{ onboardingState: string }>(
    `/pm/projects/${encodeURIComponent(slug)}/advance-live`)
}
```

- [ ] **Step 4: Add the hooks and the toast**

In `web/src/api/hooks.ts`, extend the `@/api/pm` import to include `advanceMap, advanceLive`. Replace `useSyncProject` with:

```ts
export function useSyncProject() {
  const qc = useQueryClient()
  return useMutation<{ requested: boolean }, ApiRequestError, string>({
    mutationFn: (slug) => requestProjectSync(slug),
    onSuccess: (_d, slug) => {
      // The endpoint is a 202 — the loop picks the request up on its next
      // tick — so without this the button read as "nothing happened".
      toast.success(`Sync requested for ${slug}`)
      void qc.invalidateQueries({ queryKey: QK.projects })
    },
    meta: PM_MUTATION_META,
  })
}
```

After `useClassify` add:

```ts
/** The wizard's "map merged" tap (map_proposed → map_merged). */
export function useAdvanceMap() {
  const qc = useQueryClient()
  return useMutation<{ onboardingState: string }, ApiRequestError, string>({
    mutationFn: (slug) => advanceMap(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
      void qc.invalidateQueries({ queryKey: QK.projects })
    },
    meta: PM_MUTATION_META,
  })
}

/** The wizard's "go live" tap (map_merged|classifying → live). */
export function useAdvanceLive() {
  const qc = useQueryClient()
  return useMutation<{ onboardingState: string }, ApiRequestError, string>({
    mutationFn: (slug) => advanceLive(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
      void qc.invalidateQueries({ queryKey: QK.projects })
    },
    meta: PM_MUTATION_META,
  })
}
```

- [ ] **Step 5: Fix every fixture that builds an `OnboardingView`**

Run: `(cd web && npm run typecheck)`
Expected: errors in `OnboardingWizard.test.tsx` and `pmHooks.test.tsx` fixtures missing `mapPr`. Add `mapPr: null` to each fixture object (and, in `OnboardingWizard.test.tsx`, define `const MAP_PR = { branch: 'pm/feature-map-ccswitch', url: 'https://github.com/o/r/pulls?q=is%3Apr+head%3Apm%2Ffeature-map-ccswitch' }` for Task 6 to use). Re-run until clean.

- [ ] **Step 6: Run the hook tests and the full web suite**

Run: `(cd web && npx vitest run src/projects && npm run test && npm run lint)`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
printf 'feat(web): advance-map/advance-live hooks, mapPr on the onboarding view, sync toast\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t5.txt
git add web/src/api/pm.ts web/src/api/hooks.ts web/src/projects/__tests__
git commit -F /tmp/p1-t5.txt
```

---

### Task 6: Onboarding wizard — PR link, advance buttons, scoped triage link, honest copy

**Files:**
- Modify: `web/src/projects/OnboardingWizard.tsx`
- Test: `web/src/projects/__tests__/OnboardingWizard.test.tsx`

**Interfaces:**
- Consumes: `useAdvanceMap`, `useAdvanceLive`, `useProposeMap` from `@/api/hooks`; `OnboardingView.mapPr`; `NAV_BY_ID.triage.path`.
- Produces: the wizard renders, per action:
  - `await_map_pr`: link "Open the capability-map PR ↗" (to `mapPr.url`, absent when `mapPr` is null), button "Map merged → continue" (advance-map), button "Propose again" (propose-map).
  - `await_triage`: link "Review N proposals in Triage →" to `${NAV_BY_ID.triage.path}?projectSlug=<slug>`, button "Go live" (advance-live).
  - `register`: "No repository is attached to this product."

- [ ] **Step 1: Rewrite the wizard tests for the new behaviour**

In `web/src/projects/__tests__/OnboardingWizard.test.tsx`:

Extend the `@/api/pm` mock factory with `advanceMap` and `advanceLive` spies:

```ts
const advanceMap = vi.fn().mockResolvedValue({ onboardingState: 'map_merged' })
const advanceLive = vi.fn().mockResolvedValue({ onboardingState: 'live' })
```
and in the factory: `advanceMap: (...a: unknown[]) => advanceMap(...a), advanceLive: (...a: unknown[]) => advanceLive(...a),`.

Wrap the render in a router (the triage link needs one):

```ts
import { MemoryRouter } from 'react-router-dom'

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><OnboardingWizard slug="ccswitch" /></MemoryRouter>
    </QueryClientProvider>,
  )
}
```

Replace `renders wait text and no action button for await_map_pr state` with:

```ts
  it('await_map_pr: links to the PR search, offers Map merged and Propose again', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'map_proposed', action: { type: 'await_map_pr' },
      counts: BASE_COUNTS, mapPr: MAP_PR,
    })
    renderWizard()
    const link = await screen.findByRole('link', { name: /open the capability-map pr/i })
    expect(link).toHaveAttribute('href', MAP_PR.url)
    fireEvent.click(screen.getByRole('button', { name: /map merged/i }))
    await waitFor(() => expect(advanceMap).toHaveBeenCalledWith('ccswitch'))
    fireEvent.click(screen.getByRole('button', { name: /propose again/i }))
    await waitFor(() => expect(proposeMap).toHaveBeenCalledWith('ccswitch'))
  })

  it('await_map_pr without a primary repo still offers the advance, without a link', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'map_proposed', action: { type: 'await_map_pr' },
      counts: BASE_COUNTS, mapPr: null,
    })
    renderWizard()
    await screen.findByRole('button', { name: /map merged/i })
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('await_triage: links to the inbox scoped to this product and offers Go live', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'classifying', action: { type: 'await_triage' },
      counts: { ...BASE_COUNTS, pendingProposalCount: 4 }, mapPr: MAP_PR,
    })
    renderWizard()
    const link = await screen.findByRole('link', { name: /review 4 proposals in triage/i })
    expect(link).toHaveAttribute('href', '/triage?projectSlug=ccswitch')
    fireEvent.click(screen.getByRole('button', { name: /go live/i }))
    await waitFor(() => expect(advanceLive).toHaveBeenCalledWith('ccswitch'))
  })

  it('register: says plainly that no repository is attached', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'registered', action: { type: 'register' },
      counts: { ...BASE_COUNTS, repoCount: 0 }, mapPr: null,
    })
    renderWizard()
    await screen.findByText(/no repository is attached to this product/i)
    expect(screen.queryByRole('button')).toBeNull()
  })
```

Add `mapPr: null` to every other fixture in the file (Task 5 step 5 may already have done so).

- [ ] **Step 2: Run to see them fail**

Run: `(cd web && npx vitest run src/projects/__tests__/OnboardingWizard.test.tsx)`
Expected: FAIL — no link, no such buttons.

- [ ] **Step 3: Implement the wizard changes**

Replace `web/src/projects/OnboardingWizard.tsx` from the imports through `ActionButton` with:

```tsx
/**
 * Onboarding wizard (PM product layer, phase 2): a VIEW over the onboarding
 * engine (design §5). It renders the next action for the product's current
 * onboarding_state as a small set of controls + a one-line status. It is not
 * authoritative — the backend engine decides; this only presents.
 */
import { Link } from 'react-router-dom'

import {
  useAdvanceLive, useAdvanceMap, useClassify, useOnboarding, useProposeMap,
} from '@/api/hooks'
import type { OnboardingAction, OnboardingCounts, OnboardingMapPr } from '@/api/pm'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { onboardingStateLabel } from '@/features/products/vocab'
import { NAV_BY_ID } from '@/shell/navDefs'

function InlineError({ error, fallback }: { error: unknown; fallback: string }) {
  return (
    <p role="alert" className="pm-error">
      {extractMutationErrorMessage(error, fallback)}
    </p>
  )
}

function AwaitMapPr({ slug, mapPr }: { slug: string; mapPr: OnboardingMapPr | null }) {
  const advance = useAdvanceMap()
  const proposeMap = useProposeMap()
  return (
    <>
      <span className="pm-muted">Review and merge the capability-map PR, then continue.</span>
      {mapPr && (
        <a className="pm-wizard-link" href={mapPr.url} target="_blank" rel="noreferrer">
          Open the capability-map PR ↗
        </a>
      )}
      <button className="pm-wizard-action" disabled={advance.isPending}
              onClick={() => advance.mutate(slug)}>
        {advance.isPending ? 'Continuing…' : 'Map merged → continue'}
      </button>
      <button className="pm-wizard-action" disabled={proposeMap.isPending}
              onClick={() => proposeMap.mutate(slug)}
              title="The synthesis session may have died without opening a PR">
        {proposeMap.isPending ? 'Proposing…' : 'Propose again'}
      </button>
      {advance.isError && <InlineError error={advance.error} fallback="Could not continue" />}
      {proposeMap.isError && (
        <InlineError error={proposeMap.error} fallback="Could not propose the map" />
      )}
    </>
  )
}

function AwaitTriage({ slug, counts }: { slug: string; counts: OnboardingCounts }) {
  const advance = useAdvanceLive()
  const n = counts.pendingProposalCount
  return (
    <>
      <span className="pm-muted">Drain the triage queue, then go live.</span>
      <Link className="pm-wizard-link"
            to={`${NAV_BY_ID.triage.path}?projectSlug=${encodeURIComponent(slug)}`}>
        {n === 0 ? 'Open the Triage inbox →' : `Review ${n} proposal${n === 1 ? '' : 's'} in Triage →`}
      </Link>
      <button className="pm-wizard-action" disabled={advance.isPending}
              onClick={() => advance.mutate(slug)}>
        {advance.isPending ? 'Going live…' : 'Go live'}
      </button>
      {advance.isError && <InlineError error={advance.error} fallback="Could not go live" />}
    </>
  )
}

function ActionButton(
  { action, slug, counts, mapPr }:
  { action: OnboardingAction; slug: string; counts: OnboardingCounts; mapPr: OnboardingMapPr | null },
) {
  const proposeMap = useProposeMap()
  const classify = useClassify()
  switch (action.type) {
    case 'propose_map':
      return (
        <>
          <button className="pm-wizard-action" disabled={proposeMap.isPending}
                  onClick={() => proposeMap.mutate(slug)}>
            {proposeMap.isPending ? 'Proposing…' : 'Propose capability map'}
          </button>
          {proposeMap.isError && (
            <InlineError error={proposeMap.error} fallback="Could not propose the map" />
          )}
        </>
      )
    case 'classify':
      return (
        <>
          <button className="pm-wizard-action" disabled={classify.isPending}
                  onClick={() => classify.mutate(slug)}>
            {classify.isPending ? 'Classifying…' : 'Classify backlog'}
          </button>
          {classify.isError && (
            <InlineError error={classify.error} fallback="Could not start classification" />
          )}
        </>
      )
    case 'await_backfill':
      return <span className="pm-muted">Waiting for backlog sync to finish…</span>
    case 'await_map_pr':
      return <AwaitMapPr slug={slug} mapPr={mapPr} />
    case 'await_triage':
      return <AwaitTriage slug={slug} counts={counts} />
    case 'register':
      return <span className="pm-muted">No repository is attached to this product.</span>
    case 'needs_human':
      return <span className="pm-warn">Needs attention: {action.reason}</span>
    case 'live':
      return null
    default:
      return null
  }
}
```

and update `OnboardingWizard`'s render to:

```tsx
export function OnboardingWizard({ slug }: { slug: string }) {
  const { data, isLoading } = useOnboarding(slug)
  if (isLoading || !data) return null
  if (data.state === 'live') return null
  return (
    <div className="pm-wizard">
      <div className="pm-wizard-head">
        <span className="pm-wizard-state">{onboardingStateLabel(data.state)}</span>
        <span className="pm-muted">
          {data.counts.featureCount} capabilities · {data.counts.openUnclassifiedCount} open items ·{' '}
          {data.counts.pendingProposalCount} awaiting triage
        </span>
      </div>
      <ActionButton action={data.action} slug={slug} counts={data.counts} mapPr={data.mapPr} />
    </div>
  )
}
```

The existing test `offers the Propose feature map button` looks for `/propose feature map/i`; change that regex (three occurrences) to `/propose capability map/i`.

Add one rule to `web/src/projects/projects.css` beside `.pm-wizard-action` (this is the last edit that file gets before Phase 3 deletes it):

```css
.pm-wizard-link { color: var(--accent); text-decoration: underline; margin-right: 0.75rem; }
```

- [ ] **Step 4: Run the wizard tests, then the projects folder**

Run: `(cd web && npx vitest run src/projects && npm run typecheck && npm run lint)`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
printf 'fix(web): onboarding wizard links the map PR, offers Map merged / Go live, scopes the triage link\n\nThe wizard said "then advance" with no control, and "the queue below" for a\nqueue on another page.\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t6.txt
git add web/src/projects
git commit -F /tmp/p1-t6.txt
```

---

### Task 7: Triage inbox — `?projectSlug=` scope

**Files:**
- Modify: `web/src/triage/TriageInbox.tsx:52-58,209-219`
- Test: `web/src/triage/__tests__/TriageInbox.test.tsx`

**Interfaces:**
- Consumes: `useTriage(projectSlug?)`, `useAcceptProposal(projectSlug?)`, `useRejectProposal(projectSlug?)`, `useAcceptBulk(projectSlug?)` — all already accept the slug.
- Produces: `/triage?projectSlug=<slug>` scopes the list and the mutations' cache key; a header chip "Scope: <slug>" with a "Clear" link to `NAV_BY_ID.triage.path`; the empty state names the scope.

- [ ] **Step 1: Write the failing tests**

`web/src/triage/__tests__/TriageInbox.test.tsx` has `const mocked = …` (the mocked `@/api/triage` module), a `wrapper` component around line 225, and `async function renderInbox(rows = ROWS)` around line 230 that does `render(<TriageInbox />, { wrapper })`. Change `renderInbox` to take a path and mount a router:

```tsx
import { MemoryRouter } from 'react-router-dom'

async function renderInbox(rows: triage.TriageProposal[] = ROWS, path = '/triage') {
  mocked.fetchTriage.mockResolvedValue(rows)
  const view = render(
    <MemoryRouter initialEntries={[path]}><TriageInbox /></MemoryRouter>,
    { wrapper },
  )
  // …the rest of the function is unchanged
```

Then add these two tests to the file's main `describe`:

```ts
  it('scopes the queue to ?projectSlug= and shows a clearable scope chip', async () => {
    await renderInbox(ROWS, '/triage?projectSlug=example-product')
    expect(mocked.fetchTriage).toHaveBeenCalledWith('example-product')
    expect(screen.getByText(/scope: example-product/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /clear/i })).toHaveAttribute('href', '/triage')
  })

  it('names the scope in the empty state', async () => {
    await renderInbox([], '/triage?projectSlug=example-product')
    expect(screen.getByText(/inbox zero for example-product/i)).toBeInTheDocument()
  })
```

(`renderInbox([])` waits for `/inbox zero/i`, which the scoped empty state still contains.)

- [ ] **Step 2: Run to see them fail**

Run: `(cd web && npx vitest run src/triage/__tests__/TriageInbox.test.tsx)`
Expected: the two new tests FAIL (`fetchTriage` called with `undefined`; no scope chip).

- [ ] **Step 3: Read the scope from the URL**

In `TriageInbox.tsx` add imports:

```ts
import { Link, useSearchParams } from 'react-router-dom'
import { NAV_BY_ID } from '@/shell/navDefs'
```

Change the top of the component to:

```tsx
export function TriageInbox() {
  useTriageLive()
  const [params] = useSearchParams()
  const projectSlug = params.get('projectSlug') || undefined
  const { data: rows = [], isLoading } = useTriage(projectSlug)
  const accept = useAcceptProposal(projectSlug)
  const reject = useRejectProposal(projectSlug)
  const acceptBulk = useAcceptBulk(projectSlug)
```

Replace the two early returns and the header with:

```tsx
  if (isLoading) return <div className="triage-empty">Loading…</div>
  if (!rows.length) {
    return (
      <div className="triage-empty">
        {projectSlug
          ? <>Inbox zero for {projectSlug}. <Link to={NAV_BY_ID.triage.path}>Show every product</Link></>
          : 'Inbox zero — nothing to triage.'}
      </div>
    )
  }

  return (
    <div className="triage-inbox">
      <header className="triage-header">
        <span className="triage-hint">j/k move · a accept · e edit · r reject · x select · A accept selected</span>
        {projectSlug && (
          <span className="triage-depth">
            Scope: {projectSlug} <Link to={NAV_BY_ID.triage.path}>Clear</Link>
          </span>
        )}
        {depths.map(([slug, n]) => (
          <span key={slug} className="triage-depth">{slug}: {n}</span>
        ))}
      </header>
```

- [ ] **Step 4: Run the triage tests and the full suite**

Run: `(cd web && npx vitest run src/triage && npm run test && npm run typecheck && npm run lint)`
Expected: PASS. If any other test renders `TriageInbox` without a router (grep `<TriageInbox` under `web/src`), wrap that render in `MemoryRouter` too.

- [ ] **Step 5: Commit**

```bash
printf 'feat(web): triage inbox accepts ?projectSlug= and shows a clearable scope\n\nThe per-product variant of every triage hook existed end to end and had no\ncontrol; the wizard now links into it.\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t7.txt
git add web/src/triage
git commit -F /tmp/p1-t7.txt
```

---

### Task 8: Shell — per-section list column and open-item resolvers

**Files:**
- Create: `web/src/shell/listColumns.tsx`, `web/src/shell/listColumns.test.tsx`
- Create: `web/src/shell/openItems.ts`
- Modify: `web/src/shell/regions/ContextList.tsx:8-23,67-92`
- Modify: `web/src/shell/openSessionId.ts:37-61`
- Modify: `web/src/shell/AppShell.tsx:337`
- Test: `web/src/shell/regions/ContextList.test.tsx:105-130`, `web/src/shell/openSessionId.test.tsx`

**Interfaces:**
- Produces (`listColumns.tsx`): `LIST_SECTIONS: readonly NavId[] = ['sessions']`; `interface ListColumnSources { sessions: SessionListColumnProps }`; `listColumnFor(id: NavId | null | undefined, sources: ListColumnSources): React.ReactNode`.
- Produces (`openItems.ts`): `type OpenItem = { kind: 'session'; id: string } | { kind: 'archived'; id: string }`; `type OpenItemResolver = (params: Readonly<Params<string>>) => OpenItem | null`; `OPEN_ITEM_RESOLVERS: Partial<Record<NavId, OpenItemResolver>>`.
- Produces (`ContextList`): props become `{ column: React.ReactNode }`.
- `openSessionId.ts` keeps exporting `OpenItem` (re-export), `useOpenItem`, `useOpenSessionId` with unchanged signatures.

- [ ] **Step 1: Write the failing invariant test**

`web/src/shell/listColumns.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest'
import { NAV_DEFS } from './navDefs'
import { LIST_SECTIONS } from './listColumns'

describe('listColumns', () => {
  it('LIST_SECTIONS is exactly the set of nav sections with hasList', () => {
    // One fact, two readers: `hasList` drives the region matrix, LIST_SECTIONS
    // drives which column renders. A section that flips one without the other
    // either eats a grid column with nothing in it or renders a column the
    // matrix hides.
    expect([...LIST_SECTIONS]).toEqual(NAV_DEFS.filter((d) => d.hasList).map((d) => d.id))
  })
})
```

And in `web/src/shell/openSessionId.test.tsx` add a row to the `it.each` table:

```ts
    ['/projects/abc', 'none|-'],
```

and add `'/projects/:id'` to the route list in `renderAt`. (A `:id` under a section with no resolver must not read as an open session.)

- [ ] **Step 2: Run to see them fail**

Run: `(cd web && npx vitest run src/shell/listColumns.test.tsx src/shell/openSessionId.test.tsx)`
Expected: FAIL — cannot resolve `./listColumns`; the projects row passes already (section check) but stays as a regression guard.

- [ ] **Step 3: Create the two tables**

`web/src/shell/listColumns.tsx`:

```tsx
import type * as React from 'react'
import type { NavId } from './navDefs'
import {
  SessionListColumn,
  type SessionListColumnProps,
} from '@/features/sessions/SessionListColumn'

/**
 * Which nav sections own a list column, and how each one renders.
 *
 * `NavDef.hasList` tells the region matrix whether `app.list` exists for a
 * section; this is the other half — what goes in it. The two are pinned equal
 * by `listColumns.test.tsx`. Adding a list section means: flip `hasList`, add
 * the id here, add a source field and a `case`. `AppShell` supplies the
 * sources from its workspace hooks.
 */
export const LIST_SECTIONS = ['sessions'] as const satisfies readonly NavId[]

export interface ListColumnSources {
  sessions: SessionListColumnProps
}

export function listColumnFor(
  id: NavId | null | undefined,
  sources: ListColumnSources,
): React.ReactNode {
  switch (id) {
    case 'sessions':
      return <SessionListColumn {...sources.sessions} />
    default:
      return null
  }
}
```

`web/src/shell/openItems.ts`:

```ts
import type { Params } from 'react-router-dom'
import type { NavId } from './navDefs'

/** What the current route has open, if anything. */
export type OpenItem =
  | { kind: 'session'; id: string }
  | { kind: 'archived'; id: string }

export type OpenItemResolver = (params: Readonly<Params<string>>) => OpenItem | null

/**
 * Per-section "is something open?" from the matched route's params.
 *
 * Sessions: the archived reader's param is `archivedId`, not `id`, and that
 * NAME is what keeps `PageHost` from mounting a live runtime over a dead
 * session (`session/sessionNavState.ts` decides it). A section with no entry
 * here never reports an open item, so a `:id`-shaped route elsewhere cannot
 * be mistaken for a session.
 */
export const OPEN_ITEM_RESOLVERS: Partial<Record<NavId, OpenItemResolver>> = {
  sessions: ({ id, archivedId }) => {
    if (archivedId) return { kind: 'archived', id: archivedId }
    if (id) return { kind: 'session', id }
    return null
  },
}
```

- [ ] **Step 4: Consume them**

`web/src/shell/openSessionId.ts` — replace everything from `import { useLocation, useParams }` down with:

```ts
import { useLocation, useParams } from 'react-router-dom'
import { navDefForPath } from './navDefs'
import { OPEN_ITEM_RESOLVERS } from './openItems'

export type { OpenItem } from './openItems'

/**
 * The open item, or null. See the module docstring for why one function must
 * answer for all three consumers. The per-section rule lives in
 * `openItems.ts`; this only picks the section from the path.
 */
export function useOpenItem() {
  const params = useParams()
  const { pathname } = useLocation()
  const def = navDefForPath(pathname)
  if (!def) return null
  return OPEN_ITEM_RESOLVERS[def.id]?.(params) ?? null
}

/** The open LIVE session's id, or null — unchanged signature/behaviour, now
 *  re-derived from `useOpenItem` rather than a parallel computation. */
export function useOpenSessionId(): string | null {
  const item = useOpenItem()
  return item?.kind === 'session' ? item.id : null
}
```

Keep the module's header comment; delete the paragraph that describes the "deliberately asymmetric" archived check, since the resolver is now section-scoped.

`web/src/shell/regions/ContextList.tsx`:
- Replace the `ContextListProps` interface and the `SessionListColumn` import with:

```ts
export interface ContextListProps {
  /**
   * The section's column, chosen by `shell/listColumns.ts` from the workspace
   * sources `AppShell` holds. The region itself only decides WHETHER a column
   * renders (`navDefForPath(pathname)?.hasList`) and names the landmark.
   */
  column: React.ReactNode
}
```
- Signature `export function ContextList({ column }: ContextListProps)`; guard `if (!def?.hasList || column == null) return null`; render `{column}` where `<SessionListColumn {...sessions} />` was.

`web/src/shell/AppShell.tsx` line 337:

```tsx
          {listVisible && (
            <ContextList column={listColumnFor(def?.id, { sessions: workspace.list })} />
          )}
```

with `import { listColumnFor } from './listColumns'` added to the imports.

`web/src/shell/regions/ContextList.test.tsx` `Harness`: import `SessionListColumn` from `@/features/sessions/SessionListColumn` and pass the same props object as `column={<SessionListColumn scope={…} state={state} … />}` (move the object literal into JSX props).

- [ ] **Step 5: Run the shell tests, then the full suite**

Run: `(cd web && npx vitest run src/shell && npm run test && npm run typecheck && npm run lint)`
Expected: PASS. `ContextList.test.tsx`'s identity tests must still hold (the column element is recreated each render but `SessionListColumn`'s DOM nodes are not remounted; the test asserts node identity).

- [ ] **Step 6: Commit**

```bash
printf 'refactor(shell): list column and open-item detection are per-section tables\n\nContextList rendered SessionListColumn unconditionally and useOpenItem only\nknew Sessions. LIST_SECTIONS/listColumnFor and OPEN_ITEM_RESOLVERS make a\nsecond master/detail section (Products, #993) a table entry, not a shell edit.\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t8.txt
git add web/src/shell
git commit -F /tmp/p1-t8.txt
```

---

### Task 9: Page contract for Initiatives, Products and Triage

**Files:**
- Modify: `web/src/initiatives/InitiativesTab.tsx:119-187`
- Modify: `web/src/projects/ProjectsTab.tsx:123-145`
- Modify: `web/src/triage/TriageInbox.tsx` (outer render)
- Test: `web/src/features/settings/pageContract.test.tsx:66-68`

**Interfaces:**
- Consumes: `PageHeader`, `PageContent` from `@/ui`, `Button` from `@/ui/Button`.
- Produces: each page renders `<div className="flex flex-col h-full min-h-0" data-page-variant="standard">` → `PageHeader` (the only `h1`) → `PageContent`.

- [ ] **Step 1: Add the three rows**

In `pageContract.test.tsx` replace `PAGES` with:

```ts
const PAGES: readonly PageContractRow[] = [
  { nav: 'settings',    path: '/settings/general', variant: 'split',    title: 'Settings' },
  { nav: 'initiatives', path: '/initiatives',      variant: 'standard', title: 'Initiatives' },
  { nav: 'projects',    path: '/projects',         variant: 'standard', title: 'Products' },
  { nav: 'triage',      path: '/triage',           variant: 'standard', title: 'Triage' },
]
```

- [ ] **Step 2: Run to see the new rows fail**

Run: `(cd web && npx vitest run src/features/settings/pageContract.test.tsx)`
Expected: 12 new cases FAIL (no `h1`).

- [ ] **Step 3: Wrap Initiatives**

In `InitiativesTab.tsx`:
- Add `import { PageHeader } from '@/ui/PageHeader'`, `import { PageContent } from '@/ui/PageContent'`, `import { Button } from '@/ui/Button'`.
- Change `NewInitiativeForm` to take `{ onClose }: { onClose: () => void }`, drop its internal `open` state and the collapsed button branch, call `onClose()` in the create `onSuccess` and from the Cancel button.
- Replace `InitiativesTab`:

```tsx
export function InitiativesTab() {
  useInitiativesLive()
  const { data: initiatives, isLoading, isError } = useInitiatives()
  const [creating, setCreating] = useState(false)
  return (
    <div className="flex flex-col h-full min-h-0 prdash-root" data-page-variant="standard">
      <PageHeader
        title="Initiatives"
        actions={
          <Button variant="ghost" size="sm" onClick={() => setCreating(true)}>
            + New initiative
          </Button>
        }
      />
      <PageContent busy={isLoading} className="init-board">
        {creating && <NewInitiativeForm onClose={() => setCreating(false)} />}
        {/* The back of the SDLC loop sits above its front: an open incident is the
            thing most likely to become the next initiative on this board. */}
        <IncidentQueue />
        {isError && <p className="stale" role="status">Could not load initiatives.</p>}
        {!isLoading && !isError && (initiatives?.length ?? 0) === 0 && (
          <p className="init-empty">
            No initiatives yet. Create one, or let the SDLC automations open the first intent.
          </p>
        )}
        {initiatives?.map((init) => <InitiativeRow key={init.slug} init={init} />)}
      </PageContent>
    </div>
  )
}
```

Delete the `<h2 className="init-board-title">` and the `Loading initiatives…` paragraph (`PageContent` announces loading). Remove the now-unused `.init-board-head` / `.init-board-title` rules from `initiatives.css`.

- [ ] **Step 4: Wrap Products**

In `ProjectsTab.tsx` replace `ProjectsTab`:

```tsx
export function ProjectsTab() {
  const { data: projects, isLoading } = useProjects()
  const [showForm, setShowForm] = useState(false)
  return (
    <div className="flex flex-col h-full min-h-0" data-page-variant="standard">
      <PageHeader
        title={PRODUCT.plural}
        actions={<Button size="sm" onClick={() => setShowForm(true)}>{PRODUCT.new}</Button>}
      />
      <PageContent busy={isLoading} className="pm-projects">
        {showForm && <NewProjectForm onClose={() => setShowForm(false)} />}
        {!isLoading && (projects ?? []).length === 0 && (
          <p className="pm-muted">{PRODUCT.empty}</p>
        )}
        <div className="pm-project-grid">
          {(projects ?? []).map((p) => <ProjectCard key={p.id} project={p} />)}
        </div>
        <TriageCta />
      </PageContent>
    </div>
  )
}
```

with the same three `@/ui` imports. Delete the `.pm-projects-toolbar` block and its CSS rule, and the `Loading…` paragraph.

- [ ] **Step 5: Wrap Triage**

In `TriageInbox.tsx`, rename the current component body to an inner `InboxBody` (everything from `useTriageLive()` down, unchanged, including its early returns) and add:

```tsx
export function TriageInbox() {
  return (
    <div className="flex flex-col h-full min-h-0" data-page-variant="standard">
      <PageHeader
        title="Triage"
        meta={<span className="triage-hint">j/k move · a accept · e edit · r reject · x select · A accept selected</span>}
      />
      <PageContent>
        <InboxBody />
      </PageContent>
    </div>
  )
}
```

and remove the duplicate `triage-hint` span from `InboxBody`'s header (the keys now live in `meta`). `isLoading` stays inside the body; the header must render while loading (contract §2.3).

- [ ] **Step 6: Run the contract test, the three page suites, then everything**

Run: `(cd web && npx vitest run src/features/settings/pageContract.test.tsx src/initiatives src/projects src/triage && npm run test && npm run typecheck && npm run lint && npm run build)`
Expected: PASS, build clean. `TriageInbox.test.tsx` tests that assert on the hint text keep passing (it is still rendered, in the header).

- [ ] **Step 7: Commit**

```bash
printf 'feat(web): Initiatives, Products and Triage join the page contract\n\nOne h1 via PageHeader, document.title, data-page-variant, PageContent busy\nstate; rows added to pageContract.test.tsx.\n\nRefs #993, #1414\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t9.txt
git add web/src/initiatives web/src/projects web/src/triage web/src/features/settings/pageContract.test.tsx
git commit -F /tmp/p1-t9.txt
```

---

### Task 10: Nav help text says purpose

**Files:**
- Modify: `web/src/shell/navDefs.ts:91-102`
- Test: `web/src/shell/navDefs.test.ts` (existing `gives every entry real help text` covers length)

- [ ] **Step 1: Add a guard that help text does not carry keystrokes**

In `navDefs.test.ts` add:

```ts
  it('help text describes purpose, not keys — keys belong to the shortcut registry', () => {
    for (const d of NAV_DEFS) {
      expect(d.help, `${d.id} help`).not.toMatch(/\bj\/k\b|\bpress\b|keystroke/i)
    }
  })
```

- [ ] **Step 2: Run to see it fail**

Run: `(cd web && npx vitest run src/shell/navDefs.test.ts)`
Expected: FAIL on `triage`.

- [ ] **Step 3: Rewrite the three strings**

```ts
  { id: 'initiatives', label: 'Initiatives', icon: Target,          path: '/initiatives',                       showInKiosk: false, hasList: false,
    help: 'Every initiative as a stage track: the gate it is at, the artifact each gate needs, and the open incidents that may become the next one.' },
  …
  { id: 'triage',      label: 'Triage',      icon: Inbox,           path: '/triage',                            showInKiosk: false, hasList: false,
    help: 'Proposed classifications awaiting your decision: each open issue matched to a capability with scores. Accept, adjust, or reject; the key map is on the page.' },
  …
  { id: 'projects',    label: 'Products',    icon: Package,         path: '/projects',                          showInKiosk: false, hasList: false,
    help: `The products ${BRAND_NAME} tracks: each one's repos, onboarding progress and mirrored dependencies. Triage is its own section.` },
```

- [ ] **Step 4: Run the shell tests and the full suite**

Run: `(cd web && npx vitest run src/shell && npm run test)`
Expected: PASS (`DrawerHost.test.tsx` renders the help sheet; a snapshot there, if any, needs updating with `npx vitest run -u src/shell/regions/DrawerHost.test.tsx` and a look at the diff).

- [ ] **Step 5: Commit**

```bash
printf 'docs(web): nav help for Initiatives, Triage and Products says what each is for\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p1-t10.txt
git add web/src/shell/navDefs.ts web/src/shell/navDefs.test.ts
git commit -F /tmp/p1-t10.txt
```

---

### Task 11: Verify the whole gate and open the PR

**Files:** none new.

- [ ] **Step 1: Server gate, all four steps**

Unset the live-shell variables first, and run the suite under the test-slot cap from the repo's `core.md` (acquire loop, pytest and trap in ONE Bash call):

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_SOCKET -u CCSWITCH_SESSIOND_HOST -u CCSWITCH_SESSIOND_PORT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 \
  bash -c '.venv/bin/ruff check src/ server/ && .venv/bin/mypy server/ --ignore-missing-imports && .venv/bin/python -m pytest tests/ -q && .venv/bin/python -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q' 2>&1 | tee /tmp/p1-server-gate.log | tail -20
```

Expected: ruff clean, mypy clean, both pytest runs green. (See the memory note: on rocky, scrub the whole env if stray `CCSWITCH_*` vars still leak.)

- [ ] **Step 2: Web gate**

```bash
(cd web && npm run typecheck && npm run lint && npm run test && npm run build) 2>&1 | tee /tmp/p1-web-gate.log | tail -20
```

Expected: all four green.

- [ ] **Step 3: Rebase on origin/master and push**

```bash
git fetch origin && git rebase origin/master && git push -u origin HEAD
```

- [ ] **Step 4: Open the PR, ready, auto-merge**

Write the body to `/tmp/p1-pr-body.md`:

```markdown
## Summary

Phase 1 of the roadmap drill-down (spec `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md`, §13.1): shell generalization and every dead end on the Products, Initiatives and Triage pages. No schema change.

- **Onboarding engine:** a new product can reach "propose map" (nothing writes `syncing`); `map_proposed` waits for the PR instead of re-offering itself; the dead `map_pr_*` flags are gone.
- **Wizard:** PR search link, "Map merged → continue" (advance-map, endpoint existed with no UI), "Go live" (new `advance-live`), triage link scoped to the product, honest `register` copy.
- **Vocabulary:** one module; Advance button and track agree; onboarding chip is friendly; Products says product.
- **Triage:** `?projectSlug=` scope with a clearable chip.
- **Sync now** toasts.
- **Shell:** `LIST_SECTIONS`/`listColumnFor` and `OPEN_ITEM_RESOLVERS` replace the Sessions hardcode.
- **Page contract:** Initiatives, Products, Triage rows.

Refs #993, #1414

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

```bash
gh pr create --title "feat(roadmap): phase 1 — shell generalization and dead-end fixes (#993)" --body-file /tmp/p1-pr-body.md --base master
gh pr merge <#> --squash --auto
```

If `gh pr merge --auto` reports it cannot enable auto-merge because mergeability is still `UNKNOWN`, wait for the first check to start and re-run it.
