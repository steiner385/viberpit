import pytest
from server.automations.pr_intent import PrIntentVerdict, classify_pr_intent


def _fake_query(reply: str):
    async def q(*, prompt, options=None):
        class _Blk:
            text = reply
        class _Msg:
            content = [_Blk()]
        yield _Msg()
    return q


@pytest.mark.asyncio
async def test_high_confidence_cancel_resolves():
    q = _fake_query('{"resolved": true, "reason": "cancelled", "confidence": "high"}')
    v = await classify_pr_intent([{"role": "assistant", "text": "we are dropping this"}],
                                 "fix ci", _query_fn=q)
    assert v == PrIntentVerdict(True, "cancelled", "high")


@pytest.mark.asyncio
async def test_ambiguous_is_unresolved():
    q = _fake_query('{"resolved": false, "reason": "", "confidence": "low"}')
    v = await classify_pr_intent([], "fix ci", _query_fn=q)
    assert v.resolved is False


@pytest.mark.asyncio
async def test_llm_error_returns_unresolved():
    async def boom(*, prompt, options=None):
        raise RuntimeError("model down")
        yield  # pragma: no cover
    v = await classify_pr_intent([], "fix ci", _query_fn=boom)
    assert v == PrIntentVerdict(False, "", "low")


@pytest.mark.asyncio
async def test_unparseable_reply_returns_unresolved():
    q = _fake_query("I think maybe not sure")
    v = await classify_pr_intent([], "fix ci", _query_fn=q)
    assert v.resolved is False


@pytest.mark.asyncio
async def test_sdk_import_failure_degrades(monkeypatch):
    import server.automations.pr_intent as pi
    def boom():
        raise ImportError("no sdk")
    monkeypatch.setattr(pi, "_default_sdk_query_fn", boom)
    v = await pi.classify_pr_intent([], "goal")
    assert v == pi.PrIntentVerdict(False, "", "low")


def test_system_prompt_treats_deferral_as_unresolved():
    """The classifier is the ONLY verification of a session's own no-PR claim on a
    code goal, so it must judge against the goal: recommending a next step, offering
    to continue, or waiting on the user is NOT a resolution that the goal needs no
    code change (prod 2026-09-06, session fdc6b27a)."""
    from server.automations.pr_intent import _SYSTEM
    low = _SYSTEM.lower()
    assert "goal" in low
    assert "next step" in low
    assert "offering to" in low or "offer to" in low


@pytest.mark.asyncio
async def test_no_helper_account_is_unresolved_without_opening_the_model_door(monkeypatch):
    """Live 2026-08-28..09-11: 44 of 44 classify calls failed "Not logged in" because
    the orchestrator passed no config dir and the daemon has no CLAUDE_CONFIG_DIR. An
    empty dir is a known-dead lane: degrade immediately, never spawn."""
    import server.automations.pr_intent as pi

    opened = []

    def door():
        opened.append(True)
        return _fake_query('{"resolved": true, "reason": "cancelled", "confidence": "high"}')
    monkeypatch.setattr(pi, "_default_sdk_query_fn", door)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    v = await pi.classify_pr_intent([], "goal", _config_dir="")
    assert v == pi.PrIntentVerdict(False, "", "low")
    assert opened == [], "the SDK door must not be opened without a helper account"
