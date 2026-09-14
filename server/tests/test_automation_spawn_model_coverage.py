"""Every automation that spawns sessions can be told which model to use.

Two halves:

* **Coverage sweeps** — static guards over the orchestrator's own source, so a
  spawn site added later cannot quietly go back to inheriting the account
  default. They answer "which automations spawn?" from the code rather than a
  hand-maintained list, which is the only version that stays true.
* **Per-site wiring** — the four origins that had no model control before
  (sdlc-intake, pm-onboard, pm-classify, control-band-monitor) resolve their
  ``default_model`` config into the ``model=`` / ``reasoning_effort=`` create
  kwargs and stamp the provenance into the spawned event.

backlog-runner and pr-doctor are covered in ``test_automation_spawn_model.py``;
the assessor itself in ``test_model_pick.py``.
"""
from __future__ import annotations

import ast
import inspect
import types

import pytest

from server.automations.model_pick import AUTOMATION_MODEL_CHOICES
from server.sessions.engine import AgentEngine


# --------------------------------------------------------------------------- #
# Source introspection helpers                                                 #
# --------------------------------------------------------------------------- #

def _orchestrator_ast() -> tuple[ast.Module, str]:
    import server.sessions.orchestrator as orch_mod
    src = inspect.getsource(orch_mod)
    return ast.parse(src), src


def _ctx_to_automation_id(tree: ast.Module) -> dict[str, str]:
    """``self._pr_doctor_ctx`` → ``"pr-doctor"``, read off the construction sites."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if not (isinstance(target, ast.Attribute) and target.attr.endswith("_ctx")):
            continue
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "AutomationContext"):
            continue
        if value.args and isinstance(value.args[0], ast.Constant):
            found[target.attr] = str(value.args[0].value)
    return found


def _spawn_calls(tree: ast.Module) -> list[tuple[str, ast.Call]]:
    """``(ctx attribute name, call node)`` for every ``<ctx>.spawn_session(...)``.

    Skips the recurring scheduler's call, whose context is a local built from a
    dynamic id: an ad-hoc recurring automation has no registered config schema,
    and its cron spec already carries its own ``model`` (``_recurring_spawn``).
    """
    calls: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "spawn_session"):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Attribute) and owner.attr.endswith("_ctx"):
            calls.append((owner.attr, node))
    return calls


# --------------------------------------------------------------------------- #
# Coverage sweeps                                                              #
# --------------------------------------------------------------------------- #

def test_every_spawning_automation_offers_a_session_model():
    """An origin that spawns sessions must expose a ``default_model`` setting.

    Derived, not listed: the spawn sites name their contexts and the contexts
    name their automation ids, so a new spawning origin fails here until it
    carries the setting. ``retired_config_keys`` is the one honoured opt-out —
    backlog-runner moved its ``default_model`` to the PRODUCT policy on purpose.
    """
    from server.automations.registry import registry

    tree, _src = _orchestrator_ast()
    ctx_ids = _ctx_to_automation_id(tree)
    spawning = {ctx_ids[attr] for attr, _call in _spawn_calls(tree) if attr in ctx_ids}
    assert spawning, "no spawn sites found — the AST sweep has drifted"

    missing = []
    for automation_id in sorted(spawning):
        automation = registry.get(automation_id)
        if "default_model" in getattr(automation, "retired_config_keys", ()):
            continue  # moved to a narrower policy (see backlog-runner)
        keys = {f.key for f in registry.schema_for(automation_id)}
        if "default_model" not in keys:
            missing.append(automation_id)
    assert not missing, (
        f"these automations spawn sessions with no model control: {missing}")


def test_spawning_automations_offer_the_whole_model_catalog():
    """One dropdown contract, so a new alias in the catalog reaches every
    automation at once rather than the two someone remembered to update."""
    from server.automations.registry import registry

    tree, _src = _orchestrator_ast()
    ctx_ids = _ctx_to_automation_id(tree)
    spawning = {ctx_ids[attr] for attr, _call in _spawn_calls(tree) if attr in ctx_ids}
    for automation_id in sorted(spawning):
        for field in registry.schema_for(automation_id):
            if field.key != "default_model":
                continue
            assert field.type == "enum", automation_id
            assert list(field.choices or []) == AUTOMATION_MODEL_CHOICES, automation_id


def test_every_spawn_site_passes_an_explicit_model():
    """A spawn that omits ``model=`` inherits whatever the account default
    happens to be — the shared-``settings.json`` hazard that makes a fleet-wide
    ``/model`` pick leak into unattended work. Every site states its answer."""
    tree, _src = _orchestrator_ast()
    ctx_ids = _ctx_to_automation_id(tree)
    offenders = []
    for attr, call in _spawn_calls(tree):
        if attr not in ctx_ids:
            continue
        if not any(kw.arg == "model" for kw in call.keywords):
            offenders.append(f"{ctx_ids[attr]} (line {call.lineno})")
    assert not offenders, f"spawn sites with no explicit model: {offenders}"


def test_only_one_place_resolves_a_spawn_model():
    """The engine branch (Claude assessor vs Codex effort catalog) lives in
    ``_resolve_spawn_model`` alone. It was copied verbatim at five spawn sites;
    a sixth copy is how the Claude/Codex split drifts apart (issue #1289)."""
    import server.sessions.orchestrator as orch_mod

    tree, _src = _orchestrator_ast()
    # Two functions form the one resolver: the engine branch, and the Codex leg
    # it delegates to (which needs the orchestrator's live catalog client).
    spans: dict[str, range] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
                "_resolve_spawn_model", "_resolve_codex_auto_effort"):
            spans[node.name] = range(node.lineno, (node.end_lineno or node.lineno) + 1)
    assert set(spans) == {"_resolve_spawn_model", "_resolve_codex_auto_effort"}, (
        f"resolver helpers moved or were renamed: {sorted(spans)}")
    allowed = {
        "pick_model": spans["_resolve_spawn_model"],
        "resolve_model": spans["_resolve_spawn_model"],
        "_resolve_codex_auto_effort": spans["_resolve_spawn_model"],
        "resolve_codex_model": spans["_resolve_codex_auto_effort"],
    }
    strays = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.id if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else None)
        span = allowed.get(name or "")
        if span is not None and node.lineno not in span:
            strays.append(f"{name}() at line {node.lineno}")
    assert not strays, f"model resolution outside the one resolver: {strays}"
    assert inspect.iscoroutinefunction(orch_mod.SessionOrchestrator._resolve_spawn_model)


# --------------------------------------------------------------------------- #
# The shared resolver                                                          #
# --------------------------------------------------------------------------- #

def _bare_orch():
    import server.sessions.orchestrator as orch_mod
    return orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)


def _assess_returning(tier):
    async def assess(task_summary, **kw):
        return tier
    return assess


@pytest.mark.asyncio
async def test_resolver_pins_a_claude_alias():
    orch = _bare_orch()
    choice = await orch._resolve_spawn_model(
        AgentEngine.CLAUDE_CODE, "acct", {"default_model": "opus"}, "a job")
    assert choice.model == "opus"
    assert choice.reasoning_effort is None
    assert choice.detail == {"model": "opus", "model_source": "pinned"}


@pytest.mark.asyncio
async def test_resolver_default_leaves_the_account_default_alone():
    orch = _bare_orch()
    choice = await orch._resolve_spawn_model(
        AgentEngine.CLAUDE_CODE, "acct", {"default_model": "default"}, "a job")
    assert choice.model is None and choice.detail == {}


@pytest.mark.asyncio
async def test_resolver_auto_assesses_and_honours_a_floor():
    orch = _bare_orch()
    choice = await orch._resolve_spawn_model(
        AgentEngine.CLAUDE_CODE, "acct", {"default_model": "auto"}, "a job",
        floor="opus", _assess=_assess_returning("haiku"))
    assert choice.model == "opus"
    assert choice.detail["model_source"] == "auto+floor"
    assert choice.detail["assessed"] == "haiku"
    assert choice.pick is not None and choice.pick.assessed == "haiku"


@pytest.mark.asyncio
async def test_resolver_maps_codex_onto_reasoning_effort():
    """#1289: a Codex spawn never carries a Claude alias."""
    orch = _bare_orch()

    async def fake_effort(account, config, summary):
        return "high", "auto"
    orch._resolve_codex_auto_effort = fake_effort  # type: ignore[method-assign]

    choice = await orch._resolve_spawn_model(
        AgentEngine.CODEX, "acct", {"default_model": "auto"}, "a job")
    assert choice.model is None
    assert choice.reasoning_effort == "high"
    assert choice.detail == {"reasoning_effort": "high", "model_source": "auto"}


@pytest.mark.asyncio
async def test_resolver_skips_the_codex_assessor_with_no_resolved_account():
    """PR Doctor lets ``create_session`` pick the account, so there is none to
    read a live catalog from. Falling back to the engine default beats calling
    the assessor with ``None``."""
    orch = _bare_orch()

    async def boom(account, config, summary):
        raise AssertionError("assessor called without an account")
    orch._resolve_codex_auto_effort = boom  # type: ignore[method-assign]

    choice = await orch._resolve_spawn_model(
        AgentEngine.CODEX, None, {"default_model": "auto"}, "a job")
    assert choice.model is None and choice.reasoning_effort is None
    assert choice.detail == {}


# --------------------------------------------------------------------------- #
# Per-site wiring                                                              #
# --------------------------------------------------------------------------- #

def _capture_spawn(captured):
    async def fake_spawn(**kw):
        captured.update(kw)
        return types.SimpleNamespace(id="new-sess")
    return fake_spawn


def _stub_common(orch, monkeypatch, tmp_path, *, engine, config):
    """Account, clone and default-branch seams every spawn site crosses."""
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone",
                        lambda root, repo, url: str(tmp_path))

    async def account_for(ctx, ref, **kw):
        return "acct"
    orch._automation_account_for = account_for  # type: ignore[method-assign]

    async def repo_root():
        return str(tmp_path)
    orch._effective_repo_root = repo_root  # type: ignore[method-assign]

    async def default_branch(repo):
        return "master"
    orch._sdlc_default_branch = default_branch  # type: ignore[method-assign]

    async def plan(origin_ref):
        return engine, config
    return plan


@pytest.mark.asyncio
async def test_sdlc_intake_spawn_applies_its_configured_model(tmp_path, monkeypatch):
    from server.automations.context import AutomationContext

    orch = _bare_orch()
    orch._sdlc_intake_ctx = AutomationContext("sdlc-intake")
    _stub_common(orch, monkeypatch, tmp_path, engine=AgentEngine.CLAUDE_CODE,
                 config={"default_model": "opus"})

    async def read_config():
        return "auto", {"default_model": "opus"}
    orch._sdlc_intake_read_config = read_config  # type: ignore[method-assign]

    captured: dict = {}
    orch._sdlc_intake_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    trigger = types.SimpleNamespace(slug="widget-latency", repo="o/r", title="t",
                                    origin="human", origin_ref=None)
    await orch._sdlc_intake_spawn(trigger, "write the intent", AgentEngine.CLAUDE_CODE)

    assert captured["model"] == "opus"
    assert captured["detail"]["model_source"] == "pinned"


@pytest.mark.asyncio
async def test_control_band_diagnosis_applies_its_configured_model(tmp_path, monkeypatch):
    from server.automations.context import AutomationContext

    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("sonnet"))

    orch = _bare_orch()
    orch._control_band_ctx = AutomationContext("control-band-monitor")
    plan = _stub_common(orch, monkeypatch, tmp_path, engine=AgentEngine.CLAUDE_CODE,
                        config={"default_model": "auto"})
    orch._control_band_ctx.launch_plan_or_refuse = plan  # type: ignore[method-assign]

    captured: dict = {}
    orch._control_band_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    band = types.SimpleNamespace(id="pr-cycle-time", repo="o/r", source="gh")
    tier = types.SimpleNamespace(name="2sigma", response="diagnose",
                                 permission_mode="plan")
    await orch._control_band_spawn_diagnosis(
        "item-1", "diagnose the regression", band, tier)

    assert captured["model"] == "sonnet"
    assert captured["detail"]["model_source"] == "auto"
    # The tier's own permission mode still wins — the model pick must not
    # disturb the policy file's answer.
    assert captured["permission_mode"] == "plan"


@pytest.mark.asyncio
async def test_pm_onboard_critic_applies_its_configured_model(tmp_path, monkeypatch):
    from server.automations.context import AutomationContext

    orch = _bare_orch()
    orch._pm_onboard_ctx = AutomationContext("pm-onboard")
    plan = _stub_common(orch, monkeypatch, tmp_path, engine=AgentEngine.CLAUDE_CODE,
                        config={"default_model": "haiku"})
    orch._pm_onboard_ctx.launch_plan_or_refuse = plan  # type: ignore[method-assign]

    captured: dict = {}
    orch._pm_onboard_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._pm_onboard_spawn_critic("widget", "o/r", "pm/map-widget")

    assert captured["model"] == "haiku"
    assert captured["detail"]["model_source"] == "pinned"


@pytest.mark.asyncio
async def test_pm_onboard_critic_on_codex_carries_no_claude_alias(tmp_path, monkeypatch):
    """A pinned Claude alias means nothing to Codex: the spawn takes the
    engine's own default rather than smuggling ``haiku`` into a Codex launch."""
    from server.automations.context import AutomationContext

    orch = _bare_orch()
    orch._pm_onboard_ctx = AutomationContext("pm-onboard")
    plan = _stub_common(orch, monkeypatch, tmp_path, engine=AgentEngine.CODEX,
                        config={"default_model": "haiku"})
    orch._pm_onboard_ctx.launch_plan_or_refuse = plan  # type: ignore[method-assign]

    captured: dict = {}
    orch._pm_onboard_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._pm_onboard_spawn_critic("widget", "o/r", "pm/map-widget")

    assert captured["model"] is None
    assert captured["reasoning_effort"] is None
