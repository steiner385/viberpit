from unittest import mock
import pytest
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.engine import AgentEngine, InvalidAgentEngine


def _orch():
    orch = mock.MagicMock()
    orch.create_session = mock.AsyncMock(return_value="created")
    orch.get_session = mock.MagicMock(return_value="rec")          # sync on the real orch
    orch.resolve_permission = mock.MagicMock(return_value=True)    # sync
    orch.poller = mock.MagicMock()
    orch.poller.get = mock.AsyncMock(return_value={"usage": 1})
    orch._registry = mock.MagicMock()
    orch._registry.pending = mock.MagicMock(return_value=["p1"])
    return orch


@pytest.mark.asyncio
async def test_async_method_is_awaited_and_forwarded():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.create_session(name="S", account="a") == "created"
    orch.create_session.assert_awaited_once_with(name="S", account="a")


@pytest.mark.asyncio
async def test_create_session_normalizes_explicit_engine_before_forwarding():
    orch = _orch()
    facade = InProcessOrchestratorAdapter(orch)

    await facade.create_session(name="S", engine="claude_code")

    orch.create_session.assert_awaited_once_with(
        name="S", engine=AgentEngine.CLAUDE_CODE)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["openai", None])
async def test_create_session_rejects_invalid_engine_before_forwarding(engine):
    orch = _orch()
    facade = InProcessOrchestratorAdapter(orch)

    with pytest.raises(InvalidAgentEngine):
        await facade.create_session(name="S", engine=engine)

    orch.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_method_is_wrapped_awaitable():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.get_session("s1") == "rec"
    orch.get_session.assert_called_once_with("s1")
    assert await f.resolve_permission("s1", "p1", "allow", "me") is True


@pytest.mark.asyncio
async def test_refine_prompt_codex_is_forwarded_and_awaited():
    orch = _orch()
    orch.refine_prompt_codex = mock.AsyncMock(return_value="Refined via Codex.")
    facade = InProcessOrchestratorAdapter(orch)

    result = await facade.refine_prompt_codex("codex-bot", "fix the bug", timeout_s=20.0)

    assert result == "Refined via Codex."
    orch.refine_prompt_codex.assert_awaited_once_with("codex-bot", "fix the bug", timeout_s=20.0)


@pytest.mark.asyncio
async def test_usage_wraps_poller_get():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.usage("acct") == {"usage": 1}
    orch.poller.get.assert_awaited_once_with("acct")


@pytest.mark.asyncio
async def test_pending_permissions_wraps_registry():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.pending_permissions("s1") == ["p1"]
    orch._registry.pending.assert_called_once_with("s1")


# --- regression: pipeline route family must be covered by the adapter ---

def _pipeline_orch():
    """Mock orchestrator with the three pipeline methods."""
    orch = mock.MagicMock()
    orch.poller = mock.MagicMock()
    orch._registry = mock.MagicMock()
    orch.latest_queue_entries = mock.AsyncMock(return_value={"repo/a": [{"id": 1}]})
    orch.all_merge_trains = mock.AsyncMock(return_value={"repo/a": {"status": "idle"}})
    orch.check_insights = mock.MagicMock(return_value={"repo": "repo/a", "window": "7d",
                                                        "slowest": [], "flakiest": [],
                                                        "regressions": [], "runner_wait_health": []})
    orch.ready_merge = mock.AsyncMock(return_value={"markedReady": True, "autoMergeArmed": True})
    return orch


@pytest.mark.asyncio
async def test_latest_queue_entries_delegates():
    """GET /api/pipeline/events must not raise AttributeError in in-process mode."""
    orch = _pipeline_orch()
    f = InProcessOrchestratorAdapter(orch)
    result = await f.latest_queue_entries()
    assert result == {"repo/a": [{"id": 1}]}
    orch.latest_queue_entries.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_all_merge_trains_delegates():
    """Pipeline merge-train endpoint must work in in-process mode."""
    orch = _pipeline_orch()
    f = InProcessOrchestratorAdapter(orch)
    result = await f.all_merge_trains()
    assert result == {"repo/a": {"status": "idle"}}
    orch.all_merge_trains.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_check_insights_delegates():
    """Pipeline check-insights endpoint must work in in-process mode (sync underlying method)."""
    orch = _pipeline_orch()
    f = InProcessOrchestratorAdapter(orch)
    result = await f.check_insights("repo/a", "7d")
    assert result["repo"] == "repo/a"
    assert result["window"] == "7d"
    orch.check_insights.assert_called_once_with("repo/a", "7d")


@pytest.mark.asyncio
async def test_ready_merge_delegates():
    """POST /api/pipeline/pr/ready-merge must work in in-process mode."""
    orch = _pipeline_orch()
    f = InProcessOrchestratorAdapter(orch)
    result = await f.ready_merge("repo/a", 7)
    assert result == {"markedReady": True, "autoMergeArmed": True}
    orch.ready_merge.assert_awaited_once_with("repo/a", 7)


@pytest.mark.asyncio
async def test_pr_detail_delegates():
    """GET /api/prs/{owner}/{name}/{number} must work in in-process mode."""
    orch = _pipeline_orch()
    orch.pr_detail = mock.AsyncMock(return_value={"number": 7, "checks": []})
    f = InProcessOrchestratorAdapter(orch)
    result = await f.pr_detail("repo/a", 7)
    assert result == {"number": 7, "checks": []}
    orch.pr_detail.assert_awaited_once_with("repo/a", 7)


@pytest.mark.asyncio
async def test_deploy_config_methods_delegate():
    """GET/PUT /api/pipeline/deploy-config + deploy_status must work in-process."""
    orch = mock.MagicMock()
    orch.poller = mock.MagicMock()
    orch._registry = mock.MagicMock()
    orch.deploy_config_list = mock.AsyncMock(return_value={"o/r": {"enabled": True}})
    orch.deploy_config_put = mock.AsyncMock(return_value=None)
    orch.deploy_status = mock.MagicMock(return_value={"o/r": [{"name": "qa"}]})
    f = InProcessOrchestratorAdapter(orch)
    assert await f.deploy_config_list() == {"o/r": {"enabled": True}}
    await f.deploy_config_put("o/r", [{"name": "qa"}], True)
    orch.deploy_config_put.assert_awaited_once_with("o/r", [{"name": "qa"}], True)
    assert await f.deploy_status() == {"o/r": [{"name": "qa"}]}


def test_facade_covers_every_orchestrator_method_the_api_calls():
    """Every `orch.<method>(` call in server/api/*.py must exist on the facade —
    the RemoteOrchestrator breadth test guards daemon mode; this guards in-process
    mode (the Task-3 facade gap class)."""
    import re
    from pathlib import Path
    from server.sessions.facade import InProcessOrchestratorAdapter

    api_dir = Path(__file__).resolve().parents[1] / "api"
    called = set()
    for f in api_dir.glob("*.py"):
        called |= set(re.findall(r"\borch\.(\w+)\(", f.read_text()))
    missing = {m for m in called if not hasattr(InProcessOrchestratorAdapter, m)}
    assert not missing, f"facade missing: {sorted(missing)}"


@pytest.mark.asyncio
async def test_create_session_pops_prefer_kwarg(monkeypatch):
    """In-process create_session ignores prefer= (Ruling D).

    FleetOrchestrator always passes prefer= through the unified interface.
    InProcessOrchestratorAdapter must strip it before forwarding so the underlying
    SessionOrchestrator (which has no prefer= param) never sees it.
    """
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    # Pass prefer= — must succeed and NOT forward prefer to the underlying orch
    result = await f.create_session(prefer="rocky", name="S", account="a")
    assert result == "created"
    # The underlying create_session must NOT have received prefer=
    orch.create_session.assert_awaited_once_with(name="S", account="a")


@pytest.mark.asyncio
async def test_create_session_prefer_none_also_works():
    """prefer=None (the common case) must also be stripped cleanly."""
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    result = await f.create_session(prefer=None, name="S", account="a")
    assert result == "created"
    orch.create_session.assert_awaited_once_with(name="S", account="a")


@pytest.mark.asyncio
async def test_note_user_interaction_delegates():
    orch = mock.MagicMock()
    orch.poller = mock.MagicMock(); orch._registry = mock.MagicMock()
    orch.note_user_interaction = mock.MagicMock(return_value=None)
    f = InProcessOrchestratorAdapter(orch)
    await f.note_user_interaction("s1")
    orch.note_user_interaction.assert_called_once_with("s1")


def test_every_orchestrator_method_session_actions_calls_is_proxied():
    """`server/sessions/session_actions.py` is reached with `app.state.sessions` —
    the FACADE in-process, `RemoteOrchestrator` in remote mode — never the bare
    `SessionOrchestrator`. A method missing from either proxy is an AttributeError
    → 500 at runtime, and every route test that drives session_actions through a
    *fake* orchestrator stays green while it happens (the #1199 failure shape).

    `set_model_for_session` was exactly that: absent from both proxies, so
    `POST /api/sessions/{id}/model` 500'd against the real app while
    test_sessions_actions_api.py passed on its fake.
    """
    import ast
    import inspect as _inspect
    from pathlib import Path

    from server.sessions.remote import RemoteOrchestrator

    src = Path(_inspect.getfile(InProcessOrchestratorAdapter)).parent / "session_actions.py"
    tree = ast.parse(src.read_text())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "orch"
    }
    assert called, "found no orch.* calls — the AST scan broke, not the code"

    for proxy in (InProcessOrchestratorAdapter, RemoteOrchestrator):
        missing = sorted(m for m in called if not hasattr(proxy, m))
        assert not missing, f"{proxy.__name__} does not proxy: {missing}"
