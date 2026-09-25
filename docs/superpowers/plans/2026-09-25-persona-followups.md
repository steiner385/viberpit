# Persona-layer follow-ups (#2446, #2447) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the two first consumers the persona layer (#2164, landed in PR #2453) was built for: a persona filter on the roadmap capability tree (#2446), and persona docs injected into the SDLC spec-generation prompt (#2447).

**Architecture:** Both issues thread `PersonaFeature` (persona ↔ capability resolved edges, `server/db/models.py:1922`) through an existing read path, following an existing convention exactly rather than inventing one: #2446 mirrors the `FeatureLink`/`links` batching already in `/pm/map`, and adds a filter following the "self OR any descendant matches" recursive-filter shape `treeRows.ts` already uses for text search. #2447 mirrors the "UPSTREAM INTENT (read-only)" fenced-block convention in `build_spec_prompt`, resolving "this session's capability" via `InitiativeFeature.is_primary` (an existing link, not a new column) the same optional-pointer way `_sdlc_epic_upstream_text` already treats `epic_repo`/`epic_number`.

**Tech Stack:** Python (FastAPI, SQLAlchemy async, pytest) for the server; TypeScript/React (Vitest, Testing Library) for the web.

**Spec:** steiner385/viberpit#2446, steiner385/viberpit#2447 (both "Follow-up to #2164"); background design doc `docs/superpowers/plans/2026-09-24-persona-layer.md`.

## Global Constraints

- Never invent a new "capability" link when an existing one resolves the same fact: #2447 must use `InitiativeFeature.is_primary`, not a new column on `Initiative`.
- A persona reference must always be filtered to live (non-orphaned) `Persona` rows — an orphaned persona doc must never surface in a filter dropdown or get injected into a prompt.
- Field names crossing the server/web boundary are camelCase verbatim (`web/src/api/roadmap.ts`'s header comment) — the new field is `personaSlugs`, matching `Feature.persona_slugs`'s naming in spirit but resolved (not raw-declared).
- Every new server query must be batched (no N+1 per capability/feature), consistent with `/pm/map`'s existing "fixed number of grouped queries per request" contract (`server/api/pm_tree.py` module docstring).
- `docs/product/README.md`'s persona-layer convention section documents `PersonaFeature`'s two named first consumers by issue number — keep it accurate if its wording changes as a result of this work (it should not need to; only note it if a task discovers otherwise).

## Review Focus

- A capability with an empty `personaSlugs` list (never had a `personas:` frontmatter match) must render with `links: []`/`personaSlugs: []` shape intact — never `null`, which would crash a `.length`/`.includes()` call downstream (Task 1, Task 2).
- Selecting a persona filter with an empty search query must not trivially show every product row — only products with a capability that actually carries the selected persona somewhere in its tree (Task 2, Review step in Task 2's own tests).
- An initiative-only "orphan" (no primary capability, e.g. a brand-new epic-adopted initiative with no `intent.md` yet) must not crash `_sdlc_capability_personas` — it returns `None`, and the spec prompt renders with no persona section at all, same as it does today (Task 3).
- Two personas linked to the same capability, one of them orphaned since the last pm-sync, must inject only the live one — the orphaned edge is a live DB row (`PersonaFeature` rows are rewritten wholesale, but a `Persona.orphaned=True` row can still have leftover edges until the next sync tick) (Task 1, Task 3).
- The plan stage (`spec.md` → `plan.md`) must never resolve or inject personas — the issue scopes this to the **spec** prompt only, and a stray resolution call there would waste a DB round trip on every plan spawn for no reason (Task 3).

---

### Task 1: Thread `persona_slugs` onto the `/pm/map` capability-tree payload

**Files:**
- Modify: `server/pm/tree.py:229-241` (`CapabilityIn`), `server/pm/tree.py:342-364` (`_capability_node`)
- Modify: `server/api/pm_tree.py:14-16` (imports), `server/api/pm_tree.py:91-226` (`get_map`)
- Test: `server/tests/test_pm_tree.py`
- Test: `server/tests/test_pm_map_api.py`

**Interfaces:**
- Produces: `CapabilityIn.persona_slugs: tuple[str, ...]` (default `()`); the `/pm/map` JSON node's `personaSlugs: string[]` key. Task 2 consumes `personaSlugs` on the web's `CapabilityNode` type.

- [ ] **Step 1: Write the failing pure-function tests**

Add to `server/tests/test_pm_tree.py`. First widen the existing `_cap` helper (around line 87) to accept personas:

```python
def _cap(slug, parent=None, state="live", issues=(), links=(), personas=()):
    return CapabilityIn(id=f"id-{slug}", slug=slug, title=slug.title(), state=state, summary="",
                        doc_path=f"d/{slug}.md", commit_sha=None, pr_number=None, parent_slug=parent,
                        issue_tiers=tuple(issues), links=tuple(links), persona_slugs=tuple(personas))
```

Then add two new tests (anywhere after `test_build_product_tree_nests_and_rolls_up`):

```python
def test_capability_node_carries_persona_slugs():
    product = ProductIn(
        slug="example-product", name="Example",
        capabilities=(_cap("sessions", personas=("parent", "teen")),),
        initiatives=(), awaiting_triage=0, needs_home=0)
    tree = build_product_tree(product)
    assert tree["capabilities"][0]["personaSlugs"] == ["parent", "teen"]


def test_capability_node_defaults_persona_slugs_to_empty_list():
    product = ProductIn(
        slug="example-product", name="Example",
        capabilities=(_cap("sessions"),),
        initiatives=(), awaiting_triage=0, needs_home=0)
    tree = build_product_tree(product)
    assert tree["capabilities"][0]["personaSlugs"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/tony/GitHub/.ccswitch/worktrees/viberpit/7e4fe48e && /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_tree.py -k persona_slugs -v` (the main checkout's prebuilt `.venv`, per this repo's worktree note — a fresh worktree has no built env of its own).

Expected: FAIL — `_cap() got an unexpected keyword argument 'personas'` (or, once `_cap` alone is patched in, a `KeyError`/`AssertionError` on `personaSlugs`).

- [ ] **Step 3: Add the field to `CapabilityIn` and emit it from `_capability_node`**

In `server/pm/tree.py`, extend `CapabilityIn` (line 229-241):

```python
@dataclass(frozen=True)
class CapabilityIn:
    id: str
    slug: str
    title: str | None
    state: str | None
    summary: str | None
    doc_path: str | None
    commit_sha: str | None
    pr_number: int | None
    parent_slug: str | None
    issue_tiers: tuple[str, ...]          # one tier per OPEN issue classified under it
    links: tuple[tuple[str, str, str], ...] = ()   # (kind, project slug, feature slug)
    # Resolved PersonaFeature edges (issue #2164/#2453), live personas only —
    # NOT the raw Feature.persona_slugs mirror (that's `unlinked_personas`'
    # source in server/pm/health.py, a different concern: declared-but-
    # unresolved). First consumer: steiner385/viberpit#2446's filter chip.
    persona_slugs: tuple[str, ...] = ()
```

Then in `_capability_node` (line 356-364), add the key:

```python
    return {
        "id": cap.id, "slug": cap.slug, "title": cap.title, "state": cap.state,
        "summary": cap.summary, "docPath": cap.doc_path, "commitSha": cap.commit_sha,
        "prNumber": cap.pr_number, "parentSlug": cap.parent_slug, "tier": tier,
        "counts": counts,
        "links": [{"kind": k, "projectSlug": p, "featureSlug": f} for k, p, f in cap.links],
        "personaSlugs": list(cap.persona_slugs),
        "initiatives": touching,
        "children": children,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_tree.py -v` (whole file — confirm no other test broke from the new dataclass field or dict key).

Expected: PASS, all tests in the file green.

- [ ] **Step 5: Write the failing HTTP-level test**

Add to `server/tests/test_pm_map_api.py`, a new test function (mirrors `test_map_returns_nested_capabilities_initiatives_and_tiers`'s seeding shape):

```python
def test_map_resolves_persona_slugs_onto_capability_nodes_live_only():
    import asyncio

    from server.db import db_session, init_db
    from server.db.models import Feature, Persona, PersonaFeature, Project

    async def _seed(db_url: str) -> None:
        await init_db(db_url)
        async with db_session() as db:
            prod = Project(slug="example-product", name="Example product")
            db.add(prod)
            await db.flush()
            sessions = Feature(project_id=prod.id, slug="sessions", title="Sessions", state="live")
            db.add(sessions)
            await db.flush()
            parent = Persona(project_id=prod.id, slug="parent", title="Parent")
            ghost = Persona(project_id=prod.id, slug="ghost", title="Ghost", orphaned=True)
            db.add_all([parent, ghost])
            await db.flush()
            db.add(PersonaFeature(persona_id=parent.id, feature_id=sessions.id))
            db.add(PersonaFeature(persona_id=ghost.id, feature_id=sessions.id))
            await db.commit()

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            body = r.json()
            products = {p["slug"]: p for p in body["products"]}
            roots = {cap["slug"]: cap for cap in products["example-product"]["capabilities"]}
            assert roots["sessions"]["personaSlugs"] == ["parent"]
```

- [ ] **Step 6: Run it to verify it fails**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_map_api.py -k persona_slugs -v`

Expected: FAIL — `KeyError: 'personaSlugs'`.

- [ ] **Step 7: Batch-resolve `PersonaFeature`→`Persona` in `get_map` and feed `CapabilityIn`**

In `server/api/pm_tree.py`, add `Persona, PersonaFeature` to the models import (line 14-16):

```python
from server.db.models import (
    Feature, FeatureLink, GhProjectItem, Initiative, Persona, PersonaFeature, Project,
    ProjectRepo, Session as DbSession, TriageItem, TriageProposal, WorkItem, WorkItemDep)
```

Then, right after the existing `links_by_src` block (ends at line 121, just before the `open_items` query at line 123), add:

```python
    # Resolved persona links (#2164/#2453) — same batching shape as links_by_src
    # just above: one query scoped to this request's features, live personas
    # only (an orphaned Persona's leftover PersonaFeature row must never surface
    # in the filter — steiner385/viberpit#2446).
    persona_rows = (await db.execute(select(PersonaFeature.feature_id, Persona.slug).join(
        Persona, PersonaFeature.persona_id == Persona.id).where(
        PersonaFeature.feature_id.in_(list(feat_by_id)),
        Persona.orphaned.isnot(True)))).all() if feat_by_id else []
    personas_by_feature: dict[str, list[str]] = {}
    for fid, pslug in persona_rows:
        if pslug:
            personas_by_feature.setdefault(fid, []).append(pslug)
```

Then, in the `cap_inputs` construction (line 189-195), add the kwarg:

```python
        cap_inputs = tuple(CapabilityIn(
            id=f.id, slug=f.slug or "", title=f.title, state=f.state, summary=f.summary,
            doc_path=f.repo_path, commit_sha=f.commit_sha, pr_number=f.pr_number,
            parent_slug=slug_by_fid.get(f.parent_feature_id) if f.parent_feature_id else None,
            issue_tiers=tuple(tiers_by_feature.get(f.id, [])),
            links=tuple(links_by_src.get(f.id, [])),
            persona_slugs=tuple(personas_by_feature.get(f.id, [])),
        ) for f in my_feats)
```

- [ ] **Step 8: Run it to verify it passes**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_map_api.py -v` (whole file).

Expected: PASS, all tests in the file green.

- [ ] **Step 9: Run ruff and mypy on the touched files**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m ruff check server/pm/tree.py server/api/pm_tree.py server/tests/test_pm_tree.py server/tests/test_pm_map_api.py && /home/tony/GitHub/viberpit/.venv/bin/python -m mypy server/pm/tree.py server/api/pm_tree.py --ignore-missing-imports`

Expected: no errors from either.

- [ ] **Step 10: Commit**

```bash
git add server/pm/tree.py server/api/pm_tree.py server/tests/test_pm_tree.py server/tests/test_pm_map_api.py
git commit -m "$(cat <<'EOF'
feat(pm): thread resolved persona_slugs onto the /pm/map capability tree

Batches PersonaFeature -> Persona (live only) the same way links_by_src
already batches FeatureLink, and carries the result through CapabilityIn
into each capability node's personaSlugs, so the web can build a persona
filter without a second per-capability query.

Refs #2446

Co-Authored-By: Claude <your model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Persona filter chip on the roadmap capability tree UI

**Files:**
- Modify: `web/src/api/roadmap.ts:28-44` (`CapabilityNode`)
- Modify: `web/src/features/roadmap/__fixtures__/map.ts` (`cap()` builder)
- Modify: `web/src/features/roadmap/treeRows.ts` (filter logic)
- Modify: `web/src/features/roadmap/RoadmapListColumn.tsx` (`MapScopeBody`)
- Test: `web/src/features/roadmap/treeRows.test.ts`
- Test: `web/src/features/roadmap/RoadmapListColumn.test.tsx`

**Interfaces:**
- Consumes: `CapabilityNode` from `web/src/api/roadmap.ts` (Task 1 backend already emits the `personaSlugs` JSON key this task types).
- Produces: `flattenMap(map, state, query, personaSlug?)` (4th param, optional, default `null`) and `personaSlugsInMap(map)`, both exported from `treeRows.ts` — no other task consumes these, but keep the names exact since the tests below assert against them.

- [ ] **Step 1: Add `personaSlugs` to the `CapabilityNode` type**

In `web/src/api/roadmap.ts`, extend the interface (line 28-44):

```ts
export interface CapabilityNode {
  id: string
  slug: string
  title: string | null
  state: string | null
  summary: string | null
  docPath: string | null
  commitSha: string | null
  prNumber: number | null
  parentSlug: string | null
  tier: RoadmapTier
  counts: CapabilityCounts
  links: CapabilityLink[]
  /** Slugs of initiatives that TOUCH this capability (not primary). */
  initiatives: string[]
  /** Resolved PersonaFeature edges, live personas only (steiner385/viberpit#2446). */
  personaSlugs: string[]
  children: CapabilityNode[]
}
```

- [ ] **Step 2: Give the fixture builder a default so every existing `cap(...)` call keeps compiling**

In `web/src/features/roadmap/__fixtures__/map.ts`, add `personaSlugs: []` to `cap()`'s defaults:

```ts
export function cap(over: Partial<CapabilityNode> & { slug: string }): CapabilityNode {
  return {
    id: `f-${over.slug}`, title: over.slug, state: 'live', summary: null, docPath: null,
    commitSha: null, prNumber: null, parentSlug: null, tier: 'idle',
    counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 },
    links: [], initiatives: [], personaSlugs: [], children: [], ...over,
  }
}
```

Also give two capabilities in the shared `MAP` fixture a persona so the new tests below have real data to filter on — change the `billing` and `tasks` entries:

```ts
      capabilities: [
        cap({ slug: 'billing', title: 'Billing', personaSlugs: ['parent'] }),
        // `tasks` counts 5 open issues, 3 of which are its child's: capability
        // counts are ROLLED UP by the server, so a parent's direct issues are
        // the difference. Kept different on purpose so the subtraction shows.
        cap({ slug: 'tasks', title: 'Tasks', tier: 'in-flight',
          counts: { openIssues: 5, inFlight: 0, blocked: 0, needsYou: 0 },
          children: [
            cap({ slug: 'task-views', title: 'Task views', parentSlug: 'tasks', tier: 'in-flight',
              counts: { openIssues: 3, inFlight: 0, blocked: 0, needsYou: 0 },
              personaSlugs: ['teen'], initiatives: ['views'] }),
          ] }),
      ],
```

(`billing` now carries `parent`; the nested `task-views` carries `teen`; `tasks` itself and `viberpit`'s `sessions` carry none — deliberately, so a filter test can assert a mid-tree ancestor shows only via the "descendant matches" path.)

- [ ] **Step 3: Write the failing filter tests**

Add to `web/src/features/roadmap/treeRows.test.ts` (new `describe` block, anywhere top-level):

```ts
describe('flattenMap persona filter', () => {
  it('keeps only capabilities carrying the persona, plus ancestors on the path to a match', () => {
    expect(keys(flattenMap(MAP, EXPANDED, '', 'teen'))).toEqual([
      productKey('kindash'),
      capabilityKey('kindash', 'tasks'),
      capabilityKey('kindash', 'task-views'),
      initiativeKey('views'),
    ])
  })

  it('a persona with no match anywhere yields no rows', () => {
    expect(keys(flattenMap(MAP, EXPANDED, '', 'nobody'))).toEqual([])
  })

  it('combines with a text query as an AND, not an OR', () => {
    // 'billing' text-matches only the billing capability; 'teen' matches only
    // task-views — no row satisfies both, so nothing survives.
    expect(keys(flattenMap(MAP, EXPANDED, 'billing', 'teen'))).toEqual([])
  })

  it('null personaSlug behaves exactly like the 3-arg call (no filter)', () => {
    expect(keys(flattenMap(MAP, EXPANDED, '', null))).toEqual(keys(flattenMap(MAP, EXPANDED, '')))
  })
})

describe('personaSlugsInMap', () => {
  it('collects every distinct persona slug declared anywhere in the map, sorted', () => {
    expect(personaSlugsInMap(MAP)).toEqual(['parent', 'teen'])
  })
})
```

Add `personaSlugsInMap` to the existing import line at the top of the file:

```ts
import { flattenMap, toggleKey, capabilityKey, productKey, initiativeKey, setCollapsed, personaSlugsInMap } from './treeRows'
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd web && npm run test -- treeRows.test.ts` (or `npx vitest run src/features/roadmap/treeRows.test.ts` from `web/`).

Expected: FAIL — `flattenMap` only accepts 3 args (TS will actually catch the 4th-arg call at compile time first; the test run will fail to even build) and `personaSlugsInMap` is not exported.

- [ ] **Step 5: Implement the persona filter in `treeRows.ts`**

Add a `personaMatches` helper right after `rowMatches` (after line 54):

```ts
/** True when no persona filter is active, or `row` is a capability row that
 *  declares `personaSlug` — false for every other row kind while a filter IS
 *  active, so a product/initiative/pseudo row never "vacuously" survives a
 *  persona filter the way `rowMatches('')` would with an empty query; it only
 *  shows via the existing "kept when a descendant matches" recursion below. */
function personaMatches(row: TreeRow, personaSlug: string | null): boolean {
  if (personaSlug === null) return true
  return row.kind === 'capability' && row.capability.personaSlugs.includes(personaSlug)
}
```

Change `flattenProduct`'s signature and `searching` (lines 61-62):

```ts
function flattenProduct(product: ProductNode, state: TreeState, query: string, personaSlug: string | null): TreeRow[] {
  const searching = query.trim() !== '' || personaSlug !== null
```

In `capRows` (around line 144-146), combine the self-check with `personaMatches`:

```ts
    if (searching) {
      return (rowMatches(self, query) && personaMatches(self, personaSlug)) || kids.length > 0 ? [self, ...kids] : []
    }
```

And in the product-level self-check at the bottom (around line 179-181):

```ts
  if (searching) {
    return (rowMatches(self, query) && personaMatches(self, personaSlug)) || kept.length > 0 ? [self, ...kept] : []
  }
```

Leave `initiativeRows`' own self-check (`rowMatches(self, query) || shown.length > 0`, around line 115) and the pseudo-row `kept` filter (around line 166-168) untouched — initiatives and pseudo rows carry no persona of their own, so the persona filter only ever gates them indirectly, through whether the capability branch they live under survives.

Update `flattenMap` (bottom of file) to accept and forward the new parameter, and export `personaSlugsInMap`:

```ts
export function flattenMap(map: RoadmapMap, state: TreeState, query: string, personaSlug: string | null = null): TreeRow[] {
  return map.products.flatMap((p) => flattenProduct(p, state, query, personaSlug))
}

/** Every distinct persona slug declared anywhere in the map, sorted — the
 *  filter dropdown's option list (steiner385/viberpit#2446). Derived from the
 *  map already on hand rather than a second per-product fetch: a capability's
 *  resolved personaSlugs are already threaded onto every node (Task 1). */
export function personaSlugsInMap(map: RoadmapMap): string[] {
  const seen = new Set<string>()
  const walk = (c: CapabilityNode) => { c.personaSlugs.forEach((s) => seen.add(s)); c.children.forEach(walk) }
  map.products.forEach((p) => p.capabilities.forEach(walk))
  return [...seen].sort()
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd web && npm run test -- treeRows.test.ts`

Expected: PASS, including every pre-existing test in the file (the 3-arg call sites must still compile and behave identically — `personaSlug` defaults to `null`).

- [ ] **Step 7: Write the failing UI test**

Add to `web/src/features/roadmap/RoadmapListColumn.test.tsx`, inside the `describe('RoadmapListColumn', ...)` block:

```ts
  it('a persona filter narrows the tree to that persona\'s capabilities', () => {
    mount()
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'billing')}`)).toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: 'Persona' }), { target: { value: 'teen' } })
    expect(screen.queryByTestId(`row-${capabilityKey('kindash', 'billing')}`)).toBeNull()
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeInTheDocument()
  })

  it('clearing the persona filter (back to "All personas") restores the full tree', () => {
    mount()
    const select = screen.getByRole('combobox', { name: 'Persona' })
    fireEvent.change(select, { target: { value: 'teen' } })
    fireEvent.change(select, { target: { value: '' } })
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'billing')}`)).toBeInTheDocument()
  })
```

- [ ] **Step 8: Run it to verify it fails**

Run: `cd web && npm run test -- RoadmapListColumn.test.tsx`

Expected: FAIL — no element with role `combobox` and accessible name `Persona`.

- [ ] **Step 9: Add the dropdown and wire it into `MapScopeBody`**

In `web/src/features/roadmap/RoadmapListColumn.tsx`, add an import for `listControlClass` and the new `personaSlugsInMap` export:

```ts
import { listControlClass } from '@/ui/list/listControlStyles'
```

and extend the existing `treeRows` import:

```ts
import { flattenMap, initiativeKey, personaSlugsInMap, setCollapsed, toggleKey, type TreeRow, type TreeState } from './treeRows'
```

Rewrite `MapScopeBody` (lines 134-207):

```tsx
function MapScopeBody({ map, isLoading, selectedKey, onOpen, onOpenInbox }: MapScopeBodyProps) {
  const [tree, setTree] = useState<TreeState>(() => ({ collapsed: loadGroupCollapseNs(COLLAPSE_NS) }))
  const [query, setQuery] = useState('')
  const [personaSlug, setPersonaSlug] = useState<string | null>(null)
  useEffect(() => {
    saveGroupCollapseNs(COLLAPSE_NS, new Set(tree.collapsed))
  }, [tree])
  const personaOptions = useMemo(() => (map ? personaSlugsInMap(map) : []), [map])
  const rows = useMemo(
    () => (map ? flattenMap(map, tree, query, personaSlug) : []),
    [map, tree, query, personaSlug],
  )
  const collapse = useCallback(
    (key: string, collapsed: boolean) => setTree((t) => setCollapsed(t, key, collapsed)),
    [],
  )
  const { containerRef, rovingKey, onKeyDown } = useTreeKeys(rows, selectedKey, collapse)
  const filtering = query.trim() !== '' || personaSlug !== null

  return (
    <ScopeBody
      controls={
        <div className="flex items-center gap-3 flex-wrap">
          <ListSearchInput
            value={query}
            onChange={setQuery}
            placeholder={`Search ${ROADMAP.label.toLowerCase()}…`}
            ariaLabel={`Search ${ROADMAP.label.toLowerCase()}`}
          />
          {/* Only rendered once at least one capability declares a persona —
              an empty select would offer nothing but "All personas" forever. */}
          {personaOptions.length > 0 && (
            <label className="flex items-center gap-1 text-xs text-ink-muted">
              <span>Persona</span>
              <select
                aria-label="Persona"
                className={listControlClass}
                value={personaSlug ?? ''}
                onChange={(e) => setPersonaSlug(e.target.value === '' ? null : e.target.value)}
              >
                <option value="">All personas</option>
                {personaOptions.map((slug) => (
                  <option key={slug} value={slug}>{slug}</option>
                ))}
              </select>
            </label>
          )}
        </div>
      }
    >
      {isLoading && !map && (
        <div role="status" aria-label="Loading roadmap"><Skeleton lines={6} /></div>
      )}
      {map && rows.length === 0 && (
        <EmptyState title={filtering ? 'No matches' : PRODUCT.empty} />
      )}
      {/* A real tree: one tab stop, arrows to walk and fold it (`useTreeKeys`).
          The handler hangs off the container, so every row's keys are handled in
          one place and only while focus is inside the tree. */}
      {rows.length > 0 && (
        <div
          ref={containerRef}
          role="tree"
          aria-label={ROADMAP.map}
          onKeyDown={onKeyDown}
          className="flex flex-col gap-1"
        >
          {rows.map((row) => (row.kind === 'initiative' ? (
            <InitiativeCard
              key={row.key}
              rowKey={row.key}
              idScope={TREE_ID_SCOPE}
              depth={row.depth}
              initiative={row.initiative}
              selected={selectedKey === row.key}
              focusable={rovingKey === row.key}
              onOpen={() => onOpen(row)}
              expanded={row.expanded}
              hasChildren={row.hasChildren}
              // Same fold state a capability row's chevron and `useTreeKeys`'s
              // ArrowLeft/ArrowRight both toggle through.
              onToggle={() => setTree((t) => toggleKey(t, row.key))}
            />
          ) : (
            <TreeRowCard
              key={row.key}
              row={row}
              idScope={TREE_ID_SCOPE}
              selected={selectedKey === row.key}
              focusable={rovingKey === row.key}
              onOpen={onOpen}
              onToggle={(k) => setTree((t) => toggleKey(t, k))}
              onOpenInbox={onOpenInbox}
            />
          )))}
        </div>
      )}
    </ScopeBody>
  )
}
```

(Only the `EmptyState`'s `title` condition and the two new pieces of state + the controls JSX actually changed; the rest of the function body is reproduced verbatim above so the diff is unambiguous.)

- [ ] **Step 10: Run the UI tests to verify they pass**

Run: `cd web && npm run test -- RoadmapListColumn.test.tsx treeRows.test.ts`

Expected: PASS, including every pre-existing test in both files.

- [ ] **Step 11: Run the full web check**

Run: `cd web && npm run typecheck && npm run lint && npm run test`

Expected: all green. Run the **full** `npm run test`, not a changed-files subset — per this repo's own convention, ~47 web test files manually mock `@/api/hooks`, so a change reachable from a shared component can break a file this task never touched.

- [ ] **Step 12: Commit**

```bash
git add web/src/api/roadmap.ts web/src/features/roadmap/__fixtures__/map.ts \
  web/src/features/roadmap/treeRows.ts web/src/features/roadmap/treeRows.test.ts \
  web/src/features/roadmap/RoadmapListColumn.tsx web/src/features/roadmap/RoadmapListColumn.test.tsx
git commit -m "$(cat <<'EOF'
feat(roadmap): add a persona filter chip to the capability tree

Adds a Persona <select> to the Map scope's controls strip, built from the
map already on hand (personaSlugsInMap) rather than a second fetch, and
extends flattenMap's existing "self OR any descendant matches" filter
recursion with an AND'd persona facet.

Fixes #2446

Co-Authored-By: Claude <your model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Inject linked personas' docs into the SDLC spec-generation prompt

**Files:**
- Modify: `server/sdlc/prompts.py:137-187` (`build_spec_prompt`)
- Modify: `server/sessions/orchestrator.py:11129-11220` (`_sdlc_spawn_synth`; new helper method)
- Test: `server/tests/test_sdlc_prompts.py`
- Test: Create `server/tests/test_sdlc_persona_injection.py`

**Interfaces:**
- Consumes: `PersonaFeature`/`Persona` models (`server/db/models.py:1893-1940`, already landed), `InitiativeFeature` (`server/db/models.py:2280-2296`, already landed), `SessionOrchestrator._sdlc_show_committed_file(cwd, ref, path) -> str` (existing, line 11730-11746), `_SDLC_UNREADABLE_PREFIX` (existing module constant, line 528-532 area).
- Produces: `SessionOrchestrator._sdlc_capability_personas(initiative_id: str | None) -> list[tuple[str, str]] | None` (slug, repo_path pairs); `build_spec_prompt(..., personas: list[tuple[str, str]] | None = None)`.

- [ ] **Step 1: Write the failing prompt-builder tests**

Add to `server/tests/test_sdlc_prompts.py` (near the other `spec`-prefixed tests, after `test_spec_with_no_skills_is_still_valid`):

```python
PERSONAS = [("parent", "# Parent persona\n\nRole: primary caregiver.\n")]


def test_spec_embeds_persona_docs():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG, personas=PERSONAS)
    assert "PERSONA `parent`" in p
    assert "# Parent persona" in p
    assert "Role: primary caregiver." in p


def test_spec_asks_stories_to_name_their_persona():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG, personas=PERSONAS).lower()
    assert "must name which persona" in p


def test_spec_with_no_personas_omits_the_persona_section():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG)
    assert "PERSONA `" not in p
    assert "must name which persona" not in p.lower()


def test_spec_with_multiple_personas_embeds_each_one():
    two = [("parent", "Parent doc body."), ("teen", "Teen doc body.")]
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG, personas=two)
    assert "PERSONA `parent`" in p and "Parent doc body." in p
    assert "PERSONA `teen`" in p and "Teen doc body." in p
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_sdlc_prompts.py -k persona -v`

Expected: FAIL — `build_spec_prompt() got an unexpected keyword argument 'personas'`.

- [ ] **Step 3: Add the `personas` parameter and injection block to `build_spec_prompt`**

In `server/sdlc/prompts.py`, replace `build_spec_prompt` (lines 137-187) with:

```python
def build_spec_prompt(
    intent_md: str,
    skills: list[str],
    *,
    slug: str,
    repo: str = "",
    default_branch: str = "master",
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
    constitution: str | None = None,
    constitution_path: str = ".specify/memory/constitution.md",
    personas: list[tuple[str, str]] | None = None,
) -> str:
    """Seed prompt for the Synthesizer (Design): ``intent.md`` → ``spec.md``.

    The org ``skills`` are injected as binding constraints, and the session must
    explicitly flag any policy concerns as spec constraints. When no org skills
    are supplied, *constitution* — the raw committed text of *constitution_path*,
    or None when it could not be read — decides the fallback: a ratified
    constitution's real path and principle headings, or an explicit
    governance-gap instruction (issue #2165).

    *personas* is ``[(slug, doc_body), ...]`` for every live persona linked to
    this initiative's primary capability (issue #2447), each injected as its
    own read-only block mirroring the UPSTREAM INTENT convention above it —
    or ``None``/empty when the initiative has no primary capability or that
    capability links no persona, in which case the section is omitted
    entirely rather than rendered empty.
    """
    if skills:
        skills_block = "\n".join(f"- {name}" for name in skills)
    else:
        skills_block = _constitution_block(constitution, constitution_path)
    if personas:
        persona_blocks = "\n\n".join(
            f"PERSONA `{pslug}` (read-only — do not edit docs/product/personas/{pslug}.md):\n"
            f"-----8<----- docs/product/personas/{pslug}.md -----8<-----\n"
            f"{body}\n"
            "-----8<--------------------------------------8<-----"
            for pslug, body in personas
        )
        persona_section = (
            f"{persona_blocks}\n\n"
            "Every `US-N` user story and `AC-NNN` acceptance criterion in the "
            "`## Requirements` section below must name which persona above it "
            "serves, e.g. `US-1: ... (persona: <slug>)`. If a requirement serves "
            "none of the personas above, say so explicitly rather than guessing.\n\n"
        )
    else:
        persona_section = ""
    return (
        "You are running the **Design** stage of an AI-native SDLC loop. Turn the "
        "approved intent below into a specification.\n\n"
        "UPSTREAM INTENT (read-only — do not edit intent.md):\n"
        "-----8<----- intent.md -----8<-----\n"
        f"{intent_md}\n"
        "-----8<----------------------8<-----\n\n"
        + persona_section +
        "ORG SKILLS — treat each as a binding constraint the spec must satisfy:\n"
        f"{skills_block}\n\n"
        f"Produce `{_artifact_path(slug, 'spec')}` covering: functional behavior, "
        "acceptance criteria, data/API surface, and non-goals. Explicitly **flag "
        "any policy concerns** (security, governance, ToS, data handling) as called-"
        "out spec constraints so a human can resolve them at the gate.\n\n"
        "Give every functional requirement and acceptance criterion a stable ID — "
        "it is the identity that must survive from this spec through the plan, the "
        "materialized issues, and the PRs that close them, so never renumber or "
        "reuse one once written. Use a `## Requirements` section shaped like:\n"
        "```\n"
        "- FR-001: <functional requirement, in one sentence>\n"
        "- AC-001: <acceptance criterion, covers FR-001>\n"
        "- US-1: <user story this requirement serves>\n"
        "```\n"
        "(`FR-NNN` for a functional requirement, `AC-NNN` for an acceptance "
        "criterion, `US-N` for a user story — zero-padded, sequential within "
        "this spec.)\n\n"
        + _common_instructions(slug, "spec", repo, default_branch, engine)
    )
```

- [ ] **Step 4: Run the prompt tests to verify they pass**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_sdlc_prompts.py -v` (whole file).

Expected: PASS, all tests in the file green (confirms the new optional kwarg didn't disturb any existing call site in this file).

- [ ] **Step 5: Write the failing orchestrator-level tests**

Create `server/tests/test_sdlc_persona_injection.py`:

```python
"""A spec-stage synth spawn injects its initiative's PRIMARY capability's
linked personas' docs into the seed prompt (#2447) — `PersonaFeature`
(#2164/#2453) resolved onto `InitiativeFeature.is_primary`, each doc read off
the committed default branch the same way intent.md/spec.md/the constitution
already are (`_sdlc_show_committed_file`).
"""
from __future__ import annotations

import asyncio

from server.sessions.engine import AgentEngine


def _bare_orchestrator():
    from server.sessions import orchestrator as orch_mod
    return orch_mod, orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)


async def _seed_capability_with_personas() -> str:
    """One project, one Feature (capability), a live persona and an orphaned
    persona both linked to it via PersonaFeature, one Initiative whose
    InitiativeFeature marks that capability primary. Returns initiative_id."""
    from server.db import db_session
    from server.db.models import (
        Feature, Initiative, InitiativeFeature, Persona, PersonaFeature, Project)

    async with db_session() as db:
        project = Project(slug="kindash", name="KinDash")
        db.add(project)
        await db.flush()
        feature = Feature(project_id=project.id, slug="task-views", title="Task views",
                          state="live")
        db.add(feature)
        await db.flush()
        persona = Persona(project_id=project.id, slug="parent", title="Parent",
                          repo_path="docs/product/personas/parent.md")
        orphan_persona = Persona(project_id=project.id, slug="ghost", title="Ghost",
                                 repo_path="docs/product/personas/ghost.md", orphaned=True)
        db.add_all([persona, orphan_persona])
        await db.flush()
        db.add(PersonaFeature(persona_id=persona.id, feature_id=feature.id))
        db.add(PersonaFeature(persona_id=orphan_persona.id, feature_id=feature.id))
        ini = Initiative(slug="views", repo="o/r", title="Task views", stage="design",
                         status="active", project_id=project.id)
        db.add(ini)
        await db.flush()
        db.add(InitiativeFeature(initiative_id=ini.id, feature_id=feature.id, is_primary=True))
        await db.commit()
        return ini.id


def test_capability_personas_resolves_the_primary_capabilitys_live_personas_only(isolated_db):
    ini_id = asyncio.run(_seed_capability_with_personas())
    _orch_mod, orch = _bare_orchestrator()

    result = asyncio.run(orch._sdlc_capability_personas(ini_id))

    assert result == [("parent", "docs/product/personas/parent.md")]


def test_capability_personas_is_none_without_a_primary_capability(isolated_db):
    from server.db import db_session
    from server.db.models import Initiative, Project

    async def _seed() -> str:
        async with db_session() as db:
            project = Project(slug="kindash", name="KinDash")
            db.add(project)
            await db.flush()
            ini = Initiative(slug="orphan", repo="o/r", title="Orphan", stage="design",
                             status="active", project_id=project.id)
            db.add(ini)
            await db.commit()
            return ini.id

    ini_id = asyncio.run(_seed())
    _orch_mod, orch = _bare_orchestrator()

    assert asyncio.run(orch._sdlc_capability_personas(ini_id)) is None


def test_capability_personas_is_none_without_an_initiative_id():
    _orch_mod, orch = _bare_orchestrator()
    assert asyncio.run(orch._sdlc_capability_personas(None)) is None


def _monkeypatch_spawn_seams(monkeypatch, orch, orch_mod, *, show_committed_file):
    class Ctx:
        async def launch_plan_or_refuse(self, _origin_ref):
            return AgentEngine.CLAUDE_CODE, {}

        async def spawn_session(self, **kwargs):
            captured["spawn"] = kwargs
            return type("Rec", (), {"id": "sess-1"})()

    captured: dict = {}

    async def probe(_ctx, _origin_ref, *, engine=None):
        return "acct-1"

    async def read_config():
        return "auto", {}

    async def repo_root():
        return "/tmp"

    async def default_branch(_repo):
        return "master"

    monkeypatch.setattr(orch, "_sdlc_synth_ctx", Ctx(), raising=False)
    monkeypatch.setattr(orch, "_automation_account_for", probe, raising=False)
    monkeypatch.setattr(orch, "_sdlc_synth_read_config", read_config, raising=False)
    monkeypatch.setattr(orch, "_effective_repo_root", repo_root, raising=False)
    monkeypatch.setattr(orch, "_sdlc_default_branch", default_branch, raising=False)
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone", lambda *a, **k: "/tmp/clone")
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_sdlc_show_committed_file",
                        staticmethod(show_committed_file))
    return captured


def test_spec_spawn_injects_the_primary_capabilitys_persona_docs(monkeypatch, isolated_db):
    ini_id = asyncio.run(_seed_capability_with_personas())
    orch_mod, orch = _bare_orchestrator()

    def show_committed_file(cwd, ref, path):
        if path == "docs/sdlc/views/intent.md":
            return "# Intent\n\nMake task views usable.\n"
        if path == ".specify/memory/constitution.md":
            return "# Constitution"
        if path == "docs/product/personas/parent.md":
            return "# Parent\n\nRole: primary caregiver.\n"
        raise AssertionError(f"unexpected committed-file read: {path}")

    captured = _monkeypatch_spawn_seams(monkeypatch, orch, orch_mod,
                                        show_committed_file=show_committed_file)

    ini = {"id": ini_id, "slug": "views", "repo": "o/r", "title": "Task views",
          "origin": "manual"}
    rec = asyncio.run(orch._sdlc_spawn_synth(ini, "spec"))

    assert rec is not None
    prompt = captured["spawn"]["initial_prompt"]
    assert "PERSONA `parent`" in prompt
    assert "Role: primary caregiver." in prompt
    assert "must name which persona" in prompt.lower()


def test_spec_spawn_without_a_primary_capability_omits_the_persona_section(
        monkeypatch, isolated_db):
    """An initiative with no InitiativeFeature row at all (never linked to a
    capability) must spawn exactly as it did before this feature existed."""
    orch_mod, orch = _bare_orchestrator()

    def show_committed_file(cwd, ref, path):
        if path == "docs/sdlc/views/intent.md":
            return "# Intent\n\nMake task views usable.\n"
        if path == ".specify/memory/constitution.md":
            return "# Constitution"
        raise AssertionError(f"unexpected committed-file read: {path}")

    captured = _monkeypatch_spawn_seams(monkeypatch, orch, orch_mod,
                                        show_committed_file=show_committed_file)

    ini = {"id": "ini-no-capability", "slug": "views", "repo": "o/r", "title": "Task views",
          "origin": "manual"}
    rec = asyncio.run(orch._sdlc_spawn_synth(ini, "spec"))

    assert rec is not None
    prompt = captured["spawn"]["initial_prompt"]
    assert "PERSONA `" not in prompt


def test_plan_spawn_never_resolves_or_reads_personas(monkeypatch, isolated_db):
    """The plan stage (spec.md -> plan.md) has no persona injection (issue
    scope: spec prompt only) — a plan spawn must not even ask which capability
    is primary, let alone read a persona doc off git."""
    orch_mod, orch = _bare_orchestrator()

    def show_committed_file(cwd, ref, path):
        if path == "docs/sdlc/views/spec.md":
            return "# Spec\n\nDone.\n"
        raise AssertionError(f"unexpected committed-file read for a plan spawn: {path}")

    captured = _monkeypatch_spawn_seams(monkeypatch, orch, orch_mod,
                                        show_committed_file=show_committed_file)

    async def personas_should_not_be_called(_initiative_id):
        raise AssertionError("plan spawn must not resolve capability personas")

    monkeypatch.setattr(orch, "_sdlc_capability_personas", personas_should_not_be_called,
                        raising=False)

    ini = {"id": "ini-1", "slug": "views", "repo": "o/r", "title": "Task views"}
    rec = asyncio.run(orch._sdlc_spawn_synth(ini, "plan"))

    assert rec is not None
    assert "PERSONA `" not in captured["spawn"]["initial_prompt"]
```

- [ ] **Step 6: Run them to verify they fail**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_sdlc_persona_injection.py -v`

Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute '_sdlc_capability_personas'` on every test.

- [ ] **Step 7: Add `_sdlc_capability_personas` and wire it into `_sdlc_spawn_synth`**

In `server/sessions/orchestrator.py`, add a new method directly after `_sdlc_epic_upstream_text` (which ends at line 11254, right before `_sdlc_spawn_materialize` at line 11256):

```python
    async def _sdlc_capability_personas(
            self, initiative_id: str | None,
    ) -> list[tuple[str, str]] | None:
        """(slug, repo_path) for every LIVE persona `PersonaFeature` resolved
        onto *initiative_id*'s PRIMARY capability (#2447) — or ``None`` when
        the initiative has no primary capability (`InitiativeFeature.is_primary`,
        `server/api/sdlc.py`'s existing link, not a new column), same
        optional-pointer posture as `_sdlc_epic_upstream_text`'s epic pointer.
        The caller reads each doc body off git; this only resolves WHICH docs."""
        if not initiative_id:
            return None
        from sqlalchemy import select as sa_select
        from server.db.models import InitiativeFeature, Persona, PersonaFeature
        async with db_session() as db:
            primary_id = (await db.execute(sa_select(InitiativeFeature.feature_id).where(
                InitiativeFeature.initiative_id == initiative_id,
                InitiativeFeature.is_primary.is_(True),
            ))).scalar_one_or_none()
            if primary_id is None:
                return None
            rows = (await db.execute(sa_select(Persona).join(
                PersonaFeature, PersonaFeature.persona_id == Persona.id,
            ).where(
                PersonaFeature.feature_id == primary_id, Persona.orphaned.isnot(True),
            ))).scalars().all()
        docs = [(p.slug, p.repo_path) for p in rows if p.slug and p.repo_path]
        return docs or None
```

Then in `_sdlc_spawn_synth` (lines 11129-11220), add the resolution call right after the existing `epic_upstream` block (after line 11174) and thread the result through `build()`:

```python
        epic_upstream: tuple[str, str] | None = None
        if kind == "spec" and ini.get("origin") == "epic-adoption":
            epic_upstream = await self._sdlc_epic_upstream_text(
                ini.get("epic_repo"), ini.get("epic_number"))
        # Personas (#2447): resolved for every spec spawn, not only
        # epic-adoption ones — unlike the epic-body substitute, this augments
        # a normal intent.md read rather than replacing it.
        persona_docs: list[tuple[str, str]] | None = None
        if kind == "spec":
            persona_docs = await self._sdlc_capability_personas(ini.get("id"))

        def build(cwd: str, default_branch: str) -> str:
            if epic_upstream is not None:
                content = epic_upstream[0]
            else:
                content = self._sdlc_show_committed_file(
                    cwd, f"origin/{default_branch}", upstream)
            if kind == "spec":
                const_path = ".specify/memory/constitution.md"
                raw_const_text = self._sdlc_show_committed_file(
                    cwd, f"origin/{default_branch}", const_path)
                const_text = (None if raw_const_text.startswith(_SDLC_UNREADABLE_PREFIX)
                             else raw_const_text)
                persona_texts: list[tuple[str, str]] = []
                for pslug, repo_path in (persona_docs or []):
                    body = self._sdlc_show_committed_file(
                        cwd, f"origin/{default_branch}", repo_path)
                    if not body.startswith(_SDLC_UNREADABLE_PREFIX):
                        persona_texts.append((pslug, body))
                return build_spec_prompt(
                    content, [], slug=slug, repo=repo, default_branch=default_branch,
                    engine=engine, constitution=const_text, constitution_path=const_path,
                    personas=persona_texts or None)
            return build_plan_prompt(
                content, slug=slug, repo=repo, default_branch=default_branch,
                engine=engine)
```

(Only the two new blocks — the `persona_docs` resolution before `build()`, and the `persona_texts` loop plus the `personas=` kwarg inside `build()`'s `kind == "spec"` branch — actually change; everything else in the function is unchanged.)

- [ ] **Step 8: Run the tests to verify they pass**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_sdlc_persona_injection.py server/tests/test_sdlc_epic_upstream.py server/tests/test_sdlc_prompts.py -v`

Expected: PASS — including every pre-existing test in `test_sdlc_epic_upstream.py` (confirms the persona resolution never interferes with the epic-adoption substitution path) and `test_sdlc_codex_acceptance.py`/`test_sdlc_codex_live_artifact.py` if they also call `build_spec_prompt` (run those too):

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_sdlc_codex_acceptance.py server/tests/test_sdlc_codex_live_artifact.py -v`

Expected: PASS (the new `personas` kwarg is optional, so these engine-parametrized/live-artifact tests need no changes).

- [ ] **Step 9: Run ruff and mypy on the touched files**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m ruff check server/sdlc/prompts.py server/sessions/orchestrator.py server/tests/test_sdlc_prompts.py server/tests/test_sdlc_persona_injection.py && /home/tony/GitHub/viberpit/.venv/bin/python -m mypy server/sdlc/prompts.py server/sessions/orchestrator.py --ignore-missing-imports`

Expected: no errors from either.

- [ ] **Step 10: Commit**

```bash
git add server/sdlc/prompts.py server/sessions/orchestrator.py server/tests/test_sdlc_prompts.py server/tests/test_sdlc_persona_injection.py
git commit -m "$(cat <<'EOF'
feat(sdlc): inject the primary capability's persona docs into the spec prompt

_sdlc_capability_personas resolves InitiativeFeature.is_primary ->
PersonaFeature -> live Persona (an existing link, not a new column), and
_sdlc_spawn_synth reads each doc body off the committed default branch the
same way intent.md/the constitution already are, mirroring the UPSTREAM
INTENT read-only-block convention. Scoped to the spec stage only; the plan
stage never resolves or reads personas.

Fixes #2447

Co-Authored-By: Claude <your model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Open the PR

**Files:** none (git/gh only).

- [ ] **Step 1: Push the branch**

```bash
git push -u origin HEAD
```

- [ ] **Step 2: Open the PR, ready for review, covering both issues**

```bash
gh pr create --repo steiner385/viberpit --title "feat(pm,sdlc): persona filter chip + spec-prompt injection (#2164 follow-ups)" --body-file <(cat <<'EOF'
## Summary
- Threads resolved `PersonaFeature` edges onto the `/pm/map` capability tree (`personaSlugs`) and adds a persona filter `<select>` to the roadmap Map scope's controls strip, following the tree's existing "self OR any descendant matches" filter recursion.
- Resolves an SDLC initiative's primary capability (`InitiativeFeature.is_primary`, already-landed link) and injects that capability's linked personas' docs into the spec-generation prompt as read-only blocks, mirroring the existing UPSTREAM INTENT convention; asks every user story / acceptance criterion to name its persona.

Fixes #2446
Fixes #2447

## Test plan
- [ ] `pytest server/tests/test_pm_tree.py server/tests/test_pm_map_api.py -v`
- [ ] `pytest server/tests/test_sdlc_prompts.py server/tests/test_sdlc_persona_injection.py server/tests/test_sdlc_epic_upstream.py -v`
- [ ] `cd web && npm run typecheck && npm run lint && npm run test && npm run build`
- [ ] `ruff check src/ server/ && mypy server/ --ignore-missing-imports`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)
```

- [ ] **Step 3: Mark ready and arm auto-merge**

```bash
gh pr ready <PR#> --repo steiner385/viberpit
gh pr merge <PR#> --repo steiner385/viberpit --squash --auto
```
