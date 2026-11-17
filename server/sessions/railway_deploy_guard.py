"""Hard tool-layer guard: a session never ships unverified KinDash main to production
through a Railway mutation (#3403).

Railway does not restart the running image on a live variable change: it **rebuilds
from the latest main**. On 2026-11-13 a session set two production variables on
``kindash-prod`` (``set-variables``, live) while KinDash's Nightly had been red for six
days on a real regression (KinDash#21009). The change shipped six days of unverified main
to production; KinDash's own ``deploy-production.yml`` gate never ran. A live production
variable change is a deploy, so it is guarded like one.

Guarded calls (the Railway MCP tools, any server name containing ``railway``) against the
KinDash production environment: ``set-variables`` and ``update-service`` unless
``staged: true`` (``set-variables`` also passes with ``skipDeploys: true``) and
``create-deployment``. ``redeploy`` re-runs an EXISTING build, so it ships no new commit
and is the safe path the denial offers; it is never blocked here.

A guarded call is allowed when nothing beyond the last gate-verified SHA would ship —
i.e. KinDash main HEAD is the last verified SHA (the last green scheduled ``Nightly`` or
green ``deploy-reattempt`` run). Otherwise it is denied naming the commit count and any
open ``ci:regressed`` issues, and the safe path: ``staged: true`` (the owner commits it
through KinDash's gate) or a ``redeploy`` of the current image. Fail closed: when the
facts cannot be read the call is denied too.

Not implemented here: the owner-approval override that files KinDash's "Unverified prod
deploy" audit issue — it needs a card + grant like ``draft_hold_guard`` (follow-up).

It is an SDK ``PreToolUse`` hook so it fires in every permission mode, like the other
hold guards.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from viberpit import gitrepos

from server.sessions import draft_hold_guard as _draft

# KinDash production on Railway (project ``kindash``, environment ``production``,
# service ``kindash-prod``). Railway MCP defaults an omitted environmentId to
# ``production``, so an omitted one is production too.
KINDASH_REPO = "cairnea/KinDash"
KINDASH_PROJECT_ID = "57f91054-ca5f-44b6-bcef-da2eec7b7f47"
KINDASH_PROD_ENV_ID = "923e2a14-b366-4493-bfb3-2941c5bdfaa7"
KINDASH_PROD_SERVICE_ID = "e840db3e-9886-4bf5-964d-c13c05dcefbe"
PROD_HEALTH_URLS = ("https://kindash.com/api/health", "https://kindash.com/health")

#: ``mcp__<server>__<tool>`` for the Railway MCP server, however it is mounted.
HOOK_MATCHER = r"mcp__.*railway.*__(set-variables|update-service|create-deployment)"
_TOOL = re.compile(r"^mcp__.*railway.*__(set-variables|update-service|create-deployment)$")

# The workflows whose green run on a SHA means KinDash's gate verified it.
_GATE_WORKFLOWS = (("nightly.yml", "&event=schedule"), ("deploy-reattempt.yml", ""))


@dataclass(frozen=True)
class DeployFacts:
    prod_sha: str | None
    main_sha: str | None
    verified_sha: str | None
    ahead_by: int | None          # commits in verified..main
    regressions: tuple[str, ...]  # open `ci:regressed` issue refs, e.g. "#21009"


Facts = Callable[[], DeployFacts]


def ships_main(tool: str, tool_input: object) -> bool:
    """True when this Railway call would (re)build KinDash production from main."""
    if not isinstance(tool_input, dict):
        return True  # unreadable → treat as guarded (fail closed)
    if tool_input.get("projectId") != KINDASH_PROJECT_ID:
        return False
    env = tool_input.get("environmentId")
    if env is not None and env != KINDASH_PROD_ENV_ID:
        return False
    service = tool_input.get("serviceId")
    if service is not None and service != KINDASH_PROD_SERVICE_ID:
        return False  # Postgres/Redis rebuild nothing from main
    if tool_input.get("staged") is True:
        return False  # deploys nothing until the owner commits it
    name = tool.rsplit("__", 1)[-1]
    if name == "set-variables" and tool_input.get("skipDeploys") is True:
        return False
    return name in ("set-variables", "update-service", "create-deployment")


def blocked_reason(facts: DeployFacts) -> str:
    """The denial text for the unverified-main case."""
    short = lambda s: (s or "unknown")[:7]  # noqa: E731
    regressed = (f" Open `ci:regressed` issues: {', '.join(facts.regressions)}."
                 if facts.regressions else "")
    return (
        f"Blocked: this Railway call would rebuild KinDash production from main "
        f"({short(facts.main_sha)}), shipping {facts.ahead_by} commit(s) beyond the last "
        f"gate-verified SHA ({short(facts.verified_sha)}, the last green scheduled Nightly "
        f"or green deploy-reattempt); prod is live at {short(facts.prod_sha)}.{regressed} "
        "Railway rebuilds from the latest main on a live variable/config change, it does not "
        "restart the running image. Safe paths: pass `staged: true` (the owner commits it "
        "through KinDash's deploy gate), or `redeploy` the current image. If the owner wants "
        "the unverified deploy anyway, hand back and ask them to approve it.")


def unreadable_reason(why: str) -> str:
    return (
        f"Blocked: this Railway call would rebuild KinDash production from main, and the "
        f"guard could not verify main against the last gate-verified SHA ({why}). Pass "
        "`staged: true` (the owner commits it through KinDash's deploy gate), `redeploy` "
        "the current image, or retry once GitHub is reachable.")


def judge(facts: DeployFacts) -> str | None:
    """None to allow, else the denial reason."""
    if facts.main_sha is None or facts.verified_sha is None or facts.ahead_by is None:
        return unreadable_reason("a lookup failed")
    if facts.ahead_by == 0:
        return None
    return blocked_reason(facts)


def _gh(path: str, env: dict[str, str], jq: str) -> str | None:
    try:
        proc = subprocess.run(["gh", "api", path, "--jq", jq], capture_output=True,
                              text=True, env=env, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out and out != "null" else None


def _prod_sha() -> str | None:
    import urllib.request
    for url in PROD_HEALTH_URLS:
        try:
            with urllib.request.urlopen(url, timeout=10) as res:  # noqa: S310 - fixed https URLs
                sha = json.load(res).get("commitSha")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(sha, str) and sha:
            return sha
    return None


def fetch_facts() -> DeployFacts:
    """Read the deploy facts from GitHub and prod (blocking; run off the loop)."""
    env = gitrepos._gh_env()
    token = gitrepos.app_token_for_owner(KINDASH_REPO.partition("/")[0])
    if token:
        env["GH_TOKEN"] = token
    main = _gh(f"repos/{KINDASH_REPO}/commits/main", env, ".sha")
    best: tuple[str, str] | None = None  # (created_at, head_sha), newest green gate run
    for workflow, extra in _GATE_WORKFLOWS:
        row = _gh(f"repos/{KINDASH_REPO}/actions/workflows/{workflow}/runs"
                  f"?status=success&per_page=1{extra}", env,
                  '.workflow_runs[0] | "\\(.created_at) \\(.head_sha)"')
        if row and " " in row:
            created, sha = row.split(" ", 1)
            if best is None or created > best[0]:
                best = (created, sha)
    verified = best[1] if best else None
    ahead = None
    if main and verified:
        raw = _gh(f"repos/{KINDASH_REPO}/compare/{verified}...{main}", env, ".ahead_by")
        ahead = int(raw) if raw and raw.isdigit() else None
    issues = _gh(f"repos/{KINDASH_REPO}/issues?labels=ci:regressed&state=open&per_page=10",
                 env, '[.[] | select(.pull_request | not) | "#\\(.number)"] | join(",")')
    return DeployFacts(_prod_sha(), main, verified, ahead,
                       tuple(issues.split(",")) if issues else ())


def railway_deploy_guard(facts: Facts | None = None):
    """The SDK ``PreToolUse`` hook (:data:`HOOK_MATCHER`): deny a Railway call that
    would ship unverified KinDash main to production; everything else passes."""
    async def railway_deploy_pre_tool_use(input_data, tool_use_id=None, context=None) -> dict:
        data = input_data or {}
        tool = data.get("tool_name")
        if not isinstance(tool, str) or not _TOOL.match(tool):
            return {}
        tool_input = data.get("tool_input")
        if not ships_main(tool, tool_input):
            return {}
        try:
            reason = judge(await asyncio.wait_for(
                asyncio.to_thread(facts or fetch_facts), _draft.GUARD_BUDGET_S))
        except Exception:  # noqa: BLE001 - fail closed (a timeout included)
            reason = unreadable_reason("lookup timed out or failed")
        return _draft._deny(reason) if reason else {}
    return railway_deploy_pre_tool_use
