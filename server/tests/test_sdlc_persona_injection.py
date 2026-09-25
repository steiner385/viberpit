"""A spec-stage synth spawn injects its initiative's PRIMARY capability's
linked personas' docs into the seed prompt (#2447) — `PersonaFeature`
(#2164/#2453) resolved onto `InitiativeFeature.is_primary`, each doc read off
the committed default branch the same way intent.md/spec.md/the constitution
already are (`_sdlc_show_committed_file`).
"""
from __future__ import annotations

import asyncio

from server.sessions.engine import AgentEngine


def _bare_orchestrator():
    from server.sessions import orchestrator as orch_mod
    return orch_mod, orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)


async def _seed_capability_with_personas() -> str:
    """One project, one Feature (capability), a live persona and an orphaned
    persona both linked to it via PersonaFeature, one Initiative whose
    InitiativeFeature marks that capability primary. Returns initiative_id."""
    from server.db import db_session
    from server.db.models import (
        Feature, Initiative, InitiativeFeature, Persona, PersonaFeature, Project)

    async with db_session() as db:
        project = Project(slug="kindash", name="KinDash")
        db.add(project)
        await db.flush()
        feature = Feature(project_id=project.id, slug="task-views", title="Task views",
                          state="live")
        db.add(feature)
        await db.flush()
        persona = Persona(project_id=project.id, slug="parent", title="Parent",
                          repo_path="docs/product/personas/parent.md")
        orphan_persona = Persona(project_id=project.id, slug="ghost", title="Ghost",
                                 repo_path="docs/product/personas/ghost.md", orphaned=True)
        db.add_all([persona, orphan_persona])
        await db.flush()
        db.add(PersonaFeature(persona_id=persona.id, feature_id=feature.id))
        db.add(PersonaFeature(persona_id=orphan_persona.id, feature_id=feature.id))
        ini = Initiative(slug="views", repo="o/r", title="Task views", stage="design",
                         status="active", project_id=project.id)
        db.add(ini)
        await db.flush()
        db.add(InitiativeFeature(initiative_id=ini.id, feature_id=feature.id, is_primary=True))
        await db.commit()
        return ini.id


def test_capability_personas_resolves_the_primary_capabilitys_live_personas_only(isolated_db):
    ini_id = asyncio.run(_seed_capability_with_personas())
    _orch_mod, orch = _bare_orchestrator()

    result = asyncio.run(orch._sdlc_capability_personas(ini_id))

    assert result == [("parent", "docs/product/personas/parent.md")]


def test_capability_personas_is_none_without_a_primary_capability(isolated_db):
    from server.db import db_session
    from server.db.models import Initiative, Project

    async def _seed() -> str:
        async with db_session() as db:
            project = Project(slug="kindash", name="KinDash")
            db.add(project)
            await db.flush()
            ini = Initiative(slug="orphan", repo="o/r", title="Orphan", stage="design",
                             status="active", project_id=project.id)
            db.add(ini)
            await db.commit()
            return ini.id

    ini_id = asyncio.run(_seed())
    _orch_mod, orch = _bare_orchestrator()

    assert asyncio.run(orch._sdlc_capability_personas(ini_id)) is None


def test_capability_personas_is_none_without_an_initiative_id():
    _orch_mod, orch = _bare_orchestrator()
    assert asyncio.run(orch._sdlc_capability_personas(None)) is None


def _monkeypatch_spawn_seams(monkeypatch, orch, orch_mod, *, show_committed_file):
    class Ctx:
        async def launch_plan_or_refuse(self, _origin_ref):
            return AgentEngine.CLAUDE_CODE, {}

        async def spawn_session(self, **kwargs):
            captured["spawn"] = kwargs
            return type("Rec", (), {"id": "sess-1"})()

    captured: dict = {}

    async def probe(_ctx, _origin_ref, *, engine=None):
        return "acct-1"

    async def read_config():
        return "auto", {}

    async def repo_root():
        return "/tmp"

    async def default_branch(_repo):
        return "master"

    monkeypatch.setattr(orch, "_sdlc_synth_ctx", Ctx(), raising=False)
    monkeypatch.setattr(orch, "_automation_account_for", probe, raising=False)
    monkeypatch.setattr(orch, "_sdlc_synth_read_config", read_config, raising=False)
    monkeypatch.setattr(orch, "_effective_repo_root", repo_root, raising=False)
    monkeypatch.setattr(orch, "_sdlc_default_branch", default_branch, raising=False)
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone", lambda *a, **k: "/tmp/clone")
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_sdlc_show_committed_file",
                        staticmethod(show_committed_file))
    return captured


def test_spec_spawn_injects_the_primary_capabilitys_persona_docs(monkeypatch, isolated_db):
    ini_id = asyncio.run(_seed_capability_with_personas())
    orch_mod, orch = _bare_orchestrator()

    def show_committed_file(cwd, ref, path):
        if path == "docs/sdlc/views/intent.md":
            return "# Intent\n\nMake task views usable.\n"
        if path == ".specify/memory/constitution.md":
            return "# Constitution"
        if path == "docs/product/personas/parent.md":
            return "# Parent\n\nRole: primary caregiver.\n"
        raise AssertionError(f"unexpected committed-file read: {path}")

    captured = _monkeypatch_spawn_seams(monkeypatch, orch, orch_mod,
                                        show_committed_file=show_committed_file)

    ini = {"id": ini_id, "slug": "views", "repo": "o/r", "title": "Task views",
          "origin": "manual"}
    rec = asyncio.run(orch._sdlc_spawn_synth(ini, "spec"))

    assert rec is not None
    prompt = captured["spawn"]["initial_prompt"]
    assert "PERSONA `parent`" in prompt
    assert "Role: primary caregiver." in prompt
    assert "must name which persona" in prompt.lower()


def test_spec_spawn_without_a_primary_capability_omits_the_persona_section(
        monkeypatch, isolated_db):
    """An initiative with no InitiativeFeature row at all (never linked to a
    capability) must spawn exactly as it did before this feature existed."""
    orch_mod, orch = _bare_orchestrator()

    def show_committed_file(cwd, ref, path):
        if path == "docs/sdlc/views/intent.md":
            return "# Intent\n\nMake task views usable.\n"
        if path == ".specify/memory/constitution.md":
            return "# Constitution"
        raise AssertionError(f"unexpected committed-file read: {path}")

    captured = _monkeypatch_spawn_seams(monkeypatch, orch, orch_mod,
                                        show_committed_file=show_committed_file)

    ini = {"id": "ini-no-capability", "slug": "views", "repo": "o/r", "title": "Task views",
          "origin": "manual"}
    rec = asyncio.run(orch._sdlc_spawn_synth(ini, "spec"))

    assert rec is not None
    prompt = captured["spawn"]["initial_prompt"]
    assert "PERSONA `" not in prompt


def test_plan_spawn_never_resolves_or_reads_personas(monkeypatch, isolated_db):
    """The plan stage (spec.md -> plan.md) has no persona injection (issue
    scope: spec prompt only) — a plan spawn must not even ask which capability
    is primary, let alone read a persona doc off git."""
    orch_mod, orch = _bare_orchestrator()

    def show_committed_file(cwd, ref, path):
        if path == "docs/sdlc/views/spec.md":
            return "# Spec\n\nDone.\n"
        raise AssertionError(f"unexpected committed-file read for a plan spawn: {path}")

    captured = _monkeypatch_spawn_seams(monkeypatch, orch, orch_mod,
                                        show_committed_file=show_committed_file)

    async def personas_should_not_be_called(_initiative_id):
        raise AssertionError("plan spawn must not resolve capability personas")

    monkeypatch.setattr(orch, "_sdlc_capability_personas", personas_should_not_be_called,
                        raising=False)

    ini = {"id": "ini-1", "slug": "views", "repo": "o/r", "title": "Task views"}
    rec = asyncio.run(orch._sdlc_spawn_synth(ini, "plan"))

    assert rec is not None
    assert "PERSONA `" not in captured["spawn"]["initial_prompt"]
