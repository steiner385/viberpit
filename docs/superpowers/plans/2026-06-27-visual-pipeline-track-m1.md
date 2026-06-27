# Visual Pipeline Track — M-Track-1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the grouped-by-stage Pipeline list with a Plotroom-style board — PRs grouped by repo, each rendered as a horizontal stage track whose nodes are auto-derived per repo from real CI/CD facts.

**Architecture:** A new pure backend module `server/pipeline/track.py` assembles a per-repo `StageModel` (ordered nodes: ci → ready → queue? → deploy? → merged) from the migrated ciGraph + population facts, and maps each PR's existing `stage`/`checks` cursor onto those nodes (`node_states`). The orchestrator exposes `all_repos_stage_models()` (new endpoint `GET /api/pipeline/repos`) and attaches `node_states` to each PR in `all_prs()`. The frontend renders a dumb `StageTrack` by zipping node definitions (from `/pipeline/repos`) with per-PR states (from `/api/prs`).

**Tech Stack:** Python 3 / FastAPI / dataclasses / pytest (backend); React + TS + Tailwind + TanStack Query + vitest (frontend).

## Global Constraints

- Reuse `server/pipeline/stage.py` outputs as the cursor; never re-derive stages (constitution VI). `track.py` adds assembly + placement only.
- Node-state derivation lives **only** in `place_pr_on_track` (backend). The frontend never recomputes states — it renders what the API returns (single source of truth).
- Wire `all_repos_stage_models()` across **all five** daemon↔edge layers (orchestrator → facade → remote → rpc handler → registration) with a remote-mode round-trip test (memory: remote-mode-path-asymmetry).
- New model columns / wire fields are additive + nullable/defaulted; assert serialization on a **real** GET/RPC response, not just a dataclass round-trip (memory: session-record-dual-serializer, pullrequestinfo-todict-exact-shape-test).
- Backend gate: `ruff` + `mypy` + `pytest` (full suite before push). Frontend gate: `npx tsc -b --force` + `eslint . --max-warnings 0` + full `vitest run` (memory: ccswitch-frontend-real-typecheck, ccswitch-tsc-noemit-false-green).
- Run backend tests against the prebuilt env: `<main-checkout>/.venv/bin/python -m pytest` with `PYTHONPATH=<worktree>/src`; for API auth tests use `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1` (memory: ccswitch-worktree-backend-pythonpath).

---

### Task 1: `StageNode` / `StageModel` / `NodeStatus` dataclasses

**Files:**
- Create: `server/pipeline/track.py`
- Test: `server/tests/test_track_model.py`

**Interfaces:**
- Produces: `StageNode(id:str, label:str, kind:str, order:int, source:str="derived")`, `StageModel(repo:str, nodes:tuple[StageNode,...])`, `NodeStatus(id:str, state:str)` — each frozen with `to_dict()` and `from_dict()`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_track_model.py
from server.pipeline.track import StageNode, StageModel, NodeStatus


def test_stage_node_roundtrip():
    n = StageNode(id="ci", label="CI", kind="ci", order=0)
    assert n.to_dict() == {"id": "ci", "label": "CI", "kind": "ci", "order": 0, "source": "derived"}
    assert StageNode.from_dict(n.to_dict()) == n


def test_stage_model_roundtrip():
    m = StageModel(repo="o/r", nodes=(StageNode("ci", "CI", "ci", 0), StageNode("merged", "Merged", "merged", 1)))
    d = m.to_dict()
    assert d["repo"] == "o/r"
    assert [x["id"] for x in d["nodes"]] == ["ci", "merged"]
    assert StageModel.from_dict(d) == m


def test_node_status_roundtrip():
    s = NodeStatus(id="ci", state="active")
    assert s.to_dict() == {"id": "ci", "state": "active"}
    assert NodeStatus.from_dict(s.to_dict()) == s
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_track_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.pipeline.track'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/pipeline/track.py
"""Per-repo stage-track assembly + per-PR node placement.

Distinct from server/pipeline/stage.py: stage.py *derives* a PR's single stage
string; this module *assembles* a repo's ordered node list (StageModel) and maps
a PR's stage cursor onto those nodes (NodeStatus[]). It consumes stage.py's
output as a cursor — it never re-derives stages (constitution VI).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.github.pr_status import PullRequestInfo


@dataclass(frozen=True)
class StageNode:
    id: str
    label: str
    kind: str  # "ci" | "gate" | "queue" | "deploy" | "merged"
    order: int
    source: str = "derived"  # "derived" | "override"

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "kind": self.kind,
                "order": self.order, "source": self.source}

    @classmethod
    def from_dict(cls, d: dict) -> "StageNode":
        return cls(id=d["id"], label=d["label"], kind=d["kind"],
                   order=d["order"], source=d.get("source", "derived"))


@dataclass(frozen=True)
class StageModel:
    repo: str
    nodes: tuple[StageNode, ...]

    def to_dict(self) -> dict:
        return {"repo": self.repo, "nodes": [n.to_dict() for n in self.nodes]}

    @classmethod
    def from_dict(cls, d: dict) -> "StageModel":
        return cls(repo=d["repo"], nodes=tuple(StageNode.from_dict(n) for n in d.get("nodes", [])))


@dataclass(frozen=True)
class NodeStatus:
    id: str
    state: str  # "done" | "active" | "failed" | "pending"

    def to_dict(self) -> dict:
        return {"id": self.id, "state": self.state}

    @classmethod
    def from_dict(cls, d: dict) -> "NodeStatus":
        return cls(id=d["id"], state=d["state"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_track_model.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/track.py server/tests/test_track_model.py
git commit -m "feat(pipeline): StageNode/StageModel/NodeStatus dataclasses"
```

---

### Task 2: `build_stage_model` — per-repo node assembly

**Files:**
- Modify: `server/pipeline/track.py`
- Test: `server/tests/test_build_stage_model.py`

**Interfaces:**
- Consumes: `StageNode`, `StageModel` (Task 1).
- Produces: `build_stage_model(repo: str, *, ci_graph: dict | None, has_queue: bool, has_deploy: bool) -> StageModel`.

Assembly rule (ordered): `ci` (label = `ci_graph["workflowName"]` or `"CI"`) → `ready` (label "Ready", kind "gate") → `queue` (only if `has_queue`) → `deploy:production` (label "Deploy", kind "deploy", only if `has_deploy`) → `merged` (terminal). `order` is the 0-based index in the assembled list.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_build_stage_model.py
from server.pipeline.track import build_stage_model


def _ids(m):
    return [n.id for n in m.nodes]


def test_minimal_repo_collapses_to_ci_ready_merged():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, has_deploy=False)
    assert _ids(m) == ["ci", "ready", "merged"]
    assert [n.order for n in m.nodes] == [0, 1, 2]
    assert m.nodes[0].label == "CI"  # default when no ciGraph


def test_ci_label_from_cigraph_workflow_name():
    m = build_stage_model("o/r", ci_graph={"workflowName": "Tests"}, has_queue=False, has_deploy=False)
    assert m.nodes[0].label == "Tests"


def test_queue_node_only_when_has_queue():
    m = build_stage_model("o/r", ci_graph=None, has_queue=True, has_deploy=False)
    assert _ids(m) == ["ci", "ready", "queue", "merged"]


def test_deploy_node_only_when_has_deploy():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, has_deploy=True)
    assert _ids(m) == ["ci", "ready", "deploy:production", "merged"]
    assert m.nodes[2].kind == "deploy"


def test_full_track_order():
    m = build_stage_model("o/r", ci_graph={"workflowName": "CI"}, has_queue=True, has_deploy=True)
    assert _ids(m) == ["ci", "ready", "queue", "deploy:production", "merged"]
    assert [n.order for n in m.nodes] == [0, 1, 2, 3, 4]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_build_stage_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_stage_model'`

- [ ] **Step 3: Write minimal implementation** (append to `server/pipeline/track.py`)

```python
def build_stage_model(
    repo: str,
    *,
    ci_graph: dict | None,
    has_queue: bool,
    has_deploy: bool,
) -> StageModel:
    """Assemble the ordered StageModel for a repo from its real CI/CD facts."""
    specs: list[tuple[str, str, str]] = []  # (id, label, kind)
    ci_label = (ci_graph or {}).get("workflowName") or "CI"
    specs.append(("ci", ci_label, "ci"))
    specs.append(("ready", "Ready", "gate"))
    if has_queue:
        specs.append(("queue", "Queue", "queue"))
    if has_deploy:
        specs.append(("deploy:production", "Deploy", "deploy"))
    specs.append(("merged", "Merged", "merged"))
    nodes = tuple(
        StageNode(id=sid, label=label, kind=kind, order=i)
        for i, (sid, label, kind) in enumerate(specs)
    )
    return StageModel(repo=repo, nodes=nodes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_build_stage_model.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/track.py server/tests/test_build_stage_model.py
git commit -m "feat(pipeline): build_stage_model per-repo node assembly"
```

---

### Task 3: `place_pr_on_track` — map a PR cursor onto nodes

**Files:**
- Modify: `server/pipeline/track.py`
- Test: `server/tests/test_place_pr_on_track.py`

**Interfaces:**
- Consumes: `StageModel`, `NodeStatus`, `build_stage_model`.
- Produces: `place_pr_on_track(pr: "PullRequestInfo", model: StageModel) -> list[NodeStatus]`.

Cursor rank: map the PR's `stage` to a node id — `parked`/`ci`/`""` → cursor at `ci`; `ready` → at `ready`; `queue` → at `queue`; `merged` → at `merged` (and if `pr.deployed`, cursor advances to `merged` past the deploy node, i.e. deploy node = `done`). For each node by `order`: `< cursor` → `done`; `== cursor` → `active`, but `failed` when `pr.checks == "failing"` and node.kind == "ci" (or node.kind == "queue"); `> cursor` → `pending`. A node id not reachable for this model (e.g. cursor `queue` but no queue node) clamps the cursor to the nearest existing earlier node.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_place_pr_on_track.py
from server.github.pr_status import PullRequestInfo
from server.pipeline.track import build_stage_model, place_pr_on_track


def _states(pr, model):
    return {s.id: s.state for s in place_pr_on_track(pr, model)}


def _pr(**kw):
    base = dict(number=1, url="u", state="open", checks="none", title="t")
    base.update(kw)
    return PullRequestInfo(**base)


def test_ci_pending_pr_lights_ci_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, has_deploy=False)
    st = _states(_pr(stage="ci", checks="pending"), m)
    assert st == {"ci": "active", "ready": "pending", "merged": "pending"}


def test_parked_failing_pr_lights_ci_failed():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, has_deploy=False)
    st = _states(_pr(stage="parked", checks="failing"), m)
    assert st["ci"] == "failed"


def test_ready_pr_ci_done_ready_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, has_deploy=False)
    st = _states(_pr(stage="ready", checks="passing"), m)
    assert st == {"ci": "done", "ready": "active", "merged": "pending"}


def test_merged_pr_all_done():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, has_deploy=True)
    st = _states(_pr(stage="merged", state="merged", merged=True, deployed=True), m)
    assert st == {"ci": "done", "ready": "done", "deploy:production": "done", "merged": "done"}


def test_merged_not_deployed_leaves_deploy_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, has_deploy=True)
    st = _states(_pr(stage="merged", state="merged", merged=True, deployed=False), m)
    assert st["deploy:production"] == "active"
    assert st["merged"] == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_place_pr_on_track.py -v`
Expected: FAIL with `ImportError: cannot import name 'place_pr_on_track'`

- [ ] **Step 3: Write minimal implementation** (append to `server/pipeline/track.py`)

```python
# Cursor node id by PR stage. 'parked'/'ci'/'' all sit at the CI node.
_STAGE_TO_NODE = {"": "ci", "parked": "ci", "ci": "ci", "ready": "ready",
                  "queue": "queue", "merged": "merged"}


def place_pr_on_track(pr: "PullRequestInfo", model: StageModel) -> list["NodeStatus"]:
    ids = [n.id for n in model.nodes]
    order_by_id = {n.id: n.order for n in model.nodes}
    kind_by_id = {n.id: n.kind for n in model.nodes}

    target = _STAGE_TO_NODE.get(pr.stage, "ci")
    # Clamp to the nearest existing earlier node if the target node isn't in this model.
    if target not in order_by_id:
        target = "ci" if "ci" in order_by_id else ids[0]
    cursor = order_by_id[target]
    # A merged+deployed PR has cleared the deploy node too.
    if pr.stage == "merged" and pr.deployed and "merged" in order_by_id:
        cursor = order_by_id["merged"]

    out: list[NodeStatus] = []
    for n in model.nodes:
        if n.order < cursor:
            state = "done"
        elif n.order == cursor:
            failing = pr.checks == "failing" and n.kind in ("ci", "queue")
            state = "failed" if failing else "active"
        else:
            state = "pending"
        out.append(NodeStatus(id=n.id, state=state))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_place_pr_on_track.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/track.py server/tests/test_place_pr_on_track.py
git commit -m "feat(pipeline): place_pr_on_track maps PR cursor onto nodes"
```

---

### Task 4: `PipelinePr` carries `node_states`

**Files:**
- Modify: `server/github/pr_status.py:94-115` (`PipelinePr` dataclass + `to_dict`/`from_dict`)
- Test: `server/tests/test_pipeline_pr_node_states.py`

**Interfaces:**
- Consumes: `NodeStatus` (Task 1).
- Produces: `PipelinePr(pr, session_id, session_name, node_states: tuple[NodeStatus, ...] = ())`; `to_dict()` adds `"node_states": [{id,state},...]`; `from_dict` rebuilds them.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pipeline_pr_node_states.py
from server.github.pr_status import PullRequestInfo, PipelinePr
from server.pipeline.track import NodeStatus


def _pr():
    return PullRequestInfo(number=1, url="u", state="open", checks="none", title="t", repo="o/r")


def test_node_states_in_to_dict_and_roundtrip():
    p = PipelinePr(pr=_pr(), session_id="s", session_name="n",
                   node_states=(NodeStatus("ci", "active"), NodeStatus("merged", "pending")))
    d = p.to_dict()
    assert d["node_states"] == [{"id": "ci", "state": "active"}, {"id": "merged", "state": "pending"}]
    assert PipelinePr.from_dict(d).node_states == p.node_states


def test_node_states_default_empty():
    assert PipelinePr(pr=_pr()).to_dict()["node_states"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_pipeline_pr_node_states.py -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'node_states'`

- [ ] **Step 3: Write minimal implementation**

Edit `server/github/pr_status.py`. Add the field to `PipelinePr` and update both serializers (use a local import inside the methods to avoid a circular import with `server.pipeline.track`):

```python
@dataclass(frozen=True)
class PipelinePr:
    """A population PR plus the ccswitch session that authored it (if any)."""
    pr: PullRequestInfo
    session_id: str | None = None
    session_name: str | None = None
    node_states: tuple = ()  # tuple[NodeStatus, ...]; per-repo track placement

    def to_dict(self) -> dict:
        d = self.pr.to_dict()
        d["session"] = (
            {"id": self.session_id, "name": self.session_name}
            if self.session_id else None
        )
        d["node_states"] = [s.to_dict() for s in self.node_states]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PipelinePr":
        from server.pipeline.track import NodeStatus
        sess = d.get("session") or {}
        return cls(
            pr=PullRequestInfo.from_dict(d),
            session_id=sess.get("id"),
            session_name=sess.get("name"),
            node_states=tuple(NodeStatus.from_dict(s) for s in d.get("node_states", [])),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_pipeline_pr_node_states.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_status.py server/tests/test_pipeline_pr_node_states.py
git commit -m "feat(pipeline): PipelinePr carries node_states"
```

---

### Task 5: Orchestrator `all_repos_stage_models()` + node_states in `all_prs()`

**Files:**
- Modify: `server/sessions/orchestrator.py:1129-1150` (`all_prs`) and add `all_repos_stage_models` nearby.
- Test: `server/tests/test_orchestrator_stage_models.py`

**Interfaces:**
- Consumes: `build_stage_model`, `place_pr_on_track` (Tasks 2-3); `self._pr_population.all()`, `self._pr_repos()`; `HistoryStore.ci_graph` via the orchestrator's history store.
- Produces: `SessionOrchestrator.all_repos_stage_models() -> dict[str, StageModel]`; `all_prs()` now sets `node_states` on each `PipelinePr`.

`has_queue` per repo = any cached PR has `stage == "queue"`. `has_deploy` per repo = any cached PR has `deployed is True`. `ci_graph` from the orchestrator's history store if present, else `None` (guard `getattr`; the store may be absent in unit fixtures).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_stage_models.py
import asyncio
from server.github.pr_status import PullRequestInfo
from server.pipeline.track import StageModel


class _FakePop:
    def __init__(self, data): self._d = data
    def all(self): return self._d


def _mk_orch(monkeypatch, population):
    from server.sessions import orchestrator as om
    orch = object.__new__(om.SessionOrchestrator)
    orch._pr_population = _FakePop(population)
    orch._records = {}
    orch._history = None  # no ci_graph in this fixture
    return orch


def test_all_repos_stage_models_per_repo(monkeypatch):
    pr = PullRequestInfo(number=1, url="u", state="open", checks="pending", title="t", repo="o/r", stage="ci")
    orch = _mk_orch(monkeypatch, {"o/r": (pr,)})
    models = orch.all_repos_stage_models()
    assert isinstance(models["o/r"], StageModel)
    assert [n.id for n in models["o/r"].nodes] == ["ci", "ready", "merged"]


def test_all_repos_stage_models_adds_queue_and_deploy(monkeypatch):
    q = PullRequestInfo(number=2, url="u", state="open", checks="none", title="t", repo="o/r", stage="queue")
    d = PullRequestInfo(number=3, url="u", state="merged", checks="passing", title="t", repo="o/r", stage="merged", deployed=True)
    orch = _mk_orch(monkeypatch, {"o/r": (q, d)})
    models = orch.all_repos_stage_models()
    assert [n.id for n in models["o/r"].nodes] == ["ci", "ready", "queue", "deploy:production", "merged"]


def test_all_prs_attaches_node_states(monkeypatch):
    pr = PullRequestInfo(number=1, url="u", state="open", checks="pending", title="t", repo="o/r", stage="ci")
    orch = _mk_orch(monkeypatch, {"o/r": (pr,)})
    items = orch.all_prs()
    assert items[0].node_states  # non-empty
    assert {s.id: s.state for s in items[0].node_states}["ci"] == "active"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_orchestrator_stage_models.py -v`
Expected: FAIL with `AttributeError: 'SessionOrchestrator' object has no attribute 'all_repos_stage_models'`

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/orchestrator.py`, add the new method and update `all_prs`. Replace the `all_prs` body's `out.append(...)` loop to compute models once and place each PR:

```python
def all_repos_stage_models(self) -> "dict[str, StageModel]":
    """Per-repo StageModel for every watched repo (derived from CI/CD facts)."""
    from server.pipeline.track import build_stage_model
    models: dict[str, StageModel] = {}
    pop = self._pr_population.all()
    history = getattr(self, "_history", None)
    for repo in self._pr_repos() if self._records else list(pop.keys()):
        prs = pop.get(repo, ())
        has_queue = any(p.stage == "queue" for p in prs)
        has_deploy = any(p.deployed for p in prs)
        ci_graph = history.ci_graph(repo) if history is not None else None
        models[repo] = build_stage_model(
            repo, ci_graph=ci_graph, has_queue=has_queue, has_deploy=has_deploy
        )
    return models
```

And in `all_prs`, after building `sess_by_pr`, compute models and node_states:

```python
    from server.pipeline.track import place_pr_on_track
    models = self.all_repos_stage_models()
    order = {"parked": 0, "ci": 1, "ready": 2, "queue": 3, "merged": 4}
    out: list[PipelinePr] = []
    for repo, prs in self._pr_population.all().items():
        model = models.get(repo)
        for info in prs:
            sid, sname = sess_by_pr.get((repo, info.number), (None, None))
            states = tuple(place_pr_on_track(info, model)) if model else ()
            out.append(PipelinePr(pr=info, session_id=sid, session_name=sname, node_states=states))
    out.sort(key=lambda p: (order.get(p.pr.stage, 99), p.pr.number))
    return out
```

> Note: `all_repos_stage_models` iterates `pop.keys()` when `_records` is empty (unit fixtures); the production path goes through `_pr_repos()`. Confirm the real orchestrator stores its history as `self._history` — if the attribute differs, use the actual name (grep `ci_graph(` / `HistoryStore` in orchestrator.py) and update the `getattr` accordingly.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_orchestrator_stage_models.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_stage_models.py
git commit -m "feat(pipeline): orchestrator all_repos_stage_models + node_states in all_prs"
```

---

### Task 6: Wire `all_repos_stage_models()` across daemon↔edge

**Files:**
- Modify: `server/sessions/facade.py:62` (add passthrough), `server/sessions/remote.py:119` area (add remote method), `server/ipc/orchestrator_rpc.py:65-78` (handler + registration)
- Test: `server/tests/test_stage_models_rpc_roundtrip.py`

**Interfaces:**
- Consumes: `SessionOrchestrator.all_repos_stage_models()` (Task 5).
- Produces: `InProcessOrchestratorAdapter.all_repos_stage_models()`, `RemoteOrchestrator.all_repos_stage_models()`, RPC handler `"all_repos_stage_models"` returning `{"models": {repo: model.to_dict()}}`.

- [ ] **Step 1: Write the failing test** (mirror the existing `all_prs` round-trip test shape; grep `test_*all_prs*` for the harness)

```python
# server/tests/test_stage_models_rpc_roundtrip.py
import asyncio
from server.pipeline.track import StageModel, StageNode


class _Orch:
    def all_repos_stage_models(self):
        return {"o/r": StageModel("o/r", (StageNode("ci", "CI", "ci", 0), StageNode("merged", "Merged", "merged", 1)))}


def test_rpc_handler_serializes_models():
    # The handler closes over `orch`; call register and capture the registered fn.
    from server.ipc import orchestrator_rpc as mod
    captured = {}

    class _App:
        class state:  # noqa: N801
            pass
    # Monkeypatch register_rpc to capture handlers.
    orig = mod.register_rpc
    mod.register_rpc = lambda app, name, fn: captured.__setitem__(name, fn)
    try:
        mod.register_orchestrator_rpc(_App(), _Orch())
    finally:
        mod.register_rpc = orig
    handler = captured["all_repos_stage_models"]
    out = asyncio.get_event_loop().run_until_complete(handler({}))
    assert out["models"]["o/r"]["nodes"][0]["id"] == "ci"
    # Edge side rebuilds:
    assert StageModel.from_dict(out["models"]["o/r"]).repo == "o/r"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_stage_models_rpc_roundtrip.py -v`
Expected: FAIL with `KeyError: 'all_repos_stage_models'`

- [ ] **Step 3: Write minimal implementation**

`server/sessions/facade.py` — after the `all_prs` line:

```python
    async def all_repos_stage_models(self): return await _maybe_await(self._orch.all_repos_stage_models())
```

`server/sessions/remote.py` — after `all_prs`:

```python
    async def all_repos_stage_models(self) -> "dict":
        from server.pipeline.track import StageModel
        r = await self._client.call("all_repos_stage_models")
        return {k: StageModel.from_dict(v) for k, v in (r.get("models") or {}).items()}
```

`server/ipc/orchestrator_rpc.py` — add handler after `all_prs` and register it:

```python
    async def all_repos_stage_models(params: dict) -> dict:
        models = orch.all_repos_stage_models()
        if inspect.isawaitable(models):
            models = await models
        return {"models": {k: v.to_dict() for k, v in models.items()}}
```

```python
    register_rpc(app, "all_repos_stage_models", all_repos_stage_models)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_stage_models_rpc_roundtrip.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/facade.py server/sessions/remote.py server/ipc/orchestrator_rpc.py server/tests/test_stage_models_rpc_roundtrip.py
git commit -m "feat(pipeline): wire all_repos_stage_models across daemon-edge"
```

---

### Task 7: Route `GET /api/pipeline/repos`

**Files:**
- Modify: `server/api/pipeline.py` (add router/route); confirm registration in `server/app.py` (the `/api/prs` router is already registered there — add the new route to the same module so it's already wired).
- Test: `server/tests/test_api_pipeline_repos.py`

**Interfaces:**
- Consumes: `orch.all_repos_stage_models()`.
- Produces: `GET /api/pipeline/repos` → `{ repo: StageModel.to_dict() }`, behind `current_identity`.

Add to `server/api/pipeline.py` a second router (or extend) with prefix `/api/pipeline` and a `GET /repos`. Keep the existing `/api/prs` router; export both and register the new one in `server/app.py` if it's a new `APIRouter` instance.

- [ ] **Step 1: Write the failing test** (follow the existing `/api/prs` API test for the app/TestClient + auth fixtures — grep `def test_*prs*` under `server/tests`)

```python
# server/tests/test_api_pipeline_repos.py
# Mirror the existing /api/prs API test's app construction + identity injection.
# Asserts: 401 without identity, 200 with, and the real response shape.

def test_pipeline_repos_requires_identity(client_no_auth):
    assert client_no_auth.get("/api/pipeline/repos").status_code == 401


def test_pipeline_repos_returns_models(client_with_stub_orch):
    # stub orch.all_repos_stage_models() → {"o/r": StageModel(...)}
    r = client_with_stub_orch.get("/api/pipeline/repos")
    assert r.status_code == 200
    body = r.json()
    assert body["o/r"]["nodes"][0]["id"] == "ci"
```

> Implementer: reuse the fixtures from the existing `/api/prs` test (same `create_app` + injected orchestrator + `CCSWITCH_DEV`/allowlist handling). Stub `all_repos_stage_models` to return one `StageModel`.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_api_pipeline_repos.py -v`
Expected: FAIL (404 — route not defined)

- [ ] **Step 3: Write minimal implementation**

Append to `server/api/pipeline.py`:

```python
repos_router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@repos_router.get("/repos")
async def list_stage_models(
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    orch = request.app.state.sessions
    import inspect
    models = orch.all_repos_stage_models()
    if inspect.isawaitable(models):
        models = await models
    return {repo: m.to_dict() for repo, m in models.items()}
```

In `server/app.py`, register `repos_router` next to the existing pipeline `router` (grep `pipeline` import / `include_router` and add `app.include_router(repos_router)`).

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/test_api_pipeline_repos.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add server/api/pipeline.py server/app.py server/tests/test_api_pipeline_repos.py
git commit -m "feat(pipeline): GET /api/pipeline/repos returns per-repo StageModels"
```

---

### Task 8: Frontend types + `useStageModels()` hook

**Files:**
- Modify: `web/src/api/types.ts` (add `StageNode`/`StageModel`/`NodeState`/`node_states` on `PipelinePr`), `web/src/api/hooks.ts:16-32` (QK + hook)
- Test: `web/src/api/__tests__/useStageModels.test.ts` (follow the existing hook test pattern; grep an existing `use*` hook test)

**Interfaces:**
- Produces: `StageModel` type, `useStageModels(): UseQueryResult<Record<string, StageModel>>`, `PipelinePr.node_states?: {id:string; state:NodeState}[]`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/api/__tests__/useStageModels.test.ts — mirror an existing hook test's QueryClient + api mock.
import { describe, it, expect, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
// ...wrap in a QueryClientProvider per the existing hook-test helper...
import { useStageModels } from '../hooks'

it('fetches /pipeline/repos and returns a repo→model map', async () => {
  // mock api.get to resolve { 'o/r': { repo: 'o/r', nodes: [{id:'ci',label:'CI',kind:'ci',order:0,source:'derived'}] } }
  const { result } = renderHook(() => useStageModels(), { wrapper })
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(result.current.data!['o/r'].nodes[0].id).toBe('ci')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `vitest run src/api/__tests__/useStageModels.test.ts`
Expected: FAIL (`useStageModels` not exported)

- [ ] **Step 3: Write minimal implementation**

`web/src/api/types.ts`:

```ts
export type NodeState = 'done' | 'active' | 'failed' | 'pending'

export interface StageNode {
  id: string
  label: string
  kind: 'ci' | 'gate' | 'queue' | 'deploy' | 'merged'
  order: number
  source: 'derived' | 'override'
}

export interface StageModel {
  repo: string
  nodes: StageNode[]
}
```

Add to `PipelinePr` (the interface around `types.ts:150`): `node_states?: { id: string; state: NodeState }[]`.

`web/src/api/hooks.ts` — add a query key and hook (mirror `useAllPrs`):

```ts
// in QK:
  pipelineRepos: ['pipeline-repos'] as const,

export function useStageModels() {
  return useQuery<Record<string, StageModel>>({
    queryKey: QK.pipelineRepos,
    queryFn: () => api.get<Record<string, StageModel>>('/pipeline/repos'),
    staleTime: 15_000,
    refetchInterval: 30_000,
  })
}
```

(Import `StageModel` from `./types`.)

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `vitest run src/api/__tests__/useStageModels.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/api/__tests__/useStageModels.test.ts
git commit -m "feat(web): StageModel types + useStageModels hook"
```

---

### Task 9: `StageTrack` component

**Files:**
- Create: `web/src/components/StageTrack.tsx`
- Test: `web/src/components/__tests__/StageTrack.test.tsx`

**Interfaces:**
- Consumes: `StageModel`, `NodeState` (Task 8), `formatEta` (extract from `PipelineView.tsx:11` into a shared spot or re-declare locally — prefer importing; if it's not exported, export it from `PipelineView` or move it to `web/src/lib/`).
- Produces: `<StageTrack model={StageModel | undefined} states={{id,state}[] | undefined} etaSeconds?={number|null} etaOverdue?={boolean} />`.

Render one circle per node (green check `done`, accent-filled `active`, red X `failed`, gray hollow `pending`), node `label` beneath, connector line between circles tinted to the upstream node color when that upstream is `done`. The CI node, when `active`, shows `~{formatEta(etaSeconds)}` (or `overdue`). When `model` is undefined, render a minimal `CI · Ready · Merged` skeleton (all pending) so a row never blanks.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/StageTrack.test.tsx
import { render, screen } from '@testing-library/react'
import { StageTrack } from '../StageTrack'

const model = {
  repo: 'o/r',
  nodes: [
    { id: 'ci', label: 'CI', kind: 'ci', order: 0, source: 'derived' },
    { id: 'ready', label: 'Ready', kind: 'gate', order: 1, source: 'derived' },
    { id: 'merged', label: 'Merged', kind: 'merged', order: 2, source: 'derived' },
  ],
} as const

it('renders one node per model node with its state', () => {
  render(<StageTrack model={model} states={[
    { id: 'ci', state: 'done' }, { id: 'ready', state: 'active' }, { id: 'merged', state: 'pending' },
  ]} />)
  expect(screen.getByTestId('stage-node-ci')).toHaveAttribute('data-state', 'done')
  expect(screen.getByTestId('stage-node-ready')).toHaveAttribute('data-state', 'active')
  expect(screen.getByTestId('stage-node-merged')).toHaveAttribute('data-state', 'pending')
  expect(screen.getByText('CI')).toBeInTheDocument()
})

it('falls back to a skeleton when model is undefined', () => {
  render(<StageTrack model={undefined} states={undefined} />)
  expect(screen.getByTestId('stage-node-ci')).toBeInTheDocument()
  expect(screen.getByTestId('stage-node-merged')).toBeInTheDocument()
})

it('shows ETA on an active CI node', () => {
  render(<StageTrack model={model} etaSeconds={75} states={[{ id: 'ci', state: 'active' }]} />)
  expect(screen.getByText(/~1m|~75s/)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `vitest run src/components/__tests__/StageTrack.test.tsx`
Expected: FAIL (no `StageTrack`)

- [ ] **Step 3: Write minimal implementation**

Create `web/src/components/StageTrack.tsx` rendering the nodes + connectors with `data-testid={\`stage-node-${id}\`}` and `data-state`. Map state → Tailwind classes mirroring `PrPill.tsx` conventions (green/red/amber/gray + dark variants). Skeleton uses a const `[{id:'ci',label:'CI'},{id:'ready',label:'Ready'},{id:'merged',label:'Merged'}]` all `pending`. Show ETA via `formatEta` only on the `ci` node when its state is `active`.

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `vitest run src/components/__tests__/StageTrack.test.tsx`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add web/src/components/StageTrack.tsx web/src/components/__tests__/StageTrack.test.tsx
git commit -m "feat(web): StageTrack horizontal node renderer"
```

---

### Task 10: `PipelineView` rewrite — group by repo, render tracks, delete `STAGE_SECTIONS`

**Files:**
- Modify: `web/src/components/PipelineView.tsx` (full rewrite of `PipelineBoard`)
- Modify: `web/src/components/__tests__/PipelineBoard.test.tsx`, `web/src/components/__tests__/PipelineTab.test.tsx` (update to the repo-grouped DOM)

**Interfaces:**
- Consumes: `useAllPrs`, `useFixPr`, `useStageModels` (Task 8); `StageTrack` (Task 9); `GroupHeader`, `PrPill`, `isActionablePrState`.
- Produces: repo-grouped board; each row = `PrPill` + title + `<StageTrack model={models[pr.repo]} states={pr.node_states} etaSeconds={pr.eta_seconds} etaOverdue={pr.eta_overdue} />` + preserved session click-through + Fix-PR chip.

- [ ] **Step 1: Update the failing tests**

Rewrite `PipelineBoard.test.tsx` to assert: (a) PRs are grouped under a repo `GroupHeader` (one section per distinct `pr.repo`), (b) a repo with a failing PR sorts first, (c) each row renders a `StageTrack` (`getByTestId('stage-node-ci')` within the row), (d) session-linked row navigates / shows Fix chip, unsessioned opens URL, (e) empty board state. Pass a `stageModels` prop or mock `useStageModels`. Keep existing behavioral assertions that still apply (Fix chip, session label).

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `vitest run src/components/__tests__/PipelineBoard.test.tsx src/components/__tests__/PipelineTab.test.tsx`
Expected: FAIL (board still grouped by stage; no repo headers / tracks)

- [ ] **Step 3: Rewrite `PipelineBoard`**

Group `prs` by `pr.repo`; order repo groups by (has-failing-PR first, then repo name); within a group keep the existing stage-urgency/number sort. Render a `GroupHeader` per repo (label = repo, count = PR count). Each row keeps the existing `PrPill` + title + session link + Fix chip, and inserts `<StageTrack model={models[pr.repo]} states={pr.node_states} etaSeconds={pr.eta_seconds} etaOverdue={pr.eta_overdue} />`. `PipelineView()` calls `useStageModels()` and passes `models` to `PipelineBoard`. **Delete `STAGE_SECTIONS`** and the stage-bucketing code. Keep `formatEta` exported (Task 9 imports it) or move it to `web/src/lib/`.

- [ ] **Step 4: Run the full frontend gate**

Run (in `web/`): `vitest run` then `npx tsc -b --force` then `eslint . --max-warnings 0`
Expected: all green (full suite — catches subset-only breakage, memory: ccswitch-stale-worktree-pr-and-full-vitest)

- [ ] **Step 5: Commit**

```bash
git add web/src/components/PipelineView.tsx web/src/components/__tests__/PipelineBoard.test.tsx web/src/components/__tests__/PipelineTab.test.tsx
git commit -m "feat(web): repo-grouped Pipeline board with per-PR StageTrack; drop STAGE_SECTIONS"
```

---

## Final verification (before PR)

- [ ] Backend: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main>/.venv/bin/python -m pytest server/tests/ -q` — all green (full suite; catches exact-shape asserts elsewhere).
- [ ] Backend lint: `ruff check server` + `mypy server` green.
- [ ] Frontend: `vitest run` + `npx tsc -b --force` + `eslint . --max-warnings 0` green.
- [ ] Grep `STAGE_SECTIONS` returns nothing (old vocabulary purged).
- [ ] Grep `all_repos_stage_models` appears in orchestrator, facade, remote, rpc handler, registration (5 layers wired).

## Self-review notes (plan ↔ spec)

- Spec §3.1 track.py → Tasks 1-3. §3.2 orchestrator → Task 5. §3.3 five-layer wiring → Task 6. §3.4 route → Task 7. §4.1 types+hook → Task 8. §4.2 StageTrack → Task 9. §4.3 PipelineView rewrite + delete STAGE_SECTIONS → Task 10. §7 testing folded into each task + final gate.
- Spec said `node_states` on `PullRequestInfo`; plan attaches it to **`PipelinePr`** instead (the join object the API returns) — cleaner, since states depend on the per-repo model the join layer owns, and avoids mutating the frozen poller-built `PullRequestInfo`. Functionally identical on the wire (`/api/prs` rows still carry `node_states`).
- Deploy node in M1 is gated on `any(pr.deployed)` per repo (single `production` env) — a known approximation; M-Track-2 generalizes via environment enumeration.
