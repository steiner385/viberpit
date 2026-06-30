"""Per-repo stage-track assembly + per-PR node placement.

Distinct from server/pipeline/stage.py: stage.py *derives* a PR's single stage
string; this module *assembles* a repo's ordered node list (StageModel) and maps
a PR's stage cursor onto those nodes (NodeStatus[]). It consumes stage.py's
output as a cursor — it never re-derives stages (constitution VI).
"""
from __future__ import annotations

from collections.abc import Iterable
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


# Conventional deploy-environment ordering. Lower rank = earlier in the pipeline.
_ENV_RANK = [
    (("dev", "development"), 0),
    (("test", "qa", "integration"), 1),
    (("stage", "staging", "preprod", "uat", "sandbox"), 2),
    (("prod", "production", "live", "release"), 3),
]


def _env_rank(env: str) -> int:
    low = env.lower()
    for keywords, rank in _ENV_RANK:
        if any(k in low for k in keywords):
            return rank
    return 2  # unknown envs cluster with staging-tier, alphabetical tie-break


def order_deploy_envs(envs: Iterable[str]) -> list[str]:
    """Deterministic conventional ordering: dev/qa → staging/unknown → prod, alpha tie-break."""
    uniq = sorted(set(envs))
    return sorted(uniq, key=lambda e: (_env_rank(e), e))


def build_stage_model(
    repo: str,
    *,
    ci_graph: dict | None,
    has_queue: bool,
    deploy_envs: list[str],
) -> StageModel:
    """Assemble the ordered StageModel for a repo from its real CI/CD facts."""
    specs: list[tuple[str, str, str]] = []  # (id, label, kind)
    ci_label = (ci_graph or {}).get("workflowName") or "CI"
    specs.append(("ci", ci_label, "ci"))
    specs.append(("ready", "Ready", "gate"))
    if has_queue:
        specs.append(("queue", "Queue", "queue"))
    specs.append(("merged", "Merged", "merged"))
    for env in deploy_envs:
        specs.append((f"deploy:{env}", env, "deploy"))
    nodes = tuple(
        StageNode(id=sid, label=label, kind=kind, order=i)
        for i, (sid, label, kind) in enumerate(specs)
    )
    return StageModel(repo=repo, nodes=nodes)


# Cursor node id by PR stage. 'parked'/'ci'/'' all sit at the CI node.
_STAGE_TO_NODE = {"": "ci", "parked": "ci", "ci": "ci", "ready": "ready",
                  "queue": "queue", "merged": "merged"}


def place_pr_on_track(pr: "PullRequestInfo", model: StageModel) -> list["NodeStatus"]:
    ids = [n.id for n in model.nodes]
    order_by_id = {n.id: n.order for n in model.nodes}

    if pr.stage == "merged":
        # A merged PR has landed: ci/ready/queue/merged are all done. Deploy nodes
        # (now AFTER merged) carry the promotion frontier.
        deployed = set(getattr(pr, "deployed_envs", ()) or ())
        if pr.deployed:  # back-compat: deployed=True without deployed_envs
            deployed.add("production")

        deploy_nodes = [n for n in model.nodes if n.kind == "deploy"]
        last_deployed_order = max(
            (n.order for n in deploy_nodes if n.id.split("deploy:", 1)[1] in deployed),
            default=-1,
        )
        frontier_id: str | None = None
        for n in deploy_nodes:
            env = n.id.split("deploy:", 1)[1]
            if env not in deployed and n.order > last_deployed_order:
                frontier_id = n.id
                break

        out: list[NodeStatus] = []
        for n in model.nodes:
            if n.kind == "deploy":
                env = n.id.split("deploy:", 1)[1]
                if env in deployed:
                    state = "done"
                elif n.id == frontier_id:
                    state = "active"
                else:
                    state = "pending"
            else:  # ci / ready / queue / merged — all cleared by a merged PR
                state = "done"
            out.append(NodeStatus(id=n.id, state=state))
        return out

    # Non-merged PRs: single forward cursor by stage (unchanged from M-Track-1).
    target = _STAGE_TO_NODE.get(pr.stage, "ci")
    if target not in order_by_id:
        target = "ci" if "ci" in order_by_id else ids[0]
    cursor = order_by_id[target]
    out = []
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
