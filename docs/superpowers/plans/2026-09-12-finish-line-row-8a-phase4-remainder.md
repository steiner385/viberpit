# Finish-line row 8a — Roadmap Phase 4 remainder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish roadmap drill-down Phase 4 — the inline edits the spec puts on an initiative and on an issue leaf, the product-dependency chip editor, and a virtualized leaf list — so that after this PR the only Phase 4/5 work left for row 8 (#1472) is the map-changes PR subsystem (§9, plan 8b).

**Architecture:** One new write endpoint (`PATCH /api/sdlc/initiatives/{slug}`) that shares the capability-resolution rules with `create_initiative`; everything else on the server already exists (`PATCH /work-items/{id}/overlay`, `POST`/`DELETE /projects/{slug}/edges`) and gains its first web callers. On the web, each edit is a small `Dialog` opened from the row or header it edits, mutating through a react-query hook that invalidates the map, the leaves and the initiative detail, in the optimistic-with-toast idiom the triage hooks use. `LeafList` becomes a single virtualized row model (section headers and leaves in one array) scrolled by the page's `PageContent`.

**Tech Stack:** FastAPI + SQLAlchemy async (server), React 19 + react-query + `@tanstack/react-virtual` + Tailwind v4 design tokens (web), vitest + testing-library, pytest.

**Spec:** `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` — §6 (list column behaviour: lazy, virtualized leaves), §7, §8 (inline editing table: rows "Create initiative; set primary and touched capabilities", "Reassign an issue's capability", "Edit scores", "Product dependency"), §10 (`PATCH /api/sdlc/initiatives/{slug}`, overlay PATCH "gains UI for `featureId`", edges create/delete), §13 Phase 4, §14 testing. Row 8 of `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md`. What #1583 already landed (blocked disposition, step-back, unblock, dependency remove) is NOT repeated here; §9 map-changes PRs and "inline capability editing" of the capability DOCUMENT are plan 8b.

## Global Constraints

- Every mutation records an audit row with the acting identity (`record_audit`); every write is optimistic with rollback and toast, as the triage hooks do (spec §8 last paragraph). The existing hooks use `meta: PM_MUTATION_META` and `extractMutationErrorMessage` — copy that shape.
- Design tokens only: no raw palette, no `dark:` variant, no numeric `z-`, no `min-h-[44px]` — `ccswitch/no-raw-palette` covers the whole tree (`npm run lint` fails otherwise). A primary confirm in a dialog footer is `<Button size="lg">` (the one 44 px size); a dialog is named by `title` alone.
- Vocabulary comes from `web/src/features/products/vocab.ts` (`CAPABILITY`, `INITIATIVE`, `ISSUE`, `PRODUCT`) — never a hard-coded "capability"/"initiative" in copy.
- Score bounds live in `server/pm/score.py` (`RICE_MIN`, `RICE_MAX`, `validate_rice`); the web mirrors the formula in `web/src/features/roadmap/inbox/rice.ts` and the input parsing in `inboxKeys.ts:scoreInputValue`. Never inline a second copy of either.
- New columns are nullable; this plan adds none. New model? None (so no Postgres suite / migration).
- Commits: one per task, message `<type>(<scope>): 8a task N — <what>` / blank / one paragraph / blank / `Refs #1472` / blank / `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Commit via `git commit -F /tmp/row8a-tN.msg`. Never push, never open a PR, never rebase (the controller does).
- Web gate, every exit code checked, never piped through `tail`: `(cd web && npm run typecheck; echo rc=$?; npm run lint; echo rc=$?; npm run test > /tmp/8a-tN-vitest.log 2>&1; echo rc=$?; grep -E "Test Files|Tests " /tmp/8a-tN-vitest.log; grep -c Unhandled /tmp/8a-tN-vitest.log; npm run build > /tmp/8a-tN-build.log 2>&1; echo rc=$?); rm -rf web/dist` — all four rc=0, Unhandled 0.
- Server gate (tasks that touch `server/`): `/home/tony/GitHub/viberpit/.venv/bin/ruff check src/ server/`, `/home/tony/GitHub/viberpit/.venv/bin/python -m mypy server/ --ignore-missing-imports`, and the named test files via `env -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest <files> -q`. The full `pytest server/tests/ -n auto` runs once, on the final head, by the controller.
- Run from the worktree root with absolute paths or `(cd web && …)`; a bare `cd` breaks the repo's Bash hook.

## File structure

| File | Responsibility |
|---|---|
| `server/api/sdlc.py` | `_resolve_capabilities` (shared by create + patch), `InitiativePatchBody`, `update_initiative` |
| `server/api/pm_tree.py` | `_leaf` gains `featureId` (the capability a leaf sits under, needed to seed the reassign dialog) |
| `web/src/api/sdlc.ts` | `UpdateInitiativeInput`, `updateInitiative` |
| `web/src/api/pm.ts` | `createEdge`, `deleteEdge`, `CreateEdgeInput` |
| `web/src/api/roadmap.ts` | `Leaf.featureId` |
| `web/src/api/hooks.ts` | `useUpdateInitiative`, `useCreateEdge`, `useDeleteEdge`; `useUpdateOverlay` invalidates leaves + map |
| `web/src/features/roadmap/inbox/ScoreFields.tsx` | the four RICE inputs, extracted from `ProposalDetail` so the leaf editor reuses them |
| `web/src/features/roadmap/detail/EditInitiativeDialog.tsx` | title + primary + touched capabilities editor |
| `web/src/features/roadmap/detail/LeafEditors.tsx` | `LeafScoreButton` (score dialog) and `LeafCapabilityChip` (reassign dialog) |
| `web/src/features/roadmap/detail/ProductEdgesEditor.tsx` | product-to-product dependency chips + add form |
| `web/src/features/roadmap/detail/leafRows.ts` | pure row model for the virtualized leaf list |
| `web/src/features/roadmap/detail/LeafList.tsx` | virtualized rendering; mounts the two leaf editors |
| `web/src/ui/PageContent.tsx` | exposes the page scroll element as `data-page-scroll` |
| `web/src/features/roadmap/InitiativePage.tsx`, `detail/ProductDetail.tsx`, `detail/CapabilityDetail.tsx` | mount points |

Task order is by dependency: 1 → 2 (initiative PATCH then its UI); 3 (leaf editors, adds `featureId`) → 5 (virtualization rewrites `LeafList`'s render loop — after 3 so the editors are already mounted rows); 4 (edges) is independent and may run any time after 1.

---

### Task 1: `PATCH /api/sdlc/initiatives/{slug}` — title, primary and touched capabilities

**Files:**
- Modify: `server/api/sdlc.py:224-335` (the create body + `create_initiative`), add the patch body + route after `step_back_initiative` (`:476`)
- Test: `server/tests/test_sdlc_api.py` (append after `test_initiative_without_capabilities_has_null_fields`, `:601`)

**Interfaces:**
- Consumes: `_load_initiative(db, slug)` (`sdlc.py:443`), `_initiative_to_dict`, `_decorate(db, [ini], [d])` (`:99`), `record_audit`, models `Initiative`, `InitiativeFeature`, `Feature`.
- Produces: `PATCH /api/sdlc/initiatives/{slug}` with body `{title?: str, primaryCapabilityId?: str, capabilityIds?: str[]}` → the same decorated initiative dict `GET /initiatives/{slug}` returns (`projectSlug`, `primaryCapability`, `capabilities[]` included). Errors: 404 `not_found`; 422 `bad_title` (blank after strip); 422 `capability_not_found`; 422 `capabilities_span_products`. Empty body → read-through (200, no audit). Audit type `sdlc.initiative.update`, detail `{"slug", "fields": [sorted keys given]}`.
- Semantics: `capabilityIds` REPLACES the touched set; `primaryCapabilityId` is always added to the set (as create does); `primaryCapabilityId` given alone re-primaries within the CURRENT set; `capabilityIds` given alone keeps the current primary when it is still in the new set, otherwise the initiative has no primary. `project_id` is re-derived from the primary (or the first capability), `None` when the set empties.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_sdlc_api.py` (the file already has `_client`, `_seed_initiative`, `_audits`, `IDENTITY_HEADERS`, `asyncio`, `tempfile`, `os`, `patch`):

```python
def _seed_product_with_two_capabilities(db_url: str) -> tuple[str, str, str]:
    """A product with a root capability and one child under it; returns
    (project_id, root_id, child_id). Seeded BEFORE the client opens, like
    ``test_create_with_capabilities_links_and_derives_product``."""
    from server.db import db_session, init_db
    from server.db.models import Feature, Project

    async def _go():
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="example-product", name="Example product")
            db.add(p)
            await db.flush()
            root = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
            db.add(root)
            await db.flush()
            child = Feature(project_id=p.id, slug="dictation", title="Dictation",
                            state="delivering", parent_feature_id=root.id)
            db.add(child)
            await db.commit()
            return p.id, root.id, child.id
    return asyncio.run(_go())


def test_patch_title_trims_and_audits():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url)
        with _client(db_url) as c:
            r = c.patch("/api/sdlc/initiatives/dark-mode", json={"title": "  Dark mode v2  "},
                        headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert r.json()["title"] == "Dark mode v2"
            assert r.json()["stage"] == "plan"            # untouched
            assert _audits(c, "sdlc.initiative.update") == [{"slug": "dark-mode", "fields": ["title"]}]
            assert c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()["title"] == "Dark mode v2"


def test_patch_empty_body_is_a_read_through_and_blank_title_is_422():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url)
        with _client(db_url) as c:
            r = c.patch("/api/sdlc/initiatives/dark-mode", json={}, headers=IDENTITY_HEADERS)
            assert r.status_code == 200 and r.json()["title"] == "Dark mode"
            assert _audits(c, "sdlc.initiative.update") == []
            r = c.patch("/api/sdlc/initiatives/dark-mode", json={"title": "   "}, headers=IDENTITY_HEADERS)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "bad_title"
            assert c.patch("/api/sdlc/initiatives/nope", json={"title": "x"},
                           headers=IDENTITY_HEADERS).status_code == 404


def test_patch_capabilities_replaces_the_set_and_rederives_the_product():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _, root_id, child_id = _seed_product_with_two_capabilities(db_url)
        with _client(db_url) as c:
            created = c.post("/api/sdlc/initiatives", json={
                "title": "Whisper", "repo": "o/r", "capabilityIds": [root_id]},
                headers=IDENTITY_HEADERS)
            assert created.status_code == 201, created.text
            assert created.json()["primaryCapability"] is None
            r = c.patch("/api/sdlc/initiatives/whisper", json={
                "primaryCapabilityId": child_id, "capabilityIds": [child_id]},
                headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["projectSlug"] == "example-product"
            assert body["primaryCapability"]["slug"] == "dictation"
            assert [(x["slug"], x["isPrimary"]) for x in body["capabilities"]] == [("dictation", True)]
            assert _audits(c, "sdlc.initiative.update") == [
                {"slug": "whisper", "fields": ["capabilityIds", "primaryCapabilityId"]}]


def test_patch_primary_alone_joins_the_current_set_and_dropping_it_clears_it():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _, root_id, child_id = _seed_product_with_two_capabilities(db_url)
        with _client(db_url) as c:
            c.post("/api/sdlc/initiatives", json={
                "title": "Whisper", "repo": "o/r", "capabilityIds": [root_id]}, headers=IDENTITY_HEADERS)
            # primary alone: joins the current {root} set
            r = c.patch("/api/sdlc/initiatives/whisper", json={"primaryCapabilityId": child_id},
                        headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert sorted((x["slug"], x["isPrimary"]) for x in r.json()["capabilities"]) == [
                ("dictation", True), ("sessions", False)]
            # a new set that omits the primary: the primary is gone, the product follows the first
            r = c.patch("/api/sdlc/initiatives/whisper", json={"capabilityIds": [root_id]},
                        headers=IDENTITY_HEADERS)
            assert r.json()["primaryCapability"] is None
            assert [x["slug"] for x in r.json()["capabilities"]] == ["sessions"]
            assert r.json()["projectSlug"] == "example-product"
            # an empty set: no capabilities, no product
            r = c.patch("/api/sdlc/initiatives/whisper", json={"capabilityIds": []}, headers=IDENTITY_HEADERS)
            assert r.json()["capabilities"] == [] and r.json()["projectSlug"] is None


def test_patch_rejects_unknown_and_cross_product_capabilities():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _, root_id, _ = _seed_product_with_two_capabilities(db_url)
        with _client(db_url) as c:
            c.post("/api/sdlc/initiatives", json={"title": "Whisper", "repo": "o/r"}, headers=IDENTITY_HEADERS)
            r = c.patch("/api/sdlc/initiatives/whisper", json={"capabilityIds": ["nope"]}, headers=IDENTITY_HEADERS)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "capability_not_found"
            # a second product's capability, seeded through the client's own loop
            from server.db import db_session
            from server.db.models import Feature, Project

            async def _other():
                async with db_session() as db:
                    p = Project(slug="other", name="Other")
                    db.add(p)
                    await db.flush()
                    f = Feature(project_id=p.id, slug="billing", title="Billing", state="live")
                    db.add(f)
                    await db.commit()
                    return f.id
            other_id = c.portal.call(_other)
            r = c.patch("/api/sdlc/initiatives/whisper", json={"capabilityIds": [root_id, other_id]},
                        headers=IDENTITY_HEADERS)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "capabilities_span_products"
```

Read `_audits` (`test_sdlc_api.py:780`) before using `c.portal.call` — it is the same idiom (`TestClient.portal.call` runs a coroutine on the client's loop); copy exactly how `_audits` invokes it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_sdlc_api.py -q -k "patch_"`
Expected: 5 failed — every PATCH answers 405 (no route).

- [ ] **Step 3: Extract the capability-resolution rules from `create_initiative`**

In `server/api/sdlc.py`, add above `InitiativeCreateBody`:

```python
async def _resolve_capabilities(
    db, primary_id: str | None, capability_ids: list[str],
) -> tuple[list[str], dict[str, Feature]]:
    """The capability set an initiative may link: ``capability_ids`` plus the
    primary (which always belongs to the set), de-duplicated in order; every id
    must exist and all must sit under ONE product. Shared by create and PATCH so
    the two cannot drift (constitution VI). Returns ``(wanted_ids, features)``."""
    wanted = list(dict.fromkeys(capability_ids + ([primary_id] if primary_id else [])))
    features: dict[str, Feature] = {}
    if wanted:
        features = {f.id: f for f in (await db.execute(
            select(Feature).where(Feature.id.in_(wanted)))).scalars().all()}
        missing = [fid for fid in wanted if fid not in features]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "capability_not_found",
                        "message": f"unknown capability id(s): {', '.join(missing)}"})
        if len({f.project_id for f in features.values()}) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "capabilities_span_products",
                        "message": "all capabilities must belong to one product"})
    return wanted, features


def _project_of(wanted: list[str], features: dict[str, Feature], primary_id: str | None) -> str | None:
    """The product an initiative belongs to: its primary's, else its first capability's."""
    if not wanted:
        return None
    anchor = features[primary_id] if primary_id else features[wanted[0]]
    return anchor.project_id
```

Then replace lines 291–311 of `create_initiative` (from `wanted = list(dict.fromkeys(` through `project_id = anchor.project_id`) with:

```python
    wanted, features = await _resolve_capabilities(db, body.primaryCapabilityId, body.capabilityIds)
    project_id = _project_of(wanted, features, body.primaryCapabilityId)
```

Run the existing create tests to prove the refactor is behaviour-preserving:
`… pytest server/tests/test_sdlc_api.py -q -k "create_with"` → Expected: 4 passed.

- [ ] **Step 4: Add the body and the route**

After `step_back_initiative` (`sdlc.py:476`):

```python
class InitiativePatchBody(BaseModel):
    title: str | None = None
    primaryCapabilityId: str | None = None
    capabilityIds: list[str] | None = None


@router.patch("/initiatives/{slug}")
async def update_initiative(
    slug: str,
    body: InitiativePatchBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Inline edit of what a human owns on an initiative (roadmap spec §8, §10):
    the title, and which capabilities it is primary to / touches.

    ``capabilityIds`` REPLACES the touched set; the primary always belongs to
    the set (as on create). A primary given alone re-primaries inside the
    current set; a set given alone keeps the current primary while it is still
    a member and drops it otherwise. The product is re-derived from the primary
    (else the first capability) — ``None`` when the set empties — because the
    initiative's product IS its primary capability's product (§10 model). An
    empty body is a read-through: nothing to audit."""
    ini = await _load_initiative(db, slug)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        d = _initiative_to_dict(ini)
        await _decorate(db, [ini], [d])
        return d
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={
                "error": "bad_title", "message": "title must not be blank"})
        ini.title = title
    if body.primaryCapabilityId is not None or body.capabilityIds is not None:
        current = (await db.execute(select(InitiativeFeature).where(
            InitiativeFeature.initiative_id == ini.id))).scalars().all()
        current_primary = next((r.feature_id for r in current if r.is_primary), None)
        ids = body.capabilityIds if body.capabilityIds is not None else [r.feature_id for r in current]
        if body.primaryCapabilityId is not None:
            primary: str | None = body.primaryCapabilityId
        else:
            primary = current_primary if current_primary in ids else None
        wanted, features = await _resolve_capabilities(db, primary, ids)
        for r in current:
            await db.delete(r)
        await db.flush()
        for fid in wanted:
            db.add(InitiativeFeature(initiative_id=ini.id, feature_id=fid, is_primary=(fid == primary)))
        ini.project_id = _project_of(wanted, features, primary)
    await db.flush()
    await record_audit(db=db, type="sdlc.initiative.update", identity=identity,
                       detail={"slug": slug, "fields": sorted(fields)})
    d = _initiative_to_dict(ini)
    await _decorate(db, [ini], [d])
    return d
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `… pytest server/tests/test_sdlc_api.py -q` → Expected: all passed (the five new plus the existing file).
Run: `/home/tony/GitHub/viberpit/.venv/bin/ruff check src/ server/; echo rc=$?` → 0. `… python -m mypy server/ --ignore-missing-imports` → "Success".

- [ ] **Step 6: Commit**

```bash
git add server/api/sdlc.py server/tests/test_sdlc_api.py
git commit -F /tmp/row8a-t1.msg   # feat(sdlc): 8a task 1 — PATCH /initiatives/{slug}: title, primary and touched capabilities
```

---

### Task 2: Edit an initiative from its page — title, primary, touched capabilities

**Files:**
- Modify: `web/src/api/sdlc.ts` (after `createInitiative`, `:119-122`)
- Modify: `web/src/api/hooks.ts` (after `useCreateInitiative`, `:1587-1594`)
- Create: `web/src/features/roadmap/detail/EditInitiativeDialog.tsx`
- Create: `web/src/features/roadmap/detail/EditInitiativeDialog.test.tsx`
- Modify: `web/src/features/roadmap/InitiativePage.tsx` (imports `:7-38`, `CapabilitiesTab` `:291-313`, header actions `:383-425`, dialogs after `:563`)
- Modify: `web/src/features/roadmap/InitiativePage.test.tsx` (hooks mock `:62-…`, add tests)

**Interfaces:**
- Consumes: Task 1's endpoint; `Initiative` (`sdlc.ts:79`), `ProductNode`/`CapabilityNode` (`roadmap.ts`), `flattenCapabilities` (`features/roadmap/lookup.ts:33`), `useRoadmapMap`, `Dialog`, `Field`, `Input`, `Select`, `Checkbox` (`ui/Checkbox.tsx` — read its props first), `Button`, `extractMutationErrorMessage`, `PM_MUTATION_META`, `QK.initiative(slug)`, `QK.roadmapMap`.
- Produces: `updateInitiative(slug, input: UpdateInitiativeInput): Promise<Initiative>`; `useUpdateInitiative()` → `useMutation<Initiative, ApiRequestError, { slug: string; input: UpdateInitiativeInput }>`; `EditInitiativeDialog({ initiative, products, open, onClose })`.

- [ ] **Step 1: API fetcher and hook**

`web/src/api/sdlc.ts`, after `createInitiative`:

```ts
/** What a human may change inline on an initiative (spec §8). `capabilityIds`
 *  replaces the touched set; the primary always joins it. Send only what changed. */
export interface UpdateInitiativeInput {
  title?: string
  primaryCapabilityId?: string
  capabilityIds?: string[]
}

export function updateInitiative(slug: string, input: UpdateInitiativeInput): Promise<Initiative> {
  return api.patch<Initiative>(`/sdlc/initiatives/${encodeURIComponent(slug)}`, input)
}
```

`web/src/api/hooks.ts`, after `useCreateInitiative` (add `updateInitiative, type UpdateInitiativeInput` to the `@/api/sdlc` import at the top of the file):

```ts
/** Inline edit of an initiative's title / capabilities. The detail and the map
 *  both carry the capability links (the map places the ◆ row under its primary),
 *  so both are invalidated. */
export function useUpdateInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, { slug: string; input: UpdateInitiativeInput }>({
    mutationFn: ({ slug, input }) => updateInitiative(slug, input),
    onSuccess: (_result, { slug }) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    onError: (e) => toast.error(extractMutationErrorMessage(e, 'Could not update the initiative')),
    meta: PM_MUTATION_META,
  })
}
```

(`toast` and `extractMutationErrorMessage` are already imported in `hooks.ts` — `useUpdateOverlay` at `:2002` uses both.)

- [ ] **Step 2: Write the failing dialog test**

`web/src/features/roadmap/detail/EditInitiativeDialog.test.tsx`:

```tsx
// The one form for what a human owns on an initiative: its title and where it
// sits on the map. The primary picks the product; the touched set is the rest
// of THAT product's tree, so a primary from another product empties it.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Initiative } from '@/api/sdlc'
import type { ProductNode } from '@/api/roadmap'
import { EditInitiativeDialog } from './EditInitiativeDialog'

const h = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false }))
vi.mock('@/api/hooks', () => ({
  useUpdateInitiative: () => ({ mutate: h.mutate, isPending: h.isPending, isError: false, error: null }),
}))

const cap = (id: string, slug: string, title: string, children: ProductNode['capabilities'] = []) => ({
  id, slug, title, state: 'live', summary: null, docPath: null, commitSha: null, prNumber: null,
  parentSlug: null, tier: 'idle' as const, counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 },
  links: [], initiatives: [], children,
})
const product = (slug: string, name: string, capabilities: ProductNode['capabilities']): ProductNode => ({
  slug, name, tier: 'idle', counts: { inFlight: 0, blocked: 0, needsYou: 0, capabilities: capabilities.length, awaitingTriage: 0, needsHome: 0 },
  capabilities, initiatives: [],
})
const products = [
  product('kindash', 'KinDash', [cap('f1', 'task-views', 'Task views', [cap('f2', 'filters', 'Filters')])]),
  product('web', 'Web', [cap('f9', 'billing', 'Billing')]),
]
const initiative = {
  slug: 'views', title: 'Use-case-first task views', repo: 'o/r', stage: 'build', status: 'active',
  origin: 'human', originRef: null, epicRepo: null, epicNumber: null, createdAt: null, updatedAt: null,
  artifacts: [], epic: null, projectSlug: 'kindash',
  primaryCapability: { id: 'f1', slug: 'task-views', title: 'Task views' },
  capabilities: [
    { id: 'f1', slug: 'task-views', title: 'Task views', isPrimary: true },
    { id: 'f2', slug: 'filters', title: 'Filters', isPrimary: false },
  ],
} as unknown as Initiative

describe('EditInitiativeDialog', () => {
  beforeEach(() => { h.mutate.mockReset() })

  it('is seeded from the initiative and Save is disabled until something changes', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    expect(screen.getByLabelText('Title')).toHaveValue('Use-case-first task views')
    expect(screen.getByLabelText('Primary capability')).toHaveValue('f1')
    expect(screen.getByRole('checkbox', { name: 'Filters' })).toBeChecked()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('sends only the changed fields', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Task views, use-case first' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.mutate).toHaveBeenCalledWith(
      { slug: 'views', input: { title: 'Task views, use-case first' } }, expect.anything())
  })

  it('a touched change sends the whole set with the primary in it', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    fireEvent.click(screen.getByRole('checkbox', { name: 'Filters' }))   // untick
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.mutate).toHaveBeenCalledWith(
      { slug: 'views', input: { capabilityIds: ['f1'] } }, expect.anything())
  })

  it('a primary from another product empties the touched set and sends both', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Primary capability'), { target: { value: 'f9' } })
    expect(screen.queryByRole('checkbox', { name: 'Filters' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.mutate).toHaveBeenCalledWith(
      { slug: 'views', input: { primaryCapabilityId: 'f9', capabilityIds: ['f9'] } }, expect.anything())
  })

  it('a blank title disables Save and says why', () => {
    render(<EditInitiativeDialog initiative={initiative} products={products} open onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.getByText('Give it a title')).toBeInTheDocument()
  })
})
```

- [ ] **Step 3: Run it to verify it fails**

Run: `(cd web && npx vitest run src/features/roadmap/detail/EditInitiativeDialog.test.tsx)` → Expected: FAIL, module not found.

- [ ] **Step 4: Write the dialog**

`web/src/features/roadmap/detail/EditInitiativeDialog.tsx` (`web/src/ui/Checkbox.tsx` takes `{ id, label, checked, onChange: (next: boolean) => void, disabled?, className? }` and renders a labelled `<input type="checkbox">`, so `getByRole('checkbox', { name })` finds it):

```tsx
// "Edit" on an initiative: the title and its place on the map (roadmap spec §8,
// "Create initiative; set primary and touched capabilities" — the edit half).
// The primary picks the product, so the touched checkboxes are always the REST
// of that product's tree; a primary from another product starts the set over,
// because the server refuses a set that spans products (422).
import { useMemo, useRef, useState } from 'react'
import type { Initiative, UpdateInitiativeInput } from '@/api/sdlc'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import { useUpdateInitiative } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { Checkbox } from '@/ui/Checkbox'
import { Button } from '@/ui/Button'
import { CAPABILITY, INITIATIVE } from '@/features/products/vocab'
import { flattenCapabilities } from '../lookup'

function productOfCapability(products: ProductNode[], capId: string): ProductNode | null {
  return products.find((p) => flattenCapabilities(p.capabilities).some(({ node }) => node.id === capId)) ?? null
}

export function EditInitiativeDialog({ initiative, products, open, onClose }: {
  initiative: Initiative
  /** Every product on the map — the primary may move the initiative between them. */
  products: ProductNode[]
  open: boolean
  onClose: () => void
}) {
  const update = useUpdateInitiative()
  const titleRef = useRef<HTMLInputElement>(null)
  const seedTitle = initiative.title ?? ''
  const seedPrimary = initiative.primaryCapability?.id ?? ''
  const seedTouched = initiative.capabilities.filter((c) => !c.isPrimary).map((c) => c.id)

  const [title, setTitle] = useState(seedTitle)
  const [primary, setPrimary] = useState(seedPrimary)
  const [touched, setTouched] = useState<string[]>(seedTouched)

  // The product the primary belongs to; the touched list is its tree minus the primary.
  const product = primary ? productOfCapability(products, primary) : null
  const touchable = useMemo(
    () => (product ? flattenCapabilities(product.capabilities).filter(({ node }) => node.id !== primary) : []),
    [product, primary],
  )

  const trimmed = title.trim()
  const titleChanged = trimmed !== seedTitle
  const setChanged = primary !== seedPrimary || [...touched].sort().join() !== [...seedTouched].sort().join()
  const canSave = trimmed !== '' && (titleChanged || setChanged) && !update.isPending
  const titleError = trimmed === '' ? 'Give it a title' : undefined

  const save = () => {
    const input: UpdateInitiativeInput = {}
    if (titleChanged) input.title = trimmed
    if (primary !== seedPrimary) input.primaryCapabilityId = primary
    // The set is sent whole whenever any part of it moved — it replaces, not merges.
    if (setChanged) input.capabilityIds = primary ? [primary, ...touched] : touched
    update.mutate({ slug: initiative.slug, input }, { onSuccess: onClose })
  }

  const choosePrimary = (id: string) => {
    const next = id ? productOfCapability(products, id) : null
    // Same product: keep the touched set (minus the new primary). Another
    // product, or none: start over.
    setTouched((cur) => (next && product && next.slug === product.slug ? cur.filter((x) => x !== id) : []))
    setPrimary(id)
  }

  return (
    <Dialog open={open} onClose={onClose} title={`Edit ${INITIATIVE.singular.toLowerCase()}`} size="md" initialFocus={titleRef}>
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (canSave) save() }}>
        <Field label="Title" htmlFor="ei-title" error={titleError} required>
          <Input ref={titleRef} id="ei-title" invalid={!!titleError} value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label={`Primary ${CAPABILITY.singular.toLowerCase()}`} htmlFor="ei-primary" help="Decides which product the initiative belongs to">
          <Select id="ei-primary" value={primary} onChange={(e) => choosePrimary(e.target.value)}>
            <option value="">— none —</option>
            {products.map((p) => (
              <optgroup key={p.slug} label={p.name ?? p.slug}>
                {flattenCapabilities(p.capabilities).map(({ node, depth }) => (
                  <option key={node.id} value={node.id}>{'  '.repeat(depth)}{node.title ?? node.slug}</option>
                ))}
              </optgroup>
            ))}
          </Select>
        </Field>
        {touchable.length > 0 && (
          <fieldset className="flex flex-col gap-1">
            <legend className="text-sm font-medium text-ink">Also touches</legend>
            {touchable.map(({ node, depth }: { node: CapabilityNode; depth: number }) => (
              <Checkbox
                key={node.id}
                id={`ei-touch-${node.id}`}
                label={node.title ?? node.slug}
                className={depth > 0 ? 'ml-4' : undefined}
                checked={touched.includes(node.id)}
                onChange={(next) => setTouched((cur) =>
                  next ? [...cur, node.id] : cur.filter((x) => x !== node.id))}
              />
            ))}
          </fieldset>
        )}
        {update.isError && (
          <p role="alert" className="text-sm text-danger">
            {extractMutationErrorMessage(update.error, `Could not update the ${INITIATIVE.singular.toLowerCase()}.`)}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!canSave} loading={update.isPending}>Save</Button>
        </div>
      </form>
    </Dialog>
  )
}
```

The test's accessible name for the primary select is `'Primary capability'` — `CAPABILITY.singular` is `'Capability'` (check `vocab.ts:19`); the `Field` label is what `getByLabelText` matches, so keep the label text exactly `Primary capability`.

- [ ] **Step 5: Run the dialog test to verify it passes**

Run: `(cd web && npx vitest run src/features/roadmap/detail/EditInitiativeDialog.test.tsx)` → Expected: 5 passed.

- [ ] **Step 6: Mount it on the initiative page**

In `InitiativePage.tsx`:
- import `EditInitiativeDialog` from `./detail/EditInitiativeDialog`;
- state `const [editing, setEditing] = useState(false)` beside the other confirm flags;
- in the header `actions` block, FIRST child (before Step back):

```tsx
            {/* The one place an initiative's own facts are edited; the gate
                controls beside it move it, this names it. */}
            <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>Edit</Button>
```

- `CapabilitiesTab` gains an `onEdit: () => void` prop and renders, above its list (and inside the empty state's description area, as a sibling after `EmptyState`), `<Button size="sm" variant="ghost" onClick={onEdit}>Edit {CAPABILITY.plural.toLowerCase()}</Button>`; the tab mount passes `onEdit={() => setEditing(true)}`;
- after the last confirm `Dialog` (the unblock one):

```tsx
      {initiative && map && (
        <EditInitiativeDialog
          initiative={initiative}
          products={map.products}
          open={editing}
          onClose={() => setEditing(false)}
        />
      )}
```

- [ ] **Step 7: Page tests**

In `InitiativePage.test.tsx`: add `updateMutate: vi.fn()` to the hoisted bag and `useUpdateInitiative: () => ({ mutate: h.updateMutate, isPending: false, isError: false, error: null })` to the `@/api/hooks` mock. Add:

```tsx
  it('Edit opens the dialog seeded with the title; Save sends the changed title', () => {
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const title = screen.getByLabelText('Title')
    expect(title).toHaveValue('Use-case-first task views')
    fireEvent.change(title, { target: { value: 'Views' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.updateMutate).toHaveBeenCalledWith({ slug: 'views', input: { title: 'Views' } }, expect.anything())
  })
```

(`renderPage` — use whatever helper the file already defines for rendering the page inside `MemoryRouter`; read the existing step-back test at `:165-175` and mirror it.) The map fixture's product needs the initiative's capability in its tree for the primary select to be seeded: add `capabilities: [{ id: 'f1', slug: 'task-views', title: 'Task views', state: 'live', summary: null, docPath: null, commitSha: null, prNumber: null, parentSlug: null, tier: 'idle', counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 }, links: [], initiatives: [], children: [] }]` to the hoisted `map.products[0]` (it is `[]` today at `:47`).

Run: `(cd web && npx vitest run src/features/roadmap/InitiativePage.test.tsx src/features/roadmap/InitiativePage.materialization.test.tsx)` → Expected: all passed.

- [ ] **Step 8: Gate and commit**

Web gate (Global Constraints). Then:

```bash
git add web/src/api/sdlc.ts web/src/api/hooks.ts web/src/features/roadmap/detail/EditInitiativeDialog.tsx web/src/features/roadmap/detail/EditInitiativeDialog.test.tsx web/src/features/roadmap/InitiativePage.tsx web/src/features/roadmap/InitiativePage.test.tsx
git commit -F /tmp/row8a-t2.msg   # feat(roadmap): 8a task 2 — edit an initiative's title, primary and touched capabilities from its page
```

---

### Task 3: Leaf-row edits — score chips and the capability chip

**Files:**
- Modify: `server/api/pm_tree.py:175-185` (`_leaf` gains `featureId`); test `server/tests/test_pm_map_api.py` (`test_leaves_for_capability_and_initiative`, `:195`)
- Modify: `web/src/api/roadmap.ts:85-107` (`Leaf.featureId`)
- Modify: `web/src/api/hooks.ts:2002-2015` (`useUpdateOverlay` invalidations)
- Create: `web/src/features/roadmap/inbox/ScoreFields.tsx`; modify `inbox/ProposalDetail.tsx:71-73,203-216` to use it
- Create: `web/src/features/roadmap/detail/LeafEditors.tsx`, `LeafEditors.test.tsx`
- Modify: `web/src/features/roadmap/detail/LeafList.tsx` (`LeafRow`, `LeafList` props), `detail/CapabilityDetail.tsx:177`, `InitiativePage.tsx:271` (pass the product)
- Modify: `web/src/features/roadmap/detail/CapabilityDetail.test.tsx` hooks mock (add `useUpdateOverlay`, `useRoadmapMap` if not present)

**Interfaces:**
- Consumes: `PATCH /api/pm/work-items/{id}/overlay` via `updateOverlay(itemId, edit: OverlayEdits)` (`triage.ts:106`) and `useUpdateOverlay()` (`hooks.ts:2002`); `riceScore` (`inbox/rice.ts`); `scoreInputValue` (`inbox/inboxKeys.ts:159`); `flattenCapabilities`.
- Produces: `Leaf.featureId: string | null`; `ScoreFields({ edits, onChange, firstRef?, idPrefix? })`; `LeafScoreButton({ leaf })`; `LeafCapabilityChip({ leaf, capabilities })` where `capabilities: { node: CapabilityNode; depth: number }[]`; `LeafList({ scope, product })` with `product: ProductNode | null`.

- [ ] **Step 1: `featureId` on a leaf (server)**

In `server/tests/test_pm_map_api.py::test_leaves_for_capability_and_initiative`, after `assert items[0]["parentRef"] == "o/r#10"` add `assert items[0]["featureId"] == cap_id`. Run it → FAIL (KeyError). In `pm_tree.py:_leaf` add `"featureId": w.feature_id,` after `"parentRef": parent_ref,`. Run → PASS. In `web/src/api/roadmap.ts` `Leaf`, add `/** The capability the issue sits under on the map; null while unclassified. */ featureId: string | null` after `parentRef`. Every test fixture typed as `Leaf` gains `featureId: null` — `npm run typecheck` lists them.

- [ ] **Step 2: Extract `ScoreFields`**

`web/src/features/roadmap/inbox/ScoreFields.tsx`:

```tsx
// The four RICE inputs, in formula order (`server/pm/score.py:RICE_FIELDS`).
// One component for the inbox's edit-then-accept form and the leaf row's score
// dialog, so the two never disagree on ids, labels or parsing.
import type { OverlayEdits } from '@/api/triage'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { scoreInputValue } from './inboxKeys'

export const SCORE_FIELDS = ['reach', 'impact', 'effort', 'confidence'] as const
export type ScoreField = typeof SCORE_FIELDS[number]

export function ScoreFields({ edits, onChange, firstRef, idPrefix = 'score' }: {
  edits: OverlayEdits
  onChange: (next: (s: OverlayEdits) => OverlayEdits) => void
  /** The first input, for callers that focus it on open. */
  firstRef?: React.RefObject<HTMLInputElement | null>
  idPrefix?: string
}) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {SCORE_FIELDS.map((f, i) => (
        <Field key={f} label={f} htmlFor={`${idPrefix}-${f}`}>
          <Input
            id={`${idPrefix}-${f}`}
            type="number"
            aria-label={f}
            ref={i === 0 ? firstRef : undefined}
            value={edits[f] ?? ''}
            onChange={(e) => onChange((s) => ({ ...s, [f]: scoreInputValue(e.target.value) }))}
          />
        </Field>
      ))}
    </div>
  )
}
```

In `ProposalDetail.tsx`: delete the local `FIELDS`/`ScoreField` (`:72-73`) and import `{ ScoreFields, SCORE_FIELDS, type ScoreField } from './ScoreFields'`; replace every `FIELDS` with `SCORE_FIELDS`; replace the `<div className="grid …">…</div>` block (`:203-216`) with `<ScoreFields edits={edits} onChange={setEdits} firstRef={firstRef} />`. Run `(cd web && npx vitest run src/features/roadmap/inbox)` → Expected: unchanged, all passed (ids `score-reach` and labels `reach` are preserved).

- [ ] **Step 3: `useUpdateOverlay` invalidates what a score or capability change moves**

`hooks.ts:2002-2015` — `onSuccess` becomes:

```ts
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.triage })
      // A score reorders every leaves list the row is in; a capability change
      // moves the row between nodes and shifts two nodes' counts on the map.
      void qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
```

Check `web/src/api/__tests__/triageHooks.test.tsx` for an assertion on `useUpdateOverlay`'s invalidations and extend it to expect the two new keys.

- [ ] **Step 4: Write the failing editor tests**

`web/src/features/roadmap/detail/LeafEditors.test.tsx`:

```tsx
// The two inline edits a leaf row offers (spec §8): the score, through the same
// four fields the inbox uses, and the capability it sits under.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Leaf } from '@/api/roadmap'
import { LeafScoreButton, LeafCapabilityChip } from './LeafEditors'

const h = vi.hoisted(() => ({ mutate: vi.fn() }))
vi.mock('@/api/hooks', () => ({
  useUpdateOverlay: () => ({ mutate: h.mutate, isPending: false, isError: false, error: null }),
}))

const leaf: Leaf = {
  id: 'w1', ref: 'o/r#7', repo: 'o/r', number: 7, kind: 'issue', title: 'Add filters', url: null,
  state: 'open', triageState: 'classified', score: 12.5, reach: 5, impact: 5, effort: 2, confidence: 1,
  tier: 'idle', blockedBy: [], parentRef: null, sessionStatus: null, ghCreatedAt: null, featureId: 'f1',
}
const node = (id: string, slug: string, title: string) => ({
  id, slug, title, state: 'live', summary: null, docPath: null, commitSha: null, prNumber: null,
  parentSlug: null, tier: 'idle' as const, counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 },
  links: [], initiatives: [], children: [],
})
const capabilities = [{ node: node('f1', 'task-views', 'Task views'), depth: 0 }, { node: node('f2', 'filters', 'Filters'), depth: 1 }]

describe('LeafScoreButton', () => {
  beforeEach(() => { h.mutate.mockReset() })

  it('shows the score, opens the four fields seeded, and sends only what changed', () => {
    render(<LeafScoreButton leaf={leaf} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit score for #7' }))
    expect(screen.getByLabelText('reach')).toHaveValue(5)
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('effort'), { target: { value: '4' } })
    expect(screen.getByText('6.25')).toBeInTheDocument()     // live read-out: 5*5*1/4
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.mutate).toHaveBeenCalledWith({ itemId: 'w1', edit: { effort: 4 } }, expect.anything())
  })

  it('an unscored leaf opens blank and reads "—"', () => {
    render(<LeafScoreButton leaf={{ ...leaf, score: null, reach: null, impact: null, effort: null, confidence: null }} />)
    expect(screen.getByRole('button', { name: 'Edit score for #7' })).toHaveTextContent('—')
  })
})

describe('LeafCapabilityChip', () => {
  beforeEach(() => { h.mutate.mockReset() })

  it('names the current capability and moves the leaf on Save', () => {
    render(<LeafCapabilityChip leaf={leaf} capabilities={capabilities} />)
    const chip = screen.getByRole('button', { name: 'Move #7 to another capability' })
    expect(chip).toHaveTextContent('Task views')
    fireEvent.click(chip)
    const select = screen.getByLabelText('Capability')
    expect(select).toHaveValue('f1')
    expect(screen.getByRole('button', { name: 'Move' })).toBeDisabled()
    fireEvent.change(select, { target: { value: 'f2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move' }))
    expect(h.mutate).toHaveBeenCalledWith({ itemId: 'w1', edit: { featureId: 'f2' } }, expect.anything())
  })

  it('renders nothing to click when the tree is not known', () => {
    render(<LeafCapabilityChip leaf={leaf} capabilities={[]} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
```

Run: `(cd web && npx vitest run src/features/roadmap/detail/LeafEditors.test.tsx)` → Expected: FAIL, module not found.

- [ ] **Step 5: Write the editors**

`web/src/features/roadmap/detail/LeafEditors.tsx`:

```tsx
// The two inline edits on an issue leaf (roadmap spec §8): "Edit scores" —
// click the score, four labelled fields with the computed score — and
// "Reassign an issue's capability". Both write the ccswitch overlay
// (`PATCH /pm/work-items/{id}/overlay`), immediate; the score reaches the board
// through the reconciler. Each is a small dialog rather than an in-row input so
// the row keeps its one-line shape and the change is a deliberate two-step.
import { useRef, useState } from 'react'
import type { Leaf, CapabilityNode } from '@/api/roadmap'
import type { OverlayEdits } from '@/api/triage'
import { useUpdateOverlay } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Select } from '@/ui/Select'
import { Button } from '@/ui/Button'
import { CAPABILITY } from '@/features/products/vocab'
import { ScoreFields, SCORE_FIELDS } from '../inbox/ScoreFields'
import { riceScore } from '../inbox/rice'

const seedOf = (leaf: Leaf): OverlayEdits => ({
  reach: leaf.reach ?? undefined, impact: leaf.impact ?? undefined,
  effort: leaf.effort ?? undefined, confidence: leaf.confidence ?? undefined,
})

export function LeafScoreButton({ leaf }: { leaf: Leaf }) {
  const update = useUpdateOverlay()
  const [open, setOpen] = useState(false)
  const [edits, setEdits] = useState<OverlayEdits>(() => seedOf(leaf))
  const firstRef = useRef<HTMLInputElement>(null)
  const seed = seedOf(leaf)
  const changed: OverlayEdits = {}
  for (const f of SCORE_FIELDS) if ((edits[f] ?? null) !== (seed[f] ?? null)) changed[f] = edits[f]
  const dirty = Object.keys(changed).length > 0
  const preview = riceScore(edits.reach, edits.impact, edits.effort, edits.confidence)
  const openDialog = () => { setEdits(seedOf(leaf)); setOpen(true) }
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="w-12 shrink-0 justify-end tabular-nums text-ink-muted"
        aria-label={`Edit score for #${leaf.number}`}
        title="Priority score — click to edit"
        onClick={openDialog}
      >
        {leaf.score?.toFixed(1) ?? '—'}
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`Score #${leaf.number}`} size="sm" initialFocus={firstRef}>
        <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (dirty) update.mutate({ itemId: leaf.id, edit: changed }, { onSuccess: () => setOpen(false) }) }}>
          <ScoreFields edits={edits} onChange={setEdits} firstRef={firstRef} idPrefix={`leaf-${leaf.id}`} />
          <p className="text-sm text-ink-muted">
            Score: <span aria-live="polite" className="font-semibold tabular-nums text-ink">{preview ?? '—'}</span>
          </p>
          {update.isError && <p role="alert" className="text-sm text-danger">{extractMutationErrorMessage(update.error, 'Could not save the score')}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" size="lg" disabled={!dirty || update.isPending} loading={update.isPending}>Save</Button>
          </div>
        </form>
      </Dialog>
    </>
  )
}

export function LeafCapabilityChip({ leaf, capabilities }: {
  leaf: Leaf
  /** The product's flattened tree (`flattenCapabilities`); empty when unknown. */
  capabilities: { node: CapabilityNode; depth: number }[]
}) {
  const update = useUpdateOverlay()
  const [open, setOpen] = useState(false)
  const [target, setTarget] = useState(leaf.featureId ?? '')
  if (capabilities.length === 0) return null
  const current = capabilities.find(({ node }) => node.id === leaf.featureId)?.node
  const dirty = target !== '' && target !== (leaf.featureId ?? '')
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="max-w-40 truncate text-xs text-ink-muted"
        aria-label={`Move #${leaf.number} to another ${CAPABILITY.singular.toLowerCase()}`}
        onClick={() => { setTarget(leaf.featureId ?? ''); setOpen(true) }}
      >
        {current?.title ?? current?.slug ?? `no ${CAPABILITY.singular.toLowerCase()}`}
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`Move #${leaf.number}`} size="sm">
        <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (dirty) update.mutate({ itemId: leaf.id, edit: { featureId: target } }, { onSuccess: () => setOpen(false) }) }}>
          <Field label={CAPABILITY.singular} htmlFor={`leaf-cap-${leaf.id}`}>
            <Select id={`leaf-cap-${leaf.id}`} value={target} onChange={(e) => setTarget(e.target.value)}>
              {capabilities.map(({ node, depth }) => (
                <option key={node.id} value={node.id}>{'  '.repeat(depth)}{node.title ?? node.slug}</option>
              ))}
            </Select>
          </Field>
          {update.isError && <p role="alert" className="text-sm text-danger">{extractMutationErrorMessage(update.error, 'Could not move the issue')}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" size="lg" disabled={!dirty || update.isPending} loading={update.isPending}>Move</Button>
          </div>
        </form>
      </Dialog>
    </>
  )
}
```

`max-w-40` is Tailwind's generative spacing scale (10 rem), not an arbitrary value — the palette rule only rejects pixel literals.

- [ ] **Step 6: Run the editor tests to verify they pass**

Run: `(cd web && npx vitest run src/features/roadmap/detail/LeafEditors.test.tsx)` → Expected: 4 passed.

- [ ] **Step 7: Mount them on the row**

In `LeafList.tsx`:
- `LeafList` signature becomes `export function LeafList({ scope, product }: { scope: LeavesScope; product: ProductNode | null })`; compute `const capabilities = useMemo(() => (product ? flattenCapabilities(product.capabilities) : []), [product])` (import `useMemo`, `flattenCapabilities` from `'../lookup'`, `ProductNode` type) and pass `capabilities` down to every `LeafRow`;
- `LeafRow({ leaf, capabilities })`: replace the score `<span … title="Priority score">` (`:85-87`) with `<LeafScoreButton leaf={leaf} />`, and insert `<LeafCapabilityChip leaf={leaf} capabilities={capabilities} />` immediately before it (after the blocker badge). Import both from `./LeafEditors`.
- `CapabilityDetail.tsx:177`: `<LeafList scope={{ capability: capability.id }} product={product} />` (`product` is already a prop, `:40`).
- `InitiativePage.tsx` `EpicTab`: it receives only `initiative`; add a `product: ProductNode | null` prop, pass `product={map?.products.find((p) => p.slug === productSlug) ?? null}` from the page (compute `productNode` once beside `productName`, `:353-356`), and render `<LeafList scope={{ initiative: initiative.slug }} product={product} />`.
- Tests: `CapabilityDetail.test.tsx`'s `@/api/hooks` mock (`:34`) gains `useUpdateOverlay: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null })`; same for `InitiativePage.test.tsx` and `InitiativePage.materialization.test.tsx` if the Epic tab renders leaves there (run them; add the mock where a test fails with "useUpdateOverlay is not a function").

Run: `(cd web && npx vitest run src/features/roadmap)` → Expected: all passed.

- [ ] **Step 8: Gate and commit**

Web gate; server: `ruff`, `mypy`, `pytest server/tests/test_pm_map_api.py -q`. Then:

```bash
git add server/api/pm_tree.py server/tests/test_pm_map_api.py web/src/api/roadmap.ts web/src/api/hooks.ts web/src/api/__tests__/triageHooks.test.tsx web/src/features/roadmap/inbox/ScoreFields.tsx web/src/features/roadmap/inbox/ProposalDetail.tsx web/src/features/roadmap/detail/LeafEditors.tsx web/src/features/roadmap/detail/LeafEditors.test.tsx web/src/features/roadmap/detail/LeafList.tsx web/src/features/roadmap/detail/CapabilityDetail.tsx web/src/features/roadmap/detail/CapabilityDetail.test.tsx web/src/features/roadmap/InitiativePage.tsx web/src/features/roadmap/InitiativePage.test.tsx
git commit -F /tmp/row8a-t3.msg   # feat(roadmap): 8a task 3 — score and capability edits on an issue leaf; ScoreFields shared with the inbox
```

(Add any other test fixture file `typecheck` made you touch for `featureId`.)

---

### Task 4: Product-to-product dependency chips on the Dependencies tab

**Files:**
- Modify: `web/src/api/pm.ts` (after `useProjectDeps`, `:151-159`)
- Modify: `web/src/api/hooks.ts` (after `useProject`)
- Create: `web/src/features/roadmap/detail/ProductEdgesEditor.tsx`, `ProductEdgesEditor.test.tsx`
- Modify: `web/src/features/roadmap/detail/ProductDetail.tsx:127` (the Dependencies panel)
- Modify: `web/src/features/roadmap/detail/ProductDetail.test.tsx` hooks mock

**Interfaces:**
- Consumes: `POST /api/pm/projects/{slug}/edges` body `{dstSlug, kind, note?}` → 201 `ProjectEdgeInfo`; `DELETE /api/pm/projects/{slug}/edges/{edgeId}` → 204; `useProject(slug)` (`Project.edges: ProjectEdgeInfo[]`, outgoing only — `pm.py:277`); `PROJECT_EDGE_KINDS` (`pm.ts:15`); `useRoadmapMap({ poll: false })` for the target list; `IconButton` (`ui/IconButton.tsx`, requires `aria-label`); lucide `X`.
- Produces: `createEdge(slug, input: CreateEdgeInput): Promise<ProjectEdgeInfo>`, `deleteEdge(slug, edgeId): Promise<void>`; `useCreateEdge(slug)`, `useDeleteEdge(slug)`; `ProductEdgesEditor({ product })`.

- [ ] **Step 1: Fetchers and hooks**

`web/src/api/pm.ts`, after `useProjectDeps`:

```ts
export interface CreateEdgeInput { dstSlug: string; kind: EdgeKind; note?: string }

/** A product-level edge (`depends_on` / `deploys_via` / `integrates_with`), ccswitch-owned. */
export function createEdge(slug: string, input: CreateEdgeInput): Promise<ProjectEdgeInfo> {
  return api.post<ProjectEdgeInfo>(`/pm/projects/${encodeURIComponent(slug)}/edges`, input)
}

export function deleteEdge(slug: string, edgeId: string): Promise<void> {
  return api.del<void>(`/pm/projects/${encodeURIComponent(slug)}/edges/${encodeURIComponent(edgeId)}`)
}
```

`web/src/api/hooks.ts`, after `useProject` (import `createEdge, deleteEdge, type CreateEdgeInput, type ProjectEdgeInfo` from `@/api/pm`):

```ts
/** Product dependency chips (spec §8 "Product dependency"). The project payload
 *  carries the edges; the map's product tiers read them; the deps panel below
 *  the chips polls its own key. */
export function useCreateEdge(slug: string) {
  const qc = useQueryClient()
  return useMutation<ProjectEdgeInfo, ApiRequestError, CreateEdgeInput>({
    mutationFn: (input) => createEdge(slug, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: ['pm', 'deps', slug] })
    },
    onError: (e) => toast.error(extractMutationErrorMessage(e, 'Could not add the dependency')),
    meta: PM_MUTATION_META,
  })
}

export function useDeleteEdge(slug: string) {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, string>({
    mutationFn: (edgeId) => deleteEdge(slug, edgeId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: ['pm', 'deps', slug] })
    },
    onError: (e) => toast.error(extractMutationErrorMessage(e, 'Could not remove the dependency')),
    meta: PM_MUTATION_META,
  })
}
```

- [ ] **Step 2: Write the failing test**

`web/src/features/roadmap/detail/ProductEdgesEditor.test.tsx`:

```tsx
// The product's own dependencies (spec §8 "Product dependency"): chips for the
// edges it has, a remove on each, and one row to add another.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ProductNode } from '@/api/roadmap'
import { ProductEdgesEditor } from './ProductEdgesEditor'

const h = vi.hoisted(() => ({
  create: vi.fn(), remove: vi.fn(),
  project: {
    id: 'p1', slug: 'kindash', name: 'KinDash', description: null, status: null, portfolioTags: [],
    onboardingState: 'live', createdAt: null, updatedAt: null, repos: [], featureCount: 0, workItemCount: 0,
    edges: [{ id: 'e1', srcSlug: 'kindash', dstSlug: 'web', kind: 'depends_on', note: null }],
  },
  map: { generatedAt: 'now', products: [
    { slug: 'kindash', name: 'KinDash' }, { slug: 'web', name: 'Web' }, { slug: 'infra', name: 'Infra' },
  ] },
}))
vi.mock('@/api/hooks', () => ({
  useProject: () => ({ data: h.project, isLoading: false, isError: false }),
  useRoadmapMap: () => ({ data: h.map }),
  useCreateEdge: () => ({ mutate: h.create, isPending: false }),
  useDeleteEdge: () => ({ mutate: h.remove, isPending: false }),
}))

const product = { slug: 'kindash', name: 'KinDash' } as ProductNode

describe('ProductEdgesEditor', () => {
  beforeEach(() => { h.create.mockReset(); h.remove.mockReset() })

  it('lists the edges as chips with a remove each', () => {
    render(<ProductEdgesEditor product={product} />)
    expect(screen.getByText('depends on Web')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove: depends on Web' }))
    expect(h.remove).toHaveBeenCalledWith('e1')
  })

  it('adds an edge to another product; the product itself and existing targets are not offered', () => {
    render(<ProductEdgesEditor product={product} />)
    const target = screen.getByLabelText('Product')
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    expect(screen.queryByRole('option', { name: 'KinDash' })).toBeNull()
    fireEvent.change(screen.getByLabelText('Relationship'), { target: { value: 'deploys_via' } })
    fireEvent.change(target, { target: { value: 'infra' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(h.create).toHaveBeenCalledWith({ dstSlug: 'infra', kind: 'deploys_via' }, expect.anything())
  })
})
```

Run: `(cd web && npx vitest run src/features/roadmap/detail/ProductEdgesEditor.test.tsx)` → Expected: FAIL, module not found.

- [ ] **Step 3: Write the editor**

`web/src/features/roadmap/detail/ProductEdgesEditor.tsx`:

```tsx
// Product-to-product dependencies (roadmap spec §8 "Product dependency"): the
// ccswitch-owned edges, as chips with a remove each, and one row to add one.
// Distinct from `DependencyPanel` below it, which mirrors ISSUE-level blocked-by
// drift from GitHub; these edges are what the portfolio tiers and the
// per-product dependency tab read.
import { useState } from 'react'
import { X } from 'lucide-react'
import type { ProductNode } from '@/api/roadmap'
import { PROJECT_EDGE_KINDS, type EdgeKind } from '@/api/pm'
import { useCreateEdge, useDeleteEdge, useProject, useRoadmapMap } from '@/api/hooks'
import { Field } from '@/ui/Field'
import { Select } from '@/ui/Select'
import { Input } from '@/ui/Input'
import { Button } from '@/ui/Button'
import { IconButton } from '@/ui/IconButton'
import { Skeleton } from '@/ui/Skeleton'
import { PRODUCT } from '@/features/products/vocab'

const KIND_LABEL: Record<EdgeKind, string> = {
  depends_on: 'depends on', deploys_via: 'deploys via', integrates_with: 'integrates with',
}

export function ProductEdgesEditor({ product }: { product: ProductNode }) {
  const { data: project, isLoading } = useProject(product.slug)
  const { data: map } = useRoadmapMap({ poll: false })
  const create = useCreateEdge(product.slug)
  const remove = useDeleteEdge(product.slug)
  const [kind, setKind] = useState<EdgeKind>('depends_on')
  const [dst, setDst] = useState('')
  const [note, setNote] = useState('')
  const edges = project?.edges ?? []
  const taken = new Set(edges.map((e) => `${e.kind}:${e.dstSlug}`))
  const nameOf = (slug: string | null) => map?.products.find((p) => p.slug === slug)?.name ?? slug ?? '?'
  // Every other product on the map that this one does not already point at with this kind.
  const targets = (map?.products ?? []).filter((p) => p.slug !== product.slug && !taken.has(`${kind}:${p.slug}`))
  const canAdd = dst !== '' && !create.isPending

  if (isLoading && !project) return <Skeleton lines={2} />
  return (
    <div className="flex flex-col gap-3">
      {edges.length === 0
        ? <p className="text-sm text-ink-muted">No {PRODUCT.singular.toLowerCase()} dependencies yet.</p>
        : (
          <ul aria-label={`${PRODUCT.singular} dependencies`} className="flex flex-wrap gap-2">
            {edges.map((e) => {
              const label = `${KIND_LABEL[e.kind as EdgeKind] ?? e.kind} ${nameOf(e.dstSlug)}`
              return (
                <li key={e.id} className="flex items-center gap-1 rounded-pill border border-edge bg-surface-raised py-0.5 pl-3 pr-1 text-sm text-ink" title={e.note ?? undefined}>
                  <span>{label}</span>
                  <IconButton size="sm" aria-label={`Remove: ${label}`} disabled={remove.isPending} onClick={() => remove.mutate(e.id)}>
                    <X size={14} aria-hidden="true" />
                  </IconButton>
                </li>
              )
            })}
          </ul>
        )}
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => { e.preventDefault(); if (canAdd) create.mutate({ dstSlug: dst, kind, ...(note.trim() ? { note: note.trim() } : {}) }, { onSuccess: () => { setDst(''); setNote('') } }) }}
      >
        <Field label="Relationship" htmlFor="edge-kind">
          <Select id="edge-kind" value={kind} onChange={(e) => { setKind(e.target.value as EdgeKind); setDst('') }}>
            {PROJECT_EDGE_KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
          </Select>
        </Field>
        <Field label={PRODUCT.singular} htmlFor="edge-dst">
          <Select id="edge-dst" value={dst} onChange={(e) => setDst(e.target.value)}>
            <option value="">— choose —</option>
            {targets.map((p) => <option key={p.slug} value={p.slug}>{p.name ?? p.slug}</option>)}
          </Select>
        </Field>
        <Field label="Note" htmlFor="edge-note">
          <Input id="edge-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="optional" />
        </Field>
        <Button type="submit" disabled={!canAdd} loading={create.isPending}>Add</Button>
      </form>
    </div>
  )
}
```

Check `PRODUCT.singular` in `vocab.ts:12` renders `Product` so the test's `getByLabelText('Product')` matches; `rounded-pill` is the repo's pill radius token (used by `Badge`) — confirm in `index.css` and use `rounded-full` only if it is absent.

- [ ] **Step 4: Run the test to verify it passes**

Run: `(cd web && npx vitest run src/features/roadmap/detail/ProductEdgesEditor.test.tsx)` → Expected: 2 passed.

- [ ] **Step 5: Mount it**

`ProductDetail.tsx:127`: replace `{tab === 'dependencies' && <DependencyPanel slug={product.slug} />}` with

```tsx
        {tab === 'dependencies' && (
          <div className="flex flex-col gap-4">
            <section aria-labelledby="deps-products">
              <h3 id="deps-products" className="mb-2 text-xs font-medium uppercase text-ink-muted">Between {PRODUCT.plural.toLowerCase()}</h3>
              <ProductEdgesEditor product={product} />
            </section>
            <section aria-labelledby="deps-issues">
              <h3 id="deps-issues" className="mb-2 text-xs font-medium uppercase text-ink-muted">Between {ISSUE.plural.toLowerCase()}</h3>
              <DependencyPanel slug={product.slug} />
            </section>
          </div>
        )}
```

(import `ProductEdgesEditor` and add `PRODUCT` to the vocab import.) `ProductDetail.test.tsx`'s hooks mock gains `useCreateEdge`, `useDeleteEdge` (and `useRoadmapMap` if absent) so the tab renders; add one assertion that the Dependencies tab shows the "Between products" heading.

Run: `(cd web && npx vitest run src/features/roadmap/detail/ProductDetail.test.tsx)` → Expected: all passed.

- [ ] **Step 6: Gate and commit**

Web gate. Then:

```bash
git add web/src/api/pm.ts web/src/api/hooks.ts web/src/features/roadmap/detail/ProductEdgesEditor.tsx web/src/features/roadmap/detail/ProductEdgesEditor.test.tsx web/src/features/roadmap/detail/ProductDetail.tsx web/src/features/roadmap/detail/ProductDetail.test.tsx
git commit -F /tmp/row8a-t4.msg   # feat(roadmap): 8a task 4 — product dependency chips with add/remove on the Dependencies tab
```

---

### Task 5: Virtualize the leaf list

**Files:**
- Modify: `web/src/ui/PageContent.tsx` (`data-page-scroll` on the scrolling variant)
- Create: `web/src/features/roadmap/detail/leafRows.ts`, `leafRows.test.ts`
- Modify: `web/src/features/roadmap/detail/LeafList.tsx` (render loop)
- Create: `web/src/features/roadmap/detail/LeafList.test.tsx`

**Interfaces:**
- Consumes: `useVirtualizer` from `@tanstack/react-virtual` (already a dependency; the idiom is `features/sessions/transcript/TranscriptView.tsx:118-127`); `LeafRow` and the editors from Task 3.
- Produces: `leafRows(leaves: Leaf[]): LeafListRow[]` with `type LeafListRow = { kind: 'header'; key: string; label: string } | { kind: 'leaf'; key: string; leaf: Leaf }`; `PageContent` renders `data-page-scroll=""` when `scroll` is true.

- [ ] **Step 1: The pure row model, test first**

`web/src/features/roadmap/detail/leafRows.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import type { Leaf } from '@/api/roadmap'
import { leafRows } from './leafRows'

const leaf = (id: string, state: string): Leaf => ({
  id, ref: `o/r#${id}`, repo: 'o/r', number: Number(id), kind: 'issue', title: null, url: null, state,
  triageState: null, score: null, reach: null, impact: null, effort: null, confidence: null, tier: 'idle',
  blockedBy: [], parentRef: null, sessionStatus: null, ghCreatedAt: null, featureId: null,
})

describe('leafRows', () => {
  it('puts open leaves under an Open header and closed under a Closed one, each counted', () => {
    const rows = leafRows([leaf('1', 'open'), leaf('2', 'closed'), leaf('3', 'open')])
    expect(rows.map((r) => (r.kind === 'header' ? r.label : r.leaf.id))).toEqual(['Open · 2', '1', '3', 'Closed · 1', '2'])
    expect(rows.map((r) => r.key)).toEqual(['h:open', '1', '3', 'h:closed', '2'])
  })
  it('omits an empty section', () => {
    expect(leafRows([leaf('1', 'open')]).map((r) => r.key)).toEqual(['h:open', '1'])
    expect(leafRows([leaf('1', 'closed')]).map((r) => r.key)).toEqual(['h:closed', '1'])
    expect(leafRows([])).toEqual([])
  })
})
```

`web/src/features/roadmap/detail/leafRows.ts`:

```ts
// The virtualized leaf list is ONE array of rows — section headers and leaves
// alike — so a single virtualizer can window it. Open first, closed below
// (the leaves endpoint already sorts within each), headers carrying the counts.
import type { Leaf } from '@/api/roadmap'

export type LeafListRow =
  | { kind: 'header'; key: string; label: string }
  | { kind: 'leaf'; key: string; leaf: Leaf }

export function leafRows(leaves: Leaf[]): LeafListRow[] {
  const open = leaves.filter((l) => l.state === 'open')
  const closed = leaves.filter((l) => l.state !== 'open')
  const rows: LeafListRow[] = []
  if (open.length > 0) {
    rows.push({ kind: 'header', key: 'h:open', label: `Open · ${open.length}` })
    for (const l of open) rows.push({ kind: 'leaf', key: l.id, leaf: l })
  }
  if (closed.length > 0) {
    rows.push({ kind: 'header', key: 'h:closed', label: `Closed · ${closed.length}` })
    for (const l of closed) rows.push({ kind: 'leaf', key: l.id, leaf: l })
  }
  return rows
}
```

Run: `(cd web && npx vitest run src/features/roadmap/detail/leafRows.test.ts)` → Expected: 2 passed.

- [ ] **Step 2: Expose the page scroll element**

`web/src/ui/PageContent.tsx`: add `{...(scroll ? { 'data-page-scroll': '' } : {})}` to the `<div>` props (after `aria-busy`), with the comment `// A virtualized list inside a page finds its scroll container by this attribute (LeafList); a Workspace page (scroll=false) has none — its panes scroll themselves.` Add to `web/src/ui/Page.test.tsx` (or wherever `PageContent` is tested — grep `page-content` under `web/src/ui`) one assertion: the scrolling variant has the attribute, `scroll={false}` does not.

- [ ] **Step 3: Write the failing list test**

`web/src/features/roadmap/detail/LeafList.test.tsx`:

```tsx
// The bottom of the tree renders through a window: a capability with hundreds
// of issues mounts a handful of rows, and the two section headers travel with
// the leaves inside the same virtualizer.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Leaf } from '@/api/roadmap'
import { LeafList } from './LeafList'

const h = vi.hoisted(() => ({ leaves: [] as unknown[], isLoading: false, isError: false }))
vi.mock('@/api/hooks', () => ({
  useLeaves: () => ({ data: h.leaves, isLoading: h.isLoading, isError: h.isError }),
  useRemoveDependency: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useUpdateOverlay: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
}))

const leaf = (n: number, state: string): Leaf => ({
  id: `w${n}`, ref: `o/r#${n}`, repo: 'o/r', number: n, kind: 'issue', title: `Issue ${n}`, url: null, state,
  triageState: null, score: null, reach: null, impact: null, effort: null, confidence: null, tier: 'idle',
  blockedBy: [], parentRef: null, sessionStatus: null, ghCreatedAt: null, featureId: null,
})

// The virtualizer's 150 ms scroll-reset timer must fire before jsdom's window
// goes away (see TranscriptView.test.tsx for the same flush).
afterEach(() => new Promise((r) => setTimeout(r, 200)))

describe('LeafList', () => {
  it('renders both sections with their counts and every row of a short list', () => {
    h.leaves = [leaf(1, 'open'), leaf(2, 'open'), leaf(3, 'closed')]
    render(<div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div>)
    expect(screen.getByText('Open · 2')).toBeInTheDocument()
    expect(screen.getByText('Closed · 1')).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(5)   // 2 headers + 3 leaves
    expect(screen.getByText('#3 Issue 3')).toBeInTheDocument()
  })

  it('mounts a window, not the whole list, for a long one', () => {
    h.leaves = Array.from({ length: 300 }, (_, i) => leaf(i + 1, 'open'))
    render(<div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div>)
    const rendered = screen.getAllByRole('listitem').length
    expect(rendered).toBeGreaterThan(1)
    expect(rendered).toBeLessThan(100)
    expect(screen.queryByText('#300 Issue 300')).toBeNull()
  })

  it('says so when there is nothing', () => {
    h.leaves = []
    render(<LeafList scope={{ capability: 'f1' }} product={null} />)
    expect(screen.getByText('No issues here yet')).toBeInTheDocument()
  })
})
```

Run: `(cd web && npx vitest run src/features/roadmap/detail/LeafList.test.tsx)` → Expected: the first two FAIL (the current list renders `ul`/`li` per section: 3 `li`, and all 300 rows).

- [ ] **Step 4: Rewrite the render loop**

In `LeafList.tsx`, replace `LeafList`'s body from `const leaves = data ?? []` onward:

```tsx
  const leaves = data ?? []
  if (leaves.length === 0) return <EmptyState title={`No ${ISSUE.plural.toLowerCase()} here yet`} />
  return <VirtualRows rows={leafRows(leaves)} capabilities={capabilities} />
}

// jsdom reports a zero-height scroll container, so the window collapses to the
// first row plus `overscan`; the tests rely on that (short fixtures render
// whole, long ones do not).
const OVERSCAN = 10

function VirtualRows({ rows, capabilities }: { rows: LeafListRow[]; capabilities: { node: CapabilityNode; depth: number }[] }) {
  const listRef = useRef<HTMLDivElement>(null)
  // The page scrolls (`PageContent`), not this list — it is one panel among the
  // tiles and tabs above it — so the virtualizer measures against the page's
  // scroll element and offsets by where the list starts inside it.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => listRef.current?.closest<HTMLElement>('[data-page-scroll]') ?? null,
    estimateSize: (i) => (rows[i].kind === 'header' ? 28 : 44),
    overscan: OVERSCAN,
    getItemKey: (i) => rows[i].key,
    scrollMargin: listRef.current?.offsetTop ?? 0,
  })
  return (
    <div ref={listRef} role="list" aria-label={ISSUE.plural} className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
      {virtualizer.getVirtualItems().map((item) => {
        const row = rows[item.index]
        return (
          <div
            key={item.key}
            role="listitem"
            data-index={item.index}
            ref={virtualizer.measureElement}
            className="absolute left-0 top-0 w-full pb-1"
            style={{ transform: `translateY(${item.start - virtualizer.options.scrollMargin}px)` }}
          >
            {row.kind === 'header'
              ? <h3 className="mb-1 text-xs font-medium uppercase text-ink-muted">{row.label}</h3>
              : <LeafRow leaf={row.leaf} capabilities={capabilities} />}
          </div>
        )
      })}
    </div>
  )
}
```

and change `LeafRow`'s outer element from `<li className="flex flex-col gap-1 rounded-md …">` to `<div className="flex flex-col gap-1 rounded-md …">` (the listitem role now sits on the virtual wrapper). Imports: `useRef` from react, `useVirtualizer` from `@tanstack/react-virtual`, `leafRows, type LeafListRow` from `./leafRows`, `CapabilityNode` type. Delete the two `<section>` blocks and the `open`/`closed` filters (they moved into `leafRows`).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `(cd web && npx vitest run src/features/roadmap/detail/LeafList.test.tsx src/features/roadmap/detail/leafRows.test.ts src/features/roadmap src/ui)` → Expected: all passed. If the long-list test renders every row, the scroll element was not found: confirm `closest('[data-page-scroll]')` resolves in the test wrapper and that `getScrollElement` is not returning `null` at first render (react-virtual re-reads it on each render; a `null` element disables windowing).

- [ ] **Step 6: Playwright**

`(cd web && npm run build > /dev/null 2>&1 && npx playwright test e2e/roadmap.spec.ts 2>&1 | grep -E "passed|failed|skipped"); rm -rf web/dist` → Expected: passed (that spec drills into a capability's issues; if it asserts `role="list"` structure or `Open ·` copy, read the failure and fix the SPEC's selector, not the copy).

- [ ] **Step 7: Gate and commit**

Web gate. Then:

```bash
git add web/src/ui/PageContent.tsx web/src/ui/Page.test.tsx web/src/features/roadmap/detail/leafRows.ts web/src/features/roadmap/detail/leafRows.test.ts web/src/features/roadmap/detail/LeafList.tsx web/src/features/roadmap/detail/LeafList.test.tsx
git commit -F /tmp/row8a-t5.msg   # perf(roadmap): 8a task 5 — virtualize the leaf list through the page's scroll element
```

(Add the `PageContent` test file you actually touched if it is not `Page.test.tsx`.)

---

## Self-review

**Spec coverage (Phase 4 items, §13):**
- "Lazy virtualized leaves" — lazy loading exists (`useLeaves` on tab open); virtualization = Task 5. Pagination is not in the spec and is not added (a node's leaves are one response; the window is what keeps rendering cheap).
- "Score and capability edits" — Task 3 (§8 rows "Edit scores", "Reassign an issue's capability"; the overlay PATCH "gains UI for `featureId`", §10).
- "Dependency add/remove via the reconciler" — remove shipped in #1583; add is "a GitHub-side edit for now" per §8/§10 — deliberately not in this plan.
- "Initiative edits and step-back" — step-back shipped in #1583; edits = Tasks 1–2 (`PATCH /api/sdlc/initiatives/{slug}`, §10).
- "Product dependency — chip editor on the product's Dependencies tab (endpoints exist)" — Task 4.
- Not covered, by design: §6 keyboard `e`/`d`/`a` on list rows (list-column work, not Phase 4's text); §8 rows that write Markdown (capability add/rename/reparent/summary/links) and §9 — plan 8b; "drag ◆ row to another capability" — the dialog's primary select is the same write.

**Placeholder scan:** every code step carries code; the one "read X first" instruction (Checkbox's prop names, `_audits`' portal idiom, the PageContent test file name) points at a concrete file because guessing a signature into the plan would be worse.

**Type consistency:** `UpdateInitiativeInput` (Task 2) matches `InitiativePatchBody` (Task 1) field for field; `Leaf.featureId` is added in Task 3 and consumed by Task 3's chip and Task 5's fixtures; `LeafList({ scope, product })` is the signature from Task 3 that Task 5 keeps; `LeafRow({ leaf, capabilities })` is shared by Tasks 3 and 5; `CreateEdgeInput.kind: EdgeKind` matches the server's `PROJECT_EDGE_KINDS` check.
