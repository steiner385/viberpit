"""Tests for the AI title generator (one-shot claude-agent-sdk query)."""
import asyncio
from types import SimpleNamespace

from server.sessions.title_generator import generate_title

# generate_title now shares an in-memory per-account circuit breaker; the shared
# conftest _reset_helper_call_breakers fixture clears it around every test so the
# 401/429 cases here (which open the lane) don't leak into later tests.


def _assistant(text: str):
    """A fake AssistantMessage: an object with a .content list of text blocks."""
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _fake_query(messages, *, capture=None):
    """Return a query_fn yielding *messages*; records kwargs into *capture*."""
    async def query_fn(*, prompt, options=None, **_):
        if capture is not None:
            capture["prompt"] = prompt
            capture["options"] = options
        for m in messages:
            yield m
    return query_fn


def _run(prompts, **kw):
    return asyncio.run(
        generate_title(prompts, config_dir="/cfg/acct1", cwd="/tmp/x", **kw)
    )


def test_returns_assistant_text_as_title():
    qf = _fake_query([_assistant("Fix the login bug")])
    assert _run(["fix login"], query_fn=qf) == "Fix the login bug"


def test_strips_wrapping_quotes_and_newlines():
    qf = _fake_query([_assistant('  "Refactor parser"\n')])
    assert _run(["refactor"], query_fn=qf) == "Refactor parser"


def test_collapses_internal_whitespace():
    qf = _fake_query([_assistant("Add   dark    mode\ntoggle")])
    assert _run(["dark mode"], query_fn=qf) == "Add dark mode toggle"


def test_clamps_to_60_chars():
    qf = _fake_query([_assistant("x" * 200)])
    out = _run(["long"], query_fn=qf)
    assert out is not None and len(out) <= 60


def test_clamps_at_word_boundary():
    full = "Add dark mode toggle and persist the user preference across many sessions"
    qf = _fake_query([_assistant(full)])
    out = _run(["x"], query_fn=qf)
    assert out is not None and len(out) <= 60
    # the cut lands on a word boundary, not mid-word
    assert full.startswith(out)
    assert len(full) == len(out) or full[len(out)] == " "


def test_empty_output_returns_none():
    qf = _fake_query([_assistant("   ")])
    assert _run(["nothing"], query_fn=qf) is None


def test_no_assistant_message_returns_none():
    qf = _fake_query([SimpleNamespace(data={"subtype": "init"})])  # system-like, no .content list
    assert _run(["x"], query_fn=qf) is None


def test_empty_prompts_returns_none_without_querying():
    called = {"n": 0}

    async def qf(*, prompt, options=None, **_):
        called["n"] += 1
        if False:
            yield None

    assert _run([], query_fn=qf) is None
    assert called["n"] == 0


def test_query_raising_returns_none():
    def qf(*, prompt, options=None, **_):
        raise RuntimeError("boom")

    assert _run(["x"], query_fn=qf) is None


def test_salvages_title_when_query_raises_after_yielding():
    # The claude binary can emit a result with is_error=True but subtype
    # "success", which the SDK surfaces as
    # "Claude Code returned an error result: success" *after* already yielding
    # the assistant's title. Best-effort titling must keep the text we got.
    def qf(*, prompt, options=None, **_):
        async def gen():
            yield _assistant("Fix the login bug")
            raise RuntimeError("Claude Code returned an error result: success")

        return gen()

    assert _run(["x"], query_fn=qf) == "Fix the login bug"


def _result_msg(api_error_status: int):
    """Fake ResultMessage with api_error_status set (HTTP error case)."""
    return SimpleNamespace(api_error_status=api_error_status, is_error=True, subtype="success")


def test_discards_error_text_on_http_api_error():
    # When the Claude binary fails due to a 401/429/5xx HTTP error, it emits
    # an AssistantMessage whose text IS the error string. The ResultMessage
    # carries api_error_status != None. We must return None rather than
    # persisting an error string like "Failed to authenticate. API Error: 401"
    # as the session name.
    def qf(*, prompt, options=None, **_):
        async def gen():
            yield _assistant("Failed to authenticate. API Error: 401 Invalid authentication credentials")
            yield _result_msg(401)
            raise RuntimeError("Claude Code returned an error result: success")

        return gen()

    assert _run(["x"], query_fn=qf) is None


def test_discards_error_text_on_rate_limit():
    # Same guard for 429 rate-limit errors.
    def qf(*, prompt, options=None, **_):
        async def gen():
            yield _assistant("Rate limit exceeded. API Error: 429")
            yield _result_msg(429)
            raise RuntimeError("Claude Code returned an error result: success")

        return gen()

    assert _run(["x"], query_fn=qf) is None


def test_salvages_when_result_has_no_api_error_status():
    # A non-HTTP process exit (e.g. permission denied, tool error) sets
    # api_error_status=None — in that case partial assistant text is a valid title.
    def qf(*, prompt, options=None, **_):
        async def gen():
            yield _assistant("Partial title text")
            yield SimpleNamespace(api_error_status=None, is_error=True, subtype="success")
            raise RuntimeError("Claude Code returned an error result: success")

        return gen()

    assert _run(["x"], query_fn=qf) == "Partial title text"


def test_passes_model_and_config_dir_through_options():
    capture: dict = {}
    qf = _fake_query([_assistant("Title")], capture=capture)
    _run(["p1"], model="haiku", query_fn=qf)
    opts = capture["options"]
    assert opts.model == "haiku"
    assert opts.env.get("CLAUDE_CONFIG_DIR") == "/cfg/acct1"
    # A tool-free text query must NOT cap turns at 1 — the agent SDK treats
    # hitting max_turns as an error result, which would lose the reply.
    assert opts.max_turns != 1
    # the prompt frames the messages as material to summarize into a title,
    # not a task to perform — and embeds the user prompts
    body = capture["prompt"].lower()
    assert "title" in body
    assert "p1" in capture["prompt"]
