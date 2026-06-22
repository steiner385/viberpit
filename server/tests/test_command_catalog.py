from server.sessions.commands.catalog import classify, build_catalog


def test_classify_buckets():
    assert classify("help") == "client"
    assert classify("context") == "client"
    assert classify("model") == "control"
    assert classify("clear") == "control"
    assert classify("review") == "passthrough"   # unknown → custom/passthrough


def test_build_catalog_normalizes_and_filters():
    info = {"commands": [
        {"name": "help", "description": "List commands"},
        {"name": "/model", "description": "Switch model", "argumentHint": "<model>"},
        {"name": "review", "description": "Review a PR", "argumentHint": "<pr#>"},
        {"name": "login", "description": "Sign in"},          # excluded (interactive)
        "barecmd",                                             # string form tolerated
        {"description": "no name"},                            # dropped (no name)
    ]}
    cat = build_catalog(info)
    by = {c.name: c for c in cat}
    assert "login" not in by                                  # filtered
    assert by["model"].kind == "control"
    assert by["model"].argument_hint == "<model>"
    assert by["help"].kind == "client"
    assert by["review"].kind == "passthrough"
    assert by["barecmd"].kind == "passthrough"
    assert by["model"].to_wire() == {
        "name": "model", "description": "Switch model",
        "argumentHint": "<model>", "kind": "control",
    }


def test_build_catalog_handles_none():
    assert build_catalog(None) == []
    assert build_catalog({}) == []
