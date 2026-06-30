# M-Track-7: Reorder Merged before deploy environments — Design

**Date:** 2026-06-30
**Status:** Approved (design); pending user review → implementation plan
**Program:** Visual Pipeline Track — see
`docs/superpowers/specs/2026-06-27-visual-pipeline-track-overview-design.md`. Corrects the node ordering
introduced by M-Track-2 (deploy nodes, PR #…) and adjusts the M-Track-3 tiles (PR #364) to match.

---

## 1. Goal

The stage track currently renders `CI → Ready → [Queue] → deploy:<env>… → Merged`, with **Merged last**.
That is backwards from reality: a PR's merge commit lands on the base branch **first**, and only then does
that commit promote through deploy environments (dev-qa → qa → prod). Reorder to:

```
CI → Ready → [Queue] → Merged → deploy:dev-qa → deploy:qa → deploy:prod
```

and change what the **Merged** node means accordingly (decided during brainstorming):
- **Merged = "the merge commit landed."** It goes `done` the instant `pr.stage == "merged"`.
- The deploy env nodes after it carry the promotion **frontier** (the existing per-env logic, relocated).
- The old M-Track-2 meaning ("Merged is `done` only when live in every env") is **removed**; "live
  everywhere" is now simply the last deploy node (prod) being `done`.

The M-Track-3 metric tiles reorder to match: **CI · Ready · Queue · Merged · Deploy**.

## 2. Why (the bug)

`build_stage_model` (track.py:95-101) appends `merged` **after** the deploy specs, and `place_pr_on_track`
(track.py:119-161) makes the `merged` node `done` only when the PR is live in every deploy env. So the
board implies "merged happens after prod deploy," the reverse of the git→deploy flow. Confirmed live: the
KinDash track shows `… kindash/admin-qa → kindash/qa → kindash/admin → Merged`.

## 3. Decisions (locked during brainstorming)

1. **Merged node:** `done` when `pr.stage == "merged"` (not "done when live everywhere"). No separate
   terminal/"Shipped" node — YAGNI; prod-deploy-done already conveys "live everywhere."
2. **No-deploy-env repo:** a merged PR's `merged` node (now the last node) renders **`done`** (all green
   = "shipped"). This **replaces** M-Track-1's behaviour of leaving it `active`.
3. **Tiles:** reorder `STAGE_BUCKETS` to `['ci','ready','queue','merged','deploy']` so the tiles read
   left-to-right with the track. Consequence (documented): a merged-but-still-deploying PR's *current*
   node is a deploy node, so it buckets as **Deploy**; the **Merged** tile counts merged PRs with no
   active deploy node (no-env repos, or merged-not-yet-deploying).
4. **Frontier stays on the deploy nodes:** first non-deployed env (in order) = `active`. Example — PR
   merged + live in dev-qa: `CI/Ready/Queue/Merged/dev-qa` green, **qa active**, prod pending.

## 4. Architecture — Backend (`server/pipeline/track.py`)

### 4.1 `build_stage_model` — node order
Move the `merged` spec **above** the deploy loop:
```
specs: ci, ready, [queue], merged, deploy:<env> (for env in deploy_envs)
```
`StageNode.order` is assigned by enumeration, so the renumbering is automatic. The `merged` node keeps
`kind="merged"`; deploy nodes keep `kind="deploy"`.

### 4.2 `place_pr_on_track` — merged-PR branch
Simplify the merged branch:
- `ci` / `ready` / `queue` / `merged` → **`done`** (a merged PR has landed; merged is no longer terminal).
- deploy nodes → **unchanged per-env frontier logic**: `done` if `env ∈ deployed` (where
  `deployed = set(pr.deployed_envs) ∪ ({"production"} if pr.deployed else ∅)` — the existing back-compat
  bridge), `active` if it is the frontier (first non-deployed deploy node, in order, after the furthest
  deployed), else `pending`.
- **Remove** the `merged`-node special case (the `has_deploy_nodes` "done only when all envs done" /
  M-Track-1 `active` branches). `merged` is now an ordinary pre-deploy node → always `done` for a merged PR.
- **Non-merged PRs:** unchanged — single forward cursor by `pr.stage`; `merged` + all deploy nodes
  `pending` (the cursor never reaches them).

`_STAGE_TO_NODE` is unchanged (a non-merged PR's cursor never targets `merged`).

## 5. Architecture — Frontend (`web/src/session/stageBucket.ts`)

Reorder one constant:
```
STAGE_BUCKETS = ['ci', 'ready', 'queue', 'merged', 'deploy']
```
`STAGE_BUCKET_LABELS`, `KIND_TO_BUCKET` (`merged→merged`, `deploy→deploy`), and `stageBucketOf`
(active-node-else-last-done) are **unchanged**. `StageTiles` already renders in `STAGE_BUCKETS` order, so
the tiles reorder to **CI · Ready · Queue · Merged · Deploy** with no component change.

## 6. Data flow (unchanged shape, changed order/meaning)

```
build_stage_model(repo, …, deploy_envs)  →  nodes ordered  ci,ready,[queue],merged,deploy:*
place_pr_on_track(pr, model)             →  merged PR: ci..merged done; deploy nodes per-env frontier
   → /api/prs node_states  →  StageTrack (renders nodes in order) + stageBucketOf → StageTiles
```

## 7. Error handling & edge cases

- **No deploy envs** → nodes `ci,ready,[queue],merged`; merged PR → all `done` (decision 2); non-merged →
  cursor as today.
- **Merged, deployed nowhere** (has deploy nodes, `deployed_envs` empty) → merged `done`, first deploy node
  `active` (frontier), rest `pending`.
- **Merged, live in a middle env but not earlier** (skipped/out-of-order deploy) → existing
  `last_deployed_order`/frontier rule handles it unchanged (frontier = first non-deployed after the
  furthest deployed).
- **`pr.deployed` back-compat** (`deployed=True` w/o `deployed_envs`) → bridge still adds `"production"`.
- A merged PR is never left without a green Merged node (the prior "pending merged" state is gone).

## 8. Testing (TDD) — NOTE: intentionally rewrites existing assertions

The M-Track-2 placement tests assert the OLD merged-terminal/frontier-pending semantics, and
`stageBucket.test.ts` asserts the OLD bucket order. These are **intended rewrites**, not regressions —
update them to the new order/meaning and call them out in the task so the reviewer treats them as
deliberate.

**Backend (`server/tests/` for track.py)**
- `build_stage_model`: node order is `ci, ready, [queue], merged, deploy:<env>…` (merged before deploys);
  orders monotonic.
- `place_pr_on_track` merged PR (with deploy envs): `ci..merged` = done; deploy nodes = per-env
  done/active(frontier)/pending; merged is **done** (not pending). Update the existing M-Track-2 cases
  that expected merged pending-until-all-deployed.
- `place_pr_on_track` merged PR, **no deploy envs**: merged = `done` (was `active`).
- Non-merged PR: unchanged (cursor; merged + deploy pending).
- Full backend track/pipeline suite green; whole `server/tests/` for the placement change (it's consumed
  by `all_prs`).

**Frontend (vitest)**
- `stageBucket.test.ts`: update the order assertion to `['ci','ready','queue','merged','deploy']`;
  `stageBucketOf` behaviour unchanged (re-verify active-node/last-done cases still pass).
- `StageTiles`: tiles render in order CI · Ready · Queue · Merged · Deploy.
- Full `vitest run` + `tsc -b --force` + `eslint --max-warnings 0`.

## 9. Files touched

**Modified**
- `server/pipeline/track.py` — `build_stage_model` node order; `place_pr_on_track` merged branch.
- `web/src/session/stageBucket.ts` — `STAGE_BUCKETS` order.
- Backend track placement tests (update M-Track-2 assertions + add new).
- `web/src/session/__tests__/stageBucket.test.ts` (order assertion) + `StageTiles` test (tile order).

## 10. Risks

- **Behaviour change to a shipped feature** — the placement-test and bucket-order rewrites are expected;
  any test that *unexpectedly* changes (beyond merged-ordering/semantics) is a real regression to
  investigate. `place_pr_on_track` is consumed by `all_prs` → run the whole backend suite.
- **Tile/track consistency** — `stageBucketOf` uses node order from the model, so the bucket of a PR
  follows the new order automatically; verify a merged+deploying PR buckets as Deploy and a no-env merged
  PR buckets as Merged (test both).
- **No signature change** — same function shapes; no caller fan-out (unlike M6/M3's async change).
