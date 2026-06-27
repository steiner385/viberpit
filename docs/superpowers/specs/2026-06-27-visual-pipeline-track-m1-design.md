# M-Track-1: Per-repo StageModel + Visual StageTrack — Design

**Date:** 2026-06-27
**Status:** Approved (design); pending implementation plan
**Program:** Visual Pipeline Track — see
`docs/superpowers/specs/2026-06-27-visual-pipeline-track-overview-design.md`. This is the **first and
keystone** milestone: backend derivation of a per-repo `StageModel` + the frontend horizontal
`StageTrack`, replacing the grouped-by-stage list from M5
(`docs/superpowers/specs/2026-06-25-unified-pipeline-view-design.md`).

---

## 1. Goal

Replace the stacked, grouped-by-stage list in `web/src/components/PipelineView.tsx` with a
Plotroom-style board: **PRs grouped by repo**, each PR rendered as a **horizontal stage track** whose
nodes are **derived per repo** from real CI/CD facts. Ship the smallest slice that delivers the visual
upgrade and honors "stages depend on project configuration."

In scope: the derived `StageModel`, the single `production` deploy node, the track renderer, repo
grouping, and preservation of the existing session click-through + Fix-PR behavior.

## 2. Non-goals (this milestone)

- **No multi-environment deploy nodes** — only the single `production` env we already query
  (`deploy:production`). Multi-env enumeration is M-Track-2.
- **No summary tiles / left-nav shell** — M-Track-3.
- **No override layer / "Model & Edit" editor** — M-Track-4. The `source` field on a node is always
  `"derived"` in this milestone.
- **No new stage classifier** — `track.py` consumes `stage.py`; it never re-derives stages.
- **No new pollers or GitHub scopes** — derivation runs off data the existing pollers fetch.

## 3. Architecture — Backend

### 3.1 New pure module `server/pipeline/track.py`

No I/O; fully unit-testable; the single source of truth for track *assembly* (distinct from stage
*derivation*, which stays in `stage.py`).

**Dataclasses** (frozen, with `to_dict`):

```python
@dataclass(frozen=True)
class StageNode:
    id: str            # "ci" | "ready" | "queue" | "deploy:production" | "merged"
    label: str
    kind: str          # "ci" | "gate" | "queue" | "deploy" | "merged"
    order: int
    source: str = "derived"

@dataclass(frozen=True)
class StageModel:
    repo: str
    nodes: tuple[StageNode, ...]

@dataclass(frozen=True)
class NodeStatus:
    id: str
    state: str         # "done" | "active" | "failed" | "pending"
```

**`build_stage_model(repo, *, ci_graph, has_queue, deploy_envs) -> StageModel`**
Assembles the ordered node list (the derive rule):

1. `ci` node — present if `ci_graph` is non-empty **or** the repo is otherwise known to gate on
   required checks. Label = `ci_graph.workflowName or "CI"`. `kind="ci"`, `order=0`.
2. `ready` node — always present. Label `"Ready"`, `kind="gate"`.
3. `queue` node — present iff `has_queue` (any PR in this repo has been seen with a merge-queue state).
   Label `"Queue"`, `kind="queue"`.
4. `deploy:<env>` node — one per `deploy_envs` (this milestone: `["production"]` → a single
   `deploy:production` labeled `"Deploy"`). `kind="deploy"`.
5. `merged` node — always present, terminal. Label `"Merged"`, `kind="merged"`.

A repo with no ciGraph, no queue, no deploy env collapses to the honest `CI → Ready → Merged`.

**`place_pr_on_track(pr, model) -> list[NodeStatus]`**
Maps the PR's `stage`/`checks` cursor onto the model's nodes. Defines a linear rank for the cursor
(`parked`/`ci` → at/ before the `ci` node; `ready` → at the `ready` node; `queue` → at the `queue`
node; `merged` → terminal; `deployed` flag advances past the deploy node). For each node:

- node rank `<` cursor rank → `done`
- node rank `==` cursor rank → `active`, unless the failing condition applies → `failed`
  (`checks == "failing"` on the `ci` node; queue substate `group-failed`/`unmergeable` on the `queue`
  node; `parked` cursor → the `ci` node renders `failed` when `checks == "failing"`, else `active`)
- node rank `>` cursor rank → `pending`

Pure mapping; reuses the stage strings, adds no new derivation (constitution VI).

### 3.2 Orchestrator: `all_repos_stage_models()`

`SessionOrchestrator.all_repos_stage_models() -> dict[str, StageModel]` — for each watched repo (the
existing `_pr_repos()` union), call `build_stage_model` with:

- `ci_graph` from `HistoryStore.ci_graph(repo)`,
- `has_queue` = whether any cached population PR for the repo carries a queue substate,
- `deploy_envs = ["production"]` (constant this milestone).

### 3.3 Wire across the daemon↔edge boundary (the recurring half-wired trap)

Memory: `remote-mode-path-asymmetry`, `session-record-dual-serializer`. All five layers:

1. `SessionOrchestrator.all_repos_stage_models()` — in-process (`server/sessions/orchestrator.py`).
2. `InProcessOrchestratorAdapter.all_repos_stage_models()` — passthrough (`server/sessions/facade.py`).
3. `RemoteOrchestrator.all_repos_stage_models()` — `self._client.call("all_repos_stage_models")`
   (`server/sessions/remote.py`).
4. RPC handler in `server/ipc/orchestrator_rpc.py` — register `all_repos_stage_models`, serialize via
   `StageModel.to_dict` (follow the `prs_for`/`all_prs` handler pattern).
5. Confirm the daemon's session-serving layer dispatches it.

### 3.4 Route `GET /api/pipeline/repos`

In `server/api/pipeline.py` (the existing pipeline router), behind `current_identity`. Returns a map
keyed by repo:

```json
{
  "owner/name": {
    "repo": "owner/name",
    "nodes": [
      {"id": "ci", "label": "CI", "kind": "ci", "order": 0, "source": "derived"},
      {"id": "ready", "label": "Ready", "kind": "gate", "order": 1, "source": "derived"},
      {"id": "deploy:production", "label": "Deploy", "kind": "deploy", "order": 2, "source": "derived"},
      {"id": "merged", "label": "Merged", "kind": "merged", "order": 3, "source": "derived"}
    ]
  }
}
```

`GET /api/prs` is unchanged — keeping the per-repo model on a sibling endpoint avoids duplicating one
model onto every PR row.

## 4. Architecture — Frontend

### 4.1 Types + hook

- `web/src/api/types.ts`: `StageNode`, `StageModel`, `NodeState = 'done'|'active'|'failed'|'pending'`.
- `web/src/api/hooks.ts`: `useStageModels()` → `useQuery` on `/pipeline/repos`,
  `refetchInterval ~30_000` (models change rarely — cheaper than the 10s PR poll), `staleTime ~15_000`.
  Returns `Record<string, StageModel>`.

### 4.2 New `web/src/components/StageTrack.tsx`

**Decision (resolved): keep the frontend dumb.** The per-node states come from the backend —
`/api/prs` PR objects carry a `node_states: [{id, state}]` array computed by `place_pr_on_track`. The
frontend never re-derives node states; it only renders. `StageTrack` takes `model.nodes` (definitions)
+ `pr.node_states` (states) and zips them by `id`. Visual:

- A row of circle nodes joined by connector lines; node `label` beneath each circle (Plotroom layout).
- Node fill/icon by state: `done` → green check, `active` → filled accent (CI node shows `~ETA` via
  the existing `formatEta`), `failed` → red X, `pending` → gray hollow.
- Connector segment before a node tints to the upstream node's color when that upstream is `done`.
- Reuses existing Tailwind color conventions from `PrPill.tsx` (green/red/amber/gray + dark variants).

> Design note: to satisfy "keep the frontend dumb" without bloating the per-repo model, the
> **node-state array is attached per PR** in the `/api/prs` response (`pr.node_states`), while the
> **node definitions** (label/kind/order) come from `/pipeline/repos`. The frontend zips them by `id`.
> This keeps all cursor/derivation logic backend-side (single source of truth) and the model
> un-duplicated.

### 4.3 `PipelineView.tsx` rewrite

- **Group by repo** (not stage). Reuse `GroupHeader` (collapsible + established z-index conventions,
  memory: `zindex-scale-and-list-stacking`). Repo sections ordered by urgency (a repo with any failing
  PR first), matching the screenshot's repo bars.
- PR row = `PrPill` (number/state badge) + title + `<StageTrack model={models[pr.repo]}
  states={pr.node_states} />`.
- **Preserve** existing behavior: session-linked rows click through to `/session/:id` and show the
  **Fix-PR** chip when failing + `isActionablePrState`; unsessioned rows open the GitHub `url`.
- **Delete `STAGE_SECTIONS`** and the stage-bucketing in the same PR — no half-dead vocabulary
  (working norm). Empty board → existing "No open PRs in your repos." state.
- A PR whose `repo` has no model yet (model still loading / repo not in the map) falls back to a
  minimal `CI → Ready → Merged` skeleton so a row never renders blank.

## 5. Data flow

```
watched repos ──▶ all_repos_stage_models()
  HistoryStore.ci_graph(repo) + has_queue + ["production"]
        │  build_stage_model(repo, …) → StageModel
        ▼
RPC / direct ──▶ GET /api/pipeline/repos ──▶ useStageModels() ─┐
                                                               ├─▶ PipelineView (group by repo)
population PRs ──▶ all_prs() + place_pr_on_track(pr, model) ──▶ │      └─▶ StageTrack(model, pr.node_states)
  GET /api/prs (now includes pr.node_states) ──▶ useAllPrs() ──┘
```

## 6. Error handling & edge cases

- **Repo with no ciGraph** → honest `CI → Ready → Merged`; never blank.
- **Model endpoint fails / still loading** → frontend skeleton track per §4.3; rows still render.
- **PR `stage` is `''`/unknown** → cursor treated as pre-CI; CI node `active` (or `failed` if checks
  failing). Never throws.
- **Remote vs direct mode** → `all_repos_stage_models()` identical in both; covered by a round-trip test.
- **`has_queue` flips off** (no PR currently queued) → the `queue` node simply isn't assembled; not an
  error. (Track shape can change between polls — acceptable.)

## 7. Testing (TDD)

**Backend**
- `build_stage_model`: node assembly across the matrix — ciGraph present/absent (CI label),
  `has_queue` true/false (queue node in/out), `deploy_envs` `["production"]`/`[]`; ordering + terminal
  `merged`; collapse to `CI → Ready → Merged`.
- `place_pr_on_track`: state mapping for each cursor (`parked`+failing → CI `failed`; `ci` → CI
  `active` + downstream `pending`; `ready` → CI `done`, Ready `active`; `merged` → all `done`;
  `deployed` flag advances past deploy node).
- `to_dict` round-trips for `StageNode`/`StageModel`/`NodeStatus`.
- `GET /api/pipeline/repos`: 401 without identity; 200 with; **real-response** shape (not just a
  dataclass round-trip — memory: `session-record-dual-serializer`).
- `GET /api/prs` now carries `node_states` — assert on a real GET response.
- `all_repos_stage_models()` RPC round-trip in **both** direct and remote modes.

**Frontend (vitest)**
- `StageTrack`: renders one circle per node; correct per-node state class (done/active/failed/pending);
  active CI node shows the ETA; connector tint follows `done` upstream.
- `PipelineView`: groups by repo; repo-with-failing-PR ordered first; collapse via `GroupHeader`;
  session-linked row navigates + shows Fix chip (failing+actionable); unsessioned row opens URL; empty
  board state; missing-model fallback skeleton.
- `useStageModels` hook fetch/shape.
- Verify with `npx tsc -b --force` + `eslint . --max-warnings 0` + full `vitest run` (memory:
  `ccswitch-frontend-real-typecheck`, `ccswitch-tsc-noemit-false-green`).

**Backend lint:** `ruff` + `mypy` + `pytest` (memory: `ccswitch-server-ci-lint-gates`).

## 8. Files touched (summary)

**New**
- `server/pipeline/track.py` — `StageNode`/`StageModel`/`NodeStatus`, `build_stage_model`,
  `place_pr_on_track`.
- `web/src/components/StageTrack.tsx` — the horizontal track renderer.

**Modified**
- `server/sessions/orchestrator.py` — `all_repos_stage_models()`; attach `node_states` to PRs in
  `all_prs()`.
- `server/github/pr_status.py` — `PullRequestInfo` gains `node_states` (additive, nullable-by-default;
  `to_dict`/`from_dict`).
- `server/sessions/facade.py`, `server/sessions/remote.py`, `server/ipc/orchestrator_rpc.py` — wire
  `all_repos_stage_models()` across daemon↔edge.
- `server/api/pipeline.py` — `GET /pipeline/repos` route.
- `web/src/api/types.ts` — `StageNode`/`StageModel`/`NodeState`; `node_states` on `PullRequestInfo`.
- `web/src/api/hooks.ts` — `useStageModels()`.
- `web/src/components/PipelineView.tsx` — rewrite: group by repo, render `StageTrack`, delete
  `STAGE_SECTIONS`.

## 9. Risks

- **Half-wired daemon↔edge** (recurring trap) — mitigated by the §3.3 five-layer checklist + a
  remote-mode round-trip test.
- **Dual serializer drift** — `node_states` must be added to **both** `PullRequestInfo.to_dict` and any
  manual `_record_to_dict`-style builder on the PR path; assert on a real GET (memory:
  `pullrequestinfo-todict-exact-shape-test`, `session-record-dual-serializer`). An exact-dict equality
  test on the PR payload will break and must be updated.
- **Parallel-implementation drift** — cursor logic lives only in `place_pr_on_track` (backend); the
  frontend never re-derives node states (memory: eliminating-parallel-implementations).
- **Full-suite-only failures** — per-file runs may pass while a sequence/exact-shape assert elsewhere
  breaks; run full `pytest` + full `vitest` before pushing (memory:
  `new-translator-event-dual-path-and-sequence-tests`, `git-checkout-ours-partial-automerge-trap`).
