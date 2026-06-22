import asyncio
from server.sessions.events import Suggestions
from server.sessions.suggestions import build_prompt, parse_suggestions, SuggestionService, SUGGEST_SYSTEM_PROMPT
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


def test_suggestions_to_wire():
    ev = Suggestions(session_id="s1", turn_id="t1", items=["Run the tests", "Commit it"])
    assert ev.to_wire() == {
        "type": "suggestions",
        "sessionId": "s1",
        "turnId": "t1",
        "items": ["Run the tests", "Commit it"],
    }


def test_parse_plain_json_array():
    assert parse_suggestions('["Run tests", "Commit it"]') == ["Run tests", "Commit it"]


def test_parse_tolerates_surrounding_prose_and_fences():
    raw = 'Here you go:\n```json\n["A", "B"]\n```\nHope that helps!'
    assert parse_suggestions(raw) == ["A", "B"]


def test_parse_filters_nonstrings_and_blanks_and_truncates():
    raw = '["a", "", "  ", 5, null, "b", "c", "d", "e"]'
    assert parse_suggestions(raw, limit=4) == ["a", "b", "c", "d"]


def test_parse_garbage_returns_empty():
    assert parse_suggestions("not json at all") == []
    assert parse_suggestions("") == []
    assert parse_suggestions('{"not": "a list"}') == []


def test_build_prompt_includes_context():
    p = build_prompt("User: hi\n\nAssistant: hello")
    assert "User: hi" in p and "Assistant: hello" in p


def test_generate_parses_streamed_json():
    msgs = [f.stream_start(uuid="t1"),
            f.stream_text('["Run the tests", "Commit it"]', uuid="t1"),
            f.result(uuid="t1")]
    svc = SuggestionService(lambda cwd, cfg: FakeSDKClient(msgs))
    out = asyncio.run(svc.generate("/tmp/r", "/tmp/cfg", "User: hi\n\nAssistant: done"))
    assert out == ["Run the tests", "Commit it"]


def test_generate_empty_context_skips():
    called = {"n": 0}

    def factory(cwd, cfg):
        called["n"] += 1
        return FakeSDKClient([])

    svc = SuggestionService(factory)
    assert asyncio.run(svc.generate("/tmp/r", "/tmp/cfg", "   ")) == []
    assert called["n"] == 0  # never built a client


def test_generate_swallows_errors():
    def boom(cwd, cfg):
        raise RuntimeError("sdk down")

    svc = SuggestionService(boom)
    assert asyncio.run(svc.generate("/tmp/r", "/tmp/cfg", "User: hi")) == []


def test_real_suggestion_factory_builds_isolated_oneshot(monkeypatch):
    import server.sessions.sdk_factory as sf

    captured = {}

    class _FakeClient:
        def __init__(self, options=None):
            captured["options"] = options

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setenv("CCSWITCH_SUGGEST_MODEL", "claude-haiku-4-5")

    sf.real_suggestion_factory("/tmp/r", "/tmp/cfg-acct")
    opts = captured["options"]
    assert opts.cwd == "/tmp/r"
    assert opts.env["CLAUDE_CONFIG_DIR"] == "/tmp/cfg-acct"
    assert opts.resume is None
    assert opts.max_turns == 1
    assert opts.model == "claude-haiku-4-5"
    assert opts.system_prompt == SUGGEST_SYSTEM_PROMPT


def test_real_suggestion_factory_empty_model_env_uses_account_default(monkeypatch):
    import server.sessions.sdk_factory as sf

    captured = {}

    class _FakeClient:
        def __init__(self, options=None):
            captured["options"] = options

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setenv("CCSWITCH_SUGGEST_MODEL", "")

    sf.real_suggestion_factory("/tmp/r", "/tmp/cfg-acct")
    assert captured["options"].model is None


def test_last_turn_context_extracts_latest_user_and_following_assistant():
    from server.sessions import events as ev
    from server.sessions.suggestions import last_turn_context
    evs = [
        ev.UserMessage("t1", "first question"),
        ev.TextDelta("t1", "old answer"),
        ev.UserMessage("t2", "do the thing"),
        ev.TextDelta("t2", "Sure, "),
        ev.TextDelta("t2", "done."),
    ]
    assert last_turn_context(evs) == ("do the thing", "Sure, done.")


def test_last_turn_context_empty_when_no_user_message():
    from server.sessions.suggestions import last_turn_context
    assert last_turn_context([]) == ("", "")
    from server.sessions import events as ev
    assert last_turn_context([ev.TextDelta("t1", "orphan")]) == ("", "")


def test_kick_idle_suggestions_passes_extracted_context():
    from server.sessions import events as ev
    from server.sessions.suggestions import kick_idle_suggestions

    class _SpyDriver:
        def __init__(self): self.seeded = None
        def seed_idle_suggestions(self, user_text, assistant_text):
            self.seeded = (user_text, assistant_text)

    d = _SpyDriver()
    kick_idle_suggestions(d, [ev.UserMessage("t1", "ship it"), ev.TextDelta("t1", "On it.")])
    assert d.seeded == ("ship it", "On it.")


def test_kick_idle_suggestions_blank_events_still_calls_driver():
    from server.sessions.suggestions import kick_idle_suggestions

    class _SpyDriver:
        def __init__(self): self.seeded = None
        def seed_idle_suggestions(self, user_text, assistant_text):
            self.seeded = (user_text, assistant_text)

    d = _SpyDriver()
    kick_idle_suggestions(d, [])
    assert d.seeded == ("", "")
