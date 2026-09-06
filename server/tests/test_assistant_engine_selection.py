"""Which overlay an assistant session gets, and who decides (#1321/#1290).

The overlay a drawer runs under IS its security boundary, so the seam that picks
one has two failure modes worth a test each: handing the Codex overlay to the
Claude SDK factory (or the reverse), and silently substituting an engine the
operator did not ask for.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from server.assistant.policy import assistant_engine
from server.sessions.engine import AgentEngine


class _Api:
    identity = "user@example.com"

    async def get(self, *_a, **_k):
        return []


def _api() -> Any:
    """The AssistantApi seam, duck-typed."""
    return _Api()


# ---------------------------------------------------------------------------
# The deployment gate
# ---------------------------------------------------------------------------


def test_the_drawer_runs_on_claude_by_default(monkeypatch):
    monkeypatch.delenv("CCSWITCH_ASSISTANT_ENGINE", raising=False)
    assert assistant_engine() is AgentEngine.CLAUDE_CODE


def test_an_operator_can_move_the_drawer_to_codex(monkeypatch):
    monkeypatch.setenv("CCSWITCH_ASSISTANT_ENGINE", "codex")
    assert assistant_engine() is AgentEngine.CODEX


def test_an_unknown_engine_falls_back_to_the_audited_one(monkeypatch):
    # A typo must not make the drawer unopenable, and must not pick an engine at
    # random either — it runs on the one that has always run it.
    monkeypatch.setenv("CCSWITCH_ASSISTANT_ENGINE", "gpt-9")
    assert assistant_engine() is AgentEngine.CLAUDE_CODE


# ---------------------------------------------------------------------------
# The orchestrator seam
# ---------------------------------------------------------------------------


def _orch():
    from server.sessions.orchestrator import SessionOrchestrator

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._sdk_factory = None
    orch._x11 = None
    orch._records = {}
    orch._assistant_overlays = {}
    orch._clock = time.time
    return orch


def test_a_codex_overlay_is_never_handed_to_the_claude_sdk_factory(tmp_path,
                                                                   monkeypatch):
    """`_session_factory` reads the SAME registry the Codex factory does.

    A `CodexAssistantOverlay` passed as `options_overlay=` would be merged into
    `ClaudeAgentOptions` kwargs — at best a TypeError at spawn, at worst a Claude
    assistant with no `tools=[]` and no denylist.
    """
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "cchome"))
    from server.assistant.options_codex import assistant_codex_options

    seen: list[dict] = []

    def factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                on_pre_compact=None, **rest):
        seen.append(dict(rest))
        return None

    orch = _orch()
    orch._sdk_factory = factory
    orch.register_assistant_overlay(
        "asst", assistant_codex_options(api=_api(), model="gpt-5.6-sol"))
    orch._session_factory("asst")("/tmp", "/cfg", None, "interactive", None)
    assert seen[-1] == {}


def test_the_codex_factory_seam_returns_only_codex_overlays(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "cchome"))
    from server.assistant.options_codex import (CodexAssistantOverlay,
                                                assistant_codex_options)

    orch = _orch()
    orch.register_assistant_overlay("claude-asst", {"model": "sonnet", "tools": []})
    orch.register_assistant_overlay(
        "codex-asst", assistant_codex_options(api=_api(), model="gpt-5.6-sol"))

    assert orch._codex_assistant_overlay_for_session("claude-asst") is None
    assert orch._codex_assistant_overlay_for_session("nobody") is None
    assert isinstance(orch._codex_assistant_overlay_for_session("codex-asst"),
                      CodexAssistantOverlay)


@pytest.mark.asyncio
async def test_the_setup_helper_builds_the_overlay_for_the_session_engine(tmp_path,
                                                                          monkeypatch):
    """`_setup_assistant_overlay` is the ONE place all three driver-build sites
    (spawn, resume, reassign) go through, so the engine has to reach it."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "cchome"))

    class _Result:
        def scalar_one_or_none(self): return None

    class _Ctx:
        async def __aenter__(self):
            class _Db:
                async def execute(self, *a, **k): return _Result()
            return _Db()

        async def __aexit__(self, *a): return False

    import server.db as db_mod
    monkeypatch.setattr(db_mod, "db_session", lambda: _Ctx())

    from server.assistant.options_codex import CodexAssistantOverlay

    orch = _orch()
    await orch._setup_assistant_overlay(
        "s-claude", identity="u@example.com", model="sonnet",
        engine=AgentEngine.CLAUDE_CODE)
    await orch._setup_assistant_overlay(
        "s-codex", identity="u@example.com", model="gpt-5.6-sol",
        engine=AgentEngine.CODEX)

    assert isinstance(orch._assistant_overlays["s-claude"], dict)
    assert isinstance(orch._assistant_overlays["s-codex"], CodexAssistantOverlay)
