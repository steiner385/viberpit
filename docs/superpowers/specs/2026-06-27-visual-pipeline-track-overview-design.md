# Visual Pipeline Track — Program Overview

**Date:** 2026-06-27
**Status:** Approved (program decomposition); M-Track-1 spec follows separately
**Program:** ccswitch⨉Plotroom combined-app. Realises the deferred "Phase 3 frontend merge" —
bringing Plotroom's signature visual (per-PR horizontal stage track, grouped by repo, with summary
tiles) into ccswitch's Pipeline tab. See
`docs/superpowers/specs/2026-06-23-ccswitch-plotroom-merge-overview-design.md` and
`docs/superpowers/specs/2026-06-25-unified-pipeline-view-design.md` (the M5 list view this replaces).

---

## 1. Goal

Upgrade ccswitch's Pipeline tab from a **stacked, grouped-by-stage list** of tiny `PrPill` badges to a
**Plotroom-style visual board**: PRs grouped by repo, each rendered as a horizontal **stage track**
(circle nodes joined by progress connectors — e.g. CI → Ready → Queue → Deploy → Merged), with summary
tiles up top and, eventually, a modeling surface to tweak the track per project.

## 2. The core decision (locked during brainstorming)

The stage list must **not** be a static, hardcoded set. It is a **per-repo `StageModel` derived from
each repo's real CI/CD configuration** — never a hand-maintained YAML or a frontend constant.

- **Why auto-derive, not author:** the "project configuration" that should drive the stages is the
  CI/CD configuration that *already exists in GitHub* (workflows, required checks, deploy
  environments). A separate authored model is a parallel implementation of that truth — it drifts the
  moment a workflow changes (constitution VI, single source of truth; III, verify against reality). The
  user controls all their own repos, so a config file describing their own CI is redundant with the CI.
- **What we delete:** the `STAGE_SECTIONS` constant in `web/src/components/PipelineView.tsx:21-27` —
  that array *is* the static stage vocabulary being objected to. Stage definition moves to the backend,
  computed per repo; the frontend renders whatever nodes the backend emits.
- **What we reuse:** the existing flat `stage` string (`parked`/`ci`/`ready`/`queue`/`merged`) from
  `server/pipeline/stage.py` becomes the **cursor** that walks a repo's node list to light up nodes
  (done/active/failed/pending). No new stage classifier is written.

## 3. The `StageModel`

```
StageNode:  id      ("ci" | "ready" | "queue" | "deploy:<env>" | "merged")
            label   ("CI" from ciGraph workflowName; the env name; etc.)
            kind    (ci | gate | queue | deploy | merged)
            order   (position on the track)
            source  ("derived" | "override")   # provenance, shown honestly in the UI

StageModel (per repo): repo + ordered list[StageNode]
```

PR → node-state mapping (the visual: green / red / gray):

| Node state | Render        | Derived from                                              |
|------------|---------------|-----------------------------------------------------------|
| `done`     | green check   | PR's `stage` is *past* this node                          |
| `active`   | filled + ETA  | PR's `stage` **==** this node (CI node shows `eta_*`)      |
| `failed`   | red X         | `checks == failing` (CI) / `group-failed`·`unmergeable` (queue) |
| `pending`  | gray hollow   | node is downstream of the PR's current stage              |

## 4. Decomposition (four sub-projects, sequential)

Each milestone is its own spec → plan → PR cycle. M-Track-1 is the keystone; the rest are additive.

| Milestone     | Delivers                                                                                          | Depends on  |
|---------------|---------------------------------------------------------------------------------------------------|-------------|
| **M-Track-1** | Per-repo `StageModel` derivation (backend) + per-PR `StageTrack` (frontend), grouped by repo. Replaces the stacked list; deletes `STAGE_SECTIONS`. | —           |
| **M-Track-2** | Multi-environment deploy nodes — enumerate each repo's GitHub environments (qa→staging→prod) instead of the single hardcoded `production`. | M-Track-1   |
| **M-Track-3** | Summary tiles (CI RUNNING / IN QUEUE / FAILED / READY-OTHER) + left-nav shell (Health / Pipeline / Diagnose / Insights). Pure frontend over data already flowing. | M-Track-1   |
| **M-Track-4** | "Model & Edit" editor over a sparse per-repo **override** layer (DB row patching node label/order/visibility on the derived track). Configurability lands last, as a strict superset — never gates the visual win. | M-Track-1   |

## 5. Data we already have (anchors the auto-derive)

| Input                                              | Where today                                                            | Drives                          |
|----------------------------------------------------|------------------------------------------------------------------------|---------------------------------|
| Per-repo required-check prefixes + rollup workflow | `HistoryStore.ci_graph(repo)` — `server/pipeline/history.py:91`        | The `ci` node's identity + label |
| Live per-check status / conclusion / required      | `parse_eta_checks_response` — `server/github/pr_status.py`             | Per-node done/active/failed     |
| Production deployment + status                      | population query `deployments(environments:["production"])` — `pr_status.py` | Whether a `deploy` node exists  |
| Derived stage + queue substates                     | `derive_open_stage`/`derive_merged_stage`/`derive_queue_stage` — `server/pipeline/stage.py` | The PR cursor                   |
| Per-PR ETA                                          | `compute_progress` — `server/pipeline/estimator.py`                    | Active CI node annotation       |

**Missing (the new work):** an aggregation layer turning per-PR facts into a per-repo ordered node list
(`server/pipeline/track.py`), the track renderer (`web/src/components/StageTrack.tsx`), multi-env
enumeration (M-Track-2), and the override store + editor (M-Track-4).

## 6. Non-goals (program-wide)

- No in-repo `.pr-dashboard.yml` or any YAML stage config — the override layer (M-Track-4) is a sparse
  DB patch over the derived model, not a from-scratch authoring source.
- No re-derivation of stages — `track.py` consumes `stage.py` outputs; it never reimplements them.
- No new GitHub scopes or pollers — auto-derive runs off data the existing pollers already fetch.
