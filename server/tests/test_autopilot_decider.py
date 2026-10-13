# server/tests/test_autopilot_decider.py
import pytest
from server.automations.autopilot_policy import DecisionContext
from server.automations.autopilot_decider import decide, _build_prompt
from server.automations.completion_bar import PrFact, PrFacts


def _ctx():
    return DecisionContext(
        session_id="s1", status="waiting_input", account="a", repo=None, branch=None,
        effective_goal="ship the PR", goal_source="explicit",
        stuck={"kind": "permission", "tool_name": "Bash", "tool_input": {"command": "pytest"}},
        transcript_tail=[], ledger={})


def _fake_query(reply_text):
    async def gen(*, prompt, options):
        class _Block:
            text = reply_text
        class _Msg:
            content = [_Block()]
        yield _Msg()
    return gen


@pytest.mark.asyncio
async def test_valid_json_parses_to_decision():
    fn = _fake_query('{"action": "approve", "reason": "safe test run"}')
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "approve" and d.reason == "safe test run"


@pytest.mark.asyncio
async def test_malformed_reply_escalates():
    fn = _fake_query("not json at all")
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "escalate"


@pytest.mark.asyncio
async def test_model_raising_escalates():
    async def boom(*, prompt, options):
        raise RuntimeError("model down")
        yield  # pragma: no cover
    d = await decide(_ctx(), _query_fn=boom, _config_dir="", _cwd="/tmp")
    assert d.action == "escalate"


# Production failure shape (2026-07-03): the model role-slips into prose before the
# JSON. Prose with brace-bearing text around a fenced block must still parse —
# the old greedy find("{")..rfind("}") slice raised "Extra data" here.
_PROSE_WITH_FENCED_JSON = (
    'I see you\'re in autopilot mode {status: needs_attention} so let me think.\n'
    'Some more prose with a stray { brace and } another.\n'
    '```json\n{"action": "nudge", "prompt": "continue the work", "reason": "resume"}\n```\n'
    'Trailing remark.'
)


@pytest.mark.asyncio
async def test_prose_around_fenced_json_still_parses():
    fn = _fake_query(_PROSE_WITH_FENCED_JSON)
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "nudge"
    assert d.prompt == "continue the work"


@pytest.mark.asyncio
async def test_prose_with_unfenced_json_object_still_parses():
    # No fences: prose, then a valid object, then trailing prose with braces.
    fn = _fake_query('Thinking aloud first.\n'
                     '{"action": "complete", "reason": "goal met"}\n'
                     'PS: {unrelated} trailing {braces}.')
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "complete"


@pytest.mark.asyncio
async def test_parse_failure_logs_raw_reply(caplog):
    import logging
    fn = _fake_query("pure prose, no json anywhere")
    with caplog.at_level(logging.WARNING, logger="server.automations.autopilot_decider"):
        d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "escalate"
    # The raw reply head must be in the log so production failures are diagnosable.
    assert any("pure prose" in r.getMessage() for r in caplog.records)


def test_prompt_demands_json_only():
    # The output-format demand must live in the USER prompt too (the proven
    # rank_issues pattern) — a system-prompt-only instruction drifts under weak
    # context and the model answers in prose.
    from server.automations.autopilot_decider import _build_prompt
    p = _build_prompt(_ctx())
    assert "ONLY" in p and "JSON" in p



def test_build_prompt_includes_pr_facts():
    ctx = DecisionContext(
        session_id="s1", status="complete", account="a", repo="o/r", branch="feat/x",
        effective_goal="fix ci", goal_source="explicit", stuck={"kind": "complete"},
        transcript_tail=[], ledger={},
        pr_facts=PrFacts(has_repo=True, prs=(PrFact(7, "open", False, False, "u"),)))
    prompt = _build_prompt(ctx)
    assert "pr_facts" in prompt and "\"number\": 7" in prompt and "has_repo" in prompt


# ---------------------------------------------------------------------------
# Consecutive-failure escalation (2026-08-22/23: autopilot failed every call
# for over a day with nothing louder than a per-call log.warning).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_failure_escalates_log_level(caplog):
    import logging
    from server.automations import autopilot_decider as decider_mod

    async def boom(*, prompt, options):
        raise RuntimeError("model down")
        yield  # pragma: no cover

    decider_mod._consecutive_decide_failures = 0
    try:
        with caplog.at_level(logging.WARNING, logger="server.automations.autopilot_decider"):
            for _ in range(decider_mod._ESCALATE_FAILURE_THRESHOLD - 1):
                d = await decide(_ctx(), _query_fn=boom, _config_dir="", _cwd="/tmp")
                assert d.action == "escalate"
            # Below threshold: only WARNING-level records so far.
            assert all(r.levelno == logging.WARNING for r in caplog.records)

            d = await decide(_ctx(), _query_fn=boom, _config_dir="", _cwd="/tmp")
            assert d.action == "escalate"
        # The threshold-crossing call logs at ERROR, distinct from the blips before it.
        assert any(r.levelno == logging.ERROR for r in caplog.records)
    finally:
        decider_mod._consecutive_decide_failures = 0


@pytest.mark.asyncio
async def test_success_resets_consecutive_failure_counter():
    from server.automations import autopilot_decider as decider_mod

    async def boom(*, prompt, options):
        raise RuntimeError("model down")
        yield  # pragma: no cover

    decider_mod._consecutive_decide_failures = 0
    try:
        await decide(_ctx(), _query_fn=boom, _config_dir="", _cwd="/tmp")
        await decide(_ctx(), _query_fn=boom, _config_dir="", _cwd="/tmp")
        assert decider_mod._consecutive_decide_failures == 2

        fn = _fake_query('{"action": "approve", "reason": "back up"}')
        d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
        assert d.action == "approve"
        assert decider_mod._consecutive_decide_failures == 0
    finally:
        decider_mod._consecutive_decide_failures = 0


def test_build_prompt_includes_fleet_context():
    ctx = DecisionContext(
        session_id="s1", status="idle", account="a", repo="o/r", branch="feat/x",
        effective_goal="fix ci", goal_source="explicit", stuck={"kind": "idle"},
        transcript_tail=[], ledger={},
        fleet_context={"same_repo_sessions": [
            {"session": "sib", "branch": "feat/sib", "status": "done",
             "goal": "fix parser", "pr": "PR #591 merged"}]})
    prompt = _build_prompt(ctx)
    assert "fleet" in prompt and "PR #591 merged" in prompt and "feat/sib" in prompt


# Live 2026-09-09 05:35–10:02: two Codex-account sessions had no Anthropic helper
# dir (pick_helper_config_dir() → None → ""), so every decide() spawned a CLI that
# answered "Not logged in" and raised — 270 escalation events for one session in
# 4.5h. An unusable lane is a known answer; it must never cost a model spawn.
@pytest.mark.asyncio
async def test_no_helper_account_escalates_without_opening_the_model_door(monkeypatch):
    import server.automations.autopilot_decider as mod

    def never():
        raise AssertionError("the SDK door must not be opened without a helper account")
    monkeypatch.setattr(mod, "_default_sdk_query_fn", never)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    for cfg in ("", None):
        d = await decide(_ctx(), _config_dir=cfg, _cwd="/tmp")
        assert d.action == "escalate"
        assert d.reason == "decider-skipped:no-helper-account"


# A decider failure used to be logged as the bare SDK exception — for the whole of
# #1820 that read "Claude Code returned an error result: success", which is the
# result *subtype* leaking into the message and says nothing about the failure.
# Finding the real cause (HTTP 403 oauth_not_allowed_for_organization on one
# account) took a transcript dig; the log must carry it.
@pytest.mark.asyncio
async def test_failure_log_and_reason_name_the_classified_kind(caplog):
    import logging

    from server.automations import autopilot_decider as decider_mod

    denial = ("Your organization has disabled Claude subscription access for Claude "
              "Code · Use an Anthropic API key instead, or ask your admin to enable access")

    async def denied(*, prompt, options):
        class _Block:
            text = denial

        class _Msg:
            content = [_Block()]
            error = "oauth_org_not_allowed"
        yield _Msg()
        raise RuntimeError("Claude Code returned an error result: success")

    decider_mod._consecutive_decide_failures = 0
    try:
        with caplog.at_level(logging.WARNING, logger="server.automations.autopilot_decider"):
            d = await decide(_ctx(), _query_fn=denied, _config_dir="", _cwd="/tmp")
        assert d.action == "escalate"
        assert d.reason == "decider-failed:auth_forbidden"
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "auth_forbidden" in logged
        assert "organization has disabled" in logged
    finally:
        decider_mod._consecutive_decide_failures = 0


# #2953: rocky 2026-10-11 — 25 "skipped: no helper account" WARNINGs in the ~3
# minutes after a worker restart, each escalating a real session to a human, all
# before the edge's first creds_sync renewed the envelope lease. That window
# clears by itself: wait it out quietly; keep escalate + WARNING for the rest.
@pytest.mark.asyncio
async def test_no_helper_account_during_lease_renewal_waits_quietly(monkeypatch, caplog):
    import logging

    import server.automations.autopilot_decider as mod

    def never():
        raise AssertionError("the SDK door must not be opened without a helper account")
    monkeypatch.setattr(mod, "_default_sdk_query_fn", never)
    monkeypatch.setattr(mod, "helper_credentials_pending", lambda: True)
    with caplog.at_level(logging.INFO, logger="server.automations.autopilot_decider"):
        d = await decide(_ctx(), _config_dir="", _cwd="/tmp")
    assert d.action == "skip"
    assert d.waiting_reason == "credentials_pending"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("credential" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_helper_account_outside_the_renewal_window_still_escalates(monkeypatch, caplog):
    import logging

    import server.automations.autopilot_decider as mod

    monkeypatch.setattr(mod, "helper_credentials_pending", lambda: False)
    with caplog.at_level(logging.WARNING, logger="server.automations.autopilot_decider"):
        d = await decide(_ctx(), _config_dir="", _cwd="/tmp")
    assert (d.action, d.reason) == ("escalate", "decider-skipped:no-helper-account")
    assert any("no helper account" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


@pytest.mark.parametrize("kind,level", [
    ("cli_startup_timeout", "INFO"), ("rate_limit", "INFO"), ("network", "INFO"),
    ("other", "WARNING"), (None, "WARNING"),
])
def test_sub_threshold_failure_logs_at_the_kinds_level(caplog, kind, level):
    import logging
    from server.automations import autopilot_decider as decider_mod

    decider_mod._consecutive_decide_failures = 0
    try:
        with caplog.at_level(logging.INFO, logger="server.automations.autopilot_decider"):
            decider_mod._note_decide_failure(RuntimeError("x"), kind)
        assert [r.levelname for r in caplog.records] == [level]
    finally:
        decider_mod._consecutive_decide_failures = 0


def test_external_kind_still_escalates_to_error_at_threshold(caplog):
    import logging
    from server.automations import autopilot_decider as decider_mod

    decider_mod._consecutive_decide_failures = 0
    try:
        with caplog.at_level(logging.INFO, logger="server.automations.autopilot_decider"):
            for _ in range(decider_mod._ESCALATE_FAILURE_THRESHOLD):
                decider_mod._note_decide_failure(RuntimeError("x"), "cli_startup_timeout")
        assert caplog.records[-1].levelno == logging.ERROR
    finally:
        decider_mod._consecutive_decide_failures = 0
