# Hold Ladder — 3b: hold fields on the leaf and issue APIs, the dialog's hold panel (Retry / Implement now), chips Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a person see why a roadmap issue is held and act on it from the Review-and-decide dialog: Retry (release the hold) or Implement now (dispatch a session), with server-computed consequence copy; and show the hold kind as a chip on the leaf row.

**Architecture:** Server first, small: the leaf and issue-detail serializers carry the four hold fields; `server/pm/explain.py` gains `hold_guidance` (the Retry / Implement-now consequences, same wire shape as the inbox's); manual dispatch of a held issue releases its owner-action hold with `signal = "implement"`. Web second: API types and one `useReleaseHold` hook; a `HOLD_LABEL` vocabulary; a hold chip on the leaf row; a hold panel in `IssueDispositionDialog` with Retry and Implement now rendered through the existing `ConsequenceList`. No new copy promises unbuilt behaviour (evidence and stronger-model holds get a panel and a chip only; their buttons arrive with series items 4 and 5).

**Tech Stack:** Python 3.12 / FastAPI (server slice) · React + TypeScript + React Query + vitest/RTL + Tailwind/shadcn (web).

**Spec:** `docs/superpowers/specs/2026-09-18-hold-ladder-design.md` §7 (dialog and chips) and §8 (Retry is person-only). Depends on series item 3a (branch `hold-ladder-3a`, PR #1956): `holds.RELEASE_SIGNALS`, `clear_hold`, `actor_for_request`, `POST /api/pm/work-items/{id}/hold/release`.

## Global Constraints

- Constitution VI: the chip/dialog labels live ONLY in `web/src/features/products/vocab.ts` (`HOLD_LABEL`); consequence copy is computed ONLY server-side in `server/pm/explain.py` and rendered ONLY by `ConsequenceList`; the release rule stays in `server/pm/holds.py`.
- Copy rules (roadmap plans): no em-dashes in new UI strings; nouns from `vocab.ts`; `lg` is the only Button size with a 44px target and is REQUIRED for a primary confirm in a Dialog footer.
- F6 lesson from the foundation: never describe behaviour a later series item delivers. Evidence: "Held for evidence"; stronger model: "Held for a stronger model"; decision: "Awaiting your decision" (resolved in the inbox once item 5 lands, say nothing about that yet).
- Audit every mutation; the assistant policy allowlist is deny-by-default (no new routes here beyond what 3a added).
- Web gates, all from `(cd web && …)` subshells: `npm run typecheck`, `npm run lint` (eslint `--max-warnings 0`), `npm run test` (the FULL vitest run: ~47 files mock `@/api/hooks`, a new hook in a shared component breaks tests you never touched), `npm run build`. Server gates for the server slice: ruff, mypy, `pytest server/tests/<named files>`.
- Server test invocation: `env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …`. `web/node_modules` exists in this worktree.
- Brand ratchet: no new `CCSWITCH_` literals. Commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` (that literal). Commit messages via a file and `git commit -F`; keep "release"/"deploy" and "prod"/"production" from appearing together in any command text.
- Branch: `hold-ladder-3b`, created from `hold-ladder-3a` (stacked); rebased onto `origin/master` once #1956 merges, before the PR opens.

## File structure

| File | Responsibility |
|---|---|
| `server/api/pm_tree.py` `_leaf` | + `holdKind`, `holdReason`, `holdBy`, `holdSetAt` |
| `server/pm/issue_disposition.py` `detail` | + the same four keys (NOT part of `version`) |
| `server/pm/explain.py` | + `hold_guidance(hold_kind, policy) -> {"retry": …, "implement": …}` |
| `server/api/pm.py` `get_work_item_issue` | + `guidance` (hold guidance under the owning product's effective policy, or null) |
| `server/api/automations.py` `backlog_runner_dispatch` | releases an `owner_action` hold on each dispatched ref (`signal = "implement"`) before spawning |
| `server/pm/holds.py` | `RELEASE_SIGNALS` + `"implement"` |
| `web/src/api/roadmap.ts` `Leaf`, `web/src/api/pm.ts` `WorkItemIssue` + `releaseHold` | types + fetcher |
| `web/src/api/hooks.ts` | `useReleaseHold(itemId)` |
| `web/src/features/products/vocab.ts` | `HOLD_LABEL`, `holdLabel()` |
| `web/src/features/roadmap/detail/LeafList.tsx` | hold chip + owner-action hint |
| `web/src/features/roadmap/detail/IssueDispositionDialog.tsx` | hold panel, Retry, Implement now, consequences |
| `docs/product/features/product-management-layer.md` | one sentence on the UI |
| Tests | `server/tests/test_pm_map_api.py` (leaves), `test_pm_api.py` (issue GET), `test_pm_explain.py`, `test_backlog_dispatch.py`; web: `LeafList.test.tsx`, `IssueDispositionDialog.test.tsx`, `vocab` test if one exists |

---

### Task 1: Server — hold fields on the leaf and the issue detail; `hold_guidance`; dispatch releases the hold

**Files:**
- Modify: `server/api/pm_tree.py:223-234`, `server/pm/issue_disposition.py:41-45`, `server/pm/explain.py`, `server/api/pm.py:1689-1695`, `server/api/automations.py:366-374`, `server/pm/holds.py` (`RELEASE_SIGNALS`)
- Test: `server/tests/test_pm_map_api.py`, `server/tests/test_pm_api.py`, `server/tests/test_pm_explain.py`, `server/tests/test_backlog_dispatch.py`, `server/tests/test_pm_holds.py`

**Interfaces:**
- Leaf dict and issue-detail dict gain `holdKind: str | None`, `holdReason: str | None`, `holdBy: str | None`, `holdSetAt: str | None` (ISO via `_aware`, as `_work_item_to_dict` does).
- `hold_guidance(*, hold_kind: str | None, policy: dict) -> dict | None` — `None` when `hold_kind` is not `"owner_action"`; otherwise `{"retry": Consequence-wire, "implement": Consequence-wire}` where:
  - retry: immediate `Step("Releases the hold; the item is queue-eligible again at its score.")`, then `Step(capitalize(dispatch_sentence(dispatch, min_status)), spawns_session=dispatch == "auto")`, `when = WHEN_NEXT_TICK if dispatch == "auto" else WHEN_NEVER`, reversal `"Hold it again from Edit details or the overlay."`, gate `None`.
  - implement: immediate `Step("Releases the hold and dispatches a coding session for this issue now.", spawns_session=True)`, then `Step("The session opens a PR; a merge closes the issue.", writes_github=True)`, `when = WHEN_NOW`, reversal `"Archive the session; the item returns to the queue."`, gate `None`. `available = policy dispatch != "off"`; when off, `unavailable_reason = "This product's dispatch is off; turn it on in the product's automation policy first."`.
- `GET /api/pm/work-items/{id}/issue` returns `{**detail(wi), "guidance": hold_guidance(hold_kind=wi.hold_kind, policy=<effective policy of the owning product, or effective_policy(None)>)}`. Resolve the product like the proposal list does: `owning_projects_by_repo(db, [wi.repo])` → slug → `load_policies(db)` (read `server/api/pm.py:1150-1176` for the exact helper names and copy them).
- `backlog_runner_dispatch` route: before `orch.backlog_runner_dispatch(body.refs)`, for each ref resolve the mirrored open issue and, if `hold_kind == "owner_action"`, `clear_hold(wi, actor=actor_for_request(request))` and audit `pm.hold.release` `{workItemId, kind, actorClass, signal: "implement"}` with `via="api"`; a `HoldPermissionError` (agent-class caller) → 422 `hold_not_permitted`; a `decision` hold → 422 `bad_hold` (as the Retry route does). `RELEASE_SIGNALS` gains `"implement"` (comment: "Implement now from the roadmap dialog: released and dispatched in one tap").

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_explain.py` — append:

```python
def test_hold_guidance_only_for_owner_action():
    from server.pm.explain import hold_guidance
    from server.automations.product_policy import effective_policy
    assert hold_guidance(hold_kind=None, policy=effective_policy(None)) is None
    assert hold_guidance(hold_kind="evidence", policy=effective_policy(None)) is None
    g = hold_guidance(hold_kind="owner_action", policy={**effective_policy(None), "dispatch": "auto"})
    assert set(g) == {"retry", "implement"}
    assert g["retry"]["when"] == "next_tick" and g["retry"]["then"][0]["spawnsSession"] is True
    assert g["implement"]["when"] == "now" and g["implement"]["immediate"][0]["spawnsSession"] is True
    off = hold_guidance(hold_kind="owner_action", policy={**effective_policy(None), "dispatch": "off"})
    assert off["implement"]["available"] is False and "dispatch is off" in off["implement"]["unavailableReason"]
    assert off["retry"]["when"] == "never"
```

`server/tests/test_pm_api.py` — append:

```python
def test_issue_detail_carries_hold_fields_and_guidance():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        r = c.get(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        assert r.json()["holdKind"] is None and r.json()["guidance"] is None
        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "owner_action", "holdReason": "needs the owner's keyring"},
                headers=IDENTITY_HEADERS)
        r = c.get(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS)
        body = r.json()
        assert body["holdKind"] == "owner_action" and body["holdReason"] == "needs the owner's keyring"
        assert body["holdBy"] == "testuser@example.com" and body["holdSetAt"]
        assert set(body["guidance"]) == {"retry", "implement"}
        # the hold is not part of the optimistic-concurrency version
        assert body["version"] == c.get(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS).json()["version"]
```

`server/tests/test_pm_map_api.py` — find the leaves test that seeds an item with a hold (Task 4 of 3a's foundation added `hold_kind="owner_action"` seeds) and assert the leaf dict carries `holdKind == "owner_action"`, `holdReason`, `holdBy`, `holdSetAt`; if no such test exists, add `test_leaves_carry_hold_fields` next to the existing leaves test using its seeding helpers.

`server/tests/test_backlog_dispatch.py` — read its harness first (it drives `POST /api/automations/backlog-runner/dispatch` with a fake orchestrator seam); add:

```python
def test_manual_dispatch_releases_an_owner_action_hold_first(...):
    # seed a mirrored open issue o/r#1 with hold_kind="owner_action" (a person's hold),
    # POST refs=["o/r#1"] with the person identity headers,
    # assert the spawn seam was called, the item's hold_kind is None afterwards,
    # and one pm.hold.release audit row has signal == "implement" and actorClass == "person".
    ...
```

Write the body against the file's real fixtures (the `...` above is a placeholder ONLY in this brief; the test you commit must be complete). Also: a `decision` hold → 422 `bad_hold` and no spawn.

`server/tests/test_pm_holds.py`: the `RELEASE_SIGNALS` assertion gains `"implement"`.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_explain.py server/tests/test_pm_api.py server/tests/test_pm_map_api.py server/tests/test_backlog_dispatch.py server/tests/test_pm_holds.py -q -k "hold_guidance or hold_fields or hold or implement"`
Expected: FAIL (`ImportError: hold_guidance`; missing keys).

- [ ] **Step 3: Implement**

`server/pm/explain.py` — append:

```python
def hold_guidance(*, hold_kind: str | None, policy: dict) -> dict | None:
    """``{"retry": …, "implement": …}`` for an owner_action hold (spec 2026-09-18 §7):
    what the Retry tap and the Implement-now tap do under the owning product's
    dispatch policy. ``None`` for any other kind — evidence and stronger-model holds
    exit on their own (series item 4) and a decision hold resolves in the inbox
    (item 5); neither has a button here yet, so neither gets copy."""
    if hold_kind != "owner_action":
        return None
    dispatch = str(policy.get("dispatch") or "off")
    min_status = str(policy.get("queue_min_status") or "ready")
    retry = Consequence(
        True, None,
        (Step("Releases the hold; the item is queue-eligible again at its score."),),
        (Step(capitalize(dispatch_sentence(dispatch, min_status)), spawns_session=dispatch == "auto"),),
        WHEN_NEXT_TICK if dispatch == "auto" else WHEN_NEVER,
        "Hold it again from Edit details or the overlay.", None)
    implement = Consequence(
        dispatch != "off",
        None if dispatch != "off" else
        "This product's dispatch is off; turn it on in the product's automation policy first.",
        (Step("Releases the hold and dispatches a coding session for this issue now.",
              spawns_session=True),),
        (Step("The session opens a PR; a merge closes the issue.", writes_github=True),),
        WHEN_NOW, "Archive the session; the item returns to the queue.", None)
    return {"retry": consequence_to_wire(retry), "implement": consequence_to_wire(implement)}
```

`server/pm/issue_disposition.py` `detail`: add the four keys (`holdSetAt` via the module's `_aware` if present, else import it from `server.pm.reconcile_inputs` the way `server/api/pm.py` does — check what `pm.py` imports as `_aware` and reuse that one). `server/api/pm_tree.py` `_leaf`: same four keys. `server/api/pm.py` `get_work_item_issue`: resolve the product policy as the proposal list does and add `"guidance"`. `server/api/automations.py` `backlog_runner_dispatch`: the release loop described above (import `split_ref`, `WorkItem`, `clear_hold`, `actor_for_request`, `actor_class`, `HoldPermissionError`, `record_audit`, `get_db`); the route gains `identity: str = Depends(current_identity)` and `db=Depends(get_db)`. `server/pm/holds.py`: `RELEASE_SIGNALS = ("board_edit", "body_edit", "retry", "overlay", "session", "implement")` with the comment.

- [ ] **Step 4: Run the tests, ruff, mypy**

Run: `… -m pytest server/tests/test_pm_explain.py server/tests/test_pm_api.py server/tests/test_pm_map_api.py server/tests/test_backlog_dispatch.py server/tests/test_pm_holds.py server/tests/test_assistant_policy.py -q && … -m ruff check server/ && … -m mypy server/api/pm.py server/api/pm_tree.py server/api/automations.py server/pm/explain.py server/pm/issue_disposition.py --ignore-missing-imports`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add server/api/pm_tree.py server/pm/issue_disposition.py server/pm/explain.py server/api/pm.py server/api/automations.py server/pm/holds.py server/tests/test_pm_explain.py server/tests/test_pm_api.py server/tests/test_pm_map_api.py server/tests/test_backlog_dispatch.py server/tests/test_pm_holds.py
printf 'feat(pm): hold fields on leaves and issue detail, hold guidance, dispatch clears the hold\n\nSpec 2026-09-18 §7 (server half of the dialog).\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git commit -F /tmp/c1.txt
```

---

### Task 2: Web API types, the release hook, and the hold vocabulary

**Files:**
- Modify: `web/src/api/roadmap.ts:129-158` (`Leaf`), `web/src/api/pm.ts:377-397` (`WorkItemIssue`, + `releaseHold`), `web/src/api/hooks.ts` (+ `useReleaseHold`), `web/src/features/products/vocab.ts` (+ `HOLD_LABEL`, `holdLabel`)
- Test: `web/src/features/products/vocab.test.ts` (create if absent)

**Interfaces:**
- `Leaf` gains `holdKind: string | null`, `holdReason: string | null`, `holdBy: string | null`, `holdSetAt: string | null`.
- `WorkItemIssue` gains the same four plus `guidance?: { retry: Consequence; implement: Consequence } | null` (import `Consequence` from `@/api/sdlc`).
- `releaseHold(itemId: string): Promise<{ ok: true; released: string | null; item: WorkItemDict }>` → `api.post('/pm/work-items/{id}/hold/release')`.
- `useReleaseHold(itemId)`: `useMutation`; `onSuccess` invalidates `['pm','leaves']`, `QK.roadmapMap`, `QK.queue`, `QK.workItemIssue(itemId)`; `meta: PM_MUTATION_META` (same as `useDispositionIssue`).
- `HOLD_LABEL: Record<string, string> = { owner_action: 'Held for the owner', decision: 'Awaiting your decision', evidence: 'Held for evidence', stronger_model: 'Held for a stronger model' }`; `holdLabel(kind) => kind ? (HOLD_LABEL[kind] ?? kind) : ''`.

- [ ] **Step 1: Write the failing test**

Create or extend `web/src/features/products/vocab.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { HOLD_LABEL, holdLabel } from './vocab'

describe('holdLabel', () => {
  it('names every hold kind without promising a later series item', () => {
    expect(Object.keys(HOLD_LABEL).sort()).toEqual(['decision', 'evidence', 'owner_action', 'stronger_model'])
    expect(holdLabel('owner_action')).toBe('Held for the owner')
    expect(holdLabel('evidence')).toBe('Held for evidence')
    expect(holdLabel(null)).toBe('')
    for (const v of Object.values(HOLD_LABEL)) expect(v).not.toMatch(/investigat|next run|tier/i)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/products/vocab.test.ts)`
Expected: FAIL (`HOLD_LABEL` is not exported).

- [ ] **Step 3: Implement**

`vocab.ts`, beside `REASON_LABEL`:

```ts
/** Why an issue is held (server `holdKind`, spec 2026-09-18 §3). Neutral on
 *  purpose: the evidence and stronger-model exits and the decision inbox are
 *  later series items, and a label must never promise them before they exist. */
export const HOLD_LABEL: Record<string, string> = {
  owner_action: 'Held for the owner',
  decision: 'Awaiting your decision',
  evidence: 'Held for evidence',
  stronger_model: 'Held for a stronger model',
}

export const holdLabel = (kind: string | null | undefined): string =>
  kind ? (HOLD_LABEL[kind] ?? kind) : ''
```

`roadmap.ts` `Leaf`: add the four fields with a one-line doc comment. `pm.ts`: extend `WorkItemIssue`; add:

```ts
export interface HoldReleaseResult { ok: true; released: string | null; item: Record<string, unknown> }
export function releaseHold(itemId: string): Promise<HoldReleaseResult> {
  return api.post(`/pm/work-items/${encodeURIComponent(itemId)}/hold/release`)
}
```

`hooks.ts`, after `useDispositionIssue`:

```ts
export function useReleaseHold(itemId: string) {
  const qc = useQueryClient()
  return useMutation<HoldReleaseResult, ApiRequestError, void>({
    mutationFn: () => releaseHold(itemId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.workItemIssue(itemId) })
      void qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.queue })
    },
    meta: PM_MUTATION_META,
  })
}
```

(add `releaseHold`, `HoldReleaseResult` to the `./pm` import list at the top of `hooks.ts`). Every test fixture that builds a `Leaf` or a `WorkItemIssue` literal must gain the four fields (`LeafList.test.tsx`'s `leaf()` helper, `IssueDispositionDialog.test.tsx`'s `detail`, `CapabilityDetail.test.tsx`, `NeedsYouList.test.tsx`, `leafRows.test.ts`, `__fixtures__`) — `npm run typecheck` will list them.

- [ ] **Step 4: Typecheck + the vocab test**

Run: `(cd web && npm run typecheck && npx vitest run src/features/products/vocab.test.ts)`
Expected: clean / PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/roadmap.ts web/src/api/pm.ts web/src/api/hooks.ts web/src/features/products/vocab.ts web/src/features/products/vocab.test.ts web/src/features/roadmap
printf 'feat(web): hold fields on leaf and issue types, useReleaseHold, hold labels\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git commit -F /tmp/c2.txt
```

---

### Task 3: The leaf row's hold chip and owner-action hint

**Files:**
- Modify: `web/src/features/roadmap/detail/LeafList.tsx:~84-112` (the chip block and the needs-attention hint)
- Test: `web/src/features/roadmap/detail/LeafList.test.tsx`

**Interfaces:**
- Chip: after the reason badge, `{leaf.state === 'open' && leaf.holdKind && <Badge tone={leaf.holdKind === 'owner_action' || leaf.holdKind === 'decision' ? 'warning' : 'info'} data-testid="leaf-hold">{holdLabel(leaf.holdKind)}</Badge>}`.
- Hint (the `needsAttention && leaf.kind === 'issue'` paragraph): when `leaf.holdKind === 'owner_action'`, the text is `` `Held for the owner${leaf.holdReason ? `: ${leaf.holdReason}` : ''}. Retry to return it to the queue, or open the issue to decide.` `` — checked BEFORE the existing `needs_spec` / `blocked` / default branches so a held row says why it is held.

- [ ] **Step 1: Write the failing tests**

Append to `LeafList.test.tsx` (its `leaf()` helper and the render harness `h` exist; read lines 1-60):

```ts
it('shows the hold kind as a chip and says why an owner-action hold needs you', () => {
  h.leaves = [
    leaf(1, 'open', { tier: 'needs-you', holdKind: 'owner_action', holdReason: 'needs the keyring', holdBy: 'tony@example.com', holdSetAt: '2026-09-18T00:00:00+00:00' }),
    leaf(2, 'open', { tier: 'idle', holdKind: 'evidence', holdReason: 'profile it', holdBy: 'session:s1', holdSetAt: '2026-09-18T00:00:00+00:00' }),
    leaf(3, 'closed', { holdKind: 'owner_action', holdReason: 'x', holdBy: 'y', holdSetAt: null }),
  ]
  render(...)  // the file's existing render call
  const chips = screen.getAllByTestId('leaf-hold')
  expect(chips.map((c) => c.textContent)).toEqual(['Held for the owner', 'Held for evidence'])  // closed rows show no chip
  expect(screen.getByText(/Held for the owner: needs the keyring\. Retry to return it to the queue/)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/roadmap/detail/LeafList.test.tsx)`
Expected: FAIL (no `leaf-hold` test id).

- [ ] **Step 3: Implement** the chip and the hint branch in `LeafRow` (import `holdLabel` from `@/features/products/vocab`).

- [ ] **Step 4: Run the file, then typecheck**

Run: `(cd web && npx vitest run src/features/roadmap/detail/LeafList.test.tsx && npm run typecheck)`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/roadmap/detail/LeafList.tsx web/src/features/roadmap/detail/LeafList.test.tsx
printf 'feat(web): hold chip and owner-action hint on the leaf row\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git commit -F /tmp/c3.txt
```

---

### Task 4: The dialog's hold panel with Retry and Implement now

**Files:**
- Modify: `web/src/features/roadmap/detail/IssueDispositionDialog.tsx`
- Test: `web/src/features/roadmap/detail/IssueDispositionDialog.test.tsx`

**Interfaces:**
- A `HoldPanel` section rendered in read mode when `data.holdKind` is set: a bordered box with `holdLabel(kind)` as its title, the reason, "by {holdBy}", and the set time (`new Date(holdSetAt).toLocaleString()`), `role="region" aria-label="Hold"`.
- For `owner_action` only, two buttons inside the panel: **Retry** (`Button size="lg"`, calls `useReleaseHold(itemId).mutateAsync()`; on success the dialog stays open and the panel disappears because the query was invalidated and refetched; error inline via `extractMutationErrorMessage`) and **Implement now** (`Button size="lg" variant="secondary"`, calls `useDispatchBacklogGroup().mutateAsync([data.ref])` — the issue detail must expose `ref`; add `"ref": f"{src.repo}#{src.number}"` to `issue_disposition.detail` in Task 1 if you did not, and to the `WorkItemIssue` type; on `status === 'spawned'`: `toast.success(...)` and render a link `Open session` to `sessionPath(session_id)`; on `no_account` / `refused` / other: inline `Banner tone="danger"` with `Dispatch failed: {status}`; disabled while either mutation is pending). Below the buttons, when `data.guidance` is present, a two-column `ConsequenceList` block like ProposalDetail's: `<ConsequenceList heading="Retry" consequence={guidance.retry} />` and `<ConsequenceList heading="Implement now" consequence={guidance.implement} />`; when `guidance.implement.available` is false, the Implement-now button is disabled and the list already shows the reason.
- For `evidence` / `stronger_model` / `decision`: the panel only (no buttons, no guidance).
- The existing Edit / Mark resolved / Won't do buttons stay; Retry does not close the dialog; Implement now does not close it either (the link is the exit).

- [ ] **Step 1: Write the failing tests**

Append to `IssueDispositionDialog.test.tsx` (`detail`, `reply`, `mount`, `click` exist; extend `detail` with `ref: 'o/r#1'` and null hold fields):

```ts
const held = { ...detail, holdKind: 'owner_action', holdReason: 'needs the keyring', holdBy: 'tony@example.com', holdSetAt: '2026-09-18T00:00:00+00:00',
  guidance: { retry: { available: true, unavailableReason: null, immediate: [{ text: 'Releases the hold', spawnsSession: false, writesGithub: false }], then: [], when: 'never', reversal: null, gate: null },
              implement: { available: true, unavailableReason: null, immediate: [{ text: 'Dispatches a coding session', spawnsSession: true, writesGithub: false }], then: [], when: 'now', reversal: null, gate: null } } }

it('shows the hold panel with consequences and releases the hold on Retry without closing', async () => {
  fetchMock.mockResolvedValueOnce(reply(held))
    .mockResolvedValueOnce(reply({ ok: true, released: 'owner_action', item: {} }))
    .mockResolvedValueOnce(reply({ ...detail, holdKind: null, guidance: null }))
  const { onClose } = mount()
  expect(await screen.findByRole('region', { name: 'Hold' })).toHaveTextContent('Held for the owner')
  expect(screen.getByText('needs the keyring')).toBeInTheDocument()
  expect(screen.getByText('Releases the hold')).toBeInTheDocument()
  click('Retry')
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  expect(fetchMock.mock.calls[1][0]).toMatch(/\/api\/pm\/work-items\/w1\/hold\/release$/)
  expect(fetchMock.mock.calls[1][1].method).toBe('POST')
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Hold' })).toBeNull())
  expect(onClose).not.toHaveBeenCalled()
})

it('dispatches a session on Implement now and links to it', async () => {
  fetchMock.mockResolvedValueOnce(reply(held))
    .mockResolvedValueOnce(reply({ status: 'spawned', session_id: 's-9', session_name: 'Fix keyring' }))
  mount()
  await screen.findByRole('region', { name: 'Hold' })
  click('Implement now')
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  expect(fetchMock.mock.calls[1][0]).toMatch(/\/api\/automations\/backlog-runner\/dispatch$/)
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ refs: ['o/r#1'] })
  expect(await screen.findByRole('link', { name: 'Open session' })).toHaveAttribute('href', expect.stringMatching(/\/s-9$/))
})

it('shows a refusal inline and renders no buttons for an evidence hold', async () => {
  fetchMock.mockResolvedValueOnce(reply(held)).mockResolvedValueOnce(reply({ status: 'no_account', session_id: null, session_name: null }))
  mount()
  await screen.findByRole('region', { name: 'Hold' })
  click('Implement now')
  expect(await screen.findByText(/Dispatch failed: no_account/)).toBeInTheDocument()
  cleanup()
  fetchMock.mockReset()
  fetchMock.mockResolvedValueOnce(reply({ ...held, holdKind: 'evidence', guidance: null }))
  mount()
  expect(await screen.findByRole('region', { name: 'Hold' })).toHaveTextContent('Held for evidence')
  expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Implement now' })).toBeNull()
})
```

Import `cleanup` from `@testing-library/react`. If `sonner`'s `toast` is not mocked in this file, mock it as `NewCapabilityDialog.test.tsx` does.

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/features/roadmap/detail/IssueDispositionDialog.test.tsx)`
Expected: FAIL (no Hold region).

- [ ] **Step 3: Implement** in `IssueDispositionDialog.tsx`: import `useReleaseHold`, `useDispatchBacklogGroup` from `@/api/hooks`, `ConsequenceList` from `../guidance/ConsequenceList`, `holdLabel` from `@/features/products/vocab`, `sessionPath` from `@/session/sessionNavState`, `Banner` from `@/ui/Banner`, `toast` from `sonner`. Keep the dialog's existing `pending` / `inFlight` discipline: fold `release.isPending || dispatch.isPending` into `pending`. State: `const [spawned, setSpawned] = useState<{ id: string; name: string | null } | null>(null)`, `const [dispatchError, setDispatchError] = useState<string | null>(null)`.

- [ ] **Step 4: Run the file, then the FULL web gates**

Run: `(cd web && npx vitest run src/features/roadmap/detail/IssueDispositionDialog.test.tsx && npm run typecheck && npm run lint && npm run test 2>&1 | tail -15 && npm run build 2>&1 | tail -3)`
Expected: all PASS / clean. A failure in a file you never touched that mocks `@/api/hooks` means a shared component gained a hook — extend that file's mock with `useReleaseHold` / `useDispatchBacklogGroup`.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/roadmap/detail/IssueDispositionDialog.tsx web/src/features/roadmap/detail/IssueDispositionDialog.test.tsx web/src
printf 'feat(web): hold panel with Retry and Implement now in the Review-and-decide dialog\n\nSpec 2026-09-18 §7. Closes the "go implement the issue" gap.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git commit -F /tmp/c4.txt
```

---

### Task 5: Docs, server gates, PR

- [ ] **Step 1: Docs** — in `docs/product/features/product-management-layer.md`'s Holds paragraph append: "The roadmap's Review-and-decide dialog shows the hold (kind, reason, who, when); an owner-action hold offers **Retry** (the release endpoint) and **Implement now** (manual dispatch, which releases the hold with `signal = implement` first), each with server-computed consequence copy from `server/pm/explain.py:hold_guidance`; the leaf row shows the kind as a chip." Update `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` §8's write-surface table with one row for the two buttons if that table lists dialog actions (read it first; skip if it does not).
- [ ] **Step 2: Gates** — `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/ -q`, `python scripts/brand_ratchet.py`; the web gates once more from a clean tree. The controller runs `server/tests/` detached.
- [ ] **Step 3: Commit** (`docs(pm): the dialog's hold panel`, with the trailer). The controller rebases onto `origin/master` (after #1956 merges), pushes, checks in-flight PRs, opens the PR non-draft with `Refs #1540`, and arms auto-merge.

## Self-review

**Spec coverage (3b slice):** §7 dialog shows kind/reason/author/time → T4; owner-action Retry + Implement now → T1 (server), T4; consequence copy through the explainer → T1, T4; leaf chip labels in vocab → T2, T3; hold fields on leaf/issue APIs → T1, T2. Deferred by design: "Investigating next run" copy and Investigate now (item 4); decision link to the proposal card (item 5).

**Type consistency:** `hold_guidance(*, hold_kind, policy)` (T1) feeds `guidance` on the issue GET (T1) typed as `{ retry; implement }` in `WorkItemIssue` (T2) and rendered in T4; `releaseHold` / `useReleaseHold` (T2) used by T4; `HOLD_LABEL` / `holdLabel` (T2) used by T3 and T4; the issue detail's `ref` (T1) is what T4 posts as `refs: [ref]`.

**Placeholders:** the `test_backlog_dispatch.py` body in T1 is marked as brief-only and must be written against the file's real fixtures; everything else carries its code.
