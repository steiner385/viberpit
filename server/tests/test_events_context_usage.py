from server.sessions import events as ev


def test_context_usage_to_wire():
    e = ev.ContextUsage(used_tokens=65729, max_tokens=200000, percentage=32.9,
                        model="claude-opus-4-8")
    assert e.to_wire() == {
        "type": "contextUsage",
        "usedTokens": 65729,
        "maxTokens": 200000,
        "percentage": 32.9,
        "model": "claude-opus-4-8",
    }


def test_context_usage_model_optional():
    e = ev.ContextUsage(used_tokens=10, max_tokens=200000, percentage=0.0)
    assert e.to_wire()["model"] is None
