"""Assistant policy: enabled/model resolution (env → AppSettings) and the route allowlist
the assistant bearer token may reach (spec §4.3, §4.11). One module so the middleware, the
API and the tool client agree."""
from __future__ import annotations

import logging
import re

from sqlalchemy import select

from server.config import get_settings
from server.db.models import AppSettings

log = logging.getLogger(__name__)


async def _row(db) -> AppSettings | None:
    return (await db.execute(
        select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()


async def assistant_enabled(db) -> bool:
    row = await _row(db)
    if row is not None and row.assistant_enabled is not None:
        return bool(row.assistant_enabled)
    return bool(get_settings().assistant_enabled)


async def operator_tools_enabled(db) -> bool:
    """Whether the operator tier (deploy, host upgrade, account refresh/enable/reauth,
    product automation policy edits) is on.

    Same env → AppSettings precedence as `assistant_enabled`, but opt-in: the env default
    is false (spec v2 §7, decision B1). Callers use this at BOTH enforcement points — tool
    registration and the route allowlist — so an off setting means the model cannot even
    attempt an operator call, not merely that the call is denied.
    """
    row = await _row(db)
    if row is not None and row.assistant_operator_tools is not None:
        return bool(row.assistant_operator_tools)
    return bool(get_settings().assistant_operator_tools)


def assistant_engine():
    """The engine the drawer runs on, clamped to one this build can build an
    overlay for.

    Env only — deliberately NOT an ``AppSettings`` row like ``assistant_model``.
    The engine decides which security overlay applies
    (``server/assistant/options_codex.py``), and a browser-editable setting would
    make that an in-app toggle rather than a deployment decision. An unparseable
    value resolves to Claude with a warning: the failure mode of a typo must be
    "the drawer runs on the audited engine", never "the drawer refuses to open".
    """
    from viberpit.engines import AgentEngine, InvalidAgentEngine, parse_agent_engine

    raw = get_settings().assistant_engine
    try:
        return parse_agent_engine(raw, legacy_default=True)
    except InvalidAgentEngine:
        log.warning("VIBERPIT_ASSISTANT_ENGINE=%r is not a known engine; "
                    "the assistant will run on %s", raw, AgentEngine.CLAUDE_CODE.value)
        return AgentEngine.CLAUDE_CODE


async def assistant_model(db) -> str:
    row = await _row(db)
    if row is not None and row.assistant_model is not None:
        return row.assistant_model
    return get_settings().assistant_model


# (method, path regex). Anything not listed is 403 for the assistant token, BEFORE routing.
# Keep in the same order as spec §4.2; a new tool must add its row here or its calls fail.
#
# Session ids are UUID4 strings (see `str(uuid.uuid4())` in
# server/sessions/orchestrator.py), so the id segment is matched structurally rather than
# with a loose `[A-Za-z0-9-]+` denylist-style pattern. A loose pattern would treat the
# static segments of the real router's batch/collection routes (`batch`, `search`,
# `archived`, `adopt`, `adoptable` — see server/api/sessions.py:816-819, which registers
# those routes before `/{session_id}` for exactly this reason) as a session id and
# wrongly admit routes spec §4.2 excludes, e.g. `POST /api/sessions/batch/unarchive`
# matching `_S + r"/unarchive"`, or `GET /api/sessions/adoptable` matching the bare
# `GET _S` row. A UUID4-anchored pattern can never match those static segments.
_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_S = r"/api/sessions/" + _ID
ASSISTANT_ROUTE_ALLOW: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (m, re.compile(p + r"$")) for m, p in [
        ("GET", r"/api/sessions"),
        ("GET", r"/api/sessions/search"),
        ("GET", r"/api/sessions/archived"),
        ("GET", _S),
        ("GET", _S + r"/transcript"),
        ("GET", _S + r"/export\.md"),
        ("GET", r"/api/accounts"),
        ("GET", r"/api/fleet/hosts"),
        ("GET", r"/api/prs"),
        # PR detail (spec v2 §4.6, Pack G, Task 7.5).
        ("GET", r"/api/prs/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/\d+"),
        # get_pr_files: the PR's changed files + patches, read live from GitHub.
        ("GET", r"/api/prs/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/\d+/files"),
        # GitHub writes (spec v2 §4.6, Pack G, Task 7.6). The assistant's only free-form
        # PUBLIC side effects (the Session Feedback inbox rows below also reach GitHub,
        # but as that automation's own filing) — so both are MUTATION-class (a
        # permission card) and both carry an authorship trailer on the way out.
        ("POST", r"/api/prs/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/\d+/comments"),
        ("POST", r"/api/github/issues"),
        # Triage writes on existing issues (Refs #2979): a comment (MUTATION), the
        # board move (MUTATION, journaled) and the close (DESTRUCTIVE). The per-item
        # issue routes the UI uses (`POST /api/pm/work-items/{id}/issue`) stay closed.
        ("POST", r"/api/github/issues/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/\d+/comments"),
        ("POST", r"/api/pm/board-status"),
        ("POST", r"/api/pm/issues/close"),
        ("GET", r"/api/pipeline/insights/checks"),
        ("GET", r"/api/pipeline/repos"),
        ("GET", r"/api/github/repos"),
        ("GET", r"/api/github/branches"),
        ("GET", r"/api/automations"),
        ("GET", r"/api/automations/[A-Za-z0-9_-]+/events"),
        # Automations control (spec v2 §4.3, Pack A). Every row is anchored, so the
        # `{id}` rows admit only the literal sub-route each names — the read rows below
        # cannot be stretched to reach the three fan-out POSTs, which is why those get
        # their own rows rather than sharing a looser pattern.
        ("GET", r"/api/automations/[A-Za-z0-9_-]+/actionable"),
        ("GET", r"/api/automations/[A-Za-z0-9_-]+/sessions"),
        ("GET", r"/api/automations/backlog-runner/candidates"),
        ("GET", r"/api/automations/backlog-runner/groups"),
        ("GET", r"/api/automations/cicd-doctor/findings"),
        ("GET", r"/api/automations/pr-doctor/consolidation"),
        ("GET", r"/api/automations/pr-doctor/jobs/[A-Za-z0-9-]+"),
        ("POST", r"/api/automations/schedule/preview"),
        # Session-start conveniences (spec v2 §4.5, Pack C).
        ("GET", r"/api/presets"),
        ("GET", r"/api/templates"),
        ("POST", r"/api/templates/[A-Za-z0-9-]+/render"),
        ("GET", r"/api/permission-profiles"),
        # Fan-out half (Task 6.4), each DESTRUCTIVE: they start unattended sessions.
        ("POST", r"/api/automations/[A-Za-z0-9_-]+/trigger"),
        ("POST", r"/api/automations/backlog-runner/dispatch"),
        ("POST", r"/api/automations/pr-doctor/consolidate"),
        # Session Feedback's clustered inbox (#2936). Pinned to that automation, the
        # only one with an inbox, and to the four ops the tools wrap — the generic
        # routes' other ops (e.g. a later `restore`) get no row until a tool needs one.
        ("GET", r"/api/automations/session-feedback/clusters"),
        ("POST", r"/api/automations/session-feedback/clusters/(draft|file|dismiss|attach)"),
        ("GET", r"/api/deploy/status"),
        ("GET", r"/api/auth/me"),
        ("GET", r"/api/sessions/adoptable"),
        ("GET", r"/api/sdlc/initiatives"),
        ("GET", r"/api/sdlc/initiatives/[a-z0-9-]+"),
        ("POST", r"/api/sdlc/initiatives/[a-z0-9-]+/advance"),
        # Close (gap log 2026-10-11): the endpoint itself refuses unless the epic and all
        # its sub-issues are closed. Same unbounded slug class as the rows around it —
        # adopted-epic slugs run past 100 characters (tools.py `_INITIATIVE_SLUG`).
        ("POST", r"/api/sdlc/initiatives/[a-z0-9-]+/close"),
        # READ only: accepting a materialization creates issues on GitHub, so the
        # accept/reject taps stay a human's (C4-R11) and have no row here.
        ("GET", r"/api/sdlc/initiatives/[a-z0-9-]+/materialization"),
        # PM product layer. The bare-slug row is anchored (every row gets a trailing `$`),
        # so it admits only `GET /api/pm/projects/{slug}` itself — the sibling sub-routes
        # the assistant must NOT reach (`POST …/edges`, `…/propose-map`, `…/advance-map`,
        # `…/classify`, `DELETE …/edges/{id}`) each need their own row and have none.
        ("GET", r"/api/pm/projects"),
        ("GET", r"/api/pm/projects/[a-z0-9-]+"),
        ("GET", r"/api/pm/projects/[a-z0-9-]+/deps"),
        # Stale dependency edges (gap log 2026-10-11): preview (read) and prune (a
        # card; the endpoint re-checks every edge is still stale before touching it).
        ("GET", r"/api/pm/projects/[a-z0-9-]+/deps/stale"),
        ("POST", r"/api/pm/projects/[a-z0-9-]+/deps/prune"),
        ("GET", r"/api/pm/projects/[a-z0-9-]+/onboarding"),
        ("GET", r"/api/pm/queue"),
        # Spec 2026-09-30 §4.2: the escalation read model, read-only.
        ("GET", r"/api/pm/holds"),
        # release_hold (MUTATION): the bulk release; the owner rule lives in the route
        # (`holds.release_refusal`), so this row grants no more than dispatch_backlog's.
        ("POST", r"/api/pm/holds/release"),
        ("GET", r"/api/pm/triage"),
        ("GET", r"/api/pm/map"),
        ("GET", r"/api/pm/leaves"),
        ("POST", r"/api/pm/projects/[a-z0-9-]+/sync"),
        ("POST", r"/api/pm/triage/[A-Za-z0-9-]+/(accept|reject|dismiss)"),
        # Decision proposals (spec §6, §8): accept/reject only — the propose route
        # (`POST /api/pm/work-items/{id}/decisions`) has no row here on purpose, the
        # assistant does not propose, only a coding session or a person does.
        ("GET", r"/api/pm/decisions"),
        ("POST", r"/api/pm/decisions/[A-Za-z0-9-]+/(accept|reject)"),
        ("POST", r"/api/sessions"),
        # Batch tools (spec v2 B4) — each acts on up to 20 session ids in one call.
        ("POST", r"/api/sessions/batch/(archive|unarchive|resume|rotate|permanent-delete)"),
        ("POST", _S + r"/prompt"),
        ("POST", _S + r"/interrupt"),
        ("POST", _S + r"/questions/[A-Za-z0-9-]+"),
        ("POST", _S + r"/permissions/[A-Za-z0-9-]+"),
        ("POST", _S + r"/model"),
        ("POST", _S + r"/permission-mode"),
        ("POST", _S + r"/model-approval"),
        ("DELETE", _S),
        ("POST", _S + r"/unarchive"),
        ("POST", _S + r"/autopilot"),
        ("POST", _S + r"/fix-pr"),
        ("PATCH", r"/api/automations/[A-Za-z0-9_-]+"),
        ("DELETE", _S + r"/permanent"),
        ("DELETE", _S + r"/queue"),
        ("POST", r"/api/pipeline/pr/ready-merge"),
        ("PATCH", _S),
        ("POST", _S + r"/queue-move"),
        ("POST", _S + r"/account-pin"),
        ("POST", _S + r"/rotate"),
        ("POST", _S + r"/reload"),
        ("POST", _S + r"/suggest"),
        ("POST", r"/api/sessions/adopt"),
        ("GET", r"/api/notifications/channels"),
        ("GET", r"/api/notifications/rules"),
        ("POST", r"/api/notifications/rules"),
        ("DELETE", r"/api/notifications/rules/[A-Za-z0-9-]+"),
        ("GET", r"/api/notifications/quiet-hours"),
        ("PUT", r"/api/notifications/quiet-hours"),
        ("POST", r"/api/notifications/test"),
        # Watches (spec v2 §4.7, Task 8.3): the one `/api/assistant/*` exception —
        # every other route under that prefix is blocked for the assistant token
        # (`/session`, `/tokens`, `/status`), but a watch belongs to the thread that
        # created it, so the tool needs to reach its own CRUD.
        ("GET", r"/api/assistant/watches"),
        ("POST", r"/api/assistant/watches"),
        ("DELETE", r"/api/assistant/watches/[A-Za-z0-9-]+"),
    ]
)


# Operator-tier routes (spec v2 §7, Task 9.2): deploy, host upgrade, account
# refresh/enable/reauth, and a product's automation policy. Disjoint from ASSISTANT_ROUTE_ALLOW on purpose — these are
# reachable ONLY when `operator=True` is passed to `route_allowed`, which in turn is
# gated on `operator_tools_enabled(db)`. A path here must never also appear above.
OPERATOR_ROUTE_ALLOW: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (m, re.compile(p + r"$")) for m, p in [
        ("POST", r"/api/deploy/start"),
        ("POST", r"/api/fleet/hosts/[A-Za-z0-9_.-]+/upgrade"),
        ("POST", r"/api/accounts/[A-Za-z0-9_.-]+/refresh"),
        ("PATCH", r"/api/accounts/[A-Za-z0-9_.-]+/enabled"),
        ("POST", r"/api/accounts/[A-Za-z0-9_.-]+/reauth"),
        # update_product_policy. Operator tier only: the base allowlist admits no
        # `PATCH /api/pm/...` route at all (test_route_allowlist pins that).
        ("PATCH", r"/api/pm/projects/[a-z0-9-]+/policy"),
    ]
)


def route_allowed(method: str, path: str, *, operator: bool = False) -> bool:
    path = path.split("?", 1)[0]
    method = method.upper()
    if any(m == method and rx.match(path) for m, rx in ASSISTANT_ROUTE_ALLOW):
        return True
    if operator:
        return any(m == method and rx.match(path) for m, rx in OPERATOR_ROUTE_ALLOW)
    return False


# READ-class tools whose backing route is a POST that mutates nothing (a dry-run or a
# render). The conformance test (server/tests/test_assistant_conformance.py) lets a
# READ tool use POST only when it is listed here. Later tasks add names to this set.
READ_POSTS: frozenset[str] = frozenset({"test_notification", "preview_schedule",
                                        "suggest_next_prompt", "render_template",
                                        "draft_feedback_cluster_issue"})
