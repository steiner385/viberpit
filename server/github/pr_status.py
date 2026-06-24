"""Pure helpers to query GitHub PRs by head branch and parse the response.

No I/O lives here — ``build_pr_query`` produces a GraphQL document and
``parse_pr_response`` maps a parsed response to PullRequestInfo, so both are
fully unit-testable without touching the network or the ``gh`` CLI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

PRODUCTION_ENVIRONMENT = "production"


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
class PullRequestInfo:
    number: int
    url: str
    state: str   # 'open' | 'draft' | 'merged' | 'closed'
    checks: str  # 'passing' | 'failing' | 'pending' | 'none'
    title: str
    deployed: bool = False  # merged AND live in the active production deployment
    head_sha: str = ""
    is_draft: bool = False
    merged: bool = False
    updated_at: str = ""
    checks_detail: tuple[CheckDetail, ...] = ()
    stage: str = ""  # open-stage classifier output: 'ci' | 'ready' | 'parked' | ''

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
        )


def _alias(i: int) -> str:
    return f"r{i}"


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
            f"    nodes {{ number url title state isDraft updatedAt mergedAt headRefOid\n"
            f"      commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state\n"
            f"        contexts(first: 100) {{ nodes {{\n"
            f"          __typename\n"
            f"          ... on CheckRun {{ name conclusion }}\n"
            f"          ... on StatusContext {{ context state }}\n"
            f"        }} }}\n"
            f"      }} }} }} }}\n"
            f"    }}\n"
            f"  }}\n"
            f"  deployments(environments: [{json.dumps(PRODUCTION_ENVIRONMENT)}], "
            f"orderBy: {{field: CREATED_AT, direction: DESC}}, first: 5) {{\n"
            f"    nodes {{ commitOid latestStatus {{ state }} commit {{ committedDate }} }}\n"
            f"  }}\n"
            f"}}"
        )
    return "query {\n" + "\n".join(blocks) + "\n}"


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


def _active_deploy_date(node: dict) -> str | None:
    """committedDate of the newest production deployment whose latest status is SUCCESS."""
    nodes = ((node.get("deployments") or {}).get("nodes")) or []
    for dep in nodes:  # already ordered newest-first by the query
        status = (dep.get("latestStatus") or {}).get("state")
        if status == "SUCCESS":
            return (dep.get("commit") or {}).get("committedDate")
    return None


def _is_deployed(pr: dict, state: str, active_date: str | None) -> bool:
    merged_at = pr.get("mergedAt")
    return bool(state == "merged" and merged_at and active_date and merged_at <= active_date)


def _to_info(pr: dict, active_date: str | None) -> PullRequestInfo:
    state = _classify_state(pr.get("state", ""), bool(pr.get("isDraft")))
    return PullRequestInfo(
        number=pr["number"],
        url=pr["url"],
        state=state,
        checks=_classify_checks(_rollup_state(pr)),
        title=pr.get("title", ""),
        deployed=_is_deployed(pr, state, active_date),
        head_sha=pr.get("headRefOid", ""),
        is_draft=bool(pr.get("isDraft")),
        merged=(state == "merged"),
        updated_at=pr.get("updatedAt", ""),
        checks_detail=_checks_detail(pr),
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
        active_date = _active_deploy_date(node)
        prs = ((node.get("pullRequests") or {}).get("nodes")) or []
        best: PullRequestInfo | None = None
        best_sort: tuple | None = None
        for raw in prs:
            info = _to_info(raw, active_date)
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
