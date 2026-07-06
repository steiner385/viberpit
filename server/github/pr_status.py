"""Pure helpers to query GitHub PRs by head branch and parse the response.

No I/O lives here — ``build_pr_query`` produces a GraphQL document and
``parse_pr_response`` maps a parsed response to PullRequestInfo, so both are
fully unit-testable without touching the network or the ``gh`` CLI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from server.pipeline.stage import derive_merged_stage, derive_open_stage

if TYPE_CHECKING:
    from server.pipeline.track import NodeStatus


@dataclass(frozen=True)
class CheckDetail:
    name: str
    conclusion: str  # 'success' | 'failure' | 'pending' | 'neutral' | 'skipped' | ...

    def to_dict(self) -> dict:
        return {"name": self.name, "conclusion": self.conclusion}

    @classmethod
    def from_dict(cls, d: dict) -> "CheckDetail":
        return cls(name=d.get("name", ""), conclusion=d.get("conclusion", ""))


@dataclass(frozen=True)
class CheckRow:
    name: str
    workflow: str | None
    status: str        # GraphQL status passthrough: COMPLETED/IN_PROGRESS/QUEUED/PENDING/...
    conclusion: str    # normalised lowercase: success/failure/pending/neutral/skipped/...
    duration_seconds: int | None
    required: bool
    overdue: bool
    rem_low_seconds: int | None = None
    rem_nominal_seconds: int | None = None
    rem_high_seconds: int | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "workflow": self.workflow, "status": self.status,
            "conclusion": self.conclusion, "duration_seconds": self.duration_seconds,
            "required": self.required, "overdue": self.overdue,
            "rem_low_seconds": self.rem_low_seconds,
            "rem_nominal_seconds": self.rem_nominal_seconds,
            "rem_high_seconds": self.rem_high_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CheckRow":
        return cls(
            name=d.get("name", ""), workflow=d.get("workflow"),
            status=d.get("status", ""), conclusion=d.get("conclusion", ""),
            duration_seconds=d.get("duration_seconds"),
            required=bool(d.get("required", False)), overdue=bool(d.get("overdue", False)),
            rem_low_seconds=d.get("rem_low_seconds"),
            rem_nominal_seconds=d.get("rem_nominal_seconds"),
            rem_high_seconds=d.get("rem_high_seconds"),
        )


@dataclass(frozen=True)
class CheckGroup:
    kind: str  # "head" | "merge"
    checks: tuple[CheckRow, ...]
    total_seconds: int
    cost_usd: float | None
    eta_seconds: int | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "checks": [c.to_dict() for c in self.checks],
            "total_seconds": self.total_seconds,
            "cost_usd": self.cost_usd,
            "eta_seconds": self.eta_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CheckGroup":
        return cls(
            kind=d.get("kind", ""),
            checks=tuple(CheckRow.from_dict(c) for c in d.get("checks", [])),
            total_seconds=int(d.get("total_seconds", 0)),
            cost_usd=d.get("cost_usd"),
            eta_seconds=d.get("eta_seconds"),
        )


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    url: str
    state: str   # 'open' | 'draft' | 'merged' | 'closed'
    checks: str  # 'passing' | 'failing' | 'pending' | 'none'
    title: str
    deployed: bool = False  # merged AND live in production (= "production" in deployed_envs)
    head_sha: str = ""
    is_draft: bool = False
    merged: bool = False
    updated_at: str = ""
    checks_detail: tuple[CheckDetail, ...] = ()
    stage: str = ""  # open-stage classifier output: 'ci' | 'ready' | 'parked' | ''
    repo: str = ""  # nameWithOwner, e.g. "owner/name"; populated by the population poller
    head_ref: str = ""  # PR head branch (headRefName); lets PR Doctor dispatch on population PRs
    eta_seconds: int | None = None
    eta_percent: int | None = None
    eta_overdue: bool = False
    eta_low_seconds: int | None = None
    eta_high_seconds: int | None = None
    deployed_envs: tuple[str, ...] = ()
    check_groups: tuple[CheckGroup, ...] = ()
    mergeable: str = "UNKNOWN"          # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: str = ""        # BEHIND | BLOCKED | CLEAN | DIRTY | DRAFT | UNSTABLE | UNKNOWN
    auto_merge_enabled: bool = False    # autoMergeRequest != null
    in_merge_queue: bool = False        # mergeQueueEntry != null

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "checks": self.checks,
            "title": self.title,
            "deployed": self.deployed,
            "head_sha": self.head_sha,
            "is_draft": self.is_draft,
            "merged": self.merged,
            "updated_at": self.updated_at,
            "checks_detail": [c.to_dict() for c in self.checks_detail],
            "stage": self.stage,
            "repo": self.repo,
            "head_ref": self.head_ref,
            "eta_seconds": self.eta_seconds,
            "eta_percent": self.eta_percent,
            "eta_overdue": self.eta_overdue,
            "eta_low_seconds": self.eta_low_seconds,
            "eta_high_seconds": self.eta_high_seconds,
            "deployed_envs": list(self.deployed_envs),
            "check_groups": [g.to_dict() for g in self.check_groups],
            "mergeable": self.mergeable,
            "merge_state_status": self.merge_state_status,
            "auto_merge_enabled": self.auto_merge_enabled,
            "in_merge_queue": self.in_merge_queue,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PullRequestInfo":
        """Inverse of ``to_dict`` — rebuild from a daemon→edge RPC payload."""
        return cls(
            number=d["number"],
            url=d["url"],
            state=d["state"],
            checks=d["checks"],
            title=d.get("title", ""),
            deployed=d.get("deployed", False),
            head_sha=d.get("head_sha", ""),
            is_draft=d.get("is_draft", False),
            merged=d.get("merged", False),
            updated_at=d.get("updated_at", ""),
            checks_detail=tuple(CheckDetail.from_dict(c) for c in d.get("checks_detail", [])),
            stage=d.get("stage", ""),
            repo=d.get("repo", ""),
            head_ref=d.get("head_ref", ""),
            eta_seconds=d.get("eta_seconds"),
            eta_percent=d.get("eta_percent"),
            eta_overdue=d.get("eta_overdue", False),
            eta_low_seconds=d.get("eta_low_seconds"),
            eta_high_seconds=d.get("eta_high_seconds"),
            deployed_envs=tuple(d.get("deployed_envs", [])),
            check_groups=tuple(CheckGroup.from_dict(g) for g in d.get("check_groups", [])),
            mergeable=d.get("mergeable", "UNKNOWN"),
            merge_state_status=d.get("merge_state_status", ""),
            auto_merge_enabled=d.get("auto_merge_enabled", False),
            in_merge_queue=d.get("in_merge_queue", False),
        )


@dataclass(frozen=True)
class PipelinePr:
    """A population PR plus the ccswitch session that authored it (if any)."""
    pr: PullRequestInfo
    session_id: str | None = None
    session_name: str | None = None
    node_states: "tuple[NodeStatus, ...]" = ()  # per-repo track placement

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


def _alias(i: int) -> str:
    return f"r{i}"


# Shared PR-node selection so build_pr_query (per-branch) and the population
# query (per-repo) can never drift in which fields they fetch.
_PR_NODE_FIELDS = (
    "number url title state isDraft updatedAt mergedAt headRefName headRefOid mergeStateStatus mergeable\n"
    "      autoMergeRequest { enabledAt }\n"
    "      mergeQueueEntry { state }\n"
    "      commits(last: 1) { nodes { commit { statusCheckRollup { state\n"
    "        contexts(first: 100) { nodes {\n"
    "          __typename\n"
    "          ... on CheckRun { name conclusion }\n"
    "          ... on StatusContext { context state }\n"
    "        } }\n"
    "      } } } }"
)


def build_pr_query(pairs: list[tuple[str, str]]) -> str:
    """One GraphQL doc aliasing a repository(...).pullRequests block per pair.

    Strings are JSON-encoded so a branch/owner/name can never break out of the
    string literal. Returns "" for an empty list (caller skips the gh call).
    """
    if not pairs:
        return ""
    blocks = []
    for i, (name_with_owner, branch) in enumerate(pairs):
        owner, _, name = name_with_owner.partition("/")
        blocks.append(
            f"{_alias(i)}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
            f"  pullRequests(headRefName: {json.dumps(branch)}, first: 5, "
            f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{\n"
            f"    nodes {{ {_PR_NODE_FIELDS} }}\n"
            f"  }}\n"
            f"  deployments(orderBy: {{field: CREATED_AT, direction: DESC}}, first: 30) {{\n"
            f"    nodes {{ latestEnvironment commitOid latestStatus {{ state }} commit {{ committedDate }} }}\n"
            f"  }}\n"
            f"}}"
        )
    return "query {\n" + "\n".join(blocks) + "\n}"


def build_population_query(repo: str, merged_since: str) -> str:
    """One GraphQL doc: all OPEN PRs (incl. drafts) + recent deployments across
    all environments for ``repo``, plus a search() for PRs merged on/after
    ``merged_since`` (ISO date).

    Strings are JSON-encoded so a repo/owner/name can't break the literal.
    NOTE: deployments are capped at first:30 across ALL environments — on a very
    busy repo, one env's active deploy can be pushed out of the window by churn
    in another (an accepted Option-A bound; full env enumeration is deferred).
    """
    owner, _, name = repo.partition("/")
    merged_q = f"repo:{repo} is:pr is:merged merged:>={merged_since}"
    return (
        "query {\n"
        f"  repo: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
        f"    deployments(orderBy: {{field: CREATED_AT, direction: DESC}}, first: 30) {{\n"
        f"      nodes {{ latestEnvironment commitOid latestStatus {{ state }} commit {{ committedDate }} }}\n"
        f"    }}\n"
        f"    pullRequests(states: OPEN, first: 100, "
        f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{\n"
        f"      nodes {{ {_PR_NODE_FIELDS} }}\n"
        f"    }}\n"
        f"  }}\n"
        f"  merged: search(query: {json.dumps(merged_q)}, type: ISSUE, first: 50) {{\n"
        f"    nodes {{ ... on PullRequest {{ {_PR_NODE_FIELDS} }} }}\n"
        f"  }}\n"
        "}"
    )


def build_eta_checks_query(prs: list[tuple[str, int]]) -> str:
    """One GraphQL doc aliasing a repository(owner,name).pullRequest block per PR.

    Embeds the PR number as a literal in both ``pullRequest(number: N)`` and
    ``isRequired(pullRequestNumber: N)`` so GitHub resolves required-check status
    relative to that exact PR.  Returns "" for an empty list.
    """
    if not prs:
        return ""
    blocks = []
    for i, (name_with_owner, number) in enumerate(prs):
        owner, _, name = name_with_owner.partition("/")
        blocks.append(
            f"{_alias(i)}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
            f"  pullRequest(number: {int(number)}) {{\n"
            f"    commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{\n"
            f"      contexts(first: 100) {{ nodes {{\n"
            f"        __typename\n"
            f"        ... on CheckRun {{ name status conclusion startedAt completedAt "
            f"isRequired(pullRequestNumber: {int(number)}) "
            f"checkSuite {{ workflowRun {{ workflow {{ name }} }} }} }}\n"
            f"        ... on StatusContext {{ context state }}\n"
            f"      }} }}\n"
            f"    }} }} }} }}\n"
            f"    mergeQueueEntry {{ estimatedTimeToMerge headCommit {{ statusCheckRollup {{\n"
            f"      contexts(first: 100) {{ nodes {{\n"
            f"        __typename\n"
            f"        ... on CheckRun {{ name status conclusion startedAt completedAt "
            f"checkSuite {{ workflowRun {{ workflow {{ name }} }} }} }}\n"
            f"        ... on StatusContext {{ context state }}\n"
            f"      }} }}\n"
            f"    }} }} }}\n"
            f"  }}\n"
            f"}}"
        )
    return "query {\n" + "\n".join(blocks) + "\n}"


def parse_eta_checks_response(
    response: dict, prs: list[tuple[str, int]]
) -> dict[tuple[str, int], list[dict]]:
    """Parse a GitHub GraphQL response from ``build_eta_checks_query``.

    Returns a mapping from (nameWithOwner, number) to a list of check dicts:
    ``{name, status, conclusion, started_at, completed_at, isRequired, workflowName, event}``.
    GraphQL ``status`` values (``COMPLETED``/``IN_PROGRESS``/``QUEUED``/``PENDING``/etc.)
    are passed through unchanged.  StatusContext (legacy) nodes are treated as
    ``COMPLETED`` with ``started_at`` and ``completed_at`` set to ``None``.
    """
    data = (response or {}).get("data") or {}
    out: dict[tuple[str, int], list[dict]] = {}
    for i, key in enumerate(prs):
        node = data.get(_alias(i)) or {}
        pr = node.get("pullRequest") or {}
        commits = ((pr.get("commits") or {}).get("nodes")) or []
        contexts: list[dict] = []
        if commits:
            rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") or {}
            contexts = ((rollup.get("contexts") or {}).get("nodes")) or []
        checks: list[dict] = []
        for c in contexts:
            if c.get("__typename") == "CheckRun":
                wf = (((c.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {})
                checks.append({
                    "name": c.get("name", ""), "status": c.get("status", ""),
                    "conclusion": c.get("conclusion"), "started_at": c.get("startedAt"),
                    "completed_at": c.get("completedAt"),
                    "isRequired": bool(c.get("isRequired")), "workflowName": wf.get("name"),
                    "event": "pull_request",
                })
            else:  # StatusContext — legacy, treated as completed
                checks.append({
                    "name": c.get("context", ""), "status": "COMPLETED",
                    "conclusion": c.get("state"), "started_at": None,
                    "completed_at": None,
                    "isRequired": False, "workflowName": None, "event": "pull_request",
                })
        out[key] = checks
    return out


def parse_merge_group_response(
    response: dict, prs: list[tuple[str, int]]
) -> dict[tuple[str, int], dict]:
    """Parse the mergeQueueEntry block of a ``build_eta_checks_query`` response.

    Returns (repo, number) -> {"checks": [...], "eta_seconds": int | None}.
    Empty checks + None eta when the PR is not enqueued (mergeQueueEntry null).
    """
    data = (response or {}).get("data") or {}
    out: dict[tuple[str, int], dict] = {}
    for i, key in enumerate(prs):
        node = data.get(_alias(i)) or {}
        entry = (node.get("pullRequest") or {}).get("mergeQueueEntry")
        if not entry:
            out[key] = {"checks": [], "eta_seconds": None}
            continue
        rollup = (entry.get("headCommit") or {}).get("statusCheckRollup") or {}
        contexts = ((rollup.get("contexts") or {}).get("nodes")) or []
        checks: list[dict] = []
        for c in contexts:
            if c.get("__typename") == "CheckRun":
                wf = (((c.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {})
                checks.append({
                    "name": c.get("name", ""), "status": c.get("status", ""),
                    "conclusion": c.get("conclusion"), "started_at": c.get("startedAt"),
                    "completed_at": c.get("completedAt"),
                    "isRequired": False, "workflowName": wf.get("name"),
                    "event": "merge_group",
                })
            else:
                checks.append({
                    "name": c.get("context", ""), "status": "COMPLETED",
                    "conclusion": c.get("state"), "started_at": None,
                    "completed_at": None, "isRequired": False,
                    "workflowName": None, "event": "merge_group",
                })
        # GitHub's MergeQueueEntry.estimatedTimeToMerge is in SECONDS (per the GraphQL
        # schema) — stored raw and rendered via formatEta (seconds). Do NOT multiply.
        out[key] = {"checks": checks, "eta_seconds": entry.get("estimatedTimeToMerge")}
    return out


def parse_population_response(response: dict, repo: str) -> tuple[PullRequestInfo, ...]:
    """Open + recently-merged PRs for one repo, each with ``repo`` set.

    Deduped by PR number; the OPEN listing wins over the merged search (a PR
    can't be both, but a search index lag could surface a stale duplicate).
    Reuses ``_to_info`` so stage/checks/state come from the single classifier.
    """
    data = (response or {}).get("data") or {}
    repo_node = data.get("repo") or {}
    active_dates = _active_deploy_dates(repo_node)
    open_prs = ((repo_node.get("pullRequests") or {}).get("nodes")) or []
    merged_prs = ((data.get("merged") or {}).get("nodes")) or []
    by_number: dict[int, PullRequestInfo] = {}
    for raw in (*open_prs, *merged_prs):
        if not raw:  # search() can return null union members
            continue
        info = replace(_to_info(raw, active_dates), repo=repo)
        by_number.setdefault(info.number, info)
    return tuple(by_number.values())


def _classify_state(gql_state: str, is_draft: bool) -> str:
    if gql_state == "MERGED":
        return "merged"
    if gql_state == "CLOSED":
        return "closed"
    return "draft" if is_draft else "open"


def _classify_checks(rollup_state: str | None) -> str:
    if rollup_state in ("SUCCESS",):
        return "passing"
    if rollup_state in ("FAILURE", "ERROR"):
        return "failing"
    if rollup_state in ("PENDING", "EXPECTED"):
        return "pending"
    return "none"


# Lower rank = more relevant. Tie-break by updatedAt (newest first).
_STATE_RANK = {"open": 0, "draft": 1, "merged": 2, "closed": 3}


_CONCLUSION_MAP = {
    "SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
    "PENDING": "pending", "EXPECTED": "pending", "NEUTRAL": "neutral",
    "SKIPPED": "skipped", "CANCELLED": "cancelled", "TIMED_OUT": "failure",
    "ACTION_REQUIRED": "failure", "STARTUP_FAILURE": "failure", "STALE": "neutral",
}


def _checks_detail(pr: dict) -> tuple[CheckDetail, ...]:
    nodes = ((pr.get("commits") or {}).get("nodes")) or []
    if not nodes:
        return ()
    rollup = (nodes[0].get("commit") or {}).get("statusCheckRollup")
    if not rollup:
        return ()
    ctx = ((rollup.get("contexts") or {}).get("nodes")) or []
    out: list[CheckDetail] = []
    for c in ctx:
        if c.get("__typename") == "CheckRun":
            name = c.get("name", "")
            raw = c.get("conclusion") or "PENDING"  # null conclusion = still running
        else:  # StatusContext
            name = c.get("context", "")
            raw = c.get("state", "")
        out.append(CheckDetail(name=name, conclusion=_CONCLUSION_MAP.get(raw, raw.lower())))
    return tuple(out)


def _rollup_state(pr: dict) -> str | None:
    nodes = ((pr.get("commits") or {}).get("nodes")) or []
    if not nodes:
        return None
    rollup = (nodes[0].get("commit") or {}).get("statusCheckRollup")
    return rollup.get("state") if rollup else None


def _active_deploy_dates(node: dict) -> dict[str, str]:
    """Map env name → committedDate of that env's newest SUCCESS deployment."""
    nodes = ((node.get("deployments") or {}).get("nodes")) or []
    out: dict[str, str] = {}
    for dep in nodes:  # newest-first
        env = dep.get("latestEnvironment")
        if not env or env in out:
            continue
        if (dep.get("latestStatus") or {}).get("state") != "SUCCESS":
            continue
        committed = (dep.get("commit") or {}).get("committedDate")
        if committed:
            out[env] = committed
    return out


def _is_deployed_envs(pr: dict, state: str, active_dates: dict[str, str]) -> tuple[str, ...]:
    merged_at = pr.get("mergedAt")
    if state != "merged" or not merged_at:
        return ()
    return tuple(env for env, date in active_dates.items() if date and merged_at <= date)


def _to_info(pr: dict, active_dates: dict[str, str]) -> PullRequestInfo:
    state = _classify_state(pr.get("state", ""), bool(pr.get("isDraft")))
    checks = _classify_checks(_rollup_state(pr))
    merged_at = pr.get("mergedAt")
    if merged_at:
        stage = derive_merged_stage(merged_at, datetime.now(timezone.utc)) or ""
    else:
        stage = derive_open_stage(checks, pr.get("mergeStateStatus"), bool(pr.get("isDraft")))
    deployed_envs = _is_deployed_envs(pr, state, active_dates)
    return PullRequestInfo(
        number=pr["number"],
        url=pr["url"],
        state=state,
        checks=checks,
        title=pr.get("title", ""),
        deployed=("production" in deployed_envs),
        head_sha=pr.get("headRefOid", ""),
        head_ref=pr.get("headRefName", ""),
        is_draft=bool(pr.get("isDraft")),
        merged=(state == "merged"),
        updated_at=pr.get("updatedAt", ""),
        checks_detail=_checks_detail(pr),
        stage=stage,
        deployed_envs=deployed_envs,
        mergeable=pr.get("mergeable") or "UNKNOWN",
        merge_state_status=pr.get("mergeStateStatus") or "",
        auto_merge_enabled=pr.get("autoMergeRequest") is not None,
        in_merge_queue=pr.get("mergeQueueEntry") is not None,
    )


def parse_pr_response(
    response: dict, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], PullRequestInfo]:
    data = (response or {}).get("data") or {}
    out: dict[tuple[str, str], PullRequestInfo] = {}
    for i, key in enumerate(pairs):
        node = data.get(_alias(i))
        if not node:
            continue
        active_dates = _active_deploy_dates(node)
        prs = ((node.get("pullRequests") or {}).get("nodes")) or []
        best: PullRequestInfo | None = None
        best_sort: tuple | None = None
        for raw in prs:
            info = _to_info(raw, active_dates)
            # Rank ascending, then newest updatedAt first (negate via reverse compare).
            sort_key = (_STATE_RANK.get(info.state, 99), _neg_iso(raw.get("updatedAt", "")))
            if best_sort is None or sort_key < best_sort:
                best, best_sort = info, sort_key
        if best is not None:
            out[key] = best
    return out


def _neg_iso(iso: str) -> str:
    # ISO-8601 strings sort lexicographically; invert each char so a *newer*
    # timestamp produces a *smaller* key (used as a min tie-break).
    return "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c for c in iso)
