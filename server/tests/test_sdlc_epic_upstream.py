"""Epic-adopted initiatives seed their spec-stage prompt from the mirrored epic
issue + its direct sub-issues instead of a committed ``intent.md`` that never
existed for them (#2159).

``origin == "epic-adoption"`` initiatives (``server/pm/mirror_store.py:adopt_epics``)
enter the SDLC chain directly at whatever stage their board card carries — the
intake stage that would have written ``intent.md`` never runs. Before this fix,
``_sdlc_spawn_synth`` still tried to ``git show origin/<default>:docs/sdlc/<slug>/intent.md``
for these, always got the "(could not inline ... read that file from the
repository before starting)" fallback, and the spec session invented a spec
from the slug alone.
"""
from __future__ import annotations

import asyncio

from server.sessions.engine import AgentEngine


def _bare_orchestrator():
    from server.sessions import orchestrator as orch_mod
    return orch_mod, orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)


async def _seed_epic(*, children: bool = True) -> None:
    from server.db import db_session
    from server.db.models import WorkItem

    async with db_session() as db:
        epic = WorkItem(repo="o/r", number=42, kind="issue", title="Recovery epic",
                        body="Epic body describing the goal.", is_epic=True, state="open")
        db.add(epic)
        await db.flush()
        if children:
            db.add(WorkItem(repo="o/r", number=43, kind="issue", title="Child one",
                            body="Child one body.", parent_work_item_id=epic.id,
                            state="open"))
            db.add(WorkItem(repo="o/r", number=44, kind="issue", title="Child two",
                            body="Child two body.", parent_work_item_id=epic.id,
                            state="open"))
        await db.commit()


def test_epic_upstream_text_includes_epic_and_children(isolated_db):
    asyncio.run(_seed_epic())
    _orch_mod, orch = _bare_orchestrator()

    result = asyncio.run(orch._sdlc_epic_upstream_text("o/r", 42))

    assert result is not None
    text, ref = result
    assert ref == "o/r#42"
    assert "epic o/r#42" in text
    assert "no intent.md exists" in text
    assert "Recovery epic" in text
    assert "Epic body describing the goal." in text
    assert "Child one body." in text
    assert "Child two body." in text


def test_epic_upstream_text_is_none_when_epic_not_mirrored(isolated_db):
    asyncio.run(_seed_epic(children=False))
    _orch_mod, orch = _bare_orchestrator()

    assert asyncio.run(orch._sdlc_epic_upstream_text("o/r", 999)) is None


def test_epic_upstream_text_is_none_without_a_pointer():
    _orch_mod, orch = _bare_orchestrator()
    assert asyncio.run(orch._sdlc_epic_upstream_text(None, None)) is None


def test_epic_adoption_spec_spawn_seeds_from_the_epic_not_intent_md(
        monkeypatch, isolated_db):
    """The spec-stage synth spawn for an epic-adoption initiative must never read
    the (nonexistent) committed ``intent.md`` and must carry the epic + children
    text plus the "no intent.md exists" disclosure into the prompt."""
    asyncio.run(_seed_epic())
    orch_mod, orch = _bare_orchestrator()
    captured: dict = {}

    class Ctx:
        async def launch_plan_or_refuse(self, _origin_ref):
            return AgentEngine.CLAUDE_CODE, {}

        async def spawn_session(self, **kwargs):
            captured["spawn"] = kwargs
            return type("Rec", (), {"id": "sess-1"})()

    async def probe(_ctx, _origin_ref, *, engine=None):
        return "acct-1"

    async def read_config():
        return "auto", {}

    async def repo_root():
        return "/tmp"

    async def default_branch(_repo):
        return "master"

    def show_committed_file(cwd, ref, path):
        if path == ".specify/memory/constitution.md":
            return "# Constitution"
        raise AssertionError(
            f"must not read {path} for an epic-adoption initiative")

    monkeypatch.setattr(orch, "_sdlc_synth_ctx", Ctx(), raising=False)
    monkeypatch.setattr(orch, "_automation_account_for", probe, raising=False)
    monkeypatch.setattr(orch, "_sdlc_synth_read_config", read_config, raising=False)
    monkeypatch.setattr(orch, "_effective_repo_root", repo_root, raising=False)
    monkeypatch.setattr(orch, "_sdlc_default_branch", default_branch, raising=False)
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone", lambda *a, **k: "/tmp/clone")
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_sdlc_show_committed_file",
                        staticmethod(show_committed_file))

    ini = {"id": "ini-1", "slug": "widget", "repo": "o/r", "title": "Widget",
          "origin": "epic-adoption", "epic_repo": "o/r", "epic_number": 42}
    rec = asyncio.run(orch._sdlc_spawn_synth(ini, "spec"))

    assert rec is not None
    prompt = captured["spawn"]["initial_prompt"]
    assert "epic o/r#42" in prompt
    assert "no intent.md exists" in prompt
    assert "Recovery epic" in prompt
    assert "Child one body." in prompt
    assert "Child two body." in prompt
    assert rec.upstream_ref == "o/r#42"


def test_a_non_epic_adoption_initiative_still_reads_the_committed_intent(
        monkeypatch, isolated_db):
    """Guard rail: the epic-body substitution must never engage for a normal
    (human-created) initiative — it keeps reading the real committed file."""
    orch_mod, orch = _bare_orchestrator()
    captured: dict = {}

    class Ctx:
        async def launch_plan_or_refuse(self, _origin_ref):
            return AgentEngine.CLAUDE_CODE, {}

        async def spawn_session(self, **kwargs):
            captured["spawn"] = kwargs
            return type("Rec", (), {"id": "sess-1"})()

    async def probe(_ctx, _origin_ref, *, engine=None):
        return "acct-1"

    async def read_config():
        return "auto", {}

    async def repo_root():
        return "/tmp"

    async def default_branch(_repo):
        return "master"

    def show_committed_file(cwd, ref, path):
        if path == ".specify/memory/constitution.md":
            return "# Constitution"
        return "the real committed intent body"

    monkeypatch.setattr(orch, "_sdlc_synth_ctx", Ctx(), raising=False)
    monkeypatch.setattr(orch, "_automation_account_for", probe, raising=False)
    monkeypatch.setattr(orch, "_sdlc_synth_read_config", read_config, raising=False)
    monkeypatch.setattr(orch, "_effective_repo_root", repo_root, raising=False)
    monkeypatch.setattr(orch, "_sdlc_default_branch", default_branch, raising=False)
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone", lambda *a, **k: "/tmp/clone")
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_sdlc_show_committed_file",
                        staticmethod(show_committed_file))

    ini = {"id": "ini-1", "slug": "widget", "repo": "o/r", "title": "Widget",
          "origin": "human"}
    rec = asyncio.run(orch._sdlc_spawn_synth(ini, "spec"))

    assert rec is not None
    assert "the real committed intent body" in captured["spawn"]["initial_prompt"]
    assert getattr(rec, "upstream_ref", None) is None
