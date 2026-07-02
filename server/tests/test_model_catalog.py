from server.sessions import model_catalog as mc


def test_aliases_mirror_the_binary_model_picker():
    # A true 1-to-1 map of the `claude` /model picker (verified against v2.1.197):
    # every offered alias, in picker order, plus the "default" row.
    assert mc.ALIASES == [
        "default",
        "sonnet",
        "opus",
        "haiku",
        "fable",
        "best",
        "sonnet[1m]",
        "opus[1m]",
        "fable[1m]",
        "opusplan",
    ]


def test_catalog_returns_id_label_dicts():
    cat = mc.catalog()
    assert {"id": "opus", "label": "Opus"} in cat
    assert {"id": "fable", "label": "Fable"} in cat
    assert {"id": "opus[1m]", "label": "Opus (1M context)"} in cat
    assert all(set(e) == {"id", "label"} for e in cat)


def test_catalog_order_matches_aliases():
    assert [e["id"] for e in mc.catalog()] == mc.ALIASES


def test_is_known_alias():
    assert mc.is_known_alias("opus") is True
    assert mc.is_known_alias("fable") is True
    assert mc.is_known_alias("opus[1m]") is True          # 1M-context beta variant
    assert mc.is_known_alias("best") is True
    assert mc.is_known_alias("claude-haiku-4-5") is False  # pinned ids are free-text, not aliases
