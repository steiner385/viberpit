# Deploy Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `deploy-monitor` automation that reads GitHub Deployment statuses per repo/environment, opens one durable incident per failing or stalled environment, alerts once through `notify_event("deploy_failed")`, and in `file` mode keeps exactly one GitHub issue open in that repo until the environment recovers.

**Architecture:** Three units. `server/github/deployments.py` is a pure query builder + parser (plus one `gh` fetch wrapper) sharing `_DEPLOY_NODE_FIELDS` with `pr_status.py`. `server/automations/deploy_monitor.py` holds the `Automation` contract surface, the pure `evaluate_env` policy, issue text, the injected-seam `DeployMonitorLoop`, and the card `ActionProvider`. Wiring adds the loop to the orchestrator exactly the way Error Triage is wired, plus one new notification event class.

**Tech Stack:** Python 3.12, FastAPI/SQLAlchemy (existing chassis), `gh` CLI GraphQL via `ccswitch.gitrepos._run_gh`, pytest; one data-only edit in React/TS.

**Spec:** `docs/superpowers/specs/2026-09-12-deploy-monitor-design.md`

## Global Constraints

- Automation id `deploy-monitor`, modes `("off", "watch", "file")`, cadence 120 s, `engine_policy = EnginePolicy.CLAUDE_ONLY` (never spawns a session).
- Label on filed issues: `deploy-monitor`. Title format: `[deploy-monitor] <env> deploy <failing|stalled> — <repo> @ <sha10>`.
- New notification class `deploy_failed`; call site must literally read `notify_event("deploy_failed"` (a test source-walks for it).
- Never use `func.now()`; timestamps in target state are ISO strings; `now` is an aware-UTC `datetime`.
- No new nav section, no new DB table (state lives in `AutomationTargetState` via `AutomationContext`).
- Tests run with the prebuilt env: `mkdir -p /tmp/dm-home; env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …` — a scrubbed env AND an isolated HOME are both required on this host (the live shell carries mTLS/worker-role vars that make the app import go remote-mode, and the real HOME leaks a settings model that parks sessions). Never diagnose a failure seen without this scrub.
- Commit after every task; message ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; write messages to a file and pass `-F` (the Bash hook denies command text pairing a deploy verb with a prod word — note that `deploy` appears in every message here, so never put `prod`/`production` in a commit message or command line).

---

### Task 1: GitHub deployments reader

**Files:**
- Modify: `server/github/pr_status.py:246-248` (`_DEPLOY_NODE_FIELDS`)
- Create: `server/github/deployments.py`
- Test: `server/tests/test_github_deployments.py`

**Interfaces:**
- Consumes: `pr_status._DEPLOY_NODE_FIELDS`, `pr_status._alias`, `ccswitch.gitrepos._run_gh`, `gitrepos.app_token_for_repo`.
- Produces:
  - `@dataclass(frozen=True) Deployment(env: str, sha: str, ref: str | None, creator: str | None, created_at: datetime | None, status_state: str, status_at: datetime | None, log_url: str | None, env_url: str | None)`
  - `build_deployments_query(repos: list[str], *, first: int = 50) -> str`
  - `parse_iso(value: object) -> datetime | None` (aware UTC)
  - `parse_deployments_response(data: dict, repos: list[str]) -> dict[str, list[Deployment] | None]`
  - `by_env(deployments: list[Deployment]) -> dict[str, list[Deployment]]` (preserves newest-first order per env)
  - `fetch_deployments(repos: list[str], *, run=None) -> dict[str, list[Deployment] | None]` (never raises; `None` per repo on failure)

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_github_deployments.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from server.github import deployments as mod


def _node(env="kindash / admin", sha="a" * 40, state="FAILURE", created="2026-09-12T21:12:22Z",
          status_at="2026-09-12T21:15:00Z", log_url="https://railway.app/x", creator="railway-app"):
    return {
        "databaseId": 1, "createdAt": created, "latestEnvironment": env, "commitOid": sha,
        "ref": {"name": "main"}, "creator": {"login": creator},
        "latestStatus": {"state": state, "createdAt": status_at, "logUrl": log_url,
                         "environmentUrl": "https://admin.example"},
        "commit": {"committedDate": created},
    }


def test_query_aliases_one_block_per_repo_and_json_encodes_names():
    q = mod.build_deployments_query(["cairnea/KinDash", 'o/we"ird'])
    assert q.startswith("query {")
    assert 'r0: repository(owner: "cairnea", name: "KinDash")' in q
    assert 'r1: repository(owner: "o", name: "we\\"ird")' in q
    assert "first: 50" in q
    assert "latestStatus { state createdAt logUrl environmentUrl }" in q


def test_query_empty_repos_is_empty_string():
    assert mod.build_deployments_query([]) == ""


def test_parse_iso_handles_z_and_none():
    dt = mod.parse_iso("2026-09-12T21:12:22Z")
    assert dt == datetime(2026, 9, 12, 21, 12, 22, tzinfo=timezone.utc)
    assert mod.parse_iso(None) is None
    assert mod.parse_iso("garbage") is None


def test_parse_response_keeps_order_drops_malformed_and_nulls_forbidden_repo():
    data = {"data": {
        "r0": {"deployments": {"nodes": [
            _node(sha="1" * 40), {"latestEnvironment": None, "commitOid": "x"},
            _node(env="kindash / qa", sha="2" * 40, state="success"),
            {"latestEnvironment": "kindash / qa", "commitOid": "3" * 40, "latestStatus": None},
        ]}},
        "r1": None,
    }}
    out = mod.parse_deployments_response(data, ["cairnea/KinDash", "o/forbidden"])
    deps = out["cairnea/KinDash"]
    assert [d.sha[0] for d in deps] == ["1", "2", "3"]
    assert deps[0].status_state == "FAILURE" and deps[0].creator == "railway-app"
    assert deps[0].ref == "main" and deps[0].log_url == "https://railway.app/x"
    assert deps[0].env_url == "https://admin.example"
    assert deps[1].status_state == "SUCCESS"          # upper-cased
    assert deps[2].status_state == "" and deps[2].status_at is None  # null latestStatus
    assert out["o/forbidden"] is None


def test_parse_response_missing_alias_is_none():
    assert mod.parse_deployments_response({"data": {}}, ["a/b"]) == {"a/b": None}
    assert mod.parse_deployments_response({}, ["a/b"]) == {"a/b": None}


def test_by_env_groups_preserving_newest_first():
    deps = mod.parse_deployments_response({"data": {"r0": {"deployments": {"nodes": [
        _node(env="qa", sha="1" * 40), _node(env="admin", sha="2" * 40),
        _node(env="qa", sha="3" * 40)]}}}}, ["a/b"])["a/b"]
    grouped = mod.by_env(deps)
    assert [d.sha[0] for d in grouped["qa"]] == ["1", "3"]
    assert [d.sha[0] for d in grouped["admin"]] == ["2"]


def test_fetch_groups_by_token_and_maps_failures_to_none(monkeypatch):
    from ccswitch import gitrepos
    monkeypatch.setattr(gitrepos, "app_token_for_repo",
                        lambda repo: "tok-c" if repo.startswith("cairnea/") else None)
    calls: list[tuple[list[str], str | None]] = []

    def run(args, token=None):
        calls.append((args, token))
        if token == "tok-c":
            body = {"data": {"r0": {"deployments": {"nodes": [_node()]}}}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    out = mod.fetch_deployments(["cairnea/KinDash", "steiner385/viberpit"], run=run)
    assert len(calls) == 2
    assert {t for _, t in calls} == {"tok-c", None}
    assert out["steiner385/viberpit"] is None
    assert out["cairnea/KinDash"][0].env == "kindash / admin"


def test_fetch_never_raises_on_bad_json_or_exception():
    def bad_json(args, token=None):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")

    def explode(args, token=None):
        raise RuntimeError("gh missing")

    assert mod.fetch_deployments(["a/b"], run=bad_json) == {"a/b": None}
    assert mod.fetch_deployments(["a/b"], run=explode) == {"a/b": None}
    assert mod.fetch_deployments([], run=explode) == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_github_deployments.py -q`
Expected: FAIL — `ImportError: cannot import name 'deployments'`.

- [ ] **Step 3: Extend the shared node fields and write the module**

In `server/github/pr_status.py` replace the `_DEPLOY_NODE_FIELDS` constant with:

```python
# Shared deployment-node selection so the main-window and per-env supplement
# queries can never drift in which fields feed _active_deploy_dates. The deploy
# monitor (server/github/deployments.py) reads the same node, which is why the
# selection also carries identity, creator and the status's time/log URL.
_DEPLOY_NODE_FIELDS = (
    "databaseId createdAt latestEnvironment commitOid ref { name } creator { login } "
    "latestStatus { state createdAt logUrl environmentUrl } commit { committedDate }"
)
```

Create `server/github/deployments.py`:

```python
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
```

- [ ] **Step 4: Run the tests and the pr_status suite**

Run: `env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_github_deployments.py server/tests/test_pr_status.py -q`
Expected: all PASS (if `test_pr_status.py` does not exist, run `pytest server/tests -q -k "pr_status or population or env_deploy"`).

- [ ] **Step 5: Commit**

```bash
printf 'feat(github): deployments reader — newest deployments per repo as a monitoring signal\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/dm-t1.txt
git add server/github/pr_status.py server/github/deployments.py server/tests/test_github_deployments.py
git commit -F /tmp/dm-t1.txt
```

---

### Task 2: Deploy Monitor contract surface and pure policy

**Files:**
- Create: `server/automations/deploy_monitor.py` (pure half only; Task 3 appends the loop)
- Test: `server/tests/test_deploy_monitor_policy.py`

**Interfaces:**
- Consumes: `Deployment` (Task 1), `server.automations.base.{AutomationStatus, ConfigField, ActionableItem, ActionSpec, TriggerResult}`, `EnginePolicy`, `issue_filing.neutralize_markers`, `ci_issues.BODY_MAX_CHARS`, `server.github.deploy_envs.classify_env`, `ccswitch.brand`.
- Produces:
  - `LABEL = "deploy-monitor"`, `FAILING_STATES`, `PENDING_STATES`, `HEALTHY_STATES`
  - `class DeployMonitor` (id `deploy-monitor`, `modes`, `config_schema`, `status(ctx)` reading `ctx.deploy_monitor_counts`)
  - `@dataclass(frozen=True) EnvVerdict(env: str, kind: str, newest: Deployment, streak: int)` — kind ∈ `healthy|failing|stalled|unknown`
  - `evaluate_env(env: str, deployments: list[Deployment], *, now: datetime, grace_min: int, stall_min: int) -> EnvVerdict | None`
  - `watched_env(env: str, config: dict) -> bool`
  - `incident_key(repo: str, env: str) -> str` and `split_key(key: str) -> tuple[str, str]`
  - `issue_title(repo: str, env: str, state: dict) -> str`
  - `issue_body(repo: str, env: str, state: dict, *, regression_of: int | None) -> str`
  - `recurrence_comment(state: dict) -> str`, `recovery_comment(env: str, sha: str, now: datetime) -> str`
  - `async notify_deploy_failed(title: str, body: str, *, repo: str, url: str | None) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_deploy_monitor_policy.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.automations import deploy_monitor as dm
from server.github.deployments import Deployment

NOW = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)


def _dep(sha="a" * 40, state="FAILURE", status_age_min=10, created_age_min=12, env="kindash / admin"):
    return Deployment(env=env, sha=sha, ref="main", creator="railway-app",
                      created_at=NOW - timedelta(minutes=created_age_min),
                      status_state=state, status_at=NOW - timedelta(minutes=status_age_min),
                      log_url="https://railway.app/log", env_url="https://admin.example")


def _eval(deps, grace_min=5, stall_min=30):
    return dm.evaluate_env("kindash / admin", deps, now=NOW, grace_min=grace_min, stall_min=stall_min)


def test_contract_surface():
    a = dm.DeployMonitor()
    assert a.id == "deploy-monitor" and a.modes == ("off", "watch", "file")
    assert a.work_class == "maintenance"
    keys = [f.key for f in a.config_schema]
    assert keys == ["enabled_repos", "all_envs", "grace_min", "stall_min", "comment_every_h", "daily_cap"]
    assert a.status(None).level == "idle"


def test_status_levels_from_counts():
    class Ctx:
        deploy_monitor_counts = {"repos": 2, "envs": 5, "failing": 1, "stalled": 0, "incidents": 1}
    s = dm.DeployMonitor().status(Ctx())
    assert s.level == "error" and "1 incident" in s.text and "5 envs" in s.text
    Ctx.deploy_monitor_counts = {"repos": 2, "envs": 5, "failing": 0, "stalled": 1, "incidents": 1}
    assert dm.DeployMonitor().status(Ctx()).level == "warn"
    Ctx.deploy_monitor_counts = {"repos": 2, "envs": 5, "failing": 0, "stalled": 0, "incidents": 0}
    assert dm.DeployMonitor().status(Ctx()).level == "active"
    Ctx.deploy_monitor_counts = {"repos": 0, "envs": 0, "failing": 0, "stalled": 0, "incidents": 0}
    assert dm.DeployMonitor().status(Ctx()).level == "idle"


def test_empty_is_none():
    assert _eval([]) is None


def test_failing_past_grace_counts_streak():
    v = _eval([_dep(sha="1" * 40), _dep(sha="2" * 40, state="ERROR"), _dep(sha="3" * 40, state="SUCCESS"),
               _dep(sha="4" * 40)])
    assert v.kind == "failing" and v.streak == 2 and v.newest.sha == "1" * 40


def test_failing_inside_grace_is_unknown():
    assert _eval([_dep(status_age_min=2)]).kind == "unknown"


def test_failing_with_no_timestamps_is_failing():
    d = Deployment(env="e", sha="x" * 40, ref=None, creator=None, created_at=None,
                   status_state="FAILURE", status_at=None, log_url=None, env_url=None)
    assert _eval([d]).kind == "failing"


def test_stalled_after_stall_window_else_unknown():
    assert _eval([_dep(state="IN_PROGRESS", created_age_min=45)]).kind == "stalled"
    assert _eval([_dep(state="", created_age_min=45)]).kind == "stalled"
    assert _eval([_dep(state="PENDING", created_age_min=5)]).kind == "unknown"


def test_healthy_states():
    assert _eval([_dep(state="SUCCESS"), _dep(state="FAILURE")]).kind == "healthy"
    assert _eval([_dep(state="INACTIVE")]).kind == "healthy"


def test_unrecognised_state_is_unknown():
    assert _eval([_dep(state="BANANA")]).kind == "unknown"


def test_watched_env_filters_unclassified_unless_all_envs():
    assert dm.watched_env("kindash / admin", {})
    assert dm.watched_env("production", {})
    assert not dm.watched_env("preview-123", {})
    assert dm.watched_env("preview-123", {"all_envs": True})


def test_incident_key_roundtrip():
    key = dm.incident_key("cairnea/KinDash", "kindash / admin#x")
    assert key == "cairnea/KinDash#kindash / admin#x"
    assert dm.split_key(key) == ("cairnea/KinDash", "kindash / admin#x")


def _state(**over):
    base = {"kind": "failing", "sha": "a" * 40, "ref": "main", "creator": "railway-app",
            "first_sha": "b" * 40, "first_seen_at": "2026-09-12T20:23:10+00:00",
            "last_seen_at": "2026-09-12T22:00:00+00:00", "streak": 3,
            "log_url": "https://railway.app/log <!-- x -->", "env_url": "https://admin.example"}
    base.update(over)
    return base


def test_issue_title_and_body():
    t = dm.issue_title("cairnea/KinDash", "kindash / admin", _state())
    assert t == "[deploy-monitor] kindash / admin deploy failing — cairnea/KinDash @ aaaaaaaaaa"
    assert "stalled" in dm.issue_title("o/r", "qa", _state(kind="stalled"))
    body = dm.issue_body("cairnea/KinDash", "kindash / admin", _state(), regression_of=None)
    assert "| Environment | kindash / admin |" in body
    assert "3" in body and "railway-app" in body and "bbbbbbbbbb" in body
    assert "<!--" not in body                       # neutralized
    assert body.rstrip().endswith("Target: `cairnea/KinDash#kindash / admin`")
    assert "Regression of" not in body
    assert "#41" in dm.issue_body("o/r", "qa", _state(), regression_of=41)


def test_issue_body_is_capped():
    from server.github import ci_issues
    body = dm.issue_body("o/r", "qa", _state(log_url="x" * 20000), regression_of=None)
    assert len(body) <= ci_issues.BODY_MAX_CHARS


def test_comments():
    c = dm.recurrence_comment(_state(streak=4))
    assert "4" in c and "aaaaaaaaaa" in c
    r = dm.recovery_comment("kindash / admin", "c" * 40, NOW)
    assert "cccccccccc" in r and "2026-09-12 22:00 UTC" in r
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_deploy_monitor_policy.py -q`
Expected: FAIL — `ImportError: cannot import name 'deploy_monitor'`.

- [ ] **Step 3: Write the pure half**

Create `server/automations/deploy_monitor.py`:

```python
"""Deploy Monitor — GitHub deployment statuses → incidents, alerts and issues
(spec docs/superpowers/specs/2026-09-12-deploy-monitor-design.md).

Two halves, the Error Triage / Control-Band mold: the pure half (contract
surface, ``evaluate_env`` policy, issue text) is at the top; ``DeployMonitorLoop``
(the tick, every I/O seam injected) and the card action provider follow.
The signal is read by server/github/deployments.py; the file/comment/regress
rule is server/automations/issue_filing.py.
"""
from __future__ import annotations

import asyncio  # noqa: F401  (the loop half uses it)
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable  # noqa: F401

from ccswitch import brand, gitrepos  # noqa: F401
from server.github import ci_issues
from server.github.deploy_envs import classify_env
from server.github.deployments import Deployment, by_env, parse_iso  # noqa: F401

from .base import ActionableItem, ActionSpec, AutomationStatus, ConfigField, TriggerResult  # noqa: F401
from .engine_policy import EnginePolicy
from .issue_filing import Decision, Linked, apply, decide, neutralize_markers  # noqa: F401

log = logging.getLogger(__name__)

LABEL = "deploy-monitor"
_TITLE_MAX = 120
_TS = "%Y-%m-%d %H:%M UTC"

#: GitHub DeploymentStatusState buckets. ``""`` is "no status yet" — a deployment
#: GitHub created but nothing has reported on, which is the stalled-deploy shape.
FAILING_STATES = frozenset({"FAILURE", "ERROR"})
PENDING_STATES = frozenset({"PENDING", "QUEUED", "WAITING", "IN_PROGRESS", ""})
HEALTHY_STATES = frozenset({"SUCCESS", "INACTIVE"})
INCIDENT_KINDS = ("failing", "stalled")


class DeployMonitor:
    id = "deploy-monitor"
    name = "Deploy Monitor"
    work_class = "maintenance"
    description = ("Watches GitHub deployment statuses per environment. A failing or "
                   "stalled deploy opens an incident, alerts once and — in file mode — "
                   "keeps one issue open in the repo until the environment recovers.")
    #: ``watch`` detects, feeds and alerts but never writes to GitHub; ``file`` adds
    #: the issue lifecycle. Turning it on is therefore a reversible, observable step.
    modes: tuple[str, ...] = ("off", "watch", "file")
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY  # never spawns a session
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Limit to these repos (empty = all managed repos)."),
        ConfigField(key="all_envs", label="Watch every environment", type="bool", default=False,
                    help="Also watch environments that are not qa / prod / admin "
                         "(preview and ad-hoc environments)."),
        ConfigField(key="grace_min", label="Failure grace (minutes)", type="int", default=5,
                    min=0, help="A failed status must stand this long before it counts — "
                                "absorbs a platform's automatic retry."),
        ConfigField(key="stall_min", label="Stall after (minutes)", type="int", default=30,
                    min=1, help="A deployment still in progress this long is an incident."),
        ConfigField(key="comment_every_h", label="Recurrence comment gap (hours)", type="int",
                    default=6, min=1),
        ConfigField(key="daily_cap", label="New issues per day", type="int", default=10, min=0),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "deploy_monitor_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        repos, envs = int(counts.get("repos", 0)), int(counts.get("envs", 0))
        failing, stalled = int(counts.get("failing", 0)), int(counts.get("stalled", 0))
        incidents = int(counts.get("incidents", failing + stalled))
        text = (f"{repos} repo{'' if repos == 1 else 's'} · {envs} envs watched · "
                f"{incidents} incident{'' if incidents == 1 else 's'}")
        if failing:
            level = "error"
        elif stalled:
            level = "warn"
        elif envs:
            level = "active"
        else:
            level = "idle"
        return AutomationStatus(text=text, level=level, counts=dict(counts))

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        ...  # the loop reads mode/config each tick


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvVerdict:
    env: str
    kind: str        # healthy | failing | stalled | unknown
    newest: Deployment
    streak: int      # consecutive newest-first deployments with a failing status


def evaluate_env(env: str, deployments: list[Deployment], *, now: datetime,
                 grace_min: int, stall_min: int) -> EnvVerdict | None:
    """Classify one environment from its newest-first deployments (spec §5.2).

    ``unknown`` means "say nothing this tick" — a young failure inside the grace
    window, a young in-progress deploy, or a state we do not model. The loop
    neither opens nor closes an incident on it."""
    if not deployments:
        return None
    newest = deployments[0]
    state = newest.status_state
    if state in FAILING_STATES:
        at = newest.status_at or newest.created_at
        if at is not None and now - at < timedelta(minutes=int(grace_min)):
            return EnvVerdict(env, "unknown", newest, 0)
        streak = 0
        for d in deployments:
            if d.status_state not in FAILING_STATES:
                break
            streak += 1
        return EnvVerdict(env, "failing", newest, streak)
    if state in PENDING_STATES:
        at = newest.created_at
        if at is not None and now - at >= timedelta(minutes=int(stall_min)):
            return EnvVerdict(env, "stalled", newest, 1)
        return EnvVerdict(env, "unknown", newest, 0)
    if state in HEALTHY_STATES:
        return EnvVerdict(env, "healthy", newest, 0)
    return EnvVerdict(env, "unknown", newest, 0)


def watched_env(env: str, config: dict) -> bool:
    """Preview / ad-hoc environments never open incidents unless asked to."""
    return bool(config.get("all_envs")) or classify_env(env) is not None


def incident_key(repo: str, env: str) -> str:
    return f"{repo}#{env}"


def split_key(key: str) -> tuple[str, str]:
    """Inverse of ``incident_key``. A repo is ``owner/name`` and never holds ``#``,
    so the FIRST ``#`` is the seam even if the env name contains one."""
    repo, _, env = key.partition("#")
    return repo, env


def _fmt(value: object) -> str:
    dt = value if isinstance(value, datetime) else parse_iso(value)
    return dt.strftime(_TS) if dt else "?"


def _short(sha: object) -> str:
    return str(sha or "")[:10] or "?"


def issue_title(repo: str, env: str, state: dict) -> str:
    word = "stalled" if state.get("kind") == "stalled" else "failing"
    return f"[{LABEL}] {env} deploy {word} — {repo} @ {_short(state.get('sha'))}"[:_TITLE_MAX]


def _cell(value: object) -> str:
    text = neutralize_markers(str(value)) if value not in (None, "") else "—"
    return text.replace("|", "\\|").replace("\n", " ")


def issue_body(repo: str, env: str, state: dict, *, regression_of: int | None) -> str:
    """Machine-collected; GitHub-sourced text (ref, creator, URLs) is untrusted
    and passed through ``neutralize_markers``. Hard-capped at BODY_MAX_CHARS."""
    rows = [
        ("Repository", repo),
        ("Environment", env),
        ("Kind", state.get("kind")),
        ("Newest deployment", f"`{_short(state.get('sha'))}` on {_cell(state.get('ref'))}"),
        ("First failing deployment", f"`{_short(state.get('first_sha'))}`"),
        ("Consecutive failures", int(state.get("streak") or 0)),
        ("First seen", _fmt(state.get("first_seen_at"))),
        ("Last seen", _fmt(state.get("last_seen_at"))),
        ("Created by", state.get("creator")),
        ("Log", state.get("log_url")),
        ("Environment URL", state.get("env_url")),
    ]
    if regression_of is not None:
        rows.append(("Regression of", f"#{int(regression_of)}"))
    table = "\n".join(f"| {k} | {_cell(v)} |" for k, v in rows)
    head = (f"**Filed by {brand.NAME} Deploy Monitor** from GitHub deployment statuses — "
            f"the environment's newest deployment did not reach SUCCESS.\n\n"
            f"| | |\n|---|---|\n{table}\n")
    tail = f"\nTarget: `{incident_key(repo, env)}`\n"
    body = head + tail
    if len(body) > ci_issues.BODY_MAX_CHARS:
        body = body[:ci_issues.BODY_MAX_CHARS - len(tail) - 16] + "\n…(truncated)\n" + tail
    return body


def recurrence_comment(state: dict) -> str:
    return (f"Still {state.get('kind', 'failing')}: newest deployment `{_short(state.get('sha'))}` "
            f"({int(state.get('streak') or 0)} consecutive), last seen {_fmt(state.get('last_seen_at'))}.")


def recovery_comment(env: str, sha: str, now: datetime) -> str:
    return f"Recovered: `{env}` deployed `{_short(sha)}` successfully at {now.strftime(_TS)}. Closing."


async def notify_deploy_failed(title: str, body: str, *, repo: str, url: str | None) -> None:
    """The one ``deploy_failed`` call site. Fans out like ``pr_broke`` when no
    rules are configured: a failing deploy is what a user with no rules still
    expects to hear about. (test_assistant_watches_api source-walks this literal.)"""
    from server.sessions.push import notify_event
    await notify_event("deploy_failed", title, body, repo=repo, url=url,
                       fanout_when_unconfigured=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
```

- [ ] **Step 4: Run the tests**

Run: `env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_deploy_monitor_policy.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
printf 'feat(automations): deploy monitor — contract surface and pure verdict/issue-text policy\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/dm-t2.txt
git add server/automations/deploy_monitor.py server/tests/test_deploy_monitor_policy.py
git commit -F /tmp/dm-t2.txt
```

---

### Task 3: The loop and the card action provider

**Files:**
- Modify: `server/automations/deploy_monitor.py` (append below `_utcnow`)
- Test: `server/tests/test_deploy_monitor_loop.py`

**Interfaces:**
- Consumes: everything Task 2 produces; `issue_filing.{Linked, Decision, decide, apply}`; `ci_issues.{issue_states, comment_on_issue, close_issue, issue_url, valid_repo}`; `server.github.deployments.{by_env, parse_iso}`; an `AutomationContext`-shaped ctx (`emit`, `get_target_state`, `set_target_state`, `delete_target_state`, `list_target_states`).
- Produces:
  - `class DeployMonitorLoop(ctx, *, read_config, list_repos, fetch_deployments, run=gitrepos._run_gh, notify=notify_deploy_failed, clock=_utcnow)` with `async tick()`, `async actionable_items(limit=50)`, `async action_result(key, kind) -> tuple[str, int | None]`
  - `class DeployMonitorActionProvider(orch)` with `valid_kinds = {"file_now", "mute", "unmute"}`, reading `orch._deploy_monitor_loop`.
  - Target-state row shape (JSON): `kind, sha, ref, creator, first_sha, first_seen_at, last_seen_at, streak, log_url, env_url, issue_number, filed_at, last_commented_at, last_commented_sha, issue_closed, closed_sha, recovery_commented, notified, muted`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_deploy_monitor_loop.py
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from server.automations import deploy_monitor as dm
from server.github.deployments import Deployment

T0 = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)
REPO = "cairnea/KinDash"
ADMIN = "kindash / admin"


class FakeCtx:
    """In-memory stand-in for AutomationContext: events + target state."""

    def __init__(self):
        self.events: list[dict] = []
        self.states: dict[str, dict] = {}

    async def emit(self, level, summary, *, kind=None, target=None, session_id=None, detail=None):
        self.events.append({"level": level, "summary": summary, "kind": kind,
                            "target": target, "detail": detail})

    async def get_target_state(self, target):
        return dict(self.states.get(target, {}))

    async def set_target_state(self, target, state):
        self.states[target] = dict(state)

    async def delete_target_state(self, target):
        self.states.pop(target, None)

    async def list_target_states(self):
        return {k: dict(v) for k, v in self.states.items()}

    def kinds(self):
        return [e["kind"] for e in self.events]


class FakeGh:
    """Records gh calls; answers issue creates with sequential numbers and
    issue_states GraphQL with ``self.closed``."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.next_number = 100
        self.closed: dict[int, str | None] = {}   # number -> stateReason ("completed"/"not_planned")
        self.fail_create = False

    def __call__(self, args, token=None):
        import json
        self.calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            nodes = {f"i{n}": {"number": n, "closed": True, "stateReason": r}
                     for n, r in self.closed.items()}
            return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"repository": nodes}}), stderr="")
        if args[0] == "api" and args[1].endswith("/issues") and "-X" not in args:
            if self.fail_create:
                return SimpleNamespace(returncode=1, stdout="", stderr="nope")
            self.next_number += 1
            return SimpleNamespace(returncode=0, stdout=json.dumps({"number": self.next_number}), stderr="")
        if "/comments" in args[1]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"html_url": "https://x/c/1"}), stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    def creates(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/issues") and "-X" not in c]

    def comments(self):
        return [c for c in self.calls if len(c) > 1 and "/comments" in c[1]]

    def closes(self):
        return [c for c in self.calls if "-X" in c and "state=closed" in c]

    def labels(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/labels")]


def _dep(env=ADMIN, sha="a" * 40, state="FAILURE", age_min=10):
    return Deployment(env=env, sha=sha, ref="main", creator="railway-app",
                      created_at=T0 - timedelta(minutes=age_min + 2), status_state=state,
                      status_at=T0 - timedelta(minutes=age_min), log_url="https://railway.app/log",
                      env_url=None)


class Harness:
    def __init__(self, mode="file", config=None):
        self.ctx = FakeCtx()
        self.gh = FakeGh()
        self.mode = mode
        self.config = {"enabled_repos": [], "all_envs": False, "grace_min": 5, "stall_min": 30,
                       "comment_every_h": 6, "daily_cap": 10, **(config or {})}
        self.now = T0
        self.deployments: dict[str, list[Deployment] | None] = {REPO: [_dep()]}
        self.notified: list[dict] = []
        self.notify_fails = False

        async def read_config():
            return self.mode, dict(self.config)

        async def list_repos(config):
            return list(self.deployments.keys())

        async def fetch(repos):
            return {r: self.deployments.get(r) for r in repos}

        async def notify(title, body, *, repo, url):
            if self.notify_fails:
                raise RuntimeError("push down")
            self.notified.append({"title": title, "body": body, "repo": repo, "url": url})

        self.loop = dm.DeployMonitorLoop(self.ctx, read_config=read_config, list_repos=list_repos,
                                         fetch_deployments=fetch, run=self.gh, notify=notify,
                                         clock=lambda: self.now)

    def tick(self):
        asyncio.run(self.loop.tick())

    def state(self, env=ADMIN):
        return self.ctx.states.get(dm.incident_key(REPO, env))


KEY = dm.incident_key(REPO, ADMIN)


def test_new_incident_alerts_once_and_files_one_issue():
    h = Harness()
    h.tick()
    st = h.state()
    assert st and st["kind"] == "failing" and st["issue_number"] == 101 and st["notified"] is True
    assert len(h.notified) == 1 and h.notified[0]["repo"] == REPO
    assert h.notified[0]["url"] == "https://railway.app/log"
    assert len(h.gh.creates()) == 1 and any("labels[]=deploy-monitor" in c for c in h.gh.creates()[0])
    assert "incident" in h.ctx.kinds() and "filed" in h.ctx.kinds()
    assert h.ctx.deploy_monitor_counts == {"repos": 1, "envs": 1, "failing": 1, "stalled": 0, "incidents": 1}
    h.tick()   # same sha: nothing new
    assert len(h.notified) == 1 and len(h.gh.creates()) == 1 and not h.gh.comments()


def test_watch_mode_alerts_but_never_writes_github():
    h = Harness(mode="watch")
    h.tick()
    assert len(h.notified) == 1 and h.state()["issue_number"] is None
    assert not h.gh.calls


def test_off_mode_does_nothing_and_stashes_zero_counts():
    h = Harness(mode="off")
    h.tick()
    assert not h.ctx.states and not h.notified and not h.gh.calls
    assert h.ctx.deploy_monitor_counts["envs"] == 0


def test_unclassified_env_ignored_unless_all_envs():
    h = Harness()
    h.deployments = {REPO: [_dep(env="preview-7")]}
    h.tick()
    assert not h.ctx.states
    h.config["all_envs"] = True
    h.tick()
    assert h.state("preview-7")


def test_young_failure_inside_grace_opens_nothing_then_opens():
    h = Harness()
    h.deployments = {REPO: [_dep(age_min=1)]}
    h.tick()
    assert not h.ctx.states and not h.notified
    h.now = T0 + timedelta(minutes=10)
    h.tick()
    assert h.state()


def test_ongoing_comments_only_when_sha_moves_and_throttled():
    h = Harness()
    h.tick()
    h.deployments = {REPO: [_dep(sha="b" * 40), _dep(sha="a" * 40)]}
    h.now = T0 + timedelta(minutes=10)
    h.tick()
    assert len(h.gh.comments()) == 0            # inside comment_every_h since filing
    h.now = T0 + timedelta(hours=7)
    h.tick()
    assert len(h.gh.comments()) == 1 and h.state()["last_commented_sha"] == "b" * 40
    assert h.state()["streak"] == 2
    h.now = T0 + timedelta(hours=20)
    h.tick()                                     # sha unchanged since last comment
    assert len(h.gh.comments()) == 1
    assert len(h.notified) == 1


def test_recovery_comments_closes_and_forgets():
    h = Harness()
    h.tick()
    h.deployments = {REPO: [_dep(sha="c" * 40, state="SUCCESS"), _dep()]}
    h.tick()
    assert h.state() is None
    assert len(h.gh.comments()) == 1 and len(h.gh.closes()) == 1
    assert "recovered" in h.ctx.kinds() and "closed" in h.ctx.kinds()
    assert h.ctx.deploy_monitor_counts["incidents"] == 0


def test_recovery_in_watch_mode_forgets_without_writing():
    h = Harness(mode="watch")
    h.tick()
    h.deployments = {REPO: [_dep(state="SUCCESS")]}
    h.tick()
    assert h.state() is None and not h.gh.calls


def test_close_failure_keeps_row_and_retries_without_duplicate_comment():
    h = Harness()
    h.tick()
    close_calls = {"n": 0}
    real = h.gh.__call__

    def flaky(args, token=None):
        if "state=closed" in args:
            close_calls["n"] += 1
            if close_calls["n"] == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="503")
        return real(args, token=token)

    h.loop._run = flaky
    h.deployments = {REPO: [_dep(state="SUCCESS")]}
    h.tick()
    assert h.state() is not None and h.state()["recovery_commented"] is True
    h.tick()
    assert h.state() is None and close_calls["n"] == 2
    assert len(h.gh.comments()) == 1


def test_fetch_failure_changes_nothing_and_warns_once_per_outage():
    h = Harness()
    h.tick()
    h.deployments = {REPO: None}
    h.tick()
    h.tick()
    assert h.state()["issue_number"] == 101 and h.ctx.kinds().count("fetch_failed") == 1
    h.deployments = {REPO: [_dep()]}
    h.tick()
    assert "fetch_recovered" in h.ctx.kinds()


def test_failed_create_is_retried_next_tick():
    h = Harness()
    h.gh.fail_create = True
    h.tick()
    assert h.state()["issue_number"] is None and "file_failed" in h.ctx.kinds()
    h.gh.fail_create = False
    h.tick()
    assert h.state()["issue_number"] == 101 and len(h.gh.creates()) == 2


def test_daily_cap_blocks_new_issues_but_not_alerts():
    h = Harness(config={"daily_cap": 1})
    h.deployments = {REPO: [_dep(), _dep(env="kindash / qa", sha="q" * 40)]}
    h.tick()
    assert len(h.gh.creates()) == 1 and len(h.notified) == 2
    assert h.ctx.kinds().count("cap") == 1


def test_closed_not_planned_mutes_until_recovery():
    h = Harness()
    h.tick()
    h.gh.closed = {101: "not_planned"}
    h.deployments = {REPO: [_dep(sha="b" * 40), _dep()]}
    h.now = T0 + timedelta(hours=8)
    h.tick()
    assert h.state()["muted"] is True and not h.gh.comments() and len(h.gh.creates()) == 1
    assert "muted" in h.ctx.kinds()
    h.deployments = {REPO: [_dep(state="SUCCESS")]}
    h.tick()
    assert h.state() is None and not h.gh.closes()   # already closed by the human


def test_closed_completed_regresses_only_on_a_new_sha():
    h = Harness()
    h.tick()
    h.gh.closed = {101: "completed"}
    h.now = T0 + timedelta(hours=1)
    h.tick()                                   # same sha: acknowledged, no new issue
    assert h.state()["issue_closed"] is True and len(h.gh.creates()) == 1
    h.deployments = {REPO: [_dep(sha="b" * 40), _dep()]}
    h.gh.closed = {}
    h.now = T0 + timedelta(hours=2)
    h.tick()
    assert len(h.gh.creates()) == 2 and h.state()["issue_number"] == 102
    assert h.state()["issue_closed"] is False
    assert any("regression" in e["summary"].lower() or e["kind"] == "regressed" for e in h.ctx.events)


def test_notify_failure_retries_next_tick():
    h = Harness(mode="watch")
    h.notify_fails = True
    h.tick()
    assert h.state()["notified"] is False
    h.notify_fails = False
    h.tick()
    assert h.state()["notified"] is True and len(h.notified) == 1


def test_stalled_then_failing_is_one_incident():
    h = Harness(mode="watch")
    h.deployments = {REPO: [_dep(state="IN_PROGRESS", age_min=40)]}
    h.tick()
    assert h.state()["kind"] == "stalled"
    h.deployments = {REPO: [_dep(state="FAILURE", age_min=50)]}
    h.tick()
    assert h.state()["kind"] == "failing" and len(h.notified) == 1
    assert h.ctx.kinds().count("incident") == 1


def test_actionable_items_and_actions():
    h = Harness(mode="watch")
    h.tick()
    items = asyncio.run(h.loop.actionable_items())
    assert len(items) == 1 and items[0].id == KEY
    assert {a.kind for a in items[0].actions} == {"file_now", "mute"}
    assert items[0].url == "https://railway.app/log"

    assert asyncio.run(h.loop.action_result(KEY, "mute")) == ("muted", None)
    items = asyncio.run(h.loop.actionable_items())
    assert {a.kind for a in items[0].actions} == {"unmute"}
    assert asyncio.run(h.loop.action_result(KEY, "unmute")) == ("unmuted", None)

    status, number = asyncio.run(h.loop.action_result(KEY, "file_now"))
    assert status == "filed" and number == 101
    items = asyncio.run(h.loop.actionable_items())
    assert {a.kind for a in items[0].actions} == {"mute"} and items[0].url.endswith("/issues/101")
    assert asyncio.run(h.loop.action_result("nope#x", "mute")) == ("not_found", None)
    h.mode = "off"
    assert asyncio.run(h.loop.action_result(KEY, "file_now")) == ("off", None)


def test_action_provider_maps_statuses():
    h = Harness(mode="watch")
    h.tick()
    orch = SimpleNamespace(_deploy_monitor_loop=h.loop)
    p = dm.DeployMonitorActionProvider(orch)
    assert p.valid_kinds == frozenset({"file_now", "mute", "unmute"})
    r = asyncio.run(p.trigger(KEY, "file_now"))
    assert r.status == "filed" and r.level == "ok" and "#101" in r.message
    r = asyncio.run(p.trigger("nope#x", "mute"))
    assert r.level == "warn"


def test_tick_never_raises(monkeypatch):
    h = Harness()

    async def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(h.loop, "_run_once", boom)
    h.tick()   # no exception


@pytest.mark.parametrize("bad", ["not-a-repo", "a/../b"])
def test_invalid_repo_is_skipped(bad):
    h = Harness()
    h.deployments = {bad: [_dep()]}
    h.tick()
    assert not h.ctx.states
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_deploy_monitor_loop.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'DeployMonitorLoop'`.

- [ ] **Step 3: Append the loop and provider**

Append to `server/automations/deploy_monitor.py`:

```python
# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------
# (The imports this half uses — gitrepos, by_env, parse_iso, Decision, Linked,
# apply, decide, asyncio, Awaitable, Callable — are already in the module's top
# import block from Task 2; delete their `# noqa: F401` markers now that they
# are used.)

ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ListRepos = Callable[[dict], Awaitable[list[str]]]
FetchDeployments = Callable[[list[str]], Awaitable[dict[str, list[Deployment] | None]]]
Notify = Callable[..., Awaitable[None]]

_DAY = timedelta(days=1)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class DeployMonitorLoop:
    """Restart-safe, non-reentrant tick. Every I/O seam is injected: ``run`` is
    the gh runner (``gitrepos._run_gh``), ``notify`` the ``deploy_failed`` push,
    ``clock`` an aware-UTC now. Incident state lives in the ctx's target-state
    rows keyed ``repo#env`` (spec §5.3)."""

    def __init__(self, ctx: Any, *,
                 read_config: ReadConfig,
                 list_repos: ListRepos,
                 fetch_deployments: FetchDeployments,
                 run: Callable[..., Any] = gitrepos._run_gh,
                 notify: Notify = notify_deploy_failed,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_repos = list_repos
        self._fetch = fetch_deployments
        self._run = run
        self._notify = notify
        self._clock = clock
        self._lock = asyncio.Lock()
        self._filed: list[datetime] = []          # rolling 24 h issue creates (daily cap)
        self._unreadable: set[str] = set()        # repos whose last fetch failed

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run_once()
            except Exception:  # noqa: BLE001 — a tick must never crash the loop
                log.warning("deploy_monitor.tick_failed", exc_info=True)

    async def _run_once(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            self._stash_counts(0, 0, {})
            return
        now = self._clock()
        self._filed = [t for t in self._filed if now - t < _DAY]
        repos = []
        for repo in await self._list_repos(config):
            if ci_issues.valid_repo(str(repo)):
                repos.append(str(repo))
            else:
                log.warning("deploy_monitor.invalid_repo repo=%r", repo)
        fetched = await self._fetch(repos) if repos else {}
        states = await self._ctx.list_target_states()
        watched = 0
        for repo in repos:
            deps = fetched.get(repo)
            if deps is None:
                if repo not in self._unreadable:
                    self._unreadable.add(repo)
                    await self._ctx.emit("warn", f"could not read deployments for {repo}; "
                                         "leaving its incidents untouched",
                                         kind="fetch_failed", target=repo)
                continue
            if repo in self._unreadable:
                self._unreadable.discard(repo)
                await self._ctx.emit("info", f"deployments readable again for {repo}",
                                     kind="fetch_recovered", target=repo)
            if mode == "file":
                await self._sync_issue_states(repo, states, now)
            for env, env_deps in by_env(deps).items():
                if not watched_env(env, config):
                    continue
                watched += 1
                verdict = evaluate_env(env, env_deps, now=now,
                                       grace_min=int(config.get("grace_min", 5)),
                                       stall_min=int(config.get("stall_min", 30)))
                key = incident_key(repo, env)
                await self._reconcile(repo, env, key, states.get(key), verdict, mode, config, now)
        self._stash_counts(len(repos), watched, await self._ctx.list_target_states())

    # -- issue state sync ------------------------------------------------------
    async def _sync_issue_states(self, repo: str, states: dict[str, dict], now: datetime) -> None:
        """Learn which of this repo's linked issues a human closed. ``not_planned``
        mutes the incident until recovery; any other close is an acknowledgment
        of the CURRENT sha (a later sha regresses — spec §5.4)."""
        keys = [k for k, s in states.items() if split_key(k)[0] == repo
                and s.get("issue_number") and not s.get("issue_closed") and not s.get("muted")]
        if not keys:
            return
        numbers = [int(states[k]["issue_number"]) for k in keys]
        closed = await asyncio.to_thread(ci_issues.issue_states, repo, numbers, run=self._run)
        for key in keys:
            state = states[key]
            info = closed.get(int(state["issue_number"]))
            if not info or not info[0]:
                continue
            _, reason = info
            if reason == "not_planned":
                state["muted"] = True
                state["issue_closed"] = True
                await self._ctx.emit("info", f"#{state['issue_number']} closed as not planned — "
                                     f"{key} muted until it recovers", kind="muted", target=key)
            else:
                state["issue_closed"] = True
                state["closed_sha"] = state.get("sha")
            await self._ctx.set_target_state(key, state)

    # -- per-env reconcile -----------------------------------------------------
    async def _reconcile(self, repo: str, env: str, key: str, state: dict | None,
                         verdict: EnvVerdict | None, mode: str, config: dict, now: datetime) -> None:
        if verdict is None or verdict.kind == "unknown":
            return
        if verdict.kind == "healthy":
            if state:
                await self._recover(repo, env, key, state, verdict, mode, now)
            return
        if not state:
            await self._open(repo, env, key, verdict, mode, config, now)
        else:
            await self._ongoing(repo, env, key, state, verdict, mode, config, now)

    def _measure(self, state: dict, verdict: EnvVerdict, now: datetime) -> dict:
        d = verdict.newest
        state.update(kind=verdict.kind, sha=d.sha, ref=d.ref, creator=d.creator,
                     streak=max(int(verdict.streak), 1), last_seen_at=_iso(now),
                     log_url=d.log_url, env_url=d.env_url)
        return state

    async def _open(self, repo: str, env: str, key: str, verdict: EnvVerdict,
                    mode: str, config: dict, now: datetime) -> None:
        d = verdict.newest
        state = self._measure({
            "first_sha": d.sha, "first_seen_at": _iso(now), "issue_number": None,
            "filed_at": None, "last_commented_at": None, "last_commented_sha": None,
            "issue_closed": False, "closed_sha": None, "recovery_commented": False,
            "notified": False, "muted": False,
        }, verdict, now)
        await self._ctx.set_target_state(key, state)
        streak = f" ({verdict.streak} in a row)" if verdict.streak > 1 else ""
        summary = f"{repo} · {env} deploy {verdict.kind} @ {_short(d.sha)}{streak}"
        await self._ctx.emit("warn", summary, kind="incident", target=key,
                             detail={"sha": d.sha, "kind": verdict.kind, "streak": verdict.streak,
                                     "logUrl": d.log_url})
        await self._notify_once(repo, key, state, summary)
        if mode == "file":
            await self._file(repo, env, key, state, config, now, regression_of=None)

    async def _ongoing(self, repo: str, env: str, key: str, state: dict, verdict: EnvVerdict,
                       mode: str, config: dict, now: datetime) -> None:
        d = verdict.newest
        self._measure(state, verdict, now)
        await self._ctx.set_target_state(key, state)
        if not state.get("notified"):
            summary = f"{repo} · {env} deploy {verdict.kind} @ {_short(d.sha)}"
            await self._notify_once(repo, key, state, summary)
        if mode != "file" or state.get("muted"):
            return
        if state.get("issue_number") is None:
            await self._file(repo, env, key, state, config, now, regression_of=None)
            return
        if state.get("issue_closed"):
            if d.sha != state.get("closed_sha"):
                await self._file(repo, env, key, state, config, now,
                                 regression_of=int(state["issue_number"]))
            return
        last_sha = state.get("last_commented_sha") or state.get("first_sha")
        if d.sha == last_sha:
            return
        since = parse_iso(state.get("last_commented_at")) or parse_iso(state.get("filed_at"))
        linked = Linked(number=int(state["issue_number"]), closed=False, last_commented_at=since)
        decision = decide(linked, cap_remaining=1,
                          comment_every=timedelta(hours=int(config.get("comment_every_h", 6))), now=now)
        if decision.kind != "comment":
            return
        applied = await apply(decision, repo=repo, title="", body=recurrence_comment(state),
                              labels=(), linked=linked, run=self._run)
        if not applied.commented:
            return  # retried next tick; last_commented_* only move on success
        state["last_commented_at"] = _iso(now)
        state["last_commented_sha"] = d.sha
        await self._ctx.set_target_state(key, state)
        await self._ctx.emit("info", f"commented on #{linked.number}: {env} still {verdict.kind} "
                             f"@ {_short(d.sha)}", kind="commented", target=key)

    async def _recover(self, repo: str, env: str, key: str, state: dict, verdict: EnvVerdict,
                       mode: str, now: datetime) -> None:
        sha = verdict.newest.sha
        number = state.get("issue_number")
        if mode == "file" and number and not state.get("issue_closed"):
            if not state.get("recovery_commented"):
                await asyncio.to_thread(ci_issues.comment_on_issue, repo, int(number),
                                        recovery_comment(env, sha, now), run=self._run)
                state["recovery_commented"] = True
                await self._ctx.set_target_state(key, state)
            ok = await asyncio.to_thread(ci_issues.close_issue, repo, int(number), run=self._run)
            if not ok:
                await self._ctx.emit("warn", f"could not close #{number} for {key}; will retry",
                                     kind="close_failed", target=key)
                return
            await self._ctx.emit("info", f"closed #{number}: {env} recovered @ {_short(sha)}",
                                 kind="closed", target=key,
                                 detail={"url": ci_issues.issue_url(repo, int(number))})
        await self._ctx.delete_target_state(key)
        await self._ctx.emit("info", f"{repo} · {env} recovered @ {_short(sha)}",
                             kind="recovered", target=key, detail={"sha": sha})

    # -- side effects ----------------------------------------------------------
    async def _notify_once(self, repo: str, key: str, state: dict, summary: str) -> None:
        if state.get("notified"):
            return
        try:
            await self._notify(f"{brand.NAME} — deploy {state.get('kind', 'failing')}", summary,
                               repo=repo, url=state.get("log_url"))
        except Exception:
            log.debug("deploy_monitor.notify_failed key=%s", key, exc_info=True)
            return
        state["notified"] = True
        await self._ctx.set_target_state(key, state)

    async def _file(self, repo: str, env: str, key: str, state: dict, config: dict,
                    now: datetime, *, regression_of: int | None) -> int | None:
        """Two-phase like Error Triage: claim the row (``filed_at`` set,
        ``issue_number`` None) before the gh call, write the number after. A
        failed create leaves the row retryable; the cap counts successes only."""
        cap_remaining = int(config.get("daily_cap", 10)) - len(self._filed)
        decision = decide(None, cap_remaining=cap_remaining,
                          comment_every=timedelta(hours=1), now=now)
        if decision.kind != "file":
            if not state.get("cap_noted"):
                state["cap_noted"] = True
                await self._ctx.set_target_state(key, state)
                await self._ctx.emit("info", f"daily issue cap reached; {key} not filed",
                                     kind="cap", target=key)
            return None
        if regression_of is not None:
            decision = Decision("regress", "closed")
        state.update(filed_at=_iso(now), issue_number=None, issue_closed=False, closed_sha=None,
                     last_commented_at=None, last_commented_sha=None, recovery_commented=False,
                     cap_noted=False)
        await self._ctx.set_target_state(key, state)
        title = issue_title(repo, env, state)
        applied = await apply(decision, repo=repo, title=title,
                              body=issue_body(repo, env, state, regression_of=regression_of),
                              labels=[LABEL], linked=None, run=self._run)
        if applied.issue_number is None:
            await self._ctx.emit("warn", f"issue create failed — will retry: {title}",
                                 kind="file_failed", target=key)
            return None
        state["issue_number"] = applied.issue_number
        state["last_commented_sha"] = state.get("sha")
        await self._ctx.set_target_state(key, state)
        self._filed.append(now)
        kind = "regressed" if regression_of is not None else "filed"
        await self._ctx.emit("info", f"filed #{applied.issue_number}: {title}", kind=kind, target=key,
                             detail={"url": ci_issues.issue_url(repo, applied.issue_number)})
        return applied.issue_number

    def _stash_counts(self, repos: int, envs: int, states: dict[str, dict]) -> None:
        failing = sum(1 for s in states.values() if s.get("kind") == "failing")
        stalled = sum(1 for s in states.values() if s.get("kind") == "stalled")
        self._ctx.deploy_monitor_counts = {"repos": repos, "envs": envs, "failing": failing,
                                           "stalled": stalled, "incidents": failing + stalled}

    # -- card actions ----------------------------------------------------------
    async def actionable_items(self, limit: int = 50) -> list[ActionableItem]:
        mode, _ = await self._read_config()
        states = await self._ctx.list_target_states()
        items: list[ActionableItem] = []
        for key, s in sorted(states.items(), key=lambda kv: str(kv[1].get("first_seen_at") or "")):
            repo, env = split_key(key)
            number = s.get("issue_number")
            open_issue = bool(number) and not s.get("issue_closed")
            url = ci_issues.issue_url(repo, int(number)) if open_issue else s.get("log_url")
            subtitle = (f"@ {_short(s.get('sha'))} · {int(s.get('streak') or 0)}× · "
                        f"since {_fmt(s.get('first_seen_at'))}")
            if s.get("muted"):
                actions = [ActionSpec(kind="unmute", label="Unmute")]
            else:
                actions = [ActionSpec(kind="mute", label="Mute", style="risky")]
                if not open_issue and mode != "off":
                    actions.insert(0, ActionSpec(kind="file_now", label="File issue"))
            items.append(ActionableItem(id=key, title=f"{repo} · {env} {s.get('kind', 'failing')}",
                                        subtitle=subtitle, url=url, actions=actions))
            if len(items) >= limit:
                break
        return items

    async def action_result(self, key: str, kind: str) -> tuple[str, int | None]:
        """Returns ``(status, issue_number_filed_by_this_call)``."""
        mode, config = await self._read_config()
        state = await self._ctx.get_target_state(key)
        if not state:
            return "not_found", None
        now = self._clock()
        if kind == "mute":
            state["muted"] = True
            await self._ctx.set_target_state(key, state)
            await self._ctx.emit("info", f"{key} muted by operator", kind="muted", target=key)
            return "muted", None
        if kind == "unmute":
            state["muted"] = False
            await self._ctx.set_target_state(key, state)
            await self._ctx.emit("info", f"{key} unmuted", kind="unmuted", target=key)
            return "unmuted", None
        if kind != "file_now":
            return "failed", None
        if mode == "off":
            return "off", None
        if state.get("issue_number") and not state.get("issue_closed"):
            return "failed", None
        repo, env = split_key(key)
        regression_of = int(state["issue_number"]) if state.get("issue_number") else None
        number = await self._file(repo, env, key, state, {**config, "daily_cap": 10 ** 6}, now,
                                  regression_of=regression_of)
        return ("filed", number) if number is not None else ("failed", None)


# ---------------------------------------------------------------------------
# The action provider
# ---------------------------------------------------------------------------

_ACTION_STATUS = {
    "filed": ("ok", "Issue filed"),
    "muted": ("ok", "Muted — no comments or alerts until it recovers"),
    "unmuted": ("ok", "Unmuted"),
    "not_found": ("warn", "That incident no longer exists"),
    "off": ("warn", "Deploy Monitor is off — enable it first"),
    "failed": ("error", "Action failed — see the feed"),
}


class DeployMonitorActionProvider:
    """Card actions for deploy-monitor. Reads the loop off the orchestrator lazily —
    the providers dict is built before the loops are (orchestrator.__init__ order)."""

    valid_kinds = frozenset({"file_now", "mute", "unmute"})

    def __init__(self, orch: Any) -> None:
        self._orch = orch

    @property
    def _loop(self) -> "DeployMonitorLoop":
        return self._orch._deploy_monitor_loop

    async def actionable(self) -> list[ActionableItem]:
        return await self._loop.actionable_items()

    async def trigger(self, item_id: str, kind: str) -> TriggerResult:
        status, number = await self._loop.action_result(item_id, kind)
        level, message = _ACTION_STATUS.get(status, ("error", "Action failed — see the feed"))
        if status == "filed" and number is not None:
            message = f"Issue filed: #{number}"
        return TriggerResult(status=status, level=level, message=message)
```

Note: `_cell` in Task 2 must treat the `int` streak correctly (it does: `str(value)`).

- [ ] **Step 4: Run the tests**

Run: `env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_deploy_monitor_loop.py server/tests/test_deploy_monitor_policy.py -q`
Expected: all PASS. If `test_closed_completed_regresses_only_on_a_new_sha` fails on the "regressed" assertion, check the `kind="regressed"` emit in `_file`.

- [ ] **Step 5: Lint and type-check the two new modules**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python -m ruff check server/automations/deploy_monitor.py server/github/deployments.py && /home/tony/GitHub/viberpit/.venv/bin/python -m mypy server/automations/deploy_monitor.py server/github/deployments.py --ignore-missing-imports`
Expected: clean. Fix any finding in place (do not add `# type: ignore`).

- [ ] **Step 6: Commit**

```bash
printf 'feat(automations): deploy monitor loop — incidents, one alert, issue lifecycle, card actions\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/dm-t3.txt
git add server/automations/deploy_monitor.py server/tests/test_deploy_monitor_loop.py
git commit -F /tmp/dm-t3.txt
```

---

### Task 4: Wiring, the `deploy_failed` notification class, and the web label

**Files:**
- Modify: `server/automations/registry.py` (bottom imports + registers)
- Modify: `server/sessions/orchestrator.py` — `_PRIMARY_ONLY_LOOP_ATTRS` (~line 563), action providers (~line 912), ctor after the Error Triage block (~line 1268), `start()` after the error-triage task (~line 1924), `stop()` reap tuple (~line 2262), tick trio after `_error_triage_read_config` (~line 8868), `automation_statuses` ctx map (~line 12027)
- Modify: `server/sessions/notify_rules.py:26-36` (`EVENT_CLASSES`)
- Modify: `server/assistant/watches.py` (`CONDITION_MAP`)
- Modify: `web/src/features/settings/sections/NotificationsSection.tsx:52-60`
- Test: `server/tests/test_deploy_monitor_wiring.py`; existing `server/tests/test_notify_rules.py`, `server/tests/test_assistant_watches_api.py`, `server/tests/test_primary_lease.py`, `server/tests/test_automations_api.py` (if present)

**Interfaces:**
- Consumes: `DeployMonitor`, `DeployMonitorLoop`, `DeployMonitorActionProvider` (Tasks 2–3), `server.github.deployments.fetch_deployments` (Task 1), `orchestrator._cicd_list_repos`, `read_automation_config`.
- Produces: orchestrator attributes `_deploy_monitor_ctx`, `_deploy_monitor_loop`, `_deploy_monitor_task`, methods `_deploy_monitor_tick_loop`, `_deploy_monitor_tick_once`, `_deploy_monitor_read_config`; event class `deploy_failed`; watch `("deploy", "fails")`.

- [ ] **Step 1: Write the failing wiring tests**

```python
# server/tests/test_deploy_monitor_wiring.py
from __future__ import annotations

import inspect


def test_deploy_monitor_is_registered():
    from server.automations.registry import registry
    a = registry.get("deploy-monitor")
    assert a.id == "deploy-monitor" and a.modes == ("off", "watch", "file")


def test_deploy_monitor_loop_is_primary_only_and_torn_down():
    from server.sessions import orchestrator as mod
    assert "_deploy_monitor_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    src = inspect.getsource(mod.SessionOrchestrator.stop)
    assert '"_deploy_monitor_task"' in src, "stop() must reap the deploy-monitor loop"
    start_src = inspect.getsource(mod.SessionOrchestrator.start)
    assert "deploy-monitor-loop" in start_src


def test_deploy_monitor_has_action_provider_and_status_ctx():
    from server.sessions.orchestrator import SessionOrchestrator
    init_src = inspect.getsource(SessionOrchestrator.__init__)
    assert '"deploy-monitor": DeployMonitorActionProvider(self)' in init_src
    assert "fetch_deployments" in init_src
    statuses_src = inspect.getsource(SessionOrchestrator.automation_statuses)
    assert '"deploy-monitor": getattr(self, "_deploy_monitor_ctx", None)' in statuses_src


def test_deploy_failed_is_a_notification_class_with_an_assistant_watch():
    from server.assistant.watches import CONDITION_MAP
    from server.sessions.notify_rules import EVENT_CLASSES
    assert "deploy_failed" in EVENT_CLASSES
    assert CONDITION_MAP["deploy"]["fails"] == ("deploy_failed", "scope_repo")


def test_read_config_resolves_enabled_row_without_mode_to_watch(monkeypatch):
    import asyncio
    from server.sessions.orchestrator import SessionOrchestrator
    seen = {}

    async def fake_read(automation, *, modes, default_mode=None, log_key=None):
        seen.update(modes=modes, default_mode=default_mode, log_key=log_key)
        return default_mode, {}

    from server.automations import config_store
    monkeypatch.setattr(config_store, "read_automation_config", fake_read)
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    mode, _ = asyncio.run(SessionOrchestrator._deploy_monitor_read_config(orch))
    assert mode == "watch" and seen["modes"] == ("watch", "file")


def test_web_notification_labels_include_deploy_failed():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "web/src/features/settings/sections/NotificationsSection.tsx").read_text()
    assert "{ value: 'deploy_failed', label: 'Deploy failed' }" in src
```

- [ ] **Step 2: Run to verify failure**

Run: `env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_deploy_monitor_wiring.py -q`
Expected: FAIL on the registry lookup (KeyError / None) and the rest.

- [ ] **Step 3: Register the automation**

In `server/automations/registry.py` add after `from .error_triage import ErrorTriage  # noqa: E402`:

```python
from .deploy_monitor import DeployMonitor  # noqa: E402
```

and after `registry.register(ErrorTriage())`:

```python
registry.register(DeployMonitor())
```

- [ ] **Step 4: Wire the orchestrator**

(a) `_PRIMARY_ONLY_LOOP_ATTRS`: add `"_deploy_monitor_task",` after `"_error_triage_task",`.

(b) Action providers block (~line 912): add the import and the map entry:

```python
        from server.automations.error_triage import ErrorTriageActionProvider
        from server.automations.deploy_monitor import DeployMonitorActionProvider
        ...
            "error-triage": ErrorTriageActionProvider(self),
            "deploy-monitor": DeployMonitorActionProvider(self),
```

(c) Constructor, immediately after `self._error_triage_task: asyncio.Task | None = None`:

```python
        # Deploy Monitor: GitHub deployment statuses → incidents / one alert / one
        # issue per failing environment (spec 2026-09-12). Repos come from the same
        # "managed repos" seam the CI/CD Doctor uses; the fetch is one GraphQL doc.
        from server.automations.deploy_monitor import DeployMonitorLoop
        from server.github import deployments as gh_deployments
        self._deploy_monitor_ctx = AutomationContext("deploy-monitor")
        self._deploy_monitor_loop = DeployMonitorLoop(
            self._deploy_monitor_ctx,
            read_config=self._deploy_monitor_read_config,
            list_repos=self._cicd_list_repos,
            fetch_deployments=lambda repos: asyncio.to_thread(
                gh_deployments.fetch_deployments, repos))
        self._deploy_monitor_task: asyncio.Task | None = None
```

(d) `start()`, after the `self._error_triage_task = asyncio.create_task(...)` statement:

```python
            # Deploy Monitor: 120 s — deployments are minutes apart and the read is
            # one GraphQL call per App token, so a slower cadence loses nothing.
            self._deploy_monitor_task = asyncio.create_task(
                self._deploy_monitor_tick_loop(interval=120.0), name="deploy-monitor-loop"
            )
```

(e) `stop()` reap tuple: add `"_deploy_monitor_task",` right after `"_error_triage_task",`.

(f) After `_error_triage_read_config` (before the next section comment):

```python
    # ------------------------------------------------------------------
    # Deploy Monitor control loop
    # ------------------------------------------------------------------
    async def _deploy_monitor_tick_loop(self, interval: float) -> None:
        while True:
            await self._deploy_monitor_tick_once()
            await asyncio.sleep(interval)

    async def _deploy_monitor_tick_once(self) -> None:
        if not await self._schedule_allows_tick("deploy-monitor"):
            return
        await self._deploy_monitor_loop.tick()
        try:
            mode, _ = await self._deploy_monitor_read_config()
            if mode != "off":
                await self._bump_automation_active("deploy-monitor")
        except Exception:
            log.debug("deploy_monitor.heartbeat_failed", exc_info=True)

    async def _deploy_monitor_read_config(self) -> tuple[str, dict]:
        """(effective mode, validated config); off + defaults on any bad row. An
        enabled row with no explicit mode resolves to ``watch`` — the least
        invasive active mode — never to ``file``."""
        from server.automations.deploy_monitor import DeployMonitor
        from server.automations import config_store
        active = tuple(m for m in DeployMonitor.modes if m != "off")
        return await config_store.read_automation_config(
            DeployMonitor, modes=active, default_mode=active[0],
            log_key="deploy_monitor")
```

(g) `automation_statuses` ctx map: add `"deploy-monitor": getattr(self, "_deploy_monitor_ctx", None),` after the error-triage entry.

- [ ] **Step 5: Add the notification class, the assistant watch, and the web label**

`server/sessions/notify_rules.py` — add `"deploy_failed",` to `EVENT_CLASSES` after `"account_limited",`.

`server/assistant/watches.py` — add to `CONDITION_MAP`:

```python
    "deploy": {
        # target is "owner/repo"; fires when one of that repo's deploy environments'
        # newest GitHub deployment fails or stalls (server/automations/deploy_monitor.py).
        "fails": ("deploy_failed", "scope_repo"),
    },
```

`web/src/features/settings/sections/NotificationsSection.tsx` — add after the `pr_merged` entry:

```ts
  { value: 'deploy_failed', label: 'Deploy failed' },
```

Also grep for other places that enumerate event classes or watch kinds and extend them the same way (they must all agree):

```bash
grep -rn "account_limited" server/ web/src docs/product --include=*.py --include=*.ts --include=*.tsx --include=*.md | grep -v tests/
```

If `docs/product/features/ai-assistant.md` or a notifications doc lists the classes/watch kinds, add `deploy_failed` / `deploy.fails` there too.

- [ ] **Step 6: Run the wiring and neighbouring suites**

Run: `env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_deploy_monitor_wiring.py server/tests/test_notify_rules.py server/tests/test_assistant_watches_api.py server/tests/test_primary_lease.py server/tests/test_error_triage_wiring.py -q -p no:randomly`
Expected: all PASS. Then `pytest server/tests -q -k "automation or registry or notif or watch" -x` for the wider neighbourhood.

- [ ] **Step 7: Web checks**

Run (symlink `web/node_modules` from `/home/tony/GitHub/viberpit/web/node_modules` first if missing): `(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tail -20)`
Expected: clean. Note the memory rule: `| tee | tail` masks the exit code — check the vitest summary lines for `failed`.

- [ ] **Step 8: Commit**

```bash
printf 'feat(automations): wire deploy monitor — primary-only loop, card actions, deploy_failed notification class\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/dm-t4.txt
git add server/automations/registry.py server/sessions/orchestrator.py server/sessions/notify_rules.py server/assistant/watches.py web/src/features/settings/sections/NotificationsSection.tsx server/tests/test_deploy_monitor_wiring.py
git add -u docs/product 2>/dev/null || true
git commit -F /tmp/dm-t4.txt
```

---

### Task 5: Feature doc, full gate, PR

**Files:**
- Create: `docs/product/features/deploy-monitor.md`
- Modify: `.agents/instructions/core.md` — NO (it is at the AGENTS.md render cap per memory; do not touch it)

- [ ] **Step 1: Write the feature doc**

```markdown
# Deploy Monitor

Automation id `deploy-monitor` (Delivery › Automations). Watches every managed repo's GitHub
**Deployment** records — the ones Railway and GitHub environments write per push — and turns a
failing or stalled environment into one incident.

| Mode | Behaviour |
|---|---|
| `off` | nothing |
| `watch` | incident in the feed, one `deploy_failed` notification, card actions; no GitHub writes |
| `file` | `watch` + one `deploy-monitor` issue per incident in the repo: comment when the failing sha moves (at most every *comment gap*), close with a comment on recovery |

**Signal.** An environment is *failing* when its newest deployment's status is `FAILURE`/`ERROR`
and older than the *grace* window; *stalled* when the newest deployment has no terminal status
after *stall after* minutes. Only `qa` / `prod` / `admin` environments (`classify_env`) are watched
unless *Watch every environment* is on. A repo whose deployments cannot be read leaves its
incidents untouched — the monitor never closes an incident on missing data.

**Humans closing the issue.** Closed as *not planned* mutes the incident until recovery. Closed
as *completed* acknowledges the current sha; if a later sha still fails, a new issue is filed
linking the old one.

**Notifications.** `deploy_failed` fans out like *PR broke* when no rules exist; scope a rule by
repo, or ask the assistant to "watch deploy cairnea/KinDash fails".

Spec: `docs/superpowers/specs/2026-09-12-deploy-monitor-design.md`.
```

- [ ] **Step 2: Run the four-step lint-and-test gate**

```bash
V=/home/tony/GitHub/viberpit/.venv/bin/python
$V -m ruff check src/ server/ 2>&1 | tail -5
$V -m mypy server/ --ignore-missing-imports 2>&1 | tail -5
env -i HOME=/tmp/dm-home PATH="$PATH" LANG=C.UTF-8 CCSWITCH_DEV=1 PYTHONPATH=$PWD/src $V -m pytest tests/ -q -x 2>&1 | tail -5
```

Then the full server suite in the background with the test-slot acquire in the SAME shell (core.md § "Test-run slot cap"), output tee'd to `/tmp/deploy-monitor-server-tests.log`, `-n auto --dist loadfile --timeout=300 -m "not slow"`. Re-run any failure alone; the memory file lists the known rocky-only flakes (UDS daemon spawn, reauth timing, two ws timing tests, codex overlay) — anything else is real.

- [ ] **Step 3: Brand ratchet**

Run: `$V scripts/brand_ratchet.py` — new files must not introduce an unclaimed `ccswitch` spelling (the `from ccswitch import …` imports are already claimed as the package; verify the script passes).

- [ ] **Step 4: Commit the doc, push, open the PR**

```bash
printf 'docs(product): deploy monitor feature page\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/dm-t5.txt
git add docs/product/features/deploy-monitor.md
git commit -F /tmp/dm-t5.txt
git push -u origin feat/deploy-monitor
```

PR body (write to `/tmp/dm-pr.md`; avoid the word "production"):

```markdown
## Deploy Monitor — GitHub deployment statuses as the source of truth for deploy failures

KinDash's Railway deploy of the admin console failed three pushes in a row on 2026-09-12 and nobody was told: Railway wrote `FAILURE` Deployment statuses on GitHub, but the product's workflow only emits warnings and ViberPit never looked at deployment statuses.

This adds the `deploy-monitor` automation:
- reads every managed repo's newest GitHub Deployments (one GraphQL doc per App token; shares `_DEPLOY_NODE_FIELDS` with `pr_status`)
- per environment (`classify_env` buckets by default): *failing* = newest status FAILURE/ERROR past a grace window; *stalled* = no terminal status after N minutes
- one durable incident per `repo#env` in `AutomationTargetState`; one `deploy_failed` notification (new event class, assistant watch `deploy … fails`)
- `file` mode keeps exactly one `deploy-monitor` issue open in the repo: comment when the failing sha moves, close with a comment on recovery; `not_planned` mutes, `completed` acknowledges the current sha and regresses on a new one
- card: status counts, actionable list with File issue / Mute / Unmute

Spec: `docs/superpowers/specs/2026-09-12-deploy-monitor-design.md`. Feature doc: `docs/product/features/deploy-monitor.md`.

Ships disabled. Rollout: enable as `watch`, confirm the feed shows `cairnea/KinDash · kindash / admin failing`, then switch to `file`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

```bash
gh pr create --title "feat(automations): deploy monitor — GitHub deployment statuses → incidents, alerts, issues" --body-file /tmp/dm-pr.md
gh pr merge <#> --squash --auto
```

Before `gh pr create`, run the in-flight-PR check from core.md for `server/sessions/orchestrator.py`, `server/sessions/notify_rules.py`, `server/github/pr_status.py` and rebase if another PR touches the same hunks.
