"""GitHub Deployments as a monitoring signal (deploy-monitor spec §5.1).

Railway (and GitHub's own environment deploys) write one Deployment per
environment per push and a status on it — ``FAILURE`` when the build breaks,
``SUCCESS`` when it serves. That makes the Deployments API the source of truth
for "is this environment's newest deploy failing" with no platform credentials.

Pure query builder + parser; ``fetch_deployments`` is the one ``gh`` wrapper and
never raises (a repo maps to ``None`` when its data could not be read, so the
caller can leave existing incidents untouched rather than closing them on a
blank read).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .pr_status import _DEPLOY_NODE_FIELDS, _alias

log = logging.getLogger(__name__)

#: Newest deployments fetched per repo. KinDash creates three per merge, so 50
#: covers roughly sixteen merges — far more than one incident needs.
DEPLOYMENTS_FIRST = 50


@dataclass(frozen=True)
class Deployment:
    env: str
    sha: str
    ref: str | None
    creator: str | None
    created_at: datetime | None
    status_state: str            # upper-case GitHub DeploymentStatusState; "" when null
    status_at: datetime | None
    log_url: str | None
    env_url: str | None


def build_deployments_query(repos: list[str], *, first: int = DEPLOYMENTS_FIRST) -> str:
    """One GraphQL doc aliasing ``r{i}: repository(...) { deployments(...) }`` per
    repo, newest first. Names are JSON-encoded so they cannot break the literal.
    Returns "" for an empty list (the caller skips the gh call)."""
    if not repos:
        return ""
    blocks = []
    for i, repo in enumerate(repos):
        owner, _, name = repo.partition("/")
        blocks.append(
            f"{_alias(i)}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
            f"  deployments(orderBy: {{field: CREATED_AT, direction: DESC}}, first: {int(first)}) {{\n"
            f"    nodes {{ {_DEPLOY_NODE_FIELDS} }}\n"
            f"  }}\n"
            f"}}"
        )
    return "query {\n" + "\n".join(blocks) + "\n}"


def parse_iso(value: object) -> datetime | None:
    """GitHub ISO-8601 (``...Z``) → aware UTC datetime; None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_node(node: object) -> Deployment | None:
    if not isinstance(node, dict):
        return None
    env, sha = _str(node.get("latestEnvironment")), _str(node.get("commitOid"))
    if env is None or sha is None:
        return None
    status = node.get("latestStatus") or {}
    if not isinstance(status, dict):
        status = {}
    state = _str(status.get("state"))
    return Deployment(
        env=env, sha=sha,
        ref=_str((node.get("ref") or {}).get("name")),
        creator=_str((node.get("creator") or {}).get("login")),
        created_at=parse_iso(node.get("createdAt")),
        status_state=state.upper() if state else "",
        status_at=parse_iso(status.get("createdAt")),
        log_url=_str(status.get("logUrl")),
        env_url=_str(status.get("environmentUrl")),
    )


def parse_deployments_response(data: dict, repos: list[str]) -> dict[str, list[Deployment] | None]:
    """``{repo: newest-first deployments}``; a repo whose block is missing or
    null (a forbidden Deployments App permission null-propagates it) maps to
    ``None`` — "unreadable", distinct from "no deployments"."""
    payload = (data or {}).get("data") if isinstance(data, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    out: dict[str, list[Deployment] | None] = {}
    for i, repo in enumerate(repos):
        block = payload.get(_alias(i))
        if not isinstance(block, dict):
            out[repo] = None
            continue
        nodes = ((block.get("deployments") or {}).get("nodes")) or []
        parsed = [d for d in (_parse_node(n) for n in nodes) if d is not None]
        out[repo] = parsed
    return out


def by_env(deployments: list[Deployment]) -> dict[str, list[Deployment]]:
    """Group newest-first deployments by environment, order preserved."""
    out: dict[str, list[Deployment]] = {}
    for d in deployments:
        out.setdefault(d.env, []).append(d)
    return out


def fetch_deployments(repos: list[str], *, run=None) -> dict[str, list[Deployment] | None]:
    """Fetch every repo's newest deployments. One GraphQL call per distinct App
    token (repos of one owner share one), ambient gh auth when there is none.
    Never raises: a group whose call fails maps each of its repos to ``None``."""
    from ccswitch import gitrepos

    runner = run or gitrepos._run_gh
    groups: dict[str | None, list[str]] = {}
    for repo in repos:
        groups.setdefault(gitrepos.app_token_for_repo(repo), []).append(repo)
    out: dict[str, list[Deployment] | None] = {}
    for token, group in groups.items():
        query = build_deployments_query(group)
        data: object = None
        try:
            proc = runner(["api", "graphql", "-f", f"query={query}"], token=token)
            if getattr(proc, "returncode", 1) == 0:
                data = json.loads(getattr(proc, "stdout", "") or "")
            else:
                log.warning("github.deployments_fetch_failed repos=%s: %s", group,
                            str(getattr(proc, "stderr", "")).strip())
        except Exception:
            log.warning("github.deployments_fetch_failed repos=%s", group, exc_info=True)
        if not isinstance(data, dict):
            out.update({r: None for r in group})
            continue
        out.update(parse_deployments_response(data, group))
    return out
