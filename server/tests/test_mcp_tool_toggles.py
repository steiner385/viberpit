"""D-16b server side: MCP per-tool enable/disable.

- ``mcp_status`` lists each server's tools; the sanitizer keeps them, capped (200 per
  server, 300-char descriptions). A ``toolCount``-only reply has no list: no switches.
- Two levels: the Settings default per tool (``mcp_server_scope.tool_defaults``) and the
  session override per tool (``sessions.mcp_tool_overrides``). Effective deny =
  (default-disabled minus session-enabled) plus session-disabled; a server off at the
  server level contributes none.
- ONE composer (``flag_settings.compose_permissions``) owns the flag-layer
  ``permissions`` object, and ONE driver method sends it (Review Focus 5).
- The driver sends the composed denies after ``initialize``, before the first prompt, on
  every CLI start — a daemon restart included (Review Focus 2) — and never in the spawn
  ``--settings``. A live toggle resends the whole object; ``deny: []`` restores every
  tool (Review Focus 1). A server switched off drops its tool denies; switched back on,
  they return (Review Focus 3).

Only the CLI is faked; ``test_mcp_tool_deny_cli_contract`` checks the wire against the
installed binary.
"""
from __future__ import annotations

import ast
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from viberpit.selector import Picked
from server.db import db_session, dispose_db, init_db
from server.db.models import McpServerScope
from server.db.models import Session as DbSession
from server.ipc.client import DaemonError
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.mcp_health import store as health_store
from server.mcp_health.status import (TOOL_DESCRIPTION_MAX, TOOLS_MAX, McpServerState, McpTool,
                                      mcp_tool_rule, sanitize_statuses, server_for_tool)
from server.mcp_scope import store as scope_store
from server.mcp_scope.resolve import McpToolRules, effective_disabled, effective_disabled_tools
from server.sessions import flag_settings as fs
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.engine import AgentCapabilities
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _PER_SESSION
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.permissions import PermissionRegistry
from server.sessions.remote import RemoteOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.test_fast_mode import (MODELS, FakeControl, _control_error, _fake_usage,
                                         _no_title, _no_warnings, _seed_account,
                                         _timeout_error)

SERVER_ROOT = Path(__file__).resolve().parents[1]


def _tool(name: str, **ann) -> dict:
    return {"name": name, "description": f"probe {name}", "annotations": ann}


# The probe server of the D-16 spike (§0): three tools.
PROBE_TOOLS = [_tool("tool_a", readOnly=True), _tool("tool_b", destructive=True),
               _tool("tool_c")]
PROBE = {"name": "probe", "status": "connected", "scope": "user", "tools": PROBE_TOOLS}


# ---------------------------------------------------------------- tool lists


def test_sanitize_keeps_tools_with_annotations() -> None:
    [s] = sanitize_statuses({"mcpServers": [PROBE]})
    assert s.tool_count == 3
    assert s.tools == (McpTool("tool_a", "probe tool_a", read_only=True),
                       McpTool("tool_b", "probe tool_b", destructive=True),
                       McpTool("tool_c", "probe tool_c"))
    assert s.tools[0].to_wire() == {"name": "tool_a", "description": "probe tool_a",
                                    "annotations": {"readOnly": True, "destructive": None,
                                                    "openWorld": None}}
    # The live mcpStatus event stays lean: no tool list on it.
    assert "tools" not in s.to_wire() and s.to_wire()["toolCount"] == 3


def test_tool_lists_are_capped() -> None:
    many = [{"name": f"t{i}", "description": "x" * 1000} for i in range(TOOLS_MAX + 50)]
    [s] = sanitize_statuses({"mcpServers": [{"name": "big", "status": "connected",
                                             "tools": many}]})
    assert s.tool_count == TOOLS_MAX + 50            # the real count
    assert s.tools is not None and len(s.tools) == TOOLS_MAX
    assert all(len(t.description or "") == TOOL_DESCRIPTION_MAX for t in s.tools)


def test_a_tool_count_only_reply_has_no_list() -> None:
    [s] = sanitize_statuses({"mcpServers": [{"name": "big", "status": "connected",
                                             "toolCount": 900}]})
    assert s.tools is None and s.tool_count == 900


def test_rule_names_use_the_clis_normalisation() -> None:
    assert mcp_tool_rule("probe", "tool_b") == "mcp__probe__tool_b"
    assert mcp_tool_rule("plugin:p:s", "a.b") == "mcp__plugin_p_s__a_b"
    assert server_for_tool("mcp__plugin_p_s__x", ["plugin:p:s"]) == "plugin:p:s"
    # Fix round 1 item 4: the CLI's En() collapses `_` runs and trims for claude.ai
    # connectors only (2.1.318 @280040372; probed: `claude.ai My__Conn`).
    assert mcp_tool_rule("claude.ai My__Conn", "tool_a") == "mcp__claude_ai_My_Conn__tool_a"
    assert mcp_tool_rule("claude.ai  Notion.", "x") == "mcp__claude_ai_Notion__x"
    assert mcp_tool_rule("a.b  c", "x__y") == "mcp__a_b__c__x__y"
    assert server_for_tool("mcp__claude_ai_My_Conn__t", ["claude.ai My__Conn"]) == \
        "claude.ai My__Conn"


# ---------------------------------------------------------------- resolution / composer


def test_effective_deny_formula() -> None:
    rules = McpToolRules(defaults={"probe": {"tool_a": False, "tool_b": False, "tool_c": True}},
                         overrides={"probe": {"tool_b": True, "tool_c": False},
                                    "gh": {"x": False}})
    # (default-disabled ∖ session-enabled) ∪ session-disabled
    assert effective_disabled_tools(rules) == [("gh", "x"), ("probe", "tool_a"),
                                               ("probe", "tool_c")]
    assert rules.state("probe", "tool_b") == (True, "session")
    assert rules.state("probe", "tool_a") == (False, "default")
    assert rules.state("probe", "zz") == (True, "default")


def test_the_composition_does_not_depend_on_server_level_state() -> None:
    """Fix round 1 item 3: a server off at the server level keeps its tool denies (a deny
    for an absent server is a no-op), so they hold the moment it reconnects."""
    rules = McpToolRules(defaults={"probe": {"tool_a": False}}, overrides={"gh": {"x": False}})
    assert fs.compose_permissions(rules) == {"deny": ["mcp__gh__x", "mcp__probe__tool_a"]}


def test_compose_permissions_is_the_whole_object() -> None:
    assert fs.compose_permissions(McpToolRules()) == {"deny": []}
    rules = McpToolRules(defaults={"probe": {"tool_b": False}})
    assert fs.compose_permissions(rules) == {"deny": ["mcp__probe__tool_b"]}
    assert "additionalDirectories" in (fs.compose_permissions.__doc__ or "")


def test_the_new_codes_are_settings_codes() -> None:
    assert {fs.MCP_TOOL_UNKNOWN, fs.MCP_TOOLS_UNAVAILABLE} <= fs.SETTINGS_CODES


# Review Focus 5 ------------------------------------------------------------


def _calls_of(tree: ast.AST, name: str):
    """Each ``<x>.name(...)`` call in *tree* with the function that encloses it."""
    out = []

    def walk(node: ast.AST, fn: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            here = child.name if isinstance(child, (ast.FunctionDef,
                                                    ast.AsyncFunctionDef)) else fn
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr == name):
                out.append((child, here))
            walk(child, here)
    walk(tree, None)
    return out


def _names_permissions(key: ast.AST | None) -> bool:
    if isinstance(key, ast.Constant):
        return key.value == fs.PERMISSIONS_KEY
    if isinstance(key, ast.Name):
        return key.id == "PERMISSIONS_KEY"
    if isinstance(key, ast.Attribute):
        return key.attr == "PERMISSIONS_KEY"
    return True     # anything computed is unknown: refuse it


def test_only_the_composer_path_sends_a_flag_permissions_key() -> None:
    """No ``apply_flag_settings`` caller but the driver's ``_send_permissions`` may put
    ``permissions`` in a patch, and every other caller passes a dict literal whose keys
    are visible here (a computed patch could smuggle one in)."""
    senders, opaque, seen = [], [], 0
    for path in SERVER_ROOT.rglob("*.py"):
        if "tests" in path.relative_to(SERVER_ROOT).parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for call, fn in _calls_of(tree, "apply_flag_settings"):
            seen += 1
            where = f"{path.relative_to(SERVER_ROOT)}:{call.lineno} ({fn})"
            arg = call.args[0] if call.args else None
            if fn == "_send_permissions":
                senders.append(where)
                continue
            if not isinstance(arg, ast.Dict):
                opaque.append(where)
            elif any(_names_permissions(k) for k in arg.keys):
                senders.append(where)
    assert seen >= 5, "the scan found too few calls to be meaningful"
    assert opaque == []
    assert [s for s in senders if "(_send_permissions)" not in s] == []
    assert len(senders) == 1 and senders[0].startswith("sessions/claude_driver.py")


def test_the_guard_can_fail() -> None:
    bad = ast.parse("async def f(d):\n    await d.apply_flag_settings({'permissions': {}})\n")
    [(call, fn)] = _calls_of(bad, "apply_flag_settings")
    assert fn == "f" and _names_permissions(call.args[0].keys[0])


# ---------------------------------------------------------------- stores


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'tools.db'}"
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": url}):
        await init_db(db_url=url)
        try:
            yield
        finally:
            await dispose_db()


@pytest.mark.asyncio
async def test_tool_defaults_ride_the_scope_row_without_changing_server_scope(db) -> None:
    assert await scope_store.set_tool_default("probe", "tool_b", False) == {"tool_b": False}
    assert await scope_store.set_tool_default("probe", "tool_c", True) == {
        "tool_b": False, "tool_c": True}
    assert await scope_store.tool_defaults() == {"probe": {"tool_b": False, "tool_c": True}}
    # The row a tool default created reads exactly as "no server rule".
    assert effective_disabled(["probe"], server_defaults=await scope_store.server_defaults(),
                              repo_rules={"probe": False}, session_overrides={}) == ["probe"]
    await scope_store.set_default("probe", False)          # the server-level write keeps them
    assert await scope_store.tool_defaults() == {"probe": {"tool_b": False, "tool_c": True}}
    await scope_store.delete_server("probe")
    assert await scope_store.tool_defaults() == {}


@pytest.mark.asyncio
async def test_session_tool_overrides_merge_and_a_gone_row_is_keyerror(db) -> None:
    async with db_session() as s:
        s.add(DbSession(id="a", name="n", account_name="acct", cwd="/tmp"))
    await scope_store.set_session_tool_override("a", "probe", "tool_b", True)
    await scope_store.set_session_tool_override("a", "probe", "tool_c", False)
    await scope_store.set_session_tool_override("a", "gh", "x", False)
    assert await scope_store.session_tool_overrides("a") == {
        "probe": {"tool_b": True, "tool_c": False}, "gh": {"x": False}}
    assert await scope_store.session_overrides("a") == {}     # server level untouched
    with pytest.raises(KeyError):
        await scope_store.set_session_tool_override("gone", "probe", "tool_b", True)
    assert await scope_store.session_tool_overrides("gone") == {}


@pytest.mark.asyncio
async def test_last_seen_tools_survive_observations_without_a_list(db) -> None:
    [connected] = sanitize_statuses({"mcpServers": [PROBE]})
    await health_store.upsert_states("acct", "h", [connected], source="probe", session_id=None)
    seen = await health_store.last_seen_tools()
    assert [t.name for t in seen["probe"][0]] == ["tool_a", "tool_b", "tool_c"]
    assert seen["probe"][0][0].read_only is True and seen["probe"][1].endswith("Z")
    # Disabled (0 tools) and toolCount-only observations keep the last list.
    for row in ({"name": "probe", "status": "disabled", "tools": []},
                {"name": "probe", "status": "connected", "toolCount": 3}):
        await health_store.upsert_states("acct", "h", sanitize_statuses({"mcpServers": [row]}),
                                         source="session", session_id="s")
        assert [t.name for t in (await health_store.last_seen_tools())["probe"][0]] == [
            "tool_a", "tool_b", "tool_c"]
    # A newer list on another account wins.
    newer = sanitize_statuses({"mcpServers": [{**PROBE, "tools": [_tool("tool_z")]}]})
    await health_store.upsert_states("other", "h", newer, source="probe", session_id=None)
    assert [t.name for t in (await health_store.last_seen_tools())["probe"][0]] == ["tool_z"]
    # The status rows' wire shape is unchanged (no tool lists in GET /library/mcp).
    assert all("tools" not in r for r in await health_store.rows())


# ---------------------------------------------------------------- the driver


def _driver(rules, *, control=None, desired=None, status=None, log=None):
    control = control or FakeControl()
    clients: list[FakeSDKClient] = []
    status = status if status is not None else [PROBE]

    class _Client(FakeSDKClient):
        async def query(self, prompt: str):
            if log is not None:
                log.append(("prompt", prompt))
            await super().query(prompt)

    def factory(*a, **k):
        c = _Client([], startup_frames=[], mcp_status=lambda: status)
        c._query = control  # type: ignore[attr-defined]
        clients.append(c)
        return c

    async def load(sid):
        return rules() if callable(rules) else rules

    d = ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="u",
                          desired_disabled_mcp=desired, load_mcp_tool_rules=load)
    return d, control, clients


B_OFF = McpToolRules(defaults={"probe": {"tool_b": False}})


@pytest.mark.asyncio
async def test_start_sends_the_composed_denies_before_the_first_prompt() -> None:
    log: list = []

    class _Logged(FakeControl):
        async def _send_control_request(self, request, timeout=60.0):
            log.append(("control", request))
            return await super()._send_control_request(request, timeout)

    d, control, _ = _driver(B_OFF, control=_Logged(), log=log)
    await d.start()
    try:
        assert control.patches == [{"permissions": {"deny": ["mcp__probe__tool_b"]}}]
        await d.send_prompt("hi")
        kinds = [k for k, _ in log]
        assert kinds.index("control") < kinds.index("prompt")
        assert d.mcp_tool_rules == B_OFF
    finally:
        await d.aclose()


@pytest.mark.asyncio
async def test_an_empty_deny_set_sends_nothing_at_start() -> None:
    for rules in (McpToolRules(), None):
        d, control, _ = _driver(rules)
        await d.start()
        try:
            assert control.patches == []
        finally:
            await d.aclose()


@pytest.mark.asyncio
async def test_a_reconnect_reapplies_from_the_loader() -> None:
    current = {"rules": B_OFF}
    d, control, _ = _driver(lambda: current["rules"])
    await d.start()
    try:
        current["rules"] = McpToolRules(defaults={"probe": {"tool_c": False}})
        await d.reconnect()
        assert control.patches == [{"permissions": {"deny": ["mcp__probe__tool_b"]}},
                                   {"permissions": {"deny": ["mcp__probe__tool_c"]}}]
    finally:
        await d.aclose()


# Review Focus 1 (driver half)
@pytest.mark.asyncio
async def test_a_live_reenable_sends_the_whole_object_and_deny_empty_restores() -> None:
    d, control, _ = _driver(B_OFF)
    await d.start()
    try:
        await d.set_mcp_tool_override("probe", "tool_b", True)
        assert control.patches[-1] == {"permissions": {"deny": []}}
        assert d.mcp_tool_rules.state("probe", "tool_b") == (True, "session")
    finally:
        await d.aclose()


# Fix round 1, item 2: a failed live toggle leaves the reported state = the applied one.
@pytest.mark.asyncio
async def test_a_refused_live_write_keeps_the_applied_rules() -> None:
    d, control, _ = _driver(B_OFF)
    await d.start()
    try:
        control.replies = [_control_error("nope")]
        with pytest.raises(fs.SettingsError) as ei:
            await d.set_mcp_tool_override("probe", "tool_b", True)
        assert ei.value.code == fs.SETTINGS_REFUSED and d.mcp_tool_rules == B_OFF
        # The next toggle composes from what was applied, not from the refused write.
        await d.set_mcp_tool_override("probe", "tool_a", False)
        assert control.patches[-1] == {
            "permissions": {"deny": ["mcp__probe__tool_a", "mcp__probe__tool_b"]}}
    finally:
        await d.aclose()


# Fix round 1, item 1: concurrent toggles serialize under one lock; none is lost.
@pytest.mark.asyncio
async def test_two_concurrent_overrides_both_end_up_denied() -> None:
    import asyncio
    d, control, _ = _driver(McpToolRules())
    await d.start()
    try:
        control.gate = asyncio.Event()
        a = asyncio.create_task(d.set_mcp_tool_override("probe", "tool_a", False))
        b = asyncio.create_task(d.set_mcp_tool_override("probe", "tool_b", False))
        await asyncio.sleep(0.05)
        control.gate.set()
        await asyncio.gather(a, b)
        assert control.max_in_flight == 1
        assert control.patches[-1] == {
            "permissions": {"deny": ["mcp__probe__tool_a", "mcp__probe__tool_b"]}}
        assert [d.mcp_tool_rules.state("probe", t)[0] for t in ("tool_a", "tool_b")] == [
            False, False]
    finally:
        await d.aclose()


@pytest.mark.asyncio
async def test_a_server_toggle_racing_a_tool_toggle_loses_neither() -> None:
    import asyncio
    d, control, clients = _driver(McpToolRules())
    await d.start()
    try:
        control.gate = asyncio.Event()
        tool = asyncio.create_task(d.set_mcp_tool_override("probe", "tool_b", False))
        server = asyncio.create_task(d.toggle_mcp_server("gh", False))
        await asyncio.sleep(0.05)
        control.gate.set()
        await asyncio.gather(tool, server)
        assert control.patches == [{"permissions": {"deny": ["mcp__probe__tool_b"]}}]
        assert clients[-1].toggled == [("gh", False)]
        assert d.mcp_tool_rules.state("probe", "tool_b") == (False, "session")
    finally:
        await d.aclose()


# Fix round 1, item 2: a failed start apply is retried once, then reported.
@pytest.mark.asyncio
async def test_a_failed_start_apply_retries_then_reports_not_enforced(caplog) -> None:
    from server.sessions import events as ev
    caplog.set_level(logging.INFO)
    control = FakeControl(_control_error("nope"), _control_error("nope again"))
    d, control, _ = _driver(B_OFF, control=control)
    emitted: list = []
    real_emit = d._emit

    def record(e):
        emitted.append(e)
        return real_emit(e)
    d._emit = record  # type: ignore[method-assign]
    await d.start()                                   # never fails the start
    try:
        assert len(control.patches) == 2              # one retry
        assert d.mcp_tools_enforced is False
        assert d.mcp_tool_rules == McpToolRules()      # applied: nothing — every tool on
        warnings = _no_warnings(caplog)
        assert any("mcp tool denies not applied" in m for _, m in warnings)
        [notice] = [e for e in emitted if isinstance(e, ev.ErrorEvent)]
        assert notice.code == fs.MCP_TOOLS_NOT_APPLIED and "Restart" in notice.message
        # A later live toggle composes from the desired rules and restores enforcement.
        await d.set_mcp_tool_override("probe", "tool_c", False)
        assert control.patches[-1] == {
            "permissions": {"deny": ["mcp__probe__tool_b", "mcp__probe__tool_c"]}}
        assert d.mcp_tools_enforced is True
    finally:
        await d.aclose()


@pytest.mark.asyncio
async def test_a_start_apply_that_succeeds_on_retry_is_enforced(caplog) -> None:
    caplog.set_level(logging.INFO)
    d, control, _ = _driver(B_OFF, control=FakeControl(_control_error("blip")))
    await d.start()
    try:
        assert d.mcp_tools_enforced is True and d.mcp_tool_rules == B_OFF
        assert _no_warnings(caplog) == []
    finally:
        await d.aclose()


# Review Focus 3, as amended by fix round 1 item 3: a server that is off keeps its tool
# denies in force, so re-enabling it opens no window and sends nothing.
@pytest.mark.asyncio
async def test_server_off_keeps_its_tool_denies_so_reenabling_has_no_gap() -> None:
    rules = McpToolRules(defaults={"probe": {"tool_b": False}},
                         overrides={"probe": {"tool_c": False}})
    d, control, _ = _driver(rules, desired=["probe"])
    await d.start()
    try:
        both = {"permissions": {"deny": ["mcp__probe__tool_b", "mcp__probe__tool_c"]}}
        assert control.patches == [both]       # already in force while the server is off
        await d.toggle_mcp_server("probe", True)
        await d.toggle_mcp_server("probe", False)
        await d.toggle_mcp_server("probe", True)
        assert control.patches == [both]       # no resend, no gap
        assert d.mcp_tool_rules == rules
    finally:
        await d.aclose()


# ---------------------------------------------------------------- the orchestrator


class Spawns:
    """A fake SDK factory declaring ``flag_settings``; every CLI lists the probe server."""

    def __init__(self, control: FakeControl, status=None) -> None:
        self.control = control
        self.status = status if status is not None else [PROBE]
        self.calls: list[dict] = []
        self.clients: list[FakeSDKClient] = []

    def __call__(self, cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                 on_pre_compact=None, options_overlay=None, flag_settings=None, **kw):
        self.calls.append({"resume": resume, "flag_settings": flag_settings, **kw})
        client = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[],
                               server_info={"models": MODELS},
                               mcp_status=lambda: self.status)
        client._query = self.control  # type: ignore[attr-defined]
        self.clients.append(client)
        return client


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    """A real orchestrator, Claude driver and SQLite DB on the fake CLI (no session yet)."""
    store = tmp_path / "store"; store.mkdir()
    shared = tmp_path / "dot-claude"; (shared / "projects").mkdir(parents=True)
    (shared / "settings.json").write_text("{}")
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    (cfg_dir / ".claude.json").write_text("{}")
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    clone = repo_root / "myrepo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    (clone / "README.md").write_text("hi\n")
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(clone), *args], check=True, env=git_env)
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'tools.db'}"
    patches: list = [
        patch.dict(os.environ, {"VIBERPIT_HOME": str(store), "CLAUDE_CONFIG_DIR": str(shared),
                                "VIBERPIT_DB_URL": db_url}),
        patch("server.sessions.orchestrator.compose", return_value=cfg_dir),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")),
        patch("server.sessions.orchestrator.generate_title", _no_title),
    ]
    for p in patches:
        p.start()
    _seed_account("alpha")
    await init_db(db_url=db_url)
    control = FakeControl()
    spawns = Spawns(control)
    orch = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                               usage_fn=_fake_usage, repo_root=str(repo_root))

    async def create():
        return await orch.create_session("Tools", cwd=str(clone), model="opus",
                                         permission_mode="full-auto")
    try:
        yield SimpleNamespace(orch=orch, spawns=spawns, control=control, create=create,
                              repo_root=repo_root)
    finally:
        await orch.stop()
        await dispose_db()
        for p in reversed(patches):
            p.stop()


def _tools_of(servers: list[dict], name: str = "probe") -> dict:
    return {t["name"]: (t["enabled"], t["source"])
            for t in next(s for s in servers if s["name"] == name)["tools"]}


async def _row(sid: str) -> DbSession:
    async with db_session() as db_:
        return (await db_.execute(select(DbSession).where(DbSession.id == sid))).scalar_one()


# Review Focus 1
@pytest.mark.asyncio
async def test_settings_disables_tool_b_and_the_session_reenables_it_live(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    await scope_store.set_tool_default("probe", "tool_b", False)
    rec = await env.create()
    assert env.control.patches == [{"permissions": {"deny": ["mcp__probe__tool_b"]}}]
    assert env.spawns.calls[-1]["flag_settings"] in (None, {})       # never in --settings
    servers = await env.orch.mcp_status_for(rec.id)
    assert _tools_of(servers) == {"tool_a": (True, "default"), "tool_b": (False, "default"),
                                  "tool_c": (True, "default")}
    probe = next(s for s in servers if s["name"] == "probe")
    assert probe["tools"][1]["annotations"]["destructive"] is True

    servers = await env.orch.set_mcp_tool(rec.id, "probe", "tool_b", True, identity="u")
    assert env.control.patches[-1] == {"permissions": {"deny": []}}
    assert _tools_of(servers)["tool_b"] == (True, "session")
    assert (await _row(rec.id)).mcp_tool_overrides == {"probe": {"tool_b": True}}

    await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    assert env.control.patches[-1] == {"permissions": {"deny": ["mcp__probe__tool_a"]}}
    assert _no_warnings(caplog) == []


# Review Focus 2
@pytest.mark.asyncio
async def test_a_daemon_restart_reapplies_the_overrides_before_the_first_prompt(env) -> None:
    await scope_store.set_tool_default("probe", "tool_b", False)
    rec = await env.create()
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_b", True, identity="u")
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_c", False, identity="u")
    log: list = []

    class _Logged(FakeControl):
        async def _send_control_request(self, request, timeout=60.0):
            log.append(("control", request))
            return await super()._send_control_request(request, timeout)

    control2 = _Logged()
    spawns = Spawns(control2)
    orch2 = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                                usage_fn=_fake_usage, repo_root=str(env.repo_root))
    try:
        await orch2.rehydrate()
        assert await orch2.ensure_driver(rec.id) is not None
        assert control2.patches == [{"permissions": {"deny": ["mcp__probe__tool_c"]}}]
        assert spawns.calls[-1]["flag_settings"] in (None, {})
        assert spawns.clients[-1].sent == []                 # no prompt before the deny
        assert _tools_of(await orch2.mcp_status_for(rec.id) or []) == {
            "tool_a": (True, "default"), "tool_b": (True, "session"),
            "tool_c": (False, "session")}
    finally:
        await orch2.stop()


@pytest.mark.asyncio
async def test_unknown_tool_and_unknown_list_are_refused_and_nothing_is_sent(env,
                                                                              caplog) -> None:
    caplog.set_level(logging.INFO)
    rec = await env.create()
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_mcp_tool(rec.id, "probe", "nope", False, identity="u")
    assert ei.value.code == fs.MCP_TOOL_UNKNOWN
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_mcp_tool(rec.id, "ghost", "tool_a", False, identity="u")
    assert ei.value.code == fs.MCP_TOOL_UNKNOWN
    env.spawns.status[:] = [{"name": "probe", "status": "connected", "toolCount": 3}]
    servers = await env.orch.mcp_status_for(rec.id)
    assert servers[0]["tools"] is None and servers[0]["toolCount"] == 3
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    assert ei.value.code == fs.MCP_TOOLS_UNAVAILABLE
    assert env.control.patches == []
    assert not (await _row(rec.id)).mcp_tool_overrides
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_an_engine_without_flag_settings_has_no_switches(env) -> None:
    rec = await env.create()
    with patch.object(SessionOrchestrator, "_capabilities_of",
                      lambda self, sid: AgentCapabilities(models=True)):
        assert (await env.orch.mcp_status_for(rec.id))[0]["tools"] is None
        with pytest.raises(fs.SettingsError) as ei:
            await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    assert ei.value.code == fs.MCP_TOOLS_UNAVAILABLE


@pytest.mark.asyncio
async def test_kill_switches_turn_the_switches_off(env, monkeypatch) -> None:
    for var in ("VIBERPIT_MCP_SCOPE_ENABLED", "VIBERPIT_FLAG_SETTINGS_ENABLED"):
        monkeypatch.setenv(var, "false")
        await scope_store.set_tool_default("probe", "tool_b", False)
        rec = await env.create()
        assert env.control.patches == []
        assert (await env.orch.mcp_status_for(rec.id))[0]["tools"] is None
        monkeypatch.delenv(var)


@pytest.mark.asyncio
async def test_a_driverless_session_is_unavailable_and_unknown_is_keyerror(env) -> None:
    rec = await env.create()
    await env.orch._drivers.pop(rec.id).aclose()
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    assert ei.value.code == fs.MCP_TOOLS_UNAVAILABLE
    with pytest.raises(KeyError):
        await env.orch.set_mcp_tool("nope", "probe", "tool_a", False, identity="u")


@pytest.mark.asyncio
async def test_a_cli_refusal_saves_nothing(env) -> None:
    rec = await env.create()
    env.control.replies = [_control_error("nope")]
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    assert ei.value.code == fs.SETTINGS_REFUSED
    assert not (await _row(rec.id)).mcp_tool_overrides
    assert _tools_of(await env.orch.mcp_status_for(rec.id))["tool_a"] == (True, "default")


# Fix round 1, item 5: the override is written first; a refused apply restores the
# override it replaced, and a failed write applies nothing.
@pytest.mark.asyncio
async def test_a_refused_apply_restores_the_previous_override(env) -> None:
    rec = await env.create()
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    env.control.replies = [_control_error("nope")]
    with pytest.raises(fs.SettingsError):
        await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", True, identity="u")
    assert (await _row(rec.id)).mcp_tool_overrides == {"probe": {"tool_a": False}}
    assert _tools_of(await env.orch.mcp_status_for(rec.id))["tool_a"] == (False, "session")


@pytest.mark.asyncio
async def test_a_failed_override_write_applies_nothing(env) -> None:
    rec = await env.create()

    async def broken(*a, **k):
        raise RuntimeError("db down")
    with patch.object(scope_store, "set_session_tool_override", broken):
        with pytest.raises(RuntimeError, match="db down"):
            await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    assert env.control.patches == []
    assert _tools_of(await env.orch.mcp_status_for(rec.id))["tool_a"] == (True, "default")


@pytest.mark.asyncio
async def test_a_failed_start_apply_is_served_as_not_enforced(env) -> None:
    await scope_store.set_tool_default("probe", "tool_b", False)
    env.control.replies = [_control_error("nope"), _control_error("nope")]
    rec = await env.create()
    servers = await env.orch.mcp_status_for(rec.id)
    assert servers[0]["toolsEnforced"] is False
    assert _tools_of(servers)["tool_b"] == (True, "default")   # what the CLI really runs
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    servers = await env.orch.mcp_status_for(rec.id)
    assert servers[0]["toolsEnforced"] is True
    assert _tools_of(servers)["tool_b"] == (False, "default")


# Fix round 2, item 5: write -> apply -> compensate is ONE critical section under the
# driver's per-tool lock.


def _track_accepted(control: FakeControl) -> list[dict]:
    """Record each ``permissions`` object the fake CLI ACCEPTED (a refused or timed-out
    request is recorded in ``sent`` too, but never took effect)."""
    accepted: list[dict] = []
    send = control._send_control_request

    async def tracked(request, timeout=60.0):
        reply = await send(request, timeout)
        perms = (request.get("settings") or {}).get("permissions")
        if perms is not None:
            accepted.append(perms)
        return reply
    control._send_control_request = tracked  # type: ignore[method-assign]
    return accepted


def _cli_deny(accepted: list[dict]) -> set[str]:
    """What the fake CLI runs under: the last ``permissions`` object it accepted."""
    return set(accepted[-1]["deny"]) if accepted else set()


async def _db_overrides(sid: str) -> dict:
    return (await _row(sid)).mcp_tool_overrides or {}


@pytest.mark.asyncio
async def test_the_override_is_written_inside_the_drivers_lock(env) -> None:
    rec = await env.create()
    driver = env.orch._drivers[rec.id]
    real = scope_store.set_session_tool_override
    held: list[bool] = []

    async def spy(*a, **k):
        held.append(driver._mcp_tool_lock.locked())
        return await real(*a, **k)
    with patch.object(scope_store, "set_session_tool_override", spy):
        await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
        env.control.replies = [_control_error("nope")]
        with pytest.raises(fs.SettingsError):
            await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", True, identity="u")
    assert held == [True, True, True]          # write, write, compensation: all locked


@pytest.mark.asyncio
async def test_the_ab_ordering_race_ends_with_cli_equal_db_equal_b(env) -> None:
    import asyncio
    rec = await env.create()
    accepted = _track_accepted(env.control)
    real = scope_store.set_session_tool_override
    calls = 0

    async def slow_first(*a, **k):          # A's write is slow: B must wait for all of A
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.05)
        return await real(*a, **k)
    with patch.object(scope_store, "set_session_tool_override", slow_first):
        a = asyncio.create_task(env.orch.set_mcp_tool(rec.id, "probe", "tool_a", True,
                                                      identity="u"))
        driver = env.orch._drivers[rec.id]
        while not driver._mcp_tool_lock.locked():     # A has written inside the lock
            await asyncio.sleep(0.005)
        b = asyncio.create_task(env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False,
                                                      identity="u"))
        await asyncio.gather(a, b)
    assert await _db_overrides(rec.id) == {"probe": {"tool_a": False}}
    assert _cli_deny(accepted) == {"mcp__probe__tool_a"}


@pytest.mark.asyncio
async def test_a_refused_a_does_not_clobber_b(env) -> None:
    import asyncio
    rec = await env.create()
    accepted = _track_accepted(env.control)
    # B committed before A: A's compensation restores B's value, not "no override".
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", True, identity="u")
    env.control.replies = [_control_error("nope")]
    with pytest.raises(fs.SettingsError):
        await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    assert await _db_overrides(rec.id) == {"probe": {"tool_a": True}}
    assert _cli_deny(accepted) == set()
    # Concurrent: a refused A, then B; B's committed override survives A's compensation.
    env.control.replies = [_control_error("nope")]
    env.control.gate = asyncio.Event()             # hold A's (refused) reply
    a = asyncio.create_task(env.orch.set_mcp_tool(rec.id, "probe", "tool_b", False,
                                                  identity="u"))
    while env.control.in_flight == 0:
        await asyncio.sleep(0.005)
    b = asyncio.create_task(env.orch.set_mcp_tool(rec.id, "probe", "tool_b", True,
                                                  identity="u"))
    await asyncio.sleep(0.02)
    env.control.gate.set()
    results = await asyncio.gather(a, b, return_exceptions=True)
    assert isinstance(results[0], fs.SettingsError) and not isinstance(results[1], Exception)
    assert await _db_overrides(rec.id) == {"probe": {"tool_a": True, "tool_b": True}}
    assert _cli_deny(accepted) == set()


@pytest.mark.asyncio
async def test_a_cancelled_apply_leaves_db_equal_cli_and_propagates(env) -> None:
    import asyncio
    rec = await env.create()
    accepted = _track_accepted(env.control)
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_c", False, identity="u")
    env.control.gate = asyncio.Event()
    task = asyncio.create_task(env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False,
                                                     identity="u"))
    while env.control.in_flight == 0:          # the apply is on the wire
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    env.control.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _db_overrides(rec.id) == {"probe": {"tool_c": False}}
    assert _cli_deny(accepted) == {"mcp__probe__tool_c"}      # resent to match the DB
    servers = await env.orch.mcp_status_for(rec.id)
    assert servers[0]["toolsEnforced"] is True
    assert _tools_of(servers)["tool_a"] == (True, "default")


# Fix round 3 (minor): a timeout restores the DB and resends the pre-toggle rules once.
@pytest.mark.asyncio
async def test_a_timed_out_apply_converges_when_the_resend_lands(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    rec = await env.create()
    accepted = _track_accepted(env.control)
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_c", False, identity="u")
    env.control.replies = [_timeout_error()]
    before = len(accepted)
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    assert ei.value.code == fs.SETTINGS_FAILED
    assert await _db_overrides(rec.id) == {"probe": {"tool_c": False}}
    # The timed-out object may have landed: the pre-toggle rules were resent once.
    assert len(accepted) == before + 1
    assert _cli_deny(accepted) == {"mcp__probe__tool_c"}
    assert (await env.orch.mcp_status_for(rec.id))[0]["toolsEnforced"] is True
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_timed_out_apply_whose_resend_fails_is_not_enforced(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    rec = await env.create()
    accepted = _track_accepted(env.control)
    env.control.replies = [_timeout_error(), _timeout_error()]
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False, identity="u")
    assert ei.value.code == fs.SETTINGS_FAILED
    assert await _db_overrides(rec.id) == {}
    assert (await env.orch.mcp_status_for(rec.id))[0]["toolsEnforced"] is False
    assert any("state unknown after an interrupted toggle" in r.getMessage()
               and r.levelno == logging.INFO for r in caplog.records)
    assert _no_warnings(caplog) == []
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_b", False, identity="u")
    assert (await env.orch.mcp_status_for(rec.id))[0]["toolsEnforced"] is True
    assert _cli_deny(accepted) == {"mcp__probe__tool_b"}


# Fix round 3: a cancel during the forward write is protected too.
@pytest.mark.asyncio
async def test_a_cancel_during_the_write_leaves_db_equal_cli_and_propagates(env) -> None:
    import asyncio
    rec = await env.create()
    accepted = _track_accepted(env.control)
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_c", False, identity="u")
    real = scope_store.set_session_tool_override
    writing = asyncio.Event()

    async def slow(*a, **k):
        # The statement commits, then the call is still in flight when the cancel lands:
        # its outcome is unknown to a caller that is simply cancelled.
        result = await real(*a, **k)
        writing.set()
        await asyncio.sleep(0.05)
        return result
    with patch.object(scope_store, "set_session_tool_override", slow):
        task = asyncio.create_task(env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False,
                                                         identity="u"))
        await writing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await _db_overrides(rec.id) == {"probe": {"tool_c": False}}
    assert _cli_deny(accepted) == {"mcp__probe__tool_c"}
    assert (await env.orch.mcp_status_for(rec.id))[0]["toolsEnforced"] is True
    assert not env.orch._drivers[rec.id]._mcp_tool_lock.locked()


@pytest.mark.asyncio
async def test_a_double_cancel_during_compensation_still_converges(env) -> None:
    import asyncio
    rec = await env.create()
    accepted = _track_accepted(env.control)
    await env.orch.set_mcp_tool(rec.id, "probe", "tool_c", False, identity="u")
    n = len(env.control.sent)
    env.control.gate = asyncio.Event()
    task = asyncio.create_task(env.orch.set_mcp_tool(rec.id, "probe", "tool_a", False,
                                                     identity="u"))
    while len(env.control.sent) < n + 1:          # the apply is on the wire
        await asyncio.sleep(0.005)
    task.cancel()
    while len(env.control.sent) < n + 2:          # the compensation's resend is on the wire
        await asyncio.sleep(0.005)
    task.cancel()                                  # a second cancel mid-compensation
    await asyncio.sleep(0.02)
    env.control.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _db_overrides(rec.id) == {"probe": {"tool_c": False}}
    assert _cli_deny(accepted) == {"mcp__probe__tool_c"}
    assert (await env.orch.mcp_status_for(rec.id))[0]["toolsEnforced"] is True


# ---------------------------------------------------------------- RPC / remote / fleet / facade


def test_every_layer_carries_set_mcp_tool() -> None:
    assert "set_mcp_tool" in _PER_SESSION
    assert hasattr(InProcessOrchestratorAdapter, "set_mcp_tool")
    assert hasattr(RemoteOrchestrator, "set_mcp_tool")


@pytest.mark.asyncio
async def test_the_rpc_round_trip_keeps_codes_and_keyerror(env, caplog) -> None:
    from fastapi import FastAPI
    rec = await env.create()
    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, env.orch)

    class _Loop:
        async def call(self, method, **kw):
            try:
                return await app.state.rpc[method](kw)
            except Exception as exc:      # the daemon's error envelope
                raise DaemonError(type(exc).__name__, str(exc),
                                  data=getattr(exc, "error_data", None)) from exc

    remote = RemoteOrchestrator(_Loop())
    caplog.set_level(logging.INFO)
    servers = await remote.set_mcp_tool(rec.id, "probe", "tool_b", False, identity="u")
    assert _tools_of(servers)["tool_b"] == (False, "session")
    with pytest.raises(fs.SettingsError) as ei:
        await remote.set_mcp_tool(rec.id, "probe", "nope", False, identity="u")
    assert ei.value.code == fs.MCP_TOOL_UNKNOWN
    with pytest.raises(KeyError):
        await remote.set_mcp_tool("nope", "probe", "tool_b", False, identity="u")
    assert _no_warnings(caplog) == []


# ---------------------------------------------------------------- routes

from server.tests.test_sessions_actions_api import _ENV, _H, _audit_events, _make_client  # noqa: E402

SID = "11111111-1111-4111-8111-111111111111"
_KEYS = "server.api.sessions.configured_server_keys"
WIRE = [{"name": "probe", "status": "connected", "error": None, "toolCount": 3,
         "scope": "user", "auth": None,
         "tools": [{"name": "tool_b", "description": None,
                    "annotations": {"readOnly": None, "destructive": True, "openWorld": None},
                    "enabled": False, "source": "session"}]}]


def _route_client(error: Exception | None = None):
    client, fake = _make_client()
    fake.tool_calls = []

    async def set_mcp_tool(sid, server, tool, enabled, *, identity):
        fake.tool_calls.append((sid, server, tool, enabled, identity))
        if error is not None:
            raise error
        return WIRE
    fake.set_mcp_tool = set_mcp_tool
    return client, fake


def _post_tool(client, server="probe", tool="tool_b", enabled=False, sid=SID):
    return client.post(f"/api/sessions/{sid}/mcp/{server}/tools/{tool}", headers=_H,
                       json={"enabled": enabled})


def test_the_session_route_applies_and_audits() -> None:
    client, fake = _route_client()
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["probe"]), client:
        r = _post_tool(client)
        events = client.portal.call(_audit_events)
    assert r.status_code == 200 and r.json() == {"servers": WIRE}
    assert fake.tool_calls == [(SID, "probe", "tool_b", False, "testuser@example.com")]
    assert [e[2] for e in events if e[0] == "session.mcp_tool"] == [
        {"sessionId": SID, "server": "probe", "tool": "tool_b", "enabled": False}]


def test_the_session_route_plugin_key_reaches_the_orchestrator_whole() -> None:
    client, fake = _route_client()
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["plugin:p:s"]), client:
        assert _post_tool(client, server="plugin%3Ap%3As", enabled=True).status_code == 200
    assert fake.tool_calls[0][1:4] == ("plugin:p:s", "tool_b", True)


@pytest.mark.parametrize("enabled", [True, False])
def test_the_session_route_is_human_only_both_ways(monkeypatch, enabled) -> None:
    from server.auth import identity
    monkeypatch.setattr(identity, "attested_front_end_hop", lambda _conn: False)
    client, fake = _route_client()
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["probe"]), client:
        r = _post_tool(client, enabled=enabled)
        events = client.portal.call(_audit_events)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
    assert fake.tool_calls == [] and not [e for e in events if e[0] == "session.mcp_tool"]


@pytest.mark.parametrize("error,status,code", [
    (fs.SettingsError(fs.MCP_TOOL_UNKNOWN), 409, fs.MCP_TOOL_UNKNOWN),
    (fs.SettingsError(fs.MCP_TOOLS_UNAVAILABLE), 409, fs.MCP_TOOLS_UNAVAILABLE),
    (fs.SettingsError(fs.SETTINGS_REFUSED), 409, fs.SETTINGS_REFUSED),
    (DaemonError("UnknownMethod", "set_mcp_tool"), 409, "daemon_outdated"),
    (KeyError(SID), 404, None),
])
def test_the_session_route_maps_refusals(error, status, code) -> None:
    client, _ = _route_client(error)
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["probe"]), client:
        r = _post_tool(client)
        events = client.portal.call(_audit_events)
    assert r.status_code == status
    if code is not None:
        assert r.json()["detail"]["code"] == code
    assert not [e for e in events if e[0] == "session.mcp_tool"]


def test_the_session_route_unknown_server_and_kill_switch() -> None:
    client, fake = _route_client()
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["other"]), client:
        r = _post_tool(client)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "UNKNOWN_SERVER"
    client, fake = _route_client()
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_MCP_SCOPE_ENABLED": "false"}), \
            patch(_KEYS, return_value=["probe"]), client:
        r = _post_tool(client)
    assert r.status_code == 404 and r.json() == {"error": "mcp_scope_disabled"}
    assert fake.tool_calls == []


_LIB_KEYS = "server.api.library.configured_server_keys"
LIB = "/api/library/mcp"


async def _seen_probe() -> None:
    await health_store.upsert_states("acct", "h", sanitize_statuses({"mcpServers": [PROBE]}),
                                     source="probe", session_id=None)


def test_settings_default_route_round_trips_and_get_lists_tools() -> None:
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), patch(_LIB_KEYS, return_value=["probe", "gh"]), client:
        body = client.get(f"{LIB}/scope", headers=_H).json()
        assert body["servers"]["probe"] == {"defaultEnabled": True, "tools": None,
                                            "toolsSeenAt": None}
        r = client.put(f"{LIB}/probe/tools/tool_b", json={"enabled": False}, headers=_H)
        assert r.status_code == 409 and r.json()["detail"]["code"] == fs.MCP_TOOLS_UNAVAILABLE
        client.portal.call(_seen_probe)
        r = client.put(f"{LIB}/probe/tools/nope", json={"enabled": False}, headers=_H)
        assert r.status_code == 409 and r.json()["detail"]["code"] == fs.MCP_TOOL_UNKNOWN
        r = client.put(f"{LIB}/probe/tools/tool_b", json={"enabled": False}, headers=_H)
        assert r.status_code == 204
        body = client.get(f"{LIB}/scope", headers=_H).json()
        events = client.portal.call(_audit_events)
    probe = body["servers"]["probe"]
    assert probe["defaultEnabled"] is True and probe["toolsSeenAt"].endswith("Z")
    assert [(t["name"], t["enabled"], t["source"]) for t in probe["tools"]] == [
        ("tool_a", True, "default"), ("tool_b", False, "default"), ("tool_c", True, "default")]
    assert body["servers"]["gh"]["tools"] is None
    assert [e[2] for e in events if e[0] == "library_mcp_tool_saved"] == [
        {"server": "probe", "tool": "tool_b", "enabled": False}]


def test_settings_default_route_is_human_only_and_validates_the_server(monkeypatch) -> None:
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), patch(_LIB_KEYS, return_value=["probe"]), client:
        client.portal.call(_seen_probe)
        assert client.put(f"{LIB}/gh/tools/tool_b", json={"enabled": False},
                          headers=_H).status_code == 400
        from server.auth import identity
        monkeypatch.setattr(identity, "attested_front_end_hop", lambda _conn: False)
        r = client.put(f"{LIB}/probe/tools/tool_b", json={"enabled": True}, headers=_H)
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
        assert client.portal.call(scope_store.tool_defaults) == {}


def test_settings_default_route_kill_switch() -> None:
    client, _ = _make_client()
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_MCP_SCOPE_ENABLED": "false"}), \
            patch(_LIB_KEYS, return_value=["probe"]), client:
        r = client.put(f"{LIB}/probe/tools/tool_b", json={"enabled": False}, headers=_H)
    assert r.status_code == 404 and r.json() == {"error": "mcp_scope_disabled"}


@pytest.mark.asyncio
async def test_the_scope_row_shape_is_unchanged_for_server_level_readers(db) -> None:
    await scope_store.set_tool_default("probe", "tool_b", False)
    async with db_session() as s:
        row = await s.get(McpServerScope, "probe")
    assert row is not None and row.default_enabled is True
    assert await scope_store.server_defaults() == {"probe": True}
    _ = McpServerState  # the sanitized type the rows are built from
